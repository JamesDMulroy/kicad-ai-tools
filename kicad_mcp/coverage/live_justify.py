"""Live proof for the common text-justification actions, run in the board editor and read back through the API.

A board text is made with CreateItems in the session's candidate copy, selected, and each action run with
RunAction; it passes when the text's horizontal alignment read back is the one the action names.

    python -m kicad_mcp.coverage.live_justify --inventory inv.json --project <dir> --out ev.json
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
# action -> alignment it must leave; each starts from a different alignment so the change is visible
CASES = {
    "common.Control.centerJustify": ("HA_LEFT", "HA_CENTER"),
    "common.Control.rightJustify": ("HA_CENTER", "HA_RIGHT"),
    "common.Control.leftJustify": ("HA_RIGHT", "HA_LEFT"),
}


def _alignment(item: dict) -> str:
    return item.get("text", {}).get("attributes", {}).get("horizontal_alignment", "HA_UNKNOWN")


def run_case(session: GuiSession, name: str, index: int) -> dict:
    start, want = CASES[name]
    text = {"@type": f"type.googleapis.com/{PCB}BoardText", "layer": "BL_F_SilkS",
            "text": {"text": f"JUSTIFY {index}", "position": {"x_nm": (300 + 40 * index) * MM, "y_nm": 10 * MM},
                     "attributes": {"horizontal_alignment": start}}}
    created = api.call(session, "CreateItems", {"items": [text]})["response"]
    entries = created.get("created_items", []) or created.get("items", [])
    ident = next((e.get("item", e).get("id", {}).get("value") for e in entries), None)
    row = {"action": name, "outcome": "no_effect", "detail": ""}
    if not ident:
        row.update(outcome="error", detail=f"text not created: {json.dumps(created)[:200]}")
        return row
    read = lambda: api.call(session, "GetItemsById", {"items": [{"value": ident}]})["response"]["items"][0]  # noqa: E731
    before = _alignment(read())
    api.call(session, "ClearSelection", {})
    api.call(session, "AddToSelection", {"items": [{"value": ident}]})
    api.call(session, "RunAction", {"action": name})
    time.sleep(0.8)
    after = _alignment(read())
    api.call(session, "ClearSelection", {})
    row["alignment"] = {"before": before, "after": after}
    if after == want and before != want:
        row.update(outcome="ok", detail=f"selected board text alignment {before} -> {after}")
    else:
        row["detail"] = f"alignment {before} -> {after}, wanted {want}"
    return row


def sweep(inventory: dict, project: Path) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    session = GuiSession(next(project.glob("*.kicad_pcb")))
    session.start()
    rows = []
    try:
        time.sleep(2)
        for index, name in enumerate(n for n in CASES if n in names):
            try:
                rows.append(run_case(session, name, index))
            except Exception as exc:  # noqa: BLE001
                rows.append({"action": name, "outcome": "error", "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "justify-api-readback", "passed": passed, "rows": rows}


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
