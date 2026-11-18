"""Synthetic pcbnew boards for the adapter / session tests. Everything is written to a directory the
caller owns (the tests use a temp dir); nothing is written anywhere else.

Layout of ``build(outline="rect")`` (mm, KiCad y down), outline 100..160 x 100..130:

    R1 (110,110)  R2 (115,110)  R3 (125,110)   a row of 2-pad parts, courtyard 3.6 x 1.8, pads 0.9 square
    R4 (120,122)                               free part: moved +14 mm in x it enters the GND pour
    R5 (145,120)                               GND tap inside the pour (keeps the pour from being an island)
    track N1 : R1.pad1 (109,110) -> via (104,110)         re-aims when R1 moves
    track N4 : R2.pad2 (116,110) -> R3.pad1 (124,110)     re-aims when R2 is pushed
    net N2   : R1.pad2 and R2.pad1, deliberately unrouted (one ratsnest line)
    GND pour : F.Cu x 130..158, y 102..128, plus a GND via at (140,104)
    keepout  : rule area x 150..158, y 102..108, no tracks / vias / footprints / pour

``build(outline="concave")`` uses an L shaped outline and only R1..R3, the tracks, the via and the
keepout (no pour, no R4/R5).

Board rules live where KiCad keeps them, in the sibling ``.kicad_pro`` (netclass clearance,
``min_copper_edge_clearance``) and, when ``courtyard_clearance_mm`` > 0, ``.kicad_dru``. The board is
written, reloaded (so pcbnew has a project and DRC engine), its zones are filled and it is saved again.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass

from adaptive_engine import pcbnew_adapter as adapter

RECT_OUTLINE = ((100, 100), (160, 100), (160, 130), (100, 130))
CONCAVE_OUTLINE = ((100, 100), (160, 100), (160, 118), (135, 118), (135, 130), (100, 130))
HALF_W, HALF_H, PAD_DX, PAD_SIZE = 1.8, 0.9, 1.0, 0.9


def mm(value: float) -> int:
    return int(round(value * 1_000_000))


@dataclass(frozen=True)
class Fixture:
    root: str
    board_path: str

    @property
    def pro_path(self) -> str:
        return os.path.splitext(self.board_path)[0] + ".kicad_pro"


def clone(fx: Fixture, dest_root: str) -> Fixture:
    """Copy a fixture (board + project + rules files) into ``dest_root``."""
    os.makedirs(dest_root, exist_ok=True)
    stem = os.path.splitext(fx.board_path)[0]
    new_stem = os.path.join(dest_root, os.path.basename(stem))
    for ext in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        if os.path.isfile(stem + ext):
            shutil.copyfile(stem + ext, new_stem + ext)
    return Fixture(dest_root, new_stem + ".kicad_pcb")


# ----------------------------------------------------------------------------- pcbnew helpers


def _pcb():
    return adapter.get_pcbnew()


def _v(x_mm: float, y_mm: float):
    return _pcb().VECTOR2I(mm(x_mm), mm(y_mm))


def _layered(obj, name: str, *args):
    """Call ``obj.<name>(*args)``; KiCad 9+ pad/via setters may want a layer first."""
    fn = getattr(obj, name)
    try:
        return fn(*args)
    except TypeError:
        return fn(_pcb().F_Cu, *args)


def _seg_const():
    pcb = _pcb()
    value = getattr(pcb, "SHAPE_T_SEGMENT", None)
    return pcb.S_SEGMENT if value is None else value


def _edge_segment(board, a, b, layer, width_mm=0.1):
    shape = _pcb().PCB_SHAPE(board)
    shape.SetShape(_seg_const())
    shape.SetStart(_v(*a))
    shape.SetEnd(_v(*b))
    shape.SetLayer(layer)
    shape.SetWidth(mm(width_mm))
    board.Add(shape)


def _nets(board, names):
    out = {}
    for name in names:
        item = _pcb().NETINFO_ITEM(board, name)
        board.Add(item)
        out[name] = item
    return out


def _part(board, nets, ref, cx, cy, pad_nets):
    pcb = _pcb()
    fp = pcb.FOOTPRINT(board)
    fp.SetReference(ref)
    fp.SetValue(ref)
    try:
        fp.SetFPID(pcb.LIB_ID("adaptive", ref))
    except Exception:
        pass
    fp.SetPosition(_v(cx, cy))
    corners = [
        (cx - HALF_W, cy - HALF_H),
        (cx + HALF_W, cy - HALF_H),
        (cx + HALF_W, cy + HALF_H),
        (cx - HALF_W, cy + HALF_H),
    ]
    for i in range(4):
        _edge_segment(fp, corners[i], corners[(i + 1) % 4], pcb.F_CrtYd, 0.05)
    rect = getattr(pcb, "PAD_SHAPE_RECT", None)
    if rect is None:
        rect = pcb.PAD_SHAPE_RECTANGLE
    for k, (dx, net) in enumerate(zip((-PAD_DX, PAD_DX), pad_nets)):
        pad = pcb.PAD(fp)
        pad.SetNumber(str(k + 1))
        _layered(pad, "SetShape", rect)
        pad.SetAttribute(pcb.PAD_ATTRIB_SMD)
        _layered(pad, "SetSize", _v(PAD_SIZE, PAD_SIZE))
        pad.SetPosition(_v(cx + dx, cy))
        pad.SetLayerSet(pcb.PAD.SMDMask())
        pad.SetNet(nets[net])
        fp.Add(pad)
    board.Add(fp)
    return fp


def _track(board, nets, net, a, b, width_mm=0.25):
    track = _pcb().PCB_TRACK(board)
    track.SetStart(_v(*a))
    track.SetEnd(_v(*b))
    track.SetWidth(mm(width_mm))
    track.SetLayer(_pcb().F_Cu)
    track.SetNet(nets[net])
    board.Add(track)


def _via(board, nets, net, at, diameter_mm=0.8, drill_mm=0.4):
    via = _pcb().PCB_VIA(board)
    via.SetPosition(_v(*at))
    _layered(via, "SetWidth", mm(diameter_mm))
    via.SetDrill(mm(drill_mm))
    via.SetNet(nets[net])
    board.Add(via)


def _zone(board, ring_mm, *, net=None, rule_area=False, flags=()):
    zone = _pcb().ZONE(board)
    zone.SetLayer(_pcb().F_Cu)
    if rule_area:
        zone.SetIsRuleArea(True)
        for name in flags:
            getattr(zone, name)(True)
    if net is not None:
        zone.SetNet(net)
        zone.SetMinThickness(mm(0.25))
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in ring_mm:
        try:
            outline.Append(mm(x), mm(y))
        except TypeError:
            outline.Append(_v(x, y))
    board.Add(zone)
    return zone


# ----------------------------------------------------------------------------- files


def _write_project(path, clearance_mm, edge_clearance_mm):
    default_class = {
        "bus_width": 12, "clearance": clearance_mm, "diff_pair_gap": 0.25, "diff_pair_via_gap": 0.25,
        "diff_pair_width": 0.2, "line_style": 0, "microvia_diameter": 0.3, "microvia_drill": 0.1,
        "name": "Default", "pcb_color": "rgba(0, 0, 0, 0.000)", "schematic_color": "rgba(0, 0, 0, 0.000)",
        "track_width": 0.25, "via_diameter": 0.8, "via_drill": 0.4, "wire_width": 6,
    }
    project = {
        "board": {"design_settings": {"rules": {"min_clearance": 0.0, "min_copper_edge_clearance": edge_clearance_mm}}},
        "meta": {"filename": os.path.basename(path), "version": 1},
        "net_settings": {
            "classes": [default_class],
            "meta": {"version": 3},
            "net_colors": None,
            "netclass_assignments": None,
            "netclass_patterns": [],
        },
    }
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(project, fh, indent=2)


def build(
    root: str,
    name: str = "fixture",
    *,
    outline: str = "rect",
    extras: bool | None = None,
    clearance_mm: float = 0.2,
    edge_clearance_mm: float = 0.5,
    courtyard_clearance_mm: float = 0.0,
) -> Fixture:
    """Write ``<root>/<name>.kicad_pcb`` (+ ``.kicad_pro``, optionally ``.kicad_dru``) and return it."""
    if outline not in ("rect", "concave"):
        raise ValueError(outline)
    extras = (outline == "rect") if extras is None else extras
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name + ".kicad_pcb")
    _write_project(os.path.join(root, name + ".kicad_pro"), clearance_mm, edge_clearance_mm)
    if courtyard_clearance_mm > 0:
        with open(os.path.join(root, name + ".kicad_dru"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write('(version 1)\n(rule "adaptive_courtyard"\n  (constraint courtyard_clearance (min %.4fmm)))\n'
                     % courtyard_clearance_mm)

    pcb = _pcb()
    board = pcb.BOARD()
    try:
        board.SetCopperLayerCount(2)
    except Exception:
        pass
    ring = RECT_OUTLINE if outline == "rect" else CONCAVE_OUTLINE
    for i in range(len(ring)):
        _edge_segment(board, ring[i], ring[(i + 1) % len(ring)], pcb.Edge_Cuts)

    names = ["N1", "N2", "N4", "N5", "GND", "X1", "X2", "N6"]
    nets = _nets(board, names)
    _part(board, nets, "R1", 110, 110, ("N1", "N2"))
    _part(board, nets, "R2", 115, 110, ("N2", "N4"))
    _part(board, nets, "R3", 125, 110, ("N4", "N5"))
    _track(board, nets, "N1", (109, 110), (104, 110))
    _via(board, nets, "N1", (104, 110))
    _track(board, nets, "N4", (116, 110), (124, 110))
    _zone(
        board,
        ((150, 102), (158, 102), (158, 108), (150, 108)),
        rule_area=True,
        flags=("SetDoNotAllowTracks", "SetDoNotAllowVias", "SetDoNotAllowFootprints", "SetDoNotAllowZoneFills"),
    )
    if extras:
        _part(board, nets, "R4", 120, 122, ("X1", "X2"))
        _part(board, nets, "R5", 145, 120, ("GND", "GND"))
        _via(board, nets, "GND", (140, 104))
        _zone(board, ((130, 102), (158, 102), (158, 128), (130, 128)), net=nets["GND"])

    adapter.save_board(board, path)
    reloaded = adapter.open_board(path)  # a loaded board has the project / DRC engine the filler needs
    adapter.fill_zones(reloaded)
    adapter.save_board(reloaded, path)
    return Fixture(root, path)


# ----------------------------------------------------------------------------- lookups on the model


def part(model, ref):
    return next(p for p in model.parts if p.ref == ref)


def track(model, net):
    return next(t for t in model.tracks if t.net == net)


def via(model, net):
    return next(v for v in model.vias if v.net == net)


def edge_between(model, a_mm, b_mm):
    """The Edge.Cuts segment whose end points are the two given points (either order)."""
    a, b = (mm(a_mm[0]), mm(a_mm[1])), (mm(b_mm[0]), mm(b_mm[1]))
    return next(e for e in model.outline.edges if {e.start, e.end} == {a, b})
