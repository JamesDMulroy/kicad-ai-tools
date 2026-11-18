"""Unit tests for adaptive_engine.copper (stdlib unittest, synthetic in-memory boards)."""

import dataclasses
import math
import random
import unittest
from collections import Counter

from adaptive_engine.copper import (
    CopperResult,
    adapt_copper,
    apply_changes,
    connectivity_signature,
)
from adaptive_engine.geometry import point_in_polygon
from adaptive_engine.model import (
    Blocker,
    Board,
    Keepout,
    Outline,
    Pad,
    Part,
    Rules,
    Track,
    TrackChange,
    Via,
    ViaMove,
)

MM = 1_000_000
W = 250_000
N = "N"
FCU, BCU = "F.Cu", "B.Cu"
RULES = Rules(clearance_nm=200_000, copper_edge_clearance_nm=300_000)


def rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


def sq(cx, cy, h):
    return rect(cx - h, cy - h, cx + h, cy + h)


BIG = Outline(polygons=(rect(-50 * MM, -50 * MM, 50 * MM, 50 * MM),))


def mkpart(uuid, cx, cy, net=N, court=1_500_000, half=300_000):
    courtyard = (sq(cx, cy, court),) if court else ()
    return Part(
        uuid=uuid,
        ref=uuid.upper(),
        position=(cx, cy),
        courtyard=courtyard,
        pads=(Pad(uuid + ".1", net, (cx, cy), sq(cx, cy, half)),),
    )


def mktrack(uuid, s, e, net=N, layer=FCU, **kw):
    return Track(uuid=uuid, net=net, layer=layer, start=s, end=e, width=W, **kw)


def mkvia(uuid, pos, net=N):
    return Via(uuid=uuid, net=net, pos=pos, diameter=800_000, drill=400_000, layers=(FCU, BCU))


def mkboard(parts, tracks, vias=(), keepouts=(), rules=RULES, outline=BIG):
    return Board(
        parts=tuple(parts),
        tracks=tuple(tracks),
        vias=tuple(vias),
        outline=outline,
        keepouts=tuple(keepouts),
        rules=rules,
    )


def moved_parts_board(board, moves):
    parts = tuple(
        p.translated(*moves[p.uuid]) if p.uuid in moves else p for p in board.parts
    )
    return dataclasses.replace(board, parts=parts)


def after_board(board, moves, result):
    return apply_changes(moved_parts_board(board, moves), result)


def legal_dir(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    return (dx, dy) != (0, 0) and (dx == 0 or dy == 0 or abs(dx) == abs(dy))


def reaim_case(extra_tracks=(), extra_parts=(), **kw):
    """P1 (moves) and P2 on net N joined by a horizontal 10 mm track."""
    parts = [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 0)] + list(extra_parts)
    tracks = [mktrack("t1", (0, 0), (10 * MM, 0))] + list(extra_tracks)
    return mkboard(parts, tracks, **kw)


REAIM_MOVES = {"p1": (2 * MM, 1 * MM)}
REAIM_BEND = (3 * MM, 0)  # derived by hand, see test_horizontal_segment_diagonal_move


def junction_case(oy=0, pre=""):
    parts = [
        mkpart(pre + "p1", 0, oy),
        mkpart(pre + "p2", 10 * MM, oy),
        mkpart(pre + "p3", 5 * MM, oy + 5 * MM),
    ]
    tracks = [
        mktrack(pre + "t1", (0, oy), (5 * MM, oy)),
        mktrack(pre + "t2", (5 * MM, oy), (10 * MM, oy)),
        mktrack(pre + "t3", (5 * MM, oy), (5 * MM, oy + 5 * MM)),
    ]
    return parts, tracks


def serpentine_case(group=None):
    """P1 (0,0) to P2 (0,4 mm) through an 8-segment meander with equal 1 mm pitch."""
    pts = [(0, 0), (3, 0), (3, 1), (0, 1), (0, 2), (3, 2), (3, 3), (0, 3), (0, 4)]
    pts = [(x * MM, y * MM) for x, y in pts]
    tracks = [
        mktrack("s%d" % (i + 1), pts[i], pts[i + 1], group=group) for i in range(len(pts) - 1)
    ]
    return mkboard([mkpart("p1", 0, 0), mkpart("p2", 0, 4 * MM)], tracks)


def mm_points(pts):
    return [(int(round(x * MM)), int(round(y * MM))) for x, y in pts]


def chamfer_meander_case(group=None):
    """P1 (0,0) to P2 (2.5, 4 mm): three lanes 2 mm apart, every corner is two 45 degree turns."""
    pts = mm_points(
        [(0, 0), (2.5, 0), (3, 0.5), (3, 1.5), (2.5, 2), (0.5, 2), (0, 2.5), (0, 3.5), (0.5, 4), (2.5, 4)]
    )
    tracks = [mktrack("s%d" % (i + 1), pts[i], pts[i + 1], group=group) for i in range(len(pts) - 1)]
    return mkboard([mkpart("p1", 0, 0), mkpart("p2", pts[-1][0], pts[-1][1])], tracks)


def chamfer_staircase_case():
    """Same chamfered corners but the turns alternate one by one: a staircase, not a meander."""
    pts = mm_points(
        [(0, 0), (1.5, 0), (2, 0.5), (2, 1.5), (2.5, 2), (3.5, 2), (4, 2.5), (4, 3.5), (4.5, 4), (6, 4)]
    )
    tracks = [mktrack("s%d" % (i + 1), pts[i], pts[i + 1]) for i in range(len(pts) - 1)]
    return mkboard([mkpart("p1", 0, 0), mkpart("p2", pts[-1][0], pts[-1][1])], tracks)


def arc_meander_case():
    """P1 (0,0) to P2 (3, 6 mm): three lanes 3 mm apart, every corner is a quarter arc (radius 1 mm)."""
    p = lambda x, y: (int(round(x * MM)), int(round(y * MM)))  # noqa: E731
    segs = [
        ("m1", p(0, 0), p(3, 0), None),
        ("m2", p(3, 0), p(4, 1), p(3.707107, 0.292893)),
        ("m3", p(4, 1), p(4, 2), None),
        ("m4", p(4, 2), p(3, 3), p(3.707107, 2.707107)),
        ("m5", p(3, 3), p(1, 3), None),
        ("m6", p(1, 3), p(0, 4), p(0.292893, 3.292893)),
        ("m7", p(0, 4), p(0, 5), None),
        ("m8", p(0, 5), p(1, 6), p(0.292893, 5.707107)),
        ("m9", p(1, 6), p(3, 6), None),
    ]
    tracks = [mktrack(u, s, e, mid=m) for u, s, e, m in segs]
    return mkboard([mkpart("p1", 0, 0), mkpart("p2", 3 * MM, 6 * MM)], tracks)


def pad_touch_nets(board):
    """``{(part, pad): frozenset(nets)}``: the nets of the tracks and vias whose copper touches each
    pad, read from the board geometry only (the pad's own declared net is NOT consulted)."""
    out = {}
    for part in board.parts:
        for pad in part.pads:
            nets = set()
            for t in board.tracks:
                if any(point_in_polygon(p, pad.polygon) for p in (t.start, t.end)):
                    nets.add(t.net)
            for via in board.vias:
                if point_in_polygon(via.pos, pad.polygon):
                    nets.add(via.net)
            out[(part.uuid, pad.uuid)] = frozenset(nets)
    return out


class CopperTestCase(unittest.TestCase):
    def assertConnectivityPreserved(self, board, moves, result):
        """Same connectivity partition, same unconnected-pad count and the same net per pad.

        The net of a pad is derived from the copper that touches it in the post-change board
        (``pad_touch_nets``), so a detached pad or a track re-aimed onto another net's pad fails.
        """
        after = after_board(board, moves, result)
        sig_before = connectivity_signature(board)
        sig_after = connectivity_signature(after)
        self.assertEqual(sig_before, sig_after)
        self.assertEqual(
            Counter(net for net, _ in sig_before), Counter(net for net, _ in sig_after)
        )
        nets_before = pad_touch_nets(board)
        nets_after = pad_touch_nets(after)
        self.assertEqual(nets_before, nets_after)
        self.assertEqual(
            sum(1 for n in nets_before.values() if not n), sum(1 for n in nets_after.values() if not n)
        )
        declared = {(p.uuid, q.uuid): q.net for p in after.parts for q in p.pads}
        for key, nets in nets_after.items():
            self.assertLessEqual(nets, {declared[key]}, key)
        return after


class RigidTests(CopperTestCase):
    def setUp(self):
        self.board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 0)],
            [
                mktrack("t1", (0, 0), (5 * MM, 0), layer=FCU),
                mktrack("t2", (5 * MM, 0), (10 * MM, 0), layer=BCU),
            ],
            [mkvia("v1", (5 * MM, 0))],
        )
        self.v = (1 * MM, 2 * MM)
        self.moves = {"p1": self.v, "p2": self.v}

    def test_fully_enclosed_leg_with_via_translates_rigidly(self):
        r = adapt_copper(self.board, self.moves)
        self.assertTrue(r.ok)
        self.assertEqual(
            r.track_changes,
            (
                TrackChange("t1", (1 * MM, 2 * MM), (6 * MM, 2 * MM)),
                TrackChange("t2", (6 * MM, 2 * MM), (11 * MM, 2 * MM)),
            ),
        )
        self.assertEqual(r.via_moves, (ViaMove("v1", 1 * MM, 2 * MM),))
        self.assertEqual(r.blockers, ())
        self.assertEqual(r.warnings, ())
        self.assertEqual(
            r.legs,
            (
                {
                    "net": N,
                    "track_uuids": ["t1", "t2"],
                    "kind": "rigid",
                    "length_before_nm": 10 * MM,
                    "length_after_nm": 10 * MM,
                    "tuned": False,
                },
            ),
        )
        after = self.assertConnectivityPreserved(self.board, self.moves, r)
        self.assertEqual(after.via("v1").pos, (6 * MM, 2 * MM))

    def test_only_one_part_moved_reaims_and_keeps_via_in_place(self):
        moves = {"p1": self.v}
        r = adapt_copper(self.board, moves)
        self.assertTrue(r.ok)
        # Only the segment touching the moved pad changes; the via and t2 stay.
        self.assertEqual(
            r.track_changes,
            (TrackChange("t1", (1 * MM, 2 * MM), (5 * MM, 0), bend=(3 * MM, 0)),),
        )
        self.assertEqual(r.via_moves, ())
        self.assertEqual(r.legs[0]["kind"], "reaim")
        self.assertConnectivityPreserved(self.board, moves, r)

    def test_via_enclosed_by_moved_courtyard_moves_with_part(self):
        board = mkboard(
            [mkpart("p1", 0, 0, court=3 * MM), mkpart("p9", 15 * MM, 10 * MM, net="M")],
            [mktrack("t1", (0, 0), (2 * MM, 0))],
            [mkvia("v1", (2 * MM, 0))],
        )
        moves = {"p1": (1 * MM, 1 * MM)}
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        self.assertEqual(
            r.track_changes, (TrackChange("t1", (1 * MM, 1 * MM), (3 * MM, 1 * MM)),)
        )
        self.assertEqual(r.via_moves, (ViaMove("v1", 1 * MM, 1 * MM),))
        self.assertEqual(r.legs[0]["kind"], "rigid")
        self.assertConnectivityPreserved(board, moves, r)

    def test_via_outside_moved_courtyard_is_a_fixed_terminal(self):
        board = mkboard(
            [mkpart("p1", 0, 0, court=1 * MM)],
            [mktrack("t1", (0, 0), (2 * MM, 0))],
            [mkvia("v1", (2 * MM, 0))],
        )
        r = adapt_copper(board, {"p1": (0, 1 * MM)})
        self.assertEqual(r.via_moves, ())
        self.assertEqual(r.legs[0]["kind"], "reaim")
        self.assertEqual(r.track_changes[0].end, (2 * MM, 0))

    def test_nothing_moves_nothing_changes(self):
        for moves in ({}, {"p1": (0, 0)}):
            r = adapt_copper(self.board, moves)
            self.assertEqual(r, CopperResult(True, (), (), (), (), ()))
        r = adapt_copper(self.board, {"zz": (1, 1)})
        self.assertEqual(r.warnings, ("unknown_part:zz",))
        self.assertTrue(r.ok)


class ReaimTests(CopperTestCase):
    def test_horizontal_segment_diagonal_move(self):
        board = reaim_case()
        r = adapt_copper(board, REAIM_MOVES)
        # Fixed end A = (10,0). Candidate bends keeping the horizontal class at A:
        # (1,0) total 9+1.41, (2,0) total 8+1 -> 9, (3,0) total 7+1.41 = 8.41 (shortest).
        self.assertTrue(r.ok)
        self.assertEqual(
            r.track_changes,
            (TrackChange("t1", (2 * MM, 1 * MM), (10 * MM, 0), bend=REAIM_BEND),),
        )
        c = r.track_changes[0]
        self.assertTrue(legal_dir(c.start, c.bend))
        self.assertTrue(legal_dir(c.bend, c.end))
        # the part at the fixed end keeps the original horizontal direction
        self.assertEqual(c.bend[1], c.end[1])
        self.assertEqual(r.via_moves, ())
        self.assertEqual(r.blockers, ())
        self.assertConnectivityPreserved(board, REAIM_MOVES, r)

    def test_lengths_before_after(self):
        r = adapt_copper(reaim_case(), REAIM_MOVES)
        self.assertEqual(
            r.legs,
            (
                {
                    "net": N,
                    "track_uuids": ["t1"],
                    "kind": "reaim",
                    "length_before_nm": 10 * MM,
                    "length_after_nm": int(round(7 * MM + math.hypot(MM, MM))),
                    "tuned": False,
                },
            ),
        )
        self.assertEqual(r.legs[0]["length_after_nm"], 8_414_214)

    def test_45_degree_segment(self):
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 10 * MM)],
            [mktrack("t1", (0, 0), (10 * MM, 10 * MM))],
        )
        moves = {"p1": (0, 3 * MM)}
        r = adapt_copper(board, moves)
        # A = (10,10) keeps the 45 degree class; the horizontal pad-side leg wins on length.
        self.assertTrue(r.ok)
        self.assertEqual(
            r.track_changes,
            (TrackChange("t1", (0, 3 * MM), (10 * MM, 10 * MM), bend=(3 * MM, 3 * MM)),),
        )
        c = r.track_changes[0]
        self.assertTrue(legal_dir(c.start, c.bend))
        self.assertTrue(legal_dir(c.bend, c.end))
        self.assertEqual(
            r.legs[0]["length_after_nm"], int(round(3 * MM + math.hypot(7 * MM, 7 * MM)))
        )
        self.assertEqual(r.legs[0]["length_before_nm"], int(round(math.hypot(10 * MM, 10 * MM))))
        self.assertConnectivityPreserved(board, moves, r)

    def test_no_bend_when_original_is_neither_axis_nor_45(self):
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 3 * MM)],
            [mktrack("t1", (0, 0), (10 * MM, 3 * MM))],
        )
        moves = {"p1": (1 * MM, 0)}
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        self.assertEqual(
            r.track_changes, (TrackChange("t1", (1 * MM, 0), (10 * MM, 3 * MM), bend=None),)
        )
        self.assertConnectivityPreserved(board, moves, r)

    def test_no_bend_when_reaimed_segment_is_still_axis_aligned(self):
        board = reaim_case()
        moves = {"p1": (1 * MM, 0)}
        r = adapt_copper(board, moves)
        self.assertEqual(
            r.track_changes, (TrackChange("t1", (1 * MM, 0), (10 * MM, 0), bend=None),)
        )

    def test_leg_stops_at_junction_and_only_pad_segment_changes(self):
        parts, tracks = junction_case()
        board = mkboard(parts, tracks)
        moves = {"p1": (0, 1 * MM)}
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        self.assertEqual(
            r.track_changes,
            (TrackChange("t1", (0, 1 * MM), (5 * MM, 0), bend=(1 * MM, 0)),),
        )
        c = r.track_changes[0]
        self.assertTrue(legal_dir(c.start, c.bend))
        self.assertTrue(legal_dir(c.bend, c.end))
        self.assertEqual([leg["track_uuids"] for leg in r.legs], [["t1"]])
        self.assertConnectivityPreserved(board, moves, r)

    def test_two_moved_pads_with_different_vectors_reaim_both_ends(self):
        board = reaim_case()
        moves = {"p1": (0, 1 * MM), "p2": (0, 2 * MM)}
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        c = r.track_changes[0]
        self.assertEqual((c.start, c.end), ((0, 1 * MM), (10 * MM, 2 * MM)))
        self.assertConnectivityPreserved(board, moves, r)


class BlockedTests(CopperTestCase):
    def assertBlockedNothingLost(self, board, moves, r, uuid="t1"):
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker(uuid, "leg_blocked", None),))
        self.assertEqual([leg["kind"] for leg in r.legs], ["blocked"])
        after = after_board(board, moves, r)
        before_ids = {t.uuid for t in board.tracks}
        after_ids = {t.uuid for t in after.tracks}
        self.assertTrue(before_ids <= after_ids)
        self.assertLessEqual(after_ids - before_ids, {uuid + "#b"})
        for t in board.tracks:
            if t.uuid != uuid:
                self.assertEqual(after.track(t.uuid), t)
        self.assertEqual(r.via_moves, ())

    def test_blocked_by_other_net_track(self):
        board = reaim_case(
            [mktrack("x1", (2_500_000, 800_000), (2_500_000, 3 * MM), net="X")]
        )
        r = adapt_copper(board, REAIM_MOVES)
        self.assertBlockedNothingLost(board, REAIM_MOVES, r)
        # the would-be change stays listed for diagnostics
        self.assertEqual(r.track_changes[0].bend, REAIM_BEND)

    def test_other_net_track_far_away_does_not_block(self):
        board = reaim_case([mktrack("x1", (2_500_000, 3 * MM), (2_500_000, 6 * MM), net="X")])
        self.assertTrue(adapt_copper(board, REAIM_MOVES).ok)

    def test_own_net_copper_is_never_a_violation(self):
        board = reaim_case(
            [mktrack("n2", (2_500_000, 800_000), (2_500_000, 3 * MM), net=N)]
        )
        self.assertTrue(adapt_copper(board, REAIM_MOVES).ok)

    def test_blocked_by_other_net_track_on_other_layer_is_fine(self):
        board = reaim_case(
            [mktrack("x1", (2_500_000, 800_000), (2_500_000, 3 * MM), net="X", layer=BCU)]
        )
        self.assertTrue(adapt_copper(board, REAIM_MOVES).ok)

    def test_blocked_by_keepout_no_tracks(self):
        poly = rect(2_400_000, 400_000, 2_600_000, 600_000)
        for layers, expect_ok in ((("F.Cu",), False), ((), False), (("B.Cu",), True)):
            keepout = Keepout("k1", poly, layers=layers, no_tracks=True)
            board = reaim_case(keepouts=[keepout])
            r = adapt_copper(board, REAIM_MOVES)
            self.assertEqual(r.ok, expect_ok, layers)
            if not expect_ok:
                self.assertBlockedNothingLost(board, REAIM_MOVES, r)
        # a keepout that only forbids vias does not stop a track
        board = reaim_case(keepouts=[Keepout("k2", poly, no_vias=True)])
        self.assertTrue(adapt_copper(board, REAIM_MOVES).ok)

    def test_blocked_by_other_net_pad(self):
        board = reaim_case(
            extra_parts=[mkpart("p3", 2_500_000, 500_000, net="X", court=0, half=100_000)]
        )
        r = adapt_copper(board, REAIM_MOVES)
        self.assertBlockedNothingLost(board, REAIM_MOVES, r)

    def test_other_net_pad_of_a_moved_part_is_checked_at_its_moved_position(self):
        # P4 (net X) is at rest 5 mm away from the path, but moves into the re-aimed segment.
        p4 = mkpart("p4", 2_500_000, 5_500_000, net="X", court=0, half=100_000)
        board = reaim_case(extra_parts=[p4])
        moves = {"p1": REAIM_MOVES["p1"], "p4": (0, -5 * MM)}
        r = adapt_copper(board, moves)
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker("t1", "leg_blocked", None),))
        self.assertTrue(adapt_copper(board, REAIM_MOVES).ok)

    def test_blocked_by_other_net_via(self):
        board = reaim_case(extra_tracks=(), )
        board = dataclasses.replace(board, vias=(mkvia("vx", (2_500_000, 500_000), net="X"),))
        r = adapt_copper(board, REAIM_MOVES)
        self.assertBlockedNothingLost(board, REAIM_MOVES, r)

    def test_blocked_by_edge_clearance(self):
        outline = Outline(polygons=(rect(-10 * MM, -10 * MM, 15 * MM, 1_200_000),))
        board = reaim_case(outline=outline)
        r = adapt_copper(board, REAIM_MOVES)
        # new pad-side end at y = 1.0 mm is only 0.2 mm from the edge; needs 0.3 + 0.125 mm
        self.assertBlockedNothingLost(board, REAIM_MOVES, r)
        roomy = Outline(polygons=(rect(-10 * MM, -10 * MM, 15 * MM, 5 * MM),))
        self.assertTrue(adapt_copper(reaim_case(outline=roomy), REAIM_MOVES).ok)

    def test_locked_track_is_never_changed(self):
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 0)],
            [mktrack("t1", (0, 0), (10 * MM, 0), locked=True)],
        )
        r = adapt_copper(board, REAIM_MOVES)
        self.assertEqual(r.track_changes, ())
        self.assertEqual(r.blockers, (Blocker("t1", "leg_blocked", None),))
        self.assertIn("locked_track:t1", r.warnings)

    def test_arc_reaim_is_blocked(self):
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 0)],
            [mktrack("arc", (0, 0), (10 * MM, 0), mid=(5 * MM, 1 * MM))],
        )
        r = adapt_copper(board, REAIM_MOVES)
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker("arc", "leg_blocked", None),))
        self.assertIn("arc_reaim_unsupported", r.warnings)
        self.assertEqual(r.track_changes, ())
        self.assertEqual(r.legs[0]["kind"], "blocked")

    def test_arc_rigid_translation_is_blocked(self):
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 0)],
            [mktrack("arc", (0, 0), (10 * MM, 0), mid=(5 * MM, 1 * MM))],
        )
        v = (1 * MM, 1 * MM)
        r = adapt_copper(board, {"p1": v, "p2": v})
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker("arc", "leg_blocked", None),))
        self.assertIn("arc_translate_unsupported", r.warnings)
        self.assertEqual(r.track_changes, ())


def staircase_case(segments):
    """P1 (0,0) to P2 through ``segments`` equal 1 mm steps alternating +x, +y (a monotonic staircase)."""
    pts = [(0, 0)]
    for i in range(segments):
        x, y = pts[-1]
        pts.append((x + 1, y) if i % 2 == 0 else (x, y + 1))
    pts = [(x * MM, y * MM) for x, y in pts]
    tracks = [mktrack("s%d" % (i + 1), pts[i], pts[i + 1]) for i in range(segments)]
    return mkboard([mkpart("p1", 0, 0), mkpart("p2", pts[-1][0], pts[-1][1])], tracks)


class TunedTests(CopperTestCase):
    def test_serpentine_stretch_is_blocked_as_tuned_leg(self):
        board = serpentine_case()
        moves = {"p1": (-500_000, 0)}  # s1 grows from 3.0 to 3.5 mm: the leg is stretched
        r = adapt_copper(board, moves)
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker("s1", "tuned_leg", None),))
        self.assertEqual(r.track_changes, ())
        self.assertEqual(r.via_moves, ())
        leg = r.legs[0]
        self.assertEqual(leg["track_uuids"], ["s%d" % i for i in range(1, 9)])
        self.assertEqual(leg["kind"], "blocked")
        self.assertTrue(leg["tuned"])
        self.assertEqual(leg["reason"], "stretch")
        self.assertEqual(leg["length_before_nm"], 16 * MM)
        self.assertEqual(leg["length_after_nm"], 16_500_000)  # the stretch that was refused
        after = apply_changes(board, r)
        self.assertEqual(after.tracks, board.tracks)

    def test_serpentine_shortening_reaim_is_blocked_as_shape_change(self):
        board = serpentine_case()
        moves = {"p1": (500_000, 0)}  # s1 shrinks from 3.0 to 2.5 mm: still a shape change
        r = adapt_copper(board, moves)
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker("s1", "tuned_leg", None),))
        self.assertEqual(r.track_changes, ())
        leg = r.legs[0]
        self.assertEqual(leg["kind"], "blocked")
        self.assertTrue(leg["tuned"])
        self.assertEqual(leg["reason"], "shape change")
        self.assertEqual(leg["length_before_nm"], 16 * MM)
        self.assertEqual(leg["length_after_nm"], 15_500_000)
        self.assertEqual(apply_changes(board, r).tracks, board.tracks)

    def test_serpentine_rigid_translation_is_allowed_and_has_no_reason(self):
        board = serpentine_case()
        v = (700_000, -300_000)
        r = adapt_copper(board, {"p1": v, "p2": v})
        self.assertTrue(r.ok)
        self.assertEqual(r.blockers, ())
        self.assertEqual(r.legs[0]["kind"], "rigid")
        self.assertTrue(r.legs[0]["tuned"])
        self.assertNotIn("reason", r.legs[0])

    def test_group_id_track_is_protected(self):
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 10 * MM, 0)],
            [mktrack("t1", (0, 0), (10 * MM, 0), group="tp1")],
        )
        r = adapt_copper(board, {"p1": (0, 1 * MM)})
        self.assertFalse(r.ok)
        self.assertEqual(r.blockers, (Blocker("t1", "tuned_leg", None),))
        self.assertEqual(r.track_changes, ())
        self.assertTrue(r.legs[0]["tuned"])
        self.assertEqual(r.legs[0]["reason"], "stretch")  # 10 mm becomes 10.41 mm

    def test_monotonic_equal_staircase_is_not_tuned(self):
        # 4 and 5 equal 1 mm steps (+x, +y, +x, +y, ...): the bends alternate one by one, so there
        # is no U-turn at all and the chain is a staircase however regular it is (lead decision:
        # a meander needs >= 2 consecutive U-turns of two same-sign turns each)
        for segments in (4, 5, 6, 7):
            with self.subTest(segments=segments):
                board = staircase_case(segments)
                moves = {"p1": (0, 500_000)}
                r = adapt_copper(board, moves)
                self.assertTrue(r.ok)
                self.assertFalse(r.legs[0]["tuned"])
                self.assertNotIn("reason", r.legs[0])
                self.assertEqual([c.uuid for c in r.track_changes], ["s1"])
                self.assertConnectivityPreserved(board, moves, r)

    def test_staircase_with_chamfered_corners_is_not_tuned(self):
        board = chamfer_staircase_case()
        r = adapt_copper(board, {"p1": (0, 500_000)})
        self.assertFalse(r.legs[0]["tuned"])
        self.assertNotIn("tuned_leg", [b.kind for b in r.blockers])

    def test_chamfer_meander_is_tuned(self):
        # every corner is two 45 degree turns; the old detector wanted 90 degree bends only
        for group in (None, "tp1"):
            r = adapt_copper(chamfer_meander_case(group), {"p1": (-500_000, 0)})
            self.assertTrue(r.legs[0]["tuned"], group)
        # unequal lane pitch (2 mm and 2.5 mm) is no longer a meander
        pts = mm_points(
            [(0, 0), (2.5, 0), (3, 0.5), (3, 1.5), (2.5, 2), (0.5, 2), (0, 2.5), (0, 4), (0.5, 4.5), (2.5, 4.5)]
        )
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", pts[-1][0], pts[-1][1])],
            [mktrack("s%d" % (i + 1), pts[i], pts[i + 1]) for i in range(len(pts) - 1)],
        )
        self.assertFalse(adapt_copper(board, {"p1": (-500_000, 0)}).legs[0]["tuned"])

    def test_chamfer_meander_reaim_blocks_with_tuned_leg(self):
        board = chamfer_meander_case()
        for dx, reason in (
            (-500_000, "stretch"),  # s1 grows from 2.5 to 3.0 mm
            (500_000, "shape change"),  # s1 shrinks from 2.5 to 2.0 mm
        ):
            with self.subTest(dx=dx):
                r = adapt_copper(board, {"p1": (dx, 0)})
                self.assertFalse(r.ok)
                self.assertEqual(r.blockers, (Blocker("s1", "tuned_leg", None),))
                self.assertEqual(r.track_changes, ())
                leg = r.legs[0]
                self.assertEqual(leg["track_uuids"], ["s%d" % i for i in range(1, 10)])
                self.assertEqual((leg["kind"], leg["tuned"], leg["reason"]), ("blocked", True, reason))
                self.assertEqual(apply_changes(board, r).tracks, board.tracks)
        # a diagonal re-aim is blocked just the same
        r = adapt_copper(board, {"p1": (0, 300_000)})
        self.assertEqual(r.blockers, (Blocker("s1", "tuned_leg", None),))

    def test_chamfer_meander_translates_rigidly(self):
        board = chamfer_meander_case()
        v = (700_000, -300_000)
        moves = {"p1": v, "p2": v}
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        self.assertEqual(r.blockers, ())
        self.assertEqual(len(r.track_changes), 9)
        self.assertEqual((r.legs[0]["kind"], r.legs[0]["tuned"]), ("rigid", True))
        self.assertNotIn("reason", r.legs[0])
        self.assertConnectivityPreserved(board, moves, r)

    def test_arc_meander_is_tuned_and_reaim_blocks_with_tuned_leg(self):
        board = arc_meander_case()
        for dx in (-500_000, 500_000):
            with self.subTest(dx=dx):
                r = adapt_copper(board, {"p1": (dx, 0)})
                self.assertFalse(r.ok)
                # the arc segments are never touched; the straight segment at the pad is the blocker
                self.assertEqual(r.blockers, (Blocker("m1", "tuned_leg", None),))
                self.assertEqual(r.track_changes, ())
                leg = r.legs[0]
                self.assertEqual(leg["track_uuids"], ["m%d" % i for i in range(1, 10)])
                self.assertEqual((leg["kind"], leg["tuned"]), ("blocked", True))
                self.assertEqual(leg["reason"], "stretch" if dx < 0 else "shape change")
                self.assertNotIn("arc_reaim_unsupported", r.warnings)

    def test_same_sign_spiral_is_not_tuned(self):
        pts = [(0, 0), (5, 0), (5, 5), (1, 5), (1, 1), (4, 1)]  # every bend turns the same way
        pts = [(x * MM, y * MM) for x, y in pts]
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 4 * MM, MM)],
            [mktrack("c%d" % (i + 1), pts[i], pts[i + 1]) for i in range(5)],
        )
        r = adapt_copper(board, {"p1": (0, 200_000)})
        self.assertFalse(r.legs[0]["tuned"])
        self.assertTrue(r.ok)

    def test_true_serpentine_is_tuned(self):
        # alternating U-turns, 4+ bends, equal 1 mm pitch
        r = adapt_copper(serpentine_case(), {"p1": (-500_000, 0)})
        self.assertTrue(r.legs[0]["tuned"])
        # the smallest meander is 5 segments / 4 bends (one down-U and one up-U)
        pts = [(0, 0), (3, 0), (3, 1), (0, 1), (0, 2), (3, 2)]
        pts = [(x * MM, y * MM) for x, y in pts]
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 3 * MM, 2 * MM)],
            [mktrack("m%d" % (i + 1), pts[i], pts[i + 1]) for i in range(5)],
        )
        r = adapt_copper(board, {"p1": (-500_000, 0)})
        self.assertTrue(r.legs[0]["tuned"])
        self.assertEqual(r.blockers, (Blocker("m1", "tuned_leg", None),))
        # unequal pitch (1 mm and 1.5 mm) is no longer a serpentine
        pts = [(0, 0), (3, 0), (3, 1), (0, 1), (0, 2.5), (3, 2.5)]
        pts = [(int(x * MM), int(y * MM)) for x, y in pts]
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 3 * MM, 2_500_000)],
            [mktrack("m%d" % (i + 1), pts[i], pts[i + 1]) for i in range(5)],
        )
        r = adapt_copper(board, {"p1": (-500_000, 0)})
        self.assertFalse(r.legs[0]["tuned"])

    def test_tuned_chain_translates_rigidly(self):
        for board in (serpentine_case(), serpentine_case(group="tp1")):
            v = (1 * MM, 1 * MM)
            moves = {"p1": v, "p2": v}
            r = adapt_copper(board, moves)
            self.assertTrue(r.ok)
            self.assertEqual(r.blockers, ())
            self.assertEqual(len(r.track_changes), 8)
            for c in r.track_changes:
                t = board.track(c.uuid)
                self.assertEqual(c.start, (t.start[0] + v[0], t.start[1] + v[1]))
                self.assertEqual(c.end, (t.end[0] + v[0], t.end[1] + v[1]))
                self.assertIsNone(c.bend)
            leg = r.legs[0]
            self.assertEqual(leg["kind"], "rigid")
            self.assertTrue(leg["tuned"])
            self.assertEqual(leg["length_before_nm"], leg["length_after_nm"])
            self.assertConnectivityPreserved(board, moves, r)

    def test_unequal_staircase_is_not_tuned_and_may_be_reaimed(self):
        pts = [(0, 0), (3, 0), (3, 1), (5, 1), (5, 4)]
        pts = [(x * MM, y * MM) for x, y in pts]
        board = mkboard(
            [mkpart("p1", 0, 0), mkpart("p2", 5 * MM, 4 * MM)],
            [mktrack("s%d" % (i + 1), pts[i], pts[i + 1]) for i in range(4)],
        )
        moves = {"p1": (0, 500_000)}
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        self.assertFalse(r.legs[0]["tuned"])
        self.assertEqual([c.uuid for c in r.track_changes], ["s1"])
        self.assertConnectivityPreserved(board, moves, r)


class InvariantTests(CopperTestCase):
    def multi_board(self):
        parts, tracks = junction_case(oy=20 * MM, pre="j")
        base = reaim_case()
        vparts = [mkpart("q1", 0, -20 * MM), mkpart("q2", 10 * MM, -20 * MM)]
        vtracks = [
            mktrack("u1", (0, -20 * MM), (5 * MM, -20 * MM), layer=FCU),
            mktrack("u2", (5 * MM, -20 * MM), (10 * MM, -20 * MM), layer=BCU),
        ]
        board = mkboard(
            list(base.parts) + parts + vparts,
            list(base.tracks) + tracks + vtracks,
            [mkvia("uv", (5 * MM, -20 * MM))],
        )
        moves = {
            "p1": REAIM_MOVES["p1"],
            "jp1": (0, 1 * MM),
            "q1": (1 * MM, 1 * MM),
            "q2": (1 * MM, 1 * MM),
        }
        return board, moves

    def test_width_layer_net_unchanged_and_nothing_deleted(self):
        board, moves = self.multi_board()
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        self.assertEqual({c.uuid for c in r.track_changes}, {"t1", "jt1", "u1", "u2"})
        after = after_board(board, moves, r)
        self.assertEqual(len(after.tracks), len(board.tracks) + 2)  # two bends, no deletions
        for c in r.track_changes:
            orig = board.track(c.uuid)
            new_ids = [c.uuid] + ([c.uuid + "#b"] if c.bend is not None else [])
            for uid in new_ids:
                new = after.track(uid)
                self.assertEqual(
                    (new.width, new.layer, new.net, new.group, new.locked),
                    (orig.width, orig.layer, orig.net, orig.group, orig.locked),
                )
            if c.bend is not None:
                self.assertEqual(after.track(c.uuid).end, c.bend)
                self.assertEqual(after.track(c.uuid + "#b").start, c.bend)
                self.assertEqual(after.track(c.uuid + "#b").end, c.end)
        for t in board.tracks:
            if t.uuid not in {c.uuid for c in r.track_changes}:
                self.assertEqual(after.track(t.uuid), t)
        self.assertEqual(
            [(v.uuid, v.net, v.diameter, v.drill, v.layers) for v in after.vias],
            [(v.uuid, v.net, v.diameter, v.drill, v.layers) for v in board.vias],
        )

    def test_connectivity_unchanged(self):
        board, moves = self.multi_board()
        r = adapt_copper(board, moves)
        self.assertTrue(r.ok)
        after = self.assertConnectivityPreserved(board, moves, r)
        sig = connectivity_signature(after)
        self.assertIn(
            (N, ("pad:p1/p1.1", "pad:p2/p2.1", "track:t1")),
            sig,
        )
        self.assertIn(
            (N, ("pad:q1/q1.1", "pad:q2/q2.1", "track:u1", "track:u2", "via:uv")),
            sig,
        )

    def test_connectivity_signature_detects_detached_copper(self):
        board = reaim_case()
        broken = moved_parts_board(board, REAIM_MOVES)  # pads moved, track not adapted
        self.assertNotEqual(connectivity_signature(board), connectivity_signature(broken))

    def test_connectivity_check_detects_a_broken_result(self):
        # negative test: the connectivity assertion must be able to fail
        board = reaim_case()
        nothing_adapted = CopperResult(True)  # pad moved, the track was left where it was
        with self.assertRaises(AssertionError):
            self.assertConnectivityPreserved(board, REAIM_MOVES, nothing_adapted)
        # the same board with the real adaptation passes
        self.assertConnectivityPreserved(board, REAIM_MOVES, adapt_copper(board, REAIM_MOVES))

    def test_connectivity_check_detects_copper_re_aimed_onto_another_nets_pad(self):
        # no part moves; the bogus result drags the net-N track end into a net-X pad. Each pad's
        # declared net is unchanged, only the per-pad net derived from the copper changes.
        board = reaim_case(extra_parts=[mkpart("p3", 0, 3 * MM, net="X", court=0)])
        bogus = CopperResult(True, (TrackChange("t1", (0, 3 * MM), (10 * MM, 0)),))
        before = pad_touch_nets(board)
        after = pad_touch_nets(after_board(board, {}, bogus))
        self.assertEqual(before[("p3", "p3.1")], frozenset())
        self.assertEqual(after[("p3", "p3.1")], frozenset({N}))
        self.assertEqual(after[("p1", "p1.1")], frozenset())
        with self.assertRaises(AssertionError):
            self.assertConnectivityPreserved(board, {}, bogus)

    def test_pad_nets_are_derived_from_the_post_change_copper(self):
        board, moves = self.multi_board()
        r = adapt_copper(board, moves)
        after = after_board(board, moves, r)
        nets = pad_touch_nets(after)
        self.assertEqual(nets[("p1", "p1.1")], frozenset({N}))
        self.assertEqual(nets[("q2", "q2.1")], frozenset({N}))
        self.assertTrue(all(len(v) == 1 for v in nets.values()))

    def test_missing_rule_data_warns_and_skips_only_that_check(self):
        far = mktrack("x1", (0, -30 * MM), (10 * MM, -30 * MM), net="X")
        rules = Rules(clearance_nm=None, copper_edge_clearance_nm=None)
        r = adapt_copper(reaim_case([far], rules=rules), REAIM_MOVES)
        self.assertTrue(r.ok)
        self.assertEqual(
            r.warnings, ("missing_rule:clearance", "missing_rule:copper_edge_clearance")
        )
        self.assertEqual(r.track_changes[0].bend, REAIM_BEND)

        rules = Rules(clearance_nm=200_000, copper_edge_clearance_nm=None)
        r = adapt_copper(reaim_case([far], rules=rules), REAIM_MOVES)
        self.assertEqual(r.warnings, ("missing_rule:copper_edge_clearance",))
        # the clearance check still runs
        near = mktrack("x1", (2_500_000, 800_000), (2_500_000, 3 * MM), net="X")
        self.assertFalse(adapt_copper(reaim_case([near], rules=rules), REAIM_MOVES).ok)

        rules = Rules(clearance_nm=None, copper_edge_clearance_nm=300_000)
        r = adapt_copper(reaim_case([near], rules=rules), REAIM_MOVES)
        self.assertEqual(r.warnings, ("missing_rule:clearance",))
        self.assertTrue(r.ok)

    def test_pair_clearance_override_is_used(self):
        near = mktrack("x1", (2_500_000, 800_000), (2_500_000, 3 * MM), net="X")
        rules = Rules(
            clearance_nm=None,
            copper_edge_clearance_nm=300_000,
            pair_clearance={("N", "X"): 200_000},
        )
        r = adapt_copper(reaim_case([near], rules=rules), REAIM_MOVES)
        self.assertEqual(r.warnings, ())
        self.assertFalse(r.ok)

    def test_deterministic_under_input_shuffle(self):
        board, moves = self.multi_board()
        ref = adapt_copper(board, moves)
        self.assertTrue(ref.track_changes and ref.via_moves)
        self.assertEqual(ref.track_changes, tuple(sorted(ref.track_changes, key=lambda c: c.uuid)))
        self.assertEqual(ref.via_moves, tuple(sorted(ref.via_moves, key=lambda m: m.uuid)))
        for seed in range(6):
            rnd = random.Random(seed)
            tracks, parts, vias = list(board.tracks), list(board.parts), list(board.vias)
            rnd.shuffle(tracks)
            rnd.shuffle(parts)
            rnd.shuffle(vias)
            shuffled = dataclasses.replace(
                board, tracks=tuple(tracks), parts=tuple(parts), vias=tuple(vias)
            )
            self.assertEqual(adapt_copper(shuffled, dict(reversed(list(moves.items())))), ref)

    def test_result_is_frozen(self):
        r = adapt_copper(reaim_case(), REAIM_MOVES)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            r.ok = False


if __name__ == "__main__":
    unittest.main()
