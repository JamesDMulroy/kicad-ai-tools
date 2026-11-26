"""Shared live-test harness: everything a live test starts runs on a private desktop, and the whole
process tree of the test is watched for windows on the user's desktop. Results are written as evidence.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

ADAPTIVE = Path(__file__).resolve().parents[1]           # kicad_mcp/adaptive_editor
REPO = ADAPTIVE.parents[1]                               # repository root
sys.path.insert(0, str(ADAPTIVE / "live"))
sys.path.insert(0, str(ADAPTIVE))
import kicad_env  # noqa: E402
from hidden_kicad import (HiddenDesktop, HiddenKiCad, HiddenProcess, kill_tree, owner_desktop_scan,  # noqa: E402
                          owner_windows_of_tree, run_hidden)

__all__ = ["ADAPTIVE", "REPO", "HiddenDesktop", "HiddenKiCad", "HiddenProcess", "Watch", "build_fixture_board",
           "hidden_spawner", "kicad_env", "source_digest", "owner_desktop_scan", "owner_windows_of_tree", "redact", "run_hidden", "write_evidence"]

BUILD = r'''
import os, sys
sys.path.insert(0, sys.argv[2])
from tests import fixtures
fixtures.build(sys.argv[1], "live", outline="rect")
print(os.path.join(sys.argv[1], "live.kicad_pcb"))
'''


def build_fixture_board(work: Path) -> Path:
    """Build the synthetic fixture board with KiCad's python, hidden; returns the .kicad_pcb path."""
    script = work / "mk.py"
    script.write_text(BUILD, encoding="utf-8")
    out = run_hidden([str(kicad_env.kicad_python()), "-X", "utf8", "-B", str(script), str(work / "b"),
                      str(ADAPTIVE)], cwd=str(work), timeout_s=300)
    if out["exit_code"] != 0:
        raise RuntimeError(f"fixture build failed: {out['stderr'][-1500:]}")
    return Path(out["stdout"].strip().splitlines()[-1])


class Watch:
    """Continuous tripwire over this test's entire process tree (KiCad, workers, helpers, pip...): a
    background thread samples the user's input desktop every ``interval`` seconds for the whole run,
    including drags, applies and undos. Samples where the desktop could not be opened (locked) are counted,
    never treated as clean; ``close()`` returns the record for the evidence."""

    def __init__(self, interval: float = 0.05):
        self.hits: list = []
        self.samples = 0
        self.unavailable = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(interval,), daemon=True)
        self._thread.start()

    def _sample(self) -> None:
        try:
            seen = owner_desktop_scan([os.getpid()])
        except Exception as exc:  # a failed sample is a blind sample, never a clean one
            seen = None
            self.error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self.samples += 1
            if seen is None:
                self.unavailable += 1
            elif seen:
                self.hits.extend(seen)
        for window in seen or []:  # stop the offender at once, not at the next check()
            if window["pid"] != os.getpid():
                kill_tree(window["pid"])

    def _run(self, interval: float) -> None:
        while not self._stop.wait(interval):
            self._sample()  # never raises: failures are counted as blind samples

    def check(self) -> None:
        self._sample()
        with self._lock:
            hits = list(self.hits)
        if hits:
            raise RuntimeError(f"a window of this test reached the owner desktop: {hits[:3]}")

    def close(self) -> dict:
        self._stop.set()
        self._thread.join(5)
        self._sample()
        with self._lock:
            return {"samples": self.samples, "unavailable_samples": self.unavailable, "hits": list(self.hits),
                    "observed": self.samples > 0 and self.unavailable == 0}  # any blind sample fails


def hidden_spawner(desktop: HiddenDesktop):
    """``spawn(argv)`` for EngineWorker: the worker runs on ``desktop`` with text pipes."""
    return lambda argv: HiddenProcess(argv, desktop, stdin="pipe", stdout="pipe")


def source_digest() -> dict:
    """sha256 of every packaged source file that a live run exercises, so evidence names the bytes it ran."""
    import hashlib
    package = REPO / "kicad_mcp"
    files = sorted(p for p in package.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json", ".sha256"))
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(REPO).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return {"package_tree": digest.hexdigest(), "files": len(files)}


def write_evidence(name: str, result: dict) -> Path:
    """Write the result next to the repo's evidence log (KICAD_AI_TOOLS_EVIDENCE_DIR overrides)."""
    root = Path(os.environ.get("KICAD_AI_TOOLS_EVIDENCE_DIR", str(REPO / ".tmp" / "evidence")))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.json"
    payload = {"test": name, "kicad": kicad_env.describe(), **result}
    path.write_text(redact(json.dumps(payload, indent=1, default=str)), encoding="utf-8")
    return path


def redact(text: str) -> str:
    """Evidence is shareable: the user's home directory becomes ``~`` (raw and JSON-escaped forms)."""
    home = str(Path.home())
    for form in (home.replace("\\", "\\\\"), home, home.replace("\\", "/")):
        text = text.replace(form, "~")
    return text
