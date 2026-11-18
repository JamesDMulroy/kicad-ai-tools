"""Live proof for the SPICE simulator's plot actions on a simulated plot.

The simulator frame is opened on one of KiCad's own simulation demos (it ships a workbook naming the
traces to plot), the simulation is run with its own key, and each action's key is pressed on the plot:

- exports: the save dialog is completed through UI Automation and the file must appear (CSV, PNG); the
  clipboard export runs inside the clipboard guard and needs new clipboard content and a restored owner text;
  "plot to schematic" must change the schematic (API document hash);
- the plot zooms are view actions: a change of the window against a quiet control key.

    python -m kicad_mcp.coverage.live_sim --inventory inv.json --project <demo dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time

from kicad_mcp.coverage import clipboard
from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.ipc import hotkeys

RUN = "eeschema.Simulation.runSimulation"
EXPORTS = {"eeschema.Simulator.exportCSV": ".csv", "eeschema.Simulator.exportPNG": ".png"}
VIEW = ["common.Control.zoomInHorizontally", "common.Control.zoomOutHorizontally",
        "common.Control.zoomInVertically", "common.Control.zoomOutVertically"]
PROBE = "eeschema.Simulation.probe"
SAVE_WORKBOOK = "eeschema.Simulation.saveWorkbook"
UNDO_ZOOM = "common.Control.undoZoom"  # only the plot keeps a zoom history (mathplot's undo stack)
CASES = [*EXPORTS, "eeschema.Simulator.exportToClipboard", "eeschema.Simulator.exportPlotToSchematic", *VIEW,
         UNDO_ZOOM, PROBE]


def _save_dialog(session, before: set[int], path: Path) -> str:
    """Complete the save dialog the action opened; return its title ('' if none opened)."""
    new: dict = {}
    for _ in range(32):  # the dialog can take a few seconds to show
        new = {h: t for h, t in hotkeys._visible(session).items() if h not in before and t}
        if new:
            break
        time.sleep(0.25)
    if not new:
        return ""
    title = next(iter(new.values()))
    uia = session.attach_uia()
    window = {"name": title, "match": "exact"}
    for ident in ("1001", "1148"):
        try:
            uia.call("set-value", {"window": window, "value": str(path),
                                   "selector": {"by": "automation_id", "automation_id": ident, "control_type": "Edit"}})
            break
        except Exception:  # noqa: BLE001 - the other dialog layout
            continue
    for kind in ("Button", "SplitButton"):
        try:
            uia.call("invoke", {"window": window, "selector": {"by": "automation_id", "automation_id": "1",
                                                              "control_type": kind}})
            break
        except Exception:  # noqa: BLE001
            continue
    time.sleep(2.0)
    return title


def _trigger(frame: int, canvas: int, assigned: dict, name: str, labels: dict) -> str:
    """The action's own menu item when the simulator's menu bar has it (keys reach the signal list, not
    the plot), otherwise its key."""
    label = labels.get(name, "")
    item = next((i for i in hotkeys.menu_items(frame) if label and i["label"] == label), None)
    if item is not None:
        hotkeys.user32.PostMessageW(frame, hotkeys.WM_COMMAND, item["id"], 0)
        return "menu: " + " > ".join(item["path"])
    hotkeys.press(canvas, assigned[name][1])
    return f"key {assigned[name][0]}"


def _probe(session, frame: int, canvas: int, assigned: dict, labels: dict, fixture: Path) -> dict:
    """Probe a wire of the schematic with the simulator's probe tool; the saved workbook must then list one
    more signal than before."""
    from kicad_mcp.coverage.cursor_hook import CursorHook
    from kicad_mcp.coverage.live_tools import click
    from kicad_mcp.ipc import api

    workbook = next(fixture.glob("*.wbk"))

    def saved() -> str:
        _trigger(frame, canvas, assigned, SAVE_WORKBOOK, labels)
        time.sleep(2)
        return workbook.read_text(encoding="utf-8", errors="replace")

    before = saved()
    _, sch_canvas = hotkeys.canvas_of(session, "Schematic Editor")
    hook = CursorHook(session.pid)
    tried = []
    try:
        wires = api.call(session, "GetItems", {"types": ["KOT_SCH_LINE"]})["response"].get("items", [])
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(sch_canvas, hotkeys.ctypes.byref(rect))
        cx, cy = rect.right // 2, rect.bottom // 2
        for wire in wires[:6]:
            hotkeys.select(session, wire["id"]["value"])
            hotkeys.press(sch_canvas, session.zoom_key)
            time.sleep(1.0)
            api.call(session, "ClearSelection", {})
            _trigger(frame, canvas, assigned, PROBE, labels)
            time.sleep(1.0)
            for step in range(8, 0, -1):
                hotkeys.user32.PostMessageW(sch_canvas, 0x0200, 0, ((cy - 6 * step) << 16) | (cx - 9 * step))
                time.sleep(0.06)
            hook.place(sch_canvas, cx, cy)
            click(sch_canvas, cx, cy)
            time.sleep(1.5)
            hotkeys.press(sch_canvas, hotkeys.VK_ESCAPE)
            time.sleep(0.5)
            after = saved()
            tried.append(wire["id"]["value"])
            if after != before:
                return {"outcome": "ok", "detail": "a wire probed with the simulator's probe tool: the saved workbook "
                                                   "changed (a signal was added)", "wires_tried": len(tried)}
    finally:
        hook.close()
    return {"detail": f"workbook unchanged after probing {len(tried)} wire(s)"}


@cleanup_after_call
def sweep(inventory: dict, project: Path, only: set[str] | None = None) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    labels = {a["name"]: hotkeys.normalize(a.get("friendly_name") or "") for a in inventory["actions"]}
    cases = [c for c in CASES if c in names and (not only or c in only)]
    fixture = Path(scoped_mkdtemp(prefix="kicad-ai-tools-sim-")) / project.name
    shutil.copytree(project, fixture)
    document = next(fixture.glob("*.kicad_sch"))
    out_dir = Path(scoped_mkdtemp(prefix="kicad-ai-tools-simout-"))
    from kicad_mcp.coverage.live_hotkeys import ZOOM_SELECTION

    session, assigned, frame, canvas = _start("sim", document, [a["name"] for a in inventory["actions"]],
                                              [RUN, *cases, SAVE_WORKBOOK, ZOOM_SELECTION])
    session.zoom_key = assigned[ZOOM_SELECTION][1]
    rows = []
    try:
        _trigger(frame, canvas, assigned, RUN, labels)
        time.sleep(12)
        hotkeys.settled_capture(frame, timeout_s=10)
        for name in cases:
            row = {"action": name, "outcome": "no_effect", "detail": ""}
            try:
                if name in EXPORTS:
                    target = out_dir / f"plot{EXPORTS[name]}"
                    before = set(hotkeys._visible(session))
                    row["route"] = _trigger(frame, canvas, assigned, name, labels)
                    time.sleep(1.5)
                    title = _save_dialog(session, before, target)
                    if target.exists() and target.stat().st_size > 0:
                        row.update(outcome="ok", detail=f"'{title}' completed; {target.name} written "
                                                        f"({target.stat().st_size} bytes)")
                    else:
                        row["detail"] = f"dialog '{title}'; no file"
                elif name.endswith("exportToClipboard"):
                    with clipboard.preserved() as obs:
                        row["route"] = _trigger(frame, canvas, assigned, name, labels)
                        obs.wait_for_change(3.0)
                        obs.capture()
                    row["clipboard"] = {"changed": obs.changed, "new_content": obs.new_content, "restored": obs.restored}
                    if obs.new_content and obs.restored:
                        row.update(outcome="ok", detail=f"clipboard written ({'image' if obs.has_image else 'text'}); "
                                                        "owner text restored")
                elif name.endswith("exportPlotToSchematic"):
                    # KiCad puts the plot on the clipboard as an image item and starts a paste in the schematic
                    # editor, which follows the mouse until a click places it: the guard keeps the owner's
                    # clipboard text, and the click lands on the schematic canvas.
                    from kicad_mcp.coverage.live_tools import click

                    document_before = hotkeys.document_hash(session)
                    _, sch_canvas = hotkeys.canvas_of(session, "Schematic Editor")
                    with clipboard.preserved() as obs:
                        row["route"] = _trigger(frame, canvas, assigned, name, labels)
                        time.sleep(2.5)
                        for step in range(8):
                            hotkeys.user32.PostMessageW(sch_canvas, 0x0200, 0, ((200 + 10 * step) << 16) | (200 + 15 * step))
                            time.sleep(0.1)
                        click(sch_canvas, 330, 290)
                        time.sleep(1.5)
                        obs.capture()
                    row["clipboard"] = {"changed": obs.changed, "restored": obs.restored}
                    document_after = hotkeys.document_hash(session)
                    if document_before and document_after and document_before != document_after and obs.restored:
                        row.update(outcome="ok", detail="plot image pasted into the schematic (API item hash changed); "
                                                        "owner clipboard text restored")
                elif name == PROBE:
                    row.update(_probe(session, frame, canvas, assigned, labels, fixture))
                elif name == UNDO_ZOOM:
                    first = hotkeys.settled_capture(frame)
                    _trigger(frame, canvas, assigned, VIEW[0], labels)
                    time.sleep(1.5)
                    zoomed = hotkeys.settled_capture(frame)
                    row["route"] = _trigger(frame, canvas, assigned, name, labels)
                    time.sleep(1.5)
                    back = hotkeys.settled_capture(frame)
                    if zoomed != first and back == first:
                        row.update(outcome="ok", detail="after a horizontal zoom in, the plot returned exactly to "
                                                        "its picture before it")
                    else:
                        row["detail"] = f"zoom changed plot: {zoomed != first}; undo restored it: {back == first}"
                else:
                    if hotkeys.window_is_quiet(session, canvas, frame):
                        shot = hotkeys.settled_capture(frame)
                        row["route"] = _trigger(frame, canvas, assigned, name, labels)
                        time.sleep(1.5)
                        if hotkeys.settled_capture(frame) != shot:
                            row.update(outcome="ok", detail="the plot changed (control key quiet)")
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(row["outcome"], name, row["detail"][:140], flush=True)
            if not session.healthy():
                rows[-1]["detail"] += "; editor restarted after"
                break
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "sim-plot", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="", help="comma-separated action names")
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project,
                   {n for n in args.only.split(",") if n} or None)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
