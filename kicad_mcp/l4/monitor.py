"""Owner-desktop guard for L4 runs (design B4 steps 0 and 1).

The decision logic (``task_windows_on_input_desktop``, ``descendant_pids``,
``foreground_attribution``, ``compare_wts``, ``build_verdict``) is pure and unit-tested on fake
window lists. The Win32 samplers and :class:`OwnerDesktopWatchdog` import ``hidden_desktop`` lazily.

Honest scope: invisibility is checked by polling the input desktop's top-level windows and the
foreground window (50 ms by default), not by a ``SetWinEventHook`` event stream, and the WTS console
session connect/lock state is a before/after snapshot, not a notification subscription. A window that
lives for less than one polling interval could be missed.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable


# ---- pure logic -------------------------------------------------------------------------------

def task_windows_on_input_desktop(windows: Iterable[dict], task_pids: Iterable[int]) -> list[dict]:
    """Windows of the task's process tree found on the user's input desktop (must be empty)."""
    pids = set(task_pids)
    return [w for w in windows if w.get("pid") in pids]


def descendant_pids(root_pids: Iterable[int], processes: Iterable[tuple[int, int]],
                    created: Callable[[int], int | None] | None = None) -> set[int]:
    """The roots plus every process reachable through parent links in ``(pid, parent pid)`` rows.

    With ``created`` (pid -> creation time, None if unknown), a child is followed only when it was created
    at or after its parent: a long-lived process whose real parent died keeps that parent's PID, and when
    Windows reuses the PID for one of ours, that stranger must not become "our" descendant (to be killed or
    blamed for a window). Unknown creation times are not followed."""
    children: dict[int, set[int]] = {}
    for pid, parent in processes:
        if pid != parent:
            children.setdefault(parent, set()).add(pid)
    found = set(root_pids)
    stack = list(found)
    while stack:
        parent = stack.pop()
        parent_created = created(parent) if created else None
        for child in children.get(parent, ()):
            if child in found:
                continue
            if created is not None:
                child_created = created(child)
                if parent_created is None or child_created is None or child_created < parent_created:
                    continue
            found.add(child)
            stack.append(child)
    return found


def foreground_attribution(samples: Iterable[dict], task_pids: Iterable[int]) -> dict:
    """Foreground changes and the samples whose foreground window belongs to the task.

    ``samples`` are ``{"t", "hwnd", "pid"}``. ``changes`` counts transitions between distinct
    windows (the owner's own activity is expected to change focus); only samples attributed to the
    task's process tree are violations.
    """
    pids = set(task_pids)
    ordered = list(samples)
    changes = sum(1 for earlier, later in zip(ordered, ordered[1:]) if earlier.get("hwnd") != later.get("hwnd"))
    attributed = [s for s in ordered if s.get("pid") in pids]
    return {"samples": len(ordered), "changes": changes, "attributed": attributed}


WTS_COMPARED_FIELDS = ("console_session_id", "connect_state", "lock_state")


def compare_wts(before: dict | None, after: dict | None) -> list[dict]:
    """Differences between two console-session snapshots; an unavailable snapshot is itself a difference."""
    if not before or not after or not before.get("ok") or not after.get("ok"):
        return [{"field": "snapshot_unavailable", "before": (before or {}).get("error", before and before.get("ok")),
                 "after": (after or {}).get("error", after and after.get("ok"))}]
    diffs = []
    for field in WTS_COMPARED_FIELDS:
        old, new = before.get(field), after.get(field)
        if field == "lock_state" and (old is None or new is None):
            continue  # lock state is optional; the connect state is not
        if old != new:
            diffs.append({"field": field, "before": old, "after": new})
    return diffs


def build_verdict(*, input_desktop_samples: int, window_hits: list[dict], foreground: dict,
                  wts_diffs: list[dict], extra_violations: list[dict] | None = None,
                  input_desktop_unavailable: int = 0) -> dict:
    """Aggregate the three guards. Zero samples means the monitor never saw the input desktop, and any sample
    where the input desktop could not be opened is a blind spot: both are failures (fail closed)."""
    violations: list[dict] = list(extra_violations or [])
    if input_desktop_samples <= 0:
        violations.append({"kind": "monitor_never_sampled_input_desktop"})
    if input_desktop_unavailable > 0:
        violations.append({"kind": "input_desktop_unobservable", "samples": input_desktop_unavailable})
    if window_hits:
        violations.append({"kind": "task_window_on_input_desktop", "windows": window_hits[:10],
                           "count": len(window_hits)})
    if foreground.get("attributed"):
        violations.append({"kind": "foreground_attributed_to_task", "samples": foreground["attributed"][:10],
                           "count": len(foreground["attributed"])})
    for diff in wts_diffs:
        violations.append({"kind": "wts_state_changed", **diff})
    return {"ok": not violations, "violations": violations,
            "input_desktop_samples": input_desktop_samples,
            "task_windows_on_input_desktop": len(window_hits),
            "foreground_changes_observed": foreground.get("changes", 0),
            "foreground_samples_attributed_to_task": len(foreground.get("attributed") or []),
            "wts_differences": wts_diffs}


class TaskPids:
    """Thread-safe set of PIDs this task owns (roots; descendants are derived from the process table)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pids: set[int] = set()
        self._owners: list = []  # objects with pids(): Job Object members are watched even if orphaned

    def add(self, pid: int) -> None:
        with self._lock:
            self._pids.add(int(pid))

    def add_owner(self, owner) -> None:
        """Also watch every member of ``owner.pids()`` (e.g. an OwnedProcess's Job Object)."""
        with self._lock:
            self._owners.append(owner)

    def snapshot(self) -> set[int]:
        with self._lock:
            pids, owners = set(self._pids), list(self._owners)
        for owner in owners:
            try:
                pids.update(owner.pids())
            except OSError:
                pass
        return pids


# ---- win32 samplers (Windows only) --------------------------------------------------------------

def _hd():
    from . import hidden_desktop

    return hidden_desktop


def input_desktop_windows() -> list[dict] | None:
    return _hd().input_desktop_windows()


def process_table() -> list[tuple[int, int]]:
    return _hd().process_table()


def pid_creation_time(pid: int) -> int | None:
    return _hd().pid_creation_time(pid)


def foreground_sample() -> dict:
    import ctypes
    from ctypes import wintypes as wt

    hd = _hd()
    hwnd = hd.user32.GetForegroundWindow()
    pid = wt.DWORD()
    if hwnd:
        hd.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return {"t": time.time(), "hwnd": int(hwnd or 0), "pid": pid.value or None}


def wts_snapshot() -> dict:
    """Console session id, connect state (WTSConnectState) and lock state (WTSSessionInfoEx flags)."""
    import ctypes
    import struct
    from ctypes import wintypes as wt

    kernel = ctypes.WinDLL("kernel32")
    wts = ctypes.WinDLL("wtsapi32")
    kernel.WTSGetActiveConsoleSessionId.restype = wt.DWORD
    wts.WTSQuerySessionInformationW.argtypes = [wt.HANDLE, wt.DWORD, ctypes.c_int,
                                                ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.DWORD)]
    wts.WTSQuerySessionInformationW.restype = wt.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    session = int(kernel.WTSGetActiveConsoleSessionId())
    snap: dict[str, Any] = {"ok": False, "console_session_id": session, "connect_state": None,
                            "lock_state": None, "raw_session_flags": None}
    if session == 0xFFFFFFFF:
        snap["error"] = "no_active_console_session"
        return snap

    def query(info_class: int) -> bytes | None:
        buffer, size = ctypes.c_void_p(), wt.DWORD()
        if not wts.WTSQuerySessionInformationW(None, session, info_class, ctypes.byref(buffer), ctypes.byref(size)):
            return None
        try:
            return ctypes.string_at(buffer.value, size.value)
        finally:
            wts.WTSFreeMemory(buffer)

    state = query(8)  # WTSConnectState: one int
    if state is not None and len(state) >= 4:
        snap["connect_state"] = struct.unpack_from("<i", state, 0)[0]
        snap["ok"] = True
    else:
        snap["error"] = "WTSConnectState query failed"
    # WTSSessionInfoEx: DWORD Level, 4 bytes padding, then WTSINFOEX_LEVEL1_W {ULONG SessionId;
    # int SessionState; LONG SessionFlags; ...}. SessionFlags 0 = locked, 1 = unlocked.
    extended = query(25)
    if extended is not None and len(extended) >= 20:
        flags = struct.unpack_from("<i", extended, 16)[0]
        snap["raw_session_flags"] = flags
        snap["lock_state"] = {0: "locked", 1: "unlocked"}.get(flags, "unknown")
    return snap


class OwnerDesktopWatchdog:
    """Background guard: kills (via ``on_violation``) the moment a task window or focus change is seen."""

    def __init__(self, pids: TaskPids, on_violation: Callable[[str, Any], None], interval: float = 0.05,
                 table_every: int = 5):
        self._pids = pids
        self._on_violation = on_violation
        self._interval = interval
        self._table_every = max(1, table_every)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="l4-owner-desktop-watchdog")
        self.samples = 0
        self.unavailable = 0
        self.window_hits: list[dict] = []
        self.foreground: list[dict] = []
        self._fired = False
        self._targets: set[int] = set()

    def start(self) -> None:
        self._thread.start()

    def _fire(self, kind: str, details: Any) -> None:
        if not self._fired:
            self._fired = True
            self._on_violation(kind, details)

    def _run(self) -> None:
        tick = 0
        table: list[tuple[int, int]] = []
        while not self._stop.is_set():
            try:
                if tick % self._table_every == 0:
                    table = process_table()
                self._targets = descendant_pids(self._pids.snapshot(), table, pid_creation_time)
                windows = input_desktop_windows()
                if windows is None:  # fail closed: a blind tripwire stops the task at once
                    self.unavailable += 1
                    self._fire("input_desktop_unobservable", {"samples": self.unavailable})
                else:
                    self.samples += 1
                    hits = task_windows_on_input_desktop(windows, self._targets)
                    if hits:
                        self.window_hits.extend(hits)
                        self._fire("task_window_on_input_desktop", hits)
                sample = foreground_sample()
                if not self.foreground or self.foreground[-1]["hwnd"] != sample["hwnd"]:
                    self.foreground.append(sample)
                    del self.foreground[:-500]
                if sample["pid"] in self._targets:
                    self._fire("foreground_attributed_to_task", sample)
            except Exception as exc:  # a sampling failure must never look like a clean run: stop the task
                self.unavailable += 1
                self._error = f"{type(exc).__name__}: {exc}"
                self._fire("watchdog_sampling_failed", self._error)
            tick += 1
            self._stop.wait(self._interval)

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=5)
        targets = self._targets | self._pids.snapshot()
        return {"input_desktop_samples": self.samples, "input_desktop_unavailable": self.unavailable,
                "window_hits": list(self.window_hits),
                "foreground": foreground_attribution(self.foreground, targets),
                "sampling_error": getattr(self, "_error", None)}
