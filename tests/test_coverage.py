"""Coverage inventory and gate, against a synthetic KiCad source tree (no KiCad needed)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp.coverage import gate
from kicad_mcp.coverage import inventory as inv

HANDLER = """
API_HANDLER_SCH::API_HANDLER_SCH( SCH_EDIT_FRAME* aFrame )
{
    registerHandler<GetSchematicNetlist, SchematicNetlistResponse>( &API_HANDLER_SCH::handleNetlist );
    registerHandler<commands::SaveDocument, google::protobuf::Empty>( &API_HANDLER_SCH::handleSave );
}
"""
PROTO = """
syntax = "proto3";
package kiapi.schematic.commands;
message GetSchematicNetlist { kiapi.common.types.DocumentSpecifier document = 1; }
message SchematicNetlistResponse { repeated string nets = 1; }
message SaveOptions { bool overwrite = 1; }
message SaveCopy { string path = 1; SaveOptions options = 2; }
message FocusOnItem { kiapi.common.types.KIID item = 1; }
"""
ACTIONS = """
TOOL_ACTION SCH_ACTIONS::annotate( TOOL_ACTION_ARGS()
        .Name( "eeschema.EditorControl.annotate" )
        .Scope( AS_GLOBAL )
        .FriendlyName( _( "Annotate Schematic..." ) )
        .Tooltip( _( "Fill in \\"reference\\" designators" ) ) );

TOOL_ACTION SCH_ACTIONS::placeSymbol( TOOL_ACTION_ARGS()
        .Name( "eeschema.InteractiveDrawing.placeSymbol" )
        .Scope( AS_GLOBAL )
        .Flags( AF_ACTIVATE )
        .Parameter<SCH_ACTIONS::PLACE_SYMBOL_PARAMS>( {} ) );

TOOL_ACTION SCH_ACTIONS::setLayer( TOOL_ACTION_ARGS()
        .Name( "eeschema.Edit.setLayer" )
        .Parameter<int>( 0 ) );

// No description, it is not supposed to be shown anywhere
TOOL_ACTION EDA_3D_ACTIONS::controlActivate( TOOL_ACTION_ARGS()
        .Name( "3DViewer.Control" )
        .Scope( AS_GLOBAL )
        .Flags( AF_ACTIVATE ) );

static TOOL_ACTION_ARGS makeArgs() { return foo( 1 ); }
static const TOOL_ACTION ACT_Bare = make( bar(), TOOL_ACTION_ARGS().Name( "pcbnew.Router.bare" ) );
"""


@pytest.fixture(autouse=True)
def _gate_without_live_typed_routes(monkeypatch) -> None:
    """Gate tests see only the routes they set, not the live typed-route table."""
    monkeypatch.setattr(gate, "typed_routes", lambda: dict(gate.TYPED_ROUTES))


@pytest.fixture
def source(tmp_path: Path) -> Path:
    (tmp_path / "eeschema" / "api").mkdir(parents=True)
    (tmp_path / "eeschema" / "api" / "api_handler_sch.cpp").write_text(HANDLER, encoding="utf-8")
    (tmp_path / "eeschema" / "tools").mkdir()
    (tmp_path / "eeschema" / "tools" / "sch_actions.cpp").write_text(ACTIONS, encoding="utf-8")
    (tmp_path / "api" / "proto" / "schematic").mkdir(parents=True)
    (tmp_path / "api" / "proto" / "schematic" / "schematic_commands.proto").write_text(PROTO, encoding="utf-8")
    return tmp_path


def test_api_handled_declared_and_helpers(source: Path) -> None:
    commands = {c.request: c for c in inv.scan_api(source)}
    assert commands["GetSchematicNetlist"].status == "handled"
    assert commands["SaveDocument"].status == "handled"  # namespace stripped
    assert commands["FocusOnItem"].status == "declared_only"  # declared, no handler
    assert "SaveOptions" not in commands  # a field type inside another message is a helper
    assert "SchematicNetlistResponse" not in commands
    assert "SaveCopy" in commands and commands["SaveCopy"].status == "declared_only"


def test_incomplete_source_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "eeschema" / "api").mkdir(parents=True)
    (tmp_path / "eeschema" / "api" / "api_handler_sch.cpp").write_text(HANDLER, encoding="utf-8")
    with pytest.raises(SystemExit, match="incomplete KiCad source"):
        inv.scan_api(tmp_path)  # no api/proto: never report "nothing declared"


def test_actions_classified(source: Path) -> None:
    actions = {a.name: a for a in inv.scan_actions(source)}
    annotate = actions["eeschema.EditorControl.annotate"]
    assert annotate.runnable and annotate.parameter is None and annotate.flags == "AF_NONE"
    assert annotate.friendly_name == "Annotate Schematic..."
    place = actions["eeschema.InteractiveDrawing.placeSymbol"]
    assert not place.runnable and place.parameter == "SCH_ACTIONS::PLACE_SYMBOL_PARAMS"  # AF_ACTIVATE
    assert place.caller_parameter
    set_layer = actions["eeschema.Edit.setLayer"]
    assert set_layer.parameter == "int" and set_layer.runnable and not set_layer.caller_parameter
    # TOOL_ACTION_ARGS() outside a TOOL_ACTION( wrapper: still found, and the scan terminates.
    assert actions["pcbnew.Router.bare"].runnable
    assert "3DViewer.Control" not in actions  # a hidden tool registration, not an action
    assert annotate.source.startswith("eeschema/tools/sch_actions.cpp:")


def _inventory(source: Path) -> dict:
    return inv.Inventory("10.99.0-test", "abc123", inv.scan_api(source), inv.scan_actions(source),
                         ["sch erc"]).to_json()


def test_gate_states(source: Path) -> None:
    result = gate.evaluate(_inventory(source), None)
    states = {r["item"]: r["state"] for r in result["rows"]}
    assert states["api:GetSchematicNetlist"] == "unverified"
    assert states["api:FocusOnItem"] == "upstream_gap"
    assert states["action:eeschema.EditorControl.annotate"] == "unverified"
    assert states["action:eeschema.InteractiveDrawing.placeSymbol"] == "missing"
    assert states["action:eeschema.Edit.setLayer"] == "unverified"  # its own parameter: runnable by name
    assert states["cli:sch erc"] == "unverified"
    assert result["passed"] is False


def test_gate_passes_only_when_everything_verified(source: Path, monkeypatch) -> None:
    data = _inventory(source)
    every = [i for i, _ in gate.item_ids(data)]
    monkeypatch.setattr(gate, "TYPED_ROUTES", {
        "action:eeschema.InteractiveDrawing.placeSymbol": "sch_place_symbol",
        "api:FocusOnItem": "sch_focus", "api:SaveCopy": "sch_save_copy"})
    assert gate.evaluate(data, {"commit": "abc123", "passed": every})["passed"] is True
    assert gate.evaluate(data, {"commit": "abc123", "passed": every[:-1]})["passed"] is False


def test_gate_rejects_evidence_for_another_build(source: Path) -> None:
    with pytest.raises(SystemExit, match="evidence is for commit"):
        gate.evaluate(_inventory(source), {"commit": "other", "passed": []})


def test_report_lists_every_gap(source: Path) -> None:
    text = gate.report_markdown(gate.evaluate(_inventory(source), None))
    assert "Gate: **FAIL**" in text and "`api:FocusOnItem`" in text
    assert "`action:eeschema.InteractiveDrawing.placeSymbol`" in text
    json.dumps(gate.evaluate(_inventory(source), None))  # serialisable
