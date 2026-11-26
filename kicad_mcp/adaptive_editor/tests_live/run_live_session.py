"""Live test: LiveSession against real KiCad on a private desktop. Run with a Python that has kicad-python.

Asserts:
- preview only (the board is unchanged after update);
- A->B->A gives an identical plan (baseline-relative, no drift);
- apply writes the planned final state in one commit;
- a second session's cancel writes nothing;
- an edit to an unrelated item after preview makes apply refuse (concurrent_edit);
- no window of the test's process tree reaches the user's desktop.

The engine worker and the fixture build also run on private desktops. Writes an evidence JSON;
exit 1 on any failed assertion.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (HiddenDesktop, HiddenKiCad, Watch, build_fixture_board, hidden_spawner,  # noqa: E402
                      write_evidence, source_digest)
from live_session import LiveError, LiveSession  # noqa: E402


def positions(board, refs):
    return {fp.reference_field.text.value: (fp.position.x, fp.position.y)
            for fp in board.get_footprints() if fp.reference_field.text.value in refs}


@cleanup_after_call
def main():
    work = Path(scoped_mkdtemp(prefix="als-"))
    res, fails, watch = {}, [], Watch()
    board_path = build_fixture_board(work)
    refs = ("R1", "R2", "R3", "R5")
    with HiddenKiCad(board_path, workdir=work / "p") as kc, HiddenDesktop("worker") as wdesk:
        from kipy import KiCad
        from kipy.geometry import Vector2
        k = KiCad(socket_path=kc.socket_url(kc.wait_socket()), timeout_ms=30000)
        brd = k.get_board()
        uuid = {fp.reference_field.text.value: str(fp.id.value) for fp in brd.get_footprints()}
        base = positions(brd, refs)
        spawn = hidden_spawner(wdesk)

        # A board circle (like a mounting-hole marking) has neither position nor end points: every session
        # below must snapshot, plan and apply with it present (it is still covered by the stale check).
        from kipy.board_types import BoardCircle
        circle = BoardCircle()
        circle.center = Vector2.from_xy(5_000_000, 5_000_000)
        circle.radius_point = Vector2.from_xy(6_500_000, 5_000_000)
        from kipy.proto.board.board_types_pb2 import BoardLayer
        circle.layer = BoardLayer.BL_F_SilkS  # silkscreen: never a copper obstacle for the plan
        c0 = brd.begin_commit(); brd.create_items([circle]); brd.push_commit(c0, "fixture: board circle (test)")
        res["board_circle_present"] = any(type(s).__name__ == "BoardCircle" for s in brd.get_shapes())
        if not res["board_circle_present"]:
            fails.append("the fixture board circle was not created")

        def req(dx_mm):
            return {"driver": {"kind": "part", "uuids": [uuid["R1"]]}, "displacement_nm": [int(dx_mm * 1e6), 0],
                    "mode": "cascade", "movers": [], "frozen": [], "priority": [], "eligibility_nm": None,
                    "allow_partial": True, "copper": "adapt"}

        s = LiveSession(brd, spawn=spawn); s.start()
        a1 = s.update(req(6)); watch.check()
        if positions(brd, refs) != base:
            fails.append("update modified the live board (must be preview only)")
        s.update(req(10)); a2 = s.update(req(6))
        moves = {m["uuid"]: (m["dx"], m["dy"]) for m in a1["changeset"]["part_moves"]}
        res["status_A"] = a1["status"]; res["moves_A"] = {r: moves.get(uuid[r]) for r in refs}
        if a1["changeset"] != a2["changeset"]:
            fails.append("A->B->A changeset drift")
        if uuid["R1"] not in moves or uuid["R2"] not in moves:
            fails.append("cascade plan missing R1/R2")
        res["apply"] = s.apply("Adaptive move (test)")
        after = positions(k.get_board(), refs)
        for r in refs:
            d = moves.get(uuid[r], (0, 0))
            if after[r] != (base[r][0] + d[0], base[r][1] + d[1]):
                fails.append(f"{r} applied {after[r]} != planned")

        s2 = LiveSession(k.get_board(), spawn=spawn); s2.start(); s2.update(req(4)); s2.cancel()
        if positions(k.get_board(), refs) != after:
            fails.append("cancel changed the board")

        # an unrelated part (R5) moved after preview must make apply refuse
        s3 = LiveSession(k.get_board(), spawn=spawn); s3.start(); s3.update(req(2))
        b = k.get_board()
        r5 = [fp for fp in b.get_footprints() if fp.reference_field.text.value == "R5"][0]
        c = b.begin_commit(); r5.position = Vector2.from_xy(r5.position.x + 1_000_000, r5.position.y)
        b.update_items([r5]); b.push_commit(c, "external edit (test)")
        try:
            s3.apply("should refuse")
            fails.append("apply did not detect a concurrent edit of an unrelated item")
        except LiveError as exc:
            res["concurrent_edit"] = exc.code
            if exc.code != "concurrent_edit":
                fails.append(f"wrong refusal code {exc.code}")
        s3.cancel()

        # a rotation only (no position change) and an added board item must also make apply refuse
        from kipy.board_types import BoardText
        from kipy.geometry import Angle

        def rotate_r5(board):
            fp = [f for f in board.get_footprints() if f.reference_field.text.value == "R5"][0]
            fp.orientation = Angle.from_degrees(fp.orientation.degrees + 90)
            board.update_items([fp])

        def add_text(board):
            text = BoardText()
            text.value = "external note"
            text.position = Vector2.from_xy(1_000_000, 1_000_000)
            text.layer = board.get_footprints()[0].layer  # a layer the board already uses
            board.create_items([text])

        res["stale_cases"] = {}
        for label, edit in (("rotation_only", rotate_r5), ("added_text", add_text)):
            sx = LiveSession(k.get_board(), spawn=spawn); sx.start(); sx.update(req(2))
            b = k.get_board()
            c = b.begin_commit(); edit(b); b.push_commit(c, f"external edit: {label} (test)")
            try:
                sx.apply("should refuse")
                fails.append(f"apply did not detect a concurrent {label}")
                res["stale_cases"][label] = "applied"
            except LiveError as exc:
                res["stale_cases"][label] = exc.code
                if exc.code != "concurrent_edit":
                    fails.append(f"wrong refusal code for {label}: {exc.code}")
            sx.cancel()
        watch.check()
    res["tripwire"] = tripwire = watch.close()
    res["owner_desktop_hits"] = tripwire["hits"]
    if tripwire["hits"]:
        fails.append("a window reached the owner desktop")
    if not tripwire["observed"]:
        fails.append(f"the tripwire was blind for {tripwire['unavailable_samples']} of {tripwire['samples']} "
                     "samples (owner desktop locked?); the run does not count - run again unlocked")
    res["source_sha256"] = source_digest()
    res["fails"] = fails
    res["evidence"] = str(write_evidence("live-session", res))
    print(json.dumps(res, indent=1, default=str))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
