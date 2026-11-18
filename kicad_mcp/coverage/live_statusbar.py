"""Live proof for view and cursor actions read back from the board editor's status bar (UI Automation).

The status bar shows what a person reads after these actions: the zoom ("Z 10.92"), the cursor position and
its distance from the local origin ("dx ... dy ..."). The mouse is put on the canvas with posted moves and,
inside the session's own process, the cursor hook (KiCad re-reads the screen cursor on a key).

- undoZoom: zoom in, then undo; the zoom must return to its value before;
- resetLocalCoords: with the cursor away from the local origin (dx/dy not zero), the action's key puts the
  origin under the cursor (dx and dy become zero);
- cursorClick: with the cursor over a track and nothing selected, the action selects it (API selection).

    python -m kicad_mcp.coverage.live_statusbar --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import re
import tempfile
import time

from kicad_mcp.coverage.cursor_hook import CursorHook
from kicad_mcp.coverage.live_hotkeys import ZOOM_SELECTION, _first_of, _start
from kicad_mcp.ipc import api, hotkeys

WINDOW = {"name": "PCB Editor", "match": "contains"}
ZOOM_TOOL = "common.Control.zoomTool"
CASES = ["common.Control.undoZoom", "common.Control.resetLocalCoords", "common.Control.cursorClick", ZOOM_TOOL]
# interactive tools proven here by driving them on the canvas (registered as tool-driver routes in the gate)
TOOL_CASES = [ZOOM_TOOL]
ZOOM_IN = "common.Control.zoomInCenter"


def status(uia) -> dict[str, str]:
    texts = uia.call("state", {"window": WINDOW, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "Text"}})["controls"]
    out = {}
    for t in texts:
        name = t["name"]
        if re.match(r"^Z \d", name):
            out["zoom"] = name
        elif name.startswith("dx "):
            out["local"] = name
        elif name.startswith("X "):
            out["cursor"] = name
    return out


def _put_mouse(session, hook: CursorHook, canvas: int, x: int, y: int) -> None:
    for step in range(8, 0, -1):
        hotkeys.user32.PostMessageW(canvas, 0x0200, 0, ((y - 6 * step) << 16) | (x - 9 * step))
        time.sleep(0.08)
    hook.place(canvas, x, y)
    hotkeys.user32.PostMessageW(canvas, 0x0200, 0, (y << 16) | x)
    time.sleep(0.6)


@cleanup_after_call
def sweep(inventory: dict, project: Path) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    cases = [c for c in CASES if c in names]
    document = next(project.glob("*.kicad_pcb"))
    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-sb-")) / project.name
    import shutil

    shutil.copytree(project, scratch)
    session, assigned, _frame, canvas = _start("pcbnew", scratch / document.name, [a["name"] for a in inventory["actions"]],
                                               [*cases, ZOOM_IN])
    rows = []
    hook = CursorHook(session.pid)
    try:
        uia = session.attach_uia()
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(canvas, hotkeys.ctypes.byref(rect))
        cx, cy = rect.right // 2, rect.bottom // 2
        for name in cases:
            row = {"action": name, "outcome": "no_effect", "detail": ""}
            try:
                if name.endswith("undoZoom"):
                    # by key, as a person zooms: the zoom history records view changes made in the editor
                    _put_mouse(session, hook, canvas, cx, cy)
                    start = status(uia).get("zoom")
                    hotkeys.press(canvas, assigned[ZOOM_IN][1])
                    time.sleep(1.0)
                    zoomed = status(uia).get("zoom")
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(1.0)
                    back = status(uia).get("zoom")
                    row["zoom"] = [start, zoomed, back]
                    if start and zoomed != start and back == start:
                        row.update(outcome="ok", detail=f"zoom {start} -> {zoomed} (zoom in) -> {back} (undo)")
                elif name.endswith("resetLocalCoords"):
                    _put_mouse(session, hook, canvas, cx + 150, cy + 90)
                    before = status(uia)
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(0.8)
                    _put_mouse(session, hook, canvas, cx + 150, cy + 90)
                    after = status(uia)
                    row["status"] = [before, after]
                    zero = re.match(r"dx 0\.0+\s+dy 0\.0+", after.get("local", ""))
                    if zero and not re.match(r"dx 0\.0+\s+dy 0\.0+", before.get("local", "")):
                        row.update(outcome="ok", detail=f"local coordinates '{before.get('local')}' -> "
                                                        f"'{after.get('local')}' at {after.get('cursor')}")
                elif name == ZOOM_TOOL:
                    # the zoom tool zooms to the rectangle a person drags with it (from the whole-board view)
                    api.call(session, "RunAction", {"action": "common.Control.zoomFitScreen"})
                    time.sleep(1.0)
                    before = status(uia).get("zoom")
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(0.6)
                    x0, y0, x1, y1 = cx - 120, cy - 80, cx + 120, cy + 80
                    _put_mouse(session, hook, canvas, x0, y0)
                    hook.button(True)
                    hotkeys.user32.PostMessageW(canvas, 0x0201, 1, (y0 << 16) | x0)  # WM_LBUTTONDOWN
                    for step in range(1, 11):
                        px, py = x0 + (x1 - x0) * step // 10, y0 + (y1 - y0) * step // 10
                        hook.place(canvas, px, py)
                        hotkeys.user32.PostMessageW(canvas, 0x0200, 1, (py << 16) | px)  # MK_LBUTTON
                        time.sleep(0.06)
                    time.sleep(0.2)
                    hook.button(False)
                    hotkeys.user32.PostMessageW(canvas, 0x0202, 0, (y1 << 16) | x1)  # WM_LBUTTONUP
                    time.sleep(1.0)
                    after = status(uia).get("zoom")
                    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                    row["zoom"] = [before, after]
                    if before and after and before != after:
                        row.update(outcome="ok", detail=f"dragged a box with the zoom tool: zoom {before} -> {after}")
                elif name.endswith("cursorClick"):
                    track = _first_of(session, "KOT_PCB_TRACE")
                    hotkeys.select(session, track)
                    api.call(session, "RunAction", {"action": ZOOM_SELECTION})
                    time.sleep(1.0)
                    api.call(session, "ClearSelection", {})
                    _put_mouse(session, hook, canvas, cx, cy)
                    windows = set(hotkeys._visible(session))
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(1.0)
                    menus = [h for h in hotkeys._visible(session) if h not in windows]
                    if menus:  # several items under the cursor: KiCad asks which one, as for a real click
                        row["opened"] = [hotkeys.window_text(h) or "(menu)" for h in menus]
                        hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                        time.sleep(0.5)
                    selected = [i.get("id", {}).get("value") for i in
                                api.call(session, "GetSelection", {})["response"].get("items", [])]
                    row["selected"] = selected[:3]
                    if track in selected:
                        row.update(outcome="ok", detail="with the cursor over a track and nothing selected, the "
                                                        "action selected that track (API selection)")
                    elif menus:
                        row.update(outcome="ok", detail="the click at the cursor opened KiCad's selection "
                                                        f"clarification menu {row['opened']} (several items there)")
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(row["outcome"], name, row["detail"][:140], flush=True)
    finally:
        hook.close()
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "statusbar-uia", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
