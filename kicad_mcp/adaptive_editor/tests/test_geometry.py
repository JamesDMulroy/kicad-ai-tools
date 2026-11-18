"""Unit tests for adaptive_engine.geometry and adaptive_engine.model (stdlib unittest only)."""

import json
import unittest
from fractions import Fraction

from adaptive_engine import geometry as g
from adaptive_engine import model as m


def R(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


A = R(0, 0, 10, 10)
# U shape: arms x in [0,10] and [20,30]; the notch x in (10,20), y in (10,30] is outside.
U = ((0, 0), (30, 0), (30, 30), (20, 30), (20, 10), (10, 10), (10, 30), (0, 30))


class PolygonRelationTests(unittest.TestCase):
    def test_overlap_clear_touching(self):
        overlap = R(5, 5, 15, 15)
        clear = R(20, 0, 30, 10)
        edge_touch = R(10, 0, 20, 10)
        corner_touch = R(10, 10, 20, 20)
        inside = R(2, 2, 4, 4)
        self.assertTrue(g.polygons_intersect(A, overlap))
        self.assertEqual(g.polygon_distance(A, overlap), 0.0)
        self.assertFalse(g.polygons_intersect(A, clear))
        self.assertEqual(g.polygon_distance(A, clear), 10.0)
        self.assertTrue(g.polygons_intersect(A, edge_touch))
        self.assertEqual(g.polygon_distance(A, edge_touch), 0.0)
        self.assertTrue(g.polygons_intersect(A, corner_touch))
        self.assertTrue(g.polygons_intersect(A, inside))
        self.assertTrue(g.polygons_intersect(inside, A))
        self.assertEqual(g.polygon_distance(A, inside), 0.0)

    def test_winding_does_not_matter(self):
        rev = tuple(reversed(A))
        clear = R(20, 0, 30, 10)
        self.assertEqual(g.polygon_distance(rev, clear), 10.0)
        self.assertTrue(g.polygons_intersect(rev, R(5, 5, 15, 15)))
        self.assertEqual(g.polygon_area2(A), 200)
        self.assertEqual(g.polygon_area2(rev), -200)

    def test_diagonal_distance(self):
        d = R(13, 14, 20, 20)
        self.assertEqual(g.polygon_distance(A, d), 5.0)
        self.assertEqual(g.polygon_distance(d, A), 5.0)
        self.assertEqual(g.polygon_distance2(A, d), Fraction(25))

    def test_clearance_threshold_exact_boundary(self):
        clear = R(20, 0, 30, 10)  # distance exactly 10
        self.assertFalse(g.inflate_hit(A, clear, 9))
        self.assertFalse(g.inflate_hit(A, clear, 10))  # exactly at the clearance is legal
        self.assertTrue(g.inflate_hit(A, clear, 11))
        diag = R(13, 14, 20, 20)  # distance exactly 5
        self.assertFalse(g.inflate_hit(A, diag, 5))
        self.assertTrue(g.inflate_hit(A, diag, 6))
        irrational = R(11, 11, 20, 20)  # distance sqrt(2)
        self.assertFalse(g.inflate_hit(A, irrational, 1))
        self.assertTrue(g.inflate_hit(A, irrational, 2))
        self.assertFalse(g.inflate_hit(A, R(5, 5, 15, 15), 0))

    def test_ceil_helpers(self):
        self.assertEqual(g.ceil_sqrt_frac(0), 0)
        self.assertEqual(g.ceil_sqrt_frac(4), 2)
        self.assertEqual(g.ceil_sqrt_frac(5), 3)
        self.assertEqual(g.ceil_sqrt_frac(2, 2), 1)
        self.assertEqual(g.ceil_sqrt_frac(9, 4), 2)
        self.assertEqual(g.polygon_distance_ceil(A, R(11, 11, 20, 20)), 2)
        self.assertEqual(g.polygon_distance_ceil(A, R(20, 0, 30, 10)), 10)
        self.assertEqual(g.polygon_distance_ceil(A, R(5, 5, 15, 15)), 0)

    def test_translate_and_bbox(self):
        moved = g.polygon_translate(A, 5, -3)
        self.assertEqual(moved, ((5, -3), (15, -3), (15, 7), (5, 7)))
        self.assertEqual(g.bbox(moved), (5, -3, 15, 7))
        self.assertEqual(g.bbox(U), (0, 0, 30, 30))


class PointInPolygonTests(unittest.TestCase):
    def test_concave(self):
        self.assertTrue(g.point_in_polygon((5, 20), U))
        self.assertTrue(g.point_in_polygon((25, 20), U))
        self.assertTrue(g.point_in_polygon((15, 5), U))
        self.assertTrue(g.point_in_polygon((5, 10), U))  # ray runs through vertices
        self.assertFalse(g.point_in_polygon((15, 20), U))  # inside the notch
        self.assertFalse(g.point_in_polygon((15, 30), U))
        self.assertFalse(g.point_in_polygon((31, 5), U))
        self.assertFalse(g.point_in_polygon((-1, 5), U))

    def test_boundary_flag(self):
        for pt in ((15, 10), (10, 20), (0, 0), (30, 15), (20, 30)):
            self.assertTrue(g.point_in_polygon(pt, U), pt)
            self.assertTrue(g.point_in_polygon(pt, U, include_boundary=True), pt)
            self.assertFalse(g.point_in_polygon(pt, U, include_boundary=False), pt)
        self.assertTrue(g.point_in_polygon((5, 5), U, include_boundary=False))

    def test_fraction_points(self):
        pt = (Fraction(15, 2), Fraction(15, 2))
        self.assertTrue(g.point_in_polygon(pt, A, include_boundary=False))
        self.assertFalse(g.point_in_polygon((Fraction(21, 2), 5), A))


class SegmentTests(unittest.TestCase):
    def test_intersect_cases(self):
        self.assertTrue(g.segments_intersect((0, 0), (10, 10), (0, 10), (10, 0)))  # crossing
        self.assertTrue(g.segments_intersect((0, 0), (10, 0), (5, 0), (5, 5)))  # T touch
        self.assertTrue(g.segments_intersect((0, 0), (10, 0), (5, 0), (15, 0)))  # collinear overlap
        self.assertTrue(g.segments_intersect((0, 0), (10, 0), (10, 0), (10, 5)))  # shared endpoint
        self.assertFalse(g.segments_intersect((0, 0), (10, 0), (15, 0), (20, 0)))  # collinear gap
        self.assertFalse(g.segments_intersect((0, 0), (10, 0), (0, 5), (10, 5)))  # parallel
        self.assertFalse(g.segments_intersect((0, 0), (10, 0), (5, 1), (5, 5)))  # near miss
        self.assertTrue(g.segments_intersect((5, 0), (5, 0), (0, 0), (10, 0)))  # degenerate point

    def test_point_segment_distance(self):
        self.assertEqual(g.point_segment_distance((5, 3), (0, 0), (10, 0)), 3.0)
        self.assertEqual(g.point_segment_distance((13, 4), (0, 0), (10, 0)), 5.0)
        self.assertEqual(g.point_segment_distance((-3, 4), (0, 0), (10, 0)), 5.0)
        self.assertEqual(g.point_segment_distance((3, 4), (0, 0), (0, 0)), 5.0)
        self.assertEqual(g.point_segment_distance((5, 0), (0, 0), (10, 0)), 0.0)

    def test_segment_segment_distance(self):
        self.assertEqual(g.segment_segment_distance((0, 0), (10, 0), (0, 5), (10, 5)), 5.0)
        self.assertEqual(g.segment_segment_distance((0, 0), (10, 0), (5, 5), (15, 5)), 5.0)
        self.assertEqual(g.segment_segment_distance((0, 0), (10, 0), (15, 0), (20, 0)), 5.0)
        self.assertEqual(g.segment_segment_distance((0, 0), (10, 10), (0, 10), (10, 0)), 0.0)
        self.assertEqual(g.segment_segment_distance((0, 0), (10, 0), (13, 4), (20, 9)), 5.0)
        self.assertEqual(g.segment_segment_distance((0, 0), (10, 0), (5, 0), (15, 0)), 0.0)

    def test_segment_and_point_polygon_distance(self):
        self.assertEqual(g.segment_polygon_distance((20, 5), (30, 5), A), 10.0)
        self.assertEqual(g.segment_polygon_distance((-5, 5), (15, 5), A), 0.0)
        self.assertEqual(g.segment_polygon_distance((2, 2), (3, 3), A), 0.0)  # fully inside
        self.assertEqual(g.point_polygon_distance((13, 14), A), 5.0)
        self.assertEqual(g.point_polygon_distance((5, 5), A), 0.0)
        self.assertEqual(g.point_polygon_distance((10, 5), A), 0.0)
        self.assertEqual(g.segment_polygon_distance2((20, 5), (30, 5), A), Fraction(100))


class SimplicityTests(unittest.TestCase):
    def test_simple_polygons(self):
        self.assertTrue(g.polygon_is_simple(A))
        self.assertTrue(g.polygon_is_simple(U))
        self.assertTrue(g.polygon_is_simple(((0, 0), (10, 0), (5, 5))))

    def test_bowtie_is_not_simple(self):
        self.assertFalse(g.polygon_is_simple(((0, 0), (10, 10), (10, 0), (0, 10))))

    def test_degenerate_is_not_simple(self):
        self.assertFalse(g.polygon_is_simple(((0, 0), (10, 0))))
        self.assertFalse(g.polygon_is_simple(((0, 0), (5, 0), (10, 0))))
        self.assertFalse(g.polygon_is_simple(((0, 0), (0, 0), (10, 0), (10, 10))))
        self.assertFalse(g.polygon_is_simple(((0, 0), (10, 0), (5, 0), (5, 5))))  # fold back


class SweptTests(unittest.TestCase):
    M = R(0, 0, 10, 10)

    def test_thin_obstacle_between_start_and_end(self):
        thin = R(40, 2, 41, 8)
        v = (100, 0)
        end = g.polygon_translate(self.M, *v)
        self.assertFalse(g.polygons_intersect(self.M, thin))
        self.assertFalse(g.polygons_intersect(end, thin))
        self.assertTrue(g.swept_polygon_hits(self.M, v, thin))
        self.assertEqual(g.swept_polygon_distance2(self.M, v, thin), 0)
        self.assertAlmostEqual(g.first_contact_fraction(self.M, v, thin), 0.3, places=9)

    def test_thin_obstacle_other_directions(self):
        thin = R(2, -50, 8, -49)
        self.assertTrue(g.swept_polygon_hits(self.M, (0, -100), thin))
        self.assertAlmostEqual(g.first_contact_fraction(self.M, (0, -100), thin), 0.49, places=9)
        self.assertFalse(g.swept_polygon_hits(self.M, (0, 100), thin))
        self.assertIsNone(g.first_contact_fraction(self.M, (0, 100), thin))

    def test_parallel_offset_obstacle_is_missed(self):
        beside = R(40, 20, 41, 28)  # 10 above the swept band
        v = (100, 0)
        self.assertFalse(g.swept_polygon_hits(self.M, v, beside))
        self.assertIsNone(g.first_contact_fraction(self.M, v, beside))
        self.assertEqual(g.swept_polygon_distance2(self.M, v, beside), Fraction(100))
        self.assertFalse(g.swept_polygon_hits(self.M, v, beside, clearance=9))
        self.assertTrue(g.swept_polygon_hits(self.M, v, beside, clearance=10))
        self.assertAlmostEqual(
            g.first_contact_fraction(self.M, v, beside, clearance=10), 0.3, places=9
        )

    def test_non_monotonic_feasibility(self):
        fat = R(40, 0, 60, 10)
        v = (100, 0)
        end = g.polygon_translate(self.M, *v)
        mid = g.polygon_translate(self.M, 40, 0)
        self.assertEqual(g.polygon_distance(self.M, fat), 30.0)
        self.assertEqual(g.polygon_distance(mid, fat), 0.0)
        self.assertEqual(g.polygon_distance(end, fat), 40.0)  # clear again at the end
        self.assertTrue(g.swept_polygon_hits(self.M, v, fat))
        self.assertAlmostEqual(g.first_contact_fraction(self.M, v, fat), 0.3, places=9)

    def test_first_contact_values(self):
        thin = R(40, 2, 41, 8)
        fat = R(40, 0, 60, 10)
        self.assertAlmostEqual(g.first_contact_fraction(self.M, (100, 0), fat), 0.3, places=9)
        self.assertAlmostEqual(
            g.first_contact_fraction(self.M, (100, 0), thin, clearance=5), 0.25, places=9
        )
        # already touching
        self.assertEqual(g.first_contact_fraction(self.M, (100, 0), R(10, 0, 20, 10)), 0.0)
        # overlapping
        self.assertEqual(g.first_contact_fraction(self.M, (100, 0), R(5, 5, 15, 15)), 0.0)
        # moving away from an obstacle 5 to the right
        away = R(15, 0, 25, 10)
        self.assertIsNone(g.first_contact_fraction(self.M, (-100, 0), away))
        self.assertEqual(g.first_contact_fraction(self.M, (-100, 0), away, clearance=5), 0.0)
        # diagonal corner to corner
        corner = R(20, 20, 30, 30)
        self.assertAlmostEqual(g.first_contact_fraction(self.M, (20, 20), corner), 0.5, places=9)
        # zero displacement never moves into contact
        self.assertIsNone(g.first_contact_fraction(self.M, (0, 0), fat))

    def test_swept_clearance_exact_boundary(self):
        obstacle = R(40, 0, 50, 10)
        v = (20, 0)  # ends exactly 10 from the obstacle
        self.assertEqual(g.swept_polygon_distance2(self.M, v, obstacle), Fraction(100))
        self.assertFalse(g.swept_polygon_hits(self.M, v, obstacle, clearance=9))
        self.assertTrue(g.swept_polygon_hits(self.M, v, obstacle, clearance=10))
        self.assertIsNone(g.first_contact_fraction(self.M, v, obstacle, clearance=9))
        self.assertAlmostEqual(
            g.first_contact_fraction(self.M, v, obstacle, clearance=10), 1.0, places=9
        )

    def test_swept_agrees_with_contact_fraction(self):
        obstacles = [R(40, 2, 41, 8), R(40, 20, 41, 28), R(20, 20, 30, 30), R(-30, 0, -20, 10)]
        for v in ((100, 0), (20, 20), (0, -50), (-100, 0)):
            for ob in obstacles:
                for clearance in (0, 5):
                    hit = g.swept_polygon_hits(self.M, v, ob, clearance)
                    frac = g.first_contact_fraction(self.M, v, ob, clearance)
                    self.assertEqual(hit, frac is not None, (v, ob, clearance))


class InsideTests(unittest.TestCase):
    OUTER = R(0, 0, 100, 100)

    def test_margins(self):
        inner = R(10, 10, 20, 20)
        self.assertTrue(g.polygon_inside(inner, self.OUTER))
        self.assertTrue(g.polygon_inside(inner, self.OUTER, 10))
        self.assertFalse(g.polygon_inside(inner, self.OUTER, 11))

    def test_touching_and_identical(self):
        touching = R(0, 10, 10, 20)
        self.assertTrue(g.polygon_inside(touching, self.OUTER))
        self.assertFalse(g.polygon_inside(touching, self.OUTER, 1))
        self.assertTrue(g.polygon_inside(self.OUTER, self.OUTER))

    def test_outside_and_crossing(self):
        self.assertFalse(g.polygon_inside(R(90, 90, 110, 110), self.OUTER))
        self.assertFalse(g.polygon_inside(R(200, 200, 210, 210), self.OUTER))
        self.assertFalse(g.polygon_inside(R(-5, 40, 5, 60), self.OUTER))

    def test_concave_outer_bridge_across_notch(self):
        bar = R(5, 15, 25, 18)  # both ends inside the arms, the middle crosses the notch
        self.assertTrue(g.point_in_polygon((5, 15), U))
        self.assertTrue(g.point_in_polygon((25, 15), U))
        self.assertFalse(g.polygon_inside(bar, U))
        self.assertTrue(g.polygon_inside(R(2, 2, 8, 8), U, 2))
        self.assertFalse(g.polygon_inside(R(2, 2, 8, 8), U, 3))

    def test_region_with_hole(self):
        region = (R(0, 0, 100, 100), R(40, 40, 60, 60))
        self.assertTrue(g.region_contains(region, R(10, 10, 20, 20)))
        self.assertFalse(g.region_contains(region, R(45, 45, 55, 55)))  # inside the hole
        self.assertFalse(g.region_contains(region, R(30, 30, 70, 70)))  # swallows the hole
        self.assertFalse(g.region_contains(region, R(30, 30, 50, 50)))  # crosses the hole
        self.assertTrue(g.region_contains(region, R(20, 40, 40, 60)))  # touches the hole
        self.assertFalse(g.region_contains(region, R(20, 40, 40, 60), 1))
        self.assertTrue(g.region_contains(region, R(10, 40, 35, 60), 5))
        self.assertFalse(g.region_contains(region, R(10, 40, 35, 60), 6))

    def test_segment_inside_region(self):
        region = (R(0, 0, 100, 100), R(40, 40, 60, 60))
        self.assertTrue(g.segment_inside_region((10, 10), (30, 10), region))
        self.assertFalse(g.segment_inside_region((30, 50), (70, 50), region))  # through the hole
        self.assertFalse(g.segment_inside_region((90, 50), (110, 50), region))  # leaves outer
        self.assertTrue(g.segment_inside_region((40, 40), (60, 40), region))  # along the hole
        self.assertFalse(g.segment_inside_region((40, 40), (60, 40), region, 1))
        self.assertTrue(g.segment_inside_region((10, 10), (30, 10), region, 10))
        self.assertFalse(g.segment_inside_region((10, 10), (30, 10), region, 11))
        self.assertTrue(g.segment_inside_polygon((5, 15), (5, 25), U))
        self.assertFalse(g.segment_inside_polygon((5, 15), (25, 15), U))


class ExactAreaTests(unittest.TestCase):
    """Exact (Fraction) areas and the forbidden-region helpers used by the solver's board edge."""

    def test_intersection_area_of_rectangles(self):
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(5, 5, 15, 15))), 25)
        self.assertEqual(g.intersection_area((R(5, 5, 15, 15), R(0, 0, 10, 10))), 25)
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(2, 2, 4, 4))), 4)  # contained
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(0, 0, 10, 10))), 100)  # identical
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(20, 0, 30, 10))), 0)  # disjoint
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(10, 0, 20, 10))), 0)  # touching
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(10, 10, 20, 20))), 0)  # corner
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), tuple(reversed(R(5, 5, 15, 15))))), 25)

    def test_intersection_area_of_concave_and_three_polygons(self):
        self.assertEqual(g.intersection_area((U, R(5, 15, 25, 18))), 30)  # two arm pieces
        self.assertEqual(g.intersection_area((U, R(0, 5, 30, 12))), 190)
        self.assertEqual(g.intersection_area((U, R(10, 10, 20, 30))), 0)  # exactly the notch
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(5, 5, 15, 15), R(0, 0, 7, 7))), 4)
        self.assertEqual(g.intersection_area((R(0, 0, 10, 10), R(5, 5, 15, 15), R(8, 0, 9, 3))), 0)

    def test_intersection_area_is_exact_for_rational_crossings(self):
        triangle = ((0, 0), (7, 0), (0, 7))
        self.assertEqual(g.intersection_area((triangle, R(0, 0, 4, 4))), Fraction(31, 2))
        self.assertEqual(g.intersection_area((R(0, 0, 4, 4), triangle)), Fraction(31, 2))
        self.assertEqual(g.intersection_area((triangle, R(0, 0, 7, 7))), Fraction(49, 2))
        half = Fraction(1, 2)
        self.assertEqual(g.intersection_area((R(half, 0, 3 * half, 1), R(1, 0, 2, 1))), half)

    def test_intersection_area_degenerate_input_is_zero(self):
        flat = ((0, 0), (10, 0), (20, 0))
        self.assertEqual(g.intersection_area((flat, A)), 0)
        self.assertEqual(g.intersection_area((((0, 0), (10, 10)), A)), 0)
        self.assertEqual(g.intersection_area(()), 0)

    def test_forbidden_overlap_areas(self):
        region = (R(0, 0, 100, 100), R(40, 40, 60, 60))
        self.assertEqual(g.forbidden_overlap_areas(region, R(10, 10, 20, 20)), (0, 0))
        self.assertEqual(g.forbidden_overlap_areas(region, R(90, 10, 110, 20)), (100, 0))  # sticks out
        self.assertEqual(g.forbidden_overlap_areas(region, R(45, 45, 55, 55)), (0, 100))  # in the hole
        self.assertEqual(g.forbidden_overlap_areas(region, R(30, 30, 70, 70)), (0, 400))  # swallows it
        self.assertEqual(g.forbidden_overlap_areas(region, R(20, 40, 40, 60)), (0, 0))  # touches only
        self.assertEqual(g.forbidden_overlap_areas((U,), R(5, 15, 25, 18)), (30,))  # bridges a notch
        self.assertEqual(g.forbidden_overlap_areas((), R(0, 0, 1, 1)), ())

    def test_forbidden_entry_area_outside_piece(self):
        region = (R(0, 0, 100, 100),)
        pad = R(90, 10, 98, 20)
        self.assertEqual(g.forbidden_entry_area(region, pad, (5, 0)), 30)  # x 95..98 cross x = 100
        self.assertEqual(g.forbidden_entry_area(region, pad, (2, 0)), 0)  # stays inside
        self.assertEqual(g.forbidden_entry_area(region, pad, (-5, 0)), 0)
        self.assertEqual(g.forbidden_entry_area(region, pad, (0, 5)), 0)
        self.assertEqual(g.forbidden_entry_area(region, pad, (0, 0)), 0)
        # a pad that already sticks out may retreat or slide, but not go further out
        straddle = R(98, 10, 106, 20)
        self.assertEqual(g.forbidden_entry_area(region, straddle, (5, 0)), 20)
        self.assertEqual(g.forbidden_entry_area(region, straddle, (-5, 0)), 0)
        self.assertEqual(g.forbidden_entry_area(region, straddle, (0, 30)), 0)
        self.assertEqual(g.forbidden_entry_area(region, straddle, (-5, 7)), 0)  # diagonal retreat

    def test_forbidden_entry_area_hole_and_notch(self):
        region = (R(0, 0, 100, 100), R(40, 40, 60, 60))
        pad = R(10, 45, 20, 55)
        self.assertEqual(g.forbidden_entry_area(region, pad, (20, 0)), 0)  # touches the hole only
        self.assertEqual(g.forbidden_entry_area(region, pad, (21, 0)), 10)
        self.assertGreater(g.forbidden_entry_area(region, pad, (60, 0)), 0)  # jumps the whole hole
        self.assertEqual(g.forbidden_entry_area(region, pad, (-5, 0)), 0)
        self.assertEqual(g.forbidden_entry_area(region, pad, (21, 0), edges=[]), 0)
        # concave outer boundary: sliding down inside the arm is fine, a bar that reaches the notch
        # wall and goes on into the notch (outside the board) enters the forbidden region
        self.assertEqual(g.forbidden_entry_area((U,), R(5, 12, 8, 15), (0, 5)), 0)
        self.assertEqual(g.forbidden_entry_area((U,), R(8, 12, 11, 15), (5, 0)), 6)

    def test_swept_hull_of_a_nonconvex_pad_is_covered_by_edge_sweeps(self):
        # an L-shaped pad slides into a thin cutout that only its long arm can reach
        ell = ((0, 0), (20, 0), (20, 4), (4, 4), (4, 20), (0, 20))
        region = (R(-50, -50, 100, 100), R(30, 1, 31, 3))
        self.assertGreater(g.forbidden_entry_area(region, ell, (20, 0)), 0)
        self.assertEqual(g.forbidden_entry_area(region, ell, (9, 0)), 0)  # 1 short of the cutout
        self.assertEqual(g.forbidden_entry_area(region, ell, (0, 20)), 0)  # sliding the other way


# ----------------------------------------------------------------------------- model


def _part(uuid="p1", courtyard=(), pads=None, position=(1000, 2000), **kw):
    if pads is None:
        pads = (
            m.Pad("pad1", "N1", (150, 125), R(100, 100, 200, 150)),
            m.Pad("pad2", "N2", (350, 160), R(300, 120, 400, 200)),
        )
    return m.Part(uuid, "R1", position, 90000, "F", courtyard, False, pads, **kw)


class ModelTests(unittest.TestCase):
    def test_effective_courtyard_fallback(self):
        part = _part()
        rules = m.Rules(200000, 250000, fallback_margin_nm=50)
        cy, missing = part.effective_courtyard(rules)
        self.assertTrue(missing)
        self.assertEqual(part.pad_bbox(), (100, 100, 400, 200))
        self.assertEqual(len(cy), 1)
        self.assertEqual(g.bbox(cy[0]), (50, 50, 450, 250))
        self.assertEqual(len(cy[0]), 4)
        self.assertEqual(cy, ((( 50, 50), (450, 50), (450, 250), (50, 250)),))

    def test_effective_courtyard_present(self):
        court = (R(0, 0, 500, 300),)
        part = _part(courtyard=court)
        cy, missing = part.effective_courtyard(m.Rules(1, 1, fallback_margin_nm=50))
        self.assertFalse(missing)
        self.assertEqual(cy, court)

    def test_effective_courtyard_no_pads_no_courtyard(self):
        part = _part(pads=())
        self.assertIsNone(part.pad_bbox())
        self.assertEqual(part.effective_courtyard(m.Rules(1, 1)), ((), True))

    def test_effective_courtyard_empty_polygon_counts_as_missing(self):
        part = _part(courtyard=((),))
        _, missing = part.effective_courtyard(m.Rules(1, 1, fallback_margin_nm=0))
        self.assertTrue(missing)

    def test_rules_clearance(self):
        rules = m.Rules(200000, 250000, pair_clearance={("A", "B"): 300000})
        self.assertEqual(rules.clearance("A", "B"), 300000)
        self.assertEqual(rules.clearance("B", "A"), 300000)
        self.assertEqual(rules.clearance("A", "C"), 200000)
        self.assertEqual(rules.clearance("A", "A"), 200000)
        missing = m.Rules(None, None)
        self.assertIsNone(missing.clearance("A", "B"))
        partial = m.Rules(None, None, pair_clearance={("A", "B"): 5})
        self.assertEqual(partial.clearance("B", "A"), 5)
        self.assertIsNone(partial.clearance("A", "C"))
        self.assertEqual(m.Rules(1, 2).courtyard_clearance_nm, 0)
        self.assertEqual(m.Rules(1, 2).fallback_margin_nm, 0)

    def test_part_translated(self):
        part = _part(courtyard=(R(0, 0, 500, 300),))
        moved = part.translated(-10, 25)
        self.assertEqual(moved.position, (990, 2025))
        self.assertEqual(moved.courtyard, (R(-10, 25, 490, 325),))
        self.assertEqual(moved.pads[0].center, (140, 150))
        self.assertEqual(moved.pads[0].polygon, R(90, 125, 190, 175))
        self.assertEqual(moved.pads[1].polygon, R(290, 145, 390, 225))
        self.assertEqual((moved.uuid, moved.ref, moved.rotation_mdeg, moved.side, moved.locked),
                         (part.uuid, part.ref, part.rotation_mdeg, part.side, part.locked))
        self.assertEqual(part.position, (1000, 2000))  # original untouched
        self.assertEqual(part.translated(0, 0), part)
        with self.assertRaises(Exception):
            part.position = (0, 0)  # frozen

    def test_part_side_validated(self):
        with self.assertRaises(ValueError):
            m.Part("p", "R", (0, 0), 0, "X", (), False, ())

    def test_board_lookups(self):
        board = m.Board(
            parts=(_part("p1"), _part("p2")),
            tracks=(m.Track("t1", "N1", "F.Cu", (0, 0), (10, 0), 200),),
            vias=(m.Via("v1", "N1", (5, 5), 600, 300, ("F.Cu", "B.Cu")),),
            outline=m.Outline((R(0, 0, 100, 100),), (m.EdgeSeg("e1", (0, 0), (100, 0)),)),
            keepouts=(m.Keepout("k1", R(0, 0, 5, 5), ("F.Cu",), True, True, True, False),),
            zones=(m.Zone("z1", "GND", ("F.Cu",), R(0, 0, 100, 100)),),
            rules=m.Rules(200, 250),
            contract=m.Contract({"p1": "fixed"}),
        )
        self.assertEqual(board.part("p2").uuid, "p2")
        self.assertEqual(board.track("t1").width, 200)
        self.assertEqual(board.via("v1").diameter, 600)
        self.assertEqual(board.edge("e1").end, (100, 0))
        for lookup in (board.part, board.track, board.via, board.edge):
            with self.assertRaises(KeyError):
                lookup("nope")
        with self.assertRaises(KeyError):
            board.part("t1")  # a track uuid is not a part
        self.assertEqual(board.index["p1"][0], "part")
        self.assertEqual(board.index["e1"][0], "edge")
        self.assertIs(board.index["z1"][1], board.zones[0])
        self.assertNotIn("nope", board.index)
        self.assertEqual(board.contract.permissions["p1"], "fixed")

    def test_changeset_to_dict_is_deterministic(self):
        def build(reverse):
            pm = [m.PartMove("b", 1, 2), m.PartMove("a", 3, 4)]
            tc = [m.TrackChange("t2", (0, 0), (5, 5), (5, 0)), m.TrackChange("t1", (1, 1), (2, 2))]
            vm = [m.ViaMove("v2", 1, 1), m.ViaMove("v1", 2, 2)]
            ec = [m.EdgeChange("e2", (0, 0), (1, 1)), m.EdgeChange("e1", (2, 2), (3, 3))]
            if reverse:
                pm, tc, vm, ec = pm[::-1], tc[::-1], vm[::-1], ec[::-1]
            return m.ChangeSet(tuple(pm), tuple(tc), tuple(vm), tuple(ec))

        cs1, cs2 = build(False), build(True)
        self.assertEqual(cs1, cs2)
        self.assertEqual(cs1.to_dict(), cs2.to_dict())
        self.assertEqual(json.dumps(cs1.to_dict()), json.dumps(cs2.to_dict()))
        d = cs1.to_dict()
        self.assertEqual([x["uuid"] for x in d["part_moves"]], ["a", "b"])
        self.assertEqual([x["uuid"] for x in d["track_changes"]], ["t1", "t2"])
        self.assertEqual([x["uuid"] for x in d["via_moves"]], ["v1", "v2"])
        self.assertEqual([x["uuid"] for x in d["edge_changes"]], ["e1", "e2"])
        self.assertIsNone(d["track_changes"][0]["bend"])
        self.assertEqual(d["track_changes"][1]["bend"], [5, 0])
        self.assertEqual(d["part_moves"][0], {"uuid": "a", "dx": 3, "dy": 4})
        self.assertEqual(json.loads(json.dumps(d)), d)  # JSON-safe round trip
        self.assertFalse(cs1.is_empty())

    def test_changeset_is_empty(self):
        self.assertTrue(m.ChangeSet().is_empty())
        self.assertTrue(m.ChangeSet((), (), (), ()).is_empty())
        self.assertFalse(m.ChangeSet(via_moves=(m.ViaMove("v", 1, 0),)).is_empty())
        self.assertFalse(m.ChangeSet(edge_changes=(m.EdgeChange("e", (0, 0), (1, 0)),)).is_empty())
        self.assertEqual(
            m.ChangeSet().to_dict(),
            {"part_moves": [], "track_changes": [], "via_moves": [], "edge_changes": []},
        )

    def test_blocker_and_report(self):
        with self.assertRaises(ValueError):
            m.Blocker("p1", "bogus", 0)
        with self.assertRaises(ValueError):
            m.Report("maybe", 0, 0)
        report = m.Report(
            "partial",
            1000,
            400,
            (m.Blocker("p1", "frozen", 400), m.Blocker(None, "edge", None)),
            ("missing_courtyard:p9",),
            None,
            ({"leg": 1, "length_before": 10},),
            ("b", "a"),
        )
        d = report.to_dict()
        self.assertEqual(d["status"], "partial")
        self.assertEqual(d["requested_nm"], 1000)
        self.assertEqual(d["achieved_nm"], 400)
        self.assertEqual(d["blockers"][0], {"uuid": "p1", "kind": "frozen", "at_nm": 400})
        self.assertEqual(d["blockers"][1], {"uuid": None, "kind": "edge", "at_nm": None})
        self.assertEqual(d["warnings"], ["missing_courtyard:p9"])
        self.assertIsNone(d["reject_reason"])
        self.assertEqual(d["moved_parts"], ["a", "b"])
        self.assertEqual(json.loads(json.dumps(d)), d)
        rejected = m.Report("rejected", 5, 0, reject_reason="edge_collapse")
        self.assertEqual(rejected.to_dict()["reject_reason"], "edge_collapse")
        self.assertEqual(rejected.blockers, ())


class RequestTests(unittest.TestCase):
    @staticmethod
    def base(**over):
        d = {"driver": {"kind": "part", "uuids": ["p1"]}, "displacement_nm": [1000, 0]}
        d.update(over)
        return d

    def assertCode(self, code, data):
        with self.assertRaises(m.RequestError) as ctx:
            m.Request.from_dict(data)
        self.assertEqual(ctx.exception.code, code)

    def test_defaults(self):
        req = m.Request.from_dict(self.base())
        self.assertEqual(req.driver_kind, "part")
        self.assertEqual(req.driver_uuids, ("p1",))
        self.assertEqual(req.displacement_nm, (1000, 0))
        self.assertEqual(req.mode, "cascade")
        self.assertEqual(req.movers, ())
        self.assertEqual(req.frozen, ())
        self.assertEqual(req.priority, ())
        self.assertIsNone(req.eligibility_nm)
        self.assertTrue(req.allow_partial)
        self.assertEqual(req.copper, "adapt")

    def test_full_request_round_trip(self):
        data = {
            "driver": {"kind": "part", "uuids": ["p1", "p2"]},
            "displacement_nm": [-500, 250],
            "mode": "explicit",
            "movers": ["p3", "p4"],
            "frozen": ["p5"],
            "priority": ["p4"],
            "eligibility_nm": 5000000,
            "allow_partial": False,
            "copper": "fixed",
        }
        req = m.Request.from_dict(data)
        self.assertEqual(req.movers, ("p3", "p4"))
        self.assertFalse(req.allow_partial)
        self.assertEqual(req.copper, "fixed")
        self.assertEqual(req.eligibility_nm, 5000000)
        self.assertEqual(req.to_dict(), data)
        self.assertEqual(m.Request.from_dict(req.to_dict()), req)
        self.assertEqual(req.driver, {"kind": "part", "uuids": ["p1", "p2"]})

    def test_edge_driver(self):
        data = self.base(driver={"kind": "edge", "edge_uuid": "e1", "side": "left"})
        req = m.Request.from_dict(data)
        self.assertEqual(req.driver_kind, "edge")
        self.assertEqual(req.edge_uuid, "e1")
        self.assertEqual(req.side, "left")
        self.assertEqual(req.driver, {"kind": "edge", "edge_uuid": "e1", "side": "left"})
        self.assertEqual(m.Request.from_dict(req.to_dict()), req)
        self.assertCode("bad_driver", self.base(driver={"kind": "edge", "edge_uuid": "e1"}))
        self.assertCode(
            "bad_driver", self.base(driver={"kind": "edge", "edge_uuid": "e1", "side": "up"})
        )
        self.assertCode("bad_driver", self.base(driver={"kind": "edge", "side": "left"}))

    def test_bad_shape(self):
        self.assertCode("bad_request", [])
        self.assertCode("bad_request", self.base(bogus=1))
        self.assertCode("bad_driver", {"displacement_nm": [1, 0]})
        self.assertCode("bad_driver", self.base(driver="p1"))
        self.assertCode("bad_driver", self.base(driver={"kind": "other", "uuids": ["p1"]}))
        self.assertCode("bad_driver", self.base(driver={"kind": "part", "uuids": []}))
        self.assertCode("bad_driver", self.base(driver={"kind": "part"}))
        self.assertCode("bad_driver", self.base(driver={"kind": "part", "uuids": "p1"}))
        self.assertCode("bad_driver", self.base(driver={"kind": "part", "uuids": [1]}))
        self.assertCode("bad_request", self.base(movers="p1"))
        self.assertCode("bad_request", self.base(frozen=[None]))
        self.assertCode("bad_request", self.base(allow_partial="yes"))

    def test_bad_displacement(self):
        for bad in (None, [1], [1, 2, 3], [1.5, 0], ["1", 0], [True, 0], "10,0", {"x": 1}):
            self.assertCode("bad_displacement", self.base(displacement_nm=bad))
        self.assertCode("bad_displacement", {"driver": {"kind": "part", "uuids": ["p1"]}})
        req = m.Request.from_dict(self.base(displacement_nm=(0, -7)))
        self.assertEqual(req.displacement_nm, (0, -7))

    def test_bad_enums(self):
        self.assertCode("bad_mode", self.base(mode="broadcast"))
        self.assertCode("bad_copper", self.base(copper="reroute"))
        self.assertCode("bad_eligibility", self.base(eligibility_nm=-1))
        self.assertCode("bad_eligibility", self.base(eligibility_nm=1.5))
        self.assertCode("bad_eligibility", self.base(eligibility_nm=True))
        self.assertEqual(m.Request.from_dict(self.base(eligibility_nm=0)).eligibility_nm, 0)

    def test_duplicates_are_rejected_not_deduped(self):
        self.assertCode("duplicate_uuid", self.base(driver={"kind": "part", "uuids": ["p1", "p1"]}))
        self.assertCode("duplicate_uuid", self.base(movers=["a", "b", "a"]))
        self.assertCode("duplicate_uuid", self.base(frozen=["a", "a"]))
        self.assertCode("duplicate_uuid", self.base(priority=["a", "a"]))

    def test_request_error_shape(self):
        err = m.RequestError("duplicate_uuid", "frozen repeats a")
        self.assertEqual(err.code, "duplicate_uuid")
        self.assertEqual(err.message, "frozen repeats a")
        self.assertIsInstance(err, Exception)


if __name__ == "__main__":
    unittest.main()
