"""Offline unit tests for the adaptive engine (stdlib unittest, synthetic in-memory models).

On Windows the package starts a whole-tree user-desktop guard when it is imported (before any test runs):
the same guard as the pytest session (``tests/conftest.py``) for this unittest tier, which launches
kicad-cli and KiCad's python. ``test_zz_desktop_guard`` runs last and fails if any process of this run
showed a window on the user's desktop or if the desktop could not be observed.
"""
import os
import sys
import threading
from pathlib import Path

GUARD = None

if os.name == "nt":
    _live = str(Path(__file__).resolve().parents[1] / "live")
    if _live not in sys.path:
        sys.path.insert(0, _live)
    import ctypes
    import hidden_kicad as _hk

    _kernel = ctypes.WinDLL("kernel32")
    _kernel.SetErrorMode(_kernel.GetErrorMode() | 0x0001 | 0x0002 | 0x8000)  # inherited by every launch

    class _Guard:
        def __init__(self, interval=0.05):
            self.scan, self.kill_tree = _hk.owner_desktop_scan, _hk.kill_tree  # bound now: monkeypatch-proof
            self.samples = self.unavailable = 0
            self.hits, self.killed, self.invisible = [], [], set()
            self.stop_event = threading.Event()
            self.thread = threading.Thread(target=self.run, args=(interval,), daemon=True, name="unittest-desktop-guard")
            self.thread.start()

        def sample(self):
            seen = self.scan([os.getpid()])
            self.samples += 1
            if seen is None:
                self.unavailable += 1
                return
            shown = [w for w in seen if w["visible"]]
            self.invisible.update(w["pid"] for w in seen if not w["visible"])  # e.g. pcbnew's hidden frame
            for window in shown:
                if window["pid"] != os.getpid() and window["pid"] not in self.killed:
                    self.killed.append(window["pid"])
                    self.kill_tree(window["pid"])
            self.hits.extend(shown)

        def run(self, interval):
            while not self.stop_event.wait(interval):
                try:
                    self.sample()
                except Exception:  # a failed sample is a blind sample
                    self.unavailable += 1

        def record(self):
            self.sample()
            return {"samples": self.samples, "unavailable_samples": self.unavailable, "hits": self.hits[:10],
                    "killed": self.killed, "invisible_window_owners": len(self.invisible)}

    GUARD = _Guard()
