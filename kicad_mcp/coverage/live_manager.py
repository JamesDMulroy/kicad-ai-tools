"""Live proof for the project manager's file drop (kicad.Control.viewDroppedGerbers).

Gerber and drill files dropped on the project manager are handed to the Gerber viewer: the manager collects them
and runs viewDroppedGerbers, which starts GerbView with the files on its command line. The drop is made inside the
session's own process (drop_hook: the WM_DROPFILES block is allocated there) with launch_hook attached, so the
start request is recorded; KiCad's own GerbView then opens on the private desktop. A dropped text file (an
extension the manager does not take) is the control.

    python -m kicad_mcp.coverage.live_manager --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import contextlib
import json
from pathlib import Path
import time

from kicad_mcp.coverage.drop_hook import DropHook
from kicad_mcp.coverage.launch_hook import LaunchHook
from kicad_mcp.coverage.live_hotkeys import _input_for, _start
from kicad_mcp.ipc import hotkeys

ACTION = "kicad.Control.viewDroppedGerbers"


def _gerbview_requests(hook: LaunchHook) -> list[dict]:
    return [r for r in hook.requests if "gerbview" in json.dumps(r).lower()]


@cleanup_after_call
def case_viewDroppedGerbers(inventory: dict, project: Path) -> dict:
    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-mgrdrop-"))
    gerber = _input_for("gerbview", project, scratch / "gerbers")
    files = sorted(p for p in gerber.parent.iterdir() if p.suffix.lower() in (".gbr", ".drl", ".gbrjob"))[:3]
    note = scratch / "note.txt"
    note.write_text("not a gerber", encoding="utf-8")
    document = _input_for("kicad", project, scratch / "project")
    session, _assigned, frame, _canvas = _start("kicad", document, [a["name"] for a in inventory["actions"]], [])
    row = {"action": ACTION, "outcome": "no_effect", "detail": ""}
    launch = dropper = None
    try:
        launch = LaunchHook(session.pid, str(session.app_root))
        if not launch.ready:
            row.update(outcome="error", detail=f"launch hook not installed ({launch.installed})")
            return row
        dropper = DropHook(session.pid)
        before = set(hotkeys._visible(session))
        dropper.drop(frame, [str(note)])
        time.sleep(3)
        plain = _gerbview_requests(launch)
        dropper.drop(frame, [str(p) for p in files])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not _gerbview_requests(launch):
            time.sleep(0.5)
        time.sleep(3)
        asked = _gerbview_requests(launch)
        windows = [t for h, t in hotkeys._visible(session).items() if h not in before]
        text = json.dumps(asked)
        named = [p.name for p in files if p.name in text]
        ok = not plain and bool(asked) and len(named) == len(files)
        row.update(outcome="ok" if ok else "no_effect",
                   detail=f"a dropped .txt: {len(plain)} GerbView start requests; {len(files)} Gerber/drill files dropped on the project "
                          f"manager: GerbView asked to start {len(asked)} time(s) naming {named}; new windows {windows}",
                   requests=asked[:2])
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        for hook in (dropper, launch):
            if hook is not None:
                with contextlib.suppress(Exception):
                    hook.close()
        session.close(discard=True)
    return row


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    rows = [case_viewDroppedGerbers(inventory, args.project)]
    result = {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
              "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "manager-drop",
              "passed": sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"}), "rows": rows}
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in rows:
        print(row["outcome"], row["action"], row["detail"][:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
