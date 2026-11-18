"""Live test of the packaged plugin against real KiCad on private desktops (Python with kicad-python).

1. install.py installs the plugin into a task-owned KiCad profile (ownership record written);
2. the INSTALLED panel.Controller drives an adaptive drag (R1 +6 mm, cascade) and an adaptive edge
   move (right outline edge 12 mm inward) through the IPC API: preview leaves the board unchanged,
   apply changes exactly the planned items, and ONE KiCad undo (common.Interactive.undo) restores
   the pre-apply state of every touched item;
3. KiCad discovers the installed plugin (it builds the plugin's venv and pip-installs requirements);
4. the panel's Tk drawing runs on another private desktop and draws the real preview primitives;
5. uninstall removes every installed file and the bytecode Python wrote for them;
6. no window of the test's process tree reaches the owner's desktop.
Every subprocess (fixture build, installer, engine worker, KiCad, Tk) runs on a private desktop.
Writes an evidence JSON; exit 1 on any failed assertion.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _harness import (ADAPTIVE, HiddenDesktop, HiddenKiCad, Watch, build_fixture_board,  # noqa: E402
                      hidden_spawner, run_hidden, write_evidence, source_digest)
from hidden_kicad import seed_profile  # noqa: E402

PLUGIN = ADAPTIVE / "plugin"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_installed(target: Path):
    spec = importlib.util.spec_from_file_location("installed_panel", target / "panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def state(board):
    out = {}
    for fp in board.get_footprints():
        out[str(fp.id.value)] = ("pos", fp.position.x, fp.position.y)
    for coll in (board.get_tracks(), board.get_shapes()):
        for it in coll:
            if hasattr(it, "start") and hasattr(it, "end"):
                out[str(it.id.value)] = ("seg", it.start.x, it.start.y, it.end.x, it.end.y)
    return out


def touched(cs):
    return ({m["uuid"] for m in cs["part_moves"]} | {m["uuid"] for m in cs["via_moves"]}
            | {t["uuid"] for t in cs["track_changes"]} | {e["uuid"] for e in cs["edge_changes"]})


@cleanup_after_call
def main():
    work = Path(scoped_mkdtemp(prefix="adpl-"))
    res, fails, watch = {}, [], Watch()
    board_path = build_fixture_board(work)
    profile = work / "p"
    env = seed_profile(profile)
    installer = _load(PLUGIN / "install.py", "plugin_install")
    target = installer.default_target(documents=Path(env["KICAD_DOCUMENTS_HOME"]))
    inst = run_hidden([sys.executable, "-B", str(PLUGIN / "install.py"), "install", "--target", str(target)],
                      cwd=str(work), timeout_s=120)
    res["install"] = json.loads(inst["stdout"]) if inst["exit_code"] == 0 else inst["stderr"][-800:]
    if inst["exit_code"] != 0 or not (target / ".install-record.json").is_file():
        fails.append("install failed")
    panel = load_installed(target)
    res["panel_loaded_from"] = panel.__file__
    if not str(panel.HERE).startswith(str(target)):
        fails.append("panel not loaded from the installed copy")

    with HiddenKiCad(board_path, workdir=profile) as kc, HiddenDesktop("worker") as wdesk:
        spawn = hidden_spawner(wdesk)
        from kipy import KiCad
        k = KiCad(socket_path=kc.socket_url(kc.wait_socket()), timeout_ms=30000)
        brd = k.get_board()
        refs = {fp.reference_field.text.value: fp for fp in brd.get_footprints()}

        # ---- adaptive drag
        brd.clear_selection(); brd.add_to_selection([refs["R1"]])
        ctl = panel.Controller(brd, "drag", spawn=spawn); ctl.start()
        before = state(brd)
        ctl.set_displacement(6.0, 0.0)
        pv = ctl.preview()
        res["drag_summary"] = ctl.summary()
        if not pv.get("prims") or not pv["prims"]["items"]:
            fails.append("drag: no preview primitives")
        if state(brd) != before:
            fails.append("drag: preview changed the board")
        cs = pv["changeset"]; tset = touched(cs)
        if str(refs["R1"].id.value) not in {m["uuid"] for m in cs["part_moves"]}:
            fails.append("drag: R1 not in plan")
        prims = pv["prims"]
        out = ctl.apply()
        res["drag_apply"] = out
        after = state(k.get_board())
        changed = {u for u in before if before[u] != after.get(u)}
        res["drag_changed_items"] = len(changed)
        if not changed or not changed <= tset:
            fails.append(f"drag: applied changes {len(changed)} not within plan {len(tset)}")
        undo = k.run_action("common.Interactive.undo"); time.sleep(1.0)
        res["drag_undo"] = str(undo)
        restored = state(k.get_board())
        bad = [u for u in tset if restored.get(u) != before.get(u)]
        res["drag_undo_restored"] = not bad
        if bad:
            fails.append(f"drag: one undo did not restore {len(bad)} items")
        k.run_action("common.Interactive.redo"); time.sleep(1.0)
        brd = k.get_board()

        # ---- adaptive edge move (right edge of the 100..160 x 100..130 outline)
        right = [s for s in brd.get_shapes() if hasattr(s, "start") and panel._is_edge_cuts(s)
                 and s.start.x == s.end.x == 160_000_000]
        if len(right) != 1:
            fails.append(f"edge: expected one right edge, found {len(right)}")
        else:
            brd.clear_selection(); brd.add_to_selection(right)
            ectl = panel.Controller(brd, "edge", spawn=spawn); ectl.start()
            res["edge_side"] = ectl.side
            if ectl.side != "right":
                fails.append(f"edge: side {ectl.side} != right")
            eb = state(brd)
            ectl.set_displacement(-12.0)
            ev = ectl.preview()
            res["edge_summary"] = ectl.summary()
            ecs = ev["changeset"]
            if not ecs["edge_changes"]:
                fails.append("edge: no edge changes planned")
            if state(brd) != eb:
                fails.append("edge: preview changed the board")
            res["edge_apply"] = ectl.apply()
            ea = state(k.get_board())
            seg = ea[str(right[0].id.value)]
            res["edge_after"] = seg
            if not (seg[1] == seg[3] == 148_000_000):
                fails.append(f"edge: right edge not at x=148 mm: {seg}")
            echanged = {u for u in eb if eb[u] != ea.get(u)}
            if not echanged <= touched(ecs):
                fails.append("edge: applied changes outside the plan")
            res["edge_undo"] = str(k.run_action("common.Interactive.undo")); time.sleep(1.0)
            er = state(k.get_board())
            res["edge_undo_restored"] = not any(er.get(u) != eb.get(u) for u in touched(ecs))
            if not res["edge_undo_restored"]:
                fails.append("edge: one undo did not restore the outline move")
        # ---- KiCad discovered the installed plugin: it builds the plugin's venv (only for plugins it
        # loaded from plugin.json) and pip-installs requirements.txt into it (needs PyPI access).
        env_cfg, kipy_ok, end = None, False, time.time() + 300
        while time.time() < end and not kipy_ok:
            cfgs = [c for c in profile.rglob("pyvenv.cfg") if c.parent.name == installer.PLUGIN_ID]
            env_cfg = cfgs[0] if cfgs else None
            if env_cfg is not None:
                kipy_ok = any(env_cfg.parent.rglob("kipy/__init__.py"))
            watch.check()
            time.sleep(3)
        res["plugin_env"] = {"venv": str(env_cfg.parent) if env_cfg else None, "kicad_python_installed": kipy_ok}
        if env_cfg is None:
            fails.append("KiCad did not create a venv for the installed plugin (not discovered)")
        watch.check()

    # ---- Tk panel drawing on a private desktop
    pj = work / "prims.json"; pj.write_text(json.dumps(prims), encoding="utf-8")
    so = work / "selftest.json"
    pyw = Path(sys.executable).with_name("pythonw.exe")
    run = run_hidden([str(pyw), "-B", str(target / "panel.py"), "--selftest", str(pj), str(so)], cwd=str(work),
                     timeout_s=60)
    res["tk_selftest_run"] = run
    if so.is_file():
        st = json.loads(so.read_text(encoding="utf-8")); res["tk_selftest"] = st
        if st["drawn"] != len(prims["items"]) or st["postscript_bytes"] < 1000:
            fails.append("tk: drawing incomplete")
    else:
        fails.append("tk: selftest produced no output")

    # ---- uninstall
    un = run_hidden([sys.executable, "-B", str(PLUGIN / "install.py"), "uninstall", "--target", str(target)],
                    cwd=str(work), timeout_s=120)
    res["uninstall"] = json.loads(un["stdout"]) if un["exit_code"] == 0 else un["stderr"][-800:]
    if target.exists():
        fails.append(f"uninstall left {sorted(str(x.relative_to(target)) for x in target.rglob('*'))[:10]}")
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
    res["evidence"] = str(write_evidence("plugin-live", res))
    print(json.dumps(res, indent=1, default=str))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
