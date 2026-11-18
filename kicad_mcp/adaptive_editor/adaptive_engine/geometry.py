"""Polygon and segment geometry in integer nanometres (pure Python, stdlib only).

A Point is a tuple ``(x, y)`` of ints. A Polygon is a tuple of Points, implicitly closed, any
winding. Distances are computed exactly as squared rationals (integer numerator/denominator, never
offsetting polygons); the public ``*_distance`` functions return a float square root of that exact
value, and ``*_distance2`` / ``*_ceil`` variants expose the exact value or a conservative integer.

Boundary conventions
--------------------
* ``polygons_intersect`` and ``point_in_polygon(include_boundary=True)`` treat touching as contact.
* ``inflate_hit(p, q, c)`` is a *violation* test: ``polygon_distance(p, q) < c`` (strict). A pair
  exactly ``c`` apart is legal. With ``c == 0`` nothing can be "< 0", so use ``polygons_intersect``
  when overlap or touching must be detected at zero clearance.
* ``swept_polygon_hits`` and ``first_contact_fraction`` are *contact* tests: they report the first
  moment the distance is ``<= clearance`` (inclusive), so the last legal position of a push is the
  position at that contact fraction. ``first_contact_fraction(...) is not None`` exactly when
  ``swept_polygon_hits(...)`` is True.

Public API (names and signatures are a contract for solver.py / copper.py)
---------------------------------------------------------------------------
bbox, polygon_area2, point_in_polygon, segments_intersect, point_segment_distance,
segment_segment_distance, segment_polygon_distance, point_polygon_distance, polygons_intersect,
polygon_distance, polygon_translate, polygon_is_simple, swept_polygon_hits,
first_contact_fraction, inflate_hit, polygon_inside, region_contains.

Extra public helpers
--------------------
cross(o, a, b)                      exact cross product of (a-o) x (b-o)
polygon_edges(poly)                 iterator of (a, b) edges including the closing edge
rect_polygon(x0, y0, x1, y1)        axis-aligned rectangle polygon
polygon_distance2(p, q)             exact squared distance, a Fraction (0 if intersecting)
polygon_distance_ceil(p, q)         distance rounded UP to an int (conservative clearance checks)
ceil_sqrt_frac(n, d=1)              smallest int k with k*k*d >= n
segment_polygon_distance2(a, b, poly)  exact squared distance of a segment to a filled polygon
swept_polygon_distance2(moving, (dx, dy), obstacle)
                                    exact squared minimum distance over the whole translation
segment_inside_polygon(a, b, poly, margin=0)
                                    segment lies in the closed polygon, boundary distance >= margin
segment_inside_region(a, b, polygons, margin=0)
                                    same for a region: polygons[0] outer minus polygons[1:] holes
Segments touching a boundary are allowed when margin is 0; with margin > 0 every point of the
segment must be at least margin from every boundary edge.
intersection_area(polygons)         exact area (Fraction) of the intersection of simple polygons
forbidden_overlap_areas(polygons, inner)
                                    exact area of ``inner`` inside each connected piece of the
                                    forbidden region: index 0 outside polygons[0], i the hole i
forbidden_entry_area(polygons, inner, v, edges=None)
                                    exact area of the points of ``inner`` that enter a piece of the
                                    forbidden region they are outside of when translated by t*v

Swept collision method
----------------------
For a translation by ``v`` the minimum over t in [0, 1] of distance(M + t*v, O) is zero if M and O
overlap at t = 0, otherwise it equals the minimum over (a) each vertex trajectory segment
``[p, p+v]`` of M against every edge of O and (b) each reverse trajectory ``[q-v, q]`` of every
vertex q of O against every edge of M. This is the exact swept-region test (vertex trajectories
are the parallelogram sweep edges), so an obstacle strictly between start and end is detected even
when neither end position touches it. Feasibility is not assumed monotonic.

Exact areas and the board-edge region
-------------------------------------
``intersection_area`` uses vertical slabs: between consecutive x breakpoints (all vertex x and all
edge-edge crossing x of different polygons) no edge starts, ends or crosses another, so the total
length of the cross-section of the intersection is linear in x and the slab area is that length at
the slab midpoint times the slab width. Everything is Fraction arithmetic (no clipping, no
degenerate-case branches; shared or collinear edges are harmless).

The forbidden region F of a board is the outside of ``polygons[0]`` plus every hole
``polygons[1:]``. Its connected pieces are the outside and each hole. ``forbidden_entry_area``
answers "does any point of ``inner`` cross from outside a piece of F into it while ``inner``
translates by ``t*v``, t in [0, 1]?" exactly. A point p crosses boundary edge e at some t iff
p + t*v lies on e, i.e. iff p lies in the parallelogram ``(a, b, b-v, a-v)`` swept by e moving by
``-v`` (the swept region of a closed set is the set itself plus its swept boundary). For the
outside piece the points that matter are those of ``inner`` still inside ``polygons[0]``
(``inner & parallelogram & polygons[0]``); for a hole they are those not yet in it
(``inner & parallelogram`` minus ``inner & parallelogram & hole``). Edges parallel to ``v`` sweep a
zero-area parallelogram and are skipped. Touching is not entering (the areas are zero).
"""

from __future__ import annotations

import math
from fractions import Fraction
from typing import Iterator, Sequence

Point = tuple[int, int]
Polygon = tuple[Point, ...]

__all__ = [
    "bbox",
    "polygon_area2",
    "point_in_polygon",
    "segments_intersect",
    "point_segment_distance",
    "segment_segment_distance",
    "segment_polygon_distance",
    "point_polygon_distance",
    "polygons_intersect",
    "polygon_distance",
    "polygon_translate",
    "polygon_is_simple",
    "swept_polygon_hits",
    "first_contact_fraction",
    "inflate_hit",
    "polygon_inside",
    "region_contains",
    "cross",
    "polygon_edges",
    "rect_polygon",
    "polygon_distance2",
    "polygon_distance_ceil",
    "ceil_sqrt_frac",
    "segment_polygon_distance2",
    "swept_polygon_distance2",
    "segment_inside_polygon",
    "segment_inside_region",
    "intersection_area",
    "forbidden_overlap_areas",
    "forbidden_entry_area",
]


# ----------------------------------------------------------------------------- basics


def cross(o, a, b):
    """Cross product (a - o) x (b - o); exact for ints and Fractions."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _sign(x) -> int:
    return (x > 0) - (x < 0)


def _in_box(p, a, b) -> bool:
    return (
        min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    )


def _on_segment(p, a, b) -> bool:
    # bounding-box test first: it is cheap and rejects nearly every probe (exact Fraction cross is not)
    return _in_box(p, a, b) and cross(a, b, p) == 0


def polygon_edges(poly: Sequence[Point]) -> Iterator[tuple[Point, Point]]:
    """Yield every edge ``(a, b)`` of the implicitly closed polygon."""
    n = len(poly)
    for i in range(n):
        yield poly[i], poly[(i + 1) % n]


def rect_polygon(x0: int, y0: int, x1: int, y1: int) -> Polygon:
    """Axis-aligned rectangle (counter-clockwise) from two opposite corners."""
    lx, hx = min(x0, x1), max(x0, x1)
    ly, hy = min(y0, y1), max(y0, y1)
    return ((lx, ly), (hx, ly), (hx, hy), (lx, hy))


def _require(poly) -> None:
    if len(poly) == 0:
        raise ValueError("empty polygon")


def bbox(poly: Sequence[Point]) -> tuple[int, int, int, int]:
    """Return ``(minx, miny, maxx, maxy)``."""
    _require(poly)
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


def polygon_area2(poly: Sequence[Point]) -> int:
    """Signed twice-area (positive for counter-clockwise winding)."""
    total = 0
    for a, b in polygon_edges(poly):
        total += a[0] * b[1] - b[0] * a[1]
    return total


def polygon_translate(poly: Sequence[Point], dx: int, dy: int) -> Polygon:
    return tuple((p[0] + dx, p[1] + dy) for p in poly)


# ----------------------------------------------------------------------------- point / polygon


def point_in_polygon(pt, poly: Sequence[Point], include_boundary: bool = True) -> bool:
    """Exact even-odd containment. Boundary points return ``include_boundary``.

    Works for int and Fraction coordinates.
    """
    n = len(poly)
    if n == 0:
        return False
    x, y = pt
    inside = False
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        if _on_segment(pt, a, b):
            return include_boundary
        if (a[1] > y) != (b[1] > y):
            dy = b[1] - a[1]
            lhs = (x - a[0]) * dy
            rhs = (y - a[1]) * (b[0] - a[0])
            if (dy > 0 and lhs < rhs) or (dy < 0 and lhs > rhs):
                inside = not inside
    return inside


def segments_intersect(a, b, c, d) -> bool:
    """True when segments ab and cd share at least one point (touching counts)."""
    if (
        max(a[0], b[0]) < min(c[0], d[0])
        or max(c[0], d[0]) < min(a[0], b[0])
        or max(a[1], b[1]) < min(c[1], d[1])
        or max(c[1], d[1]) < min(a[1], b[1])
    ):
        return False
    o1 = _sign(cross(c, d, a))
    o2 = _sign(cross(c, d, b))
    o3 = _sign(cross(a, b, c))
    o4 = _sign(cross(a, b, d))
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    if o1 == 0 and _in_box(a, c, d):
        return True
    if o2 == 0 and _in_box(b, c, d):
        return True
    if o3 == 0 and _in_box(c, a, b):
        return True
    if o4 == 0 and _in_box(d, a, b):
        return True
    return False


# ----------------------------------------------------------------------------- exact distances
# Internal squared distances are (numerator, denominator) integer pairs, denominator > 0.


def _pt_seg_d2(p, a, b) -> tuple[int, int]:
    ex, ey = b[0] - a[0], b[1] - a[1]
    px, py = p[0] - a[0], p[1] - a[1]
    l2 = ex * ex + ey * ey
    if l2 == 0:
        return (px * px + py * py, 1)
    t = px * ex + py * ey
    if t <= 0:
        return (px * px + py * py, 1)
    if t >= l2:
        qx, qy = p[0] - b[0], p[1] - b[1]
        return (qx * qx + qy * qy, 1)
    cr = px * ey - py * ex
    return (cr * cr, l2)


def _fmin(f: tuple[int, int], g: tuple[int, int]) -> tuple[int, int]:
    return f if f[0] * g[1] <= g[0] * f[1] else g


def _seg_seg_d2(a, b, c, d, check_intersect: bool = True) -> tuple[int, int]:
    if check_intersect and segments_intersect(a, b, c, d):
        return (0, 1)
    best = _pt_seg_d2(a, c, d)
    best = _fmin(best, _pt_seg_d2(b, c, d))
    best = _fmin(best, _pt_seg_d2(c, a, b))
    best = _fmin(best, _pt_seg_d2(d, a, b))
    return best


def _fsqrt(f: tuple[int, int]) -> float:
    return math.sqrt(f[0] / f[1])


def ceil_sqrt_frac(n: int, d: int = 1) -> int:
    """Smallest integer ``k >= 0`` with ``k * k * d >= n`` (ceil of sqrt(n / d))."""
    if n <= 0:
        return 0
    k = math.isqrt(n // d)
    while k * k * d < n:
        k += 1
    return k


def point_segment_distance(p, a, b) -> float:
    return _fsqrt(_pt_seg_d2(p, a, b))


def segment_segment_distance(a, b, c, d) -> float:
    return _fsqrt(_seg_seg_d2(a, b, c, d))


def _seg_poly_d2(a, b, poly) -> tuple[int, int]:
    _require(poly)
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return (0, 1)
    best = None
    for c, d in polygon_edges(poly):
        cand = _seg_seg_d2(a, b, c, d)
        best = cand if best is None else _fmin(best, cand)
        if best[0] == 0:
            break
    return best


def segment_polygon_distance2(a, b, poly) -> Fraction:
    n, d = _seg_poly_d2(a, b, poly)
    return Fraction(n, d)


def segment_polygon_distance(a, b, poly) -> float:
    """Distance from segment ab to the filled polygon (0 if they touch or ab is inside)."""
    return _fsqrt(_seg_poly_d2(a, b, poly))


def point_polygon_distance(p, poly) -> float:
    """Distance from p to the filled polygon (0 inside or on the boundary)."""
    return segment_polygon_distance(p, p, poly)


def _bbox_disjoint(bp, bq) -> bool:
    return bp[2] < bq[0] or bq[2] < bp[0] or bp[3] < bq[1] or bq[3] < bp[1]


def polygons_intersect(p, q) -> bool:
    """True when the filled polygons share any point (edge crossing, containment or touching)."""
    if len(p) == 0 or len(q) == 0:
        return False
    if _bbox_disjoint(bbox(p), bbox(q)):
        return False
    for a, b in polygon_edges(p):
        for c, d in polygon_edges(q):
            if segments_intersect(a, b, c, d):
                return True
    return point_in_polygon(p[0], q) or point_in_polygon(q[0], p)


def _polygon_d2(p, q) -> tuple[int, int]:
    _require(p)
    _require(q)
    if polygons_intersect(p, q):
        return (0, 1)
    best = None
    for a, b in polygon_edges(p):
        for c, d in polygon_edges(q):
            cand = _seg_seg_d2(a, b, c, d, check_intersect=False)
            best = cand if best is None else _fmin(best, cand)
    return best


def polygon_distance2(p, q) -> Fraction:
    """Exact squared distance between two filled polygons (0 if they intersect)."""
    n, d = _polygon_d2(p, q)
    return Fraction(n, d)


def polygon_distance(p, q) -> float:
    return _fsqrt(_polygon_d2(p, q))


def polygon_distance_ceil(p, q) -> int:
    """Distance rounded up to an integer, so ``<`` clearance checks stay conservative."""
    n, d = _polygon_d2(p, q)
    return ceil_sqrt_frac(n, d)


def inflate_hit(p, q, clearance) -> bool:
    """True when ``polygon_distance(p, q) < clearance`` (exact; no polygon offsetting)."""
    if clearance <= 0:
        return False
    n, d = _polygon_d2(p, q)
    return n < clearance * clearance * d


def polygon_is_simple(poly: Sequence[Point]) -> bool:
    """True for a non-degenerate polygon without self-intersection or repeated vertices."""
    n = len(poly)
    if n < 3:
        return False
    for i in range(n):
        if tuple(poly[i]) == tuple(poly[(i + 1) % n]):
            return False
    if polygon_area2(poly) == 0:
        return False
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        c = poly[(i + 2) % n]
        if cross(a, b, c) == 0:
            fold = (b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])
            if fold < 0:
                return False
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if segments_intersect(a, b, poly[j], poly[(j + 1) % n]):
                return False
    return True


# ----------------------------------------------------------------------------- swept motion


def _swept_pairs(moving, v, obstacle):
    dx, dy = v
    for p in moving:
        t = (p[0] + dx, p[1] + dy)
        for c, d in polygon_edges(obstacle):
            yield p, t, c, d
    for q in obstacle:
        s = (q[0] - dx, q[1] - dy)
        for a, b in polygon_edges(moving):
            yield s, q, a, b


def _swept_bbox_miss(moving, v, obstacle, clearance) -> bool:
    x0, y0, x1, y1 = bbox(moving)
    ox0, oy0, ox1, oy1 = bbox(obstacle)
    dx, dy = v
    c = max(clearance, 0)
    sx0, sx1 = min(x0, x0 + dx), max(x1, x1 + dx)
    sy0, sy1 = min(y0, y0 + dy), max(y1, y1 + dy)
    return sx1 + c < ox0 or ox1 + c < sx0 or sy1 + c < oy0 or oy1 + c < sy0


def swept_polygon_distance2(moving, v, obstacle) -> Fraction:
    """Exact squared minimum distance between ``moving + t*v`` (t in [0, 1]) and ``obstacle``."""
    _require(moving)
    _require(obstacle)
    if polygons_intersect(moving, obstacle):
        return Fraction(0)
    best = None
    for a, b, c, d in _swept_pairs(moving, v, obstacle):
        cand = _seg_seg_d2(a, b, c, d)
        best = cand if best is None else _fmin(best, cand)
        if best[0] == 0:
            break
    return Fraction(best[0], best[1])


def swept_polygon_hits(moving_poly, d, obstacle_poly, clearance=0) -> bool:
    """True if translating ``moving_poly`` by ``d`` ever comes within ``clearance`` of the obstacle.

    Contact is inclusive (distance <= clearance). Exact for translations; see the module docstring.
    """
    _require(moving_poly)
    _require(obstacle_poly)
    c = max(clearance, 0)
    if _swept_bbox_miss(moving_poly, d, obstacle_poly, c):
        return False
    if polygons_intersect(moving_poly, obstacle_poly):
        return True
    c2 = c * c
    for a, b, cc, dd in _swept_pairs(moving_poly, d, obstacle_poly):
        n, den = _seg_seg_d2(a, b, cc, dd)
        if n <= c2 * den:
            return True
    return False


def _disk_entry(p, w, q, r):
    """Smallest t >= 0 (and <= 1) where |p + t*w - q| <= r, or None."""
    ux, uy = p[0] - q[0], p[1] - q[1]
    a = w[0] * w[0] + w[1] * w[1]
    b = w[0] * ux + w[1] * uy
    c = ux * ux + uy * uy - r * r
    disc = b * b - a * c
    if disc < 0:
        return None
    sq = math.sqrt(disc)
    if b <= 0:
        qq = -b + sq
        if qq == 0:
            t1 = t2 = 0.0
        else:
            t1 = c / qq
            t2 = qq / a
    else:
        qq = -b - sq
        t1 = qq / a
        t2 = c / qq
    if t1 > t2:
        t1, t2 = t2, t1
    if t2 < 0 or t1 > 1:
        return None
    return max(t1, 0.0)


def _first_entry(p, w, a, b, r):
    """Smallest t in [0, 1] where the point p + t*w is within r of segment ab, or None."""
    best = None
    for q in (a, b):
        t = _disk_entry(p, w, q, r)
        if t is not None and (best is None or t < best):
            best = t
    ex, ey = b[0] - a[0], b[1] - a[1]
    l2 = ex * ex + ey * ey
    if l2 == 0:
        return best
    px, py = p[0] - a[0], p[1] - a[1]
    s0 = px * ex + py * ey
    sw = w[0] * ex + w[1] * ey
    n0 = px * ey - py * ex
    nw = w[0] * ey - w[1] * ex
    rr = r * math.sqrt(l2)
    lo, hi = 0.0, 1.0
    for alpha, beta, lob, hib in ((s0, sw, 0, l2), (n0, nw, -rr, rr)):
        if beta == 0:
            if alpha < lob or alpha > hib:
                return best
        else:
            ta = (lob - alpha) / beta
            tb = (hib - alpha) / beta
            if ta > tb:
                ta, tb = tb, ta
            lo = max(lo, ta)
            hi = min(hi, tb)
    if lo <= hi + 1e-12 and (best is None or lo < best):
        best = lo
    return best


def first_contact_fraction(moving_poly, d, obstacle_poly, clearance=0):
    """Smallest t in [0, 1] with ``distance(moving + t*d, obstacle) <= clearance``, else None.

    Returns 0.0 when already in contact. Does not assume monotonic feasibility: the result is the
    first contact over the whole path, computed from exact vertex-trajectory versus edge entry
    times (floats), gated by the exact swept test.
    """
    _require(moving_poly)
    _require(obstacle_poly)
    dx, dy = d
    c = max(clearance, 0)
    n, den = _polygon_d2(moving_poly, obstacle_poly)
    if n <= c * c * den:
        return 0.0
    if dx == 0 and dy == 0:
        return None
    if _swept_bbox_miss(moving_poly, d, obstacle_poly, c):
        return None
    best = None
    w = (dx, dy)
    for p in moving_poly:
        for a, b in polygon_edges(obstacle_poly):
            t = _first_entry(p, w, a, b, c)
            if t is not None and (best is None or t < best):
                best = t
    wr = (-dx, -dy)
    for q in obstacle_poly:
        for a, b in polygon_edges(moving_poly):
            t = _first_entry(q, wr, a, b, c)
            if t is not None and (best is None or t < best):
                best = t
    if best is not None:
        return min(max(best, 0.0), 1.0)
    if not swept_polygon_hits(moving_poly, d, obstacle_poly, c):
        return None
    steps = 1024  # numerical fallback; only reached on float edge cases
    for i in range(1, steps + 1):
        t = i / steps
        moved = polygon_translate(moving_poly, round(dx * t), round(dy * t))
        n, den = _polygon_d2(moved, obstacle_poly)
        if n <= c * c * den:
            return t
    return 1.0


# ----------------------------------------------------------------------------- containment


def _cut_params(a, b, polys) -> list[Fraction]:
    """Sorted parameters t in [0, 1] where segment ab meets (or runs along) any polygon edge."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    r2 = rx * rx + ry * ry
    ts = {Fraction(0), Fraction(1)}
    for poly in polys:
        for c, d in polygon_edges(poly):
            sx, sy = d[0] - c[0], d[1] - c[1]
            qx, qy = c[0] - a[0], c[1] - a[1]
            rxs = rx * sy - ry * sx
            if rxs != 0:
                t = Fraction(qx * sy - qy * sx, rxs)
                u = Fraction(qx * ry - qy * rx, rxs)
                if 0 <= t <= 1 and 0 <= u <= 1:
                    ts.add(t)
            elif r2 != 0 and qx * ry - qy * rx == 0:
                for e in (c, d):
                    t = Fraction((e[0] - a[0]) * rx + (e[1] - a[1]) * ry, r2)
                    if 0 <= t <= 1:
                        ts.add(t)
    return sorted(ts)


def segment_inside_region(a, b, polygons, margin=0) -> bool:
    """Segment ab lies in the region ``polygons[0]`` minus the open holes ``polygons[1:]``.

    Boundary contact is allowed; with ``margin > 0`` every point of ab must also be at least
    ``margin`` from every boundary edge of the region.
    """
    polygons = tuple(polygons)
    if not polygons or len(polygons[0]) == 0:
        return False
    outer = polygons[0]
    holes = polygons[1:]
    ts = _cut_params(a, b, polygons)
    for t1, t2 in zip(ts, ts[1:]):
        tm = (t1 + t2) / 2
        mid = (a[0] + tm * (b[0] - a[0]), a[1] + tm * (b[1] - a[1]))
        if not point_in_polygon(mid, outer, True):
            return False
        for hole in holes:
            if point_in_polygon(mid, hole, False):
                return False
    if margin > 0:
        m2 = margin * margin
        for poly in polygons:
            for c, d in polygon_edges(poly):
                n, den = _seg_seg_d2(a, b, c, d)
                if n < m2 * den:
                    return False
    return True


def segment_inside_polygon(a, b, poly, margin=0) -> bool:
    """Segment ab lies in the closed polygon, at least ``margin`` from its boundary."""
    return segment_inside_region(a, b, (poly,), margin)


def region_contains(polygons, inner, margin=0) -> bool:
    """``inner`` lies in the region ``polygons[0]`` minus holes ``polygons[1:]``.

    Its boundary must stay at least ``margin`` from every region boundary, and no hole may be
    swallowed by ``inner``.
    """
    polygons = tuple(polygons)
    if not polygons or len(inner) == 0:
        return False
    for a, b in polygon_edges(inner):
        if not segment_inside_region(a, b, polygons, margin):
            return False
    for hole in polygons[1:]:
        for hv in hole:
            if point_in_polygon(hv, inner, False):
                return False
    return True


def polygon_inside(inner, outer, margin=0) -> bool:
    """``inner`` is inside ``outer`` and its boundary is at least ``margin`` from outer's boundary."""
    return region_contains((outer,), inner, margin)


# ----------------------------------------------------------------------------- exact areas


def _crossing_x(a, b, c, d):
    """x of the single point where segments ab and cd cross or touch, else None.

    Collinear overlaps return None (their end points are vertices, hence breakpoints anyway).
    """
    if (
        max(a[0], b[0]) < min(c[0], d[0])
        or max(c[0], d[0]) < min(a[0], b[0])
        or max(a[1], b[1]) < min(c[1], d[1])
        or max(c[1], d[1]) < min(a[1], b[1])
    ):
        return None
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if den == 0:
        return None
    qx, qy = c[0] - a[0], c[1] - a[1]
    t = Fraction(qx * sy - qy * sx) / den
    u = Fraction(qx * ry - qy * rx) / den
    if 0 <= t <= 1 and 0 <= u <= 1:
        return a[0] + t * rx
    return None


def _crossing_ys(segments, xm) -> list:
    """Sorted y values where the non-vertical ``segments`` (left end first) cross the line x = xm."""
    ys = []
    for a, b in segments:
        if a[0] < xm < b[0]:
            ys.append(a[1] + Fraction(b[1] - a[1]) * (xm - a[0]) / (b[0] - a[0]))
    ys.sort()
    return ys


def _intersect_intervals(first, second) -> list:
    """Intersection of two sorted lists of (lo, hi) intervals."""
    out = []
    i = j = 0
    while i < len(first) and j < len(second):
        lo = max(first[i][0], second[j][0])
        hi = min(first[i][1], second[j][1])
        if lo < hi:
            out.append((lo, hi))
        if first[i][1] < second[j][1]:
            i += 1
        else:
            j += 1
    return out


def intersection_area(polygons) -> Fraction:
    """Exact area of the intersection of the filled simple polygons (any winding, any shape).

    Vertical-slab method, see the module docstring. Returns Fraction(0) when there is no common
    area (disjoint, touching, degenerate or fewer than three vertices).
    """
    polys = [tuple(p) for p in polygons]
    if not polys or any(len(p) < 3 for p in polys):
        return Fraction(0)
    boxes = [bbox(p) for p in polys]
    x_lo = max(b[0] for b in boxes)
    x_hi = min(b[2] for b in boxes)
    y_lo = max(b[1] for b in boxes)
    y_hi = min(b[3] for b in boxes)
    if x_lo >= x_hi or y_lo >= y_hi:
        return Fraction(0)
    segs = []
    xs = {Fraction(x_lo), Fraction(x_hi)}
    for p in polys:
        lst = []
        for q in p:
            if x_lo < q[0] < x_hi:
                xs.add(Fraction(q[0]))
        for a, b in polygon_edges(p):
            if a[0] == b[0]:
                continue
            if a[0] > b[0]:
                a, b = b, a
            if b[0] <= x_lo or a[0] >= x_hi:
                continue
            lst.append((a, b))
        segs.append(lst)
    for i, first in enumerate(segs):
        for second in segs[i + 1 :]:
            for a, b in first:
                for c, d in second:
                    x = _crossing_x(a, b, c, d)
                    if x is not None and x_lo < x < x_hi:
                        xs.add(Fraction(x))
    ordered = sorted(xs)
    total = Fraction(0)
    for x0, x1 in zip(ordered, ordered[1:]):
        xm = (x0 + x1) / 2
        common = None
        for lst in segs:
            ys = _crossing_ys(lst, xm)
            ivs = list(zip(ys[0::2], ys[1::2]))
            common = ivs if common is None else _intersect_intervals(common, ivs)
            if not common:
                break
        if common:
            total += (x1 - x0) * sum((hi - lo for lo, hi in common), Fraction(0))
    return total


def forbidden_overlap_areas(polygons, inner) -> tuple:
    """Exact area of ``inner`` inside each connected piece of the forbidden region.

    ``polygons[0]`` is the outer boundary and ``polygons[1:]`` are holes. Index 0 of the result is
    the area of ``inner`` outside the outer boundary, index i the area inside hole i. All zero
    means ``inner`` overlaps no cutout or outside area (touching does not count).
    """
    polygons = tuple(polygons)
    inner = tuple(inner)
    if not polygons or len(inner) < 3:
        return tuple(Fraction(0) for _ in polygons)
    whole = Fraction(abs(polygon_area2(inner))) / 2
    areas = [whole - intersection_area((inner, polygons[0]))]
    for hole in polygons[1:]:
        areas.append(intersection_area((inner, hole)))
    return tuple(areas)


def forbidden_entry_area(polygons, inner, v, edges=None) -> Fraction:
    """Exact area of the points of ``inner`` that enter a piece of the forbidden region.

    A point enters the outside (or a hole) when, translated by ``t*v`` for some t in [0, 1], it
    crosses the outer boundary from inside (or a hole boundary from outside). Points already inside
    the forbidden piece never count, so a polygon that overlaps the forbidden region may retreat or
    slide along it (zero) but not go deeper or jump to another piece (positive). ``edges`` is an
    optional iterable of ``(ring_index, a, b)`` restricting the boundary edges that are tested
    (edges that cannot be reached by the sweep may be left out); the default is every edge.
    """
    polygons = tuple(polygons)
    dx, dy = v
    inner = tuple(inner)
    if not polygons or len(inner) < 3 or (dx == 0 and dy == 0):
        return Fraction(0)
    if edges is None:
        edges = [(i, a, b) for i, ring in enumerate(polygons) for a, b in polygon_edges(ring)]
    ibox = bbox(inner)
    total = Fraction(0)
    for i, a, b in edges:
        if (b[0] - a[0]) * dy - (b[1] - a[1]) * dx == 0:
            continue  # parallel to the motion: the swept parallelogram has no area
        quad = (a, b, (b[0] - dx, b[1] - dy), (a[0] - dx, a[1] - dy))
        if _bbox_disjoint(ibox, bbox(quad)):
            continue
        ring = polygons[i]
        if i == 0:
            total += intersection_area((inner, quad, ring))
        else:
            total += intersection_area((inner, quad)) - intersection_area((inner, quad, ring))
    return total
