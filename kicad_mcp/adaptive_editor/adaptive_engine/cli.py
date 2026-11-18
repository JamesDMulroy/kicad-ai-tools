"""JSON-in / JSON-out command line for the adaptive engine (run with KiCad's python.exe).

    python.exe cli.py {plan|preview|apply|undo|capability} --json request.json

Prints exactly one JSON object on stdout. Exit codes: 0 ok, 2 refused, 1 error.

Request files
-------------
plan / preview  ``{"board": "<.kicad_pcb>", "request": {<engine request, ADAPTIVE-ENGINE-SPEC s3>},
                  "render": true, "kicad_cli": "<optional path>"}``
apply           ``{"session": "<session dir | session.json | id>", "expected_sha256": "<original sha>",
                  "force": false, "board": "<optional, needed only when session is a bare id>"}``
undo            ``{"session": "...", "board": "<optional>"}``
capability      no file needed.

Exit 2 (refused) is a valid request that was not carried out: an apply/undo guard said no, or the engine
returned ``rejected`` (bad uuid, missing rule, topology...) or ``blocked`` (nothing can move) with an
empty change set. The JSON then has ``"ok": false, "refused": true`` and a ``code``. Exit 1 is anything
that failed (unreadable request, missing board, pcbnew / kicad-cli failure, unexpected exception) with
``"ok": false, "error": true``. ``plan`` and ``preview`` of a partial or full result exit 0.

Native code (pcbnew, wx) may print to fd 1; when run as a script fd 1 is redirected to stderr and the
single JSON object is written to the original stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

COMMANDS = ("plan", "preview", "apply", "undo", "capability")


class CliError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


def _session_module():
    try:
        from . import session  # noqa: PLC0415
    except ImportError:
        package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
        if package_root not in sys.path:
            sys.path.insert(0, package_root)
        from adaptive_engine import session  # noqa: PLC0415
    return session


def _parse(argv: list[str]) -> tuple[str, str | None]:
    if not argv:
        raise CliError("usage", "expected one of: " + ", ".join(COMMANDS))
    command = argv[0]
    if command not in COMMANDS:
        raise CliError("usage", f"unknown command {command!r}")
    json_path = None
    i = 1
    while i < len(argv):
        if argv[i] == "--json" and i + 1 < len(argv):
            json_path = argv[i + 1]
            i += 2
        else:
            raise CliError("usage", f"unexpected argument {argv[i]!r}")
    if command != "capability" and json_path is None:
        raise CliError("usage", "--json <request.json> is required")
    return command, json_path


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CliError("bad_request_file", repr(exc))
    if not isinstance(data, dict):
        raise CliError("bad_request_file", "the request file must hold a JSON object")
    return data


def _require(data: dict, key: str, kind: type):
    value = data.get(key)
    if not isinstance(value, kind) or (kind is str and not value):
        raise CliError("bad_request", f"{key!r} is required ({kind.__name__})")
    return value


def _engine_outcome(payload: dict) -> tuple[dict, int]:
    status = payload.get("status")
    if status == "rejected":
        payload.update(ok=False, refused=True, code="rejected:" + str(payload["report"].get("reject_reason")))
        return payload, 2
    if status == "blocked":
        payload.update(ok=False, refused=True, code="blocked")
        return payload, 2
    payload["ok"] = True
    return payload, 0


def _dispatch(command: str, data: dict) -> tuple[dict, int]:
    session = _session_module()
    if command == "capability":
        payload = session.capability()
        payload["ok"] = True
        return payload, 0
    if command in ("plan", "preview"):
        board = _require(data, "board", str)
        request = _require(data, "request", dict)
        if command == "plan":
            return _engine_outcome(session.plan(board, request))
        options = {}
        if "render" in data:
            options["render"] = bool(data["render"])
        if isinstance(data.get("kicad_cli"), str):
            options["kicad_cli"] = data["kicad_cli"]
        return _engine_outcome(session.preview(board, request, **options))
    board = data.get("board") if isinstance(data.get("board"), str) else None
    sess = _require(data, "session", str)
    if command == "apply":
        sha = _require(data, "expected_sha256", str)
        force = data.get("force", False)
        if not isinstance(force, bool):
            raise CliError("bad_request", "'force' must be a boolean")
        payload = session.apply(sess, sha, force, board_path=board)
    else:
        payload = session.undo(sess, board_path=board)
    payload["ok"] = True
    return payload, 0


def _emit(out, obj: dict) -> None:
    out.write(json.dumps(obj, sort_keys=True, ensure_ascii=True, default=str) + "\n")
    out.flush()


def main(argv: list[str] | None = None, out=None) -> int:
    """Run one command; write exactly one JSON object to ``out`` (default stdout); return the exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out = sys.stdout if out is None else out
    try:
        command, json_path = _parse(argv)
        data = _load_json(json_path) if json_path else {}
        payload, code = _dispatch(command, data)
        payload["command"] = command
        _emit(out, payload)
        return code
    except Exception as exc:  # noqa: BLE001 - the CLI must always answer with one JSON object
        refused = type(exc).__name__ == "SessionRefused"
        obj = {
            "ok": False,
            "refused": refused,
            "error": not refused,
            "code": getattr(exc, "code", type(exc).__name__),
            "message": getattr(exc, "message", "") or str(exc),
            "details": getattr(exc, "details", {}) or {},
        }
        if not refused and not hasattr(exc, "code"):
            obj["trace"] = traceback.format_exc().splitlines()[-6:]
        _emit(out, obj)
        return 2 if refused else 1


def _run_as_script() -> None:
    import io

    saved = os.dup(1)
    os.dup2(2, 1)  # anything native code prints goes to stderr, not into the JSON
    out = io.TextIOWrapper(os.fdopen(saved, "wb"), encoding="utf-8", newline="\n")
    code = main(sys.argv[1:], out)
    out.flush()
    sys.exit(code)


if __name__ == "__main__":
    _run_as_script()
