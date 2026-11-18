"""Live proof for the board editor's zone order and merge actions, read back through the API.

Each action gets the zones a person would have: two overlapping copper zones on one layer (made with
CreateItems in the session's candidate copy, off the board's own area), the right one selected through the
API, then the action run with RunAction on the board. It passes only when the zones read back changed as
the action says: the selected zone's priority went below, above or to the other side of its neighbour, or
two zones became one.

    python -m kicad_mcp.coverage.live_zones --inventory inv.json --project <dir> --out ev.json
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
PREFIX = "pcbnew.EditorControl."
# action -> (which zone of the pair is selected: "high" | "low" | "both", what must be true afterwards)
CASES = {
    "zonePriorityLower": ("high", lambda hi, lo, n: hi is not None and lo is not None and hi < lo),
    "zonePriorityMoveToBottom": ("high", lambda hi, lo, n: hi is not None and lo is not None and hi < lo),
    "zonePriorityMoveToTop": ("low", lambda hi, lo, n: hi is not None and lo is not None and lo > hi),
    "zoneMerge": ("both", lambda hi, lo, n: n == 1),
}


def _square(x: float, y: float, size: float, priority: int) -> dict:
    corners = [(x, y), (x + size, y), (x + size, y + size), (x, y + size)]
    return {"@type": f"type.googleapis.com/{PCB}Zone", "layers": ["BL_B_Cu"], "priority": priority,
            "outline": {"polygons": [{"outline": {"closed": True, "nodes": [
                {"point": {"x_nm": int(cx * MM), "y_nm": int(cy * MM)}} for cx, cy in corners]}}]}}


def _created(result: dict) -> list[str]:
    ids = []
    for entry in result.get("created_items", []) or result.get("items", []):
        item = entry.get("item", entry)
        value = item.get("id", {}).get("value")
        if value:
            ids.append(value)
    return ids


def _priorities(session: GuiSession, ids: list[str]) -> tuple[list[int | None], int]:
    found = api.call(session, "GetItemsById", {"items": [{"value": i} for i in ids]})["response"].get("items", [])
    by_id = {z.get("id", {}).get("value"): int(z.get("priority", 0)) for z in found}
    return [by_id.get(i) for i in ids], len(found)


def run_case(session: GuiSession, name: str, index: int) -> dict:
    which, check = CASES[name]
    x = 300 + 60 * index  # each case its own place, away from the board
    result = api.call(session, "CreateItems", {"items": [_square(x, 10, 20, 3), _square(x + 10, 20, 20, 1)]})["response"]
    ids = _created(result)
    row = {"action": PREFIX + name, "outcome": "no_effect", "detail": ""}
    if len(ids) != 2:
        row.update(outcome="error", detail=f"zones not created: {json.dumps(result)[:200]}")
        return row
    high, low = ids
    before, _ = _priorities(session, ids)
    api.call(session, "ClearSelection", {})
    chosen = {"high": [high], "low": [low], "both": ids}[which]
    api.call(session, "AddToSelection", {"items": [{"value": i} for i in chosen]})
    reply = api.call(session, "RunAction", {"action": PREFIX + name})["response"]
    time.sleep(1.0)
    after, remaining = _priorities(session, ids)
    row["priorities"] = {"before": before, "after": after, "zones_left": remaining}
    hi, lo = after
    if check(hi, lo, remaining):
        if name == "zoneMerge":
            row.update(outcome="ok", detail=f"two overlapping zones selected; {remaining} zone left of the pair")
        else:
            row.update(outcome="ok", detail=f"selected {which} zone; priorities (high, low) {before} -> {after}")
    else:
        row["detail"] = f"RunAction {reply.get('status', reply)}; priorities {before} -> {after}; zones left {remaining}"
    api.call(session, "ClearSelection", {})
    return row


def sweep(inventory: dict, project: Path) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    cases = [n for n in CASES if PREFIX + n in names]
    session = GuiSession(next(project.glob("*.kicad_pcb")))
    session.start()
    rows = []
    try:
        time.sleep(2)
        for index, name in enumerate(cases):
            try:
                rows.append(run_case(session, name, index))
            except Exception as exc:  # noqa: BLE001
                rows.append({"action": PREFIX + name, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "zones-api-readback", "passed": passed, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:140])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
