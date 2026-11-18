"""Cascade / explicit / freeze / priority / eligibility / stop-at-limit solver and edge drag.

Pure Python, integer nanometres, no pcbnew import. Entry points: ``solve`` (part drivers; edge
drivers are forwarded), ``solve_edge`` and the ``SolveResult`` dataclass.

Interpretations (the spec leaves these open)
--------------------------------------------
* Distances along the displacement are integers. ``requested_nm = round(|d|)``; a scale of ``L`` nm
  moves the driver by ``d * L / requested_nm`` (rounded to nm; exactly ``d`` at the full scale).
  Pushed parts move along ``d`` only, by ``L_i <= L`` in the same units.
* Courtyard clearance is exactly ``rules.courtyard_clearance_nm`` (default 0); it is never rounded
  up. With ``c == 0`` a violation is a strict interior overlap and exactly touching is legal. With
  ``c > 0`` a violation is ``distance < c`` (strict) and a pair exactly ``c`` apart is legal.
  Keepouts ("no footprints") and courtyard-versus-copper use the same strict-overlap test
  (clearance 0). Pad-to-copper uses the board's clearance rule for the (pad net, copper net) pair.
* Rules are never invented. If a rule the solve needs is missing (pad-to-copper clearance in
  ``copper == 'fixed'`` mode, any net-pair clearance when copper exists in ``'adapt'`` mode, or the
  copper-to-edge clearance when the board has an outline) the result is ``status == 'rejected'``
  with ``reject_reason == 'missing_rule'`` and the warning ``missing_rule:<name>`` (``clearance``
  or ``copper_edge_clearance``); nothing is solved.
* No exemption for pre-existing violations. Every start-state violation is reported as the warning
  ``pre_existing_violation:<kind>:<uuid>[:<uuid>]`` (kinds ``courtyard``, ``keepout``, ``copper``,
  ``edge``; ``SolveResult.pre_existing`` holds the same as ``{"kind", "uuids"}`` dicts). The rule is
  that a move may never make one worse, and a mover never passes through its obstacle:
  - distance violations (``0 < d0 < c``): the swept path may never come closer than the start;
  - overlaps (and touching when ``c > 0``): along the motion axis the overlap is an interval
    ``(a, b)`` around the start; the penetration depth ``min(t - a, b - t)`` may not increase, so
    the mover may only advance when it is at least as close to the exit ahead as to the one behind
    (moving away is legal) and it may not enter a second overlap afterwards;
  - board edge: exact, see "Board edge" below (overlap area per piece of the forbidden region and
    distance to every full boundary edge may not grow; ``edge_ok`` end position, ``edge_path_ok``
    whole path); edge-drag seeding pushes such parts just far enough that this holds.
  Movable parts that already overlap are pushed only if the cascade requires it and always end up
  no deeper in each other; the pair test uses the RELATIVE displacement of the two parts, so two
  parts that move by the same amount keep their relative state.
* Wave: a mover is any part with a positive displacement. A permitted part is pushed when the
  relative motion of a mover (path 0..L_mover against the part at its original position, moved by
  its own L) violates the rule above. The push is the minimum integer ``L_q`` that makes the pair
  legal (bisection against the exact test, never bounding boxes). A pushed part becomes a mover
  itself (a chain, not a broadcast).
* Order: among all pending contacts the next one processed is chosen by
  ``(contact position along d, not priority-listed, uuid)``. The contact position is the driver
  displacement (rounded to nm) at which the contact happens, so the nearest contact is always
  processed first. Priority (spec section 3) only breaks ties between contacts at the SAME
  contact distance; the uuid string breaks the remaining ties. ``SolveResult.wave`` lists the
  pushed parts in that order.
* Obstacles: frozen (request), locked (native), contract-fixed, explicit-mode non-movers (kind
  ``frozen``) and parts beyond ``eligibility_nm`` (kind ``ineligible``; distance is
  ``polygon_distance`` from the driver courtyard, or from the edge segment for edge drivers).
  Precedence frozen > locked > contract > explicit-non-mover > ineligible. A contract-fixed part
  stays an obstacle in every mode (the contract is never widened).
* Hard checks on every moved part: obstacle parts (swept courtyard, kind of the obstacle);
  keepouts with ``no_footprints`` (kind ``keepout``); the board edge (every pad polygon inside the
  outline minus ``copper_edge_clearance_nm`` along the whole path, or exactly not worse than at
  the start, kind ``edge`` with the part uuid); and, with ``copper == 'fixed'``, all tracks and vias (courtyard and pads, layers
  ignored, kind ``copper`` with the track/via uuid). Copper attached to the part itself (a track
  end or via inside one of its pads on the same net) is not an obstacle to its own courtyard;
  instead the part may not move to a place where that end is no longer inside the pad: the move is
  blocked with kind ``copper`` and the uuid of the track (or via). ``at_nm`` is the driver
  displacement at which the contact occurs.
* Board edge (exact for every part, pre-violating or not; no vertex depths, no path sampling).
  The forbidden region F is the outside of the outer outline plus every hole, grown by the
  copper-to-edge clearance ``m``; a pad is legal when ``geometry.region_contains`` accepts it, i.e.
  every full boundary edge is at least ``m`` away and no cutout or notch is swallowed. A pad that
  violates this at the start has an exact start state against the ORIGINAL outline: its overlap
  area with each connected piece of F (``forbidden_overlap_areas``: the outside, hole i) and its
  squared distance ``d0`` to every full outline/hole edge (Fractions, never vertices).
  - End position (``edge_ok``, against the dragged outline for an edge drag): every pad is legal,
    or its overlap area with each piece of F is at most the start area and every boundary edge is
    at least ``min(d0, m)`` away (``d0`` of the same edge at the start). For a pad that was legal
    at the start ``min(d0, m) == m`` and every start area is zero, so this is just "legal".
  - Path (``edge_path_ok``), swept against the ORIGINAL outline: (i) the swept distance between
    the pad and every edge stays at least ``min(d0, m)`` (``swept_polygon_distance2``, exact);
    (ii) no point of the pad that is outside a piece of F at the start enters it along the path:
    a point enters through edge e exactly when it lies in the parallelogram swept by e moving by
    ``-v``, so ``forbidden_entry_area`` (exact slab areas, touching is legal) must be zero. A
    notch, slot or cutout cannot be jumped or swallowed, a piece the pad did not touch is never
    touched, a pad that overlaps F may retreat from it or slide along it but never go deeper.
  Both conditions only get harder with a longer path (a sub-path of a legal path is legal), so the
  edge blocker's ``at_nm`` (bisection) and the stop-at-limit search agree: the last legal
  position is the contact with the wall (``m`` away from it, or touching it at ``m == 0``). The
  existing violation never exempts a pad from the rule: it only sets the bound it may not exceed.
* Stop-at-limit: the full displacement is evaluated first; if illegal and ``allow_partial`` the
  scale is stepped in ``max(10000, requested/200)`` nm steps, stopping at the FIRST illegal step
  (nothing beyond it is reached), then bisected to integer nm inside the last interval only. The
  blockers reported are those of the first illegal step. ``copper == 'adapt'`` calls
  ``_adapt_copper_hook`` after each legal candidate.
* Edge drag: KiCad coordinates (y down): ``left`` = -x, ``right`` = +x, ``top`` = -y,
  ``bottom`` = +y outward normal. The normal displacement is ``[dx, dy] . outward_normal``
  (positive grows, negative shrinks); when that projection is 0, ``dy == 0`` and ``dx != 0`` (the
  scalar form for top/bottom edges) ``dx`` itself is the signed displacement along the outward
  normal. Topology is validated at the full displacement and rejected, never partially applied.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from fractions import Fraction

from . import geometry as geo
from .model import (
    Blocker,
    Board,
    ChangeSet,
    EdgeChange,
    PartMove,
    Report,
    Request,
    RequestError,
)

__all__ = ["SolveResult", "solve", "solve_edge"]

_STEP_MIN_NM = 10000
_EDGE_NORMALS = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


@dataclass(frozen=True)
class SolveResult:
    changeset: ChangeSet
    report: Report
    wave: tuple = field(default=())  # pushed part uuids in wave (contact) order
    pre_existing: tuple = field(default=())  # start-state violations as {"kind", "uuids"} dicts


class _Reject(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _adapt_copper_hook(board, part_moves, allow_partial_hint=True):
    """Single seam to copper.py (imported lazily so the solver is testable standalone)."""
    from .copper import adapt_copper

    return adapt_copper(board, part_moves, allow_partial_hint)


# ----------------------------------------------------------------------------- small helpers


def _rdiv(a: int, b: int) -> int:
    return (2 * a + b) // (2 * b)


def _far(ba, v, bb, c) -> bool:
    """True when the swept bbox of ``ba`` (moved by v) cannot come within c of ``bb``."""
    return (
        max(ba[2], ba[2] + v[0]) + c <= bb[0]
        or bb[2] + c <= min(ba[0], ba[0] + v[0])
        or max(ba[3], ba[3] + v[1]) + c <= bb[1]
        or bb[3] + c <= min(ba[1], ba[1] + v[1])
    )


def _boxes_far(a, b, reach) -> bool:
    return a[2] + reach <= b[0] or b[2] + reach <= a[0] or a[3] + reach <= b[1] or b[3] + reach <= a[1]


def _union_box(polys):
    boxes = [geo.bbox(p) for p in polys if p]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _sgn(x) -> int:
    return (x > 0) - (x < 0)


def _rect(poly):
    """``(x0, y0, x1, y1)`` when ``poly`` is an axis-aligned rectangle, else None."""
    if len(poly) != 4:
        return None
    pts = [tuple(p) for p in poly]
    if len(set(pts)) != 4:
        return None
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        if (a[0] == b[0]) == (a[1] == b[1]):
            return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def _div(n, d):
    """Exact n/d: a plain int when it divides (fast integer geometry), else a Fraction."""
    if type(n) is int and n % d == 0:
        return n // d
    return Fraction(n, d)


def _all_int(poly) -> bool:
    return all(type(c) is int for pt in poly for c in pt)


def _scaled(poly, k):
    return tuple((pt[0] * k, pt[1] * k) for pt in poly)


def _boundary_probes(poly):
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        yield a
        yield (_div(a[0] + b[0], 2), _div(a[1] + b[1], 2))


def _interior_probes(poly):
    n = len(poly)
    if n <= 16:
        for i in range(n):
            for j in range(i + 2, n):
                if i == 0 and j == n - 1:
                    continue
                a, b = poly[i], poly[j]
                yield (_div(a[0] + b[0], 2), _div(a[1] + b[1], 2))
    for i in range(n):
        a, b, c = poly[i], poly[(i + 1) % n], poly[(i + 2) % n]
        yield (_div(a[0] + b[0] + c[0], 3), _div(a[1] + b[1] + c[1], 3))


def _strict_overlap(p, q) -> bool:
    """True when the interiors of the filled polygons share a point (touching is NOT overlap).

    Exact for int and Fraction coordinates. Axis-aligned rectangles take a closed-form path; other
    polygons are tested by proper edge crossings plus probe points strictly inside both shapes.
    """
    if not p or not q:
        return False
    rp, rq = _rect(p), _rect(q)
    if rp and rq:
        return rp[0] < rq[2] and rq[0] < rp[2] and rp[1] < rq[3] and rq[1] < rp[3]
    bp, bq = geo.bbox(p), geo.bbox(q)
    if bp[2] <= bq[0] or bq[2] <= bp[0] or bp[3] <= bq[1] or bq[3] <= bp[1]:
        return False
    if _all_int(p) and _all_int(q):
        # scale by 6 so every probe point (halves, thirds) is an exact int: integer predicates are
        # much faster than Fractions and overlap is scale invariant
        p, q = _scaled(p, 6), _scaled(q, 6)
    for a, b in geo.polygon_edges(p):
        for c, d in geo.polygon_edges(q):
            if (
                _sgn(geo.cross(c, d, a)) * _sgn(geo.cross(c, d, b)) < 0
                and _sgn(geo.cross(a, b, c)) * _sgn(geo.cross(a, b, d)) < 0
            ):
                return True
    for pt in _boundary_probes(p):
        if geo.point_in_polygon(pt, q, False):
            return True
    for pt in _boundary_probes(q):
        if geo.point_in_polygon(pt, p, False):
            return True
    for pt in _interior_probes(p):
        if geo.point_in_polygon(pt, p, False) and geo.point_in_polygon(pt, q, False):
            return True
    for pt in _interior_probes(q):
        if geo.point_in_polygon(pt, p, False) and geo.point_in_polygon(pt, q, False):
            return True
    return False


def _line_times(p, w, c, d, ts) -> None:
    """Add the t where the point ``p + t*w`` crosses the supporting line of edge cd."""
    ex, ey = d[0] - c[0], d[1] - c[1]
    den = ex * w[1] - ey * w[0]
    if den:
        ts.add(Fraction(-(ex * (p[1] - c[1]) - ey * (p[0] - c[0])), den))


def _general_intervals(a, v, b, window):
    """Open t-intervals (Fractions) where ``a + t*v`` strictly overlaps ``b``; any polygons."""
    ts: set = set()
    for p in a:
        for c, d in geo.polygon_edges(b):
            _line_times(p, v, c, d, ts)
    nv = (-v[0], -v[1])
    for q in b:
        for c, d in geo.polygon_edges(a):
            _line_times(q, nv, c, d, ts)
    if window is not None:
        ts = {t for t in ts if window[0] <= t <= window[1]} | {window[0], window[1]}
    times = sorted(ts)

    int_inputs = _all_int(a) and _all_int(b) and type(v[0]) is int and type(v[1]) is int
    b_scaled: dict = {}

    def over(t) -> bool:
        if int_inputs:
            # common denominator: moved and b become integer polygons (overlap is scale invariant)
            t = Fraction(t)
            num, den = t.numerator, t.denominator
            bs = b_scaled.get(den)
            if bs is None:
                bs = b_scaled[den] = _scaled(b, den)
            moved = tuple((p[0] * den + num * v[0], p[1] * den + num * v[1]) for p in a)
            return _strict_overlap(moved, bs)
        moved = tuple((p[0] + t * v[0], p[1] + t * v[1]) for p in a)
        return _strict_overlap(moved, b)

    out: list = []
    for lo, hi in zip(times, times[1:]):
        if over((lo + hi) / 2):
            if out and out[-1][1] == lo and over(lo):
                out[-1] = (out[-1][0], hi)
            else:
                out.append((lo, hi))
    return out


def _overlap_intervals(a, v, b, whole_line):
    """Open t-intervals where ``a + t*v`` strictly overlaps ``b`` (``v != (0, 0)``)."""
    ra, rb = _rect(a), _rect(b)
    if ra and rb:
        lo = hi = None
        for axis in (0, 1):
            a0, a1, b0, b1, w = ra[axis], ra[axis + 2], rb[axis], rb[axis + 2], v[axis]
            if w == 0:
                if not (a0 < b1 and a1 > b0):
                    return []
                continue
            t0, t1 = Fraction(b0 - a1, w), Fraction(b1 - a0, w)
            if t0 > t1:
                t0, t1 = t1, t0
            lo = t0 if lo is None else max(lo, t0)
            hi = t1 if hi is None else min(hi, t1)
        if lo is None or lo >= hi:
            return []
        return [(lo, hi)]
    return _general_intervals(a, v, b, None if whole_line else (Fraction(0), Fraction(1)))


def _violation(a, v, b, c):
    """None when sliding polygon ``a`` by ``t*v`` (t in [0, 1]) past ``b`` is legal, else the first
    t (float) at which it goes wrong.

    Legal means: no violation along the path (strict overlap when ``c == 0``; strict
    ``distance < c`` when ``c > 0``) or, when the pair already violates at t = 0, the violation never
    gets worse (see the module docstring). ``v`` is the RELATIVE displacement of ``a`` versus ``b``.
    """
    if v == (0, 0) or not a or not b:
        return None
    if _far(geo.bbox(a), v, geo.bbox(b), c):
        return None
    ov0 = _strict_overlap(a, b)
    if c > 0 and not ov0:
        d0 = geo.polygon_distance2(a, b)
        if d0 > 0:
            if geo.swept_polygon_distance2(a, v, b) >= min(d0, Fraction(c * c)):
                return None
            f = geo.first_contact_fraction(a, v, b, c)
            return 0.0 if f is None else f
    if not ov0 and not (_rect(a) and _rect(b)) and geo.swept_polygon_distance2(a, v, b) > 0:
        return None
    ivs = _overlap_intervals(a, v, b, ov0)
    if ov0:
        cur = next((iv for iv in ivs if iv[0] < 0 < iv[1]), None)
        if cur is None or -cur[0] < cur[1]:
            return 0.0  # nearer the entrance than the exit: any advance would go deeper
        later = [lo for lo, _ in ivs if lo >= cur[1] and lo < 1]
        return float(min(later)) if later else None
    hits = [max(lo, 0) for lo, hi in ivs if hi > 0 and lo < 1]
    return float(min(hits)) if hits else None


def _hit(mp, v, qp, c) -> bool:
    """Some polygon pair of (mp, qp) is illegal for the relative displacement v."""
    for a in mp:
        for b in qp:
            if _violation(a, v, b, c) is not None:
                return True
    return False


def _first_contact(mp, v, qp, c):
    """Earliest fraction of v at which some pair is illegal, or None when all are legal."""
    best = None
    for a in mp:
        for b in qp:
            f = _violation(a, v, b, c)
            if f is not None and (best is None or f < best):
                best = f
    return best


def _violating(a, b, c) -> bool:
    """Static test: the two polygons violate clearance ``c`` right now."""
    if not a or not b:
        return False
    if _strict_overlap(a, b):
        return True
    if c <= 0:
        return False
    if _far(geo.bbox(a), (0, 0), geo.bbox(b), c):
        return False
    return geo.polygon_distance2(a, b) < Fraction(c * c)


def _at(f, lp: int, total: int) -> int:
    """Driver displacement at which a mover (moved lp of total) reaches fraction f of its path."""
    return (total - lp) + int(round((f or 0.0) * lp))


def _dedupe(blockers):
    best = {}
    for b in blockers:
        key = (b.uuid, b.kind)
        if key not in best or (b.at_nm or 0) < (best[key].at_nm or 0):
            best[key] = b
    return sorted(best.values(), key=lambda b: (b.at_nm is None, b.at_nm or 0, b.uuid or "", b.kind))


def _seg_poly(a, b, w):
    h = (w + 1) // 2
    dx, dy = b[0] - a[0], b[1] - a[1]
    ln = math.hypot(dx, dy)
    if ln == 0:
        return ((a[0] - h, a[1] - h), (a[0] + h, a[1] - h), (a[0] + h, a[1] + h), (a[0] - h, a[1] + h))
    ox, oy = int(round(-dy / ln * h)), int(round(dx / ln * h))
    ex, ey = int(round(dx / ln * h)), int(round(dy / ln * h))
    return (
        (a[0] - ex + ox, a[1] - ey + oy),
        (b[0] + ex + ox, b[1] + ey + oy),
        (b[0] + ex - ox, b[1] + ey - oy),
        (a[0] - ex - ox, a[1] - ey - oy),
    )


def _via_poly(p, d):
    r = (d + 1) // 2
    k = int(round(r * 0.41421356))
    return (
        (p[0] - k, p[1] - r), (p[0] + k, p[1] - r), (p[0] + r, p[1] - k), (p[0] + r, p[1] + k),
        (p[0] + k, p[1] + r), (p[0] - k, p[1] + r), (p[0] - r, p[1] + k), (p[0] - r, p[1] - k),
    )


def _copper_obstacles(board):
    out = []
    for t in board.tracks:
        if t.mid is None:
            out.append((t.uuid, t.net, _seg_poly(t.start, t.end, t.width)))
        else:
            out.append((t.uuid, t.net, _seg_poly(t.start, t.mid, t.width)))
            out.append((t.uuid, t.net, _seg_poly(t.mid, t.end, t.width)))
    for v in board.vias:
        out.append((v.uuid, v.net, _via_poly(v.pos, v.diameter)))
    return sorted(out, key=lambda o: o[0])


class _Motion:
    """Maps an integer distance along the displacement to a translation vector."""

    def __init__(self, dx: int, dy: int, unit: bool = False) -> None:
        self.dx, self.dy, self.unit = dx, dy, unit
        self.n = 1 if unit else max(1, int(round(math.hypot(dx, dy))))

    def vec(self, dist: int):
        if dist <= 0:
            return (0, 0)
        if self.unit:
            return (self.dx * dist, self.dy * dist)
        if dist >= self.n:
            return (self.dx, self.dy)
        return (_rdiv(self.dx * dist, self.n), _rdiv(self.dy * dist, self.n))


@dataclass
class _Eval:
    legal: bool
    disp: dict
    order: list
    blockers: list
    copper: object = None


# ----------------------------------------------------------------------------- context


class _Ctx:
    def __init__(self, board: Board, req: Request, motion: _Motion, drivers, edge_seg=None):
        self.board, self.req, self.m = board, req, motion
        self.drivers = tuple(drivers)
        rules = board.rules
        self.c = max(int(rules.courtyard_clearance_nm or 0), 0)
        self.parts = {p.uuid: p for p in board.parts}
        self.prio = set(req.priority)
        self.warnings: set[str] = set()
        self.missing: set[str] = set()
        self.pre_report: tuple = ()
        self.cy = {}
        for u in sorted(self.parts):
            polys, missing = self.parts[u].effective_courtyard(rules)
            self.cy[u] = tuple(p for p in polys if len(p) > 0)
            if missing:
                self.warnings.add(f"missing_courtyard:{u}")
        self.margin = rules.copper_edge_clearance_nm
        if board.outline.polygons and self.margin is None:
            self.missing.add("copper_edge_clearance")
        self.margin = self.margin or 0
        self.old_polys = board.outline.polygons
        self.copper_obs = _copper_obstacles(board) if req.copper == "fixed" else []
        self._edge_start: dict = {}
        self.attached: dict = {}
        self.attached_ids: dict = {}
        self.kind = {}
        for u in sorted(self.parts):
            if u in self.drivers:
                continue
            k = self._obstacle_kind(u, edge_seg)
            if k:
                self.kind[u] = k
        self._find_missing_rules()
        if not self.missing:
            self._find_attached()
            self._scan_pre_existing()

    def missing_warnings(self) -> tuple:
        return tuple(f"missing_rule:{n}" for n in sorted(self.missing))

    # -- rules ---------------------------------------------------------------------------------
    def _find_missing_rules(self) -> None:
        rules, board = self.board.rules, self.board
        cnets = {t.net for t in board.tracks} | {v.net for v in board.vias}
        if not cnets:
            return
        if self.req.copper == "fixed":
            for u in sorted(self.parts):
                if u in self.kind:
                    continue
                for pad in self.parts[u].pads:
                    if not pad.polygon:
                        continue
                    for cn in sorted(cnets):
                        if pad.net and pad.net == cn:
                            continue
                        if rules.clearance(pad.net, cn) is None:
                            self.missing.add("clearance")
                            return
        else:
            nets = cnets | {pad.net for part in board.parts for pad in part.pads if pad.net}
            for a in sorted(cnets):
                for n in sorted(nets):
                    if a != n and rules.clearance(a, n) is None:
                        self.missing.add("clearance")
                        return

    # -- permissions -------------------------------------------------------------------------
    def _distance_to_driver(self, u, edge_seg) -> float:
        polys = self.cy[u]
        if not polys:
            return float("inf")
        if edge_seg is not None:
            return min(geo.segment_polygon_distance(edge_seg.start, edge_seg.end, p) for p in polys)
        best = float("inf")
        for d in self.drivers:
            for a in self.cy[d]:
                for b in polys:
                    best = min(best, geo.polygon_distance(a, b))
        return best

    def _obstacle_kind(self, u, edge_seg):
        req, part = self.req, self.parts[u]
        if u in req.frozen:
            return "frozen"
        if part.locked:
            return "locked"
        contract = self.board.contract
        if contract is not None and contract.permissions.get(u) == "fixed":
            return "contract"
        if req.mode == "explicit" and u not in req.movers:
            return "frozen"
        if req.eligibility_nm is not None and self._distance_to_driver(u, edge_seg) > req.eligibility_nm:
            return "ineligible"
        return None

    def driver_kind(self, u):
        if u in self.req.frozen:
            return "frozen"
        if self.parts[u].locked:
            return "locked"
        contract = self.board.contract
        if contract is not None and contract.permissions.get(u) == "fixed":
            return "contract"
        return None

    # -- copper attached to a part (copper == 'fixed') -----------------------------------------
    def _find_attached(self) -> None:
        if self.req.copper != "fixed":
            return
        ends: dict = {}
        for t in self.board.tracks:
            for pt in (t.start, t.end):
                ends.setdefault(t.net, []).append((t.uuid, pt))
        for via in self.board.vias:
            ends.setdefault(via.net, []).append((via.uuid, via.pos))
        for u in sorted(self.parts):
            items = []
            for pad in self.parts[u].pads:
                if not pad.polygon or not pad.net:
                    continue
                for cu, pt in ends.get(pad.net, ()):
                    if geo.point_in_polygon(pt, pad.polygon):
                        items.append((cu, pt, pad.polygon))
            if items:
                self.attached[u] = items
                self.attached_ids[u] = {cu for cu, _, _ in items}

    def _pad_holds(self, pt, poly, dist) -> bool:
        v = self.m.vec(dist)
        return geo.point_in_polygon(pt, geo.polygon_translate(poly, v[0], v[1]))

    def attached_blockers(self, p, lp, total):
        out = []
        for cu, pt, poly in self.attached.get(p, ()):
            if self._pad_holds(pt, poly, lp):
                continue
            lo, hi = 0, lp
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if self._pad_holds(pt, poly, mid):
                    lo = mid
                else:
                    hi = mid
            out.append(Blocker(cu, "copper", (total - lp) + lo))
        return out

    def share(self, a, b) -> bool:
        """Courtyards only interact when the parts can physically meet: same board side, or either part
        has through-hole pads (it occupies both sides). Top and bottom SMD parts overlap freely."""
        pa, pb = self.parts[a], self.parts[b]
        return pa.side == pb.side or pa.through or pb.through

    # -- pre-existing violations (reported, never exempted) -----------------------------------
    def _scan_pre_existing(self) -> None:
        found: set = set()
        c = self.c
        rules = self.board.rules
        ids = [u for u in sorted(self.cy) if self.cy[u]]
        cybox = {u: _union_box(self.cy[u]) for u in ids}
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                if _boxes_far(cybox[a], cybox[b], c) or not self.share(a, b):
                    continue
                if any(_violating(p, q, c) for p in self.cy[a] for q in self.cy[b]):
                    found.add(("courtyard", a, b))
        for u in ids:
            for k in self.board.keepouts:
                if k.no_footprints and k.polygon and any(_strict_overlap(p, k.polygon) for p in self.cy[u]):
                    found.add(("keepout", u, k.uuid))
        if self.copper_obs:
            reach = max([c, rules.clearance_nm or 0] + list(rules.pair_clearance.values()))
            cboxes = [geo.bbox(poly) for _, _, poly in self.copper_obs]
            for u in sorted(self.parts):
                part = self.parts[u]
                pbox = _union_box(list(self.cy[u]) + [pad.polygon for pad in part.pads if pad.polygon])
                if pbox is None:
                    continue
                attached = self.attached_ids.get(u, ())
                for (cu, cnet, cpoly), cbox in zip(self.copper_obs, cboxes):
                    if _boxes_far(pbox, cbox, reach):
                        continue
                    if cu not in attached and any(_strict_overlap(p, cpoly) for p in self.cy[u]):
                        found.add(("copper", u, cu))
                    for pad in part.pads:
                        if not pad.polygon or (pad.net and pad.net == cnet):
                            continue
                        cl = rules.clearance(pad.net, cnet)
                        if cl is not None and _violating(pad.polygon, cpoly, cl):
                            found.add(("copper", u, cu))
        if self.old_polys:
            for u in sorted(self.parts):
                if self.parts[u].pads and not self.pads_inside(u, (0, 0), self.old_polys):
                    found.add(("edge", u, ""))
        ordered = sorted(found)
        self.pre_report = tuple({"kind": k, "uuids": [x for x in (a, b) if x]} for k, a, b in ordered)
        for k, a, b in ordered:
            self.warnings.add("pre_existing_violation:" + ":".join(x for x in (k, a, b) if x))

    # -- board edge --------------------------------------------------------------------------------
    def pads_inside(self, u, v, polys) -> bool:
        for pad in self.parts[u].pads:
            if not pad.polygon:
                continue
            if not geo.region_contains(polys, geo.polygon_translate(pad.polygon, v[0], v[1]), self.margin):
                return False
        return True

    def _pad_start(self, poly):
        """Exact start state of a pad that violates the edge, against the ORIGINAL outline.

        ``(areas, dist2)``: the overlap area with each connected piece of the forbidden region
        (index 0 the outside of the outer outline, i the i-th hole) and, per outline ring and edge,
        the squared distance from the full edge to the pad. All values are exact Fractions.
        """
        areas = geo.forbidden_overlap_areas(self.old_polys, poly)
        dist2 = tuple(
            tuple(geo.segment_polygon_distance2(a, b, poly) for a, b in geo.polygon_edges(ring))
            for ring in self.old_polys
        )
        return areas, dist2

    def _start_info(self, u):
        """Per pad: None when the pad is legal at the start, else its ``_pad_start`` state.

        Returns None when every pad is legal at the start (the common case).
        """
        if u not in self._edge_start:
            info = None
            if self.old_polys:
                per_pad = []
                for pad in self.parts[u].pads:
                    if not pad.polygon or geo.region_contains(self.old_polys, pad.polygon, self.margin):
                        per_pad.append(None)
                    else:
                        per_pad.append(self._pad_start(pad.polygon))
                if any(p is not None for p in per_pad):
                    info = tuple(per_pad)
            self._edge_start[u] = info
        return self._edge_start[u]

    def _not_worse(self, moved, polys, start) -> bool:
        """The violating pad ``moved`` is no deeper in ``polys`` than it was at the start.

        Exact: the overlap area with every piece of the forbidden region is at most the start
        area, and every boundary edge (the full edge, not its vertices) is at least
        ``min(d0, margin)`` away, where ``d0`` is the start distance to the same edge.
        """
        areas0, dist0 = start
        areas1 = geo.forbidden_overlap_areas(polys, moved)
        if len(areas1) != len(areas0) or any(a1 > a0 for a1, a0 in zip(areas1, areas0)):
            return False
        m = self.margin
        m2 = m * m
        box = geo.bbox(moved)
        for ring, ring_d0 in zip(polys, dist0):
            for (a, b), d0 in zip(geo.polygon_edges(ring), ring_d0):
                bound = min(d0, m2)
                if bound <= 0:
                    continue
                ebox = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
                if _boxes_far(box, ebox, m):
                    continue
                if geo.segment_polygon_distance2(a, b, moved) < bound:
                    return False
        return True

    def edge_ok(self, u, v, polys) -> bool:
        """End position ``v`` against ``polys``: every pad is legal, or was already violating at the
        start and is exactly no deeper (``_not_worse``). A legal start allows nothing but legal."""
        start = self._start_info(u)
        for k, pad in enumerate(self.parts[u].pads):
            if not pad.polygon:
                continue
            moved = geo.polygon_translate(pad.polygon, v[0], v[1])
            if geo.region_contains(polys, moved, self.margin):
                continue
            if start is None or start[k] is None or not self._not_worse(moved, polys, start[k]):
                return False
        return True

    def edge_path_ok(self, u, v, polys) -> bool:
        """``edge_ok`` at the end ``v`` (against ``polys``) AND legal along the whole straight path.

        The path is swept against the original outline with the same exact rule for every part
        (see ``_pad_path_ok``); a sub-path of a legal path is legal.
        """
        if not self.edge_ok(u, v, polys):
            return False
        if v == (0, 0) or not self.old_polys:
            return True
        return all(self._pad_path_ok(pad.polygon, v) for pad in self.parts[u].pads if pad.polygon)

    def _pad_path_ok(self, poly, v) -> bool:
        """Exact straight-path rule for one pad against the original outline.

        (i) distance: for every outline/hole edge the swept pad stays at least ``min(d0, margin)``
            from the full edge, ``d0`` being the start distance (``swept_polygon_distance2``);
        (ii) crossing: no point of the pad that is outside a piece of the forbidden region at the
            start enters it along the path (``forbidden_entry_area`` is zero; touching is legal).
        Together they forbid touching a piece the pad did not touch, swallowing a cutout, jumping a
        slot or notch, and going deeper into a violation, while retreating from it or sliding along
        it stays legal. Only edges near the swept box can matter.
        """
        m = self.margin
        m2 = m * m
        x0, y0, x1, y1 = geo.bbox(poly)
        box = (min(x0, x0 + v[0]), min(y0, y0 + v[1]), max(x1, x1 + v[0]), max(y1, y1 + v[1]))
        near = []
        for i, ring in enumerate(self.old_polys):
            for a, b in geo.polygon_edges(ring):
                ebox = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
                if not _boxes_far(box, ebox, m):
                    near.append((i, a, b))
        for _, a, b in near:
            bound = min(geo.segment_polygon_distance2(a, b, poly), m2)
            if bound > 0 and geo.swept_polygon_distance2(poly, v, (a, b)) < bound:
                return False
        return geo.forbidden_entry_area(self.old_polys, poly, v, near) == 0

    # -- the wave --------------------------------------------------------------------------------
    def _rel(self, lm, lq):
        a, b = self.m.vec(lm), self.m.vec(lq)
        return (a[0] - b[0], a[1] - b[1])

    def min_push(self, m, lm, q, lq):
        if lm <= lq:
            return None

        def hit(l):
            return _hit(self.cy[m], self._rel(lm, l), self.cy[q], self.c)

        if hit(lm):
            return None
        lo, hi = lq, lm
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if hit(mid):
                lo = mid
            else:
                hi = mid
        return hi

    def wave(self, seed, total, seed_order=()):
        disp = dict(seed)
        order = list(seed_order)
        movable = [u for u in sorted(self.cy) if u not in self.kind and u not in self.drivers]
        skip = set()
        fc_cache: dict = {}
        for _ in range(200000):
            best = None
            for m in sorted(disp):
                lm = disp[m]
                if lm <= 0 or not self.cy[m]:
                    continue
                for q in movable:
                    if q == m or (m, q) in skip or not self.cy[q] or not self.share(m, q):
                        continue
                    lq = disp.get(q, 0)
                    if lm <= lq:
                        continue
                    # the scan restarts after every push; a pair's answer depends only on (lm, lq)
                    ck = (m, q, lm, lq)
                    if ck in fc_cache:
                        f = fc_cache[ck]
                    else:
                        f = fc_cache[ck] = _first_contact(self.cy[m], self._rel(lm, lq), self.cy[q], self.c)
                    if f is None:
                        continue
                    contact = int(round((total - lm) + f * lm))
                    key = (contact, 0 if q in self.prio else 1, q, m)
                    if best is None or key < best[0]:
                        best = (key, m, q, lm, lq)
            if best is None:
                break
            _, m, q, lm, lq = best
            new = self.min_push(m, lm, q, lq)
            if new is None:
                skip.add((m, q))
                continue
            disp[q] = new
            if q not in order:
                order.append(q)
        return disp, order

    # -- hard obstacles --------------------------------------------------------------------------
    def hard_blockers(self, disp, total, polys_now):
        out = []
        rules = self.board.rules
        for p in sorted(u for u in disp if disp[u] > 0):
            lp = disp[p]
            v = self.m.vec(lp)
            if v == (0, 0):
                continue
            cy = self.cy[p]
            part = self.parts[p]
            if cy:
                for q in sorted(self.kind):
                    qp = self.cy[q]
                    if qp and self.share(p, q):
                        f = _first_contact(cy, v, qp, self.c)
                        if f is not None:
                            out.append(Blocker(q, self.kind[q], _at(f, lp, total)))
                for k in self.board.keepouts:
                    if not k.no_footprints or not k.polygon:
                        continue
                    f = _first_contact(cy, v, (k.polygon,), 0)
                    if f is not None:
                        out.append(Blocker(k.uuid, "keepout", _at(f, lp, total)))
            attached = self.attached_ids.get(p, ())
            for cu, cnet, cpoly in self.copper_obs:
                fs = []
                cp = (cpoly,)
                if cy and cu not in attached:
                    f = _first_contact(cy, v, cp, 0)
                    if f is not None:
                        fs.append(f)
                for pad in part.pads:
                    if not pad.polygon or (pad.net and pad.net == cnet):
                        continue
                    cl = rules.clearance(pad.net, cnet)
                    if cl is None:
                        continue  # refused up front (missing_rule); kept for safety
                    f = _first_contact((pad.polygon,), v, cp, cl)
                    if f is not None:
                        fs.append(f)
                if fs:
                    out.append(Blocker(cu, "copper", _at(min(fs), lp, total)))
            out.extend(self.attached_blockers(p, lp, total))
            if polys_now and part.pads and not self.edge_path_ok(p, v, polys_now):
                lo, hi = 0, lp
                if self.edge_path_ok(p, (0, 0), polys_now):
                    while hi - lo > 1:
                        mid = (lo + hi) // 2
                        if self.edge_path_ok(p, self.m.vec(mid), polys_now):
                            lo = mid
                        else:
                            hi = mid
                out.append(Blocker(p, "edge", (total - lp) + lo))
        return out

    def finish(self, disp, order, blockers, total) -> _Eval:
        blockers = _dedupe(blockers)
        if blockers:
            return _Eval(False, disp, order, blockers)
        cu = None
        if self.req.copper == "adapt":
            moves = {}
            for u in sorted(disp):
                v = self.m.vec(disp[u])
                if disp[u] > 0 and v != (0, 0):
                    moves[u] = v
            if moves:
                cu = _adapt_copper_hook(self.board, moves, self.req.allow_partial)
                cb = [b if b.at_nm is not None else dataclasses.replace(b, at_nm=total) for b in cu.blockers]
                if cb or not cu.ok:
                    return _Eval(False, disp, order, _dedupe(cb), cu)
        return _Eval(True, disp, order, [], cu)


# ----------------------------------------------------------------------------- stop-at-limit


def _search(ev, n: int, allow_partial: bool):
    """Return (achieved, eval_at_achieved_or_None, first_illegal_eval_or_None)."""
    full = ev(n)
    if full.legal:
        return n, full, None
    if not allow_partial:
        return 0, None, full
    step = max(_STEP_MIN_NM, n // 200)
    last, first_ill, pos = 0, None, step
    while pos < n:
        r = ev(pos)
        if not r.legal:
            first_ill = r
            break
        last = pos
        pos += step
    if first_ill is None:
        first_ill, pos = full, n
    lo, hi = last, pos
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if ev(mid).legal:
            lo = mid
        else:
            hi = mid
    return lo, (ev(lo) if lo > 0 else None), first_ill


def _rejected(code: str, warnings=()) -> SolveResult:
    return SolveResult(ChangeSet(), Report(status="rejected", reject_reason=code, warnings=tuple(warnings)))


def _coerce(request):
    if isinstance(request, Request):
        return request
    try:
        return Request.from_dict(request)
    except RequestError as e:
        raise _Reject(e.code)


def _check_uuids(board: Board, req: Request) -> None:
    for name in ("driver_uuids", "movers", "frozen", "priority"):
        items = getattr(req, name)
        if len(set(items)) != len(items):
            raise _Reject("duplicate_uuid")
    for name in ("driver_uuids", "movers", "frozen", "priority"):
        for u in getattr(req, name):
            if u not in board.index or board.index[u][0] != "part":
                raise _Reject("unknown_uuid")
    if req.mode == "explicit" and not req.movers:
        raise _Reject("explicit_without_movers")


def _assemble(ctx: _Ctx, n: int, achieved: int, best, first_ill, full_ok_expected, edge_changes=()):
    """Build the SolveResult shared by the part and the edge solver."""
    warnings = set(ctx.warnings)
    if achieved <= 0 or best is None:
        blockers = first_ill.blockers if first_ill is not None else []
        return SolveResult(
            ChangeSet(),
            Report(status="blocked", requested_nm=n, achieved_nm=0, blockers=tuple(blockers), warnings=tuple(sorted(warnings))),
            pre_existing=ctx.pre_report,
        )
    moves = []
    moved = []
    for u in sorted(best.disp):
        v = ctx.m.vec(best.disp[u])
        if best.disp[u] > 0 and v != (0, 0):
            moves.append(PartMove(u, v[0], v[1]))
            moved.append(u)
    cu = best.copper
    tracks, vias, legs = (), (), ()
    if cu is not None:
        tracks, vias, legs = tuple(cu.track_changes), tuple(cu.via_moves), tuple(cu.legs)
        warnings.update(cu.warnings)
    full = achieved >= n
    report = Report(
        status="ok" if full else "partial",
        requested_nm=n,
        achieved_nm=achieved,
        blockers=() if full else tuple(first_ill.blockers if first_ill is not None else ()),
        warnings=tuple(sorted(warnings)),
        legs=legs,
        moved_parts=tuple(moved),
    )
    cs = ChangeSet(tuple(moves), tracks, vias, tuple(edge_changes))
    return SolveResult(cs, report, tuple(u for u in best.order if u in set(moved)), ctx.pre_report)


# ----------------------------------------------------------------------------- part drivers


def solve(board: Board, request) -> SolveResult:
    # covers: ADE-1
    """Solve a part-driver request (edge drivers are forwarded to ``solve_edge``)."""
    try:
        req = _coerce(request)
        if req.driver_kind == "edge":
            return solve_edge(board, req)
        _check_uuids(board, req)
        dx, dy = req.displacement_nm
        if dx == 0 and dy == 0:
            raise _Reject("no_displacement")
    except _Reject as e:
        return _rejected(e.code)

    motion = _Motion(dx, dy)
    n = motion.n
    ctx = _Ctx(board, req, motion, req.driver_uuids)
    if ctx.missing:
        return _rejected("missing_rule", ctx.missing_warnings())
    bad = [Blocker(u, ctx.driver_kind(u), 0) for u in sorted(req.driver_uuids) if ctx.driver_kind(u)]
    if bad:
        return SolveResult(
            ChangeSet(),
            Report(status="blocked", requested_nm=n, blockers=tuple(bad), warnings=tuple(sorted(ctx.warnings))),
            pre_existing=ctx.pre_report,
        )

    def ev(total):
        disp, order = ctx.wave({u: total for u in req.driver_uuids}, total)
        return ctx.finish(disp, order, ctx.hard_blockers(disp, total, board.outline.polygons), total)

    achieved, best, first_ill = _search(ev, n, req.allow_partial)
    return _assemble(ctx, n, achieved, best, first_ill, True)


# ----------------------------------------------------------------------------- edge driver


def _edge_geometry(board: Board, req: Request):
    """Validate the dragged edge; return the data solve_edge needs or raise _Reject."""
    try:
        seg = board.edge(req.edge_uuid)
    except KeyError:
        raise _Reject("unknown_uuid")
    others = sorted((e for e in board.outline.edges if e.uuid != seg.uuid), key=lambda e: e.uuid)
    adj_s = [e for e in others if seg.start in (e.start, e.end)]
    adj_e = [e for e in others if seg.end in (e.start, e.end)]
    if seg.mid is not None or any(e.mid is not None for e in adj_s + adj_e):
        raise _Reject("arc_edge")
    vertical = seg.start[0] == seg.end[0]
    horizontal = seg.start[1] == seg.end[1]
    if vertical == horizontal:
        raise _Reject("not_axis_aligned")
    polys = board.outline.polygons
    if not polys or len(adj_s) != 1 or len(adj_e) != 1 or adj_s[0].uuid == adj_e[0].uuid:
        raise _Reject("unsupported_topology")
    outer, holes = polys[0], polys[1:]
    if seg.start not in outer or seg.end not in outer:
        raise _Reject("unsupported_topology")
    if any(not geo.point_in_polygon(h[0], outer, True) for h in holes):
        raise _Reject("unsupported_topology")

    def inside(pt):
        return geo.point_in_polygon(pt, outer, False) and not any(geo.point_in_polygon(pt, h, True) for h in holes)

    mid = ((seg.start[0] + seg.end[0]) // 2, (seg.start[1] + seg.end[1]) // 2)
    nx, ny = (1, 0) if vertical else (0, 1)
    pos_in = inside((mid[0] + nx, mid[1] + ny))
    neg_in = inside((mid[0] - nx, mid[1] - ny))
    if pos_in == neg_in:
        raise _Reject("unsupported_topology")
    n_out = (nx, ny) if neg_in else (-nx, -ny)
    if _EDGE_NORMALS[req.side] != n_out:
        raise _Reject("side_mismatch")
    dx, dy = req.displacement_nm
    e = dx * n_out[0] + dy * n_out[1]
    if e == 0 and dy == 0 and dx != 0:
        e = dx  # scalar form along the outward normal
    if e == 0:
        raise _Reject("no_displacement")
    return seg, adj_s[0], adj_e[0], outer, holes, n_out, e


def solve_edge(board: Board, request) -> SolveResult:
    """Drag one outline edge along its normal and push parts the new outline would exclude."""
    try:
        req = _coerce(request)
        if req.driver_kind != "edge":
            raise _Reject("bad_driver")
        _check_uuids(board, req)
        seg, adj_s, adj_e, outer, holes, n_out, e = _edge_geometry(board, req)
    except _Reject as ex:
        return _rejected(ex.code)

    sign = 1 if e > 0 else -1
    total_e = abs(e)

    def delta(t):
        return (sign * t * n_out[0], sign * t * n_out[1])

    def outer_at(t):
        d = delta(t)
        return tuple((v[0] + d[0], v[1] + d[1]) if v in (seg.start, seg.end) else v for v in outer)

    def region_at(t):
        return (outer_at(t),) + tuple(holes)

    d_full = delta(total_e)
    strip = (seg.start, seg.end, (seg.end[0] + d_full[0], seg.end[1] + d_full[1]), (seg.start[0] + d_full[0], seg.start[1] + d_full[1]))
    if any(geo.polygons_intersect(h, strip) for h in holes):
        return _rejected("cutout_touches_edge")
    a0, a1 = geo.polygon_area2(outer), geo.polygon_area2(outer_at(total_e))
    if a1 == 0 or (a1 > 0) != (a0 > 0):
        return _rejected("edge_collapse")
    if not geo.polygon_is_simple(outer_at(total_e)):
        return _rejected("self_intersection")

    motion = _Motion(-n_out[0], -n_out[1], unit=True)  # parts are pushed inward
    ctx = _Ctx(board, req, motion, (), edge_seg=seg)
    if ctx.missing:
        return _rejected("missing_rule", ctx.missing_warnings())

    def dot(p):
        return p[0] * n_out[0] + p[1] * n_out[1]

    base = dot(seg.start)  # coordinate of the dragged edge along the outward normal

    def limit_for(u, t):
        """Largest edge displacement t' < t where the part's pads are still legal (obstacle limit)."""
        lo, hi = 0, t
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if ctx.edge_ok(u, (0, 0), region_at(mid)):
                lo = mid
            else:
                hi = mid
        return lo

    def ev(t):
        poly_now = region_at(t)
        if t > 0 and (geo.polygon_area2(poly_now[0]) == 0 or not geo.polygon_is_simple(poly_now[0])):
            return _Eval(False, {}, [], [])
        blockers, seeds = [], {}
        for u in sorted(ctx.parts):
            part = ctx.parts[u]
            if not part.pads or ctx.edge_ok(u, (0, 0), poly_now):
                continue
            if u in ctx.kind:
                blockers.append(Blocker(u, ctx.kind[u], limit_for(u, t)))
                continue
            # inward push needed: how far the pads overshoot the moved edge (plus clearance)
            pad_ext = max(dot(p) for pad in part.pads for p in pad.polygon)
            lo, hi = 0, pad_ext - (base + sign * t - ctx.margin)
            if hi <= 0 or not ctx.edge_ok(u, motion.vec(hi), poly_now):
                blockers.append(Blocker(u, "edge", limit_for(u, t)))
                continue
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if ctx.edge_ok(u, motion.vec(mid), poly_now):
                    hi = mid
                else:
                    lo = mid
            seeds[u] = hi
        seed_order = sorted(seeds, key=lambda u: (0 if u in ctx.prio else 1, u))
        disp, order = ctx.wave(seeds, t, seed_order)
        blockers += ctx.hard_blockers(disp, t, poly_now)
        return ctx.finish(disp, order, blockers, t)

    achieved, best, first_ill = _search(ev, total_e, req.allow_partial)
    if achieved <= 0 or best is None:
        return _assemble(ctx, total_e, 0, None, first_ill, True)
    d = delta(achieved)

    def shifted(pt):
        return (pt[0] + d[0], pt[1] + d[1])

    changes = [EdgeChange(seg.uuid, shifted(seg.start), shifted(seg.end))]
    for adj, shared in ((adj_s, seg.start), (adj_e, seg.end)):
        a = shifted(adj.start) if adj.start == shared else adj.start
        b = shifted(adj.end) if adj.end == shared else adj.end
        changes.append(EdgeChange(adj.uuid, a, b))
    return _assemble(ctx, total_e, achieved, best, first_ill, True, changes)
