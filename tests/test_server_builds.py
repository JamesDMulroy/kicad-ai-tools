"""The workflow MCP server must build and list every tool: a tool whose annotations cannot be resolved breaks
the whole server at start-up, not just that tool."""
import asyncio

import pytest


def test_workflow_server_builds_and_lists_tools():
    pytest.importorskip("mcp")
    from kicad_mcp.workflow_server import create_server

    tools = {t.name for t in asyncio.run(create_server().list_tools())}
    for name in ("kicad_library_table_get", "kicad_search_libraries", "kicad_focus_on_item", "kicad_update_board_stackup",
                 "kicad_extract_embedded_file", "kicad_recover_autosave", "kicad_select_items_on_sheet"):
        assert name in tools
