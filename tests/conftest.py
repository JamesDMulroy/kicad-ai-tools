"""Session-wide safety net for every test launch (Windows).

- Crash-quiet: the test process turns off critical-error, GP-fault and open-file dialogs for itself;
  every process a test starts (directly, through run_contained, a winproc job or a private desktop)
  inherits that error mode.
- Whole-tree tripwire: for the entire session a background thread samples the user's input desktop
  every 50 ms for windows owned by any process in this pytest process's tree (creation-time checked, so a
  stranger with a reused PID is never included). A visible window kills its exact owner process and fails
  the session (never-shown windows, such as the hidden frame and IME window KiCad's python creates when it
  imports pcbnew, are counted in the report). Samples where the desktop cannot be observed (locked screen,
  remote session) fail the session only if it launched a process; a session that launched nothing passes
  and reports "desktop not observable, no launches".
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

SEM_FLAGS = 0x0001 | 0x0002 | 0x8000  # FAILCRITICALERRORS | NOGPFAULTERRORBOX | NOOPENFILEERRORBOX


class SessionTripwire:
    def __init__(self, interval: float = 0.05, *, scan=None, kill_tree=None, children=None):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "kicad_mcp" / "adaptive_editor" / "live"))
        import hidden_kicad
        self._scan = scan if scan is not None else hidden_kicad.owner_desktop_scan
        self._kill_tree = kill_tree if kill_tree is not None else hidden_kicad.kill_tree
        self._children = children if children is not None else (
            lambda: hidden_kicad.tree_pids([os.getpid()]) - {os.getpid()})
        self.interval = interval
        self.samples = 0
        self.unavailable = 0
        self.launched = False
        self.hits: list[dict] = []
        self.killed: list[int] = []
        self.invisible: set[int] = set()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pytest-owner-desktop-tripwire")

    def start(self) -> None:
        self._thread.start()

    def _sample(self) -> None:
        if self._children():
            self.launched = True
        seen = self._scan([os.getpid()])
        self.samples += 1
        if seen is None:
            self.unavailable += 1
            return
        shown = [w for w in seen if w["visible"]]
        self.invisible.update(w["pid"] for w in seen if not w["visible"])
        for window in shown:
            if window["pid"] != os.getpid() and window["pid"] not in self.killed:
                self.killed.append(window["pid"])
                self._kill_tree(window["pid"])
        self.hits.extend(shown)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self._sample()
            except Exception as exc:
                self.unavailable += 1
                self.error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> dict:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(5)
        return {"samples": self.samples, "unavailable_samples": self.unavailable, "hits": self.hits[:10],
                "killed": self.killed, "launched": self.launched, "invisible_window_owners": len(self.invisible),
                "error": getattr(self, "error", None)}


def tripwire_verdict(record: dict) -> tuple[bool, str]:
    if record["hits"]:
        return False, "visible windows detected"
    blind = bool(record["unavailable_samples"] or record["samples"] == 0)
    if record["launched"] and blind:
        return False, "desktop not observable after launches"
    if blind:
        return True, "desktop not observable, no launches"
    return True, "owner desktop observed cleanly"


_TRIPWIRE: SessionTripwire | None = None


def pytest_sessionstart(session):
    global _TRIPWIRE
    if os.name != "nt":
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32")
    kernel.SetErrorMode(kernel.GetErrorMode() | SEM_FLAGS)
    _TRIPWIRE = SessionTripwire()
    _TRIPWIRE.start()


def pytest_sessionfinish(session, exitstatus):
    if _TRIPWIRE is None:
        return
    record = _TRIPWIRE.stop()
    out = os.environ.get("KICAD_AI_TOOLS_TRIPWIRE_RECORD")
    if out:
        import json
        Path(out).write_text(json.dumps(record, indent=1), encoding="utf-8")
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    line = (f"owner-desktop tripwire: {record['samples']} samples, {record['unavailable_samples']} blind, "
            f"{len(record['hits'])} visible window(s), {record['invisible_window_owners']} process(es) with "
            "never-shown windows")
    if reporter is not None:
        reporter.write_line(line)
    passed, verdict_line = tripwire_verdict(record)
    if record["unavailable_samples"] or record["samples"] == 0:
        if reporter is not None:
            reporter.write_line(verdict_line)
    if not passed:
        if reporter is not None:
            reporter.write_line(f"FAILED: the session tripwire did not observe a clean owner desktop: {record}")
        session.exitstatus = 3


