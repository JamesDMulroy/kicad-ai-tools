"""Live proof for the selection clarification menu (common.InteractiveSelection.selectionMenu).

The schematic editor runs this action when a click lands on more than one item its guess cannot choose between
(SCH_SELECTION_TOOL::selectPoint, eeschema/tools/sch_selection_tool.cpp:1890): the "Clarify Selection" popup
lists the candidates. (The board editor calls its menu function directly, never the action.) The guess keeps the
item closest to the click and only the items wholly inside the inner half of its box
(GuessSelectionCandidates, sch_selection_tool.cpp:2137-2311), so the fixture is a 60 x 40 mm sheet symbol with a
small, spaced-out text at its centre: a click there (between the text's letters) is closest to the sheet's centre
and keeps the text too. A click inside the sheet away from the text is the control (only the sheet: no menu).

    python -m kicad_mcp.coverage.live_selmenu --inventory inv.json --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import contextlib
import json
import os
from pathlib import Path
import time
import uuid

from kicad_mcp.coverage.cursor_hook import CursorHook
from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.ipc import hotkeys

ACTION = "common.InteractiveSelection.selectionMenu"
FIT = "common.Control.zoomFitObjects"
FONT = "(effects (font (size 1.27 1.27)) (justify left bottom))"


def make_schematic(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    root, sub = uuid.uuid4(), uuid.uuid4()
    sheet = folder / "stack.kicad_sch"
    sheet.write_text(f"""(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")
  (uuid "{root}") (paper "A4") (lib_symbols)
  (text "W     W" (exclude_from_sim no) (at 110 80 0) (effects (font (size 2 2))) (uuid "{uuid.uuid4()}"))
  (sheet (at 80 60) (size 60 40) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (stroke (width 0.1524) (type solid)) (fill (color 0 0 0 0.0000)) (uuid "{sub}")
    (property "Sheetname" "Sub" (at 80 59.3 0) {FONT})
    (property "Sheetfile" "sub.kicad_sch" (at 80 100.6 0) {FONT})
    (instances (project "stack" (path "/{root}" (page "2")))))
  (sheet_instances (path "/" (page "1")))
)
""", encoding="utf-8")
    (folder / "sub.kicad_sch").write_text(f"""(kicad_sch (version 20250114) (generator "eeschema") (generator_version "9.0")
  (uuid "{uuid.uuid4()}") (paper "A4") (lib_symbols)
)
""", encoding="utf-8")
    (folder / "stack.kicad_pro").write_text("{}\n", encoding="utf-8")
    return sheet


def _menus(session) -> list[int]:
    return [w["hwnd"] for w in session.kicad.desktop.windows()
            if w["pid"] == session.pid and w["visible"] and w["class"] == "#32768"]


def _click(hook: CursorHook, canvas: int, x: int, y: int) -> None:
    point = (y << 16) | x
    hook.place(canvas, x, y)
    hotkeys.user32.PostMessageW(canvas, 0x0200, 0, point)
    time.sleep(0.3)
    hook.button(True)
    hotkeys.user32.PostMessageW(canvas, 0x0201, 1, point)
    time.sleep(0.15)
    hook.button(False)
    hotkeys.user32.PostMessageW(canvas, 0x0202, 0, point)


def _menu_after_click(session, hook: CursorHook, canvas: int, x: int, y: int) -> list[str]:
    from kicad_mcp.coverage.live_designblocks import Run as BlockRun

    _click(hook, canvas, x, y)
    if os.environ.get("KICAD_AI_DUMP"):
        from kicad_mcp.coverage.live_board_misc import _dump

        time.sleep(1.0)
        frame = hotkeys.user32.GetAncestor(canvas, 2)
        _dump(Path(os.environ["KICAD_AI_DUMP"]), **{f"click-{x}-{y}": hotkeys.settled_capture(frame)})
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        menus = _menus(session)
        if menus:
            entries = [e for e in BlockRun.popup_entries(menus[0]) if e]
            hotkeys.press(menus[0], hotkeys.VK_ESCAPE)
            time.sleep(0.8)
            return entries
        time.sleep(0.2)
    return []


@cleanup_after_call
def sweep(inventory: dict) -> dict:
    sheet = make_schematic(Path(scoped_mkdtemp(prefix="kicad-ai-tools-selmenu-")) / "proj")
    session, assigned, _frame, canvas = _start("eeschema", sheet, [a["name"] for a in inventory["actions"]], [FIT])
    row = {"action": ACTION, "outcome": "no_effect", "detail": ""}
    hook = None
    try:
        hook = CursorHook(session.pid)
        hotkeys.press(canvas, assigned[FIT][1])
        time.sleep(1.5)
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(canvas, hotkeys.ctypes.byref(rect))
        width, height = rect.right, rect.bottom
        # fitted to the sheet: its centre (and the text's) is about the middle of the view
        alone = _menu_after_click(session, hook, canvas, int(width * 0.68), int(height * 0.64))
        hotkeys.press(canvas, hotkeys.VK_ESCAPE)
        found: dict = {}
        for dx, dy in ((0, 0), (0, -8), (0, 8), (-8, 0), (8, 0), (0, -16), (0, 16)):
            entries = _menu_after_click(session, hook, canvas, width // 2 + dx, height // 2 + dy)
            hotkeys.press(canvas, hotkeys.VK_ESCAPE)
            if entries:
                found = {"offset": (dx, dy), "entries": entries}
                break
        stacked = found.get("entries", [])
        ok = not alone and len(stacked) >= 2
        row.update(outcome="ok" if ok else "no_effect",
                   detail=f"click inside the sheet away from the text: menu {alone or 'none'}; click on the text at the sheet's centre "
                          f"(offset {found.get('offset')} px from the view centre): popup entries {stacked}")
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        if hook is not None:
            with contextlib.suppress(Exception):
                hook.close()
        session.close(discard=True)
    rows = [row]
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "selection-menu",
            "passed": sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"}), "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
