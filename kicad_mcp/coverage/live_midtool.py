"""Live proof for in-tool actions: press the action's own key while the router (or tuner) is mid-route.

An in-tool action (``pcbnew.InteractiveRouter.PlaceVia``, ``lengthTuner.AmplIncrease``...) only exists
while its tool is running a route, so it is run as a person runs it:

1. a pad (router) or track (tuner) of the test board is selected through the API and the view is fitted
   to it, which puts it in the middle of the canvas (proven: a click there selects exactly that item);
2. the tool is activated (RunAction on the board), the item is clicked, and the mouse is moved away;
3. the unbound control key is pressed and the editor window captured (it must not change), then the
   action's key is pressed. An action counts only with an observed effect: the editor window changed
   against the control, a document change, an opened window, or an editor state change;
4. Escape ends the route; the next action starts a fresh one.

    python -m kicad_mcp.coverage.live_midtool --inventory inv.json --project <dir> --out ev.json \
        [--group router|tuner] [--names a,b]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from kicad_mcp.coverage.live_actions import editor_state
from kicad_mcp.coverage.live_tools import click, user32
from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import GuiSession

WM_MOUSEMOVE = 0x0200
GROUPS = {
    # group -> (tool activation action, what is clicked to start, action-name filter)
    "router": ("pcbnew.InteractiveRouter.SingleTrack", "pad", ("pcbnew.InteractiveRouter.",)),
    "tuner": ("pcbnew.LengthTuner.TuneSingleTrack", "track", ("pcbnew.lengthTuner.", "pcbnew.LengthTuner.Settings")),
}
ACTIVATORS = {"pcbnew.InteractiveRouter.SingleTrack", "pcbnew.LengthTuner.TuneSingleTrack",
              "pcbnew.InteractiveRouter.DiffPair", "pcbnew.InteractiveRouter.Autoroute"}


def _targets(inventory: dict, group: str, skip: set[str], names: list[str] | None) -> list[str]:
    activator, _, filters = GROUPS[group]
    out = []
    for a in inventory["actions"]:
        n = a["name"]
        if not a["runnable"] or f"action:{n}" in skip or n in ACTIVATORS or not n.startswith(filters):
            continue
        if names and n not in names:
            continue
        out.append(n)
    return out


def _item_for(session: GuiSession, kind: str) -> str | None:
    """A pad with a net on the front copper (router), or a track (tuner)."""
    if kind == "pad":
        pads = api.call(session, "GetItems", {"types": ["KOT_PCB_PAD"]})["response"].get("items", [])
        for pad in pads:
            layers = pad.get("pad_stack", {}).get("layers", [])
            if pad.get("net", {}).get("name") and "BL_F_Cu" in layers and pad.get("type") == "PT_SMD":
                return pad["id"]["value"]
        return None
    tracks = api.call(session, "GetItems", {"types": ["KOT_PCB_TRACE"]})["response"].get("items", [])
    return next((t["id"]["value"] for t in tracks if t.get("id", {}).get("value")), None)


def _centre(canvas: int) -> tuple[int, int]:
    rect = hotkeys._Rect()
    user32.GetClientRect(canvas, hotkeys.ctypes.byref(rect))
    return rect.right // 2, rect.bottom // 2


def _move(canvas: int, x: int, y: int) -> None:
    for step in (0.3, 0.6, 1.0):
        user32.PostMessageW(canvas, WM_MOUSEMOVE, 0, (int(y * step) << 16) | int(x * step))
        time.sleep(0.12)
    time.sleep(0.4)


def _idle(session: GuiSession, canvas: int) -> bool:
    for _ in range(12):
        try:
            api.call(session, "GetVersion", {})
            api.call(session, "GetOpenDocuments", {"type": "DOCTYPE_PCB"})
            return True
        except (api.ApiCallError, ConnectionError):
            hotkeys.press(canvas, hotkeys.VK_ESCAPE)
            time.sleep(0.5)
    return False


def prepare(session: GuiSession, canvas: int, kind: str) -> tuple[int, int] | None:
    """Select the item, fit the view to it (it ends up in the canvas middle) and clear the selection."""
    item = _item_for(session, kind)
    if item is None:
        return None
    api.call(session, "ClearSelection", {})
    api.call(session, "AddToSelection", {"items": [{"value": item}]})
    api.call(session, "RunAction", {"action": "common.Control.zoomFitSelection"})
    time.sleep(1.5)
    api.call(session, "ClearSelection", {})
    time.sleep(0.4)
    return _centre(canvas)


def run_one(session: GuiSession, frame: int, canvas: int, centre: tuple[int, int], activator: str, key: int,
            text=None) -> dict:
    """One fresh route: activate, click the item, move, control press, action press, Escape. With ``text``
    (a ``TextHook``), the message panel's painted text is also read before and after the action's key."""
    _idle(session, canvas)
    api.call(session, "RunAction", {"action": activator})
    time.sleep(0.6)
    x, y = centre
    click(canvas, x, y)
    _move(canvas, x + 140, y + 60)
    windows = hotkeys._visible(session)
    children = hotkeys.visible_children(frame)
    # The first key of any kind makes the router redraw its preview (measured: the following unbound-key
    # presses change nothing), so the control is pressed until two captures in a row agree; that steady
    # picture is the baseline the action's own key is judged against.
    control = hotkeys.settled_capture(frame)
    quiet = False
    for _ in range(4):
        previous = control
        hotkeys.press(canvas, hotkeys.CONTROL_KEY[1])
        time.sleep(0.8)
        control = hotkeys.settled_capture(frame)
        if control == previous:
            quiet = True
            break
    document, state = hotkeys.document_hash(session), editor_state(session)
    panel = text.painted(frame) if text else set()
    windows_control = hotkeys._visible(session)
    hotkeys.press(canvas, key)
    time.sleep(0.9)
    after = hotkeys.settled_capture(frame)
    panel_after = text.painted(frame) if text else set()
    opened = [t or "(untitled)" for h, t in hotkeys._visible(session).items() if h not in windows_control]
    effects = []
    if opened:
        effects.append(f"opened {opened}")
    if document and hotkeys.document_hash(session) not in (None, document):
        effects.append("document changed")
    new_state = editor_state(session)
    if state and new_state and new_state != state:
        effects.append("editor state changed")
    if text and panel_after - panel:
        effects.append(f"message panel now shows {sorted(panel_after - panel)[:4]} (was {sorted(panel - panel_after)[:4]})")
    if quiet and after != control:  # the router window is noisy; pixels only count when the control was steady
        effects.append("editor window changed against the control press")
    for hwnd in [h for h in hotkeys._visible(session) if h not in windows]:
        user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
    time.sleep(0.4)
    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
    time.sleep(0.4)
    return {"effects": hotkeys.judge(effects, hotkeys.new_messages(frame, children)), "control_quiet": quiet}


MODE_TEXT = {"pcbnew.InteractiveRouter.ShoveMode": "Shove", "pcbnew.InteractiveRouter.WalkaroundMode": "Walk around",
             "pcbnew.InteractiveRouter.HighlightMode": "Highlight collisions"}


def run_mode(session: GuiSession, frame: int, canvas: int, centre: tuple[int, int], activator: str, key: int,
             expected: str, text) -> dict:
    """A router mode key, pressed with no route running; the next route's message panel names the mode."""
    _idle(session, canvas)
    hotkeys.press(canvas, key)
    time.sleep(0.8)
    api.call(session, "RunAction", {"action": activator})
    time.sleep(0.6)
    x, y = centre
    click(canvas, x, y)
    _move(canvas, x + 140, y + 60)
    for _ in range(2):  # the router fills the panel once it has handled input mid-route
        hotkeys.press(canvas, hotkeys.CONTROL_KEY[1])
        time.sleep(0.5)
        _move(canvas, x + 150, y + 70)
    time.sleep(0.8)
    shown = text.painted(frame) if text else set()
    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
    time.sleep(0.4)
    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
    time.sleep(0.4)
    effects = [f"the next route's message panel shows mode '{expected}'"] if expected in shown else []
    return {"effects": effects, "control_quiet": None, "panel": sorted(shown)[:40]}


def sweep(inventory: dict, project: Path, group: str, skip: set[str], names: list[str] | None) -> dict:
    activator, kind, _ = GROUPS[group]
    targets = _targets(inventory, group, skip, names)
    all_actions = [a["name"] for a in inventory["actions"]]
    rows: list[dict] = []
    for first in range(0, len(targets), len(hotkeys.KEYS)):
        batch = targets[first:first + len(hotkeys.KEYS)]
        session = GuiSession(next(project.glob("*.kicad_pcb")))
        assigned = hotkeys.write_hotkeys(session.profile / "config" / session._settings_version(), all_actions, batch)
        session.start()
        text = None
        try:
            frame, canvas = hotkeys.canvas_of(session, "PCB Editor")
            time.sleep(2)
            centre = prepare(session, canvas, kind)
            try:
                from kicad_mcp.coverage.text_hook import TextHook

                text = TextHook(session.pid)
            except Exception:  # noqa: BLE001 - Frida absent: pixels and state only
                text = None
            for name in batch:
                if centre is None:
                    rows.append({"action": name, "outcome": "error", "detail": f"no {kind} to start from"})
                    continue
                try:
                    if name in MODE_TEXT:
                        result = run_mode(session, frame, canvas, centre, activator, assigned[name][1],
                                          MODE_TEXT[name], text)
                    else:
                        result = run_one(session, frame, canvas, centre, activator, assigned[name][1], text)
                    outcome = "ok" if result["effects"] else "no_effect"
                    row = {"action": name, "outcome": outcome, "detail": "; ".join(result["effects"]),
                           "control_quiet": result["control_quiet"], "route": "midtool",
                           **({"panel": result["panel"]} if "panel" in result else {})}
                except Exception as exc:  # noqa: BLE001 - every failure is recorded
                    row = {"action": name, "outcome": "error", "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
                rows.append(row)
                if not session.healthy():
                    row["detail"] += "; editor restarted after"
                    break
        finally:
            if text:
                text.close()
            session.close(discard=True)
    passed = sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "route": "tool-midtool", "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--group", choices=sorted(GROUPS), default="router")
    parser.add_argument("--names")
    parser.add_argument("--skip-passed", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    skip: set[str] = set()
    for f in args.skip_passed:
        skip |= set(json.loads(f.read_text(encoding="utf-8")).get("passed", []))
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    result = sweep(inventory, args.project, args.group, skip, args.names.split(",") if args.names else None)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
