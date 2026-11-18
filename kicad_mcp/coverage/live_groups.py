"""Live proof for "select items" (an internal selection action other tools run with their items), through
the flow a person uses: two board texts are grouped, then ungrouped; Ungroup hands its former members to
``common.InteractiveSelection.selectItems``, so they must end up selected (API selection) while the group is
gone.

    python -m kicad_mcp.coverage.live_groups --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

from kicad_mcp.ipc import api
from kicad_mcp.ipc.gui_session import GuiSession

PCB = "kiapi.board.types."
MM = 1_000_000
ACTION = "common.InteractiveSelection.selectItems"


def _selection(session) -> list[dict]:
    return api.call(session, "GetSelection", {})["response"].get("items", [])


def sweep(inventory: dict, project: Path) -> dict:
    session = GuiSession(next(project.glob("*.kicad_pcb")))
    session.start()
    row = {"action": ACTION, "outcome": "no_effect", "detail": ""}
    try:
        time.sleep(2)
        texts = [{"@type": f"type.googleapis.com/{PCB}BoardText", "layer": "BL_F_SilkS",
                  "text": {"text": f"GROUP {i}", "position": {"x_nm": (300 + 20 * i) * MM, "y_nm": 40 * MM}}}
                 for i in range(2)]
        created = api.call(session, "CreateItems", {"items": texts})["response"]
        ids = [e.get("item", e)["id"]["value"] for e in (created.get("created_items", []) or created.get("items", []))]
        api.call(session, "ClearSelection", {})
        api.call(session, "AddToSelection", {"items": [{"value": i} for i in ids]})
        api.call(session, "RunAction", {"action": "common.Interactive.group"})
        time.sleep(1.0)
        grouped = _selection(session)
        group_ids = [i["id"]["value"] for i in grouped if i.get("@type", "").endswith("Group")]
        api.call(session, "ClearSelection", {})
        if group_ids:
            api.call(session, "AddToSelection", {"items": [{"value": group_ids[0]}]})
        api.call(session, "RunAction", {"action": "common.Interactive.ungroup"})
        time.sleep(1.0)
        selected = sorted(i["id"]["value"] for i in _selection(session))
        row.update(created=ids, group=group_ids, selected_after_ungroup=selected)
        if group_ids and selected == sorted(ids):
            row.update(outcome="ok", detail="after Ungroup, exactly the group's two former members are selected "
                                            "(Ungroup passes them to selectItems)")
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        session.close(discard=True)
    print(row["outcome"], ACTION, row["detail"], row.get("group"), flush=True)
    rows = [row, cursor_case(project), cursor_case(project, by_key="common.Control.cursorClick", inventory=inventory)]
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "groups-api",
            "passed": sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok"), "rows": rows}


CURSOR = "common.InteractiveSelection.cursor"


def cursor_case(project: Path, by_key: str | None = None, inventory: dict | None = None) -> dict:
    """With nothing selected and the mouse over a board text (centred on it), "select under cursor" selects it.
    With ``by_key`` (the keyboard "Click" action, which keeps its own parameter only when pressed), the action's
    key is pressed instead: a click synthesized at the mouse selects the text."""
    from kicad_mcp.coverage.cursor_hook import CursorHook
    from kicad_mcp.ipc import hotkeys

    action = by_key or CURSOR
    assigned = {}
    if by_key:
        from kicad_mcp.coverage.live_hotkeys import _start

        session, assigned, _f, _c = _start("pcbnew", next(project.glob("*.kicad_pcb")),
                                           [a["name"] for a in inventory["actions"]], [by_key])
    else:
        session = GuiSession(next(project.glob("*.kicad_pcb")))
        session.start()
    row = {"action": action, "outcome": "no_effect", "detail": ""}
    hook = None
    try:
        _frame, canvas = hotkeys.canvas_of(session, "PCB Editor")
        time.sleep(2)
        text = {"@type": f"type.googleapis.com/{PCB}BoardText", "layer": "BL_F_SilkS",
                "text": {"text": "CURSOR", "position": {"x_nm": 320 * MM, "y_nm": 60 * MM},
                         "attributes": {"size": {"x_nm": 5 * MM, "y_nm": 5 * MM}}}}
        created = api.call(session, "CreateItems", {"items": [text]})["response"]
        ident = (created.get("created_items", []) or created.get("items", []))[0].get("item", {})["id"]["value"]
        api.call(session, "ClearSelection", {})
        api.call(session, "AddToSelection", {"items": [{"value": ident}]})
        api.call(session, "RunAction", {"action": "common.Control.zoomFitSelection"})
        time.sleep(1.0)
        api.call(session, "ClearSelection", {})
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(canvas, hotkeys.ctypes.byref(rect))
        cx, cy = rect.right // 2, rect.bottom // 2
        hook = CursorHook(session.pid)
        for step in range(8, 0, -1):
            hotkeys.user32.PostMessageW(canvas, 0x0200, 0, ((cy - 6 * step) << 16) | (cx - 9 * step))
            time.sleep(0.06)
        hook.place(canvas, cx, cy)
        hotkeys.user32.PostMessageW(canvas, 0x0200, 0, (cy << 16) | cx)
        time.sleep(0.5)
        if by_key:
            hotkeys.press(canvas, assigned[by_key][1])
        else:
            api.call(session, "RunAction", {"action": CURSOR})
        time.sleep(1.0)
        selected = [i["id"]["value"] for i in _selection(session)]
        row["selected"] = selected[:3]
        if ident in selected:
            row.update(outcome="ok", detail="with nothing selected and the mouse over a board text, the action "
                                            "selected it (API selection)")
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    finally:
        if hook:
            hook.close()
        session.close(discard=True)
    print(row["outcome"], action, row["detail"], flush=True)
    return row


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
