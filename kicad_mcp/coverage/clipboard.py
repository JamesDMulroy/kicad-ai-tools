"""Clipboard guard: verify actions that write the clipboard without losing the owner's text.

The owner approved this on 2026-10-14 on the condition that the current text is saved first and restored
within about two seconds, always (finally block). Only text can be restored; an image or other
format that was on the clipboard before is lost, which is why the guard records what it saw.
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import time

CF_UNICODETEXT = 13
CF_BITMAP = 2
CF_DIB = 8
CF_DIBV5 = 17
GMEM_MOVEABLE = 0x0002

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32.GetClipboardData.restype = wintypes.HANDLE
_u32.GetClipboardData.argtypes = [wintypes.UINT]
_u32.SetClipboardData.restype = wintypes.HANDLE
_u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_u32.OpenClipboard.argtypes = [wintypes.HWND]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_k32.GlobalAlloc.restype = wintypes.HGLOBAL
_k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]


def sequence_number() -> int:
    return int(_u32.GetClipboardSequenceNumber())


def formats() -> list[int]:
    out: list[int] = []
    if not _open():
        return out
    try:
        fmt = 0
        while True:
            fmt = _u32.EnumClipboardFormats(fmt)
            if not fmt:
                return out
            out.append(int(fmt))
    finally:
        _u32.CloseClipboard()


def _open(tries: int = 20) -> bool:
    for _ in range(tries):
        if _u32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def get_text() -> str | None:
    if not _open():
        raise RuntimeError("clipboard busy")
    try:
        handle = _u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _k32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _k32.GlobalUnlock(handle)
    finally:
        _u32.CloseClipboard()


def set_text(text: str | None) -> None:
    if not _open():
        raise RuntimeError("clipboard busy")
    try:
        _u32.EmptyClipboard()
        if text is None:
            return
        data = (text + "\0").encode("utf-16-le")
        handle = _k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = _k32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        _k32.GlobalUnlock(handle)
        _u32.SetClipboardData(CF_UNICODETEXT, handle)
    finally:
        _u32.CloseClipboard()


@dataclass
class Observation:
    """What the guarded action did to the clipboard."""

    before_sequence: int = 0
    before_formats: list[int] = field(default_factory=list)
    after_sequence: int = 0
    after_formats: list[int] = field(default_factory=list)
    text: str | None = None
    saved_text: str | None = None
    saved_ok: bool = False  # the owner's text was read, so it can be restored
    restored: bool | None = None  # None until the guard exits; True only after a verified restore

    @property
    def changed(self) -> bool:
        return self.after_sequence != self.before_sequence

    @property
    def has_image(self) -> bool:
        return any(f in (CF_BITMAP, CF_DIB, CF_DIBV5) for f in self.after_formats)

    @property
    def new_content(self) -> bool:
        """The clipboard changed and now holds an image or text that differs from the owner's text."""
        return self.changed and (self.has_image or bool(self.text and self.text != self.saved_text))

    def capture(self) -> None:
        """Call right after the action, before leaving the guard."""
        self.after_sequence = sequence_number()
        self.after_formats = formats()
        if CF_UNICODETEXT in self.after_formats:
            with contextlib.suppress(RuntimeError):
                self.text = get_text()

    def wait_for_change(self, timeout_s: float = 2.0) -> bool:
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            if sequence_number() != self.before_sequence:
                return True
            time.sleep(0.1)
        return sequence_number() != self.before_sequence


@contextlib.contextmanager
def preserved():
    """Save the clipboard text, yield an Observation, and always restore the text on exit.

    Nothing is restored (and nothing cleared) when the text could not be read; ``restored`` says
    whether the owner's text is back, verified by reading it again.
    """
    obs = Observation()
    try:
        obs.saved_text = get_text()
        obs.saved_ok = True
    except RuntimeError:
        obs.saved_ok = False
    obs.before_sequence = sequence_number()
    obs.before_formats = formats()
    try:
        yield obs
    finally:
        if not obs.after_sequence:
            with contextlib.suppress(Exception):
                obs.capture()
        obs.restored = False
        if not obs.saved_ok:
            pass
        elif sequence_number() == obs.before_sequence:
            obs.restored = True  # untouched
        else:
            with contextlib.suppress(Exception):
                set_text(obs.saved_text)
                if obs.saved_text is None:  # nothing textual to put back; only an empty clipboard is whole again
                    obs.restored = not obs.before_formats
                else:
                    obs.restored = get_text() == obs.saved_text
