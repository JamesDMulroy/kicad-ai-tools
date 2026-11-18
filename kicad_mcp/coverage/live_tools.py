"""Live proof for interactive tools by driving them as a person does: activate, click, finish.

KiCad's interactive tools wait for the mouse. Mouse messages posted to the editor canvas on the private
desktop reach them like real clicks (proven: the board line tool draws a segment from posted clicks), so
each tool is activated (RunAction on the board, a private-profile hotkey in the schematic editor), given a
click sequence on the canvas, and finished with Escape. A tool is verified only with an observed effect:
the document changed (API item hash), the editor window or its state changed, or it opened a window.

    python -m kicad_mcp.coverage.live_tools --inventory inv.json --project <dir> --skip-passed ev... --out ev.json
"""
from __future__ import annotations

import argparse
import ctypes
import json
from pathlib import Path
import time

from kicad_mcp.coverage.live_actions import EXTERNAL, editor_state
from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import GuiSession

user32 = ctypes.windll.user32
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
# Candidate click sequences (client pixels): a two-point tool, then a closing double click. A session uses
# the first sequence that is neutral, i.e. that has no effect with no tool active, so any effect seen after
# activating a tool is the tool's, not the clicks'.
CANDIDATES = [[(x, y, False), (x + 120, y, False), (x + 120, y + 80, True)]
              for x, y in ((40, 40), (40, 500), (700, 40), (700, 500), (300, 300), (60, 260))]


def click(canvas: int, x: int, y: int, double: bool = False) -> None:
    point = (y << 16) | x
    user32.PostMessageW(canvas, WM_MOUSEMOVE, 0, point)
    time.sleep(0.12)
    user32.PostMessageW(canvas, WM_LBUTTONDOWN, 1, point)
    user32.PostMessageW(canvas, WM_LBUTTONUP, 0, point)
    if double:
        user32.PostMessageW(canvas, WM_LBUTTONDBLCLK, 1, point)
        user32.PostMessageW(canvas, WM_LBUTTONUP, 0, point)
    time.sleep(0.3)


def _idle(session: GuiSession, canvas: int, tries: int = 12) -> bool:
    """Escape until the editor answers API requests again (the tool has ended)."""
    for _ in range(tries):
        try:
            api.call(session, "GetVersion", {})
            api.call(session, "GetOpenDocuments", {"type": "DOCTYPE_PCB" if session.kind == "pcb"
                                                    else "DOCTYPE_SCHEMATIC"})
            return True
        except (api.ApiCallError, ConnectionError):
            hotkeys.press(canvas, hotkeys.VK_ESCAPE)
            time.sleep(0.5)
    return False


def neutral_clicks(session: GuiSession, frame: int, canvas: int) -> list[tuple[int, int, bool]] | None:
    """The first candidate click sequence that changes nothing on its own."""
    for clicks in CANDIDATES:
        if not drive(session, frame, canvas, lambda: None, clicks):
            return clicks
    return None


def drive(session: GuiSession, frame: int, canvas: int, activate, clicks) -> list[str]:
    _idle(session, canvas)
    windows = hotkeys._visible(session)
    children = hotkeys.visible_children(frame)
    pixels, document = hotkeys.capture(frame), hotkeys.document_hash(session)
    state = editor_state(session) if session.kind == "pcb" else None
    activate()
    time.sleep(0.6)
    opened = [t or "(untitled)" for h, t in hotkeys._visible(session).items() if h not in windows]
    for hwnd in [h for h in hotkeys._visible(session) if h not in windows]:
        user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
    for x, y, double in clicks:
        click(canvas, x, y, double)
    time.sleep(0.4)
    after_pixels = hotkeys.capture(frame)
    _idle(session, canvas)
    effects = []
    if opened:
        effects.append(f"opened {opened}")
    after_document = hotkeys.document_hash(session)
    if document and after_document and document != after_document:
        effects.append("document changed")
    if after_pixels != pixels:
        effects.append("editor window changed")
    if state:
        after_state = editor_state(session)
        if after_state and after_state != state:
            effects.append("editor state changed")
    return hotkeys.judge(effects, hotkeys.new_messages(frame, children))


def sweep(inventory: dict, project: Path, skip: set[str]) -> dict:
    targets = [a for a in inventory["actions"] if not a["runnable"] and f"action:{a['name']}" not in skip
               and not EXTERNAL.match(a["name"].rsplit(".", 1)[-1])]
    board = [a["name"] for a in targets if a["editor"] in ("pcbnew", "common")]
    schematic = [a["name"] for a in targets if a["editor"] == "eeschema"]
    rows: list[dict] = []

    def board_session():
        started = GuiSession(next(project.glob("*.kicad_pcb"))).start()
        frame, canvas = hotkeys.canvas_of(started, "PCB Editor")
        return started, frame, canvas, neutral_clicks(started, frame, canvas)

    session, frame, canvas, clicks = board_session()
    try:
        for name in board:
            if clicks is None:
                rows.append({"action": name, "outcome": "error", "detail": "no neutral click points on the canvas"})
                continue
            failed = False
            try:
                effects = drive(session, frame, canvas,
                                lambda name=name, session=session: api.call(session, "RunAction", {"action": name}), clicks)
                rows.append({"action": name, "outcome": "ok" if effects else "no_effect", "detail": "; ".join(effects)})
            except Exception as exc:  # noqa: BLE001
                rows.append({"action": name, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"})
                failed = True
            if failed or not session.healthy():  # a tool that stays active would poison the next one
                session.close(discard=True)
                session, frame, canvas, clicks = board_session()
    finally:
        session.close(discard=True)

    all_actions = [a["name"] for a in inventory["actions"]]
    for first in range(0, len(schematic), len(hotkeys.KEYS)):
        batch = schematic[first:first + len(hotkeys.KEYS)]
        session = GuiSession(next(project.glob("*.kicad_sch")))
        assigned = hotkeys.write_hotkeys(session.profile / "config" / session._settings_version(), all_actions, batch)
        session.start()
        try:
            frame, canvas = hotkeys.canvas_of(session, "Schematic Editor")
            clicks = neutral_clicks(session, frame, canvas)
            for name in batch:
                if clicks is None:
                    rows.append({"action": name, "outcome": "error", "detail": "no neutral click points"})
                    continue
                try:
                    effects = drive(session, frame, canvas,
                                    lambda key=assigned[name][1], canvas=canvas: hotkeys.press(canvas, key), clicks)
                    rows.append({"action": name, "outcome": "ok" if effects else "no_effect",
                                 "detail": "; ".join(effects)})
                except Exception as exc:  # noqa: BLE001
                    rows.append({"action": name, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"})
                if not session.healthy():
                    break
        finally:
            session.close(discard=True)
    passed = sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "route": "tool-driver", "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--skip-passed", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    skip = {i for f in args.skip_passed for i in json.loads(f.read_text(encoding="utf-8")).get("passed", [])}
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, skip)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
