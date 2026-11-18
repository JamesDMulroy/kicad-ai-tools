"""Run any editor action by name through a hotkey, for frames whose API has no RunAction.

The schematic editor (and the other non-board frames) register no RunAction handler, but every action
can carry a hotkey, and a GUI session's profile is private: this module writes that profile's
``user.hotkeys`` so a batch of actions each get their own plain key (every other action gets none, so no
default can collide), starts the editor, and posts the key to its canvas. Posted messages carry no
modifier state, so only plain keys are used and actions go in batches.

A keystroke is not acknowledged the way RunAction is, so ``dispatch`` reports what it observed: a window
the action opened, a change in the document (a hash of all items, read through the API), or a change in
the canvas (a capture of the editor window before and after, which works on a private desktop).
"""
from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
from pathlib import Path
import time

from kicad_mcp.ipc import api

# Plain keys only (posted messages carry no Ctrl/Shift/Alt): F1..F24, A..Z, 0..9.
# F1 and F10 are left out: wx and Windows answer them (help, menu-bar activation) on their own.
KEYS: list[tuple[str, int]] = ([(f"F{i}", 0x6F + i) for i in range(1, 25) if i not in (1, 10)]
                               + [(chr(c), c) for c in range(ord("A"), ord("Z") + 1)]
                               + [(chr(c), c) for c in range(ord("0"), ord("9"))])
# Never bound to anything: pressing it is the control for a pixel-only effect (see ``window_is_quiet``).
CONTROL_KEY: tuple[str, int] = ("9", ord("9"))
WM_KEYDOWN, WM_KEYUP, WM_CLOSE = 0x0100, 0x0101, 0x0010
VK_ESCAPE = 0x1B
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
# Per-monitor DPI aware, so window rectangles are real pixels: a DPI-unaware process gets scaled-down
# rectangles on a high-DPI display, and its captures would miss most of the editor window.
user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
user32.GetParent.restype = wt.HWND  # PER_MONITOR_AWARE_V2; no-op if already set


def write_hotkeys(config_dir: Path, all_actions: list[str], batch: list[str]) -> dict[str, tuple[str, int]]:
    """Give each action of ``batch`` its own plain key and every other action none; returns the map."""
    if len(batch) > len(KEYS):
        raise ValueError(f"a batch holds at most {len(KEYS)} actions")
    assigned = dict(zip(batch, KEYS))
    lines = [f"{name}\t{assigned[name][0] if name in assigned else ''}\t" for name in all_actions]
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "user.hotkeys").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return assigned


class _Rect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _children(hwnd: int) -> list[tuple[int, str, int, bool]]:
    found: list[tuple[int, str, int, bool]] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def callback(child, _):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, name, 64)
        rect = _Rect()
        user32.GetWindowRect(child, ctypes.byref(rect))
        found.append((int(child), name.value, (rect.right - rect.left) * (rect.bottom - rect.top),
                      bool(user32.IsWindowVisible(child))))
        return True

    user32.EnumChildWindows(hwnd, proc(callback), 0)
    return found


def canvas_of(session, title_part: str) -> tuple[int, int]:
    """(frame hwnd, canvas hwnd): the editor frame whose title contains ``title_part``, and its largest
    visible drawing child (KiCad's GAL canvas)."""
    frames = [w for w in session.kicad.desktop.windows()
              if w["pid"] == session.pid and w["visible"] and title_part in w["title"]]
    if not frames:
        raise RuntimeError(f"no '{title_part}' frame on the private desktop")
    frame = frames[0]["hwnd"]
    children = _children(frame)
    # The 3D viewer's keys go to its wxGLCanvas itself; the 2D editors' (whose OpenGL view is also a
    # wxGLCanvas) to the drawing panel around it.
    gl = [c for c in children if c[1] == "wxGLCanvas" and c[3]]
    if gl and _is_3d(frame):
        drawing = gl
    elif gl:  # the drawing panel is the OpenGL canvas's parent (the largest panel may be a library tree)
        biggest = max(gl, key=lambda c: c[2])[0]
        drawing = [(user32.GetParent(biggest), "wxWindowNR", 1, True)]
    else:
        drawing = [c for c in children if c[1] == "wxWindowNR" and c[3]]
    # A frame without a drawing panel (the footprint assigner) dispatches hotkeys from its lists.
    lists = [c for c in children if c[1] == "SysListView32" and c[3]]
    if drawing:
        return frame, max(drawing, key=lambda c: c[2])[0]
    return frame, max(lists, key=lambda c: c[2])[0] if lists else frame


# The 3D viewer's canvas never renders two frames exactly alike (measured idle: up to 0.014% of the
# window's pixels differ; its smallest real effect on the canvas, a model-visibility toggle, changes
# 0.086%). Inside a wxGLCanvas pixels are compared with this tolerance (a share of the canvas); everywhere
# else in the window, panels and toolbars included, they must match exactly, except the status bar of a
# GL window, whose "last render time" readout changes on every repaint.
GL_TOLERANCE = 0.0005
_SAMPLE = 3  # inside a GL canvas, compare every 3rd pixel of every 3rd row


class Shot:
    """A window's rendered pixels; ``==`` is exact, except inside an OpenGL canvas (``gl``: its
    left, top, right, bottom in window pixels), compared with ``GL_TOLERANCE``, and the rows of that
    window's status bar (``skip``: top, bottom), which are not compared."""

    def __init__(self, raw: bytes, width: int, height: int, gl: tuple[int, int, int, int] | None,
                 skip: tuple[int, int] | None = None):
        self.raw, self.width, self.height, self.gl, self.skip = raw, width, height, gl, skip
        self.digest = hashlib.sha256(raw).hexdigest()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Shot):
            return NotImplemented
        if self.digest == other.digest:
            return True
        if (self.width, self.height, self.gl) != (other.width, other.height, other.gl) or self.gl is None:
            return False
        left, top, right, bottom = self.gl
        row = 4 * self.width
        changed = total = 0
        for y in range(self.height):
            mine, theirs = self.raw[y * row:(y + 1) * row], other.raw[y * row:(y + 1) * row]
            if self.skip and self.skip[0] <= y < self.skip[1]:
                continue
            if mine == theirs:
                if top <= y < bottom and (y - top) % _SAMPLE == 0:
                    total += len(range(left, right, _SAMPLE))
                continue
            if not top <= y < bottom:
                return False  # outside the canvas: exact
            if mine[:4 * left] != theirs[:4 * left] or mine[4 * right:] != theirs[4 * right:]:
                return False
            if (y - top) % _SAMPLE:
                continue
            for x in range(4 * left, 4 * right, 4 * _SAMPLE):
                total += 1
                if any(abs(mine[x + c] - theirs[x + c]) > 24 for c in range(3)):
                    changed += 1
        return changed <= GL_TOLERANCE * max(1, total)

    __hash__ = None  # tolerant equality cannot be hashed consistently


def _is_3d(hwnd: int) -> bool:
    title = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, title, 256)
    return "3D Viewer" in title.value


def _gl_rects(hwnd: int, window: _Rect) -> tuple[tuple[int, int, int, int] | None, tuple[int, int] | None]:
    """(the 3D canvas rectangle, the status bar's rows) in window pixels, for a 3D viewer window. The 2D
    editors' OpenGL canvases render deterministically and are compared exactly."""
    if not _is_3d(hwnd):
        return None, None
    found: dict[str, tuple[int, int, int, int]] = {}
    for child, klass, _area, visible in _children(hwnd):
        if klass in ("wxGLCanvas", "msctls_statusbar32") and visible and klass not in found:
            rect = _Rect()
            user32.GetWindowRect(child, ctypes.byref(rect))
            found[klass] = (rect.left - window.left, rect.top - window.top, rect.right - window.left,
                            rect.bottom - window.top)
    gl = found.get("wxGLCanvas")
    status = found.get("msctls_statusbar32")
    return gl, (status[1], status[3]) if gl and status else None


def settled_capture(hwnd: int, timeout_s: float = 3.0, interval_s: float = 0.25) -> Shot:
    """The window's pixels once a repaint in progress has finished (three captures in a row agree)."""
    previous, agreed = capture(hwnd), 0
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline and agreed < 2:
        time.sleep(interval_s)
        current = capture(hwnd)
        agreed = agreed + 1 if current == previous else 0
        previous = current
    return previous


def capture(hwnd: int) -> Shot:
    """The window's rendered pixels (PrintWindow, which works on a private desktop)."""
    rect = _Rect()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    gl, skip = _gl_rects(hwnd, rect)
    width, height = max(1, rect.right - rect.left), max(1, rect.bottom - rect.top)
    screen = user32.GetDC(hwnd)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    old = gdi32.SelectObject(memory, bitmap)
    try:
        user32.PrintWindow(hwnd, memory, 2)  # PW_RENDERFULLCONTENT
        buffer = ctypes.create_string_buffer(width * height * 4)
        gdi32.GetBitmapBits(bitmap, len(buffer), buffer)
        return Shot(buffer.raw, width, height, gl, skip)
    finally:
        gdi32.SelectObject(memory, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, screen)


def document_hash(session) -> str | None:
    """A hash of every item in the session's document, read through the API (None if unavailable)."""
    try:
        items = api.call(session, "GetItems", {"types": []})["response"].get("items", [])
    except Exception:  # noqa: BLE001 - an editor busy in a dialog cannot answer; that is not a change
        return None
    return hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", _Rect)]


def focused(hwnd: int) -> int:
    """The control holding keyboard focus in ``hwnd``'s thread (a frame without a canvas routes keys
    through it), or ``hwnd`` itself when the thread reports none."""
    thread = user32.GetWindowThreadProcessId(hwnd, None)
    info = _GuiThreadInfo()
    info.cbSize = ctypes.sizeof(_GuiThreadInfo)
    if thread and user32.GetGUIThreadInfo(thread, ctypes.byref(info)) and info.hwndFocus:
        return int(info.hwndFocus)
    return hwnd


def press(hwnd: int, vk: int) -> None:
    user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 0x00000001)
    user32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001)


def _visible(session) -> dict[int, str]:
    pids = {session.pid, *session.process.pids()}
    return {w["hwnd"]: w["title"] for w in session.kicad.desktop.windows() if w["visible"] and w["pid"] in pids}


def select(session, item_id: str | None) -> None:
    """Select one item through the API (an action that works on the selection then has something)."""
    with contextlib.suppress(Exception):
        api.call(session, "ClearSelection", {})
        if item_id:
            api.call(session, "AddToSelection", {"items": [{"value": item_id}]})


def dispatch(session, canvas: int, vk: int, frame: int | None = None, settle_s: float = 0.8) -> dict:
    """Press one key on the canvas; report what changed (windows, document, the whole editor window's
    pixels), then close what it opened and press Escape."""
    watched = frame or canvas
    windows_before = _visible(session)
    children_before = visible_children(watched)
    pixels_before = settled_capture(watched)
    document_before = document_hash(session)
    state_before = _board_state(session)
    press(focused(canvas) if frame is not None and canvas == frame else canvas, vk)
    time.sleep(settle_s)
    opened = [t or "(untitled)" for h, t in _visible(session).items() if h not in windows_before]
    pixels_after = capture(watched)
    state_after = _board_state(session)  # before Escape, which would clear the prepared selection
    for hwnd in [h for h in _visible(session) if h not in windows_before]:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(0.3)
    press(canvas, VK_ESCAPE)  # leave any interactive tool the action started
    time.sleep(0.2)
    document_after = document_hash(session)
    effects = []
    if opened:
        effects.append(f"opened {opened}")
    if document_before and document_after and document_before != document_after:
        effects.append("document changed")
    if pixels_before != pixels_after:
        effects.append("editor window changed")
    if state_before and state_after and state_before != state_after:
        effects.append("editor state changed")
    messages = new_messages(watched, children_before)
    return {"effects": judge(effects, messages), "messages": messages}


def window_is_quiet(session, canvas: int, frame: int | None = None, settle_s: float = 0.8) -> bool:
    """A key bound to nothing changes nothing: the control that makes a pixel-only effect count. (Pressing
    the action again is no control: an action that sets a fixed state, like a view preset, changes
    nothing the second time.)"""
    return not dispatch(session, canvas, CONTROL_KEY[1], frame, settle_s)["effects"]


def _board_state(session) -> str | None:
    """The board editor's state (active and visible layers, appearance, selection), or the schematic
    editor's selection, when the session has one."""
    if getattr(session, "kind", "") == "schematic":
        try:
            items = api.call(session, "GetSelection", {})["response"].get("items", [])
        except Exception:  # noqa: BLE001 - busy in a dialog
            return None
        return "selection:" + ",".join(sorted(i.get("id", {}).get("value", "") for i in items))
    if getattr(session, "kind", "") != "pcb":
        return None
    from kicad_mcp.coverage.live_actions import editor_state

    return editor_state(session)


# Keys KiCad binds to nothing by default, so a few slots need no blanking of other actions.
SLOT_KEYS: list[tuple[str, int]] = [(f"F{i}", 0x6F + i) for i in range(13, 25)]


class HotkeyRunner:
    """Run any action by name in a GUI session whose frame has no RunAction (the schematic editor).

    Twelve keys (F13..F24) are assigned to the most recently used actions. A hit is a keystroke; a miss
    saves the document to the candidate, reassigns the least recently used key, and restarts the editor
    on the candidate (undo history does not survive the restart; the document does)."""

    def __init__(self, session):
        self.session = session
        self.slots: dict[str, tuple[str, int]] = {}  # action -> key, oldest first

    def _config(self) -> Path:
        return self.session.profile / "config" / self.session._settings_version()

    def _assign(self, action: str) -> None:
        if len(self.slots) >= len(SLOT_KEYS):
            self.slots.pop(next(iter(self.slots)))
        used = {key for key, _ in self.slots.values()}
        self.slots[action] = next(k for k in SLOT_KEYS if k[0] not in used)
        lines = [f"{name}\t{key}\t" for name, (key, _) in self.slots.items()]
        self._config().mkdir(parents=True, exist_ok=True)
        (self._config() / "user.hotkeys").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def run(self, action: str, title_part: str = "Schematic Editor") -> dict:
        restarted = False
        if action in self.slots:
            self.slots[action] = self.slots.pop(action)  # most recently used goes last
        else:
            with contextlib.suppress(Exception):
                api.call(self.session, "SaveDocument", {})  # keep the work across the restart
            self._assign(action)
            self.session.restart()
            restarted = True
        frame, canvas = canvas_of(self.session, title_part)
        observed = dispatch(self.session, canvas, self.slots[action][1], frame)
        return {"key": self.slots[action][0], "editor_restarted": restarted, **observed}


# ---- menus: every frame's native menu bar, read and invoked like a click -------------------------------

WM_COMMAND = 0x0111
MF_BYPOSITION, MF_CHECKED = 0x0400, 0x0008


def normalize(text: str) -> str:
    """Menu or action label without accelerator marks, shortcut, ellipsis or bidi marks."""
    text = text.split("\t", 1)[0].replace("&", "")
    text = "".join(ch for ch in text if ch not in "‪‫‬‎‏")
    return text.replace("...", "").replace("…", "").strip().lower()


def menu_items(frame: int) -> list[dict]:
    """Every leaf of the frame's menu bar: path, normalized label, command id, checked state."""
    found: list[dict] = []

    def walk(menu: int, path: list[str]) -> None:
        for index in range(max(user32.GetMenuItemCount(menu), 0)):
            label = ctypes.create_unicode_buffer(256)
            user32.GetMenuStringW(menu, index, label, 256, MF_BYPOSITION)
            submenu = user32.GetSubMenu(menu, index)
            if submenu:
                walk(submenu, [*path, label.value])
            elif label.value:
                state = user32.GetMenuState(menu, index, MF_BYPOSITION)
                found.append({"path": [*path, label.value], "label": normalize(label.value),
                              "id": int(user32.GetMenuItemID(menu, index)), "menu": int(menu), "index": index,
                              "checked": bool(state & MF_CHECKED) if state != 0xFFFFFFFF else False})

    bar = user32.GetMenu(frame)
    if bar:
        walk(bar, [])
    return found


def checked(item: dict) -> bool:
    state = user32.GetMenuState(item["menu"], item["index"], MF_BYPOSITION)
    return bool(state & MF_CHECKED) if state != 0xFFFFFFFF else False


def dispatch_menu(session, frame: int, item: dict, canvas: int | None = None, settle_s: float = 1.0) -> dict:
    """Click a menu item (WM_COMMAND with its id); report what changed, including its check mark."""
    windows_before = _visible(session)
    children_before = visible_children(frame)
    pixels_before = settled_capture(frame)
    document_before = document_hash(session)
    was_checked = checked(item)
    user32.PostMessageW(frame, WM_COMMAND, item["id"], 0)
    time.sleep(settle_s)
    opened = [t or "(untitled)" for h, t in _visible(session).items() if h not in windows_before]
    pixels_after = capture(frame)
    now_checked = checked(item)
    for hwnd in [h for h in _visible(session) if h not in windows_before]:
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    time.sleep(0.3)
    press(canvas or frame, VK_ESCAPE)
    time.sleep(0.2)
    document_after = document_hash(session)
    effects = []
    if opened:
        effects.append(f"opened {opened}")
    if document_before and document_after and document_before != document_after:
        effects.append("document changed")
    if pixels_before != pixels_after:
        effects.append("editor window changed")
    if was_checked != now_checked:
        effects.append(f"check mark {'set' if now_checked else 'cleared'}")
    messages = new_messages(frame, children_before)
    return {"effects": judge(effects, messages), "messages": messages}


# ---- messages: an action that only shows "can't do that" has not done its job ---------------------------

def visible_children(frame: int) -> set[int]:
    return {hwnd for hwnd, _klass, _area, visible in _children(frame) if visible}


def window_text(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value


def new_messages(frame: int, before: set[int]) -> list[str]:
    """Text of child windows that became visible (KiCad's info bar shows refusals and notices)."""
    texts = []
    for hwnd in visible_children(frame) - before:
        own = window_text(hwnd)
        nested = [window_text(h) for h, _k, _a, v in _children(hwnd) if v and window_text(h)]
        text = " ".join(t for t in [own, *nested] if t).strip()
        if text:
            texts.append(text[:160])
    return texts


def judge(effects: list[str], messages: list[str]) -> list[str]:
    """Drop a window change that is only a message appearing: the message is reported, not counted."""
    if messages and effects == ["editor window changed"]:
        return []
    return effects
