"""Release check: build the wheel and sdist with hatchling, verify what ships, and run pyflakes.

    python scripts/check_package.py

Needs ``hatchling`` and ``pyflakes`` in the running interpreter (a dev venv); nothing is installed for you.
Builds into a temporary folder and exits non-zero on any problem.
"""
from __future__ import annotations

import os
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WHEEL_MUST_HAVE = (
    "kicad_mcp/launcher.py",
    "kicad_mcp/setup.py",
    "kicad_mcp/workflow_server.py",
    "kicad_mcp/adaptive_editor/kicad_env.py",
    "kicad_mcp/adaptive_editor/plugin/plugin.json",
    "kicad_mcp/adaptive_editor/plugin/requirements.txt",
    "kicad_mcp/l4/registry/APPROVED.sha256",
    "kicad_mcp/l4/registry/ui-registry-10.0.5.json",
    "kicad_mcp/adaptive_editor/plugin/adaptive_drag.py",
    "kicad_mcp/adaptive_editor/plugin/adaptive_edge.py",
    "kicad_mcp/adaptive_editor/plugin/panel.py",
    "kicad_mcp/adaptive_editor/plugin/install.py",
    "kicad_mcp/utils/winproc.py",
    "kicad_mcp/coverage/live_actions.py",  # imported by the server's kicad_run_action tool
)
NEVER_SHIPPED = ("/tests/", "/tests_live/", "__pycache__", ".pyc", "/.tmp/", "/.venv")
# Dev tools kept in the source distribution only: the icon generator and the live coverage drivers' Frida hooks.
WHEEL_ONLY_EXCLUDED = ("make_icons.py", "_hook.py", "/coverage/live_external.py", "/coverage/gate.py")


def _icons() -> tuple[str, ...]:
    """Every plugin icon on disk must ship: KiCad shows them on the toolbar."""
    folder = ROOT / "kicad_mcp" / "adaptive_editor" / "plugin" / "icons"
    icons = tuple(p.relative_to(ROOT).as_posix() for p in sorted(folder.glob("*.png")))
    if not icons:
        raise SystemExit("no plugin icons found on disk")
    return icons


def _build(out: Path) -> tuple[Path, Path]:
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder
    wheel = Path(next(WheelBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])))
    sdist = Path(next(SdistBuilder(str(ROOT)).build(directory=str(out), versions=["standard"])))
    return wheel, sdist


def _check_listing(label: str, names: list[str], must_have: tuple[str, ...], never: tuple[str, ...]) -> list[str]:
    problems = [f"{label}: missing {m}" for m in must_have if not any(n.endswith(m) for n in names)]
    for name in names:
        probe = "/" + name
        problems += [f"{label}: ships {name}" for bad in never if bad in probe]
    return problems


def _pyflakes() -> list[str]:
    from pyflakes.api import checkRecursive
    from pyflakes.reporter import Reporter

    class Collect(Reporter):
        def __init__(self):
            self.lines: list[str] = []

        def unexpectedError(self, filename, msg):
            self.lines.append(f"{filename}: {msg}")

        def syntaxError(self, filename, msg, lineno, offset, text):
            self.lines.append(f"{filename}:{lineno}: {msg}")

        def flake(self, message):
            self.lines.append(str(message))

    reporter = Collect()
    checkRecursive([str(ROOT / "kicad_mcp"), str(ROOT / "scripts")], reporter)
    return reporter.lines


def main() -> int:
    problems: list[str] = []
    with tempfile.TemporaryDirectory(prefix="kicad-ai-tools-check-") as tmp:
        wheel, sdist = _build(Path(tmp))
        with zipfile.ZipFile(wheel) as zf:
            wheel_names = zf.namelist()
        with tarfile.open(sdist) as tf:
            sdist_names = [n.split("/", 1)[1] for n in tf.getnames() if "/" in n]
        problems += _check_listing(wheel.name, wheel_names, WHEEL_MUST_HAVE + _icons(),
                                   NEVER_SHIPPED + WHEEL_ONLY_EXCLUDED)
        problems += _check_listing(sdist.name, sdist_names, WHEEL_MUST_HAVE + ("README.md", "LICENSE"), NEVER_SHIPPED)
        print(f"{wheel.name}: {len(wheel_names)} files; {sdist.name}: {len(sdist_names)} files")
    flakes = _pyflakes()
    problems += [f"pyflakes: {line}" for line in flakes]
    for line in problems:
        print(line)
    print("package check:", "ok" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    os.chdir(ROOT)
    sys.exit(main())
