"""Live sweep of editor actions through each frame's native menu bar.

Most user-facing actions are menu items labelled with the action's friendly name. For each frame this
sweep opens the frame on a private desktop, reads its menu bar (labels, command ids, check marks), and
for every not-yet-verified action whose friendly name matches a menu label, clicks it (WM_COMMAND with
the item's id, exactly what a click sends). An action counts as verified only with an observed effect:
a window it opened, a document change, a change in the editor window, or its check mark flipping.

    python -m kicad_mcp.coverage.live_menus --inventory inv.json --project <dir> --editor eeschema \
        --skip-passed ev1.json ev2.json --out menus.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import tempfile
import time

from kicad_mcp.coverage.live_actions import EXTERNAL
from kicad_mcp.coverage.live_hotkeys import (
    FRAMES,
    HOSTED,
    OPENER,
    SELECTIONS,
    _first_of,
    _input_for,
    open_hosted,
)
from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import FrameSession, GuiSession

FRAME_EXE = {"gerbview": "gerbview.exe", "plEditor": "pl_editor.exe", "kicad": "kicad.exe"}
# common.* actions appear in every frame's menus; each frame sweep also tries those.
SHARED_PREFIX = "common."


def _open(editor: str, document: Path, all_actions: list[str]):
    if editor in FRAME_EXE:
        session = FrameSession(document, FRAME_EXE[editor])
    else:
        session = GuiSession(document)
    assigned = {}
    if editor == "cvpcb":  # the footprint assigner opens from the schematic editor by a reserved key
        config = session.profile / "config" / session._settings_version()
        assigned = hotkeys.write_hotkeys(config, all_actions, [OPENER])
    session.start()
    if editor == "3DViewer":
        api.call(session, "RunAction", {"action": "common.Control.show3DViewer"})
        time.sleep(3)
    if editor == "cvpcb":
        _, canvas = hotkeys.canvas_of(session, "Schematic Editor")
        hotkeys.press(canvas, assigned[OPENER][1])
        time.sleep(4)
    if editor in HOSTED:  # opened the way a person would, with a library item open in a library editor
        try:
            open_hosted(session, editor)
        except BaseException:
            session.close(discard=True)
            raise
    frame, canvas = hotkeys.canvas_of(session, FRAMES[editor][1])
    return session, frame, canvas


def _clear_late_windows(session, baseline: dict[int, str], settle_s: float = 0.6) -> None:
    time.sleep(settle_s)
    for _ in range(3):
        extra = [h for h in hotkeys._visible(session) if h not in baseline]
        if not extra:
            return
        for hwnd in extra:
            hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
        time.sleep(0.5)


@cleanup_after_call
def sweep(inventory: dict, project: Path, editor: str, skip: set[str]) -> dict:
    document = _input_for(editor, project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-menus-")))
    all_actions = [a["name"] for a in inventory["actions"]]
    prefix = HOSTED[editor][2] if editor in HOSTED else f"{editor}."
    wanted = {a["name"]: hotkeys.normalize(a["friendly_name"]) for a in inventory["actions"]
              if (a["name"].startswith(prefix) or a["name"].startswith(SHARED_PREFIX)) and a["friendly_name"]
              and f"action:{a['name']}" not in skip and not EXTERNAL.match(a["name"].rsplit(".", 1)[-1])}
    rows: list[dict] = []
    session, frame, canvas = _open(editor, document, all_actions)
    baseline = hotkeys._visible(session)
    try:
        items = hotkeys.menu_items(frame)
        by_label: dict[str, dict] = {}
        for item in items:
            by_label.setdefault(item["label"], item)
        matched = [(name, by_label[label]) for name, label in wanted.items() if label in by_label]
        kinds = SELECTIONS.get("schematic", [None]) if editor == "eeschema" else [None]
        for name, item in matched:
            if not session.healthy():
                session.close(discard=True)
                session, frame, canvas = _open(editor, document, all_actions)
                baseline = hotkeys._visible(session)
                fresh = {i["label"]: i for i in hotkeys.menu_items(frame)}
                item = fresh.get(item["label"], item)
            row = {"action": name, "outcome": "no_effect", "menu": " > ".join(item["path"]), "detail": ""}
            _clear_late_windows(session, baseline)  # a window the previous click opened late is not this one's
            for kind in kinds:
                hotkeys.select(session, _first_of(session, kind))
                observed = hotkeys.dispatch_menu(session, frame, item, canvas)
                if observed["effects"] == ["editor window changed"] and not hotkeys.window_is_quiet(
                        session, canvas or frame, frame):  # pixels alone, and the window is noisy
                    observed = {"effects": [], "messages": observed["messages"]}
                if observed["effects"]:
                    row.update(outcome="ok", detail="; ".join(observed["effects"]), selected=kind or "nothing")
                    break
                if not session.healthy():
                    break
            hotkeys.select(session, None)
            rows.append(row)
    finally:
        session.close(discard=True)
    passed = sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "editor": editor, "menu_items": len(items),
            "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--editor", choices=sorted(FRAMES), required=True)
    parser.add_argument("--skip-passed", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    skip = {i for f in args.skip_passed for i in json.loads(f.read_text(encoding="utf-8")).get("passed", [])}
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.editor, skip)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps({"menu_items": result["menu_items"], **counts}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
