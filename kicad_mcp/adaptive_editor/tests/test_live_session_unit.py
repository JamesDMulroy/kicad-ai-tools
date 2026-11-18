"""LiveSession's stale check and worker errors without KiCad: fake board items carry a protobuf-like
``proto``. Needs kipy (the live venv); skipped where it is not installed."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

try:
    import kipy  # noqa: F401
except ImportError:  # the engine tests also run under KiCad's own python, which has no kipy
    kipy = None

if kipy is not None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "live"))
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import live_session as ls


class _Proto:
    def __init__(self, data: bytes):
        self.data = data

    def SerializeToString(self, deterministic=False):
        return self.data


class _Item:
    def __init__(self, uid: str, data: bytes):
        self.id = type("Id", (), {"value": uid})()
        self.proto = _Proto(data)


class _Board:
    def __init__(self, **collections):
        self.collections = collections

    def __getattr__(self, name):
        if name.startswith("get_") and name[4:] in self.collections:
            return lambda: list(self.collections[name[4:]])
        raise AttributeError(name)


@unittest.skipIf(kipy is None, "kipy (the live venv) is not installed")
class StaleStateTests(unittest.TestCase):
    def session(self, board):
        s = object.__new__(ls.LiveSession)
        s.board = board
        return s

    def test_any_change_to_any_item_kind_changes_the_board_state(self):
        zone = _Item("z1", b"zone outline A")
        arc = _Item("a1", b"arc start end mid=1")
        board = _Board(footprints=[_Item("f1", b"fp")], tracks=[arc], vias=[], shapes=[], zones=[zone],
                       text=[], dimensions=[], groups=[])
        before = self.session(board)._board_state()
        zone.proto.data = b"zone outline B"  # a keepout or zone edited
        self.assertNotEqual(before, self.session(board)._board_state())
        zone.proto.data = b"zone outline A"
        arc.proto.data = b"arc start end mid=2"  # only the arc midpoint moved
        self.assertNotEqual(before, self.session(board)._board_state())
        arc.proto.data = b"arc start end mid=1"
        board.collections["text"].append(_Item("t1", b"new text"))  # an item added
        self.assertNotEqual(before, self.session(board)._board_state())
        board.collections["text"].clear()
        self.assertEqual(before, self.session(board)._board_state())  # back to identical: equal again


    def test_shapes_without_end_points_never_break_the_snapshot(self):
        from kipy.board_types import BoardCircle, BoardPolygon, BoardRectangle
        circle, rect, poly = BoardCircle(), BoardRectangle(), BoardPolygon()  # e.g. a mounting-hole circle
        for n, shape in enumerate((circle, rect, poly)):
            shape.proto.id.value = f"shape-{n}"  # board items always carry distinct ids
        board = _Board(footprints=[], tracks=[], vias=[], shapes=[circle, rect, poly], zones=[], text=[],
                       dimensions=[], groups=[])
        session = self.session(board)
        self.assertEqual(session._board_sigs(), {})  # not move targets: no geometry signature, no crash
        self.assertEqual(len(session._board_state()), 3)  # still covered by the whole-state stale check

    def test_a_stale_check_that_cannot_see_everything_refuses(self):
        board = _Board(footprints=[], tracks=[], vias=[], shapes=[], zones=[], text=[], dimensions=[])  # no groups
        with self.assertRaises(ls.LiveError) as caught:
            self.session(board)._board_state()
        self.assertEqual(caught.exception.code, "stale_check_unavailable")
        item = _Item("x", b"")
        del item.proto
        board = _Board(footprints=[item], tracks=[], vias=[], shapes=[], zones=[], text=[], dimensions=[],
                       groups=[])
        with self.assertRaises(ls.LiveError):
            self.session(board)._board_state()


@unittest.skipIf(kipy is None, "kipy (the live venv) is not installed")
class WorkerErrorTests(unittest.TestCase):
    def test_a_broken_pipe_is_a_typed_worker_error(self):
        class Gone:
            def write(self, _text):
                raise BrokenPipeError("worker exited")

            def flush(self):
                pass

        worker = object.__new__(ls.EngineWorker)
        worker.proc = type("P", (), {"stdin": Gone(), "stdout": None})()
        with self.assertRaises(ls.LiveError) as caught:
            worker.call({"op": "ping"})
        self.assertEqual(caught.exception.code, "worker_died")

    def test_a_garbled_reply_is_a_typed_protocol_error(self):
        import io
        worker = object.__new__(ls.EngineWorker)
        worker.proc = type("P", (), {"stdin": io.StringIO(), "stdout": io.StringIO("not json\n")})()
        with self.assertRaises(ls.LiveError) as caught:
            worker.call({"op": "ping"})
        self.assertEqual(caught.exception.code, "worker_protocol")


if __name__ == "__main__":
    unittest.main()
