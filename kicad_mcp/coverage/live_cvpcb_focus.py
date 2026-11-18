"""Live proof for the footprint assigner's focus keys (move between its three lists).

The assigner's lists sit left to right: libraries, components (symbols), footprints. With the focus hook
telling KiCad which list a person is in, each action's key is pressed and passes when KiCad moves focus
(``SetFocus``) to the neighbouring list in its direction.

    python -m kicad_mcp.coverage.live_cvpcb_focus --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import ctypes
import json
from pathlib import Path
import tempfile
import time

from kicad_mcp.coverage.focus_hook import FocusHook
from kicad_mcp.coverage.live_cvpcb import _list_ids
from kicad_mcp.coverage.live_hotkeys import _input_for, _start
from kicad_mcp.ipc import hotkeys

ORDER = ["libraries", "components", "footprints"]
CASES = {"cvpcb.Control.changeFocusRight": 1, "cvpcb.Control.changeFocusLeft": -1}


def _hwnds(frame: int, ids: dict[str, str]) -> dict[str, int]:
    by_id = {}
    for hwnd, cls, _area, visible in hotkeys._children(frame):
        if cls == "SysListView32" and visible:
            by_id[str(ctypes.windll.user32.GetDlgCtrlID(ctypes.c_void_p(hwnd)))] = hwnd
    return {name: by_id[ident] for name, ident in ids.items() if ident in by_id}


@cleanup_after_call
def sweep(inventory: dict, project: Path) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    cases = [c for c in CASES if c in names]
    document = _input_for("cvpcb", project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-cvf-")))
    session, assigned, frame, _canvas = _start("cvpcb", document, [a["name"] for a in inventory["actions"]], cases)
    hook = FocusHook(session.pid)
    rows = []
    try:
        lists = _hwnds(frame, _list_ids(session.attach_uia()))
        for name in cases:
            step = CASES[name]
            row = {"action": name, "outcome": "no_effect", "detail": "", "lists": {k: hex(v) for k, v in lists.items()}}
            results = []
            for start in ORDER:
                if start not in lists:
                    continue
                expected = ORDER[(ORDER.index(start) + step) % 3]
                hook.focus(lists[start])
                hotkeys.press(hotkeys.focused(frame), assigned[name][1])
                calls = hook.set_calls(1.2)
                results.append((start, expected, lists.get(expected) in calls))
                row.setdefault("set_focus_calls", []).append([hex(c) for c in calls])
            hook.focus(None)
            row["moves"] = results
            if results and all(ok for _, _, ok in results):
                row.update(outcome="ok", detail="focus moved " + ", ".join(f"{a} -> {b}" for a, b, _ in results))
            rows.append(row)
            print(row["outcome"], name, row["detail"] or results, flush=True)
    finally:
        hook.close()
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "cvpcb-focus-hook", "passed": passed, "rows": rows}


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
