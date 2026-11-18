"""Gate routes for the API commands KiCad declares but does not implement (no dependencies on purpose:
the coverage gate imports this under plain Python). Command -> MCP tool in kicad_mcp/tools/fallback_tools.py."""

ROUTES = {"GetLibraryTable": "kicad_library_table_get", "AddLibraryTableEntry": "kicad_library_table_add",
          "UpdateLibraryTableEntry": "kicad_library_table_update", "DeleteLibraryTableEntry": "kicad_library_table_delete",
          "SearchLibraries": "kicad_search_libraries", "FocusOnItem": "kicad_focus_on_item",
          "RefreshEditor": "kicad_refresh_editor", "UpdateBoardStackup": "kicad_update_board_stackup"}
