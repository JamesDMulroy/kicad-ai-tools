"""Copper adaptation: re-aim track legs to moved terminals (pure Python, no pcbnew import).

Public API
----------
``adapt_copper(board, part_moves, allow_partial_hint=True) -> CopperResult``
``connectivity_signature(board) -> frozenset``   per-net connectivity partition (test helper)
``apply_changes(board, result) -> Board``        apply track changes / bends / via moves (test helper)

Units are integer nanometres. ``part_moves`` maps each moving part uuid to its FULL displacement
``(dx, dy)`` from its original position; parts not in the dict stay. The board passed in is the
board *before* the move. The engine never deletes, reroutes or detaches anything: a segment is
only shifted rigidly together with its whole chain, or re-aimed with at most one bend point.

Model
-----
* Nodes: track ends coincide when they share a point, a layer and a net; a via at that point on
  one of its layers merges the layers into one node; a track end inside the polygon of a same-net
  pad also belongs to that pad (pads carry no layer in the model, so a pad is treated as present
  on every copper layer: conservative for clearance, and a pad terminal on any layer).
* A node is a *terminal* when it holds a pad, a via with other than two track ends, a junction of
  three or more track ends, or a free end. A plain 2-end node or a via with exactly two track ends
  and no pad is a pass-through of the chain.
* A *chain* is the maximal run of tracks between two terminals (the leg of the spec).
* Terminal vectors: a pad moves with its part; a terminal via moves with the one moved part whose
  courtyard encloses it (a via only counts as moving if every chain on it also translates rigidly);
  a junction is fixed; a free end is unconstrained.
* Rigid: all constrained terminals share one non-zero vector -> the whole chain and its internal
  vias translate by it. Otherwise the segments touching a moved pad are re-aimed (pad end follows
  the pad, far end stays) and nothing else changes.
* Re-aim bend: if the original segment was horizontal, vertical or 45 degrees and the re-aimed
  segment is not, one bend point is inserted. The part at the fixed end keeps the ORIGINAL direction
  class; the pad-side part is another legal class. Among the candidates the preferred one keeps
  the original sense of direction at the fixed end, then has the smaller total length, then the
  smaller point. A segment that was not 0/45/90 is re-aimed without a bend.
* Arcs cannot be re-aimed or rigidly translated (``TrackChange`` has no ``mid``): the leg is
  reported ``leg_blocked`` with warning ``arc_reaim_unsupported`` / ``arc_translate_unsupported``
  and no change is emitted. Arc length uses the two chords through ``mid``; arcs of other nets are
  checked as those chords.
* Tuned chains are any chain with a track that has a ``group`` (a tuning-pattern group is always
  tuned) or a meander. A meander is a chain holding at least 2 consecutive U-turns. A *corner* is
  a turn of about 90 degrees (+-5): a single 90 degree bend, two successive same-sign 45 degree
  bends (a chamfer), or an arc segment with ``mid`` (its sweep is ``2 * angle(chord1, chord2)``).
  A *U-turn* is two successive corners of the same sign (about 180 degrees in total, so its entry
  and exit legs are antiparallel). Successive U-turns must alternate handedness (corner signs
  ``s, s, -s, -s``) and have equal pitch: the distance between the two legs of each U-turn, equal
  within 10 nm. A staircase (corner signs alternate one by one) or a spiral (one sign) is never a
  meander, however regular its steps, whatever the number of segments.
* Tuned geometry is never re-aimed, only rigidly translated: rigid translation of a tuned chain is
  always allowed, any re-aim (shape change) is blocked with ``tuned_leg`` and no change is emitted
  for that chain. The leg dict of a blocked tuned chain carries ``reason``: ``'stretch'`` when the
  re-aimed length would grow by more than 1 nm, otherwise ``'shape change'`` (a shortening or a
  length-neutral re-aim). Legs that are not tuned blocks have no ``reason`` key.
* Rule failures keep the would-be changes in the result (diagnostics) but add a ``leg_blocked``
  blocker per failing track and set ``ok`` False. Missing rule data warns ``missing_rule:<name>``
  and skips only that check when this module is called directly; ``solver.solve`` refuses to
  solve at all (``rejected`` / ``missing_rule``) when a clearance it needs is missing, so no value
  is ever guessed there.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

from .geometry import (
    bbox,
    point_in_polygon,
    point_segment_distance,
    region_contains,
    segment_polygon_distance,
    segment_segment_distance,
)
from .model import Blocker, Board, Point, Track, TrackChange, ViaMove

__all__ = ["CopperResult", "adapt_copper", "apply_changes", "connectivity_signature"]

ZERO = (0, 0)
_EPS = 1e-6
_LEN_TOL = 1.0  # nm: a tuned chain longer by more than this after a re-aim is stretched
_CORNER_DEG = 90.0  # a meander corner turns by this much ...
_CORNER_TOL_DEG = 5.0  # ... within this tolerance (also for the legs of a U-turn: 180 +- this)
_MIN_TURN_DEG = 1.0  # smaller turns count as a straight continuation
_PITCH_TOL_NM = 10.0  # U-turn pitches closer than this are equal
_DIRS = {"D1": (1, 1), "D2": (1, -1), "H": (1, 0), "V": (0, 1)}


@dataclass(frozen=True)
class CopperResult:
    ok: bool
    track_changes: tuple[TrackChange, ...] = ()
    via_moves: tuple[ViaMove, ...] = ()
    blockers: tuple[Blocker, ...] = ()
    legs: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()


# ----------------------------------------------------------------------------- small helpers


def _sh(p: Point, v) -> Point:
    return (p[0] + v[0], p[1] + v[1])


def _len(a: Point, b: Point) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _classify(dx: int, dy: int) -> str | None:
    """Direction class of a vector: H, V, D1 (dx == dy), D2 (dx == -dy) or None."""
    if dx == 0 and dy == 0:
        return None
    if dy == 0:
        return "H"
    if dx == 0:
        return "V"
    if dx == dy:
        return "D1"
    if dx == -dy:
        return "D2"
    return None


def _track_len(t: Track) -> float:
    if t.mid is not None:
        return _len(t.start, t.mid) + _len(t.mid, t.end)
    return _len(t.start, t.end)


def _change_len(c: TrackChange) -> float:
    if c.bend is not None:
        return _len(c.start, c.bend) + _len(c.bend, c.end)
    return _len(c.start, c.end)


def _pieces_of_change(c: TrackChange) -> list[tuple[Point, Point]]:
    if c.bend is not None:
        return [(c.start, c.bend), (c.bend, c.end)]
    return [(c.start, c.end)]


def _pieces_of_track(t: Track) -> list[tuple[Point, Point]]:
    if t.mid is not None:
        return [(t.start, t.mid), (t.mid, t.end)]
    return [(t.start, t.end)]


def _seg_far(a, b, c, d, reach: float) -> bool:
    """Cheap bounding-box reject: the two segments are farther apart than ``reach``."""
    return (
        max(a[0], b[0]) + reach < min(c[0], d[0])
        or max(c[0], d[0]) + reach < min(a[0], b[0])
        or max(a[1], b[1]) + reach < min(c[1], d[1])
        or max(c[1], d[1]) + reach < min(a[1], b[1])
    )


def _intersect_lines(a: Point, d1, b: Point, d2) -> Point | None:
    """Integer intersection of the lines a + s*d1 and b + t*d2, or None."""
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if den == 0:
        return None
    num = (b[0] - a[0]) * d2[1] - (b[1] - a[1]) * d2[0]
    if num % den != 0:
        return None
    s = num // den
    return (a[0] + s * d1[0], a[1] + s * d1[1])


def _reaim_segment(old_s, old_e, new_s, new_e, anchor_idx: int):
    """Re-aimed segment as ``(start, end, bend)`` in the track's own orientation.

    ``anchor_idx`` is the end (0 start, 1 end) that keeps the original direction class.
    """
    if anchor_idx == 0:
        old_a, old_b, a, b = old_s, old_e, new_s, new_e
    else:
        old_a, old_b, a, b = old_e, old_s, new_e, new_s
    d0 = (old_b[0] - old_a[0], old_b[1] - old_a[1])
    cls = _classify(*d0)
    if cls is None or a == b or _classify(b[0] - a[0], b[1] - a[1]) is not None:
        return new_s, new_e, None
    best = None
    for other in sorted(_DIRS):
        if other == cls:
            continue
        k = _intersect_lines(a, _DIRS[cls], b, _DIRS[other])
        if k is None or k == a or k == b:
            continue
        ka = (k[0] - a[0], k[1] - a[1])
        kb = (b[0] - k[0], b[1] - k[1])
        if _classify(*ka) != cls or _classify(*kb) in (None, cls):
            continue
        same_sense = ka[0] * d0[0] + ka[1] * d0[1] > 0
        key = (0 if same_sense else 1, round(math.hypot(*ka) + math.hypot(*kb), 6), k)
        if best is None or key < best[0]:
            best = (key, k)
    if best is None:
        return new_s, new_e, None
    return new_s, new_e, best[1]


# ----------------------------------------------------------------------------- graph


class _Node:
    __slots__ = ("key", "net", "point", "via", "pads", "ends", "kind")

    def __init__(self, key, net, point, via, pads):
        self.key = key
        self.net = net
        self.point = point
        self.via = via
        self.pads = pads  # sorted [(part_uuid, Pad)]
        self.ends: list[tuple[str, int]] = []
        self.kind = ""


class _Chain:
    __slots__ = ("net", "segs", "n0", "n1", "vias")

    def __init__(self, net, segs, n0, n1, vias):
        self.net = net
        self.segs = segs  # [(Track, forward)] in traversal order
        self.n0 = n0
        self.n1 = n1
        self.vias = vias  # internal pass-through vias

    @property
    def uuids(self) -> list[str]:
        return sorted(t.uuid for t, _ in self.segs)


class _Plan:
    def __init__(self, chain: _Chain, kind: str, tuned: bool):
        self.chain = chain
        self.kind = kind
        self.tuned = tuned
        self.changes: dict[str, TrackChange] = {}
        self.via_moves: dict[str, tuple[int, int]] = {}
        self.blockers: list[tuple[str, str]] = []
        self.rule_blocked = False
        self.reason: str | None = None  # 'stretch' | 'shape change' for blocked tuned chains

    @property
    def emit(self) -> bool:
        return not self.blockers


def _turn_deg(a, b) -> float:
    """Signed turn in degrees from direction ``a`` to direction ``b`` (positive when cross > 0)."""
    return math.degrees(math.atan2(a[0] * b[1] - a[1] * b[0], a[0] * b[0] + a[1] * b[1]))


def _meander_atoms(chain: _Chain) -> list:
    """Turns along the chain as ``[turn_deg, entry_line, exit_line]``.

    A turn is the bend between two successive straight segments, or the sweep of an arc segment
    (arcs are assumed tangent to their neighbours, so no extra bend is counted at an arc joint).
    A line is ``(a, b)``, the straight segment in traversal direction; ``None`` where the chain
    has no straight segment on that side. Turns below ``_MIN_TURN_DEG`` are straight continuations.
    """
    atoms: list = []
    last_line = None
    last_dir = None  # direction of the previous piece when that was a straight segment
    for t, fwd in chain.segs:
        a, b = (t.start, t.end) if fwd else (t.end, t.start)
        if t.mid is not None:
            d1 = (t.mid[0] - a[0], t.mid[1] - a[1])
            d2 = (b[0] - t.mid[0], b[1] - t.mid[1])
            last_dir = None
            if d1 == ZERO or d2 == ZERO:
                continue
            sweep = 2.0 * _turn_deg(d1, d2)
            if abs(sweep) >= _MIN_TURN_DEG:
                atoms.append([sweep, last_line, None])
            continue
        d = (b[0] - a[0], b[1] - a[1])
        if d == ZERO:
            continue
        if last_dir is not None:
            ang = _turn_deg(last_dir, d)
            if abs(ang) >= _MIN_TURN_DEG:
                atoms.append([ang, last_line, (a, b)])
        for atom in atoms:
            if atom[2] is None:
                atom[2] = (a, b)  # the straight segment after an arc closes its exit side
        last_line, last_dir = (a, b), d
    return atoms


def _meander_corners(atoms: list) -> list:
    """Group turns into corners ``(sign, entry_line, exit_line)``; ``None`` marks a turn that is
    not part of a corner (it breaks any meander across it).

    A corner is one turn of about 90 degrees (a 90 degree bend or an arc) or two successive
    same-sign turns adding up to about 90 degrees (a 45 + 45 degree chamfer).
    """
    corners: list = []
    i = 0
    while i < len(atoms):
        ang, entry, exit_ = atoms[i]
        sign = 1 if ang > 0 else -1
        if abs(abs(ang) - _CORNER_DEG) <= _CORNER_TOL_DEG:
            corners.append((sign, entry, exit_))
            i += 1
            continue
        if i + 1 < len(atoms):
            ang2, _, exit2 = atoms[i + 1]
            if ang * ang2 > 0 and abs(abs(ang + ang2) - _CORNER_DEG) <= _CORNER_TOL_DEG:
                corners.append((sign, entry, exit2))
                i += 2
                continue
        corners.append(None)
        i += 1
    return corners


def _u_pitch(c1, c2):
    """Distance between the entry leg of corner ``c1`` and the exit leg of corner ``c2`` (the two
    legs of one U-turn), or None when a leg is missing or the legs are not antiparallel."""
    entry, exit_ = c1[1], c2[2]
    if entry is None or exit_ is None:
        return None
    da = (entry[1][0] - entry[0][0], entry[1][1] - entry[0][1])
    db = (exit_[1][0] - exit_[0][0], exit_[1][1] - exit_[0][1])
    if abs(abs(_turn_deg(da, db)) - 180.0) > _CORNER_TOL_DEG:
        return None
    mx = (exit_[0][0] + exit_[1][0]) / 2
    my = (exit_[0][1] + exit_[1][1]) / 2
    return abs(da[0] * (my - entry[0][1]) - da[1] * (mx - entry[0][0])) / math.hypot(*da)


def _serpentine_window(corners: list) -> bool:
    """>= 2 consecutive U-turns of alternating handedness and equal pitch.

    The corner signs of the four corners involved are ``s, s, -s, -s``: the first two form one
    U-turn, the next two the following U-turn of the opposite hand. A staircase (signs alternate
    every corner) and a spiral (one sign) never contain that pattern.
    """
    for i in range(len(corners) - 3):
        w = corners[i : i + 4]
        if any(c is None for c in w):
            continue
        s = w[0][0]
        if (w[1][0], w[2][0], w[3][0]) != (s, -s, -s):
            continue
        p1, p2 = _u_pitch(w[0], w[1]), _u_pitch(w[2], w[3])
        if p1 is not None and p2 is not None and abs(p1 - p2) <= _PITCH_TOL_NM:
            return True
    return False


def _is_serpentine(chain: _Chain) -> bool:
    return _serpentine_window(_meander_corners(_meander_atoms(chain)))


def _is_tuned(chain: _Chain) -> bool:
    return any(t.group is not None for t, _ in chain.segs) or _is_serpentine(chain)


# ----------------------------------------------------------------------------- main entry


def adapt_copper(
    board: Board, part_moves: dict[str, Point], allow_partial_hint: bool = True
) -> CopperResult:
    # covers: ADE-2
    """Adapt copper for the given part displacements. See the module docstring.

    ``allow_partial_hint`` is accepted for API symmetry; the partial back-off lives in the solver.
    """
    del allow_partial_hint
    warnings: set[str] = set()
    moves: dict[str, tuple[int, int]] = {}
    for uuid in sorted(part_moves):
        raw = part_moves[uuid]
        vec = (int(raw[0]), int(raw[1]))
        try:
            board.part(uuid)
        except KeyError:
            warnings.add("unknown_part:" + uuid)
            continue
        if vec != ZERO:
            moves[uuid] = vec
    if not moves or not board.tracks:
        return CopperResult(True, (), (), (), (), tuple(sorted(warnings)))

    rules = board.rules
    tracks = sorted(board.tracks, key=lambda t: t.uuid)
    tracks_by = {t.uuid: t for t in tracks}

    # ---- pad index (original positions) and via index
    pad_index: dict[str, list] = {}
    for part in sorted(board.parts, key=lambda p: p.uuid):
        for pad in sorted(part.pads, key=lambda p: p.uuid):
            pts = pad.polygon if len(pad.polygon) > 0 else (pad.center,)
            pad_index.setdefault(pad.net, []).append((bbox(pts), part.uuid, pad))
    via_index: dict[tuple, list] = {}
    for via in sorted(board.vias, key=lambda v: v.uuid):
        via_index.setdefault((via.net, via.pos), []).append(via)

    pad_cache: dict[tuple, list] = {}

    def pads_at(net: str, p: Point) -> list:
        key = (net, p)
        if key not in pad_cache:
            out = []
            for (x0, y0, x1, y1), pu, pad in pad_index.get(net, ()):
                if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
                    if len(pad.polygon) > 0:
                        inside = point_in_polygon(p, pad.polygon)
                    else:
                        inside = p == pad.center
                    if inside:
                        out.append((pu, pad))
            pad_cache[key] = out
        return pad_cache[key]

    # ---- nodes
    nodes: dict[tuple, _Node] = {}
    end_node: dict[tuple[str, int], _Node] = {}
    for t in tracks:
        for idx, p in ((0, t.start), (1, t.end)):
            via = None
            for cand in via_index.get((t.net, p), ()):
                if not cand.layers or t.layer in cand.layers:
                    via = cand
                    break
            key = ("via", via.uuid) if via is not None else ("pt", t.net, t.layer, p)
            node = nodes.get(key)
            if node is None:
                node = _Node(key, t.net, p, via, pads_at(t.net, p))
                nodes[key] = node
            node.ends.append((t.uuid, idx))
            end_node[(t.uuid, idx)] = node
    for node in nodes.values():
        deg = len(node.ends)
        if node.pads:
            node.kind = "pad"
        elif deg == 2:
            node.kind = "pass"
        elif node.via is not None:
            node.kind = "via"
        elif deg >= 3:
            node.kind = "junction"
        else:
            node.kind = "free"

    # ---- chains
    chains: list[_Chain] = []
    visited: set[str] = set()
    for node in sorted(nodes.values(), key=lambda n: n.key):
        if node.kind == "pass":
            continue
        for tu, idx in sorted(node.ends):
            if tu in visited:
                continue
            segs, vias = [], []
            cur_tu, cur_idx = tu, idx
            far_node = node
            while True:
                visited.add(cur_tu)
                t = tracks_by[cur_tu]
                segs.append((t, cur_idx == 0))
                far_node = end_node[(cur_tu, 1 - cur_idx)]
                if far_node.kind != "pass":
                    break
                nxt = [e for e in far_node.ends if e != (cur_tu, 1 - cur_idx)]
                if len(nxt) != 1 or nxt[0][0] in visited:
                    break
                if far_node.via is not None:
                    vias.append(far_node.via)
                cur_tu, cur_idx = nxt[0]
            chains.append(_Chain(segs[0][0].net, segs, node, far_node, vias))
    chains.sort(key=lambda c: c.uuids)

    # ---- terminal vectors
    moved_parts = [p for p in sorted(board.parts, key=lambda p: p.uuid) if p.uuid in moves]
    enc_cache: dict[str, tuple[int, int]] = {}

    def enclosure(via) -> tuple[int, int]:
        if via.uuid not in enc_cache:
            vecs = set()
            for part in moved_parts:
                for poly in part.courtyard:
                    if len(poly) >= 3 and point_in_polygon(via.pos, poly):
                        vecs.add(moves[part.uuid])
                        break
            if len(vecs) > 1:
                warnings.add("ambiguous_via_enclosure:" + via.uuid)
            enc_cache[via.uuid] = next(iter(vecs)) if len(vecs) == 1 else ZERO
        return enc_cache[via.uuid]

    moving_vias: set[str] = set()
    via_nodes_chains: dict[tuple, list[_Chain]] = {}
    for c in chains:
        for n in (c.n0, c.n1):
            if n.kind == "via":
                via_nodes_chains.setdefault(n.key, []).append(c)
                if enclosure(n.via) != ZERO:
                    moving_vias.add(n.via.uuid)

    def node_vecs(node: _Node):
        if node.pads:
            return {moves.get(pu, ZERO) for pu, _ in node.pads}
        if node.kind == "free":
            return None
        if node.kind == "via":
            return {enclosure(node.via) if node.via.uuid in moving_vias else ZERO}
        return {ZERO}

    def rigid_vec(c: _Chain):
        union: set = set()
        for n in (c.n0, c.n1):
            vs = node_vecs(n)
            if vs is not None:
                union |= vs
        if len(union) == 1:
            v = next(iter(union))
            return v if v != ZERO else None
        return None

    while True:
        bad = set()
        for key, attached in via_nodes_chains.items():
            via = nodes[key].via
            if via.uuid in moving_vias and any(rigid_vec(c) is None for c in attached):
                bad.add(via.uuid)
        if not bad:
            break
        moving_vias -= bad

    # ---- plans
    def follow(node: _Node):
        """Vector a chain end follows at this node: None, a vector, or 'ambiguous'."""
        if not node.pads:
            return None
        vs = {moves.get(pu, ZERO) for pu, _ in node.pads}
        if vs == {ZERO}:
            return None
        if len(vs) == 1:
            return next(iter(vs))
        return "ambiguous"

    plans: list[_Plan] = []
    chain_len: dict[int, tuple[float, float]] = {}

    def block(plan: _Plan, uuid: str, kind: str = "leg_blocked") -> None:
        plan.blockers.append((uuid, kind))

    for ci, chain in enumerate(chains):
        before = sum(_track_len(t) for t, _ in chain.segs)
        rv = rigid_vec(chain)
        first = chain.uuids[0]
        if rv is not None:
            plan = _Plan(chain, "rigid", _is_tuned(chain))
            for t, _ in chain.segs:
                plan.changes[t.uuid] = TrackChange(t.uuid, _sh(t.start, rv), _sh(t.end, rv))
                if t.mid is not None:
                    warnings.add("arc_translate_unsupported")
                    block(plan, t.uuid)
                if t.locked:
                    warnings.add("locked_track:" + t.uuid)
                    block(plan, t.uuid)
            vias = list(chain.vias)
            for n in (chain.n0, chain.n1):
                if n.kind == "via" and n.via.uuid in moving_vias:
                    vias.append(n.via)
            for via in vias:
                plan.via_moves[via.uuid] = rv
                if via.locked:
                    warnings.add("locked_via:" + via.uuid)
                    block(plan, first)
            after = before if plan.blockers else sum(_change_len(c) for c in plan.changes.values())
            plans.append(plan)
            chain_len[ci] = (before, after)
            plan.chain_index = ci
            continue

        f0, f1 = follow(chain.n0), follow(chain.n1)
        if f0 is None and f1 is None:
            continue
        plan = _Plan(chain, "reaim", _is_tuned(chain))
        plan.chain_index = ci
        plans.append(plan)
        if f0 == "ambiguous" or f1 == "ambiguous":
            warnings.add("ambiguous_pad_terminal")
            block(plan, first)
            chain_len[ci] = (before, before)
            continue
        moved: dict[str, dict[int, tuple[int, int]]] = {}
        first_t, first_fwd = chain.segs[0]
        last_t, last_fwd = chain.segs[-1]
        if f0 is not None:
            moved.setdefault(first_t.uuid, {})[0 if first_fwd else 1] = f0
        if f1 is not None:
            moved.setdefault(last_t.uuid, {})[1 if last_fwd else 0] = f1
        after = 0.0
        for t, _ in chain.segs:
            ends = moved.get(t.uuid)
            if ends is None:
                after += _track_len(t)
                continue
            if t.mid is not None:
                if plan.tuned:
                    block(plan, t.uuid, "tuned_leg")
                else:
                    warnings.add("arc_reaim_unsupported")
                    block(plan, t.uuid)
                after += _track_len(t)
                continue
            ns = _sh(t.start, ends[0]) if 0 in ends else t.start
            ne = _sh(t.end, ends[1]) if 1 in ends else t.end
            anchor = 0 if 0 not in ends else 1
            s, e, bend = _reaim_segment(t.start, t.end, ns, ne, anchor)
            change = TrackChange(t.uuid, s, e, bend)
            if s == e:
                warnings.add("zero_length_segment:" + t.uuid)
            plan.changes[t.uuid] = change
            after += _change_len(change)
            if plan.tuned:
                block(plan, t.uuid, "tuned_leg")
            if t.locked:
                warnings.add("locked_track:" + t.uuid)
                block(plan, t.uuid)
        if plan.tuned and any(kind == "tuned_leg" for _, kind in plan.blockers):
            plan.reason = "stretch" if after - before > _LEN_TOL else "shape change"
        chain_len[ci] = (before, after)

    # ---- vias inside moved pads follow the pad (every chain on that node follows it too)
    for plan in plans:
        for n in (plan.chain.n0, plan.chain.n1):
            if n.pads and n.via is not None:
                f = follow(n)
                if f is not None and f != "ambiguous":
                    plan.via_moves[n.via.uuid] = f
                    if n.via.locked:
                        warnings.add("locked_via:" + n.via.uuid)
                        block(plan, plan.chain.uuids[0])

    # ---- connectivity invariant: nothing may be left detached
    pad_world: dict[str, tuple] = {}
    for part in board.parts:
        v = moves.get(part.uuid, ZERO)
        for pad in part.pads:
            pts = pad.polygon if len(pad.polygon) > 0 else (pad.center,)
            pad_world[part.uuid + "\0" + pad.uuid] = tuple(_sh(p, v) for p in pts)
    all_via_moves: dict[str, tuple[int, int]] = {}
    for plan in plans:
        for u, v in plan.via_moves.items():
            all_via_moves[u] = v

    def attached_ok(plan: _Plan) -> bool:
        chain = plan.chain
        pts = []
        for t, fwd in chain.segs:
            c = plan.changes.get(t.uuid)
            s, e = (c.start, c.end) if c is not None else (t.start, t.end)
            pts.append((s, e) if fwd else (e, s))
        for (_, a_end), (b_start, _) in zip(pts, pts[1:]):
            if a_end != b_start:
                return False
        for node, point in ((chain.n0, pts[0][0]), (chain.n1, pts[-1][1])):
            if node.pads:
                if not any(
                    point_in_polygon(point, pad_world[pu + "\0" + pad.uuid])
                    for pu, pad in node.pads
                ):
                    return False
            elif node.kind == "via":
                mv = all_via_moves.get(node.via.uuid, ZERO)
                if point != _sh(node.via.pos, mv):
                    return False
            elif node.kind == "free":
                if plan.kind == "rigid":
                    expected = _sh(node.point, _rigid_shift(plan))
                else:
                    expected = node.point
                if point != expected:
                    return False
            elif point != node.point:
                return False
        return True

    def _rigid_shift(plan: _Plan):
        t, _ = plan.chain.segs[0]
        c = plan.changes[t.uuid]
        return (c.start[0] - t.start[0], c.start[1] - t.start[1])

    for plan in plans:
        if plan.emit and not attached_ok(plan):
            warnings.add("connectivity_invariant_failed:" + plan.chain.uuids[0])
            block(plan, plan.chain.uuids[0])

    # ---- rule checks on what would be emitted
    geom: dict[str, list] = {}
    for plan in plans:
        if plan.emit:
            for u, c in plan.changes.items():
                geom[u] = _pieces_of_change(c)
    emitted_via_moves: dict[str, tuple[int, int]] = {}
    for plan in plans:
        if plan.emit:
            emitted_via_moves.update(plan.via_moves)
    via_pos: dict[str, Point] = {
        via.uuid: _sh(via.pos, emitted_via_moves.get(via.uuid, ZERO)) for via in board.vias
    }
    pads_world = []
    for part in board.parts:
        for pad in part.pads:
            poly = pad_world[part.uuid + "\0" + pad.uuid]
            pads_world.append((pad.net, poly, bbox(poly)))
    track_items = [
        (t.uuid, t.net, t.layer, geom.get(t.uuid) or _pieces_of_track(t), t.width)
        for t in tracks
    ]

    outline = tuple(p for p in board.outline.polygons if len(p) > 0)

    def clearance(net_a: str, net_b: str):
        c = rules.clearance(net_a, net_b)
        if c is None:
            warnings.add("missing_rule:clearance")
        return c

    def edge_ok(inner, half: float) -> bool:
        ec = rules.copper_edge_clearance_nm
        if ec is None:
            warnings.add("missing_rule:copper_edge_clearance")
            return True
        if not outline:
            warnings.add("missing_outline")
            return True
        return region_contains(outline, inner, ec + int(math.ceil(half)))

    def layer_hit(item_layers, layer: str) -> bool:
        return not item_layers or layer in item_layers

    def segment_violates(t: Track, a: Point, b: Point) -> bool:
        hw = t.width / 2
        for u2, n2, l2, pieces, w2 in track_items:
            if u2 == t.uuid or n2 == t.net or l2 != t.layer:
                continue
            clr = clearance(t.net, n2)
            if clr is None:
                continue
            need = clr + hw + w2 / 2
            for c, d in pieces:
                if _seg_far(a, b, c, d, need):
                    continue
                if segment_segment_distance(a, b, c, d) + _EPS < need:
                    return True
        for via in board.vias:
            if via.net == t.net or not layer_hit(via.layers, t.layer):
                continue
            clr = clearance(t.net, via.net)
            if clr is None:
                continue
            need = clr + hw + via.diameter / 2
            pos = via_pos[via.uuid]
            if _seg_far(a, b, pos, pos, need):
                continue
            if point_segment_distance(pos, a, b) + _EPS < need:
                return True
        for net, poly, (x0, y0, x1, y1) in pads_world:
            if net == t.net:
                continue
            clr = clearance(t.net, net)
            if clr is None:
                continue
            need = clr + hw
            if _seg_far(a, b, (x0, y0), (x1, y1), need):
                continue
            if segment_polygon_distance(a, b, poly) + _EPS < need:
                return True
        for k in board.keepouts:
            if k.no_tracks and layer_hit(k.layers, t.layer) and len(k.polygon) > 0:
                if segment_polygon_distance(a, b, k.polygon) + _EPS < hw:
                    return True
        return not edge_ok((a, b), hw)

    def via_violates(via, pos: Point) -> bool:
        r = via.diameter / 2
        for u2, n2, l2, pieces, w2 in track_items:
            if n2 == via.net or not layer_hit(via.layers, l2):
                continue
            clr = clearance(via.net, n2)
            if clr is None:
                continue
            need = clr + r + w2 / 2
            for c, d in pieces:
                if _seg_far(pos, pos, c, d, need):
                    continue
                if point_segment_distance(pos, c, d) + _EPS < need:
                    return True
        for other in board.vias:
            if other.uuid == via.uuid or other.net == via.net:
                continue
            clr = clearance(via.net, other.net)
            if clr is None:
                continue
            if _len(pos, via_pos[other.uuid]) + _EPS < clr + r + other.diameter / 2:
                return True
        for net, poly, (x0, y0, x1, y1) in pads_world:
            if net == via.net:
                continue
            clr = clearance(via.net, net)
            if clr is None:
                continue
            if segment_polygon_distance(pos, pos, poly) + _EPS < clr + r:
                return True
        for k in board.keepouts:
            if k.no_vias and len(k.polygon) > 0:
                if not k.layers or not via.layers or set(k.layers) & set(via.layers):
                    if segment_polygon_distance(pos, pos, k.polygon) + _EPS < r:
                        return True
        return not edge_ok((pos,), r)

    vias_by = {v.uuid: v for v in board.vias}
    for plan in plans:
        if not plan.emit:
            continue
        failed = False
        for t, _ in plan.chain.segs:
            c = plan.changes.get(t.uuid)
            if c is None:
                continue
            if any(segment_violates(t, a, b) for a, b in _pieces_of_change(c)):
                block(plan, t.uuid)
                failed = True
        for vu in sorted(plan.via_moves):
            via = vias_by.get(vu)
            if via is not None and via_violates(via, via_pos[vu]):
                block(plan, plan.chain.uuids[0])
                failed = True
        plan.rule_blocked = failed

    # ---- assemble
    changes: dict[str, TrackChange] = {}
    via_moves: dict[str, tuple[int, int]] = {}
    blockers: set[tuple[str, str]] = set()
    legs = []
    for plan in plans:
        if plan.emit or plan.rule_blocked:
            changes.update(plan.changes)
            via_moves.update(plan.via_moves)
        blockers.update(plan.blockers)
        before, after = chain_len[plan.chain_index]
        leg = {
            "net": plan.chain.net,
            "track_uuids": plan.chain.uuids,
            "kind": "blocked" if plan.blockers else plan.kind,
            "length_before_nm": int(round(before)),
            "length_after_nm": int(round(after)),
            "tuned": plan.tuned,
        }
        if plan.reason is not None:
            leg["reason"] = plan.reason
        legs.append(leg)
    legs.sort(key=lambda d: d["track_uuids"])
    return CopperResult(
        ok=not blockers,
        track_changes=tuple(changes[u] for u in sorted(changes)),
        via_moves=tuple(ViaMove(u, v[0], v[1]) for u, v in sorted(via_moves.items())),
        blockers=tuple(Blocker(u, k, None) for u, k in sorted(blockers)),
        legs=tuple(legs),
        warnings=tuple(sorted(warnings)),
    )


# ----------------------------------------------------------------------------- helpers for tests


def apply_changes(board: Board, result: CopperResult) -> Board:
    """New board with the track changes and via moves applied.

    A bent change keeps the original uuid for the first half (start -> bend) and gives the second
    half (bend -> end) the derived uuid ``uuid + '#b'``. Nothing is ever removed; width, layer,
    net, lock and group are copied from the original track.
    """
    by_uuid = {c.uuid: c for c in result.track_changes}
    for u in by_uuid:
        board.track(u)  # KeyError for an unknown uuid
    tracks = []
    for t in board.tracks:
        c = by_uuid.get(t.uuid)
        if c is None:
            tracks.append(t)
        elif c.bend is None:
            tracks.append(dataclasses.replace(t, start=c.start, end=c.end))
        else:
            tracks.append(dataclasses.replace(t, start=c.start, end=c.bend))
            tracks.append(
                dataclasses.replace(t, uuid=t.uuid + "#b", start=c.bend, end=c.end)
            )
    vmoves = {m.uuid: m for m in result.via_moves}
    vias = []
    for v in board.vias:
        m = vmoves.get(v.uuid)
        vias.append(v if m is None else dataclasses.replace(v, pos=_sh(v.pos, (m.dx, m.dy))))
    return dataclasses.replace(board, tracks=tuple(tracks), vias=tuple(vias))


def connectivity_signature(board: Board) -> frozenset:
    """Per-net connectivity partition: ``frozenset((net, sorted item labels))``.

    Items are ``track:<uuid>`` (a ``#b`` split suffix is stripped), ``via:<uuid>`` and
    ``pad:<part>/<pad>``. Two items are joined when same-net track ends coincide on one layer,
    a via sits on a track end on one of its layers, or a track end lies inside a same-net pad.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    net_of: dict[str, str] = {}
    pads = []
    for part in board.parts:
        for pad in part.pads:
            label = "pad:%s/%s" % (part.uuid, pad.uuid)
            net_of[label] = pad.net
            find(label)
            pads.append((label, pad))
    for via in board.vias:
        label = "via:" + via.uuid
        net_of[label] = via.net
        find(label)
    ends: dict[tuple, str] = {}
    for t in board.tracks:
        label = "track:" + t.uuid.split("#b")[0]
        net_of[label] = t.net
        find(label)
        for p in (t.start, t.end):
            key = (t.net, t.layer, p)
            if key in ends:
                union(ends[key], label)
            else:
                ends[key] = label
            for via in board.vias:
                if via.net == t.net and via.pos == p and (not via.layers or t.layer in via.layers):
                    union("via:" + via.uuid, label)
            for plabel, pad in pads:
                if pad.net != t.net:
                    continue
                if len(pad.polygon) > 0:
                    inside = point_in_polygon(p, pad.polygon)
                else:
                    inside = p == pad.center
                if inside:
                    union(plabel, label)
    groups: dict[str, list[str]] = {}
    for label in net_of:
        groups.setdefault(find(label), []).append(label)
    return frozenset(
        (net_of[items[0]], tuple(sorted(items))) for items in groups.values()
    )
