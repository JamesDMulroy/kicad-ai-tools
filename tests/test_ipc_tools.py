"""MCP IPC tools and typed-route helpers without KiCad: error paths and payload shaping."""
from __future__ import annotations

import threading
import time

from kicad_mcp.coverage import live_typed
from kicad_mcp.tools import ipc_tools


def test_unknown_session_is_a_structured_error() -> None:
    for result in (ipc_tools.api_call("nope", "GetVersion"), ipc_tools.run_action("nope", "x.y"),
                   ipc_tools.session_changes("nope"), ipc_tools.session_promote("nope"),
                   ipc_tools.session_close("nope")):
        assert result["status"] == "error" and "nope" in result["error"]["message"]


def test_session_cap_refuses_before_starting_kicad(monkeypatch) -> None:
    monkeypatch.setattr(ipc_tools, "_sessions", {str(i): object() for i in range(ipc_tools.MAX_SESSIONS)})
    result = ipc_tools.session_open("does/not/matter.kicad_sch")
    assert result["status"] == "error" and "sessions are open" in result["error"]["message"]


def test_concurrent_session_opens_respect_cap(monkeypatch) -> None:
    class SlowSession:
        def __init__(self, document):
            self.document = document

        def start(self):
            time.sleep(0.2)
            return self

    monkeypatch.setattr(ipc_tools, "_sessions", {})
    monkeypatch.setattr(ipc_tools, "_pending", 0)
    monkeypatch.setattr(ipc_tools, "HeadlessSession", SlowSession)
    monkeypatch.setattr(ipc_tools, "_describe", lambda _session: {})
    results = []
    threads = [threading.Thread(target=lambda: results.append(ipc_tools.session_open("x.kicad_pcb")))
               for _ in range(ipc_tools.MAX_SESSIONS + 3)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(result["status"] == "ok" for result in results) <= ipc_tools.MAX_SESSIONS
    finally:
        with ipc_tools._lock:
            ipc_tools._sessions.clear()


def test_failed_session_start_releases_pending_slot(monkeypatch) -> None:
    class FailedSession:
        def __init__(self, document):
            pass

        def start(self):
            raise RuntimeError("start failed")

    monkeypatch.setattr(ipc_tools, "_sessions", {})
    monkeypatch.setattr(ipc_tools, "_pending", 0)
    monkeypatch.setattr(ipc_tools, "HeadlessSession", FailedSession)
    result = ipc_tools.session_open("x.kicad_pcb")
    assert result["status"] == "error"
    assert ipc_tools._pending == 0


def test_created_id_reads_both_reply_shapes() -> None:
    assert live_typed._created_id({"created_items": [{"item": {"id": {"value": "a"}}}]}) == "a"
    assert live_typed._created_id({"items": [{"id": {"value": "b"}}]}) == "b"
    assert live_typed._created_id({"status": "IRS_OK"}) is None


def test_clone_drops_the_id_and_minimal_carries_the_type(monkeypatch) -> None:
    line = {"@type": "type.googleapis.com/kiapi.schematic.types.SchematicLine", "id": {"value": "x"},
            "start": {"x_nm": "1"}}
    monkeypatch.setattr(live_typed, "_items", lambda ctx, kot: [line])
    clone = live_typed._clone_or_minimal(None, live_typed.SCH + "SchematicLine")
    # moved so KiCad cannot merge it into the original; the source item is untouched
    assert "id" not in clone and clone["start"] == {"x_nm": str(1 + 7 * live_typed.MM)} and "id" in line
    monkeypatch.setattr(live_typed, "_items", lambda ctx, kot: [])
    minimal = live_typed._clone_or_minimal(None, live_typed.PCB + "Via")
    assert minimal["@type"].endswith("/kiapi.board.types.Via") and "position" in minimal
    assert live_typed._clone_or_minimal(None, live_typed.PCB + "Pad") is None  # nothing to clone, no minimal


def test_every_typed_action_names_a_real_item_type_or_command() -> None:
    for actions in (live_typed.SCHEMATIC_ACTIONS, live_typed.BOARD_ACTIONS):
        for name, how in actions.items():
            if isinstance(how, tuple):  # (item type, geometry or expected fields, builder)
                how, check, build = how
                assert isinstance(check, (str, dict)) and callable(build), name
            assert callable(how) or how.startswith(("kiapi.schematic.types.", "kiapi.board.types.")), name
