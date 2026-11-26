"""Live proof for next / previous unit (registered by the symbol editor's control tool, though named
``eeschema.EditorControl``): KiCad's ECC83 demo symbol, a dual triode, is opened in the symbol editor, the
action's key is pressed, and the editor's unit selector is read back through UI Automation.

    python -m kicad_mcp.coverage.live_units --inventory inv.json --project <ecc83 demo dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import json
from pathlib import Path
import shutil
import time

from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.ipc import api, hotkeys

CASES = ["eeschema.EditorControl.nextUnit", "eeschema.EditorControl.previousUnit"]


def _unit_box(uia) -> str:
    """The symbol editor's unit selector as shown ("Unit A", "Unit B" ...)."""
    window = {"name": "Symbol Editor", "match": "contains"}
    boxes = uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "ComboBox"}})["controls"]
    values = []
    for box in boxes:
        try:
            values.append(uia.call("read-value", {"window": window, "selector": {
                "by": "automation_id", "automation_id": box["automation_id"]}})["value"])
        except Exception:  # noqa: BLE001
            continue
    return next((v for v in values if "nit" in v), "|".join(values))


@cleanup_after_call
def sweep(inventory: dict, project: Path) -> dict:
    """The actions switch the unit the symbol editor shows: the dual triode ECC83 is opened there, and the
    editor's unit selector is read back (UI Automation) around each key."""
    names = {a["name"] for a in inventory["actions"]}
    cases = [c for c in CASES if c in names]
    fixture = Path(scoped_mkdtemp(prefix="kicad-ai-tools-units-")) / project.name
    shutil.copytree(project, fixture)
    pro = next(fixture.glob("*.kicad_pro"))
    document = fixture / (pro.stem + ".kicad_sch")
    session, assigned, _frame, canvas = _start("symedit", document, [a["name"] for a in inventory["actions"]], cases)
    rows = []
    try:
        api.call(session, "OpenLibraryItem", {"type": "DOCTYPE_SYMBOL",
                                              "identifier": {"library_nickname": pro.stem, "entry_name": "ECC83"}})
        time.sleep(3)
        _frame, canvas = hotkeys.canvas_of(session, "Symbol Editor")
        uia = session.attach_uia()
        for name in cases:
            row = {"action": name, "outcome": "no_effect", "detail": ""}
            try:
                before = _unit_box(uia)
                hotkeys.press(canvas, assigned[name][1])
                time.sleep(1.2)
                after = _unit_box(uia)
                row["unit"] = [before, after]
                if before and after and before != after:
                    row.update(outcome="ok", detail=f"ECC83 opened; the editor's unit selector '{before}' -> '{after}'")
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(row["outcome"], name, row["detail"][:140], row.get("unit"), flush=True)
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "units-symedit-uia", "passed": passed, "rows": rows}


@cleanup_after_call
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
