"""File adapter: load a .kicad_pcb with KiCad's pcbnew into the neutral model, apply a ChangeSet back.

Runs under KiCad's bundled python.exe (located by ``kicad_env``). ``pcbnew`` is imported lazily
through ``get_pcbnew()`` so that importing this module never fails on a plain Python; every function
that needs pcbnew raises ``AdapterError('pcbnew_unavailable')`` there instead.

Headless policy
---------------
Before pcbnew is imported the vendored ``kicad_headless.py`` (next to this module) is imported so its
non-interactive policy takes effect, and after the import ``wx.DisableAsserts()`` is called so a wx
assertion can never raise a modal dialog. ``headless_policy()`` reports what happened.

Units are integer nanometres (KiCad's internal unit). Nothing here guesses a value:

* Rules come from the board's design settings. When the project file (``.kicad_pro``) that stores them
  is absent, or lacks a value, that value is ``None`` and ``LoadInfo.warnings`` carries
  ``rule_missing:<name>``. ``courtyard_clearance_nm`` is the spec default 0 unless a sibling
  ``.kicad_dru`` holds an unconditional ``courtyard_clearance`` constraint.
* ``fallback_margin_nm`` is 0 (the spec defines the fallback box, not a margin).
* Edge shapes that are not straight segments or arcs (rectangles, circles, polygons) contribute to the
  outline polygons but cannot be dragged: they get the warning ``edge_shape_not_draggable:<uuid>:<kind>``.

``load_board(path_or_BOARD)`` returns the ``model.Board``; ``load_board_with_info`` also returns the
live pcbnew BOARD and the warnings. ``apply_changeset(board, changeset)`` mutates a pcbnew BOARD; it
validates every uuid first, so an unknown uuid changes nothing.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import geometry as geo
from .model import (
    Board,
    ChangeSet,
    EdgeSeg,
    Keepout,
    Outline,
    Pad,
    Part,
    Rules,
    Track,
    Via,
    Zone,
)

__all__ = [
    "AdapterError",
    "LoadInfo",
    "apply_changeset",
    "connectivity_stats",
    "fill_zones",
    "get_pcbnew",
    "headless_policy",
    "load_board",
    "load_board_with_info",
    "open_board",
    "save_board",
    "zone_filled_areas",
]

HEADLESS_DIR = os.path.dirname(os.path.abspath(__file__))  # vendored kicad_headless.py

_state: dict[str, Any] = {"pcbnew": None, "policy": None}


class AdapterError(Exception):
    """Typed adapter failure: ``code`` is machine readable."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------------- pcbnew access


def _import_headless_helper() -> dict:
    info: dict[str, Any] = {"helper": "absent", "helper_public": [], "wx_asserts": "not_applied"}
    directory = os.environ.get("ADAPTIVE_KICAD_HEADLESS_DIR", HEADLESS_DIR)
    path = os.path.join(directory, "kicad_headless.py")
    if os.path.isfile(path):
        try:
            spec = importlib.util.spec_from_file_location("kicad_headless", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            info["helper"] = "loaded"
            info["helper_public"] = sorted(
                n for n in dir(module) if not n.startswith("_") and callable(getattr(module, n, None))
            )[:40]
        except (Exception, SystemExit) as exc:  # the helper must never stop the engine from loading
            info["helper"] = "load_failed: %r" % (exc,)
    return info


def headless_policy() -> dict:
    """What the non-interactive policy did (``helper``, ``wx_asserts``). Empty until pcbnew is loaded."""
    return dict(_state["policy"] or {})


def get_pcbnew():
    """Import pcbnew once (headless policy first) and return the module."""
    if _state["pcbnew"] is None:
        policy = _import_headless_helper()
        try:
            import pcbnew as module  # noqa: PLC0415 - lazy by design
        except Exception as exc:
            raise AdapterError("pcbnew_unavailable", repr(exc))
        try:
            import wx  # noqa: PLC0415

            disable = getattr(wx, "DisableAsserts", None)
            if disable is None:
                policy["wx_asserts"] = "unavailable"
            else:
                disable()
                policy["wx_asserts"] = "disabled"
        except Exception as exc:
            policy["wx_asserts"] = "wx_unavailable: %r" % (exc,)
        _state["pcbnew"] = module
        _state["policy"] = policy
    return _state["pcbnew"]


# ----------------------------------------------------------------------------- small helpers


def _xy(p) -> tuple[int, int]:
    return (int(p.x), int(p.y))


def _vec(x: int, y: int):
    return get_pcbnew().VECTOR2I(int(x), int(y))


def _uuid(item) -> str:
    try:
        return item.m_Uuid.AsString()
    except Exception:
        return str(item.m_Uuid)


def _cast(item, class_name: str, probe: str):
    """Downcast a BOARD_ITEM wrapper when SWIG did not (``probe`` is a method of the target class)."""
    if hasattr(item, probe):
        return item
    fn = getattr(get_pcbnew(), "Cast_to_" + class_name, None)
    if fn is not None:
        try:
            return fn(item) or item
        except Exception:
            return item
    return item


def _chain_points(chain) -> tuple:
    pts: list[tuple[int, int]] = []
    for i in range(chain.PointCount()):
        q = _xy(chain.CPoint(i))
        if not pts or pts[-1] != q:
            pts.append(q)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return tuple(pts)


def _polygon_set_outlines(sps) -> list[tuple]:
    out = []
    for i in range(sps.OutlineCount()):
        pts = _chain_points(sps.Outline(i))
        if len(pts) >= 3:
            out.append(pts)
    return out


def _shape_const(name: str, legacy: str):
    pcb = get_pcbnew()
    value = getattr(pcb, name, None)
    return getattr(pcb, legacy, None) if value is None else value


def _flag(zone, *names: str) -> bool:
    for name in names:
        fn = getattr(zone, name, None)
        if fn is not None:
            try:
                return bool(fn())
            except Exception:
                continue
    return False


def _layer_names(board, zone) -> tuple[str, ...]:
    try:
        return tuple(board.GetLayerName(layer) for layer in zone.GetLayerSet().Seq())
    except Exception:
        return ()


# ----------------------------------------------------------------------------- rules


def _opt_int(value) -> int | None:
    if value is None:
        return None
    has = getattr(value, "has_value", None)
    if has is not None:
        try:
            return int(value.value()) if has() else None
        except Exception:
            return None
    try:
        return int(value)
    except Exception:
        return None


def _netclass_clearance(netclass) -> int | None:
    if netclass is None:
        return None
    has = getattr(netclass, "HasClearance", None)
    try:
        if has is not None and not has():
            return None
        return _opt_int(netclass.GetClearance())
    except Exception:
        return None


def _project_rule_presence(path: str) -> dict | None:
    """Which rule values the sibling .kicad_pro actually stores; None when there is no project file."""
    pro = os.path.splitext(path)[0] + ".kicad_pro"
    if not os.path.isfile(pro):
        return None
    try:
        with open(pro, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {"edge": False, "min_clearance": False, "netclass": False}
    rules = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    classes = (data.get("net_settings") or {}).get("classes") or []
    default = next((c for c in classes if isinstance(c, dict) and c.get("name") == "Default"), None)
    return {
        "edge": "min_copper_edge_clearance" in rules,
        "min_clearance": "min_clearance" in rules,
        "netclass": bool(default and "clearance" in default),
    }


def _balanced_blocks(text: str, head: str) -> list[str]:
    blocks = []
    i = 0
    while True:
        i = text.find(head, i)
        if i < 0:
            return blocks
        depth = 0
        j = i
        while j < len(text):
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        blocks.append(text[i : j + 1])
        i = j + 1


_UNIT_NM = {"mm": 1_000_000.0, "mil": 25_400.0, "in": 25_400_000.0, "um": 1_000.0, "\u00b5m": 1_000.0}


def _courtyard_clearance_from_dru(path: str) -> int | None:
    """Largest unconditional ``courtyard_clearance`` min constraint of the sibling .kicad_dru, if any."""
    dru = os.path.splitext(path)[0] + ".kicad_dru"
    if not os.path.isfile(dru):
        return None
    try:
        with open(dru, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return None
    best = None
    for block in _balanced_blocks(text, "(rule"):
        if "(condition" in block:
            continue
        m = re.search(r"\(constraint\s+courtyard_clearance\s*\(\s*min\s+([-0-9.eE+]+)\s*([a-z\u00b5]+)?", block)
        if not m:
            continue
        unit = m.group(2) or "mm"
        if unit not in _UNIT_NM:
            continue
        value = int(round(float(m.group(1)) * _UNIT_NM[unit]))
        best = value if best is None else max(best, value)
    return best


def _read_rules(board, path: str | None, used_nets: set[str], warnings: list[str]) -> Rules:
    ds = board.GetDesignSettings()
    presence = _project_rule_presence(path) if path else None
    edge: int | None
    candidates: list[int] = []
    default_nc: int | None = None
    min_clearance: int | None = None
    if path and presence is None:
        warnings.append("rule_missing:project_file")
        edge = None
    else:
        edge = None
        if presence is None or presence["edge"]:
            edge = int(ds.m_CopperEdgeClearance)
        if presence is None or presence["netclass"]:
            default_nc = _netclass_clearance(ds.m_NetSettings.GetDefaultNetclass())
        if presence is None or presence["min_clearance"]:
            min_clearance = int(ds.m_MinClearance)
    for value in (default_nc, min_clearance):
        if value is not None:
            candidates.append(value)
    clearance = max(candidates) if candidates else None
    if edge is None:
        warnings.append("rule_missing:copper_edge_clearance")
    if clearance is None:
        warnings.append("rule_missing:clearance")

    pair: dict[tuple[str, str], int] = {}
    if clearance is not None:
        eff: dict[str, int] = {}
        for name in sorted(used_nets):
            try:
                net = board.FindNet(name)
                nc = _netclass_clearance(net.GetNetClass()) if net is not None else None
            except Exception:
                nc = None
            eff[name] = max([v for v in (nc, default_nc, min_clearance) if v is not None] or [clearance])
        names = sorted(eff)
        if any(eff[n] != clearance for n in names):
            for i, a in enumerate(names):
                for b in names[i + 1 :]:
                    value = max(eff[a], eff[b])
                    if value != clearance:
                        pair[(a, b)] = value

    courtyard = _courtyard_clearance_from_dru(path) if path else None
    if courtyard is None:
        courtyard = 0
        warnings.append("courtyard_clearance:default_0")
    return Rules(
        clearance_nm=clearance,
        copper_edge_clearance_nm=edge,
        courtyard_clearance_nm=courtyard,
        fallback_margin_nm=0,
        pair_clearance=pair,
    )


# ----------------------------------------------------------------------------- loading


@dataclass
class LoadInfo:
    board: Board
    warnings: tuple[str, ...]
    pcb: Any = field(repr=False)  # the live pcbnew BOARD the model was read from
    path: str | None = None


def open_board(path: str):
    """``pcbnew.LoadBoard`` with a typed error."""
    pcb = get_pcbnew()
    if not os.path.isfile(path):
        raise AdapterError("board_not_found", path)
    try:
        board = pcb.LoadBoard(path)
    except Exception as exc:
        raise AdapterError("open_failed", repr(exc))
    if board is None:
        raise AdapterError("open_failed", path)
    return board


def save_board(board, path: str) -> None:
    pcb = get_pcbnew()
    try:
        ok = pcb.SaveBoard(path, board)
    except Exception as exc:
        raise AdapterError("save_failed", repr(exc))
    if ok is False or not os.path.isfile(path):
        raise AdapterError("save_failed", path)


def _courtyard(fp, side: str) -> tuple:
    pcb = get_pcbnew()
    build = getattr(fp, "BuildCourtyardCaches", None)
    if build is not None:
        try:
            build()
        except Exception:
            pass
    layer = pcb.B_CrtYd if side == "B" else pcb.F_CrtYd
    try:
        return tuple(_polygon_set_outlines(fp.GetCourtyard(layer)))
    except Exception:
        return ()


def _pad_polygon(pad) -> tuple:
    pcb = get_pcbnew()
    layer = pcb.B_Cu if (pad.IsOnLayer(pcb.B_Cu) and not pad.IsOnLayer(pcb.F_Cu)) else pcb.F_Cu
    sps = None
    for call in (lambda: pad.GetEffectivePolygon(layer), lambda: pad.GetEffectivePolygon()):
        try:
            sps = call()
            break
        except Exception:
            sps = None
    if sps is not None:
        try:
            if sps.OutlineCount() > 0:
                pts = _chain_points(sps.Outline(0))
                if len(pts) >= 3:
                    return pts
        except Exception:
            pass
    bb = pad.GetBoundingBox()
    l, t, r, b = int(bb.GetLeft()), int(bb.GetTop()), int(bb.GetRight()), int(bb.GetBottom())
    return ((l, t), (r, t), (r, b), (l, b))


def _has_through_pad(fp) -> bool:
    pcb = get_pcbnew()
    try:
        through = {pcb.PAD_ATTRIB_PTH, pcb.PAD_ATTRIB_NPTH}
        return any(pad.GetAttribute() in through for pad in fp.Pads())
    except Exception:
        return False


def _read_part(fp) -> Part:
    pcb = get_pcbnew()
    side = "B" if fp.GetLayer() == pcb.B_Cu else "F"
    try:
        rotation = int(round(fp.GetOrientation().AsDegrees() * 1000))
    except Exception:
        rotation = int(round(float(fp.GetOrientationDegrees()) * 1000))
    pads = tuple(
        Pad(
            uuid=_uuid(pad),
            net=pad.GetNetname() or "",
            center=_xy(pad.GetPosition()),
            polygon=_pad_polygon(pad),
        )
        for pad in fp.Pads()
    )
    return Part(
        uuid=_uuid(fp),
        ref=fp.GetReference(),
        position=_xy(fp.GetPosition()),
        rotation_mdeg=rotation,
        side=side,
        courtyard=_courtyard(fp, side),
        locked=bool(fp.IsLocked()),
        pads=pads,
        through=_has_through_pad(fp),
    )


def _via_width(via) -> int:
    pcb = get_pcbnew()
    for call in (lambda: via.GetWidth(), lambda: via.GetWidth(pcb.F_Cu)):
        try:
            return int(call())
        except Exception:
            continue
    raise AdapterError("via_width_unreadable", _uuid(via))


def _via_drill(via) -> int:
    for name in ("GetDrillValue", "GetDrill"):
        fn = getattr(via, name, None)
        if fn is not None:
            try:
                return int(fn())
            except Exception:
                continue
    return 0


def _via_layers(board, via) -> tuple[str, ...]:
    try:
        stack = list(board.GetEnabledLayers().CuStack())
        top, bottom = via.TopLayer(), via.BottomLayer()
        if top in stack and bottom in stack:
            i, j = stack.index(top), stack.index(bottom)
            lo, hi = min(i, j), max(i, j)
            return tuple(board.GetLayerName(layer) for layer in stack[lo : hi + 1])
        return (board.GetLayerName(top), board.GetLayerName(bottom))
    except Exception:
        return ("F.Cu", "B.Cu")


def _tuning_group(item) -> str | None:
    try:
        group = item.GetParentGroup()
        if group is not None and "GENERATOR" in str(group.GetClass()).upper():
            return _uuid(group)
    except Exception:
        pass
    return None


def _edge_shapes(board) -> list:
    pcb = get_pcbnew()
    shapes = []
    for item in board.GetDrawings():
        if item.GetClass() != "PCB_SHAPE" or item.GetLayer() != pcb.Edge_Cuts:
            continue
        shapes.append(_cast(item, "PCB_SHAPE", "GetShape"))
    return shapes


def _read_outline(board, warnings: list[str]) -> Outline:
    pcb = get_pcbnew()
    seg_t = _shape_const("SHAPE_T_SEGMENT", "S_SEGMENT")
    arc_t = _shape_const("SHAPE_T_ARC", "S_ARC")
    edges = []
    for shape in _edge_shapes(board):
        kind = shape.GetShape()
        uuid = _uuid(shape)
        if seg_t is not None and kind == seg_t:
            edges.append(EdgeSeg(uuid, _xy(shape.GetStart()), _xy(shape.GetEnd())))
        elif arc_t is not None and kind == arc_t:
            # KiCad 10's PCB_SHAPE has no GetArcStart/GetArcEnd: for an arc GetStart()/GetEnd() are its ends.
            a_start = shape.GetArcStart() if hasattr(shape, "GetArcStart") else shape.GetStart()
            a_end = shape.GetArcEnd() if hasattr(shape, "GetArcEnd") else shape.GetEnd()
            edges.append(EdgeSeg(uuid, _xy(a_start), _xy(a_end), _xy(shape.GetArcMid())))
        else:
            warnings.append(f"edge_shape_not_draggable:{uuid}:{kind}")

    polygons: tuple = ()
    for args in ((), (False,), (True,)):
        sps = pcb.SHAPE_POLY_SET()
        try:
            ok = board.GetBoardPolygonOutlines(sps, *args)
        except TypeError:
            continue
        except Exception:
            break
        if ok is False or sps.OutlineCount() == 0:
            continue
        best = max(
            range(sps.OutlineCount()),
            key=lambda i: abs(geo.polygon_area2(_chain_points(sps.Outline(i)))),
        )
        if sps.OutlineCount() > 1:
            warnings.append(f"multiple_outlines:{sps.OutlineCount()}")
        rings = [_chain_points(sps.Outline(best))]
        for h in range(sps.HoleCount(best)):
            rings.append(_chain_points(sps.Hole(best, h)))
        polygons = tuple(r for r in rings if len(r) >= 3)
        break
    if not polygons:
        warnings.append("no_board_outline")
    return Outline(polygons=polygons, edges=tuple(edges))


def _read_zones(board, warnings: list[str]):
    keepouts, zones = [], []
    for z in board.Zones():
        uuid = _uuid(z)
        sps = z.Outline()
        rings = _polygon_set_outlines(sps)
        if not rings:
            warnings.append(f"zone_without_outline:{uuid}")
            continue
        if len(rings) > 1:
            warnings.append(f"zone_multi_outline:{uuid}")
        layers = _layer_names(board, z)
        if z.GetIsRuleArea():
            keepouts.append(
                Keepout(
                    uuid=uuid,
                    polygon=rings[0],
                    layers=layers,
                    no_tracks=_flag(z, "GetDoNotAllowTracks"),
                    no_vias=_flag(z, "GetDoNotAllowVias"),
                    no_footprints=_flag(z, "GetDoNotAllowFootprints"),
                    no_pour=_flag(z, "GetDoNotAllowZoneFills"),
                )
            )
        else:
            zones.append(Zone(uuid=uuid, net=z.GetNetname() or "", layers=layers, polygon=rings[0]))
    return tuple(keepouts), tuple(zones)


def load_board_with_info(source) -> LoadInfo:
    """Read a board (path or pcbnew BOARD) into the neutral model."""
    if isinstance(source, (str, os.PathLike)):
        path = os.path.abspath(os.fspath(source))
        pcb_board = open_board(path)
    else:
        pcb_board = source
        name = ""
        try:
            name = pcb_board.GetFileName()
        except Exception:
            pass
        path = os.path.abspath(name) if name else None
    warnings: list[str] = []
    try:
        pcb_board.BuildConnectivity()
    except Exception:
        pass

    parts = tuple(_read_part(fp) for fp in pcb_board.GetFootprints())
    tracks, vias = [], []
    for item in pcb_board.GetTracks():
        cls = item.GetClass()
        if cls == "PCB_VIA":
            via = _cast(item, "PCB_VIA", "GetDrillValue")
            vias.append(
                Via(
                    uuid=_uuid(via),
                    net=via.GetNetname() or "",
                    pos=_xy(via.GetPosition()),
                    diameter=_via_width(via),
                    drill=_via_drill(via),
                    layers=_via_layers(pcb_board, via),
                    locked=bool(via.IsLocked()),
                )
            )
        elif cls in ("PCB_TRACK", "PCB_ARC"):
            mid = None
            if cls == "PCB_ARC":
                arc = _cast(item, "PCB_ARC", "GetMid")
                mid = _xy(arc.GetMid())
                item = arc
            tracks.append(
                Track(
                    uuid=_uuid(item),
                    net=item.GetNetname() or "",
                    layer=pcb_board.GetLayerName(item.GetLayer()),
                    start=_xy(item.GetStart()),
                    end=_xy(item.GetEnd()),
                    width=int(item.GetWidth()),
                    mid=mid,
                    locked=bool(item.IsLocked()),
                    group=_tuning_group(item),
                )
            )
    outline = _read_outline(pcb_board, warnings)
    keepouts, zones = _read_zones(pcb_board, warnings)
    used_nets = (
        {p.net for part in parts for p in part.pads if p.net}
        | {t.net for t in tracks if t.net}
        | {v.net for v in vias if v.net}
    )
    rules = _read_rules(pcb_board, path, used_nets, warnings)
    model = Board(
        parts=parts,
        tracks=tuple(tracks),
        vias=tuple(vias),
        outline=outline,
        keepouts=keepouts,
        zones=zones,
        rules=rules,
    )
    return LoadInfo(model, tuple(sorted(set(warnings))), pcb_board, path)


def load_board(source) -> Board:
    """Neutral model of a .kicad_pcb path or a pcbnew BOARD."""
    return load_board_with_info(source).board


# ----------------------------------------------------------------------------- zones and connectivity


def zone_filled_areas(board) -> dict[str, int]:
    """Filled copper area (nm^2, rounded) per non-rule-area zone, summed over its layers."""
    out: dict[str, int] = {}
    for z in board.Zones():
        if z.GetIsRuleArea():
            continue
        total = 0.0
        for layer in z.GetLayerSet().Seq():
            try:
                has = getattr(z, "HasFilledPolysForLayer", None)
                if has is not None and not has(layer):
                    continue
                total += float(z.GetFilledPolysList(layer).Area())
            except Exception:
                continue
        out[_uuid(z)] = int(round(total))
    return out


def fill_zones(board) -> int:
    """Refill every zone with ZONE_FILLER (rule areas are skipped by the filler). Returns zone count."""
    pcb = get_pcbnew()
    board.BuildConnectivity()
    zones = board.Zones()
    count = sum(1 for z in zones if not z.GetIsRuleArea())
    if count == 0:
        return 0
    filler = pcb.ZONE_FILLER(board)
    ok = filler.Fill(zones)
    if ok is False:
        raise AdapterError("zone_fill_failed")
    return count


def connectivity_stats(board) -> dict:
    """``{'unconnected': n, 'pad_nets': {'<ref>:<pad uuid>': netname}}`` after a connectivity rebuild."""
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    try:
        conn.RecalculateRatsnest()
    except Exception:
        pass
    unconnected = None
    for args in ((), (False,)):
        try:
            unconnected = int(conn.GetUnconnectedCount(*args))
            break
        except Exception:
            continue
    pad_nets = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            pad_nets[f"{fp.GetReference()}:{_uuid(pad)}"] = pad.GetNetname() or ""
    return {"unconnected": unconnected, "pad_nets": pad_nets}


# ----------------------------------------------------------------------------- applying a ChangeSet


def apply_changeset(board, changeset: ChangeSet) -> None:
    """Mutate the pcbnew BOARD: part moves, track ends (+bend split), via moves, Edge.Cuts endpoints.

    Every uuid is validated before anything changes. A ``TrackChange.bend`` keeps the original uuid for
    ``start -> bend`` and adds a new track ``bend -> end`` with the same net, layer, width and lock.
    """
    pcb = get_pcbnew()
    parts = {_uuid(fp): fp for fp in board.GetFootprints()}
    tracks: dict[str, Any] = {}
    vias: dict[str, Any] = {}
    for item in board.GetTracks():
        cls = item.GetClass()
        if cls == "PCB_VIA":
            vias[_uuid(item)] = _cast(item, "PCB_VIA", "GetDrillValue")
        elif cls == "PCB_TRACK":
            tracks[_uuid(item)] = item
        elif cls == "PCB_ARC":
            tracks[_uuid(item)] = None  # present, but arcs cannot be re-aimed
    seg_t = _shape_const("SHAPE_T_SEGMENT", "S_SEGMENT")
    edges = {}
    for shape in _edge_shapes(board):
        edges[_uuid(shape)] = shape

    for move in changeset.part_moves:
        if move.uuid not in parts:
            raise AdapterError("unknown_uuid", f"part:{move.uuid}")
    for move in changeset.via_moves:
        if move.uuid not in vias:
            raise AdapterError("unknown_uuid", f"via:{move.uuid}")
    for change in changeset.track_changes:
        if change.uuid not in tracks:
            raise AdapterError("unknown_uuid", f"track:{change.uuid}")
        if tracks[change.uuid] is None:
            raise AdapterError("arc_track_unsupported", change.uuid)
    for change in changeset.edge_changes:
        shape = edges.get(change.uuid)
        if shape is None:
            raise AdapterError("unknown_uuid", f"edge:{change.uuid}")
        if seg_t is None or shape.GetShape() != seg_t:
            raise AdapterError("edge_shape_unsupported", change.uuid)

    for move in changeset.part_moves:
        fp = parts[move.uuid]
        pos = fp.GetPosition()
        fp.SetPosition(_vec(pos.x + move.dx, pos.y + move.dy))
    for move in changeset.via_moves:
        via = vias[move.uuid]
        pos = via.GetPosition()
        via.SetPosition(_vec(pos.x + move.dx, pos.y + move.dy))
    for change in changeset.track_changes:
        track = tracks[change.uuid]
        track.SetStart(_vec(*change.start))
        if change.bend is None:
            track.SetEnd(_vec(*change.end))
            continue
        track.SetEnd(_vec(*change.bend))
        extra = pcb.PCB_TRACK(board)
        extra.SetStart(_vec(*change.bend))
        extra.SetEnd(_vec(*change.end))
        extra.SetLayer(track.GetLayer())
        extra.SetWidth(track.GetWidth())
        extra.SetNetCode(track.GetNetCode())
        try:
            extra.SetLocked(track.IsLocked())
        except Exception:
            pass
        board.Add(extra)
    for change in changeset.edge_changes:
        shape = edges[change.uuid]
        shape.SetStart(_vec(*change.start))
        shape.SetEnd(_vec(*change.end))
    board.BuildConnectivity()
