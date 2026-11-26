"""MCP tools for the API commands KiCad declares but does not implement.

The proto of the KiCad build declares GetLibraryTable, AddLibraryTableEntry, UpdateLibraryTableEntry,
DeleteLibraryTableEntry, SearchLibraries, FocusOnItem, RefreshEditor and UpdateBoardStackup, yet no
handler answers them ("declared_only" in the coverage inventory). Each function here does what the proto
describes by other means, on a session (a candidate copy of the project, so nothing reaches the real
files until ``kicad_session_promote``):

- library tables and library search: the table files themselves (``kicad_mcp.ipc.libtables``);
- FocusOnItem: resolves a footprint/pad/symbol/sheet to item ids and runs the handled FocusOnItems
  (zoom to fit), plus a selection highlight;
- RefreshEditor: the editor's own Redraw action (common.Control.zoomRedraw);
- UpdateBoardStackup: rewrites the board file's stackup block and reloads the document into the editor
  (RevertDocument), then returns the editor's own normalized readback.

Every function takes a session object (a GuiSession for the editor-bound ones) and returns JSON-ready
dicts shaped like the proto's responses; the ``register_fallback_tools`` wrappers take a session id.
"""
from __future__ import annotations

import contextlib
import re
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from kicad_mcp.ipc import api, libtables
from kicad_mcp.ipc.session import SessionError

FRAMES = {"FT_PCB_EDITOR": "pcb", "FT_SCHEMATIC_EDITOR": "schematic"}
FRAME_TITLES = {"pcb": "PCB Editor", "schematic": "Schematic Editor"}
REDRAW_ACTION = "common.Control.zoomRedraw"
_RUNNERS: dict[int, Any] = {}


# --- library tables / search -----------------------------------------------------------------------


# Library-table literals at module level: tool annotations are strings (postponed), and FastMCP resolves
# them against module globals, not the registering function's locals.
Scope = Literal["LTS_GLOBAL", "LTS_PROJECT", "LTS_BOTH"]
Kind = Literal["LT_SYMBOL", "LT_FOOTPRINT", "LT_DESIGN_BLOCK"]

def _tables(session) -> libtables.TableSet:
    return libtables.TableSet.of(session)


def get_library_table(session, type: str, scope: str = "LTS_BOTH", substituted: bool = False) -> dict[str, Any]:
    return libtables.get_library_table(_tables(session), type, scope, substituted)


def add_library_table_entry(session, entry: dict[str, Any], scope: str) -> dict[str, Any]:
    return libtables.add_library_table_entry(_tables(session), entry, scope)


def update_library_table_entry(session, entry: dict[str, Any], scope: str) -> dict[str, Any]:
    return libtables.update_library_table_entry(_tables(session), entry, scope)


def delete_library_table_entry(session, type: str, nickname: str, scope: str) -> dict[str, Any]:
    return libtables.delete_library_table_entry(_tables(session), type, nickname, scope)


def search_libraries(session, type: str, query: str, names_only: bool = False,
                     libraries: list[str] | None = None) -> dict[str, Any]:
    return libtables.search_libraries(_tables(session), type, query, names_only, libraries)


def reload_tables(session) -> dict[str, Any]:
    """Make the running editor read the (edited) library tables: save its document into the candidate,
    then restart the editor on the same candidate and profile. Library tables are read at start-up and
    on project change; KiCad has no API command that re-reads them."""
    api.call(session, "SaveDocument", {})
    session.restart()
    return {"status": "ok", "pid": session.pid}


# --- FocusOnItem -----------------------------------------------------------------------------------

def _kiid(item: dict[str, Any]) -> str:
    return item.get("id", {}).get("value", "")


def _find_footprint(session, reference: str) -> dict[str, Any] | None:
    items = api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"].get("items", [])
    for item in items:
        if item.get("reference_field", {}).get("text", {}).get("text", {}).get("text") == reference:
            return item
    return None


def _find_symbol(session, reference: str) -> dict[str, Any] | None:
    items = api.call(session, "GetItems", {"types": ["KOT_SCH_SYMBOL"]})["response"].get("items", [])
    for item in items:
        if item.get("reference_field", {}).get("text", {}).get("text") == reference:
            return item
    return None


def _sheet_items(session, sheet_path: dict[str, Any]) -> list[str]:
    from google.protobuf import json_format

    document = json_format.MessageToDict(session.document_spec(), preserving_proto_field_name=True)
    document["sheet_path"] = sheet_path
    ids: list[str] = []
    for kind in ("KOT_SCH_SYMBOL", "KOT_SCH_LINE", "KOT_SCH_LABEL", "KOT_SCH_SHEET", "KOT_SCH_TEXT",
                 "KOT_SCH_JUNCTION", "KOT_SCH_GLOBAL_LABEL"):
        with contextlib.suppress(Exception):  # a type the sheet has none of is not an error
            got = api.call(session, "GetItems", {"header": {"document": document}, "types": [kind]})
            ids += [_kiid(i) for i in got["response"].get("items", [])]
    return [i for i in ids if i]


def focus_on_item(session, focus_item: dict[str, Any], highlight: bool = True,
                  margin_nm: int | None = None) -> dict[str, Any]:
    """FocusOnItem: bring one item into view. ``focus_item`` is a SelectionSpec as JSON:
    ``{"footprint": {"reference": "U1"}}``, ``{"pad": {"reference": "U1", "number": "3"}}`` or
    ``{"sheet_path": {"path": [...], "path_human_readable": "/"}}``. In a schematic a footprint spec names
    the symbol with that reference and a pad spec focuses its symbol (pins have no ids of their own).
    The view is zoomed to fit the item (the handled FocusOnItems); with ``highlight`` the item also becomes
    the selection, which replaces what was selected before (KiCad's own focus brightens the item and
    un-brightens the previous one, and a selection is the highlight the API can set)."""
    kind = session.kind
    document: dict[str, Any] | None = None
    if "footprint" in focus_item or "pad" in focus_item:
        spec = focus_item.get("footprint") or focus_item["pad"]
        reference = spec.get("reference", "")
        parent = _find_footprint(session, reference) if kind == "pcb" else _find_symbol(session, reference)
        if parent is None:
            raise SessionError(f"no {'footprint' if kind == 'pcb' else 'symbol'} with reference {reference!r}")
        target, label = _kiid(parent), f"{'footprint' if kind == 'pcb' else 'symbol'} {reference}"
        if "pad" in focus_item and kind == "pcb":
            number = focus_item["pad"].get("number", "")
            pads = [s for s in parent["definition"]["items"]
                    if s.get("@type", "").endswith("kiapi.board.types.Pad") and s.get("number") == number]
            if not pads:
                raise SessionError(f"footprint {reference} has no pad {number!r}")
            target, label = _kiid(pads[0]), f"pad {reference}.{number}"
        ids = [target]
    elif "sheet_path" in focus_item:
        if kind != "schematic":
            raise SessionError("a sheet path can only be focused in a schematic session")
        from google.protobuf import json_format

        document = json_format.MessageToDict(session.document_spec(), preserving_proto_field_name=True)
        document["sheet_path"] = focus_item["sheet_path"]
        ids = _sheet_items(session, focus_item["sheet_path"])
        label = f"sheet {focus_item['sheet_path'].get('path_human_readable', '')}"
        if not ids:
            raise SessionError("that sheet has no items to focus on")
    else:
        raise SessionError("focus_item needs one of footprint, pad or sheet_path")
    request: dict[str, Any] = {"items": [{"value": i} for i in ids]}
    if document is not None:
        request["document"] = document
    if margin_nm is not None:
        request["margin"] = {"value_nm": str(margin_nm)}
    api.call(session, "FocusOnItems", request)
    selected = False
    if highlight and "sheet_path" not in focus_item:
        api.call(session, "ClearSelection", {})
        api.call(session, "AddToSelection", {"items": [{"value": i} for i in ids]})
        selected = True
    return {"status": "CS_OK", "message": f"focused on {label}", "items": ids, "selected": selected}


# --- RefreshEditor ---------------------------------------------------------------------------------

def refresh_editor(session, frame: str = "FT_PCB_EDITOR") -> dict[str, Any]:
    """RefreshEditor: redraw the given frame if it is open in the session. The board editor takes the
    handled RunAction; other frames take the same action by its hotkey (see ``kicad_run_action``)."""
    kind = FRAMES.get(frame)
    if kind is None:
        return {"refreshed": False, "reason": f"{frame} is not an editor a session can open"}
    if session.kind != kind:
        return {"refreshed": False, "reason": f"the session's editor is the {FRAME_TITLES[session.kind]}, "
                                              f"{frame} is not open"}
    if kind == "pcb":
        api.call(session, "RunAction", {"action": REDRAW_ACTION})
    else:
        from kicad_mcp.ipc.hotkeys import HotkeyRunner

        runner = _RUNNERS.setdefault(id(session), HotkeyRunner(session))  # a first use restarts the editor
        runner.run(REDRAW_ACTION, FRAME_TITLES[kind])
    return {"refreshed": True, "frame": frame, "action": REDRAW_ACTION}


# --- UpdateBoardStackup ----------------------------------------------------------------------------

_LAYER_NAMES = {"BL_F_Cu": "F.Cu", "BL_B_Cu": "B.Cu", "BL_F_SilkS": "F.SilkS", "BL_B_SilkS": "B.SilkS",
                "BL_F_Paste": "F.Paste", "BL_B_Paste": "B.Paste", "BL_F_Mask": "F.Mask", "BL_B_Mask": "B.Mask"}
_TYPE_NAMES = {("BSLT_SILKSCREEN", True): "Top Silk Screen", ("BSLT_SILKSCREEN", False): "Bottom Silk Screen",
               ("BSLT_SOLDERPASTE", True): "Top Solder Paste", ("BSLT_SOLDERPASTE", False): "Bottom Solder Paste",
               ("BSLT_SOLDERMASK", True): "Top Solder Mask", ("BSLT_SOLDERMASK", False): "Bottom Solder Mask"}
_EDGE = {"BECT_PLAIN": "yes", "BECT_BEVELED": "bevelled"}


def _layer_name(layer: str) -> str:
    if layer in _LAYER_NAMES:
        return _LAYER_NAMES[layer]
    match = re.fullmatch(r"BL_In(\d+)_Cu", layer)
    if match:
        return f"In{match.group(1)}.Cu"
    raise SessionError(f"unsupported stackup layer {layer!r}")


def _mm(distance: dict[str, Any] | None) -> str:
    nm = int((distance or {}).get("value_nm", 0))
    text = f"{nm / 1e6:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _num(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def _color(color: dict[str, Any] | None) -> str | None:
    if not color or not any(color.get(c) for c in ("r", "g", "b", "a")):
        return None
    channels = [round(255 * float(color.get(c, 0))) for c in ("r", "g", "b", "a")]
    return "#" + "".join(f"{c:02X}" for c in channels)


def _q(text: str) -> str:
    return libtables.quote(text)


def stackup_block(stackup: dict[str, Any], indent: str = "\t\t") -> str:
    """The board file's ``(stackup ...)`` block for a BoardStackup (JSON of the proto), in KiCad's format."""
    i1, i2 = indent + "\t", indent + "\t\t"
    out = [f"{indent}(stackup"]
    dielectrics = 0
    for layer in stackup.get("layers", []):
        kind, name = layer.get("type", ""), layer.get("layer", "")
        lines: list[str] = []
        if kind == "BSLT_DIELECTRIC":
            dielectrics += 1
            group = layer.get("dielectric") or {}
            dtype = {"BSDT_PREPREG": "prepreg"}.get(group.get("type", ""), "core")
            title = f"dielectric {dielectrics}"
            lines.append(f"(type {_q(dtype)})")
            for n, props in enumerate(group.get("layer") or [{}]):
                prefix = "addsublayer " if n else ""
                parts = [f"(thickness {_mm(props.get('thickness'))}"
                         + (" locked" if props.get("thickness_locked") else "") + ")"]
                material = props.get("material_name", "")
                if material:
                    parts.append(f"(material {_q(material)})")
                    parts.append(f"(epsilon_r {_num(props.get('epsilon_r', 0))})")
                    parts.append(f"(loss_tangent {_num(props.get('loss_tangent', 0))})")
                    if props.get("spec_frequency"):
                        parts.append(f"(spec_frequency {_num(props['spec_frequency'])})")
                        model = "djordjevic_sarkar" if props.get("dielectric_model") == "DM_DJORDJEVIC_SARKAR" \
                            else "constant"
                        parts.append(f"(dielectric_model {model})")
                lines += [prefix.strip()] if prefix else []
                lines += parts
        else:
            title = _layer_name(name)
            top = not name.startswith("BL_B_")
            lines.append(f"(type {_q('copper' if kind == 'BSLT_COPPER' else _TYPE_NAMES[(kind, top)])})"
                         if kind in ("BSLT_COPPER", "BSLT_SILKSCREEN", "BSLT_SOLDERPASTE", "BSLT_SOLDERMASK")
                         else "")
            colour = _color(layer.get("color"))
            if colour and kind != "BSLT_COPPER":
                lines.append(f"(color {_q(colour)})")
            if kind in ("BSLT_COPPER", "BSLT_SOLDERMASK"):
                lines.append(f"(thickness {_mm(layer.get('thickness'))})")
            if kind == "BSLT_SOLDERMASK":
                mask = layer.get("soldermask") or {}
                material = mask.get("material_name", "")
                if material and material != "Not specified":
                    lines += [f"(material {_q(material)})", f"(epsilon_r {_num(mask.get('epsilon_r', 0))})",
                              f"(loss_tangent {_num(mask.get('loss_tangent', 0))})"]
            if kind == "BSLT_SILKSCREEN":
                material = (layer.get("silkscreen") or {}).get("material_name", "")
                if material and material != "Not specified":
                    lines.append(f"(material {_q(material)})")
            if not lines[0]:
                raise SessionError(f"unsupported stackup layer type {kind!r} for {name!r}")
        out.append(f"{i1}(layer {_q(title)}")
        out += [f"{i2}{line}" for line in lines]
        out.append(f"{i1})")
    finish = (stackup.get("finish") or {}).get("type_name", "")
    if finish:
        out.append(f"{i1}(copper_finish {_q(finish)})")
    out.append(f"{i1}(dielectric_constraints {'yes' if (stackup.get('impedance') or {}).get('is_controlled') else 'no'})")
    connector = _EDGE.get(((stackup.get("edge") or {}).get("connector") or {}).get("type", ""))
    if connector:
        out.append(f"{i1}(edge_connector {connector})")
    if ((stackup.get("edge") or {}).get("plating") or {}).get("has_edge_plating"):
        out.append(f"{i1}(edge_plating yes)")
    out.append(f"{indent})")
    return "\n".join(out)


def _block_span(text: str, token: str) -> tuple[int, int]:
    """Start (of the line's indentation) and end of the s-expression opened by ``(token`` in a file."""
    start = text.find(f"({token}")
    if start < 0:
        raise SessionError(f"the board file has no ({token} block")
    depth, pos, in_string = 0, start, False
    while pos < len(text):
        c = text[pos]
        if in_string:
            if c == "\\":
                pos += 1
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                break
        pos += 1
    if depth != 0:
        raise SessionError(f"the board file's ({token} block is not closed")
    line_start = text.rfind("\n", 0, start) + 1
    return line_start, pos + 1


def _copper_layers(stackup: dict[str, Any]) -> list[str]:
    return [layer["layer"] for layer in stackup.get("layers", []) if layer.get("type") == "BSLT_COPPER"]


def update_board_stackup(session, stackup: dict[str, Any], settle_s: float = 2.0) -> dict[str, Any]:
    """UpdateBoardStackup: replace the board's stackup with ``stackup`` (a BoardStackup as JSON, as
    GetBoardStackup returns it) and return the editor's readback, in normalized form.

    The editor's document is saved to the candidate file, the file's stackup block is rewritten, and the
    editor reloads the file (RevertDocument), so unsaved in-editor edits are saved first and the undo
    history is cleared. The copper layers must be the board's enabled ones (change those with
    SetBoardEnabledLayers first): this does not remove layers or the content on them."""
    if session.kind != "pcb":
        raise SessionError("a board stackup belongs to a board session")
    current = api.call(session, "GetBoardStackup", {})["response"]["stackup"]
    if _copper_layers(stackup) != _copper_layers(current):
        raise SessionError(f"the copper layers must stay {_copper_layers(current)}; "
                           f"change the enabled layers with SetBoardEnabledLayers first")
    api.call(session, "SaveDocument", {})
    path = session.document
    text = path.read_text(encoding="utf-8")
    begin, end = _block_span(text, "stackup")
    indent = re.match(r"[ \t]*", text[begin:]).group(0)
    path.write_text(text[:begin] + stackup_block(stackup, indent) + text[end:], encoding="utf-8", newline="\n")
    api.call(session, "RevertDocument", {})
    time.sleep(settle_s)
    return api.call(session, "GetBoardStackup", {})["response"]


# --- MCP registration ------------------------------------------------------------------------------

def register_fallback_tools(mcp: FastMCP) -> None:
    from kicad_mcp.tools import ipc_tools

    def run(session_id: str, fn, *args, **kwargs) -> dict[str, Any]:
        try:
            return {"status": "ok", **fn(ipc_tools._get(session_id), *args, **kwargs)}
        except Exception as exc:  # noqa: BLE001 - every failure is a structured answer
            return ipc_tools._error(exc)


    @mcp.tool()
    def kicad_library_table_get(session_id: str, type: Kind, scope: Scope = "LTS_BOTH",
                                substituted: bool = False) -> dict[str, Any]:
        """GetLibraryTable (declared by KiCad, not implemented there): the rows of the session's symbol,
        footprint or design-block library table. Project scope is the session's candidate project; global
        scope is the session's private profile (a GUI session), never the owner's real configuration.
        substituted=True expands ${VARS} in the URIs."""
        return run(session_id, get_library_table, type, scope, substituted)

    @mcp.tool()
    def kicad_library_table_add(session_id: str, entry: dict[str, Any], scope: Literal["LTS_GLOBAL", "LTS_PROJECT"]
                                ) -> dict[str, Any]:
        """AddLibraryTableEntry: add a row. entry is a LibraryTableEntry as JSON: nickname, uri, type
        (LT_SYMBOL|LT_FOOTPRINT|LT_DESIGN_BLOCK), optional description, options (map), disabled, hidden.
        Returns LibraryCommandStatus (code LCS_OK, LCS_ALREADY_EXISTS, LCS_READ_ONLY or LCS_ERROR). A running
        editor reads tables at start-up: call kicad_library_table_reload to have it load the change."""
        return run(session_id, add_library_table_entry, entry, scope)

    @mcp.tool()
    def kicad_library_table_update(session_id: str, entry: dict[str, Any], scope: Literal["LTS_GLOBAL", "LTS_PROJECT"]
                                   ) -> dict[str, Any]:
        """UpdateLibraryTableEntry: change the row whose nickname matches entry.nickname (fields the entry
        leaves out are kept). LCS_NOT_FOUND if there is none."""
        return run(session_id, update_library_table_entry, entry, scope)

    @mcp.tool()
    def kicad_library_table_delete(session_id: str, type: Kind, nickname: str,
                                   scope: Literal["LTS_GLOBAL", "LTS_PROJECT"]) -> dict[str, Any]:
        """DeleteLibraryTableEntry: remove the row with this nickname (the library's files stay)."""
        return run(session_id, delete_library_table_entry, type, nickname, scope)

    @mcp.tool()
    def kicad_library_table_reload(session_id: str) -> dict[str, Any]:
        """Have the session's editor read the library tables again (saves its document, restarts the editor on
        the same candidate). Needed after add/update/delete for GetLibraryStatuses, GetLibraryItems and the
        editor's choosers to show the change."""
        try:
            return reload_tables(ipc_tools._get(session_id))
        except Exception as exc:  # noqa: BLE001
            return ipc_tools._error(exc)

    @mcp.tool()
    def kicad_search_libraries(session_id: str, type: Kind, query: str, names_only: bool = False,
                               libraries: list[str] | None = None) -> dict[str, Any]:
        """SearchLibraries (declared by KiCad, not implemented there): symbols, footprints or design blocks
        matching query in the session's enabled libraries (a project row hides a same-named global row),
        filtered the way KiCad's library chooser filters: every space-separated term must match one of the
        item's search texts (library nickname, name, lib:name, keywords, description, and for a symbol its
        footprint and units=N); a term matches as a substring, a * / ? wildcard, or an explicit regex
        (^...$ or /.../). names_only=True matches the item name alone."""
        return run(session_id, search_libraries, type, query, names_only, libraries)

    @mcp.tool()
    def kicad_focus_on_item(session_id: str, focus_item: dict[str, Any], highlight: bool = True,
                            margin_nm: int | None = None) -> dict[str, Any]:
        """FocusOnItem (declared by KiCad, not implemented there): zoom the editor to one footprint, pad,
        symbol or sheet. focus_item is a SelectionSpec as JSON: {"footprint": {"reference": "U1"}},
        {"pad": {"reference": "U1", "number": "3"}} or {"sheet_path": {...}}. highlight=True also makes the
        item the selection (replacing the previous one). GUI session."""
        return run(session_id, focus_on_item, focus_item, highlight, margin_nm)

    @mcp.tool()
    def kicad_refresh_editor(session_id: str, frame: Literal["FT_PCB_EDITOR", "FT_SCHEMATIC_EDITOR"] = "FT_PCB_EDITOR"
                             ) -> dict[str, Any]:
        """RefreshEditor (declared by KiCad, not implemented there): redraw the editor frame (the Redraw
        action), if the session has that frame open. GUI session."""
        return run(session_id, refresh_editor, frame)

    @mcp.tool()
    def kicad_update_board_stackup(session_id: str, stackup: dict[str, Any]) -> dict[str, Any]:
        """UpdateBoardStackup (declared by KiCad, not implemented there): set the board's stackup from a
        BoardStackup JSON (take kicad_api_call GetBoardStackup's stackup, edit thicknesses, materials,
        dielectrics, finish, edge settings, send it back). The copper layers must stay the enabled ones.
        The editor saves, the file's stackup is rewritten and the editor reloads it; the result is the
        editor's readback. Board GUI session."""
        return run(session_id, update_board_stackup, stackup)
