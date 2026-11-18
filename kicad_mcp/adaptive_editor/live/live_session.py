"""LiveSession: adaptive edits in the running KiCad 10 PCB Editor through the IPC API (kipy).

KiCad 10.0.5's IPC API does not show staged (uncommitted) changes, and only the first update per item
inside a commit takes effect (tests_live/probe_commit_visibility.py). So (LIVE-PLUGIN-SPEC §2):
  start()  snapshot the live board text (UUIDs preserved) plus the project's rule files; the engine
           worker (KiCad's python, pcbnew) loads it as the baseline; record each item's baseline state.
  update() solve from the baseline for the current request; returns the plan and a preview (SVG or
           drawing primitives). The live board is NOT touched.
  apply()  stale check (every touched item must still equal the snapshot, else ``concurrent_edit``),
           then ONE commit writing each touched item once and creating bend segments -> one undo step.
           Zone refill is optional and a separate KiCad operation.
  cancel() nothing was written; just end the session.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from kipy.geometry import Vector2


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # adaptive_editor/ (kicad_env)
import kicad_env  # noqa: E402

WORKER = str(Path(__file__).resolve().parent / "engine_worker.py")
CREATE_NO_WINDOW = 0x08000000
SEM_FLAGS = 0x0001 | 0x0002 | 0x8000  # inherited by the worker: no crash / error dialogs


class LiveError(RuntimeError):
    def __init__(self, code, message=""):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def worker_argv() -> list[str]:
    py = kicad_env.kicad_python()
    if py is None:
        raise LiveError("kicad_python_unavailable", "KiCad's bundled python (pcbnew) was not found on this platform")
    return [str(py), "-X", "utf8", "-B", WORKER]


def _default_spawn(argv):
    """Windowless worker; the no-crash-dialog error mode is set around creation so the child inherits it."""
    flags, old = 0, None
    if os.name == "nt":
        import ctypes
        flags = CREATE_NO_WINDOW
        old = ctypes.windll.kernel32.SetErrorMode(SEM_FLAGS)
    try:
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, encoding="utf-8", creationflags=flags)
    finally:
        if old is not None:
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(old)


class EngineWorker:
    """The engine under KiCad's python, one JSON line per request. ``spawn(argv)`` must return an object
    with text ``stdin``/``stdout`` streams, ``poll()``, ``wait(timeout)`` and ``kill()`` (tests pass a
    private-desktop spawner)."""

    def __init__(self, spawn=None):
        self.proc = (spawn or _default_spawn)(worker_argv())

    def call(self, msg):
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n"); self.proc.stdin.flush()
            line = self.proc.stdout.readline()
        except (OSError, ValueError) as exc:  # BrokenPipeError, or a closed stream
            raise LiveError("worker_died", repr(exc)) from exc
        if not line:
            raise LiveError("worker_died")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise LiveError("worker_protocol", line[:200]) from exc

    def close(self):
        if self.proc.poll() is None:
            try:
                self.call({"op": "quit"})
            except Exception:  # a dead or wedged worker is killed below
                pass
            try:
                self.proc.wait(10)
            except Exception:
                self.proc.kill()
        if hasattr(self.proc, "close"):
            self.proc.close()


def _xy(v):
    return (int(v.x), int(v.y))


def _item_digest(item) -> str:
    """sha256 of the item's serialized IPC protobuf (deterministic), its complete state."""
    import hashlib
    proto = getattr(item, "proto", None)
    if proto is None:  # fail closed: without its serialized state an item's edits cannot be detected
        raise LiveError("stale_check_unavailable", f"{type(item).__name__} exposes no IPC state")
    return hashlib.sha256(proto.SerializeToString(deterministic=True)).hexdigest()


@temporary_owner(cleanup='_close', allocating=('start',))
class LiveSession:
    def __init__(self, board, spawn=None):
        self.board = board
        self.spawn = spawn
        self.worker = None
        self.snapshot_sigs: dict[str, tuple] = {}  # uuid -> baseline state signature
        self.last = None

    # ---------------------------------------------------------------- lifecycle
    STATE_COLLECTIONS = ("get_footprints", "get_tracks", "get_vias", "get_shapes", "get_zones", "get_text",
                         "get_dimensions", "get_groups")

    def _board_state(self) -> dict[str, str]:
        """Every board item (footprints, tracks and arcs, vias, shapes, zones and keepouts, text, dimensions,
        groups) keyed by collection and id, valued by a digest of its WHOLE serialized state (the IPC
        protobuf): any edit at all - an arc midpoint, a rotation, a flip, a layer, a zone outline, a text
        change - or an added or deleted item changes it."""
        state = {}
        for getter in self.STATE_COLLECTIONS:
            fetch = getattr(self.board, getter, None)
            if fetch is None:  # fail closed: a stale check that cannot see a collection is no check
                raise LiveError("stale_check_unavailable", f"this kipy has no Board.{getter}")
            for it in fetch():
                state[f"{getter[4:]}:{it.id.value}"] = _item_digest(it)
        return state

    def _board_sigs(self) -> dict[str, tuple]:
        """Geometry of every item the plan can move (the values apply writes from): footprints, vias,
        tracks and arcs, and segment-like shapes (segments, arcs, beziers). Circles, rectangles and
        polygons have no position or end points; they are never move targets, and the whole-state stale
        check (``_board_state``) still covers them."""
        sigs = {}
        for coll in (self.board.get_footprints(), self.board.get_tracks(), self.board.get_vias(),
                     self.board.get_shapes()):
            for it in coll:
                sig = self._sig(it)
                if sig is not None:
                    sigs[str(it.id.value)] = sig
        return sigs

    def start(self):
        # covers: ADE-5
        # Signatures before and after the text snapshot must agree, so baseline and signatures match.
        for _attempt in range(3):
            state_before = self._board_state()
            before = self._board_sigs()
            text = self.board.get_as_string()
            if self._board_state() == state_before:
                break
        else:
            raise LiveError("concurrent_edit", "the board kept changing while the snapshot was taken")
        self.tmpdir = scoped_mkdtemp(prefix="adaptive-live-")
        doc = self.board.document
        stem = Path(doc.board_filename).stem if doc.board_filename else "snapshot"
        proj_dir = Path(doc.project.path) if doc.project.path else None
        snap = os.path.join(self.tmpdir, stem + ".kicad_pcb")
        with open(snap, "w", encoding="utf-8") as fh:
            fh.write(text)
        # The engine needs the project's rules (.kicad_pro netclasses, .kicad_dru); it never invents them.
        self.rule_files = []
        if proj_dir and proj_dir.is_dir():
            for ext in (".kicad_pro", ".kicad_dru"):
                src = proj_dir / (stem + ext)
                if src.is_file():
                    import shutil
                    shutil.copyfile(src, os.path.join(self.tmpdir, stem + ext)); self.rule_files.append(str(src))
        self.worker = EngineWorker(self.spawn)
        r = self.worker.call({"op": "load", "board": snap})
        if not r.get("ok"):
            self.worker.close(); raise LiveError("load_failed", str(r))
        self.snapshot_sigs = before  # geometry the plan is applied from
        self.snapshot_state = state_before  # every item's whole state, for the stale check at apply
        return r

    def _fetch(self, uuids):
        items = self.board.get_items_by_id([_kiid(u) for u in uuids]) if uuids else []
        return {str(i.id.value): i for i in items}

    @staticmethod
    def _sig(item):
        """("pos", x, y, ...) for footprints/vias, ("seg", sx, sy, ex, ey, ...) for tracks/shapes; the tail
        carries orientation, layer and width where the item has them, so a rotation, flip or re-layer
        between preview and apply is detected."""
        extra = []
        orientation = getattr(item, "orientation", None)
        if orientation is not None:
            extra.append(round(float(getattr(orientation, "degrees", orientation)), 6))
        for name in ("layer", "width"):
            value = getattr(item, name, None)
            if value is not None:
                extra.append(int(value) if isinstance(value, (int, float)) else str(value))
        if hasattr(item, "position") and not hasattr(item, "start"):
            return ("pos",) + _xy(item.position) + tuple(extra)
        if hasattr(item, "start") and hasattr(item, "end"):
            return ("seg",) + _xy(item.start) + _xy(item.end) + tuple(extra)
        return None  # e.g. a board circle, rectangle or polygon: not something a plan moves

    @staticmethod
    def _set(item, sig):
        """Write only the geometry part of a signature (x/y, or segment endpoints)."""
        if sig[0] == "pos":
            item.position = Vector2.from_xy(sig[1], sig[2])
        else:
            item.start = Vector2.from_xy(sig[1], sig[2]); item.end = Vector2.from_xy(sig[3], sig[4])

    def update(self, request: dict, render=True) -> dict:
        """Solve from the baseline snapshot. Does NOT touch the live board (preview only)."""
        r = self.worker.call({"op": "solve", "request": request, "render": render})
        if not r.get("ok"):
            raise LiveError(r.get("code", "solve_failed"), r.get("message", ""))
        self.last = r
        return {"status": r.get("status"), "report": r.get("report"), "changeset": r["changeset"],
                "svg": r.get("svg"), "prims": r.get("prims")}

    def edges(self) -> list[dict]:
        """Board-outline edges of the snapshot: [{"uuid", "start", "end"}] in nm."""
        r = self.worker.call({"op": "edges"})
        if not r.get("ok"):
            raise LiveError(r.get("code", "edges_failed"), r.get("message", ""))
        return r["edges"]

    def apply(self, message: str = "Adaptive move", refill: bool = False) -> dict:
        # covers: ADE-5
        """Write the last previewed ChangeSet in ONE commit (one undo step); each item written once."""
        if not self.last:
            raise LiveError("nothing_to_apply")
        cs = self.last["changeset"]
        touched = ([m["uuid"] for m in cs["part_moves"]] + [m["uuid"] for m in cs["via_moves"]]
                   + [t["uuid"] for t in cs["track_changes"]] + [e["uuid"] for e in cs["edge_changes"]])
        if not touched:
            self._close(); return {"applied": False, "reason": "empty_changeset"}
        live = self._fetch(touched)
        missing = [u for u in touched if u not in live]
        if missing:
            raise LiveError("unknown_item", ",".join(missing))
        # Stale check over the WHOLE board: a moved obstacle, a rotation or flip, an added or deleted item
        # since the snapshot all make the plan invalid.
        now = self._board_state()
        if now != self.snapshot_state:
            changed = sorted(u for u in set(now) | set(self.snapshot_state)
                             if now.get(u) != self.snapshot_state.get(u))
            raise LiveError("concurrent_edit", f"{len(changed)} item(s) changed since preview, e.g. {changed[:3]}")
        unsupported = [u for u in touched if u not in self.snapshot_sigs]
        if unsupported:
            raise LiveError("unsupported_item", f"the plan moves items it cannot write: {unsupported[:3]}")
        targets: dict[str, tuple] = {}
        for m in cs["part_moves"] + cs["via_moves"]:
            b = self.snapshot_sigs[m["uuid"]]
            targets[m["uuid"]] = ("pos", b[1] + m["dx"], b[2] + m["dy"])  # only x/y are written
        bends = []
        for tc in cs["track_changes"]:
            end = tc["end"] if tc["bend"] is None else tc["bend"]
            targets[tc["uuid"]] = ("seg", tc["start"][0], tc["start"][1], end[0], end[1])
            if tc["bend"] is not None:
                bends.append((tc["uuid"], tc["bend"], tc["end"]))
        for e in cs["edge_changes"]:
            targets[e["uuid"]] = ("seg", e["start"][0], e["start"][1], e["end"][0], e["end"][1])
        commit = self.board.begin_commit()
        try:
            items = []
            for u, sig in targets.items():
                it = live[u]; self._set(it, sig); items.append(it)
            self.board.update_items(items)
            if bends:
                from kipy.board_types import Track
                made = []
                for u, bend, end in bends:
                    base = live[u]
                    tr = Track()
                    tr.start = Vector2.from_xy(*bend); tr.end = Vector2.from_xy(*end)
                    tr.width = base.width; tr.layer = base.layer; tr.net = base.net
                    made.append(tr)
                self.board.create_items(made)
            self.board.push_commit(commit, message)
        except BaseException as exc:
            try:
                self.board.drop_commit(commit)
            except Exception as drop_exc:  # keep the original error; record the cleanup failure on it
                exc.add_note(f"drop_commit also failed: {drop_exc!r}")
            raise
        refilled = False
        if refill:
            self.board.refill_zones(); refilled = True  # a separate KiCad operation (teardrops regenerate)
        self._close()
        return {"applied": True, "items": len(targets), "bends": len(bends), "refilled": refilled}

    def cancel(self):
        """Nothing was written to the board; just end the session."""
        self.last = None
        self._close()

    def _close(self):
        if self.worker:
            self.worker.close(); self.worker = None


def _kiid(uuid: str):
    from kipy.proto.common.types import KIID
    k = KIID(); k.value = uuid
    return k
