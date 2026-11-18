"""Live proof for the drawing-sheet editor's interactive tools, driven on its canvas and read back from disk.

The drawing-sheet editor has no API (upstream gap 6), so its tools are run as a person runs them: the
tool's own key (the session profile's hotkey), clicks on the canvas, Escape, then Save. A tool passes only
when the saved sheet file holds what it makes: one more line, rectangle, text or bitmap, the selected item
at a new position (move), or the items of another sheet (append). File dialogs the tools open (choose
image, append sheet) are answered through UI Automation with a fixture file.

    python -m kicad_mcp.coverage.live_pleditor --inventory inv.json --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import base64
import json
from pathlib import Path
import re
import tempfile
import time

from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.coverage.live_tools import click
from kicad_mcp.coverage.live_typed import TINY_PNG
from kicad_mcp.ipc import hotkeys

PREFIX = "plEditor."
SAVE, SELECT_ALL = "common.Control.save", "common.Interactive.selectAll"
SHEET = """(kicad_wks (version 20231118) (generator "kicad-ai-tools")
  (setup (textsize 1.5 1.5) (linewidth 0.15) (textlinewidth 0.15))
  (rect (name "") (start 10 10) (end 20 20))
)
"""
OTHER = """(kicad_wks (version 20231118) (generator "kicad-ai-tools")
  (setup (textsize 1.5 1.5) (linewidth 0.15) (textlinewidth 0.15))
  (line (name "appended") (start 30 30) (end 60 30))
  (tbtext "APPENDED" (name "appended-text") (pos 40 40))
)
"""
# tool -> (what it makes in the file, file dialog it opens or None)
CASES = {
    "InteractiveDrawing.drawLine": ("(line", None),
    "InteractiveDrawing.drawRectangle": ("(rect", None),
    "InteractiveDrawing.placeText": ("(tbtext", None),
    "InteractiveDrawing.placeImage": ("(bitmap", "Choose Image"),
    "InteractiveEdit.appendWorksheet": ("appended", "Append Existing Drawing Sheet"),
    "InteractiveMove.move": (None, None),
}


def _count(text: str, needle: str) -> int:
    return len(re.findall(re.escape(needle) + r"(?![A-Za-z_])", text))


RECT_START = re.compile(r'\(rect\s+\(name ""\)\s+\(start ([-\d.]+ [-\d.]+)\)')
LINE = re.compile(r"\(line\s+\(name \"\"\)\s+\(start ([-\d.]+ [-\d.]+)\)\s+\(end ([-\d.]+ [-\d.]+)\)")


def _answer_dialog(session, title: str, path: Path) -> bool:
    """Type ``path`` into the file dialog named ``title`` and press Open; False if it never appeared."""
    uia = session.attach_uia()
    window = {"name": title, "match": "contains"}
    for _ in range(20):
        try:
            uia.call("set-value", {"window": window, "value": str(path),
                                   "selector": {"by": "automation_id", "automation_id": "1148", "control_type": "Edit"}})
            break
        except Exception:  # noqa: BLE001 - not open yet
            time.sleep(0.5)
    else:
        return False
    try:
        uia.call("invoke", {"window": window, "selector": {"by": "automation_id", "automation_id": "1", "control_type": "SplitButton"}})
    except Exception:
        tree = uia.call("dump-tree", {"window": window, "max_depth": 8, "max_nodes": 400})
        Path(tempfile.gettempdir(), "kicad-ai-tools-dialog-dump.json").write_text(json.dumps(tree), encoding="utf-8")
        raise
    time.sleep(1.5)
    return True


def _clicks(canvas: int, tool: str) -> None:
    if tool.endswith(("drawLine", "drawRectangle")):
        click(canvas, 300, 260)
        for step in range(1, 7):  # the shape follows the mouse to its second corner
            hotkeys.user32.PostMessageW(canvas, 0x0200, 0, ((260 + 12 * step) << 16) | (300 + 20 * step))
            time.sleep(0.1)
        click(canvas, 420, 332)
    else:
        click(canvas, 360, 300)
    time.sleep(0.5)


@cleanup_after_call
def sweep(inventory: dict) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    cases = [c for c in CASES if PREFIX + c in names]
    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-wks-"))
    document = scratch / "coverage.kicad_wks"
    document.write_text(SHEET, encoding="utf-8")
    image = scratch / "fixture.png"
    image.write_bytes(base64.b64decode(TINY_PNG))
    other = scratch / "other.kicad_wks"
    other.write_text(OTHER, encoding="utf-8")
    batch = [PREFIX + c for c in cases] + [SAVE, SELECT_ALL]
    session, assigned, frame, canvas = _start("plEditor", document, [a["name"] for a in inventory["actions"]], batch)
    rows = []
    try:
        sheet = Path(session.document)
        hotkeys.press(canvas, assigned[SAVE][1])  # the baseline is the editor's own saved form of the sheet
        time.sleep(1.5)
        for step in range(10):  # the canvas starts following the mouse only after it has seen it move
            hotkeys.user32.PostMessageW(canvas, 0x0200, 0, ((200 + 15 * step) << 16) | (200 + 25 * step))
            time.sleep(0.1)
        for case in cases:
            name = PREFIX + case
            makes, dialog = CASES[case]
            row = {"action": name, "outcome": "no_effect", "detail": "", "route": "tool-driver-file"}
            try:
                before = sheet.read_text(encoding="utf-8")
                windows = set(hotkeys._visible(session))
                if case.endswith("move"):
                    hotkeys.press(canvas, assigned[SELECT_ALL][1])
                    time.sleep(0.5)
                hotkeys.press(canvas, assigned[name][1])
                time.sleep(1.0)
                if dialog and not _answer_dialog(session, dialog, image if "Image" in dialog else other):
                    row["detail"] = f"'{dialog}' did not open"
                if case.endswith("move"):
                    hotkeys.user32.PostMessageW(canvas, 0x0200, 0, (330 << 16) | 400)  # WM_MOUSEMOVE
                    time.sleep(0.3)
                    click(canvas, 420, 340)
                elif not case.endswith("appendWorksheet"):
                    _clicks(canvas, case)
                for _ in range(2):
                    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                    time.sleep(0.3)
                for hwnd in [h for h in hotkeys._visible(session) if h not in windows]:
                    hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
                hotkeys.press(canvas, assigned[SAVE][1])
                time.sleep(1.5)
                after = sheet.read_text(encoding="utf-8")
                if case.endswith("drawLine") and not [m for m in LINE.findall(after) if m[0] != m[1]]                         and _count(after, makes) > _count(before, makes):
                    row["detail"] = "a line was added, but with zero length"
                elif makes and _count(after, makes) > _count(before, makes):
                    row.update(outcome="ok", detail=f"saved sheet: '{makes.strip()}' {_count(before, makes)} -> "
                                                    f"{_count(after, makes)}")
                elif makes is None:
                    start, moved = RECT_START.findall(before), RECT_START.findall(after)
                    if start and moved and start != moved:
                        row.update(outcome="ok", detail=f"selected rectangle moved: start {start[0]} -> {moved[0]}")
                if row["outcome"] != "ok":
                    import difflib

                    row["diff"] = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0))[:30]
                if row["outcome"] != "ok" and not row["detail"]:
                    row["detail"] = "saved sheet unchanged" if before == after else "sheet changed, not as expected"
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            if not session.healthy():
                rows[-1]["detail"] += "; editor restarted after"
                break
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "tool-driver-file", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")))
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
