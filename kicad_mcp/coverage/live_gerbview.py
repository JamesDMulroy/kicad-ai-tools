"""Live proof for the Gerber viewer's layer and highlight actions, read back through UI Automation.

The Gerber viewer has no API. What a person sees is exposed to UI Automation instead: the layer manager
lists every loaded layer as "<n> <file> (<function>)" in display order, and the toolbar's boxes show the
active layer and the component, net, aperture attribute and D-code being highlighted. Each action's own
key (the session profile's hotkey) is pressed on the canvas and passes only when that readback changes.

Two sessions: the layer actions run on the whole board (its Gerber job file loads every layer); the
highlight actions on its front copper alone, where a click on a drawn pixel selects one item (with
several layers, items stack and the viewer asks which one in a popup menu).

    python -m kicad_mcp.coverage.live_gerbview --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import collections
import json
from pathlib import Path
import re
import tempfile
import time

from kicad_mcp.coverage.live_hotkeys import _input_for, _start
from kicad_mcp.coverage.live_tools import click
from kicad_mcp.ipc import hotkeys

WINDOW = {"name": "Gerber Viewer", "match": "contains"}
LAYER_ROW = re.compile(r"^\d+ ")
LAYER_STEPS = ["layerNext", "layerPrev", "moveLayerDown", "moveLayerUp", "reloadAllLayers"]
# clearAllLayers runs last in the second session, which still has its layer loaded
HIGHLIGHT_STEPS = ["highlightNet", "highlightComponent", "highlightAttribute", "highlightDCode", "highlightClear",
                   "clearAllLayers"]
PREFIX = "gerbview.Control."


def readback(uia) -> dict:
    """The layer manager's rows in order and every toolbar box's shown value."""
    texts = uia.call("state", {"window": WINDOW, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "Text"}})["controls"]
    boxes = uia.call("state", {"window": WINDOW, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "ComboBox"}})["controls"]
    values = {}
    for box in boxes:
        ident = box["automation_id"]
        try:
            values[f"{box['name'].strip() or 'box'}#{ident}"] = uia.call(
                "read-value", {"window": WINDOW, "selector": {"by": "automation_id", "automation_id": ident}})["value"]
        except Exception:  # noqa: BLE001 - a box without a value
            continue
    return {"layers": [t["name"] for t in texts if LAYER_ROW.match(t["name"])], "boxes": values}


def _differences(before: dict, after: dict) -> list[str]:
    effects = []
    if before["layers"] != after["layers"]:
        if len(before["layers"]) != len(after["layers"]):
            effects.append(f"layer list {len(before['layers'])} -> {len(after['layers'])} rows")
        else:
            moved = [a for a, b in zip(after["layers"], before["layers"]) if a != b]
            effects.append(f"layer order changed: {moved[:2]}")
    for name, value in after["boxes"].items():
        if before["boxes"].get(name) != value:
            effects.append(f"{name} '{before['boxes'].get(name)}' -> '{value}'")
    return effects


def _drawn_points(canvas: int, count: int = 25) -> list[tuple[int, int]]:
    """Canvas pixels that show something (not the background colour), spread over the view."""
    shot = hotkeys.capture(canvas)
    width, height, raw = shot.width, shot.height, shot.raw
    step = 7
    colours = collections.Counter()
    samples = []
    for y in range(20, height - 20, step):
        for x in range(20, width - 20, step):
            offset = (y * width + x) * 4
            colour = bytes(raw[offset:offset + 3])
            colours[colour] += 1
            samples.append((x, y, colour))
    background = colours.most_common(1)[0][0]
    drawn = [(x, y) for x, y, c in samples if c != background]
    if not drawn:
        return []
    stride = max(1, len(drawn) // count)
    return drawn[::stride][:count]


def _press(canvas: int, assigned: dict, name: str) -> None:
    hotkeys.press(canvas, assigned[PREFIX + name][1])
    time.sleep(1.5)


def _close_new_windows(session, before: set[int]) -> list[str]:
    opened = [t or "(untitled)" for h, t in hotkeys._visible(session).items() if h not in before]
    for hwnd in [h for h in hotkeys._visible(session) if h not in before]:
        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
    return opened


@cleanup_after_call
def layer_phase(inventory: dict, project: Path, names: set[str]) -> list[dict]:
    gerber = _input_for("gerbview", project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-gerb-")))
    job = next(gerber.parent.glob("*.gbrjob"))
    steps = [s for s in LAYER_STEPS if PREFIX + s in names]
    session, assigned, _frame, canvas = _start("gerbview", job, [a["name"] for a in inventory["actions"]],
                                               [PREFIX + s for s in steps])
    rows = []
    try:
        uia = session.attach_uia()
        for step in steps:
            row = {"action": PREFIX + step, "outcome": "no_effect", "detail": ""}
            try:
                if step == "reloadAllLayers":
                    # One layer's file function is changed on disk (glue -> drawing): only a reload that really
                    # reads the files again shows that row with its new function.
                    edited = next(Path(session.document).parent.glob("*B_Adhesive*"), None)
                    if edited:
                        text = edited.read_text(encoding="utf-8", errors="replace")
                        edited.write_text(text.replace("%TF.FileFunction,Glue,Bot*%",
                                                       "%TF.FileFunction,OtherDrawing,Comment*%"), encoding="utf-8")
                windows = set(hotkeys._visible(session))
                before = readback(uia)
                _press(canvas, assigned, step)
                opened = _close_new_windows(session, windows)
                time.sleep(0.8)
                after = readback(uia)
                effects = _differences(before, after)
                if opened:
                    row["windows"] = opened
                if effects:
                    row.update(outcome="ok", detail="; ".join(effects))
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(row["outcome"], row["action"], row["detail"][:140], flush=True)
            if not session.healthy():
                rows[-1]["detail"] += "; viewer restarted after"
                break
    finally:
        session.close(discard=True)
    return rows


@cleanup_after_call
def highlight_phase(inventory: dict, project: Path, names: set[str]) -> list[dict]:
    gerber = _input_for("gerbview", project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-gerb1-")))
    steps = [s for s in HIGHLIGHT_STEPS if PREFIX + s in names]
    session, assigned, _frame, canvas = _start("gerbview", gerber, [a["name"] for a in inventory["actions"]],
                                               [PREFIX + s for s in steps])
    rows: dict[str, dict] = {PREFIX + s: {"action": PREFIX + s, "outcome": "no_effect", "detail": ""} for s in steps}
    try:
        uia = session.attach_uia()
        deadline = time.monotonic() + 360
        for x, y in _drawn_points(canvas):
            if time.monotonic() > deadline:
                break
            pending = [s for s in steps if s not in ("highlightClear", "clearAllLayers")
                       and rows[PREFIX + s]["outcome"] != "ok"]
            if not pending:
                break
            hotkeys.press(canvas, hotkeys.VK_ESCAPE)
            windows = set(hotkeys._visible(session))
            click(canvas, x, y)
            time.sleep(0.6)
            if _close_new_windows(session, windows):  # stacked items: the viewer asked which one
                hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                continue
            for step in pending:
                before = readback(uia)
                _press(canvas, assigned, step)
                effects = _differences(before, readback(uia))
                if effects:
                    print("ok", PREFIX + step, effects[:2], flush=True)
                    rows[PREFIX + step].update(outcome="ok", detail=f"item at ({x},{y}) selected; " + "; ".join(effects))
        for last in ("highlightClear", "clearAllLayers"):
            if last not in steps:
                continue
            row = rows[PREFIX + last]
            before = readback(uia)
            _press(canvas, assigned, last)
            effects = _differences(before, readback(uia))
            if effects:
                row.update(outcome="ok", detail="; ".join(effects))
    finally:
        session.close(discard=True)
    return list(rows.values())


@cleanup_after_call
def sweep(inventory: dict, project: Path, only: set[str] | None = None) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    if only:
        names &= only
    rows = layer_phase(inventory, project, names) + highlight_phase(inventory, project, names)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "gerbview-uia", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated action names")
    args = parser.parse_args(argv)
    only = {n for n in args.only.split(",") if n} or None
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, only)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
