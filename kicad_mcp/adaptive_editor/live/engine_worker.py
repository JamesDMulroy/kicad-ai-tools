"""Persistent engine worker, run with KiCad's python.exe (pcbnew available).

Protocol (one JSON object per line on stdin, one per line on stdout):
  {"op": "load", "board": "<path>"}                    -> {"ok": true, "parts": n}
  {"op": "solve", "request": {...}, "render": true|"prims"}
                                                       -> {"ok": true, "status", "changeset", "report", "svg"|"prims"}
  {"op": "edges"}                                      -> {"ok": true, "edges": [{"uuid", "start", "end"}]}
  {"op": "quit"}
The board is loaded once into the neutral model; each solve starts from that baseline, so a result
depends only on the request (LIVE-PLUGIN-SPEC §2).
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))  # adaptive_editor/ (adaptive_engine package, kicad_env)
sys.path.insert(0, HERE)  # live/ (preview_render)

_real_stdout = sys.stdout
sys.stdout = sys.stderr  # native pcbnew/wx chatter must not corrupt the protocol


def _emit(obj) -> None:
    _real_stdout.write(json.dumps(obj) + "\n")
    _real_stdout.flush()


def main() -> None:
    from adaptive_engine import pcbnew_adapter, solver
    from adaptive_engine.model import Request
    model = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            op = msg.get("op")
            if op == "load":
                model = pcbnew_adapter.load_board(msg["board"])
                _emit({"ok": True, "parts": len(model.parts), "tracks": len(model.tracks)})
            elif op == "solve":
                if model is None:
                    _emit({"ok": False, "code": "not_loaded"}); continue
                req = Request.from_dict(msg["request"])
                res = solver.solve(model, req)
                rep = res.report.to_dict() if hasattr(res.report, "to_dict") else {}
                cs = res.changeset.to_dict()
                out = {"ok": True, "status": rep.get("status"), "changeset": cs, "report": rep}
                if msg.get("render") == "prims":
                    from preview_render import primitives
                    out["prims"] = primitives(model, cs)
                elif msg.get("render"):
                    from preview_render import render
                    out["svg"] = render(model, cs)
                _emit(out)
            elif op == "edges":
                if model is None:
                    _emit({"ok": False, "code": "not_loaded"}); continue
                _emit({"ok": True, "edges": [{"uuid": e.uuid, "start": list(e.start), "end": list(e.end)}
                                             for e in model.outline.edges]})
            elif op == "quit":
                _emit({"ok": True}); return
            else:
                _emit({"ok": False, "code": "unknown_op"})
        except Exception as exc:  # report, never crash the live session
            _emit({"ok": False, "code": "error", "message": repr(exc)})


if __name__ == "__main__":
    main()
