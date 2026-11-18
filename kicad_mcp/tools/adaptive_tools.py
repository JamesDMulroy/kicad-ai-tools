"""Adaptive (Fusion-style) board edits for AI clients: plan, preview, apply, undo.

The engine is the AdaptiveEditor package (``adaptive_engine``: neutral model, solver, pcbnew
adapter, session journal). It runs under KiCad's own ``python.exe`` (pcbnew) through
``secure_subprocess.run_contained`` (hidden, job-contained, output captured); this server never
imports pcbnew. Each call writes one request JSON to a private temp dir and runs::

    <KiCad>/bin/python.exe -X utf8 -B <engine>/adaptive_engine/cli.py <command> --json <request>

Tools
-----
* ``adaptive_capability`` - engine/KiCad discovery plus the engine's own capability report.
* ``adaptive_plan`` - solve only (no files written); returns the change set and report.
* ``adaptive_preview`` - solve, write a candidate copy in ``<board dir>/.adaptive/<session>/``,
  refill zones there, run DRC on original and candidate, optionally render; the board is untouched.
* ``adaptive_apply`` - replace the board with the previewed candidate (sha256-guarded, backup kept).
* ``adaptive_undo`` - restore the backup of an applied session (refused if the board changed since).

Interactive use inside the running PCB Editor is the AdaptiveEditor IPC plugin (``Adaptive Drag``,
``Adaptive Edge Move``): preview in its panel, apply as one KiCad undo step. Live cursor tracking on
the KiCad canvas itself is not available through KiCad 10.0.5's IPC API (uncommitted changes are not
rendered), so it would need native code; ``native_board_interaction_capability`` says so.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from kicad_mcp.utils import secure_subprocess

ENGINE_ENV = "KICAD_MCP_ADAPTIVE_ENGINE"  # optional override; the engine ships in this package
TIMEOUT_S = 900
REQUEST_KEYS = {"driver", "displacement_nm", "mode", "movers", "frozen", "priority", "eligibility_nm",
                "allow_partial", "copper"}


def engine_root() -> Path | None:
    raw = os.environ.get(ENGINE_ENV)
    root = Path(raw) if raw else Path(__file__).resolve().parents[1] / "adaptive_editor"
    return root if (root / "adaptive_engine" / "cli.py").is_file() else None


def kicad_python() -> Path | None:
    """KiCad's bundled interpreter (pcbnew), resolved by kicad_env (env, per-machine config, discovery)."""
    from kicad_mcp.adaptive_editor import kicad_env
    try:
        py = kicad_env.kicad_python()
    except kicad_env.KiCadNotFound:
        return None
    return py if py and py.is_file() else None


def _refusal(code: str, message: str, **details) -> dict[str, Any]:
    return {"ok": False, "refused": True, "error": False, "code": code, "message": message, "details": details}


def _board(path: Any) -> Path | dict:
    if not isinstance(path, str) or not path.strip():
        return _refusal("bad_board_path", "board_path must be a non-empty string")
    p = Path(path)
    if not p.is_absolute():
        return _refusal("bad_board_path", "board_path must be absolute", board_path=path)
    if p.suffix.lower() != ".kicad_pcb":
        return _refusal("bad_board_path", "board_path must be a .kicad_pcb file", board_path=path)
    if not p.is_file():
        return _refusal("board_not_found", "board file does not exist", board_path=path)
    return p.resolve()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_list(v) -> bool:
    return isinstance(v, list) and len(v) <= 10000 and all(isinstance(x, str) and 0 < len(x) <= 64 for x in v)


def _request(request: Any) -> dict | None:
    """Shape check of the whole request before any process starts (the engine re-validates)."""
    if not isinstance(request, dict):
        return _refusal("bad_request", "request must be an object (ADAPTIVE-ENGINE-SPEC s3)")
    unknown = sorted(set(request) - REQUEST_KEYS)
    if unknown:
        return _refusal("bad_request", f"unknown request field(s): {', '.join(unknown)}", allowed=sorted(REQUEST_KEYS))
    driver = request.get("driver")
    if not isinstance(driver, dict) or driver.get("kind") not in ("part", "edge"):
        return _refusal("bad_driver", "driver must be {kind: part, uuids: [...]} or {kind: edge, edge_uuid, side}")
    if driver["kind"] == "part":
        if set(driver) - {"kind", "uuids"} or not _str_list(driver.get("uuids")) or not driver["uuids"]:
            return _refusal("bad_driver", "a part driver is {kind: part, uuids: [non-empty uuid strings]}")
    elif (set(driver) - {"kind", "edge_uuid", "side"} or not isinstance(driver.get("edge_uuid"), str)
          or not driver["edge_uuid"] or driver.get("side") not in ("left", "right", "top", "bottom")):
        return _refusal("bad_driver", "an edge driver is {kind: edge, edge_uuid: str, side: left|right|top|bottom}")
    disp = request.get("displacement_nm")
    if not (isinstance(disp, list) and len(disp) == 2 and all(_is_int(v) and abs(v) <= 10**10 for v in disp)):
        return _refusal("bad_displacement", "displacement_nm must be two integers (nm)")
    checks = {"mode": lambda v: v in ("cascade", "explicit"), "copper": lambda v: v in ("adapt", "fixed"),
              "allow_partial": lambda v: isinstance(v, bool),
              "eligibility_nm": lambda v: v is None or (_is_int(v) and 0 <= v <= 10**10),
              "movers": _str_list, "frozen": _str_list, "priority": _str_list}
    bad = sorted(k for k, ok in checks.items() if k in request and not ok(request[k]))
    if bad:
        return _refusal("bad_request", f"invalid value for: {', '.join(bad)}")
    return None


_HEX64 = __import__("re").compile(r"[0-9a-fA-F]{64}")


def _session(session: Any, board_path: str | None) -> dict | None:
    """A session is an id (then board_path is required) or an existing session dir / session.json."""
    if not isinstance(session, str) or not session.strip() or len(session) > 1024 \
            or any(ord(c) < 32 for c in session):
        return _refusal("bad_session", "session must be a session id, directory or session.json path")
    if board_path is None:
        p = Path(session)
        journal = p if p.name == "session.json" else p / "session.json"
        if not (p.is_absolute() and journal.is_file()):
            return _refusal("bad_session", "without board_path, session must be an existing session dir or "
                            "session.json from adaptive_preview")
        try:
            recorded = json.loads(journal.read_text(encoding="utf-8")).get("board_path")
        except (OSError, ValueError):
            return _refusal("bad_session", "session.json is unreadable")
        checked = _board(recorded)
        if isinstance(checked, dict):
            return _refusal("bad_session", "the board recorded in session.json is not a valid board path",
                            reason=checked.get("code"))
        return None
    # With board_path, the session is either the absolute session directory / session.json that preview
    # returned, or a plain session id (no separators, no "..", no drive). Either way its journal must record
    # this same board, so a session of another board is never applied here.
    p = Path(session)
    if p.is_absolute():
        journal = p if p.name == "session.json" else p / "session.json"
        if not journal.is_file():
            return _refusal("bad_session", "session directory has no session.json")
    elif session in (".", "..") or any(s in session for s in ("/", "\\", ":")) or len(p.parts) != 1:
        return _refusal("bad_session", "session must be a session id (a plain name) or a session directory")
    else:
        journal = Path(board_path).resolve().parent / ".adaptive" / session / "session.json"
    if journal.is_file():
        try:
            recorded = json.loads(journal.read_text(encoding="utf-8")).get("board_path")
        except (OSError, ValueError):
            return _refusal("bad_session", "session.json is unreadable")
        if not recorded or os.path.normcase(str(Path(recorded).resolve())) !=                 os.path.normcase(str(Path(board_path).resolve())):
            return _refusal("bad_session", "the session belongs to a different board")
    return None


@cleanup_after_call
def run_engine(command: str, payload: dict | None) -> dict[str, Any]:
    # covers: ADE-4
    """Run one engine CLI command hidden and contained; always returns one JSON-able dict."""
    root, py = engine_root(), kicad_python()
    if root is None:
        return {"ok": False, "error": True, "code": "engine_not_found",
                "message": f"adaptive engine not found; set {ENGINE_ENV} to the AdaptiveEditor/python directory"}
    if py is None:
        return {"ok": False, "error": True, "code": "kicad_python_not_found",
                "message": "KiCad's python.exe (with pcbnew) was not found; set KICAD_APP_PATH"}
    work = Path(scoped_mkdtemp(prefix="kicad-mcp-adaptive-"))
    try:
        argv = [str(py), "-X", "utf8", "-B", str(root / "adaptive_engine" / "cli.py"), command]
        if payload is not None:
            req = work / "request.json"
            req.write_text(json.dumps(payload), encoding="utf-8")
            argv += ["--json", str(req)]
        try:
            done = secure_subprocess.run_contained(argv, cwd=str(work), timeout=TIMEOUT_S, text=True,
                                                   encoding="utf-8", errors="replace")
        except Exception as exc:  # timeout or containment failure
            return {"ok": False, "error": True, "code": "engine_run_failed", "message": f"{type(exc).__name__}: {exc}"}
        lines = [l for l in (done.stdout or "").splitlines() if l.strip().startswith("{")]
        if not lines:
            return {"ok": False, "error": True, "code": "engine_no_output", "exit_code": done.returncode,
                    "stderr_tail": (done.stderr or "")[-2000:]}
        try:
            out = json.loads(lines[-1])
        except ValueError:
            return {"ok": False, "error": True, "code": "engine_bad_output", "exit_code": done.returncode,
                    "stdout_tail": lines[-1][:2000]}
        out["exit_code"] = done.returncode
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


def capability_report(probe_engine: bool = True) -> dict[str, Any]:
    root, py = engine_root(), kicad_python()
    report: dict[str, Any] = {
        "engine_root": str(root) if root else None, "kicad_python": str(py) if py else None,
        "tools": ["adaptive_plan", "adaptive_preview", "adaptive_apply", "adaptive_undo"],
        "operations": {
            "footprint_drag": {"supported": True, "file_level": "adaptive_plan/preview/apply/undo with a part driver",
                               "in_editor": "AdaptiveEditor IPC plugin action 'Adaptive Drag'"},
            "edge_move": {"supported": True, "file_level": "adaptive_* with an edge driver (straight, axis-aligned)",
                          "in_editor": "AdaptiveEditor IPC plugin action 'Adaptive Edge Move'"},
            "canvas_mouse_tracking": {
                "supported": False,
                "reason": ("KiCad 10.0.5's IPC API does not render uncommitted changes, so a follow-the-cursor "
                           "preview on the KiCad canvas needs native C++ in pcbnew; the plugin previews in its "
                           "own panel (drag there or use sliders) and applies as one undo step")}},
    }
    if probe_engine and root and py:
        report["engine"] = run_engine("capability", None)
    return report


def register_adaptive_tools(mcp: FastMCP) -> None:
    # covers: ADE-4
    @mcp.tool()
    def adaptive_capability() -> dict[str, Any]:
        """Adaptive engine status: where it is, KiCad's python, the engine's capability report, and what is
        supported (footprint drag, edge move) versus not (live canvas cursor tracking)."""
        return capability_report()

    @mcp.tool()
    def adaptive_plan(board_path: str, request: dict[str, Any]) -> dict[str, Any]:
        """Solve an adaptive move without writing anything. ``request`` (integer nm): {"driver": {"kind": "part",
        "uuids": [...]} | {"kind": "edge", "edge_uuid": ..., "side": "left|right|top|bottom"},
        "displacement_nm": [dx, dy], "mode": "cascade|explicit", "movers": [], "frozen": [], "priority": [],
        "eligibility_nm": null, "allow_partial": true, "copper": "adapt|fixed"}. Returns status
        (ok/partial/blocked/rejected), the change set and the report."""
        board = _board(board_path)
        if isinstance(board, dict):
            return board
        bad = _request(request)
        if bad:
            return bad
        return run_engine("plan", {"board": str(board), "request": request})

    @mcp.tool()
    def adaptive_preview(board_path: str, request: dict[str, Any], render: bool = True) -> dict[str, Any]:
        """Plan, then build a candidate copy under <board dir>/.adaptive/<session>/ (zones refilled), run DRC on
        original and candidate and optionally render it. The board itself is not modified. Returns the session
        id/dir, the original's sha256 (needed by adaptive_apply), DRC comparison and render paths."""
        board = _board(board_path)
        if isinstance(board, dict):
            return board
        bad = _request(request)
        if bad:
            return bad
        return run_engine("preview", {"board": str(board), "request": request, "render": bool(render)})

    @mcp.tool()
    def adaptive_apply(session: str, expected_sha256: str, force: bool = False,
                       board_path: str | None = None) -> dict[str, Any]:
        """Replace the board with a previewed candidate. Refused if the board changed since preview
        (sha256), or if DRC regressed unless force=true. A byte-exact backup is kept for adaptive_undo."""
        bad = _session(session, board_path)
        if bad:
            return bad
        if not isinstance(expected_sha256, str) or not _HEX64.fullmatch(expected_sha256):
            return _refusal("bad_sha256", "expected_sha256 must be the 64-hex sha256 reported by adaptive_preview")
        payload: dict[str, Any] = {"session": session, "expected_sha256": expected_sha256, "force": bool(force)}
        if board_path is not None:
            board = _board(board_path)
            if isinstance(board, dict):
                return board
            payload["board"] = str(board)
        return run_engine("apply", payload)

    @mcp.tool()
    def adaptive_undo(session: str, board_path: str | None = None) -> dict[str, Any]:
        """Restore the backup made by adaptive_apply. Refused if the board changed after the apply."""
        bad = _session(session, board_path)
        if bad:
            return bad
        payload: dict[str, Any] = {"session": session}
        if board_path is not None:
            board = _board(board_path)
            if isinstance(board, dict):
                return board
            payload["board"] = str(board)
        return run_engine("undo", payload)
