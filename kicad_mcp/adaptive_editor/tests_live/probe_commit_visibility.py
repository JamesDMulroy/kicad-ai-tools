"""Probe: are staged (uncommitted) IPC changes visible, and does a second update_items in one commit
count? (KiCad 10.0.5: no and no.) Runs KiCad on a private desktop; writes an evidence JSON."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import HiddenKiCad, Watch, build_fixture_board, write_evidence  # noqa: E402

with tempfile.TemporaryDirectory(prefix="probe-commit-") as scratch:
    work = Path(scratch)
    watch = Watch()
    board = build_fixture_board(work)
    out = {}
    with HiddenKiCad(board, workdir=work / "p") as kc:
        sock = kc.wait_socket()
        from kipy import KiCad
        from kipy.geometry import Vector2
        k = KiCad(socket_path=kc.socket_url(sock), timeout_ms=30000)
        b = k.get_board()
        fp = [f for f in b.get_footprints() if f.reference_field.text.value == "R1"][0]
        u = fp.id
        c = b.begin_commit()
        fp.position = Vector2.from_xy(fp.position.x + 3_000_000, fp.position.y)
        ret = b.update_items([fp])
        out["update_returned"] = [(r.position.x, r.position.y) for r in ret] if ret else ret
        out["in_commit_get_by_id"] = [(i.position.x, i.position.y) for i in b.get_items_by_id([u])]
        import re as _re
        s = b.get_as_string(); m = _re.search(r'\(property "Reference" "R1"', s)
        blk = s[max(0, s.rfind("(footprint", 0, m.start())):m.start()] if m else ""
        at = _re.search(r'\(at ([\d.\-]+) ([\d.\-]+)', blk)
        out["in_commit_board_text_R1_at"] = at.groups() if at else None
        out["in_commit_get_footprints"] = [(f.position.x, f.position.y) for f in b.get_footprints() if f.reference_field.text.value == "R1"]
        # second update in same commit, from a fresh fetch
        f2 = b.get_items_by_id([u])[0]
        f2.position = Vector2.from_xy(118_000_000, f2.position.y)
        b.update_items([f2])
        out["after_second_update"] = [(i.position.x, i.position.y) for i in b.get_items_by_id([u])]
        b.push_commit(c, "probe")
        out["after_push"] = [(i.position.x, i.position.y) for i in b.get_items_by_id([u])]
        watch.check()
    out["evidence"] = str(write_evidence("probe-commit-visibility", out))
    print(json.dumps(out, indent=1))
