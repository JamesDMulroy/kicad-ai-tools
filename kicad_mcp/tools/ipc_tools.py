"""MCP tools over KiCad's official IPC API (KiCad 11+), for any document type and any command.

Work happens in sessions. A session serves a candidate copy of the project (headless, or the real
editor on a private desktop for GUI-only commands and editor actions); nothing reaches the user's files
until ``kicad_session_promote``, which refuses if an original changed since the copy was taken and
backs up every file it replaces. ``kicad_session_changes`` shows what would be written, with KiCad's
own diff of each changed document, before anything is promoted.

Commands and their request schemas come from the installed KiCad build itself, so every command that
build defines is reachable, including commands newer than this package.
"""
from __future__ import annotations

import atexit
import contextlib
import subprocess
import sys
import threading
from typing import Any, Literal
import uuid

from mcp.server.fastmcp import FastMCP

from kicad_mcp.ipc import api
from kicad_mcp.ipc.session import HeadlessSession, SessionError

MAX_SESSIONS = 4  # each is a KiCad process; the cap keeps a busy machine responsive
_sessions: dict[str, Any] = {}
_pending = 0  # reserved slots for sessions still starting
_runners: dict[str, Any] = {}  # session id -> HotkeyRunner (schematic sessions)
_lock = threading.Lock()


def _get(session_id: str):
    session = _sessions.get(session_id)
    if session is None:
        raise SessionError(f"no open session {session_id!r}; open one with kicad_session_open")
    return session


def _error(exc: Exception) -> dict[str, Any]:
    status = getattr(exc, "status", "") or type(exc).__name__
    return {"status": "error", "error": {"code": status, "message": str(exc)}}


def session_open(document: str, mode: Literal["headless", "gui"] = "headless") -> dict[str, Any]:
    global _pending
    with _lock:
        if len(_sessions) + _pending >= MAX_SESSIONS:
            return _error(SessionError(f"{MAX_SESSIONS} sessions are open; close one first"))
        _pending += 1
    inserted = False
    try:
        if mode == "gui":
            from kicad_mcp.ipc.gui_session import GuiSession

            session = GuiSession(document).start()
        else:
            session = HeadlessSession(document).start()
        session_id = uuid.uuid4().hex[:12]
        with _lock:
            _sessions[session_id] = session
            _pending -= 1
            inserted = True
        return {"status": "ok", "session_id": session_id, **_describe(session)}
    except Exception as exc:  # noqa: BLE001 - every failure is a structured answer to the caller
        return _error(exc)
    finally:
        if not inserted:
            with _lock:
                _pending -= 1


def _describe(session) -> dict[str, Any]:
    return {"mode": "headless" if isinstance(session, HeadlessSession) else "gui",
            "kind": session.kind, "kicad_commit": session.commit, "document": str(session.document),
            "source_project": str(session.candidate.source_dir), "pid": session.pid}


def session_list() -> dict[str, Any]:
    with _lock:
        return {"sessions": [{"session_id": sid, **_describe(s)} for sid, s in _sessions.items()]}


def session_close(session_id: str, discard: bool = True) -> dict[str, Any]:
    with _lock:
        session = _sessions.pop(session_id, None)
        _runners.pop(session_id, None)
    if session is None:
        return _error(SessionError(f"no open session {session_id!r}"))
    session.close(discard=discard)
    return {"status": "ok", "closed": session_id, "candidate_discarded": discard}


def session_changes(session_id: str) -> dict[str, Any]:
    """Changed candidate files, each with KiCad's own diff against the original when it is a document."""
    try:
        session = _get(session_id)
    except SessionError as exc:
        return _error(exc)
    changes = []
    for name in session.candidate.changed_files():
        entry: dict[str, Any] = {"file": name}
        original = session.candidate.source_dir / name
        kind = {".kicad_sch": "sch", ".kicad_pcb": "pcb"}.get(original.suffix)
        if kind and original.exists():
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            diff = subprocess.run([session.cli, kind, "diff", "--format", "text", str(original),
                                   str(session.candidate.work_dir / name)], capture_output=True, text=True,
                                  timeout=300, creationflags=flags)
            entry["kicad_diff"] = (diff.stdout or diff.stderr).strip()[:20000]
        changes.append(entry)
    return {"status": "ok", "changes": changes}


def session_promote(session_id: str) -> dict[str, Any]:
    try:
        session = _get(session_id)
        api.call(session, "SaveDocument", {})  # the editor's in-memory state to the candidate files first
        return {"status": "ok", **session.candidate.promote()}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def api_commands(contains: str = "") -> dict[str, Any]:
    """Every request message the installed KiCad defines (optionally filtered by a substring)."""
    try:
        from kicad_mcp.ipc.schema import load
        from kicad_mcp.ipc.session import find_kicad_cli, installed_commit

        schema = load(installed_commit(find_kicad_cli()))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    names = sorted(d.name for d in schema.messages.values()
                   if d.file.package in api.COMMAND_PACKAGES and not d.name.endswith("Response")
                   and contains.lower() in d.name.lower())
    return {"status": "ok", "kicad_commit": schema.commit, "commands": names}


def api_describe(command: str) -> dict[str, Any]:
    try:
        from kicad_mcp.ipc.schema import load
        from kicad_mcp.ipc.session import find_kicad_cli, installed_commit

        return {"status": "ok", **api.describe(load(installed_commit(find_kicad_cli())), command)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def api_call(session_id: str, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        return {"status": "ok", **api.call(_get(session_id), command, payload)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def run_action(session_id: str, action: str, route: str = "hotkey") -> dict[str, Any]:
    """Run an editor action. The default route is a private-profile hotkey, which keeps the action's own
    parameter; route="runaction" uses the API's RunAction (board editor only), which in KiCad 11 nightlies
    replaces every action's parameter with `true` (see docs/IPC-API-COVERAGE.md), so it is only right for
    actions that take none."""
    try:
        session = _get(session_id)
        if isinstance(session, HeadlessSession):
            raise SessionError("editor actions need a GUI session (kicad_session_open with mode='gui')")
        if route == "runaction":
            if session.kind != "pcb":
                raise SessionError("RunAction exists only in the board editor's API")
            from kicad_mcp.coverage.live_actions import (
                _run_once as run,  # once, no selection changes
            )

            outcome, detail = run(session, action)
            return {"status": "ok" if outcome == "ok" else "error", "outcome": outcome, "detail": detail}
        from kicad_mcp.ipc.hotkeys import HotkeyRunner

        runner = _runners.setdefault(session_id, HotkeyRunner(session))
        title = "PCB Editor" if session.kind == "pcb" else "Schematic Editor"
        result = runner.run(action, title)
        return {"status": "ok", "outcome": "dispatched", **result}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def close_all() -> None:
    with _lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        with contextlib.suppress(Exception):  # best effort at shutdown: close every session
            session.close(discard=True)


atexit.register(close_all)


def register_ipc_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def kicad_session_open(document: str, mode: Literal["headless", "gui"] = "headless") -> dict[str, Any]:
        """Open a session on a .kicad_sch or .kicad_pcb, served from a private candidate copy of its project.

        mode="headless" (no window) covers most API commands; mode="gui" runs the real editor on a private,
        never-shown desktop for GUI-only commands (selection, layers, appearance) and editor actions.
        Nothing reaches the original files until kicad_session_promote."""
        return session_open(document, mode)

    @mcp.tool()
    def kicad_session_list() -> dict[str, Any]:
        """Open sessions: id, mode, document kind, KiCad commit, source project, process id."""
        return session_list()

    @mcp.tool()
    def kicad_session_close(session_id: str, discard: bool = True) -> dict[str, Any]:
        """End a session and its KiCad process; discard=True also deletes its candidate copy."""
        return session_close(session_id, discard)

    @mcp.tool()
    def kicad_session_changes(session_id: str) -> dict[str, Any]:
        """What promote would write: changed files, each with KiCad's own diff against the original."""
        return session_changes(session_id)

    @mcp.tool()
    def kicad_session_promote(session_id: str) -> dict[str, Any]:
        """Save the session's document and write its changed files back to the real project.

        Refuses if any original changed since the session opened; every replaced file is backed up first
        (the result names the backup folder)."""
        return session_promote(session_id)

    @mcp.tool()
    def kicad_api_commands(contains: str = "") -> dict[str, Any]:
        """Every API command the installed KiCad defines (filter by substring)."""
        return api_commands(contains)

    @mcp.tool()
    def kicad_api_describe(command: str) -> dict[str, Any]:
        """A command's request schema (fields, types, enum values) from the installed KiCad."""
        return api_describe(command)

    @mcp.tool()
    def kicad_api_call(session_id: str, command: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run any KiCad API command in a session. payload is the request as JSON (see kicad_api_describe);
        unknown fields are refused, and the session's document is filled in where the request takes one."""
        return api_call(session_id, command, payload)

    @mcp.tool()
    def kicad_run_action(session_id: str, action: str,
                         route: Literal["hotkey", "runaction"] = "hotkey") -> dict[str, Any]:
        """Run a KiCad editor action by name (e.g. pcbnew.ZoneFiller.zoneFillAll) in a GUI session, and report
        what it visibly did. The default hotkey route keeps the action's own parameter; route="runaction"
        (board only) uses the API's RunAction, which in current nightlies passes the wrong parameter to
        actions that carry one. A dialog the action opens is closed and reported."""
        return run_action(session_id, action, route)
