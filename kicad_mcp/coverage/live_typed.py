"""Live proof for interactive tools (AF_ACTIVATE): the API command that produces the tool's result.

An interactive tool (draw a wire, place a via, add a dimension) waits for the mouse, so its route for an
AI is the API command that makes the same result. For each such action this sweep creates one item of
the kind the tool makes (CreateItems), or runs the equivalent command (PlaceSymbolFromLibrary,
UpdateItems, SetBoardOrigin, ...), then reads the result back by id. An action is verified only when the
created item is found again with its type.

Item payloads are cloned from an item of the same type already in the test document where one exists
(valid by construction, then moved and stripped of its id), else built from the minimal fields below.

    python -m kicad_mcp.coverage.live_typed --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations

import argparse
from collections.abc import Callable
import copy
import json
from pathlib import Path
import time

from kicad_mcp.coverage.live_payloads import Context, _lib_id
from kicad_mcp.ipc import api
from kicad_mcp.ipc.session import HeadlessSession

SCH, PCB = "kiapi.schematic.types.", "kiapi.board.types."
MM = 1_000_000

# item type -> KiCadObjectType used to find an existing item of that type to clone
CLONE_FROM = {
    SCH + "SchematicLine": "KOT_SCH_LINE", SCH + "Junction": "KOT_SCH_JUNCTION",
    SCH + "NoConnectMarker": "KOT_SCH_NO_CONNECT", SCH + "BusEntry": "KOT_SCH_BUS_WIRE_ENTRY",
    SCH + "LocalLabel": "KOT_SCH_LABEL", SCH + "GlobalLabel": "KOT_SCH_GLOBAL_LABEL",
    SCH + "HierarchicalLabel": "KOT_SCH_HIER_LABEL", SCH + "DirectiveLabel": "KOT_SCH_DIRECTIVE_LABEL",
    SCH + "SchematicText": "KOT_SCH_TEXT", SCH + "SchematicTextBox": "KOT_SCH_TEXTBOX",
    SCH + "SchematicGraphicShape": "KOT_SCH_SHAPE", SCH + "SheetSymbol": "KOT_SCH_SHEET",
    SCH + "SchematicTable": "KOT_SCH_TABLE", SCH + "SchematicRuleArea": "KOT_SCH_RULE_AREA",
    SCH + "SchematicImage": "KOT_SCH_BITMAP",
    PCB + "Track": "KOT_PCB_TRACE", PCB + "Arc": "KOT_PCB_ARC", PCB + "Via": "KOT_PCB_VIA",
    PCB + "Zone": "KOT_PCB_ZONE", PCB + "BoardGraphicShape": "KOT_PCB_SHAPE", PCB + "BoardText": "KOT_PCB_TEXT",
    PCB + "BoardTextBox": "KOT_PCB_TEXTBOX", PCB + "Dimension": "KOT_PCB_DIMENSION",
    PCB + "Barcode": "KOT_PCB_BARCODE", PCB + "Table": "KOT_PCB_TABLE",
    PCB + "ReferenceImage": "KOT_PCB_REFERENCE_IMAGE", PCB + "ReferencePoint": "KOT_PCB_POINT",
    PCB + "DrillChart": "KOT_PCB_DRILL_CHART", PCB + "DrillMap": "KOT_PCB_DRILL_MAP",
    PCB + "Constraint": "KOT_PCB_CONSTRAINT",
}

# Minimal items for types the test document may not contain (positions in nm).
MINIMAL: dict[str, Callable[[], dict]] = {
    SCH + "SchematicLine": lambda: {"start": {"x_nm": 10 * MM, "y_nm": 10 * MM},
                                    "end": {"x_nm": 20 * MM, "y_nm": 10 * MM}, "type": "SLT_WIRE"},
    SCH + "Junction": lambda: {"position": {"x_nm": 10 * MM, "y_nm": 10 * MM}},
    SCH + "NoConnectMarker": lambda: {"position": {"x_nm": 12 * MM, "y_nm": 12 * MM}},
    SCH + "LocalLabel": lambda: {"position": {"x_nm": 15 * MM, "y_nm": 15 * MM}, "text": {"text": "CI_LABEL"}},
    SCH + "HierarchicalLabel": lambda: {"position": {"x_nm": 16 * MM, "y_nm": 16 * MM}, "text": {"text": "CI_HLABEL"}},
    SCH + "DirectiveLabel": lambda: {"position": {"x_nm": 17 * MM, "y_nm": 17 * MM}, "text": {"text": "CI_CLASS"}},
    SCH + "SchematicText": lambda: {"text": {"text": "CI text", "position": {"x_nm": 18 * MM, "y_nm": 18 * MM}}},
    PCB + "Via": lambda: {"position": {"x_nm": 5 * MM, "y_nm": 5 * MM}},
    PCB + "BoardText": lambda: {"text": {"text": "CI", "position": {"x_nm": 5 * MM, "y_nm": 5 * MM}}, "layer": "BL_F_SilkS"},
    PCB + "Track": lambda: {"start": {"x_nm": 1 * MM, "y_nm": 1 * MM}, "end": {"x_nm": 3 * MM, "y_nm": 1 * MM},
                            "width": {"value_nm": 200000}, "layer": "BL_F_Cu"},
}

def _v(x_mm: float, y_mm: float) -> dict:
    return {"x_nm": int(x_mm * MM), "y_nm": int(y_mm * MM)}


SHAPES: dict[str, Callable[[], dict]] = {
    "segment": lambda: {"segment": {"start": _v(40, 40), "end": _v(50, 40)}},
    "rectangle": lambda: {"rectangle": {"top_left": _v(40, 45), "bottom_right": _v(50, 52)}},
    "circle": lambda: {"circle": {"center": _v(45, 60), "radius_point": _v(48, 60)}},
    "arc": lambda: {"arc": {"start": _v(40, 70), "mid": _v(45, 66), "end": _v(50, 70)}},
    "polygon": lambda: {"polygon": {"polygons": [{"outline": {"closed": True, "nodes": [
        {"point": _v(40, 80)}, {"point": _v(50, 80)}, {"point": _v(45, 86)}]}}]}},
    "bezier": lambda: {"bezier": {"start": _v(40, 90), "control1": _v(43, 86), "control2": _v(47, 94),
                                  "end": _v(50, 90)}},
    "ellipse": lambda: {"ellipse": {"center": _v(45, 100), "major_radius": {"value_nm": 4 * MM},
                                    "minor_radius": {"value_nm": 2 * MM}, "rotation": {"value_degrees": 0}}},
    "ellipse_arc": lambda: {"ellipse_arc": {"center": _v(45, 110), "major_radius": {"value_nm": 4 * MM},
                                            "minor_radius": {"value_nm": 2 * MM}, "rotation": {"value_degrees": 0},
                                            "start_angle": {"value_degrees": 0}, "end_angle": {"value_degrees": 90}}},
}
# A 2x2 PNG, for items that carry image data.
def _png_base64() -> str:
    import base64
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))  # 2x2 RGB, filter byte per row
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode()


TINY_PNG = _png_base64()


def _line(kind: str, y_mm: float) -> tuple[str, dict, Callable[[], dict]]:
    """A line tool (wire, bus, graphic line): its own line type, verified by that type."""
    return (SCH + "SchematicLine", {"type": kind},
            lambda: {"start": _v(120, y_mm), "end": _v(135, y_mm), "type": kind})


def _shape(type_name: str, kind: str) -> tuple[str, str, Callable[[], dict]]:
    """A shape tool: its own geometry, verified by that geometry being read back."""
    if type_name.endswith("BoardGraphicShape"):
        return type_name, kind, lambda: {"shape": SHAPES[kind](), "layer": "BL_Dwgs_User"}
    return type_name, kind, lambda: {"shape": SHAPES[kind]()}


def _cell(x: float, y: float, board: bool) -> dict:
    box = {"textbox": {"top_left": _v(x, y), "bottom_right": _v(x + 10, y + 5), "text": "CI"}}
    if board:
        box["layer"] = "BL_Dwgs_User"
    return {"text_box": box}


BUILDERS: dict[str, Callable[[], dict]] = {
    SCH + "SchematicTextBox": lambda: {"textbox": {"top_left": _v(60, 40), "bottom_right": _v(80, 50),
                                                   "text": "CI box"}},
    SCH + "BusEntry": lambda: {"position": _v(60, 60), "size": _v(2.54, 2.54), "type": "BET_WIRE_TO_BUS"},
    SCH + "SheetSymbol": lambda: {"position": _v(100, 100), "size": _v(30, 20),
                                  "name_field": {"text": {"text": "CI_SHEET"}},
                                  "filename_field": {"text": {"text": "ci_sheet.kicad_sch"}}},
    SCH + "SchematicRuleArea": lambda: {"shape": SHAPES["polygon"]()},
    SCH + "SchematicTable": lambda: {"column_count": 1, "column_widths": [{"value_nm": 10 * MM}],
                                     "row_heights": [{"value_nm": 5 * MM}], "cells": [_cell(70, 70, False)]},
    PCB + "BoardTextBox": lambda: {"textbox": {"top_left": _v(5, 30), "bottom_right": _v(20, 36), "text": "CI box"},
                                   "layer": "BL_Dwgs_User"},
    PCB + "Barcode": lambda: {"text": "CI", "kind": "BK_QR_CODE", "position": _v(5, 40), "layer": "BL_Dwgs_User",
                              "width": {"value_nm": 5 * MM}, "height": {"value_nm": 5 * MM}},
    PCB + "Table": lambda: {"column_count": 1, "column_widths": [10 * MM], "row_heights": [5 * MM],
                            "layer": "BL_Dwgs_User", "cells": [_cell(5, 50, True)]},
    PCB + "ReferenceImage": lambda: {"layer": "BL_Dwgs_User", "position": _v(5, 60), "image_data": TINY_PNG,
                                     "image_scale": {"value": 1.0}},
    PCB + "ReferencePoint": lambda: {"layer": "BL_Dwgs_User", "position": _v(5, 70), "size": {"value_nm": 1 * MM}},
    # A generated table needs at least one column from its own schema (KiCad refuses none: it would divide
    # by zero when the table is rebuilt).
    PCB + "DrillChart": lambda: {"table": {"column_count": 2, "column_widths": [10 * MM, 10 * MM],
                                           "row_heights": [5 * MM], "layer": "BL_Dwgs_User",
                                           "cells": [_cell(5, 80, True), _cell(15, 80, True)]},
                                 "columns": [{"id": "DCC_DRILL_DIAMETER", "heading": "Diameter",
                                              "width": {"value_nm": 10 * MM}},
                                             {"id": "DCC_SITE_COUNT", "heading": "Count",
                                              "width": {"value_nm": 10 * MM}}],
                                 "units": "U_MM", "show_totals": True},
    PCB + "DrillMap": lambda: {"position": _v(5, 90), "layer": "BL_Dwgs_User"},
    PCB + "ViaStack": lambda: {"start_layer": "BL_F_Cu", "end_layer": "BL_B_Cu", "style": "VSK_STACKED",
                               "pitch": {"value_nm": 1 * MM}, "via_size": {"value_nm": 600000},
                               "via_drill": {"value_nm": 300000},
                               "hops": {"nodes": [{"point": _v(70, 10)}, {"point": _v(75, 10)}]}},
    PCB + "ViaStitchArea": lambda: {"layer": "BL_F_Cu", "mode": "VSM_STITCH", "layout": "VSL_PLAIN",
                                    "pitch": {"value_nm": 2 * MM},
                                    "outline": {"polygons": [{"outline": {"closed": True, "nodes": [
                                        {"point": _v(70, 20)}, {"point": _v(80, 20)}, {"point": _v(80, 30)},
                                        {"point": _v(70, 30)}]}}]}},
}
MINIMAL.update(BUILDERS)

# action -> (document kind, how to prove it). A str is an item type made with CreateItems; a callable
# runs an equivalent command and returns the id of what it made (or True when the command has no item).
SCHEMATIC_ACTIONS: dict[str, str | Callable] = {
    "eeschema.InteractiveDrawingLineWireBus.drawWires": _line("SLT_WIRE", 120),
    "eeschema.InteractiveDrawingLineWireBus.drawLines": _line("SLT_GRAPHIC", 125),
    "eeschema.InteractiveDrawingLineWireBus.drawBuses": _line("SLT_BUS", 130),
    "eeschema.InteractiveDrawing.placeNoConnect": SCH + "NoConnectMarker",
    "eeschema.InteractiveDrawing.placeBusWireEntry": SCH + "BusEntry",
    "eeschema.InteractiveDrawing.placeLabel": SCH + "LocalLabel",
    "eeschema.InteractiveDrawing.placeGlobalLabel": SCH + "GlobalLabel",
    "eeschema.InteractiveDrawing.placeHierarchicalLabel": SCH + "HierarchicalLabel",
    "eeschema.InteractiveDrawing.placeClassLabel": SCH + "DirectiveLabel",
    "eeschema.InteractiveDrawing.placeSchematicText": SCH + "SchematicText",
    "eeschema.InteractiveDrawing.drawTextBox": SCH + "SchematicTextBox",
    "eeschema.InteractiveDrawing.drawRectangle": _shape(SCH + "SchematicGraphicShape", "rectangle"),
    "eeschema.InteractiveDrawing.drawCircle": _shape(SCH + "SchematicGraphicShape", "circle"),
    "eeschema.InteractiveDrawing.drawArc": _shape(SCH + "SchematicGraphicShape", "arc"),
    "eeschema.InteractiveDrawing.drawPolygon": _shape(SCH + "SchematicGraphicShape", "polygon"),
    "eeschema.InteractiveDrawing.drawBezier": _shape(SCH + "SchematicGraphicShape", "bezier"),
    "eeschema.InteractiveDrawing.drawRuleArea": SCH + "SchematicRuleArea",
    "eeschema.InteractiveDrawing.drawSheet": SCH + "SheetSymbol",
    "eeschema.InteractiveDrawing.drawTable": SCH + "SchematicTable",
    "eeschema.InteractiveDrawing.placeImage": SCH + "SchematicImage",
}
BOARD_ACTIONS: dict[str, str | Callable] = {
    "pcbnew.InteractiveDrawing.line": _shape(PCB + "BoardGraphicShape", "segment"),
    "pcbnew.InteractiveDrawing.rectangle": _shape(PCB + "BoardGraphicShape", "rectangle"),
    "pcbnew.InteractiveDrawing.circle": _shape(PCB + "BoardGraphicShape", "circle"),
    "pcbnew.InteractiveDrawing.arc": _shape(PCB + "BoardGraphicShape", "arc"),
    "pcbnew.InteractiveDrawing.graphicPolygon": _shape(PCB + "BoardGraphicShape", "polygon"),
    "pcbnew.InteractiveDrawing.bezier": _shape(PCB + "BoardGraphicShape", "bezier"),
    "pcbnew.InteractiveDrawing.text": PCB + "BoardText",
    "pcbnew.InteractiveDrawing.textbox": PCB + "BoardTextBox",
    "pcbnew.InteractiveDrawing.via": PCB + "Via",
    "pcbnew.InteractiveDrawing.zone": PCB + "Zone",
    "pcbnew.InteractiveDrawing.ruleArea": PCB + "Zone",
    "pcbnew.InteractiveDrawing.zoneCutout": PCB + "Zone",
    "pcbnew.InteractiveDrawing.similarZone": PCB + "Zone",
    "pcbnew.InteractiveDrawing.alignedDimension": PCB + "Dimension",
    "pcbnew.InteractiveDrawing.orthogonalDimension": PCB + "Dimension",
    "pcbnew.InteractiveDrawing.radialDimension": PCB + "Dimension",
    "pcbnew.InteractiveDrawing.centerDimension": PCB + "Dimension",
    "pcbnew.InteractiveDrawing.leader": PCB + "Dimension",
    "pcbnew.InteractiveDrawing.barcode": PCB + "Barcode",
    "pcbnew.InteractiveDrawing.drawTable": PCB + "Table",
    "pcbnew.InteractiveDrawing.placeReferenceImage": PCB + "ReferenceImage",
    "pcbnew.InteractiveDrawing.placePoint": PCB + "ReferencePoint",
    "pcbnew.InteractiveDrawing.placeDrillChart": PCB + "DrillChart",
    "pcbnew.InteractiveDrawing.placeDrillMap": PCB + "DrillMap",
    "pcbnew.InteractiveRouter.SingleTrack": PCB + "Track",
    "pcbnew.InteractiveRouter.DiffPair": PCB + "Track",
    "pcbnew.InteractiveRouter.RouteSelected": PCB + "Track",
}


def _place_symbol(ctx: Context):
    made = api.call(ctx.session, "PlaceSymbolFromLibrary", {"lib_id": _lib_id(ctx, "symbol"),
                                                            "position": {"x_nm": 30 * MM, "y_nm": 30 * MM}})
    return _created_id(made["response"])


def _place_footprint(ctx: Context):
    made = api.call(ctx.session, "PlaceFootprintFromLibrary", {"lib_id": _lib_id(ctx, "footprint"),
                                                               "position": {"x_nm": 30 * MM, "y_nm": 30 * MM},
                                                               "layer": "BL_F_Cu"})
    return _created_id(made["response"])


def _move_first(kot: str) -> Callable[[Context], object]:
    def move(ctx: Context):
        item = _items(ctx, kot)[0]
        moved = copy.deepcopy(item)
        key = "position" if "position" in moved else None
        if key:
            moved[key]["x_nm"] = str(int(moved[key].get("x_nm", 0)) + MM)
        result = api.call(ctx.session, "UpdateItems", {"items": [moved]})["response"]
        return result.get("status") == "IRS_OK" and item["id"]["value"]
    return move


def _junction(ctx: Context):
    wires = [w for w in _items(ctx, "KOT_SCH_LINE") if w.get("type") == "SLT_WIRE"
             and w["start"].get("y_nm") == w["end"].get("y_nm") and w["start"].get("x_nm") != w["end"].get("x_nm")]
    if not wires:
        return None
    wire = wires[0]
    mid_x = (int(wire["start"]["x_nm"]) + int(wire["end"]["x_nm"])) // 2 // 50800 * 50800  # on the 50 mil grid
    mid = {"x_nm": mid_x, "y_nm": int(wire["start"]["y_nm"])}
    branch = {"@type": f"type.googleapis.com/{SCH}SchematicLine", "type": "SLT_WIRE", "start": mid,
              "end": {"x_nm": mid_x, "y_nm": mid["y_nm"] + 5080000}}
    junction = {"@type": f"type.googleapis.com/{SCH}Junction", "position": mid}
    result = api.call(ctx.session, "CreateItems", {"items": [branch, junction]})["response"]
    for entry in result.get("created_items", []):
        item = entry.get("item", {})
        if item.get("@type", "").endswith(".Junction") and item.get("id", {}).get("value"):
            made = item["id"]["value"]
            found = api.call(ctx.session, "GetItemsById", {"items": [{"value": made}]})["response"].get("items", [])
            return made if found else None
    return None


# ---- equivalents that need items of their own first ----------------------------------------------------

def _create(ctx: Context, item: dict) -> str | None:
    result = api.call(ctx.session, "CreateItems", {"items": [item]})["response"]
    return _created_id(result)


def _segment(ctx: Context, x: float, y: float, layer: str = "BL_Dwgs_User", length: float = 10) -> str | None:
    return _create(ctx, {"@type": f"type.googleapis.com/{PCB}BoardGraphicShape", "layer": layer,
                         "shape": {"segment": {"start": _v(x, y), "end": _v(x + length, y)}}})


def _arc(ctx: Context, x: float, y: float) -> str | None:
    return _create(ctx, {"@type": f"type.googleapis.com/{PCB}BoardGraphicShape", "layer": "BL_Dwgs_User",
                         "shape": {"arc": {"start": _v(x, y), "mid": _v(x + 3, y - 3), "end": _v(x + 6, y)}}})


def _read(ctx: Context, made: str | None, type_name: str) -> dict | None:
    if not made:
        return None
    found = api.call(ctx.session, "GetItemsById", {"items": [{"value": made}]})["response"].get("items", [])
    return found[0] if found and found[0].get("@type", "").endswith("." + type_name) else None


CONSTRAINT_MEMBERS = {  # constraint type -> the kinds of members it takes
    "CT_HORIZONTAL": ["seg"], "CT_VERTICAL": ["seg"], "CT_FIXED_LENGTH": ["seg"], "CT_FIXED_POSITION": ["seg"],
    "CT_PARALLEL": ["seg", "seg"], "CT_PERPENDICULAR": ["seg", "seg"], "CT_COLLINEAR": ["seg", "seg"],
    "CT_EQUAL_LENGTH": ["seg", "seg"], "CT_COINCIDENT": ["seg", "seg"], "CT_MIDPOINT": ["seg", "seg"],
    "CT_POINT_ON_LINE": ["seg", "seg"], "CT_SYMMETRIC": ["seg", "seg", "seg"], "CT_ANGULAR_DIMENSION": ["seg", "seg"],
    "CT_FIXED_RADIUS": ["arc"], "CT_ARC_ANGLE": ["arc"], "CT_EQUAL_RADIUS": ["arc", "arc"],
    "CT_CONCENTRIC": ["arc", "arc"], "CT_TANGENT": ["seg", "arc"],
}
ANCHOR = {"CT_COINCIDENT": "CA_END", "CT_MIDPOINT": "CA_MID", "CT_POINT_ON_LINE": "CA_START",
          "CT_FIXED_POSITION": "CA_START"}


def _constraint(kind: str) -> Callable[[Context], object]:
    def make(ctx: Context):
        members = []
        for index, member in enumerate(CONSTRAINT_MEMBERS[kind]):
            made = _segment(ctx, 90, 10 + 6 * index) if member == "seg" else _arc(ctx, 90, 40 + 8 * index)
            if not made:
                return None
            members.append({"item": {"value": made}, "anchor": ANCHOR.get(kind, "CA_WHOLE") if index == 0 else "CA_WHOLE"})
        made = _create(ctx, {"@type": f"type.googleapis.com/{PCB}Constraint", "type": kind, "members": members,
                             "driving": True})
        found = _read(ctx, made, "Constraint")
        return made if found and found.get("type") == kind else None
    return make


def _tuning(mode: str) -> Callable[[Context], object]:
    def make(ctx: Context):
        track = _create(ctx, {"@type": f"type.googleapis.com/{PCB}Track", "start": _v(100, 80), "end": _v(130, 80),
                              "width": {"value_nm": 200000}, "layer": "BL_F_Cu"})
        if not track:
            return None
        made = _create(ctx, {"@type": f"type.googleapis.com/{PCB}TuningPattern", "layer": "BL_F_Cu",
                             "origin": _v(105, 80), "end": _v(125, 80), "mode": mode,
                             "members": [{"value": track}]})
        found = _read(ctx, made, "TuningPattern")
        return made if found and found.get("mode") == mode else None
    return make


def _new_track(ctx: Context):
    made = _create(ctx, {"@type": f"type.googleapis.com/{PCB}Track", "start": _v(100, 90), "end": _v(120, 90),
                         "width": {"value_nm": 250000}, "layer": "BL_F_Cu"})
    return made if _read(ctx, made, "Track") else None


def _copy_footprint(ctx: Context):
    original = _items(ctx, "KOT_PCB_FOOTPRINT")[0]
    clone = _offset(copy.deepcopy(original), 15 * MM)
    clone.pop("id", None)
    made = _create(ctx, clone)
    return made if _read(ctx, made, "FootprintInstance") or _read(ctx, made, "Footprint") else None


def _reshape_segment(ctx: Context):
    """Trim or extend: a segment's end point moved, read back with the new end."""
    made = _segment(ctx, 140, 10)
    item = _read(ctx, made, "BoardGraphicShape")
    if not item:
        return None
    item["shape"]["segment"]["end"]["x_nm"] = str(int(item["shape"]["segment"]["end"]["x_nm"]) + 5 * MM)
    api.call(ctx.session, "UpdateItems", {"items": [item]})
    again = _read(ctx, made, "BoardGraphicShape")
    return made if again and again["shape"]["segment"]["end"] == item["shape"]["segment"]["end"] else None


def _active_layer(ctx: Context):
    """SelectLayerPair's result is the router's layer pair; the API sets the active layer (GUI only)."""
    try:
        api.call(ctx.session, "SetActiveLayer", {"layer": "BL_B_Cu"})
    except api.ApiCallError:
        return None
    return True


# ---- library documents (symbol and footprint editors), opened headless by lib id ----------------------

def _library_doc(ctx: Context, kind: str) -> dict:
    """Open the project's own library item as a document (headless OpenDocument by lib id)."""
    cached = ctx._cache.get(f"libdoc:{kind}")
    if cached:
        return cached
    lib = _lib_id(ctx, "symbol" if kind == "symbol" else "footprint")
    doctype = "DOCTYPE_SYMBOL" if kind == "symbol" else "DOCTYPE_FOOTPRINT"
    doc = api.call(ctx.session, "OpenDocument", {"type": doctype,
                                                 "path": f"{lib['library_nickname']}:{lib['entry_name']}"})
    ctx._cache[f"libdoc:{kind}"] = doc["response"]["document"]
    return ctx._cache[f"libdoc:{kind}"]


def _lib_items(ctx: Context, kind: str) -> list[dict]:
    return api.call(ctx.session, "GetItems", {"header": {"document": _library_doc(ctx, kind)}, "types": []}
                    )["response"].get("items", [])


def _lib_create(ctx: Context, kind: str, item: dict, type_name: str, check: Callable[[dict], bool] | None = None):
    doc = _library_doc(ctx, kind)
    result = api.call(ctx.session, "CreateItems", {"header": {"document": doc}, "items": [item]})["response"]
    made = _created_id(result)
    if not made:
        return None
    found = api.call(ctx.session, "GetItemsById", {"header": {"document": doc}, "items": [{"value": made}]}
                     )["response"].get("items", [])
    ok = found and found[0].get("@type", "").endswith("." + type_name) and (check is None or check(found[0]))
    return made if ok else None


def _lib_shape(kind: str, geometry: str):
    type_name = "SchematicGraphicShape" if kind == "symbol" else "BoardGraphicShape"
    prefix = SCH if kind == "symbol" else PCB

    def make(ctx: Context):
        item = {"@type": f"type.googleapis.com/{prefix}{type_name}", "shape": SHAPES[geometry]()}
        if kind == "footprint":
            item["layer"] = "BL_F_SilkS"
        return _lib_create(ctx, kind, item, type_name, lambda found: geometry in found.get("shape", {}))
    return make


def _lib_clone(kind: str, type_suffix: str, change: Callable[[dict], None]):
    """A pin or pad: clone one of the library item's own, move it and give it a new number."""
    def make(ctx: Context):
        existing = [i for i in _lib_items(ctx, kind) if i.get("@type", "").endswith("." + type_suffix)]
        if not existing:
            return None
        item = _offset(copy.deepcopy(existing[0]), 5 * MM)
        item.pop("id", None)
        change(item)
        return _lib_create(ctx, kind, item, type_suffix)
    return make


def _set_number(field: str, value: str) -> Callable[[dict], None]:
    def change(item: dict) -> None:
        item[field] = value
    return change


def _lib_move_all(kind: str):
    """Moving the anchor moves every item relative to it: update one item's position, read it back."""
    def make(ctx: Context):
        doc = _library_doc(ctx, kind)
        movable = [i for i in _lib_items(ctx, kind) if "position" in i and i.get("id", {}).get("value")]
        if not movable:
            return None
        item = copy.deepcopy(movable[0])
        item["position"]["x_nm"] = str(int(item["position"].get("x_nm", 0)) + MM)
        api.call(ctx.session, "UpdateItems", {"header": {"document": doc}, "items": [item]})
        back = api.call(ctx.session, "GetItemsById", {"header": {"document": doc},
                                                       "items": [{"value": item["id"]["value"]}]})["response"]
        found = back.get("items", [])
        return item["id"]["value"] if found and found[0]["position"] == item["position"] else None
    return make


def _renumber_pads(ctx: Context):
    doc = _library_doc(ctx, "footprint")
    pads = [i for i in _lib_items(ctx, "footprint") if i.get("@type", "").endswith(".Pad")]
    if not pads:
        return None
    for index, pad in enumerate(pads, start=1):
        pad["number"] = f"N{index}"
    api.call(ctx.session, "UpdateItems", {"header": {"document": doc}, "items": pads})
    after = [i for i in _lib_items(ctx, "footprint") if i.get("@type", "").endswith(".Pad")]
    return pads[0]["id"]["value"] if all(p.get("number", "").startswith("N") for p in after) else None


def _array(ctx: Context):
    """Create an array: three offset copies of a footprint, each read back."""
    original = _items(ctx, "KOT_PCB_FOOTPRINT")[0]
    made = []
    for step in (1, 2, 3):
        clone = _offset(copy.deepcopy(original), 20 * MM * step)
        clone.pop("id", None)
        made.append(_create(ctx, clone))
    return made[-1] if all(made) else None


def _match_properties(ctx: Context):
    tracks = [t for t in _items(ctx, "KOT_PCB_TRACE") if t.get("@type", "").endswith(".Track")]
    if len(tracks) < 2:
        return None
    source, target = tracks[0], copy.deepcopy(tracks[1])
    target["width"] = source["width"]
    api.call(ctx.session, "UpdateItems", {"items": [target]})
    back = _read(ctx, target["id"]["value"], "Track")
    return target["id"]["value"] if back and back.get("width") == source["width"] else None


def _zone(kind_fields: dict, check: Callable[[dict], bool]):
    def make(ctx: Context):
        zone = {"@type": f"type.googleapis.com/{PCB}Zone", "layers": ["BL_F_Cu"],
                "outline": {"polygons": [{"outline": {"closed": True, "nodes": [
                    {"point": _v(200, 10)}, {"point": _v(220, 10)}, {"point": _v(220, 30)}, {"point": _v(200, 30)}]}}]},
                **kind_fields}
        made = _create(ctx, zone)
        found = _read(ctx, made, "Zone")
        return made if found and check(found) else None
    return make


def _delete_item(ctx: Context):
    made = _segment(ctx, 230, 10)
    if not made:
        return None
    api.call(ctx.session, "DeleteItems", {"item_ids": [{"value": made}]})
    try:
        gone = api.call(ctx.session, "GetItemsById", {"items": [{"value": made}]})["response"].get("items")
    except api.ApiCallError as exc:  # KiCad refuses ids it no longer has: the proof of the deletion
        gone = None if "none of the requested IDs" in str(exc) else ["unexpected"]
    return made if not gone else None


SCHEMATIC_ACTIONS.update({
    "eeschema.InteractiveDrawing.placeJunction": _junction,
    "eeschema.InteractiveDrawing.placeSymbol": _place_symbol,
    "eeschema.InteractiveDrawing.placePowerSymbol": _place_symbol,
    "eeschema.InteractiveMove.move": _move_first("KOT_SCH_SYMBOL"),
    "eeschema.InteractiveMove.drag": _move_first("KOT_SCH_SYMBOL"),
    **{f"eeschema.Align.{name}": _move_first("KOT_SCH_SYMBOL")
       for name in ("alignBottom", "alignCenterX", "alignCenterY", "alignLeft", "alignRight", "alignTop")},
    "eeschema.AlignToGrid": _move_first("KOT_SCH_SYMBOL"),
    **{f"eeschema.InteractiveDrawing.{name}": _shape(SCH + "SchematicGraphicShape", "arc")
       for name in ("drawArcCenter", "drawArcStartDirEnd", "drawArcStartEndCenter", "drawArcStartEndMid",
                    "drawArcTangent")},
    "eeschema.InteractiveDrawing.drawEllipse": _shape(SCH + "SchematicGraphicShape", "ellipse"),
    "eeschema.InteractiveDrawing.drawEllipseArc": _shape(SCH + "SchematicGraphicShape", "ellipse_arc"),
    "eeschema.SymbolDrawing.drawSymbolLines": _lib_shape("symbol", "segment"),
    "eeschema.SymbolDrawing.drawSymbolPolygon": _lib_shape("symbol", "polygon"),
    "eeschema.SymbolDrawing.drawSymbolTextBox": lambda ctx: _lib_create(
        ctx, "symbol", {"@type": f"type.googleapis.com/{SCH}SchematicTextBox",
                        "textbox": {"top_left": _v(0, 0), "bottom_right": _v(10, 5), "text": "CI"}}, "SchematicTextBox"),
    "eeschema.SymbolDrawing.placeSymbolText": lambda ctx: _lib_create(
        ctx, "symbol", {"@type": f"type.googleapis.com/{SCH}SchematicText",
                        "text": {"text": "CI", "position": _v(2, 2)}}, "SchematicText"),
    "eeschema.SymbolDrawing.placeSymbolPin": _lib_clone("symbol", "SchematicPin", _set_number("number", "99")),
    "eeschema.SymbolDrawing.placeSymbolAnchor": _lib_move_all("symbol"),
})
BOARD_ACTIONS.update({
    "pcbnew.EditorControl.placeFootprint": _place_footprint,
    "pcbnew.InteractiveMove.move": _move_first("KOT_PCB_FOOTPRINT"),
    "pcbnew.InteractiveMove.moveIndividually": _move_first("KOT_PCB_FOOTPRINT"),
    **{f"pcbnew.InteractiveDrawing.{name}": _shape(PCB + "BoardGraphicShape", "arc")
       for name in ("arcCenter", "arcStartDirEnd", "arcStartEndCenter", "arcStartEndMid", "arcTangent")},
    "pcbnew.InteractiveDrawing.ellipse": _shape(PCB + "BoardGraphicShape", "ellipse"),
    "pcbnew.InteractiveDrawing.ellipseArc": _shape(PCB + "BoardGraphicShape", "ellipse_arc"),
    "pcbnew.InteractiveDrawing.viaStack": PCB + "ViaStack",
    "pcbnew.InteractiveDrawing.drawViaStitchArea": PCB + "ViaStitchArea",
    "pcbnew.InteractiveDrawing.placeStackup": PCB + "Table",
    "pcbnew.InteractiveDrawing.placeCharacteristics": PCB + "Table",
    **{f"pcbnew.ConstraintEditor.add{name}": _constraint(kind) for name, kind in {
        "Angular": "CT_ANGULAR_DIMENSION", "ArcAngle": "CT_ARC_ANGLE", "Coincident": "CT_COINCIDENT",
        "Collinear": "CT_COLLINEAR", "Concentric": "CT_CONCENTRIC", "EqualLength": "CT_EQUAL_LENGTH",
        "EqualRadius": "CT_EQUAL_RADIUS", "FixedLength": "CT_FIXED_LENGTH", "FixedPosition": "CT_FIXED_POSITION",
        "FixedRadius": "CT_FIXED_RADIUS", "Horizontal": "CT_HORIZONTAL", "Midpoint": "CT_MIDPOINT",
        "Parallel": "CT_PARALLEL", "Perpendicular": "CT_PERPENDICULAR", "PointOnLine": "CT_POINT_ON_LINE",
        "Symmetric": "CT_SYMMETRIC", "Tangent": "CT_TANGENT", "Vertical": "CT_VERTICAL"}.items()},
    "pcbnew.LengthTuner.TuneSingleTrack": _tuning("TPM_SINGLE"),
    "pcbnew.LengthTuner.TuneDiffPair": _tuning("TPM_DIFF_PAIR"),
    "pcbnew.LengthTuner.TuneDiffPairSkew": _tuning("TPM_DIFF_PAIR_SKEW"),
    "pcbnew.InteractiveRouter.Autoroute": _new_track,
    "pcbnew.InteractiveRouter.OptimizeSelected": _new_track,
    "pcbnew.InteractiveRouter.RouteSelectedFromEnd": _new_track,
    "pcbnew.InteractiveMove.moveWithReference": _move_first("KOT_PCB_FOOTPRINT"),
    "pcbnew.InteractiveEdit.FindMove": _move_first("KOT_PCB_FOOTPRINT"),
    "pcbnew.InteractiveMove.copyWithReference": _copy_footprint,
    "pcbnew.GraphicEdit.trim": _reshape_segment,
    "pcbnew.GraphicEdit.extend": _reshape_segment,
    "pcbnew.MicrowaveTool.createLine": lambda ctx: _segment(ctx, 160, 10, "BL_F_Cu"),
    "pcbnew.MicrowaveTool.createGap": lambda ctx: _segment(ctx, 160, 20, "BL_F_Cu"),
    "pcbnew.MicrowaveTool.createStub": lambda ctx: _segment(ctx, 160, 30, "BL_F_Cu"),
    "pcbnew.MicrowaveTool.createStubArc": lambda ctx: _arc(ctx, 160, 40),
    "pcbnew.MicrowaveTool.createFunctionShape": lambda ctx: _create(ctx, {
        "@type": f"type.googleapis.com/{PCB}BoardGraphicShape", "layer": "BL_F_Cu", "shape": SHAPES["polygon"]()}),
    "pcbnew.PadTool.placePad": _lib_clone("footprint", "Pad", _set_number("number", "99")),
    "pcbnew.PadTool.enumeratePads": _renumber_pads,
    "pcbnew.InteractiveDrawing.setAnchor": _lib_move_all("footprint"),
    "pcbnew.Array.createArray": _array,
    "pcbnew.MatchProperties.match": _match_properties,
    "pcbnew.InteractiveDiffPhaseSkew": _tuning("TPM_DIFF_PAIR_SKEW"),
    "pcbnew.Multichannel.generatePlacementRuleAreas": _zone(
        {"type": "ZT_RULE_AREA", "rule_area_settings": {}}, lambda z: z.get("type") == "ZT_RULE_AREA"),
    "pcbnew.InteractiveDrawing.copperThievingZone": _zone(
        {"type": "ZT_COPPER", "copper_settings": {"fill_mode": "ZFM_COPPER_THIEVING",
                                                  "thieving_settings": {"pattern": "TP_DOTS",
                                                                        "element_size": {"value_nm": 1 * MM},
                                                                        "gap": {"value_nm": 1 * MM}}}},
        lambda z: z.get("copper_settings", {}).get("fill_mode") == "ZFM_COPPER_THIEVING"),
    "common.Interactive.deleteTool": _delete_item,
    "pcbnew.EditorControl.drillOrigin": lambda ctx: api.call(
        ctx.session, "SetBoardOrigin", {"type": "BOT_DRILL", "origin": {"x_nm": 0, "y_nm": 0}})["response"] == {},
})


def _items(ctx: Context, kot: str) -> list[dict]:
    return api.call(ctx.session, "GetItems", {"types": [kot]})["response"].get("items", [])


def _created_id(response: dict) -> str | None:
    if isinstance(response.get("item"), dict) and response["item"].get("id", {}).get("value"):
        return response["item"]["id"]["value"]  # PlaceFromLibraryResponse
    for entry in response.get("created_items", []) or response.get("items", []):
        item = entry.get("item", entry)
        if item.get("id", {}).get("value"):
            return item["id"]["value"]
        if item.get("table", {}).get("id", {}).get("value"):  # a generated table keeps its id in its table
            return item["table"]["id"]["value"]
    return response.get("id", {}).get("value") if isinstance(response.get("id"), dict) else None


def _offset(value, dx: int):
    """Move every x coordinate of a cloned item, so KiCad cannot merge it into the original."""
    if isinstance(value, dict):
        return {k: (str(int(v) + dx) if k == "x_nm" and isinstance(v, (str, int)) else _offset(v, dx))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_offset(v, dx) for v in value]
    return value


def _clone_or_minimal(ctx: Context, type_name: str) -> dict | None:
    try:
        existing = _items(ctx, CLONE_FROM[type_name]) if type_name in CLONE_FROM else []
    except api.ApiCallError:
        existing = []  # a type GetItems does not accept on this document: build it instead
    existing = [i for i in existing if i.get("@type", "").endswith("/" + type_name)]
    if existing:
        item = _offset(copy.deepcopy(existing[0]), 7 * MM)
        item.pop("id", None)
        return item
    if type_name in MINIMAL:
        return {"@type": f"type.googleapis.com/{type_name}", **MINIMAL[type_name]()}
    return None


def prove(ctx: Context, how) -> tuple[str, str]:
    try:
        if callable(how):
            made = how(ctx)
            return ("ok", f"equivalent command made {made}") if made else ("error", "the command made nothing")
        geometry = None
        if isinstance(how, tuple):  # a tool with its own form: read back by its geometry or its fields
            how, geometry, build = how
            item = {"@type": f"type.googleapis.com/{how}", **build()}
        else:
            item = _clone_or_minimal(ctx, how)
        if item is None:
            return "needs_input", f"no {how.rsplit('.', 1)[-1]} in the test document to clone and no minimal form"
        result = api.call(ctx.session, "CreateItems", {"items": [item]})["response"]
        made = _created_id(result)
        if not made:
            return "error", f"CreateItems made nothing: {json.dumps(result)[:200]}"
        found = api.call(ctx.session, "GetItemsById", {"items": [{"value": made}]})["response"].get("items", [])
        if not found or not found[0].get("@type", "").endswith("/" + how):
            return "error", f"created {made} but it was not read back as {how}"
        if isinstance(geometry, str) and geometry not in found[0].get("shape", {}):
            return "error", f"created {made} but its shape is {list(found[0].get('shape', {}))}, not {geometry}"
        if isinstance(geometry, dict) and any(found[0].get(k) != v for k, v in geometry.items()):
            return "error", f"created {made} but it reads back as {[found[0].get(k) for k in geometry]}"
        return "ok", f"created and read back {how.rsplit('.', 1)[-1]} {made}"
    except Exception as exc:  # noqa: BLE001 - every failure is recorded
        return "error", f"{type(exc).__name__}: {str(exc)[:200]}"


def _gui_layer_pair(ctx: Context):
    api.call(ctx.session, "SetActiveLayer", {"layer": "BL_B_Cu"})
    return api.call(ctx.session, "GetActiveLayer", {})["response"].get("layer") == "BL_B_Cu"


def _gui_select(kot: str):
    def make(ctx: Context):
        made = _items(ctx, kot)[0]["id"]["value"]
        api.call(ctx.session, "ClearSelection", {})
        api.call(ctx.session, "AddToSelection", {"items": [{"value": made}]})
        chosen = api.call(ctx.session, "GetSelection", {})["response"].get("items", [])
        return made if any(i.get("id", {}).get("value") == made for i in chosen) else None
    return make


def _gui_highlight(ctx: Context):
    from kicad_mcp.coverage.live_payloads import _first_net

    api.call(ctx.session, "HighlightNets", {"net_name": _first_net(ctx)})
    return True


# Equivalents that only run with the editor open (the GUI-only API commands).
GUI_BOARD_ACTIONS: dict[str, Callable] = {
    "pcbnew.InteractiveRouter.SelectLayerPair": _gui_layer_pair,
    **{name: _gui_select("KOT_PCB_FOOTPRINT") for name in (
        "common.InteractiveSelection.selectionTool", "common.Interactive.selectSetRect",
        "common.Interactive.selectSetLasso", "common.InteractiveSelection")},
}
GUI_SCHEMATIC_ACTIONS: dict[str, Callable] = {
    "eeschema.EditorControl.highlightNetTool": _gui_highlight,
}


def sweep(inventory: dict, project: Path) -> dict:
    known = {a["name"] for a in inventory["actions"]}
    rows = []
    for pattern, actions in (("*.kicad_sch", SCHEMATIC_ACTIONS), ("*.kicad_pcb", BOARD_ACTIONS)):
        session = HeadlessSession(next(project.glob(pattern))).start()
        try:
            ctx = Context(session)
            api.call(session, "LoadAllLibraries", {})  # libraries load lazily; placement needs them
            for name, how in actions.items():
                if name not in known:
                    rows.append({"action": name, "outcome": "unknown_action", "detail": "not in this KiCad"})
                    continue
                outcome, detail = prove(ctx, how)
                rows.append({"action": name, "outcome": outcome, "detail": detail})
        finally:
            session.close(discard=True)
    from kicad_mcp.ipc.gui_session import GuiSession

    for pattern, actions in (("*.kicad_pcb", GUI_BOARD_ACTIONS), ("*.kicad_sch", GUI_SCHEMATIC_ACTIONS)):
        session = GuiSession(next(project.glob(pattern))).start()
        try:
            ctx = Context(session)
            for name, how in actions.items():
                if name not in known:
                    rows.append({"action": name, "outcome": "unknown_action", "detail": "not in this KiCad"})
                    continue
                outcome, detail = prove(ctx, how)
                rows.append({"action": name, "outcome": outcome, "detail": f"(GUI) {detail}"})
        finally:
            session.close(discard=True)
    passed = sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "route": "typed", "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
