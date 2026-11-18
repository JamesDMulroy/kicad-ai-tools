"""Adaptive engine MCP tools: input refusals (no engine run) and a live plan/preview/apply/undo
round trip on a synthetic board built by the AdaptiveEditor fixtures under KiCad's python.exe.
Live tests skip only when KiCad's python.exe or the engine is absent, or KICAD_MCP_ADAPTIVE_LIVE=0."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os

import pytest
from mcp.server.fastmcp import FastMCP

from kicad_mcp.tools import adaptive_tools as at
from kicad_mcp.tools.workflow_tools import register_workflow_tools
from kicad_mcp.utils import secure_subprocess


def _payload(result):
    if isinstance(result, tuple):
        result = result[1] if len(result) > 1 and isinstance(result[1], dict) else result[0]
    if isinstance(result, list):
        result = json.loads(result[0].text)
    if isinstance(result, dict) and set(result) == {"result"} and isinstance(result["result"], dict):
        result = result["result"]
    return result


@pytest.fixture
def server():
    s = FastMCP("adaptive-test")
    at.register_adaptive_tools(s)
    return s


def call(server, name, **args):
    return _payload(asyncio.run(server.call_tool(name, args)))


REQ = {"driver": {"kind": "part", "uuids": ["x"]}, "displacement_nm": [1000000, 0]}


def test_tools_are_registered(server):
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"adaptive_capability", "adaptive_plan", "adaptive_preview", "adaptive_apply", "adaptive_undo"} <= names


@pytest.mark.parametrize("path,code", [("relative.kicad_pcb", "bad_board_path"), ("", "bad_board_path"),
                                       (r"C:\nowhere\x.txt", "bad_board_path"),
                                       (r"C:\nowhere\missing.kicad_pcb", "board_not_found")])
def test_board_path_refusals(server, path, code):
    out = call(server, "adaptive_plan", board_path=path, request=REQ)
    assert out["ok"] is False and out["refused"] is True and out["code"] == code


def test_unknown_request_field_is_refused(server, tmp_path):
    board = tmp_path / "b.kicad_pcb"; board.write_text("(kicad_pcb)", encoding="utf-8")
    out = call(server, "adaptive_preview", board_path=str(board), request={**REQ, "teleport": True})
    assert out["code"] == "bad_request" and "teleport" in out["message"]


def test_apply_and_undo_input_refusals(server, tmp_path):
    board = tmp_path / "b.kicad_pcb"; board.write_text("(kicad_pcb)", encoding="utf-8")
    assert call(server, "adaptive_apply", session="", expected_sha256="0" * 64)["code"] == "bad_session"
    for sha in ("abc", "g" * 64, "0" * 63):
        out = call(server, "adaptive_apply", session="s", expected_sha256=sha, board_path=str(board))
        assert out["code"] == "bad_sha256", sha
    assert call(server, "adaptive_undo", session=" ")["code"] == "bad_session"
    # without board_path the session must be an existing session dir / session.json with a valid board
    assert call(server, "adaptive_undo", session="some-id")["code"] == "bad_session"
    sdir = tmp_path / ".adaptive" / "s1"; sdir.mkdir(parents=True)
    (sdir / "session.json").write_text('{"board_path": "relative.kicad_pcb"}', encoding="utf-8")
    assert call(server, "adaptive_undo", session=str(sdir))["code"] == "bad_session"


@pytest.mark.parametrize("request_obj,code", [
    ({"driver": {"kind": "part", "uuids": []}, "displacement_nm": [1, 0]}, "bad_driver"),
    ({"driver": {"kind": "edge", "edge_uuid": "e", "side": "north"}, "displacement_nm": [1, 0]}, "bad_driver"),
    ({"driver": {"kind": "part", "uuids": ["x"], "extra": 1}, "displacement_nm": [1, 0]}, "bad_driver"),
    ({"driver": {"kind": "part", "uuids": ["x"]}, "displacement_nm": [1.5, 0]}, "bad_displacement"),
    ({"driver": {"kind": "part", "uuids": ["x"]}, "displacement_nm": [1, 0], "mode": "teleport"}, "bad_request"),
    ({"driver": {"kind": "part", "uuids": ["x"]}, "displacement_nm": [1, 0], "allow_partial": "yes"}, "bad_request"),
])
def test_nested_request_fields_are_validated_before_any_run(server, tmp_path, request_obj, code):
    board = tmp_path / "b.kicad_pcb"; board.write_text("(kicad_pcb)", encoding="utf-8")
    out = call(server, "adaptive_plan", board_path=str(board), request=request_obj)
    assert out["ok"] is False and out["refused"] is True and out["code"] == code


def test_missing_engine_is_an_explicit_error(monkeypatch, tmp_path):
    monkeypatch.setenv(at.ENGINE_ENV, str(tmp_path / "no-engine"))
    out = at.run_engine("capability", None)
    assert out["ok"] is False and out["code"] == "engine_not_found"


def test_native_capability_reports_routes_and_the_canvas_gap():
    s = FastMCP("wf")
    register_workflow_tools(s)
    drag = call(s, "native_board_interaction_capability", operation="footprint_drag")
    canvas = call(s, "native_board_interaction_capability", operation="canvas_mouse_tracking")
    other = call(s, "native_board_interaction_capability", operation="autoroute")
    edge = call(s, "native_board_interaction_capability", operation="edge_shove")
    assert canvas["supported"] is False and "native" in canvas["reason"]
    assert other["supported"] is False and "footprint_drag" in other["known"]
    if at.engine_root() and at.kicad_python():
        assert drag["supported"] is True and "Adaptive Drag" in drag["in_editor"]
        assert edge["supported"] is True and edge["operation"] == "edge_shove"
    else:
        assert drag["supported"] is False


# ---- live -------------------------------------------------------------------------------------

def _live_reason():
    if os.environ.get("KICAD_MCP_ADAPTIVE_LIVE") == "0":
        return "KICAD_MCP_ADAPTIVE_LIVE=0"
    if at.engine_root() is None:
        return "adaptive engine not found"
    if at.kicad_python() is None:
        return "KiCad python.exe not found"
    return None


live = pytest.mark.skipif(_live_reason() is not None, reason=str(_live_reason()))

BUILD = r'''
import os, sys
sys.path.insert(0, sys.argv[2])
from tests import fixtures
fixtures.build(sys.argv[1], "live", outline="rect")
import pcbnew
b = pcbnew.LoadBoard(os.path.join(sys.argv[1], "live.kicad_pcb"))
print("R1=" + [f.m_Uuid.AsString() for f in b.GetFootprints() if f.GetReference() == "R1"][0])
'''


@pytest.fixture(scope="module")
def live_board(tmp_path_factory):
    work = tmp_path_factory.mktemp("adaptive-live")
    script = work / "mk.py"; script.write_text(BUILD, encoding="utf-8")
    done = secure_subprocess.run_contained([str(at.kicad_python()), "-X", "utf8", "-B", str(script),
                                            str(work / "b"), str(at.engine_root())],
                                           timeout=300, text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr[-2000:]
    r1 = [l.split("=", 1)[1].strip() for l in done.stdout.splitlines() if l.startswith("R1=")][0]
    return work / "b" / "live.kicad_pcb", r1


@live
def test_capability_runs_the_engine(server):
    out = call(server, "adaptive_capability")
    assert out["engine"]["ok"] is True, out
    assert out["operations"]["canvas_mouse_tracking"]["supported"] is False


@live
def test_plan_preview_apply_undo_round_trip(server, live_board):
    board, r1 = live_board
    original = board.read_bytes()
    req = {"driver": {"kind": "part", "uuids": [r1]}, "displacement_nm": [6_000_000, 0], "mode": "cascade",
           "allow_partial": True, "copper": "adapt"}
    plan = call(server, "adaptive_plan", board_path=str(board), request=req)
    assert plan["ok"] is True and plan["status"] in ("ok", "partial"), plan
    assert r1 in {m["uuid"] for m in plan["changeset"]["part_moves"]}
    assert board.read_bytes() == original  # plan writes nothing

    prev = call(server, "adaptive_preview", board_path=str(board), request=req, render=False)
    assert prev["ok"] is True, prev
    assert board.read_bytes() == original  # preview never touches the board
    sha = hashlib.sha256(original).hexdigest()
    session = prev.get("session_dir") or prev.get("session") or prev.get("session_id")
    assert session, prev

    applied = call(server, "adaptive_apply", session=str(session), expected_sha256=sha, force=True,
                   board_path=str(board))
    assert applied["ok"] is True, applied
    assert board.read_bytes() != original

    undone = call(server, "adaptive_undo", session=str(session), board_path=str(board))
    assert undone["ok"] is True, undone
    assert board.read_bytes() == original


@live
def test_stale_sha_is_refused(server, live_board):
    board, r1 = live_board
    req = {"driver": {"kind": "part", "uuids": [r1]}, "displacement_nm": [2_000_000, 0]}
    prev = call(server, "adaptive_preview", board_path=str(board), request=req, render=False)
    assert prev["ok"] is True, prev
    session = prev.get("session_dir") or prev.get("session") or prev.get("session_id")
    out = call(server, "adaptive_apply", session=str(session), expected_sha256="0" * 64, board_path=str(board))
    assert out["ok"] is False and out["refused"] is True, out


def test_a_session_id_must_be_a_plain_name_of_this_boards_session(tmp_path):
    board = tmp_path / "proj" / "b.kicad_pcb"
    board.parent.mkdir()
    board.write_text("(kicad_pcb)", encoding="utf-8")
    other = tmp_path / "other" / "o.kicad_pcb"
    other.parent.mkdir()
    other.write_text("(kicad_pcb)", encoding="utf-8")
    for bad in ("..", "../x", "a/b", "a\\b", "C:x", str(tmp_path / "no-session-here")):
        refusal = at._session(bad, str(board))
        assert refusal and refusal["code"] == "bad_session", bad
    sdir = board.parent / ".adaptive" / "s1"
    sdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({"board_path": str(other)}), encoding="utf-8")
    refusal = at._session("s1", str(board))  # a session journal that names another board
    assert refusal and refusal["code"] == "bad_session" and "different board" in refusal["message"]
    assert at._session(str(sdir), str(board))["code"] == "bad_session"  # also by directory
    (sdir / "session.json").write_text(json.dumps({"board_path": str(board)}), encoding="utf-8")
    assert at._session("s1", str(board)) is None
    assert at._session(str(sdir), str(board)) is None
