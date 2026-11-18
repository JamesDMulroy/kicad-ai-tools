"""Solver and edge-drag tests on synthetic in-memory boards (stdlib unittest, no pcbnew, no copper.py).

All coordinates in helpers are millimetres, converted to integer nanometres by ``mm``.
Default rules: courtyard clearance 0.2 mm, copper edge clearance 0.3 mm, pad clearance 0.15 mm,
fallback margin 0.25 mm. Parts are rectangles with a pad inset 0.1 mm from the courtyard.
"""

import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from adaptive_engine import geometry as geo
from adaptive_engine import solver
from adaptive_engine.model import (
    Blocker,
    Board,
    ChangeSet,
    Contract,
    EdgeSeg,
    Keepout,
    Outline,
    Pad,
    Part,
    Request,
    Rules,
    Track,
    TrackChange,
    Via,
    ViaMove,
)

MM = 1_000_000
C = 200_000  # courtyard clearance


def mm(v):
    return int(round(v * MM))


def rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def make_part(uuid, x0, y0, x1, y1, locked=False, courtyard=True, net="N2", inset=100_000):
    x0, y0, x1, y1 = mm(x0), mm(y0), mm(x1), mm(y1)
    cy = (rect(x0, y0, x1, y1),) if courtyard else ()
    centre = ((x0 + x1) // 2, (y0 + y1) // 2)
    pad = Pad(uuid + ".1", net, centre, rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset))
    return Part(uuid=uuid, ref=uuid, position=centre, courtyard=cy, locked=locked, pads=(pad,))


def rect_outline(x0, y0, x1, y1, **edge_kw):
    edges = {
        "e_top": EdgeSeg("e_top", (x0, y0), (x1, y0)),
        "e_right": EdgeSeg("e_right", (x1, y0), (x1, y1)),
        "e_bottom": EdgeSeg("e_bottom", (x1, y1), (x0, y1)),
        "e_left": EdgeSeg("e_left", (x0, y1), (x0, y0)),
    }
    for name, mid in edge_kw.items():
        e = edges[name]
        edges[name] = EdgeSeg(e.uuid, e.start, e.end, mid=mid)
    return Outline(polygons=(rect(x0, y0, x1, y1),), edges=tuple(edges.values()))


def make_rules(**kw):
    base = dict(
        clearance_nm=150_000,
        copper_edge_clearance_nm=300_000,
        courtyard_clearance_nm=C,
        fallback_margin_nm=250_000,
    )
    base.update(kw)
    return Rules(**base)


def make_board(parts, outline=None, rules=None, **kw):
    return Board(
        parts=tuple(parts),
        outline=outline if outline is not None else rect_outline(0, 0, mm(100), mm(100)),
        rules=rules if rules is not None else make_rules(),
        **kw,
    )


def req(drivers, dx, dy, **kw):
    d = {
        "driver": {"kind": "part", "uuids": list(drivers)},
        "displacement_nm": [dx, dy],
        "mode": "cascade",
        "copper": "fixed",
    }
    d.update(kw)
    return d


def ereq(edge_uuid, side, dx, dy, **kw):
    d = {
        "driver": {"kind": "edge", "edge_uuid": edge_uuid, "side": side},
        "displacement_nm": [dx, dy],
        "mode": "cascade",
        "copper": "fixed",
    }
    d.update(kw)
    return d


def moves(res):
    return {m.uuid: (m.dx, m.dy) for m in res.changeset.part_moves}


def blk(res):
    return [(b.uuid, b.kind) for b in res.report.blockers]


def place(uuid, a0, a1, b0, b1, direction, **kw):
    """Place a rectangle given as [a0, a1] along the motion axis and [b0, b1] across it."""
    if direction == (1, 0):
        box = (a0, b0, a1, b1)
    elif direction == (-1, 0):
        box = (100 - a1, b0, 100 - a0, b1)
    elif direction == (0, 1):
        box = (b0, a0, b1, a1)
    else:
        box = (b0, 100 - a1, b1, 100 - a0)
    return make_part(uuid, *box, **kw)


def chain3():
    """D(10..14) | 1 mm | A(15..19) | 3 mm | B(22..26) | 0.5 mm | C(26.5..30.5), all y 30..34."""
    return [
        make_part("D", 10, 30, 14, 34),
        make_part("A", 15, 30, 19, 34),
        make_part("B", 22, 30, 26, 34),
        make_part("C", 26.5, 30, 30.5, 34),
    ]


class MoveTests(unittest.TestCase):
    def test_single_driver_both_axes_and_directions(self):
        for dx, dy in ((5 * MM, 0), (-5 * MM, 0), (0, 5 * MM), (0, -5 * MM)):
            with self.subTest(d=(dx, dy)):
                res = solver.solve(make_board([make_part("A", 30, 30, 34, 34)]), req(["A"], dx, dy))
                self.assertEqual(res.report.status, "ok")
                self.assertEqual((res.report.requested_nm, res.report.achieved_nm), (5 * MM, 5 * MM))
                self.assertEqual(moves(res), {"A": (dx, dy)})
                self.assertEqual(res.report.blockers, ())

    def test_diagonal_displacement_is_exact_for_driver(self):
        res = solver.solve(make_board([make_part("A", 30, 30, 34, 34)]), req(["A"], 3 * MM, 4 * MM))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(res.report.requested_nm, 5 * MM)
        self.assertEqual(moves(res), {"A": (3 * MM, 4 * MM)})

    def test_diagonal_push_is_along_displacement(self):
        board = make_board([make_part("D", 10, 30, 14, 34), make_part("A", 15, 30, 19, 34)])
        res = solver.solve(board, req(["D"], 3 * MM, 4 * MM))
        self.assertEqual(res.report.status, "ok")
        dx, dy = moves(res)["A"]
        self.assertGreater(dx, 0)
        self.assertGreater(dy, 0)
        self.assertLessEqual(abs(4 * dx - 3 * dy), 4)

    def test_cascade_chain_of_three_all_directions(self):
        for direction in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            with self.subTest(direction=direction):
                parts = [
                    place("D", 10, 14, 30, 34, direction),
                    place("A", 15, 19, 30, 34, direction),
                    place("B", 22, 26, 30, 34, direction),
                    place("C", 26.5, 30.5, 30, 34, direction),
                    place("E", 60, 64, 30, 34, direction),
                ]
                d = (direction[0] * 5 * MM, direction[1] * 5 * MM)
                res = solver.solve(make_board(parts), req(["D"], *d))
                # gaps absorb motion: A moves 5-(1-0.2), B moves 4.2-(3-0.2), C moves 1.4-(0.5-0.2)
                expect = {"D": 5 * MM, "A": 4_200_000, "B": 1_400_000, "C": 1_100_000}
                self.assertEqual(res.report.status, "ok")
                self.assertEqual(
                    moves(res), {u: (direction[0] * v, direction[1] * v) for u, v in expect.items()}
                )
                self.assertEqual(res.wave, ("A", "B", "C"))
                self.assertEqual(res.report.moved_parts, ("A", "B", "C", "D"))
                self.assertNotIn("E", moves(res))

    def test_request_object_equals_dict(self):
        board = make_board(chain3())
        d = req(["D"], 5 * MM, 0)
        self.assertEqual(
            solver.solve(board, d).changeset.to_dict(),
            solver.solve(board, Request.from_dict(d)).changeset.to_dict(),
        )


class PermissionTests(unittest.TestCase):
    def pair(self, **kw):
        return make_board([make_part("D", 10, 30, 14, 34), make_part("A", 15, 30, 19, 34, **kw.pop("a", {}))], **kw)

    def check_blocked_by_a(self, res, kind):
        # gap 1 mm, clearance 0.2 mm: the driver travels 0.8 mm before A is hit
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 800_000, delta=1)
        self.assertEqual(blk(res), [("A", kind)])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 800_000, delta=1)
        self.assertEqual(list(moves(res)), ["D"])
        self.assertAlmostEqual(moves(res)["D"][0], 800_000, delta=1)

    def test_frozen_blocks(self):
        res = solver.solve(self.pair(), req(["D"], 5 * MM, 0, frozen=["A"]))
        self.check_blocked_by_a(res, "frozen")

    def test_native_lock_blocks(self):
        res = solver.solve(self.pair(a={"locked": True}), req(["D"], 5 * MM, 0))
        self.check_blocked_by_a(res, "locked")

    def test_contract_fixed_unlocked_part_blocks(self):
        board = self.pair(contract=Contract({"A": "fixed", "D": "movable"}))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.check_blocked_by_a(res, "contract")

    def test_contract_is_never_widened_by_all_movers_or_explicit_set(self):
        board = make_board(chain3()[:3], contract=Contract({"B": "fixed"}))
        for extra in ({}, {"mode": "explicit", "movers": ["A", "B"]}):
            with self.subTest(extra=extra):
                res = solver.solve(board, req(["D"], 5 * MM, 0, **extra))
                self.assertEqual(res.report.status, "partial")
                self.assertAlmostEqual(res.report.achieved_nm, 3_600_000, delta=1)
                self.assertEqual(blk(res), [("B", "contract")])
                self.assertAlmostEqual(moves(res)["A"][0], 2_800_000, delta=1)
                self.assertNotIn("B", moves(res))

    def test_native_lock_is_never_widened_by_explicit_set(self):
        board = self.pair(a={"locked": True})
        res = solver.solve(board, req(["D"], 5 * MM, 0, mode="explicit", movers=["A"]))
        self.check_blocked_by_a(res, "locked")

    def test_frozen_wins_over_movers(self):
        res = solver.solve(self.pair(), req(["D"], 5 * MM, 0, mode="explicit", movers=["A"], frozen=["A"]))
        self.check_blocked_by_a(res, "frozen")

    def test_locked_driver_blocks_everything(self):
        board = make_board([make_part("D", 10, 30, 14, 34, locked=True)])
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "blocked")
        self.assertEqual(res.report.blockers, (Blocker("D", "locked", 0),))
        self.assertTrue(res.changeset.is_empty())

    def test_frozen_and_contract_fixed_driver_block(self):
        board = make_board([make_part("D", 10, 30, 14, 34)], contract=Contract({"D": "fixed"}))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual((res.report.status, blk(res)), ("blocked", [("D", "contract")]))
        self.assertTrue(res.changeset.is_empty())
        res = solver.solve(make_board([make_part("D", 10, 30, 14, 34)]), req(["D"], 5 * MM, 0, frozen=["D"]))
        self.assertEqual((res.report.status, blk(res)), ("blocked", [("D", "frozen")]))
        self.assertTrue(res.changeset.is_empty())

    def test_explicit_set_honoured(self):
        board = make_board(chain3()[:3])
        res = solver.solve(board, req(["D"], 5 * MM, 0, mode="explicit", movers=["A"]))
        # B is not listed: it is an obstacle of kind 'frozen'; A can travel 3-0.2 = 2.8 mm, so D 3.6 mm
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 3_600_000, delta=1)
        self.assertEqual(blk(res), [("B", "frozen")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 3_600_000, delta=1)
        self.assertEqual(set(moves(res)), {"D", "A"})
        self.assertAlmostEqual(moves(res)["A"][0], 2_800_000, delta=1)
        # listing B lets the whole chain move
        res = solver.solve(board, req(["D"], 5 * MM, 0, mode="explicit", movers=["A", "B"]))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res), {"D": (5 * MM, 0), "A": (4_200_000, 0), "B": (1_400_000, 0)})

    def test_eligibility_radius_excludes_far_part(self):
        board = make_board(chain3()[:3])
        res = solver.solve(board, req(["D"], 5 * MM, 0, eligibility_nm=2 * MM))
        # A is 1 mm from the driver (eligible); B is 8 mm away (ineligible obstacle)
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 3_600_000, delta=1)
        self.assertEqual(blk(res), [("B", "ineligible")])
        self.assertNotIn("B", moves(res))
        res = solver.solve(board, req(["D"], 5 * MM, 0, eligibility_nm=20 * MM))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res)["B"], (1_400_000, 0))

    def test_priority_only_breaks_ties_at_equal_contact_distance(self):
        # spec section 3: priority parts are pushed first only at EQUAL contact; otherwise the
        # nearest contact comes first, even when a farther part is priority-listed
        parts = [
            make_part("D", 10, 30, 14, 50),
            make_part("a_far", 17, 30, 21, 34),  # 3 mm gap
            make_part("b_near", 15, 40, 19, 44),  # 1 mm gap
        ]
        board = make_board(parts)
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual(res.wave, ("b_near", "a_far"))  # first contact beats uuid order
        self.assertEqual(moves(res)["b_near"], (4_200_000, 0))
        self.assertEqual(moves(res)["a_far"], (2_200_000, 0))
        res = solver.solve(board, req(["D"], 5 * MM, 0, priority=["a_far"]))
        self.assertEqual(res.wave, ("b_near", "a_far"))  # priority does not jump the queue
        self.assertEqual(moves(res)["b_near"], (4_200_000, 0))
        self.assertEqual(moves(res)["a_far"], (2_200_000, 0))
        res = solver.solve(board, req(["D"], 5 * MM, 0, priority=["b_near"]))
        self.assertEqual(res.wave, ("b_near", "a_far"))

    def test_priority_wins_ties_at_equal_contact_distance(self):
        parts = [
            make_part("D", 10, 30, 14, 50),
            make_part("a_tie", 15, 30, 19, 34),  # both 1 mm from the driver
            make_part("b_tie", 15, 40, 19, 44),
        ]
        board = make_board(parts)
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual(res.wave, ("a_tie", "b_tie"))  # uuid order without priority
        res = solver.solve(board, req(["D"], 5 * MM, 0, priority=["b_tie"]))
        self.assertEqual(res.wave, ("b_tie", "a_tie"))
        self.assertEqual(moves(res)["a_tie"], (4_200_000, 0))
        self.assertEqual(moves(res)["b_tie"], (4_200_000, 0))


class LimitTests(unittest.TestCase):
    def test_stop_at_limit_reports_achieved_and_blockers(self):
        board = make_board([make_part("D", 10, 30, 14, 34), make_part("A", 15, 30, 19, 34)])
        res = solver.solve(board, req(["D"], 5 * MM, 0, frozen=["A"]))
        self.assertEqual((res.report.requested_nm, res.report.status), (5 * MM, "partial"))
        self.assertAlmostEqual(res.report.achieved_nm, 800_000, delta=1)
        self.assertEqual(blk(res), [("A", "frozen")])

    def test_zero_room_is_blocked_with_empty_changeset(self):
        board = make_board([make_part("D", 10, 30, 14, 34), make_part("A", 14.2, 30, 18, 34)])
        res = solver.solve(board, req(["D"], 5 * MM, 0, frozen=["A"]))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [("A", "frozen")])
        self.assertEqual(res.report.blockers[0].at_nm, 0)
        self.assertTrue(res.changeset.is_empty())

    def test_allow_partial_false_gives_empty_changeset(self):
        board = make_board([make_part("D", 10, 30, 14, 34), make_part("A", 15, 30, 19, 34)])
        res = solver.solve(board, req(["D"], 5 * MM, 0, frozen=["A"], allow_partial=False))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(res.report.requested_nm, 5 * MM)
        self.assertEqual(blk(res), [("A", "frozen")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 800_000, delta=1)
        self.assertTrue(res.changeset.is_empty())
        self.assertEqual(res.changeset.to_dict(), ChangeSet().to_dict())

    def test_not_monotonic_legal_gap_beyond_obstacle_is_not_reached(self):
        # The full 10 mm move ends in free space beyond a thin frozen obstacle, but the swept path
        # crosses it: the first illegal step bounds the result.
        parts = [make_part("D", 10, 30, 11, 34), make_part("ob", 12, 28, 12.5, 36)]
        board = make_board(parts)
        res = solver.solve(board, req(["D"], 10 * MM, 0, frozen=["ob"]))
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 800_000, delta=1)
        self.assertLess(res.report.achieved_nm, 10 * MM)
        self.assertEqual(blk(res), [("ob", "frozen")])
        self.assertEqual(moves(res)["D"][0], res.report.achieved_nm)
        res = solver.solve(board, req(["D"], 10 * MM, 0, frozen=["ob"], allow_partial=False))
        self.assertEqual(res.report.status, "blocked")
        self.assertTrue(res.changeset.is_empty())
        free = solver.solve(make_board(parts[:1]), req(["D"], 10 * MM, 0))
        self.assertEqual(free.report.status, "ok")

    def test_keepout_blocks_and_other_keepouts_do_not(self):
        ko = Keepout("ko1", rect(mm(20.0125), mm(25), mm(30), mm(40)), no_footprints=True)
        pour_only = Keepout("ko2", rect(mm(16), mm(25), mm(18), mm(40)), no_pour=True, no_tracks=True)
        board = make_board([make_part("D", 10, 30, 14, 34)], keepouts=(ko, pour_only))
        res = solver.solve(board, req(["D"], 10 * MM, 0))
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 6_012_500, delta=1)
        self.assertEqual(blk(res), [("ko1", "keepout")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 6_012_500, delta=1)

    def test_board_edge_clearance_blocks(self):
        board = make_board(
            [make_part("D", 10, 20, 14, 24)], outline=rect_outline(0, 0, mm(50), mm(50))
        )
        res = solver.solve(board, req(["D"], 40 * MM, 0))
        # pad right edge 13.9 mm; allowed up to 50 - 0.3 = 49.7 mm
        self.assertEqual(res.report.status, "partial")
        self.assertEqual(res.report.achieved_nm, 35_800_000)
        self.assertEqual(blk(res), [("D", "edge")])
        self.assertEqual(res.report.blockers[0].at_nm, 35_800_000)
        self.assertEqual(moves(res), {"D": (35_800_000, 0)})

    def test_missing_edge_clearance_rule_is_refused_not_guessed(self):
        board = make_board(
            [make_part("D", 10, 20, 14, 24)],
            outline=rect_outline(0, 0, mm(50), mm(50)),
            rules=make_rules(copper_edge_clearance_nm=None),
        )
        res = solver.solve(board, req(["D"], 40 * MM, 0))
        self.assertEqual((res.report.status, res.report.reject_reason), ("rejected", "missing_rule"))
        self.assertEqual(res.report.warnings, ("missing_rule:copper_edge_clearance",))
        self.assertTrue(res.changeset.is_empty())

    def test_missing_courtyard_uses_fallback_and_warns(self):
        parts = [make_part("D", 10, 30, 14, 34), make_part("M", 15, 30, 19, 34, courtyard=False)]
        res = solver.solve(make_board(parts), req(["D"], 5 * MM, 0))
        self.assertIn("missing_courtyard:M", res.report.warnings)
        # fallback courtyard = pad bbox (15.1..18.9) + 0.25 => left edge 14.85, pushed to 19.2
        self.assertEqual(moves(res)["M"], (4_350_000, 0))

    def test_fixed_copper_track_blocks(self):
        track = Track("t1", "N1", "F.Cu", (mm(17), mm(25)), (mm(17), mm(40)), 200_000)
        board = make_board([make_part("D", 10, 30, 14, 34)], tracks=(track,))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        # pad right 13.9 + l must stay 0.15 mm clear of the track edge at 16.9 mm
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 2_850_000, delta=1)
        self.assertEqual(blk(res), [("t1", "copper")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 2_850_000, delta=1)

    def test_fixed_copper_via_blocks(self):
        via = Via("v1", "N1", (mm(17), mm(32)), 600_000, 300_000, ("F.Cu", "B.Cu"))
        board = make_board([make_part("D", 10, 30, 14, 34)], vias=(via,))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertAlmostEqual(res.report.achieved_nm, 2_650_000, delta=1)
        self.assertEqual(blk(res), [("v1", "copper")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 2_650_000, delta=1)


def poly_outline(*rings):
    """Outline from rings given in millimetres: the first ring is the boundary, the rest are holes."""
    return Outline(polygons=tuple(tuple((mm(x), mm(y)) for x, y in ring) for ring in rings))


# Two arms (x 0..20 and 30..50) joined along the top; the notch x 20..30, y 15..40 is outside.
NOTCH_RING = [(0, 0), (50, 0), (50, 40), (30, 40), (30, 15), (20, 15), (20, 40), (0, 40)]
# A plain board with a cutout x 20..30, y 10..35 in the middle.
CUTOUT_RINGS = ([(0, 0), (50, 0), (50, 40), (0, 40)], [(20, 10), (30, 10), (30, 35), (20, 35)])


class SweptBoardEdgeTests(unittest.TestCase):
    """The board edge is swept like courtyards: no jumping over a notch or a cutout.

    D's pads span x 5.1..8.9, y 25.1..28.9, i.e. the left arm / left of the cutout. A move of 30 mm
    in +x lands it at x 35.1..38.9, which is legal on its own, but the path crosses the notch wall
    at x = 20 (and the one at x = 30) or the cutout.
    """

    def board(self, outline, margin=300_000):
        return make_board(
            [make_part("D", 5, 25, 9, 29)],
            outline=outline,
            rules=make_rules(copper_edge_clearance_nm=margin),
        )

    def check_stops_at_the_wall(self, outline, margin, expect):
        board = self.board(outline, margin)
        res = solver.solve(board, req(["D"], 30 * MM, 0))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("partial", expect))
        self.assertEqual(res.report.requested_nm, 30 * MM)
        self.assertEqual(blk(res), [("D", "edge")])
        self.assertEqual(res.report.blockers[0].at_nm, expect)
        self.assertEqual(moves(res), {"D": (expect, 0)})
        # without partial results the whole move is refused and the blocker still says where
        res = solver.solve(board, req(["D"], 30 * MM, 0, allow_partial=False))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [("D", "edge")])
        self.assertEqual(res.report.blockers[0].at_nm, expect)
        self.assertTrue(res.changeset.is_empty())

    def test_jump_across_a_concave_notch_is_blocked_before_the_notch(self):
        outline = poly_outline(NOTCH_RING)
        # pad right edge 8.9 + t may come to 20 - 0.3 (edge clearance) = 19.7: t = 10.8 mm
        self.check_stops_at_the_wall(outline, 300_000, 10_800_000)
        # the landing spot is legal on its own, so only a swept check can refuse the jump ...
        landing = make_board(
            [make_part("D", 35, 25, 39, 29)],
            outline=outline,
            rules=make_rules(copper_edge_clearance_nm=300_000),
        )
        res = solver.solve(landing, req(["D"], 1 * MM, 0))
        self.assertEqual((res.report.status, res.report.warnings), ("ok", ()))
        # ... and the same 30 mm move is fine on a board without the notch, or inside the arm
        plain = self.board(rect_outline(0, 0, mm(50), mm(40)))
        res = solver.solve(plain, req(["D"], 30 * MM, 0))
        self.assertEqual((res.report.status, moves(res)), ("ok", {"D": (30 * MM, 0)}))
        res = solver.solve(self.board(outline), req(["D"], 5 * MM, 0))
        self.assertEqual((res.report.status, moves(res)), ("ok", {"D": (5 * MM, 0)}))

    def test_jump_across_a_cutout_is_blocked_before_the_cutout(self):
        self.check_stops_at_the_wall(poly_outline(*CUTOUT_RINGS), 300_000, 10_800_000)

    def test_zero_edge_clearance_still_cannot_cross_a_notch_or_cutout(self):
        # touching is legal at zero clearance: the pad may come to rest against the wall at x = 20
        self.check_stops_at_the_wall(poly_outline(NOTCH_RING), 0, 11_100_000)
        self.check_stops_at_the_wall(poly_outline(*CUTOUT_RINGS), 0, 11_100_000)

    def test_part_that_already_violates_the_edge_cannot_jump_a_notch_either(self):
        # D sticks 0.05 mm into the left wall's clearance band (distance 0.25 < 0.3 mm) at the start;
        # moving along the wall may not make it worse and it still may not cross the notch
        board = make_board(
            [make_part("D", 0.15, 25, 4.15, 29)],  # pad x 0.25..4.05, 0.25 mm from the left edge
            outline=poly_outline(NOTCH_RING),
            rules=make_rules(copper_edge_clearance_nm=300_000),
        )
        res = solver.solve(board, req(["D"], 30 * MM, 0))
        self.assertIn("pre_existing_violation:edge:D", res.report.warnings)
        self.assertNotEqual(res.report.status, "ok")
        self.assertLess(res.report.achieved_nm, 30 * MM)


class ExactBoardEdgeTests(unittest.TestCase):
    """The board edge is exact for parts that already violate it, and the same rule applies to
    every part: full boundary edges (never pad vertices), no path sampling.

    Every pre-violating part below starts 0.05 mm inside the 0.3 mm copper-to-edge band of the left
    wall (pad 0.25 mm from it) and the default clearance is 0.3 mm, so a pad may only be 0.3 mm from
    anything else that it did not already touch.
    """

    MARGIN = 300_000

    @staticmethod
    def moved_pad(board, res, uuid="D"):
        dx, dy = moves(res).get(uuid, (0, 0))
        return geo.polygon_translate(board.part(uuid).pads[0].polygon, dx, dy)

    def assert_stops_at(self, board, request_dx, expect, ring, uuid="D"):
        """Blocked by the board edge exactly ``expect`` nm along +x; the pad ends 0.3 mm from ``ring``."""
        res = solver.solve(board, req([uuid], request_dx, 0))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("partial", expect))
        self.assertEqual(res.report.requested_nm, request_dx)
        self.assertEqual(blk(res), [(uuid, "edge")])
        self.assertEqual(res.report.blockers[0].at_nm, expect)
        self.assertEqual(moves(res), {uuid: (expect, 0)})
        self.assertGreaterEqual(
            geo.polygon_distance2(self.moved_pad(board, res, uuid), ring), self.MARGIN * self.MARGIN
        )
        partial = res
        # without partial results the whole move is refused and the blocker still says where
        res = solver.solve(board, req([uuid], request_dx, 0, allow_partial=False))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [(uuid, "edge")])
        self.assertEqual(res.report.blockers[0].at_nm, expect)
        self.assertTrue(res.changeset.is_empty())
        return partial

    def test_pre_violating_part_cannot_swallow_a_cutout(self):
        # reviewer's example: the pad (x 0.25..4.05, y 25.1..28.9) ends at x 18.25..22.05 after the
        # full +18 mm, covering the whole cutout x 20..21, y 26..28 with every vertex far from every
        # boundary; vertex depths and 16 path samples all stay negative, so the old code said "ok"
        cutout = rect(mm(20), mm(26), mm(21), mm(28))
        outline = poly_outline([(0, 0), (50, 0), (50, 40), (0, 40)], [(20, 26), (21, 26), (21, 28), (20, 28)])
        board = make_board([make_part("D", 0.15, 25, 4.15, 29)], outline=outline)
        res = solver.solve(board, req(["D"], 18 * MM, 0))
        self.assertIn("pre_existing_violation:edge:D", res.report.warnings)
        self.assertNotEqual(res.report.status, "ok")
        self.assertEqual(blk(res), [("D", "edge")])
        # pad right edge 4.05 + t may come to 20 - 0.3 = 19.7: t = 15.65 mm
        self.assertEqual((res.report.status, res.report.achieved_nm), ("partial", 15_650_000))
        self.assertEqual(res.report.blockers[0].at_nm, 15_650_000)
        pad = self.moved_pad(board, res)
        self.assertEqual(pad[0], (250_000 + 15_650_000, mm(25.1)))
        self.assertFalse(geo.polygons_intersect(pad, cutout))
        self.assertEqual(geo.forbidden_overlap_areas(outline.polygons, pad), (0, 0))
        self.assertTrue(geo.region_contains(outline.polygons, pad, self.MARGIN))
        # the same move is fine for a pad that cannot reach the cutout
        res = solver.solve(board, req(["D"], 15 * MM, 0))
        self.assertEqual((res.report.status, moves(res)), ("ok", {"D": (15 * MM, 0)}))

    def test_notch_narrower_than_the_pad_is_blocked(self):
        # 1 mm wide notch x 42.75..43.75 (y 15..40), pad 2 mm wide: the pad covers the notch for a
        # while, with both vertex columns on the board. The 16 old path samples are 4 mm apart and
        # none of them sees the notch, and the 64 mm landing spot is legal.
        notch = rect(mm(42.75), mm(15), mm(43.75), mm(40))
        ring = [(0, 0), (100, 0), (100, 40), (43.75, 40), (43.75, 15), (42.75, 15), (42.75, 40), (0, 40)]
        outline = poly_outline(ring)
        cases = (
            ("pre-violating", make_part("D", 0.15, 25, 2.35, 29), 64 * MM, 40_200_000),
            ("legal start", make_part("D", 5, 25, 7.2, 29), 60 * MM, 35_350_000),
        )
        for name, part, dx, expect in cases:
            with self.subTest(case=name):
                board = make_board([part], outline=outline)
                res = self.assert_stops_at(board, dx, expect, notch)
                if name == "pre-violating":
                    self.assertIn("pre_existing_violation:edge:D", res.report.warnings)
                landing = make_board([part.translated(dx, 0)], outline=outline)
                res = solver.solve(landing, req(["D"], 1 * MM, 0))
                self.assertEqual(res.report.status, "ok")  # the far side is legal on its own

    def test_long_move_across_a_narrow_slot_is_blocked(self):
        # the pads (0.5 mm) are narrower than the 1 mm slot x 42.5..43.5, y 20..35: their vertices
        # are never inside the slot at any of the old 4 mm samples
        slot = rect(mm(42.5), mm(20), mm(43.5), mm(35))
        outline = poly_outline([(0, 0), (100, 0), (100, 40), (0, 40)], [(42.5, 20), (43.5, 20), (43.5, 35), (42.5, 35)])
        cases = (
            ("pre-violating", make_part("D", 0.15, 25, 0.85, 29), 64 * MM, 41_450_000),
            ("legal start", make_part("D", 5, 25, 5.7, 29), 60 * MM, 36_600_000),
        )
        for name, part, dx, expect in cases:
            with self.subTest(case=name):
                board = make_board([part], outline=outline)
                res = self.assert_stops_at(board, dx, expect, slot)
                self.assertEqual(
                    geo.forbidden_overlap_areas(outline.polygons, self.moved_pad(board, res, "D")), (0, 0)
                )
                landing = make_board([part.translated(dx, 0)], outline=outline)
                res = solver.solve(landing, req(["D"], 1 * MM, 0))
                self.assertEqual(res.report.status, "ok")

    def test_edge_connector_may_retreat_and_slide_but_not_go_deeper(self):
        # connector pad x 49..51, y 20.1..23.9 overhangs the 50 mm wide board by 1 mm
        board = make_board(
            [make_part("C", 48.9, 20, 51.1, 24)], outline=rect_outline(0, 0, mm(50), mm(40))
        )
        for dx, dy in ((-3 * MM, 0), (-500_000, 0), (-1 * MM, 0), (0, 5 * MM), (-1 * MM, 3 * MM)):
            with self.subTest(move=(dx, dy)):  # inwards (even while still sticking out) or along the wall
                res = solver.solve(board, req(["C"], dx, dy))
                self.assertEqual((res.report.status, moves(res)), ("ok", {"C": (dx, dy)}))
                self.assertIn("pre_existing_violation:edge:C", res.report.warnings)
        for dx, dy in ((1 * MM, 0), (10_000, 0), (1 * MM, -1 * MM)):
            with self.subTest(move=(dx, dy)):  # further out stays blocked
                res = solver.solve(board, req(["C"], dx, dy))
                self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
                self.assertEqual(blk(res), [("C", "edge")])
                self.assertTrue(res.changeset.is_empty())
        # retreating all the way across the board is still stopped by the opposite wall
        res = solver.solve(board, req(["C"], -60 * MM, 0))
        self.assertEqual((res.report.status, blk(res)), ("partial", [("C", "edge")]))
        self.assertEqual(res.report.achieved_nm, 48_700_000)  # pad left 49.0 - t >= 0.3

    def test_pre_violating_part_may_slide_along_the_wall_without_getting_deeper(self):
        board = make_board(
            [make_part("D", 0.15, 25, 4.15, 29)], outline=rect_outline(0, 0, mm(50), mm(40))
        )  # pad 0.25 mm from the left wall
        res = solver.solve(board, req(["D"], 0, 5 * MM))
        self.assertEqual((res.report.status, moves(res)), ("ok", {"D": (0, 5 * MM)}))
        self.assertIn("pre_existing_violation:edge:D", res.report.warnings)
        res = solver.solve(board, req(["D"], 5 * MM, 0))  # away from the wall
        self.assertEqual((res.report.status, moves(res)), ("ok", {"D": (5 * MM, 0)}))
        res = solver.solve(board, req(["D"], -100_000, 0))  # 0.05 mm closer to the wall: deeper
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [("D", "edge")])
        res = solver.solve(board, req(["D"], 5 * MM, -5 * MM))  # diagonal: away from the wall
        self.assertEqual(res.report.status, "ok")
        res = solver.solve(board, req(["D"], 0, 12 * MM))  # along the wall into the bottom wall
        self.assertEqual((res.report.status, res.report.achieved_nm), ("partial", 10_800_000))
        self.assertEqual(blk(res), [("D", "edge")])

    def test_pre_violating_part_does_not_get_a_free_pass_at_other_walls(self):
        # the 0.05 mm violation of the left wall does not loosen the 0.3 mm rule at the notch wall
        outline = poly_outline(NOTCH_RING)
        board = make_board([make_part("D", 0.15, 25, 4.15, 29)], outline=outline)
        res = solver.solve(board, req(["D"], 30 * MM, 0))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("partial", 15_650_000))
        self.assertEqual(blk(res), [("D", "edge")])

    def test_edge_drag_pushes_a_pre_violating_part_exactly_far_enough(self):
        # pad x 48.1..51.9 sticks out 1.9 mm; the edge comes in by 10 mm: the push keeps it at 1.9 mm
        board = make_board(
            [make_part("P", 48, 20, 52, 24), make_part("Q", 40, 30, 44, 34)],
            outline=rect_outline(0, 0, mm(50), mm(50)),
        )
        res = solver.solve_edge(board, ereq("e_right", "right", -10 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res)["P"], (-10 * MM, 0))
        moved = geo.polygon_translate(board.part("P").pads[0].polygon, -10 * MM, 0)
        new_outline = (rect(0, 0, mm(40), mm(50)),)
        self.assertEqual(geo.forbidden_overlap_areas(new_outline, moved)[0], mm(1.9) * mm(3.8))

    def test_no_vertex_depth_or_sampled_path_logic_remains(self):
        self.assertFalse(hasattr(solver, "_DEPTH_SAMPLES"))
        self.assertFalse(hasattr(solver, "_DEPTH_EPS"))
        for name in ("edge_depth", "_start_depth", "_path_depth_ok", "_path_inside"):
            self.assertFalse(hasattr(solver._Ctx, name), name)


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, res, code):
        self.assertEqual(res.report.status, "rejected")
        self.assertEqual(res.report.reject_reason, code)
        self.assertTrue(res.changeset.is_empty())

    def test_unknown_uuid_rejected_everywhere(self):
        board = make_board(chain3())
        self.assert_rejected(solver.solve(board, req(["nope"], 5 * MM, 0)), "unknown_uuid")
        for key in ("movers", "frozen", "priority"):
            with self.subTest(key=key):
                extra = {key: ["nope"]}
                if key == "movers":
                    extra["mode"] = "explicit"
                self.assert_rejected(solver.solve(board, req(["D"], 5 * MM, 0, **extra)), "unknown_uuid")

    def test_duplicate_uuid_rejected(self):
        board = make_board(chain3())
        self.assert_rejected(solver.solve(board, req(["D", "D"], 5 * MM, 0)), "duplicate_uuid")
        for key in ("movers", "frozen", "priority"):
            with self.subTest(key=key):
                self.assert_rejected(
                    solver.solve(board, req(["D"], 5 * MM, 0, **{key: ["A", "A"]})), "duplicate_uuid"
                )

    def test_explicit_without_movers_and_zero_displacement_rejected(self):
        board = make_board(chain3())
        self.assert_rejected(
            solver.solve(board, req(["D"], 5 * MM, 0, mode="explicit")), "explicit_without_movers"
        )
        self.assert_rejected(solver.solve(board, req(["D"], 0, 0)), "no_displacement")

    def test_malformed_request_rejected_with_code(self):
        res = solver.solve(make_board(chain3()), {"driver": {"kind": "part", "uuids": ["D"]}})
        self.assertEqual(res.report.status, "rejected")
        self.assertEqual(res.report.reject_reason, "bad_displacement")
        self.assertTrue(res.changeset.is_empty())

    def test_solve_forwards_edge_requests(self):
        board = make_board([], outline=rect_outline(0, 0, mm(50), mm(50)))
        d = ereq("e_right", "right", -10 * MM, 0)
        self.assertEqual(
            solver.solve(board, d).changeset.to_dict(), solver.solve_edge(board, d).changeset.to_dict()
        )


class DeterminismTests(unittest.TestCase):
    def snapshot(self, res):
        return (res.changeset.to_dict(), res.report.to_dict(), res.wave)

    def test_repeatable_and_independent_of_part_order(self):
        parts = chain3() + [make_part("E", 60, 30, 64, 34), make_part("F", 40, 60, 44, 64)]
        request = req(["D"], 5 * MM, 0, frozen=["F"])
        base = self.snapshot(solver.solve(make_board(parts), request))
        self.assertEqual(base, self.snapshot(solver.solve(make_board(parts), request)))
        for order in (parts[::-1], parts[2:] + parts[:2], [parts[i] for i in (3, 0, 4, 2, 5, 1)]):
            self.assertEqual(base, self.snapshot(solver.solve(make_board(order), request)))
        self.assertEqual(base[1]["status"], "ok")

    def test_blocked_result_is_deterministic_too(self):
        parts = chain3() + [make_part("Z", 20, 28, 21, 36)]
        request = req(["D"], 5 * MM, 0, frozen=["Z", "B"])
        base = self.snapshot(solver.solve(make_board(parts), request))
        self.assertEqual(base, self.snapshot(solver.solve(make_board(parts[::-1]), request)))


class PreExistingViolationTests(unittest.TestCase):
    """No exemption: a violation present at the start never gets worse, and is reported."""

    OBSTACLE_KIND = {
        "frozen": "frozen",
        "locked": "locked",
        "contract": "contract",
        "ineligible": "ineligible",
        "explicit": "frozen",  # a part outside the explicit mover set is an obstacle of kind frozen
    }

    def obstacle_boards(self, a_x0, a_x1, names):
        d = make_part("D", 10, 30, 14, 34)

        def a(**kw):
            return make_part("A", a_x0, 30, a_x1, 34, **kw)

        boards = {
            "frozen": (make_board([d, a()]), dict(frozen=["A"])),
            "locked": (make_board([d, a(locked=True)]), {}),
            "contract": (make_board([d, a()], contract=Contract({"A": "fixed"})), {}),
            "ineligible": (make_board([d, a()]), dict(eligibility_nm=50_000)),
            "explicit": (
                make_board([d, a(), make_part("X", 60, 60, 64, 64)]),
                dict(mode="explicit", movers=["X"]),
            ),
        }
        return {n: boards[n] for n in names}

    def check_blocks_toward_and_allows_away(self, boards):
        for name, (board, extra) in boards.items():
            with self.subTest(obstacle=name):
                res = solver.solve(board, req(["D"], 5 * MM, 0, **extra))
                self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
                self.assertEqual(blk(res), [("A", self.OBSTACLE_KIND[name])])
                self.assertTrue(res.changeset.is_empty())
                self.assertIn("pre_existing_violation:courtyard:A:D", res.report.warnings)
                self.assertIn({"kind": "courtyard", "uuids": ["A", "D"]}, res.pre_existing)
                away = solver.solve(board, req(["D"], -5 * MM, 0, **extra))
                self.assertEqual(away.report.status, "ok")
                self.assertEqual(moves(away), {"D": (-5 * MM, 0)})

    def test_obstacle_already_inside_clearance_still_blocks(self):
        # gap 0.1 mm < 0.2 mm clearance at the start: moving closer is illegal, moving away is fine
        boards = self.obstacle_boards(14.1, 18, list(self.OBSTACLE_KIND))
        self.check_blocks_toward_and_allows_away(boards)

    def test_obstacle_already_overlapped_still_blocks_and_cannot_be_crossed(self):
        # 1 mm overlap at the start: the mover may not go deeper, so it can never pass through
        boards = self.obstacle_boards(13, 17, ["frozen", "locked", "contract", "explicit"])
        self.check_blocks_toward_and_allows_away(boards)

    def test_overlapping_movable_parts_are_never_driven_deeper(self):
        ahead = [make_part("D", 10, 30, 14, 34), make_part("M", 13, 30, 17, 34)]  # 1 mm overlap
        res = solver.solve(make_board(ahead), req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        # M has to travel with D, so the overlap stays 1 mm (it is not 6 mm)
        self.assertEqual(moves(res), {"D": (5 * MM, 0), "M": (5 * MM, 0)})
        self.assertEqual(res.wave, ("M",))
        self.assertIn("pre_existing_violation:courtyard:D:M", res.report.warnings)
        # M behind D: D simply moves away from it, nothing needs to be pushed
        behind = [make_part("D", 13, 30, 17, 34), make_part("M", 10, 30, 14, 34)]
        res = solver.solve(make_board(behind), req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res), {"D": (5 * MM, 0)})

    def test_keepout_already_overlapped_still_blocks(self):
        ko = Keepout("ko1", rect(mm(13), mm(28), mm(20), mm(36)), no_footprints=True)
        board = make_board([make_part("D", 10, 30, 14, 34)], keepouts=(ko,))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(res.report.blockers, (Blocker("ko1", "keepout", 0),))
        self.assertIn("pre_existing_violation:keepout:D:ko1", res.report.warnings)
        away = solver.solve(board, req(["D"], -5 * MM, 0))
        self.assertEqual((away.report.status, moves(away)), ("ok", {"D": (-5 * MM, 0)}))

    def test_fixed_track_already_touching_still_blocks(self):
        track = Track("t1", "N1", "F.Cu", (mm(14), mm(25)), (mm(14), mm(40)), 200_000)
        board = make_board([make_part("D", 10, 30, 14, 34)], tracks=(track,))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [("t1", "copper")])
        self.assertIn("pre_existing_violation:copper:D:t1", res.report.warnings)
        away = solver.solve(board, req(["D"], -5 * MM, 0))
        self.assertEqual((away.report.status, moves(away)), ("ok", {"D": (-5 * MM, 0)}))

    def test_part_already_outside_the_outline_may_not_move_further_out(self):
        board = make_board(
            [make_part("D", 46, 20, 52, 24)], outline=rect_outline(0, 0, mm(50), mm(50))
        )  # pads reach x = 51.9 mm, past the 50 mm outline
        res = solver.solve(board, req(["D"], 3 * MM, 0))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [("D", "edge")])
        self.assertIn("pre_existing_violation:edge:D", res.report.warnings)
        for dx in (-1 * MM, -5 * MM):  # moving inwards is fine, even while still outside
            away = solver.solve(board, req(["D"], dx, 0))
            self.assertEqual((away.report.status, moves(away)), ("ok", {"D": (dx, 0)}))

    def test_edge_drag_does_not_skip_a_part_that_is_already_outside(self):
        board = make_board(
            [make_part("P", 48, 20, 52, 24)], outline=rect_outline(0, 0, mm(50), mm(50))
        )
        res = solver.solve_edge(board, ereq("e_right", "right", -10 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        # P sticks out 1.9 mm already; it is pushed so that it never sticks out further than that
        self.assertEqual(moves(res), {"P": (-10 * MM, 0)})
        self.assertIn("pre_existing_violation:edge:P", res.report.warnings)
        res = solver.solve_edge(board, ereq("e_right", "right", -10 * MM, 0, frozen=["P"]))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(res.report.blockers, (Blocker("P", "frozen", 0),))
        self.assertTrue(res.changeset.is_empty())


class ZeroCourtyardClearanceTests(unittest.TestCase):
    def board(self):
        return make_board(
            [make_part("D", 10, 30, 14, 34), make_part("A", 15, 30, 19, 34)],
            rules=make_rules(courtyard_clearance_nm=0),
        )

    def test_exact_touch_is_legal_and_not_forced_to_one_nm(self):
        res = solver.solve(self.board(), req(["D"], 1 * MM, 0, frozen=["A"]))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("ok", 1 * MM))
        self.assertEqual(moves(res), {"D": (1 * MM, 0)})
        self.assertEqual(res.report.warnings, ())
        res = solver.solve(self.board(), req(["D"], 2 * MM, 0, frozen=["A"]))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("partial", 1 * MM))
        self.assertEqual(blk(res), [("A", "frozen")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 1 * MM, delta=1)
        self.assertEqual(moves(res), {"D": (1 * MM, 0)})

    def test_cascade_pushes_by_exactly_the_gap(self):
        res = solver.solve(self.board(), req(["D"], 3 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res), {"D": (3 * MM, 0), "A": (2 * MM, 0)})  # touching, not 1 nm apart

    def test_parts_that_touch_at_the_start_are_not_a_violation(self):
        board = make_board(
            [make_part("D", 10, 30, 14, 34), make_part("A", 14, 30, 18, 34)],
            rules=make_rules(courtyard_clearance_nm=0),
        )
        res = solver.solve(board, req(["D"], -2 * MM, 0, frozen=["A"]))
        self.assertEqual((res.report.status, moves(res)), ("ok", {"D": (-2 * MM, 0)}))
        self.assertEqual(res.report.warnings, ())
        self.assertEqual(res.pre_existing, ())


class MissingRuleTests(unittest.TestCase):
    def assert_missing(self, res, name):
        self.assertEqual((res.report.status, res.report.reject_reason), ("rejected", "missing_rule"))
        self.assertEqual(res.report.warnings, ("missing_rule:" + name,))
        self.assertEqual(res.report.achieved_nm, 0)
        self.assertTrue(res.changeset.is_empty())

    def track(self):
        return Track("t1", "N1", "F.Cu", (mm(17), mm(25)), (mm(17), mm(40)), 200_000)

    def test_fixed_copper_needs_a_pad_to_copper_clearance(self):
        part = make_part("D", 10, 30, 14, 34)  # pad net N2, track net N1
        board = make_board([part], tracks=(self.track(),), rules=make_rules(clearance_nm=None))
        self.assert_missing(solver.solve(board, req(["D"], 5 * MM, 0)), "clearance")
        # a rule for exactly the pair that matters is enough, and the solve runs on it
        rules = make_rules(clearance_nm=None, pair_clearance={("N1", "N2"): 150_000})
        res = solver.solve(make_board([part], tracks=(self.track(),), rules=rules), req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "partial")
        self.assertAlmostEqual(res.report.achieved_nm, 2_850_000, delta=1)
        # nothing to check without copper: no refusal
        res = solver.solve(make_board([part], rules=make_rules(clearance_nm=None)), req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "ok")

    def test_adapt_mode_needs_the_clearance_for_the_copper_it_may_re_aim(self):
        board = make_board(
            [make_part("D", 10, 30, 14, 34)],
            tracks=(self.track(),),
            rules=make_rules(clearance_nm=None),
        )
        self.assert_missing(solver.solve(board, req(["D"], 5 * MM, 0, copper="adapt")), "clearance")

    def test_edge_clearance_is_needed_when_the_board_has_an_outline(self):
        board = make_board(
            [make_part("P", 42, 20, 46, 24)],
            outline=rect_outline(0, 0, mm(50), mm(50)),
            rules=make_rules(copper_edge_clearance_nm=None),
        )
        self.assert_missing(solver.solve(board, req(["P"], -1 * MM, 0)), "copper_edge_clearance")
        self.assert_missing(
            solver.solve_edge(board, ereq("e_right", "right", -10 * MM, 0)), "copper_edge_clearance"
        )
        no_outline = make_board(
            [make_part("P", 42, 20, 46, 24)],
            outline=Outline(),
            rules=make_rules(copper_edge_clearance_nm=None),
        )
        res = solver.solve(no_outline, req(["P"], -1 * MM, 0))
        self.assertEqual((res.report.status, res.report.warnings), ("ok", ()))


class FixedCopperAttachedTrackTests(unittest.TestCase):
    def board(self):
        part = make_part("D", 10, 30, 14, 34, net="N")  # pad x 10.1..13.9, centre (12, 32)
        track = Track("t1", "N", "F.Cu", (mm(12), mm(32)), (mm(30), mm(32)), 200_000)
        return make_board([part], tracks=(track,))

    def test_move_that_keeps_the_track_on_the_pad_is_fine(self):
        for d in ((1 * MM, 0), (0, 1_500_000), (-1_000_000, 0)):
            with self.subTest(d=d):
                res = solver.solve(self.board(), req(["D"], *d))
                self.assertEqual((res.report.status, moves(res)), ("ok", {"D": d}))
                self.assertEqual(res.report.warnings, ())

    def test_move_that_would_leave_the_track_unconnected_is_blocked(self):
        res = solver.solve(self.board(), req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "partial")
        self.assertEqual(res.report.achieved_nm, 1_900_000)  # the track end is on the pad edge
        self.assertEqual(res.report.blockers, (Blocker("t1", "copper", 1_900_000),))
        self.assertEqual(moves(res), {"D": (1_900_000, 0)})
        res = solver.solve(self.board(), req(["D"], 5 * MM, 0, allow_partial=False))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(blk(res), [("t1", "copper")])
        self.assertTrue(res.changeset.is_empty())

    def test_via_in_the_pad_stays_attached_too(self):
        part = make_part("D", 10, 30, 14, 34, net="N")
        via = Via("v1", "N", (mm(12), mm(32)), 600_000, 300_000, ("F.Cu", "B.Cu"))
        board = make_board([part], vias=(via,))
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.achieved_nm, 1_900_000)
        self.assertEqual(blk(res), [("v1", "copper")])


class RealCopperAdaptTests(unittest.TestCase):
    """solve() with copper='adapt' against the real adapt_copper (no mocks)."""

    def board(self):
        p1 = make_part("P1", 19, 19, 21, 21, net="N")  # pad centre (20, 20)
        p2 = make_part("P2", 29, 19, 31, 21, net="N")  # pad centre (30, 20)
        t1 = Track("t1", "N", "F.Cu", (mm(20), mm(20)), (mm(30), mm(20)), 250_000)
        x1 = Track("x1", "X", "F.Cu", (mm(22), mm(21)), (mm(22), mm(23)), 250_000)
        return make_board([p1, p2], tracks=(t1, x1), rules=make_rules(clearance_nm=200_000))

    def test_partial_back_off_to_the_largest_legal_displacement(self):
        board = self.board()
        res = solver.solve(board, req(["P1"], 0, 5 * MM, copper="adapt"))
        # the re-aimed diagonal (20, 20+L) -> (20+L, 20) must stay 0.2 + 0.125 + 0.125 mm from the
        # other-net track end (22, 21): (3 mm - L) / sqrt(2) >= 0.45 mm, so L <= 2_363_603 nm
        self.assertEqual(res.report.status, "partial")
        self.assertEqual(res.report.requested_nm, 5 * MM)
        self.assertEqual(res.report.achieved_nm, 2_363_603)
        self.assertEqual(res.report.blockers, (Blocker("t1", "leg_blocked", 2_375_000),))
        self.assertEqual(moves(res), {"P1": (0, 2_363_603)})
        self.assertEqual(
            res.changeset.track_changes,
            (
                TrackChange(
                    "t1",
                    (mm(20), mm(20) + 2_363_603),
                    (mm(30), mm(20)),
                    bend=(mm(20) + 2_363_603, mm(20)),
                ),
            ),
        )
        self.assertEqual(res.report.legs[0]["kind"], "reaim")

    def test_without_partial_nothing_changes(self):
        res = solver.solve(self.board(), req(["P1"], 0, 5 * MM, copper="adapt", allow_partial=False))
        self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
        self.assertEqual(res.report.blockers, (Blocker("t1", "leg_blocked", 5 * MM),))
        self.assertTrue(res.changeset.is_empty())

    def test_small_move_adapts_the_track(self):
        res = solver.solve(self.board(), req(["P1"], 0, 1 * MM, copper="adapt"))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(len(res.changeset.track_changes), 1)
        self.assertEqual(res.changeset.track_changes[0].start, (mm(20), mm(21)))


def fake_copper(**kw):
    base = dict(ok=True, track_changes=(), via_moves=(), blockers=(), legs=(), warnings=())
    base.update(kw)
    return SimpleNamespace(**base)


class CopperHookTests(unittest.TestCase):
    def setUp(self):
        self.board = make_board([make_part("D", 10, 30, 14, 34)])
        self.tc = TrackChange("t1", (1, 2), (3, 4))
        self.vm = ViaMove("v1", 5 * MM, 0)

    def test_hook_contract_and_merge(self):
        result = fake_copper(
            track_changes=(self.tc,), via_moves=(self.vm,), legs=({"leg": "L1"},), warnings=("copper_w",)
        )
        with mock.patch.object(solver, "_adapt_copper_hook", return_value=result) as hook:
            res = solver.solve(self.board, req(["D"], 5 * MM, 0, copper="adapt"))
        hook.assert_called_once()
        args = hook.call_args.args
        self.assertIs(args[0], self.board)
        self.assertEqual(args[1], {"D": (5 * MM, 0)})  # full displacement vector per moved part
        self.assertTrue(args[2])
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(res.changeset.track_changes, (self.tc,))
        self.assertEqual(res.changeset.via_moves, (self.vm,))
        self.assertEqual(res.report.legs, ({"leg": "L1"},))
        self.assertIn("copper_w", res.report.warnings)

    def test_hook_blockers_back_off_to_largest_legal_scale(self):
        def fake(board, moves_, allow_partial=True):
            if moves_["D"][0] > 3 * MM:
                return fake_copper(ok=False, blockers=(Blocker("t1", "leg_blocked", None),))
            return fake_copper(track_changes=(self.tc,))

        with mock.patch.object(solver, "_adapt_copper_hook", side_effect=fake):
            res = solver.solve(self.board, req(["D"], 5 * MM, 0, copper="adapt"))
            self.assertEqual(res.report.status, "partial")
            self.assertEqual(res.report.achieved_nm, 3 * MM)
            self.assertEqual(res.report.blockers, (Blocker("t1", "leg_blocked", 3_025_000),))
            self.assertEqual(moves(res), {"D": (3 * MM, 0)})
            self.assertEqual(res.changeset.track_changes, (self.tc,))
            res = solver.solve(self.board, req(["D"], 5 * MM, 0, copper="adapt", allow_partial=False))
            self.assertEqual((res.report.status, res.report.achieved_nm), ("blocked", 0))
            self.assertEqual(res.report.blockers, (Blocker("t1", "leg_blocked", 5 * MM),))
            self.assertTrue(res.changeset.is_empty())

    def test_tuned_leg_blocker_blocks(self):
        block = fake_copper(ok=False, blockers=(Blocker("t9", "tuned_leg", 123),))
        with mock.patch.object(solver, "_adapt_copper_hook", return_value=block):
            res = solver.solve(self.board, req(["D"], 5 * MM, 0, copper="adapt", allow_partial=False))
        self.assertEqual(res.report.status, "blocked")
        self.assertEqual(res.report.blockers, (Blocker("t9", "tuned_leg", 123),))
        self.assertTrue(res.changeset.is_empty())

    def test_fixed_copper_never_calls_hook(self):
        with mock.patch.object(solver, "_adapt_copper_hook") as hook:
            res = solver.solve(self.board, req(["D"], 5 * MM, 0, copper="fixed"))
        hook.assert_not_called()
        self.assertEqual(res.report.status, "ok")


def edge_dict(res, uuid):
    return {e["uuid"]: (tuple(e["start"]), tuple(e["end"])) for e in res.changeset.to_dict()["edge_changes"]}[uuid]


class EdgeTests(unittest.TestCase):
    def board50(self, parts, **kw):
        return make_board(parts, outline=rect_outline(0, 0, mm(50), mm(50)), **kw)

    def test_all_four_sides_shrink_and_push(self):
        m = mm
        cases = {
            "right": dict(edge="e_right", disp=(-10 * MM, 0), part=(42, 20, 46, 24), move=(-6_200_000, 0),
                          changes={"e_bottom": ((m(40), m(50)), (0, m(50))),
                                   "e_right": ((m(40), 0), (m(40), m(50))),
                                   "e_top": ((0, 0), (m(40), 0))}),
            "left": dict(edge="e_left", disp=(10 * MM, 0), part=(4, 20, 8, 24), move=(6_200_000, 0),
                         changes={"e_bottom": ((m(50), m(50)), (m(10), m(50))),
                                  "e_left": ((m(10), m(50)), (m(10), 0)),
                                  "e_top": ((m(10), 0), (m(50), 0))}),
            "top": dict(edge="e_top", disp=(0, 10 * MM), part=(20, 4, 24, 8), move=(0, 6_200_000),
                        changes={"e_left": ((0, m(50)), (0, m(10))),
                                 "e_right": ((m(50), m(10)), (m(50), m(50))),
                                 "e_top": ((0, m(10)), (m(50), m(10)))}),
            "bottom": dict(edge="e_bottom", disp=(0, -10 * MM), part=(20, 42, 24, 46), move=(0, -6_200_000),
                           changes={"e_bottom": ((m(50), m(40)), (0, m(40))),
                                    "e_left": ((0, m(40)), (0, 0)),
                                    "e_right": ((m(50), 0), (m(50), m(40)))}),
        }
        for side, c in cases.items():
            with self.subTest(side=side):
                board = self.board50([make_part("P", *c["part"])])
                res = solver.solve_edge(board, ereq(c["edge"], side, *c["disp"]))
                self.assertEqual(res.report.status, "ok")
                self.assertEqual((res.report.requested_nm, res.report.achieved_nm), (10 * MM, 10 * MM))
                self.assertEqual(moves(res), {"P": c["move"]})
                self.assertEqual(sorted(e.uuid for e in res.changeset.edge_changes), sorted(c["changes"]))
                for uuid, (start, end) in c["changes"].items():
                    self.assertEqual(edge_dict(res, uuid), (start, end), uuid)

    def test_pushed_parts_push_their_neighbours(self):
        parts = [
            make_part("P", 42, 20, 46, 24),
            make_part("Q", 37, 20, 41, 24),  # 1 mm left of P, pad also outside the new outline
            make_part("R", 30, 20, 34, 24),  # 3 mm left of Q, only reached through Q
            make_part("S", 5, 20, 9, 24),
        ]
        res = solver.solve_edge(self.board50(parts), ereq("e_right", "right", -10 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res), {"P": (-6_200_000, 0), "Q": (-5_400_000, 0), "R": (-2_600_000, 0)})
        self.assertEqual(res.wave, ("P", "Q", "R"))

    def test_scalar_form_and_growth(self):
        board = self.board50([make_part("P", 20, 20, 24, 24)])
        res = solver.solve_edge(board, ereq("e_bottom", "bottom", -2 * MM, 0))  # scalar along +y normal
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(edge_dict(res, "e_bottom"), ((mm(50), mm(48)), (0, mm(48))))
        res = solver.solve_edge(board, ereq("e_right", "right", 5 * MM, 0))  # outward: grows
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(edge_dict(res, "e_right"), ((mm(55), 0), (mm(55), mm(50))))
        self.assertEqual(moves(res), {})

    def test_frozen_like_part_blocks_shrink_at_exact_limit(self):
        # F's pad right edge is 45.9 mm: the right edge may come in to 50 - 0.3 - ... = limit 3.8 mm
        part = make_part("F", 42, 20, 46, 24)
        cases = {
            "frozen": (self.board50([part]), dict(frozen=["F"])),
            "locked": (self.board50([make_part("F", 42, 20, 46, 24, locked=True)]), {}),
            "contract": (self.board50([part], contract=Contract({"F": "fixed"})), {}),
            "ineligible": (self.board50([part]), dict(eligibility_nm=1 * MM)),
        }
        for kind, (board, extra) in cases.items():
            with self.subTest(kind=kind):
                res = solver.solve_edge(board, ereq("e_right", "right", -10 * MM, 0, **extra))
                self.assertEqual(res.report.status, "partial")
                self.assertEqual(res.report.achieved_nm, 3_800_000)
                self.assertEqual(res.report.blockers, (Blocker("F", kind, 3_800_000),))
                self.assertEqual(moves(res), {})
                self.assertEqual(edge_dict(res, "e_right"), ((mm(46.2), 0), (mm(46.2), mm(50))))
                res = solver.solve_edge(board, ereq("e_right", "right", -10 * MM, 0, allow_partial=False, **extra))
                self.assertEqual(res.report.status, "blocked")
                self.assertTrue(res.changeset.is_empty())

    def test_frozen_neighbour_limits_a_pushed_chain(self):
        parts = [make_part("P", 42, 20, 46, 24), make_part("G", 30, 20, 34, 24)]
        res = solver.solve_edge(self.board50(parts), ereq("e_right", "right", -12 * MM, 0, frozen=["G"]))
        # P may be pushed 42 - 34.2 = 7.8 mm, so the edge stops at 50 - 38.1 - 0.3 = 11.6 mm in
        self.assertEqual(res.report.status, "partial")
        self.assertEqual(res.report.achieved_nm, 11_600_000)
        self.assertEqual([(b.uuid, b.kind) for b in res.report.blockers], [("G", "frozen")])
        self.assertAlmostEqual(res.report.blockers[0].at_nm, 11_600_000, delta=1)
        self.assertEqual(moves(res), {"P": (-7_800_000, 0)})
        self.assertEqual(edge_dict(res, "e_right"), ((mm(38.4), 0), (mm(38.4), mm(50))))

    def assert_rejected(self, board, request, code):
        res = solver.solve_edge(board, request)
        self.assertEqual((res.report.status, res.report.reject_reason), ("rejected", code))
        self.assertTrue(res.changeset.is_empty())

    def test_edge_collapse_rejected(self):
        board = self.board50([make_part("P", 20, 20, 24, 24)])
        self.assert_rejected(board, ereq("e_right", "right", -60 * MM, 0), "edge_collapse")
        self.assert_rejected(board, ereq("e_right", "right", -50 * MM, 0), "edge_collapse")  # zero area

    def test_arc_edge_rejected(self):
        board = make_board([], outline=rect_outline(0, 0, mm(50), mm(50), e_right=(mm(52), mm(25))))
        self.assert_rejected(board, ereq("e_right", "right", -10 * MM, 0), "arc_edge")
        board = make_board([], outline=rect_outline(0, 0, mm(50), mm(50), e_top=(mm(25), -mm(2))))
        self.assert_rejected(board, ereq("e_right", "right", -10 * MM, 0), "arc_edge")  # adjacent arc

    def test_self_intersection_rejected(self):
        pts = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]
        pts = [(mm(x), mm(y)) for x, y in pts]
        edges = tuple(EdgeSeg(f"l{i}", pts[i], pts[(i + 1) % 6]) for i in range(6))
        board = make_board([], outline=Outline(polygons=(tuple(pts),), edges=edges))
        # l2 is the notch edge (10,5)-(5,5); pulling it 7 mm toward -y crosses the top edge
        self.assert_rejected(board, ereq("l2", "bottom", 0, -7 * MM), "self_intersection")

    def test_other_typed_rejections(self):
        board = self.board50([make_part("P", 20, 20, 24, 24)])
        self.assert_rejected(board, ereq("e_right", "left", -10 * MM, 0), "side_mismatch")
        self.assert_rejected(board, ereq("nope", "right", -10 * MM, 0), "unknown_uuid")
        self.assert_rejected(board, ereq("e_right", "right", 0, 0), "no_displacement")
        slanted = Outline(
            polygons=(((0, 0), (mm(50), 0), (mm(55), mm(50)), (0, mm(50))),),
            edges=(
                EdgeSeg("e_top", (0, 0), (mm(50), 0)),
                EdgeSeg("e_right", (mm(50), 0), (mm(55), mm(50))),
                EdgeSeg("e_bottom", (mm(55), mm(50)), (0, mm(50))),
                EdgeSeg("e_left", (0, mm(50)), (0, 0)),
            ),
        )
        self.assert_rejected(make_board([], outline=slanted), ereq("e_right", "right", -10 * MM, 0), "not_axis_aligned")
        outline = rect_outline(0, 0, mm(50), mm(50))
        two_outers = Outline(polygons=outline.polygons + (rect(mm(60), 0, mm(70), mm(10)),), edges=outline.edges)
        self.assert_rejected(make_board([], outline=two_outers), ereq("e_right", "right", -10 * MM, 0), "unsupported_topology")

    def test_cutout_touching_the_moved_region_rejected(self):
        outline = rect_outline(0, 0, mm(50), mm(50))
        near = Outline(polygons=outline.polygons + (rect(mm(44), mm(20), mm(48), mm(24)),), edges=outline.edges)
        self.assert_rejected(make_board([], outline=near), ereq("e_right", "right", -10 * MM, 0), "cutout_touches_edge")
        far = Outline(polygons=outline.polygons + (rect(mm(10), mm(20), mm(14), mm(24)),), edges=outline.edges)
        res = solver.solve_edge(make_board([], outline=far), ereq("e_right", "right", -10 * MM, 0))
        self.assertEqual(res.report.status, "ok")

    def test_edge_result_is_deterministic(self):
        parts = [make_part("P", 42, 20, 46, 24), make_part("Q", 37, 20, 41, 24), make_part("R", 30, 20, 34, 24)]
        request = ereq("e_right", "right", -10 * MM, 0)
        a = solver.solve_edge(self.board50(parts), request)
        b = solver.solve_edge(self.board50(parts[::-1]), request)
        self.assertEqual((a.changeset.to_dict(), a.report.to_dict(), a.wave), (b.changeset.to_dict(), b.report.to_dict(), b.wave))


class BoardSideTests(unittest.TestCase):
    """Courtyards interact only on the same side, or when either part has through-hole pads."""

    def on_side(self, part, side, through=False):
        return dataclasses.replace(part, side=side, through=through)

    def test_bottom_smd_part_is_not_pushed_by_top_driver(self):
        board = make_board([make_part("D", 10, 30, 14, 34), self.on_side(make_part("A", 15, 30, 19, 34), "B")])
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        self.assertEqual(moves(res), {"D": (5 * MM, 0)})

    def test_through_hole_part_on_other_side_is_pushed(self):
        board = make_board([make_part("D", 10, 30, 14, 34), self.on_side(make_part("A", 15, 30, 19, 34), "B", through=True)])
        res = solver.solve(board, req(["D"], 5 * MM, 0))
        self.assertEqual(res.report.status, "ok")
        self.assertGreater(moves(res)["A"][0], 0)


if __name__ == "__main__":
    unittest.main()
