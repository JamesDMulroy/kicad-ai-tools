"""Adapter, session and CLI tests (ADAPTIVE-ENGINE-SPEC section 7, Session part) on pcbnew boards built by
fixtures.py. Run with KiCad's python:

    <KiCad python> -X utf8 -B -m unittest discover -s kicad_mcp/adaptive_editor/tests -t kicad_mcp/adaptive_editor

Everything is written to a temp directory. kicad-cli and the CLI subprocess are started hidden.
Where a test is about session logic rather than DRC itself it injects a fake DRC runner and skips the
render, so only ``test_real_preview_artifacts`` and the subprocess CLI test pay for kicad-cli / pcbnew.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import Counter

from adaptive_engine import geometry as geo
from adaptive_engine import pcbnew_adapter as adapter
from adaptive_engine import session
from adaptive_engine import cli
from adaptive_engine.model import ChangeSet, EdgeChange, PartMove, TrackChange, ViaMove

from . import fixtures as fx
from .fixtures import mm

try:
    adapter.get_pcbnew()
except adapter.AdapterError as exc:  # pragma: no cover - plain python without pcbnew
    raise unittest.SkipTest("pcbnew unavailable: %s" % exc)

ROOT = None
RECT = None
CONCAVE = None
_counter = [0]


def setUpModule():
    global ROOT, RECT, CONCAVE
    ROOT = tempfile.mkdtemp(prefix="adaptive_a2_")
    unittest.addModuleCleanup(shutil.rmtree, ROOT, ignore_errors=True)
    RECT = fx.build(os.path.join(ROOT, "rect"), "rect", outline="rect")
    CONCAVE = fx.build(os.path.join(ROOT, "concave"), "concave", outline="concave")


def tearDownModule():
    if ROOT:
        shutil.rmtree(ROOT, ignore_errors=True)


def fresh(source=None) -> fx.Fixture:
    """A private copy of a fixture board so a test may modify it."""
    _counter[0] += 1
    return fx.clone(source or RECT, os.path.join(ROOT, "work%03d" % _counter[0]))


def read(path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def uuids(items) -> list[str]:
    return [i["uuid"] for i in items]


def drc_runner(original_items, candidate_items):
    """A DRC runner returning fixed violations: items are ``[(type, [uuid, ...]), ...]``."""

    def make(items):
        return {
            "violations": [
                {"type": t, "severity": "error", "description": t, "items": [{"uuid": u} for u in us]}
                for t, us in items
            ],
            "unconnected_items": [],
        }

    def run(board_path, out_json, cli_path, timeout):
        return make(original_items if os.path.basename(board_path).startswith("original") else candidate_items)

    return run


CLEAN = drc_runner([], [])


def cascade_request(model):
    return {
        "driver": {"kind": "part", "uuids": [fx.part(model, "R1").uuid]},
        "displacement_nm": [mm(3), mm(0.6)],
        "mode": "cascade",
        "copper": "adapt",
        "allow_partial": True,
    }


def pour_request(model):
    return {
        "driver": {"kind": "part", "uuids": [fx.part(model, "R4").uuid]},
        "displacement_nm": [mm(14), 0],
        "mode": "cascade",
        "copper": "adapt",
        "allow_partial": True,
    }


def norm(board):
    """Model with polygon vertex order normalised (the order pcbnew returns is not part of the contract)."""

    def poly(p):
        return tuple(sorted(p))

    parts = tuple(
        (p.uuid, p.ref, p.position, p.rotation_mdeg, p.side, p.locked,
         tuple(sorted(poly(c) for c in p.courtyard)),
         tuple(sorted((a.uuid, a.net, a.center, poly(a.polygon)) for a in p.pads)))
        for p in sorted(board.parts, key=lambda p: p.uuid)
    )
    return (
        parts,
        tuple(sorted(board.tracks, key=lambda t: t.uuid)),
        tuple(sorted(board.vias, key=lambda v: v.uuid)),
        tuple(sorted(poly(p) for p in board.outline.polygons)),
        tuple(sorted(board.outline.edges, key=lambda e: e.uuid)),
        tuple(sorted((k.uuid, poly(k.polygon), k.layers, k.no_tracks, k.no_vias, k.no_footprints, k.no_pour)
                     for k in board.keepouts)),
        tuple(sorted((z.uuid, z.net, z.layers, poly(z.polygon)) for z in board.zones)),
        board.rules,
    )


# ----------------------------------------------------------------------------- adapter


class AdapterTests(unittest.TestCase):
    def test_load_rect_board(self):
        info = adapter.load_board_with_info(RECT.board_path)
        b = info.board
        self.assertEqual(sorted(p.ref for p in b.parts), ["R1", "R2", "R3", "R4", "R5"])
        r1 = fx.part(b, "R1")
        self.assertEqual(r1.position, (mm(110), mm(110)))
        self.assertEqual(r1.side, "F")
        self.assertFalse(r1.locked)
        self.assertEqual(len(r1.pads), 2)
        self.assertEqual(sorted(p.net for p in r1.pads), ["N1", "N2"])
        tol = mm(0.06)
        x0, y0, x1, y1 = geo.bbox(r1.courtyard[0])
        for got, want in zip((x0, y0, x1, y1), (mm(108.2), mm(109.1), mm(111.8), mm(110.9))):
            self.assertLessEqual(abs(got - want), tol)
        pad1 = next(p for p in r1.pads if p.net == "N1")
        self.assertEqual(pad1.center, (mm(109), mm(110)))
        px0, py0, px1, py1 = geo.bbox(pad1.polygon)
        self.assertLessEqual(abs((px1 - px0) - mm(0.9)), mm(0.01))
        self.assertLessEqual(abs((py1 - py0) - mm(0.9)), mm(0.01))

        self.assertEqual(sorted((t.net, t.layer, t.width) for t in b.tracks),
                         [("N1", "F.Cu", mm(0.25)), ("N4", "F.Cu", mm(0.25))])
        self.assertEqual(sorted((v.net, v.diameter, v.drill) for v in b.vias),
                         [("GND", mm(0.8), mm(0.4)), ("N1", mm(0.8), mm(0.4))])
        self.assertTrue(all(v.layers[0] == "F.Cu" and v.layers[-1] == "B.Cu" for v in b.vias))

        self.assertEqual(len(b.outline.polygons), 1)
        self.assertEqual(len(b.outline.polygons[0]), 4)
        self.assertEqual(len(b.outline.edges), 4)
        self.assertEqual(len({e.uuid for e in b.outline.edges}), 4)
        fx.edge_between(b, (100, 100), (160, 100))

        self.assertEqual(len(b.keepouts), 1)
        k = b.keepouts[0]
        self.assertTrue(k.no_tracks and k.no_vias and k.no_footprints and k.no_pour)
        self.assertEqual(len(b.zones), 1)
        self.assertEqual(b.zones[0].net, "GND")

        self.assertEqual(b.rules.clearance_nm, mm(0.2))
        self.assertEqual(b.rules.copper_edge_clearance_nm, mm(0.5))
        self.assertEqual(b.rules.courtyard_clearance_nm, 0)
        self.assertIn("courtyard_clearance:default_0", info.warnings)
        self.assertNotIn("rule_missing:clearance", info.warnings)

    def test_load_concave_outline(self):
        b = adapter.load_board(CONCAVE.board_path)
        self.assertEqual(len(b.outline.polygons), 1)
        ring = b.outline.polygons[0]
        self.assertEqual(sorted(ring), sorted((mm(x), mm(y)) for x, y in fx.CONCAVE_OUTLINE))
        self.assertTrue(geo.polygon_is_simple(ring))
        self.assertEqual(len(b.outline.edges), 6)
        self.assertEqual(sorted(p.ref for p in b.parts), ["R1", "R2", "R3"])
        self.assertEqual(b.zones, ())

    def test_courtyard_clearance_from_rules_file(self):
        built = fx.build(os.path.join(ROOT, "dru"), "dru", outline="concave", courtyard_clearance_mm=0.25)
        info = adapter.load_board_with_info(built.board_path)
        self.assertEqual(info.board.rules.courtyard_clearance_nm, mm(0.25))
        self.assertNotIn("courtyard_clearance:default_0", info.warnings)

    def test_missing_project_reports_rules_missing(self):
        w = fresh(CONCAVE)
        os.remove(w.pro_path)
        info = adapter.load_board_with_info(w.board_path)
        self.assertIsNone(info.board.rules.clearance_nm)
        self.assertIsNone(info.board.rules.copper_edge_clearance_nm)
        self.assertIn("rule_missing:project_file", info.warnings)
        self.assertIn("rule_missing:clearance", info.warnings)
        self.assertIn("rule_missing:copper_edge_clearance", info.warnings)

    def test_load_from_board_object(self):
        w = fresh()
        pcb = adapter.open_board(w.board_path)
        self.assertEqual(norm(adapter.load_board(pcb)), norm(adapter.load_board(w.board_path)))

    def test_roundtrip_empty_changeset(self):
        w = fresh()
        before = adapter.load_board(w.board_path)
        pcb = adapter.open_board(w.board_path)
        adapter.apply_changeset(pcb, ChangeSet())
        out = fx.clone(w, os.path.join(ROOT, "rt%03d" % _counter[0]))
        adapter.save_board(pcb, out.board_path)
        self.assertEqual(norm(adapter.load_board(out.board_path)), norm(before))

    def test_apply_part_via_track_edge(self):
        w = fresh()
        model = adapter.load_board(w.board_path)
        r2 = fx.part(model, "R2")
        t1 = fx.track(model, "N1")
        v1 = fx.via(model, "N1")
        top = fx.edge_between(model, (100, 100), (160, 100))
        pcb = adapter.open_board(w.board_path)
        adapter.apply_changeset(
            pcb,
            ChangeSet(
                part_moves=(PartMove(r2.uuid, mm(0.5), mm(-0.25)),),
                via_moves=(ViaMove(v1.uuid, mm(-1), 0),),
                track_changes=(TrackChange(t1.uuid, t1.start, (mm(105), mm(110))),),
                edge_changes=(EdgeChange(top.uuid, (top.start[0], top.start[1] + mm(1)), (top.end[0], top.end[1] + mm(1))),),
            ),
        )
        out = fx.clone(w, os.path.join(ROOT, "ap%03d" % _counter[0]))
        adapter.save_board(pcb, out.board_path)
        after = adapter.load_board(out.board_path)
        r2b = fx.part(after, "R2")
        self.assertEqual(r2b.position, (r2.position[0] + mm(0.5), r2.position[1] - mm(0.25)))
        for pa, pb in zip(sorted(r2.pads, key=lambda p: p.uuid), sorted(r2b.pads, key=lambda p: p.uuid)):
            self.assertEqual(pb.center, (pa.center[0] + mm(0.5), pa.center[1] - mm(0.25)))
        self.assertEqual(fx.via(after, "N1").pos, (v1.pos[0] - mm(1), v1.pos[1]))
        t1b = next(t for t in after.tracks if t.uuid == t1.uuid)
        self.assertEqual((t1b.start, t1b.end), (t1.start, (mm(105), mm(110))))
        moved = next(e for e in after.outline.edges if e.uuid == top.uuid)
        self.assertEqual(moved.start[1], top.start[1] + mm(1))

    def test_apply_bend_splits_track(self):
        w = fresh()
        model = adapter.load_board(w.board_path)
        t1 = fx.track(model, "N1")
        bend = (mm(106), mm(110.4))
        pcb = adapter.open_board(w.board_path)
        adapter.apply_changeset(
            pcb, ChangeSet(track_changes=(TrackChange(t1.uuid, t1.start, t1.end, bend),))
        )
        out = fx.clone(w, os.path.join(ROOT, "bd%03d" % _counter[0]))
        adapter.save_board(pcb, out.board_path)
        after = adapter.load_board(out.board_path)
        n1 = [t for t in after.tracks if t.net == "N1"]
        self.assertEqual(len(n1), 2)
        self.assertEqual(len({(t.layer, t.width, t.net) for t in n1}), 1)
        first = next(t for t in n1 if t.uuid == t1.uuid)
        other = next(t for t in n1 if t.uuid != t1.uuid)
        self.assertEqual((first.start, first.end), (t1.start, bend))
        self.assertEqual((other.start, other.end), (bend, t1.end))

    def test_unknown_uuid_changes_nothing(self):
        w = fresh()
        model = adapter.load_board(w.board_path)
        r1 = fx.part(model, "R1")
        pcb = adapter.open_board(w.board_path)
        with self.assertRaises(adapter.AdapterError) as cm:
            adapter.apply_changeset(
                pcb, ChangeSet(part_moves=(PartMove(r1.uuid, mm(1), 0), PartMove("no-such-uuid", 1, 1)))
            )
        self.assertEqual(cm.exception.code, "unknown_uuid")
        self.assertEqual(fx.part(adapter.load_board(pcb), "R1").position, r1.position)


# ----------------------------------------------------------------------------- DRC comparison


class DrcCompareTests(unittest.TestCase):
    def report(self, *violations):
        return {
            "violations": [
                {"type": t, "severity": "error", "description": t, "items": [{"uuid": u} for u in us]}
                for t, us in violations
            ]
        }

    def test_introduced_violation_flagged_at_equal_count(self):
        original = self.report(("clearance", ["A", "B"]))
        candidate = self.report(("clearance", ["A", "C"]))
        result = session.compare_drc(original, candidate)
        self.assertEqual(result["original_count"], result["candidate_count"])
        self.assertTrue(result["regressions"])
        self.assertEqual([n["uuids"] for n in result["new"]], [["A", "C"]])
        self.assertEqual([n["uuids"] for n in result["resolved"]], [["A", "B"]])

    def test_same_identity_in_any_order_is_not_new(self):
        original = self.report(("clearance", ["A", "B"]), ("courtyards_overlap", ["X", "Y"]))
        candidate = self.report(("courtyards_overlap", ["Y", "X"]), ("clearance", ["B", "A"]))
        result = session.compare_drc(original, candidate)
        self.assertFalse(result["regressions"])
        self.assertEqual(result["new"], [])

    def test_extra_instance_of_known_identity_is_new(self):
        original = self.report(("clearance", ["A", "B"]))
        candidate = self.report(("clearance", ["A", "B"]), ("clearance", ["A", "B"]))
        self.assertTrue(session.compare_drc(original, candidate)["regressions"])

    def test_unconnected_items_are_compared_too(self):
        original = {"violations": [], "unconnected_items": []}
        candidate = {"violations": [], "unconnected_items": [
            {"type": "unconnected_items", "severity": "error", "items": [{"uuid": "P1"}, {"uuid": "P2"}]}]}
        self.assertTrue(session.compare_drc(original, candidate)["regressions"])


# ----------------------------------------------------------------------------- session


def preview_fake(board_path, request, original_items=(), candidate_items=(), **kw):
    return session.preview(
        board_path, request, render=False,
        drc_runner=drc_runner(list(original_items), list(candidate_items)), **kw,
    )


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.w = fresh()
        self.model = adapter.load_board(self.w.board_path)
        self.original = read(self.w.board_path)
        self.original_sha = session.sha256_file(self.w.board_path)

    def listing(self):
        return sorted(n for n in os.listdir(self.w.root) if n != ".adaptive")

    def test_preview_never_touches_original(self):
        before = (read(self.w.board_path), read(self.w.pro_path), self.listing())
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        self.assertEqual(res["report"]["status"], "ok", res["report"])
        self.assertEqual((read(self.w.board_path), read(self.w.pro_path), self.listing()), before)
        self.assertTrue(os.path.isfile(res["candidate_path"]))
        self.assertTrue(res["candidate_path"].startswith(os.path.join(self.w.root, ".adaptive")))
        self.assertNotEqual(read(res["candidate_path"]), self.original)
        journal = json.loads(read(os.path.join(res["session"], "session.json")))
        self.assertEqual(journal["sha256"]["original"], self.original_sha)
        self.assertEqual(journal["sha256"]["candidate"], session.sha256_file(res["candidate_path"]))
        self.assertEqual(journal["status"], "previewed")
        self.assertEqual(journal["request"]["driver"], cascade_request(self.model)["driver"])

    def test_plan_leaves_nothing_behind(self):
        before = (read(self.w.board_path), os.listdir(self.w.root))
        out = session.plan(self.w.board_path, cascade_request(self.model))
        self.assertEqual(out["report"]["status"], "ok")
        self.assertEqual((read(self.w.board_path), os.listdir(self.w.root)), before)

    def test_real_preview_artifacts(self):
        res = session.preview(self.w.board_path, cascade_request(self.model))
        self.assertEqual(read(self.w.board_path), self.original)
        self.assertTrue(res["png_path"] or res["svg_path"], res.get("render"))
        for path in (res["png_path"], res["svg_path"]):
            if path:
                self.assertTrue(os.path.isfile(path))
        drc = res["drc"]
        self.assertIn("available", drc)
        if drc["available"]:
            self.assertIsInstance(drc["original_count"], int)
            self.assertIsInstance(drc["candidate_count"], int)
            self.assertIn("new", drc)
        self.assertIn("drc_regressions", res)

    def test_cascade_pushes_part_and_reaims_track(self):
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        report = res["report"]
        self.assertEqual(report["status"], "ok", report)
        r1, r2 = fx.part(self.model, "R1"), fx.part(self.model, "R2")
        moves = {m["uuid"]: m for m in res["changeset"]["part_moves"]}
        self.assertIn(r1.uuid, moves)
        self.assertIn(r2.uuid, moves, "R1 must push R2")
        self.assertNotIn(fx.part(self.model, "R3").uuid, moves)
        tc = {t["uuid"]: t for t in res["changeset"]["track_changes"]}
        n1, n4 = fx.track(self.model, "N1"), fx.track(self.model, "N4")
        self.assertIn(n1.uuid, tc, "the attached track of the driver must re-aim")
        self.assertIn(n4.uuid, tc, "the attached track of the pushed part must re-aim")

        after = adapter.load_board(res["candidate_path"])
        r2b = fx.part(after, "R2")
        self.assertEqual(r2b.position, (r2.position[0] + moves[r2.uuid]["dx"], r2.position[1] + moves[r2.uuid]["dy"]))
        # the N4 copper still runs from R2's moved pad to R3's pad, with the bend as its only inner node
        new_pad = next(p for p in r2b.pads if p.net == "N4").center
        r3_pad = next(p for p in fx.part(after, "R3").pads if p.net == "N4").center
        ends = Counter(pt for t in after.tracks if t.net == "N4" for pt in (t.start, t.end))
        self.assertEqual(ends[new_pad], 1)
        self.assertEqual(ends[r3_pad], 1)
        self.assertTrue(all(c in (1, 2) for c in ends.values()))
        self.assertEqual(len([t for t in after.tracks if t.net == "N1"]), 2, "bend split expected for N1")

    def test_connectivity_unchanged_after_apply(self):
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        self.assertEqual(res["report"]["status"], "ok", res["report"])
        before = adapter.connectivity_stats(adapter.open_board(self.w.board_path))
        session.apply(res["session"], self.original_sha)
        after = adapter.connectivity_stats(adapter.open_board(self.w.board_path))
        self.assertIsNotNone(before["unconnected"])
        self.assertEqual(after["unconnected"], before["unconnected"])
        self.assertEqual(after["pad_nets"], before["pad_nets"])
        self.assertEqual(res["connectivity"]["unconnected_before"], res["connectivity"]["unconnected_after"])
        self.assertTrue(res["connectivity"]["pad_nets_unchanged"])

    def test_drc_identity_regression_blocks_apply_without_force(self):
        res = preview_fake(
            self.w.board_path, cascade_request(self.model),
            original_items=[("clearance", ["A", "B"])], candidate_items=[("clearance", ["A", "C"])],
        )
        self.assertEqual(res["drc"]["original_count"], res["drc"]["candidate_count"])
        self.assertTrue(res["drc_regressions"])
        self.assertIn("drc_new_violations", res["regression_reasons"])
        with self.assertRaises(session.SessionRefused) as cm:
            session.apply(res["session"], self.original_sha)
        self.assertEqual(cm.exception.code, "drc_regressions")
        self.assertEqual(read(self.w.board_path), self.original)
        out = session.apply(res["session"], self.original_sha, force=True)
        self.assertTrue(out["forced"])
        self.assertEqual(session.sha256_file(self.w.board_path), res["candidate_sha256"])

    def test_unavailable_drc_counts_as_regression(self):
        def broken(board_path, out_json, cli_path, timeout):
            raise session.SessionError("drc_failed", "boom")

        res = session.preview(self.w.board_path, cascade_request(self.model), render=False, drc_runner=broken)
        self.assertFalse(res["drc"]["available"])
        self.assertTrue(res["drc_regressions"])
        self.assertEqual(res["regression_reasons"], ["drc_unavailable"])

    def test_apply_refuses_on_changed_original(self):
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        with open(self.w.board_path, "ab") as fh:
            fh.write(b"\n")
        edited = read(self.w.board_path)
        with self.assertRaises(session.SessionRefused) as cm:
            session.apply(res["session"], self.original_sha)
        self.assertEqual(cm.exception.code, "original_changed")
        self.assertEqual(read(self.w.board_path), edited)
        self.assertFalse(os.path.exists(os.path.join(res["session"], "backup.kicad_pcb")))

    def test_apply_refuses_wrong_expected_sha(self):
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        with self.assertRaises(session.SessionRefused) as cm:
            session.apply(res["session"], "0" * 64)
        self.assertEqual(cm.exception.code, "expected_sha256_mismatch")
        self.assertEqual(read(self.w.board_path), self.original)

    def test_apply_then_undo_is_byte_identical(self):
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        applied = session.apply(res["session"], self.original_sha)
        self.assertEqual(session.sha256_file(self.w.board_path), res["candidate_sha256"])
        self.assertEqual(read(self.w.board_path), read(res["candidate_path"]))
        self.assertEqual(applied["backup_sha256"], self.original_sha)
        self.assertEqual(read(applied["backup_path"]), self.original)
        self.assertEqual([n for n in os.listdir(self.w.root) if n.endswith(".tmp")], [])
        with self.assertRaises(session.SessionRefused) as again:
            session.apply(res["session"], self.original_sha)
        self.assertEqual(again.exception.code, "already_applied")
        undone = session.undo(res["session"])
        self.assertEqual(undone["status"], "undone")
        self.assertEqual(read(self.w.board_path), self.original)
        self.assertEqual(session.sha256_file(self.w.board_path), self.original_sha)
        with self.assertRaises(session.SessionRefused) as twice:
            session.undo(res["session"])
        self.assertEqual(twice.exception.code, "not_applied")

    def test_undo_refuses_after_external_edit(self):
        res = preview_fake(self.w.board_path, cascade_request(self.model))
        session.apply(res["session"], self.original_sha)
        with open(self.w.board_path, "ab") as fh:
            fh.write(b"\n# external edit\n")
        edited = read(self.w.board_path)
        with self.assertRaises(session.SessionRefused) as cm:
            session.undo(res["session"])
        self.assertEqual(cm.exception.code, "board_changed_since_apply")
        self.assertEqual(read(self.w.board_path), edited)

    def test_refill_changes_zone_filled_area(self):
        res = preview_fake(self.w.board_path, pour_request(self.model))
        self.assertEqual(res["report"]["status"], "ok", res["report"])
        self.assertEqual([m["uuid"] for m in res["changeset"]["part_moves"]], [fx.part(self.model, "R4").uuid])
        zone = self.model.zones[0].uuid
        before = adapter.zone_filled_areas(adapter.open_board(self.w.board_path))[zone]
        after = adapter.zone_filled_areas(adapter.open_board(res["candidate_path"]))[zone]
        self.assertGreater(before, 0)
        self.assertLess(after, before, "pads of another net inside the pour must cut it")
        self.assertEqual(res["refill"]["zones_refilled"], 1)
        self.assertIn(zone, res["refill"]["changed"])
        self.assertEqual(res["refill"]["filled_area_after_nm2"][zone], after)

    def test_blocked_or_rejected_request_keeps_no_session(self):
        bad = cascade_request(self.model)
        bad["driver"] = {"kind": "part", "uuids": ["not-a-part"]}
        res = preview_fake(self.w.board_path, bad)
        self.assertEqual(res["status"], "rejected")
        self.assertIsNone(res["session"])
        self.assertIsNone(res["candidate_path"])
        self.assertFalse(os.path.exists(os.path.join(self.w.root, ".adaptive")))
        self.assertEqual(read(self.w.board_path), self.original)

    def test_missing_board_is_an_error(self):
        with self.assertRaises(session.SessionError) as cm:
            session.preview(os.path.join(self.w.root, "nope.kicad_pcb"), {})
        self.assertEqual(cm.exception.code, "board_not_found")


# ----------------------------------------------------------------------------- CLI


class CliTests(unittest.TestCase):
    def setUp(self):
        self.w = fresh()
        self.model = adapter.load_board(self.w.board_path)
        self.sha = session.sha256_file(self.w.board_path)

    def request_file(self, data) -> str:
        path = os.path.join(self.w.root, "req%d.json" % len(os.listdir(self.w.root)))
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(data, str):
                fh.write(data)
            else:
                json.dump(data, fh)
        return path

    def run_cli(self, command, data=None):
        argv = [command]
        if data is not None:
            argv += ["--json", self.request_file(data)]
        out = io.StringIO()
        code = cli.main(argv, out)
        text = out.getvalue()
        self.assertEqual(len(text.strip().splitlines()), 1, text)
        return code, json.loads(text)

    def test_exit_0_capability_plan_preview_apply_undo(self):
        code, obj = self.run_cli("capability")
        self.assertEqual(code, 0)
        self.assertTrue(obj["ok"])
        self.assertTrue(obj["pcbnew"]["available"])
        code, obj = self.run_cli("plan", {"board": self.w.board_path, "request": cascade_request(self.model)})
        self.assertEqual((code, obj["ok"], obj["report"]["status"]), (0, True, "ok"))
        code, obj = self.run_cli(
            "preview", {"board": self.w.board_path, "request": cascade_request(self.model), "render": False})
        self.assertEqual((code, obj["ok"]), (0, True), obj)
        # real DRC ran in the preview; force keeps the apply/undo exit codes independent of its verdict
        code, applied = self.run_cli(
            "apply", {"session": obj["session"], "expected_sha256": self.sha, "force": bool(obj["drc_regressions"])})
        self.assertEqual((code, applied["ok"]), (0, True), applied)
        code, undone = self.run_cli("undo", {"session": obj["session"]})
        self.assertEqual((code, undone["ok"]), (0, True), undone)
        self.assertEqual(session.sha256_file(self.w.board_path), self.sha)

    def test_exit_2_refused(self):
        bad = cascade_request(self.model)
        bad["driver"] = {"kind": "part", "uuids": ["not-a-part"]}
        code, obj = self.run_cli("plan", {"board": self.w.board_path, "request": bad})
        self.assertEqual(code, 2)
        self.assertTrue(obj["refused"] and not obj["ok"])
        self.assertEqual(obj["code"], "rejected:unknown_uuid")

        res = preview_fake(self.w.board_path, cascade_request(self.model))
        code, obj = self.run_cli("apply", {"session": res["session"], "expected_sha256": "f" * 64})
        self.assertEqual(code, 2)
        self.assertTrue(obj["refused"])
        self.assertEqual(obj["code"], "expected_sha256_mismatch")
        code, obj = self.run_cli("undo", {"session": res["session"]})
        self.assertEqual((code, obj["code"]), (2, "not_applied"))

    def test_exit_1_error(self):
        code, obj = self.run_cli("plan", "{not json")
        self.assertEqual((code, obj["ok"], obj["error"]), (1, False, True))
        code, obj = self.run_cli("plan", {"board": os.path.join(self.w.root, "missing.kicad_pcb"),
                                          "request": cascade_request(self.model)})
        self.assertEqual((code, obj["code"]), (1, "board_not_found"))
        code, obj = self.run_cli("undo", {"session": os.path.join(self.w.root, ".adaptive", "nope")})
        self.assertEqual((code, obj["code"]), (1, "session_not_found"))
        code, obj = self.run_cli("apply", {"session": "x"})
        self.assertEqual((code, obj["code"]), (1, "bad_request"))
        code, obj = self.run_cli("frobnicate")
        self.assertEqual((code, obj["code"]), (1, "usage"))

    def test_script_prints_one_json_object_and_exit_code(self):
        bad = cascade_request(self.model)
        bad["driver"] = {"kind": "part", "uuids": ["not-a-part"]}
        req = self.request_file({"board": self.w.board_path, "request": bad})
        script = os.path.join(os.path.dirname(os.path.abspath(cli.__file__)), "cli.py")
        res = session.run_hidden([sys.executable, "-X", "utf8", "-B", script, "plan", "--json", req], 180.0,
                                 cwd=self.w.root)
        self.assertFalse(res.timed_out, res.stderr)
        self.assertEqual(res.returncode, 2, res.stderr)
        obj = json.loads(res.stdout)
        self.assertEqual(obj["code"], "rejected:unknown_uuid")
        self.assertEqual(len(res.stdout.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
