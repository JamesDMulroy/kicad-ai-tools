"""Live proof for the schematic wire tool's in-tool keys, read back as the wires the drawing commits.

A wire is drawn on an empty sheet the way a person draws it: the tool's key, a click at the start, the mouse
moved diagonally (the tool proposes two segments with a corner), then "Finish". The committed wires are read
back through the API. A control drawing with no extra key gives the reference; each in-tool key is pressed
during an otherwise identical drawing, and passes when the committed wires differ as the key says:

- finish: the wires exist only after it (none before);
- switchPosture: the corner is on the other side (same end points, different segments);
- undoLastSegment: the segment fixed by the second click is taken back (fewer or different segments).

The mouse position is told to KiCad inside the session's own process (cursor hook), as in live_statusbar.

    python -m kicad_mcp.coverage.live_wiretool --inventory inv.json --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import tempfile
import time
import uuid

from kicad_mcp.coverage.cursor_hook import CursorHook
from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.coverage.live_tools import click
from kicad_mcp.ipc import api, hotkeys

TOOL = "eeschema.InteractiveDrawingLineWireBus.drawWires"
FINISH = "common.Interactive.finish"
POSTURE = "eeschema.InteractiveDrawingLineWireBus.switchPosture"
UNDO = "eeschema.InteractiveDrawingLineWireBus.undoLastSegment"
REPEAT = "eeschema.InteractiveEdit.repeatDrawItem"
CASES = [FINISH, POSTURE, UNDO, REPEAT]
BLANK_SCH = """(kicad_sch (version 20250114) (generator "eeschema") (generator_version "10.99")
  (uuid "{uuid}") (paper "A4")
  (sheet_instances (path "/" (page "1")))
)
"""


def _wires(session, canvas: int) -> set[tuple]:
    """The sheet's wires, once the tool has ended (KiCad answers "busy" while a tool runs: Escape again)."""
    for _ in range(10):
        try:
            items = api.call(session, "GetItems", {"types": ["KOT_SCH_LINE"]})["response"].get("items", [])
            break
        except api.ApiCallError:
            hotkeys.press(canvas, hotkeys.VK_ESCAPE)
            time.sleep(0.6)
    else:
        raise RuntimeError("the editor stayed busy")
    out = set()
    for w in items:
        s, e = w.get("start", {}), w.get("end", {})
        out.add((int(s.get("x_nm", 0)), int(s.get("y_nm", 0)), int(e.get("x_nm", 0)), int(e.get("y_nm", 0))))
    return out


def _move(canvas: int, hook: CursorHook, x: int, y: int, start: tuple[int, int] | None = None) -> None:
    sx, sy = start or (x - 60, y - 40)
    for step in range(1, 9):
        px, py = sx + (x - sx) * step // 8, sy + (y - sy) * step // 8
        hook.place(canvas, px, py)
        hotkeys.user32.PostMessageW(canvas, 0x0200, 0, (py << 16) | px)
        time.sleep(0.07)
    time.sleep(0.3)


def _draw(session, canvas: int, hook: CursorHook, assigned: dict, origin: tuple[int, int], extra: str | None,
          finish: bool = True, key: int | None = None) -> set[tuple]:
    """One drawing at ``origin`` (client pixels); returns the wires it committed."""
    before = _wires(session, canvas)
    x, y = origin
    hotkeys.press(canvas, assigned[TOOL][1])
    time.sleep(0.6)
    _move(canvas, hook, x, y)
    hook.place(canvas, x, y)
    click(canvas, x, y)
    if extra == UNDO:  # a second click fixes the first segments; undo takes them back
        _move(canvas, hook, x + 80, y + 50, (x, y))
        hook.place(canvas, x + 80, y + 50)
        click(canvas, x + 80, y + 50)
        _move(canvas, hook, x + 160, y + 100, (x + 80, y + 50))
    else:
        _move(canvas, hook, x + 160, y + 100, (x, y))
    if extra:
        hotkeys.press(canvas, key if key is not None else assigned[extra][1])
        time.sleep(0.6)
        _move(canvas, hook, x + 160, y + 100, (x + 150, y + 95))
    if finish:
        hotkeys.press(canvas, assigned[FINISH][1])
        time.sleep(0.8)
    for _ in range(2):
        hotkeys.press(canvas, hotkeys.VK_ESCAPE)
        time.sleep(0.3)
    return _wires(session, canvas) - before


def _shape(wires: set[tuple], origin_nm: tuple[int, int] | None = None) -> list[tuple]:
    """Wires relative to their own top-left corner, so drawings at different places compare."""
    if not wires:
        return []
    ox = min(min(w[0], w[2]) for w in wires)
    oy = min(min(w[1], w[3]) for w in wires)
    return sorted((w[0] - ox, w[1] - oy, w[2] - ox, w[3] - oy) for w in wires)


@cleanup_after_call
def sweep(inventory: dict) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    cases = [c for c in CASES if c in names]
    project = Path(scoped_mkdtemp(prefix="kicad-ai-tools-wire-")) / "blank"
    project.mkdir()
    (project / "blank.kicad_sch").write_text(BLANK_SCH.format(uuid=uuid.uuid4()), encoding="utf-8")
    (project / "blank.kicad_pro").write_text("{}", encoding="utf-8")
    session, assigned, _frame, canvas = _start("eeschema", project / "blank.kicad_sch",
                                               [a["name"] for a in inventory["actions"]], [TOOL, *cases])
    hook = CursorHook(session.pid)
    rows = []
    try:
        control = _draw(session, canvas, hook, assigned, (200, 150), None)
        unfinished = _draw(session, canvas, hook, assigned, (200, 400), None, finish=False)
        reference = _shape(control)
        for index, name in enumerate(cases):
            row = {"action": name, "outcome": "no_effect", "detail": "", "control": reference}
            try:
                if name == REPEAT:
                    # right after a drawing, "repeat last item" places a copy of the last wire it made
                    _draw(session, canvas, hook, assigned, (420, 620), None)
                    before = _wires(session, canvas)
                    _move(canvas, hook, 600, 650)
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(1.0)
                    added = _wires(session, canvas) - before
                    row["added"] = sorted(added)
                    if added:
                        row.update(outcome="ok", detail=f"repeat placed {len(added)} new wire(s) after the drawing")
                elif name == FINISH:
                    row["without_finish"] = sorted(unfinished)
                    if control and not unfinished:
                        row.update(outcome="ok", detail=f"the drawing committed {len(control)} wire(s) only with "
                                                        "Finish (none without it)")
                else:
                    # the same drawing three times: twice with an unbound key where the action's key goes
                    # (the controls must agree), once with the action's key
                    quiet = hotkeys.CONTROL_KEY[1]
                    base_y = 120 + 260 * index
                    first = _shape(_draw(session, canvas, hook, assigned, (420, base_y), name, key=quiet))
                    second = _shape(_draw(session, canvas, hook, assigned, (700, base_y), name, key=quiet))
                    got = _shape(_draw(session, canvas, hook, assigned, (980, base_y), name))
                    row.update(control=first, control_repeat=second, wires=got)
                    if first and first == second and got and got != first:
                        row.update(outcome="ok", detail=f"committed wires {got} differ from two identical control "
                                                        f"drawings {first} (unbound key in its place)")
                    elif first != second:
                        row["detail"] = "the control drawings disagree; not judged"
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(row["outcome"], name, row["detail"][:160], flush=True)
    finally:
        hook.close()
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "wiretool-api", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
