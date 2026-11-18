"""Live proof for the MCP fallbacks of the eight API commands KiCad declares but does not implement.

GetLibraryTable, AddLibraryTableEntry, UpdateLibraryTableEntry, DeleteLibraryTableEntry, SearchLibraries,
FocusOnItem, RefreshEditor and UpdateBoardStackup are in the proto but have no handler (the inventory's
``declared_only``). ``kicad_mcp.tools.fallback_tools`` gives each one by other means; this driver runs the
real editors on private desktops and, for every function, checks a readback from KiCad itself against a
control (state before, or an unrelated call):

- library tables: the table file is edited; KiCad's own GetLibraryStatuses / GetLibraryItems, after the
  editor reads the tables, show the row, its changed URI/description, and then its absence; the other
  rows are unchanged;
- SearchLibraries: results equal the filter applied to KiCad's own GetLibraryItems list;
- FocusOnItem: the status-bar zoom and the canvas pixels change, GetSelection names the item; a missing
  reference and an unrelated call leave the view alone;
- RefreshEditor: BeginPaint calls on the canvas (Frida, in the session's process) follow the call and not
  the unrelated calls;
- UpdateBoardStackup: GetBoardStackup after the update shows the new thicknesses/finish/edge settings, the
  rest of the stackup and of the board file are unchanged, and sending the original back restores it.

    python -m kicad_mcp.coverage.live_fallbacks --inventory inv.json --out ev.json [--only pcb,sch]
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import json
from pathlib import Path
import re
import shutil
import stat
import time

from kicad_mcp.coverage.fallback_routes import ROUTES
from kicad_mcp.coverage.live_statusbar import status as bar_status
from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import GuiSession
from kicad_mcp.tools import fallback_tools as fb

PCB_FIXTURE = "stickhub-fixture"
GLOBAL_FP = "extra_global"
PAINT_SCRIPT = """
let counts = {};
const hook = (mod, name) => {
    let f = null;
    try { f = Process.getModuleByName(mod).findExportByName(name); } catch (e) { return; }
    if (f) Interceptor.attach(f, { onEnter(args) { const h = name === 'BeginPaint' ? args[0].toString() : name; counts[h] = (counts[h] || 0) + 1; } });
};
hook('user32.dll', 'BeginPaint');
hook('gdi32.dll', 'SwapBuffers');
hook('opengl32.dll', 'wglSwapBuffers');
rpc.exports = { get() { return counts; }, reset() { counts = {}; } };
"""
COMMANDS = tuple(ROUTES)


def row(command: str, name: str, ok: bool, detail: str, **evidence) -> dict:
    return {"item": f"api:{command}", "case": name, "outcome": "ok" if ok else "fail", "detail": detail, **evidence}


class PaintCounter:
    """BeginPaint calls per window in the session's editor process (a repaint of a canvas shows here)."""

    def __init__(self, pid: int):
        import frida

        self._session = frida.attach(pid)
        self._script = self._session.create_script(PAINT_SCRIPT)
        self._script.load()

    def reset(self) -> None:
        self._script.exports_sync.reset()

    def counts(self, hwnds: set[int]) -> dict[str, int]:
        got = self._script.exports_sync.get()
        wanted = {hex(h) for h in hwnds}
        swaps = got.get("SwapBuffers", 0) + got.get("wglSwapBuffers", 0)
        return {"canvas": sum(n for h, n in got.items() if h in wanted) + swaps, "all": sum(got.values()), "swaps": swaps}

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._script.unload()
        with contextlib.suppress(Exception):
            self._session.detach()


def _descendants(hwnd: int) -> set[int]:
    found = {hwnd}
    for child, _cls, _area, _visible in hotkeys._children(hwnd):
        found.add(child)
    return found


def _statuses(session) -> dict[tuple[str, str], dict]:
    reply = api.call(session, "GetLibraryStatuses", {"scope": "LTS_BOTH"})["response"].get("libraries", [])
    return {(e["entry"]["type"], e["entry"]["nickname"]): e for e in reply}


def _items(session, kind: str, nickname: str) -> list[str]:
    reply = api.call(session, "GetLibraryItems", {"type": kind, "nickname": [nickname]})["response"]
    return sorted(i["entry_name"] for i in reply.get("items", []))


def _wait_loaded(session, key: tuple[str, str], timeout_s: float = 30.0) -> dict:
    """The library's status entry once its load has finished (or {} if the table has no such row)."""
    started = time.monotonic()
    entry: dict = {}
    while time.monotonic() - started < timeout_s:
        entry = _statuses(session).get(key, {})
        if entry.get("status") in ("LLS_LOADED", "LLS_ERROR", "LLS_INVALID"):
            break
        if not entry and time.monotonic() - started > 3:
            break
        time.sleep(0.5)
    return entry


def _make_libs(workdir: Path) -> tuple[list[str], list[str]]:
    """Two small footprint libraries made from the project's own, and an empty design-block folder."""
    names = sorted(p.stem for p in (workdir / "footprints.pretty").glob("*.kicad_mod"))
    first, second = names[:3], names[3:7]
    for lib, picked in (("extra.pretty", first), ("extra2.pretty", second)):
        (workdir / lib).mkdir(exist_ok=True)
        for name in picked:
            shutil.copy(workdir / "footprints.pretty" / f"{name}.kicad_mod", workdir / lib)
    (workdir / "extra.kicad_blocks").mkdir(exist_ok=True)
    return first, second


def table_phase(session, rows: list[dict]) -> None:
    work = session.candidate.work_dir
    first, second = _make_libs(work)
    fp = "LT_FOOTPRINT"

    # GetLibraryTable: our rows equal KiCad's own list of the same table
    got = fb.get_library_table(session, fp, "LTS_PROJECT")
    api_rows = {k[1] for k, v in _statuses(session).items() if k[0] == fp and v["entry"]["scope"] == "LTS_PROJECT"}
    ours = {r["nickname"] for r in got["rows"]}
    glob0 = fb.get_library_table(session, fp, "LTS_GLOBAL")
    both = fb.get_library_table(session, fp, "LTS_BOTH", substituted=True)
    expanded = both["rows"][0]["uri"] if both["rows"] else ""
    rows.append(row("GetLibraryTable", "rows equal KiCad's own list", ours == api_rows == {"footprints"}
                    and got["tables"][0]["valid"] and glob0["rows"] == [] and expanded.startswith(str(work)),
                    f"project fp table {sorted(ours)}; KiCad GetLibraryStatuses {sorted(api_rows)}; global rows "
                    f"{len(glob0['rows'])}; substituted uri {expanded}",
                    control="global table (the session profile's) is empty and is a different file than the project's"))
    base_items = _items(session, fp, "footprints")
    new_keys = [(fp, "extra_fp"), (fp, GLOBAL_FP), ("LT_DESIGN_BLOCK", "extra_blocks")]
    listed_before = _statuses(session)  # KiCad's own listing before any add
    absent_before = [k[1] for k in new_keys if k not in listed_before]

    # AddLibraryTableEntry: project + global + design block, then the editor reads them
    entry = {"nickname": "extra_fp", "uri": "${KIPRJMOD}/extra.pretty", "type": fp, "description": "added by MCP",
             "options": {"a": "b|c", "flag": ""}}
    added = fb.add_library_table_entry(session, entry, "LTS_PROJECT")
    added_global = fb.add_library_table_entry(session, {"nickname": GLOBAL_FP, "uri": "${KIPRJMOD}/extra2.pretty",
                                                        "type": fp}, "LTS_GLOBAL")
    added_block = fb.add_library_table_entry(session, {"nickname": "extra_blocks", "uri": "${KIPRJMOD}/extra.kicad_blocks",
                                                       "type": "LT_DESIGN_BLOCK", "hidden": True}, "LTS_PROJECT")
    duplicate = fb.add_library_table_entry(session, entry, "LTS_PROJECT")
    bad_scope = fb.add_library_table_entry(session, entry, "LTS_BOTH")
    table_text = (work / "fp-lib-table").read_text(encoding="utf-8")
    back = fb.get_library_table(session, fp, "LTS_PROJECT")["rows"]
    mine = next((r for r in back if r["nickname"] == "extra_fp"), {})
    fb.reload_tables(session)
    status = _wait_loaded(session, (fp, "extra_fp"))
    status_global = _wait_loaded(session, (fp, GLOBAL_FP))
    status_block = _statuses(session).get(("LT_DESIGN_BLOCK", "extra_blocks"), {})
    items_added = _items(session, fp, "extra_fp")
    listed_after = _statuses(session)
    present_after = [k[1] for k in new_keys if k in listed_after]
    ok = (len(absent_before) == 3 and len(present_after) == 3
          and added["code"] == added_global["code"] == added_block["code"] == "LCS_OK"
          and duplicate["code"] == "LCS_ALREADY_EXISTS" and bad_scope["code"] == "LCS_ERROR"
          and mine.get("options") == {"a": "b|c", "flag": ""} and mine.get("description") == "added by MCP"
          and status.get("status") == "LLS_LOADED" and status["entry"].get("description") == "added by MCP"
          and status_global.get("status") == "LLS_LOADED" and status_global["entry"]["scope"] == "LTS_GLOBAL"
          and items_added == sorted(first) and _items(session, fp, "footprints") == base_items
          and status_block.get("entry", {}).get("hidden") is True)
    rows.append(row("AddLibraryTableEntry", "rows added to project, global and design-block tables", ok,
                    f"before the adds KiCad's own listing (GetLibraryStatuses) had none of {absent_before}; "
                    f"codes {added['code']}/{added_global['code']}/{added_block['code']}, duplicate "
                    f"{duplicate['code']}, LTS_BOTH {bad_scope['code']}; after the editor read the tables KiCad lists "
                    f"extra_fp {status.get('status')} with items {items_added} (files: {sorted(first)}), "
                    f"{GLOBAL_FP} {status_global.get('status')} scope {status_global.get('entry', {}).get('scope')}, "
                    f"design block row hidden={status_block.get('entry', {}).get('hidden')}",
                    control=f"each new nickname was absent before ({absent_before}) and listed after ({present_after}); "
                            f"'footprints' items unchanged ({len(base_items)}); table file rows: "
                            f"{table_text.count('(lib ')}"))

    # UpdateLibraryTableEntry: a new URI and description; others stay
    updated = fb.update_library_table_entry(session, {"nickname": "extra_fp", "type": fp,
                                                       "uri": "${KIPRJMOD}/extra2.pretty",
                                                       "description": "changed by MCP"}, "LTS_PROJECT")
    missing = fb.update_library_table_entry(session, {"nickname": "nope", "type": fp, "uri": "x"}, "LTS_PROJECT")
    kept = next(r for r in fb.get_library_table(session, fp, "LTS_PROJECT")["rows"] if r["nickname"] == "extra_fp")
    fb.reload_tables(session)
    status = _wait_loaded(session, (fp, "extra_fp"))
    items_updated = _items(session, fp, "extra_fp")
    other = _items(session, fp, GLOBAL_FP)
    ok = (updated["code"] == "LCS_OK" and missing["code"] == "LCS_NOT_FOUND"
          and kept.get("options") == {"a": "b|c", "flag": ""}  # the fields the update left out are kept
          and status["entry"].get("description") == "changed by MCP" and items_updated == sorted(second)
          and items_updated != items_added and other == sorted(second)
          and _items(session, fp, "footprints") == base_items)
    rows.append(row("UpdateLibraryTableEntry", "uri and description changed", ok,
                    f"update {updated['code']}, unknown nickname {missing['code']}; KiCad now lists extra_fp items "
                    f"{items_updated} (was {items_added}) and description {status['entry'].get('description')!r}",
                    control="options the update left out are kept; 'footprints' items unchanged"))

    # DeleteLibraryTableEntry
    gone = [fb.delete_library_table_entry(session, fp, "extra_fp", "LTS_PROJECT"),
            fb.delete_library_table_entry(session, fp, GLOBAL_FP, "LTS_GLOBAL"),
            fb.delete_library_table_entry(session, "LT_DESIGN_BLOCK", "extra_blocks", "LTS_PROJECT")]
    absent = fb.delete_library_table_entry(session, fp, "extra_fp", "LTS_PROJECT")
    table = work / "fp-lib-table"
    table.chmod(table.stat().st_mode & ~stat.S_IWRITE)
    try:
        locked = fb.delete_library_table_entry(session, fp, "footprints", "LTS_PROJECT")
    finally:
        table.chmod(table.stat().st_mode | stat.S_IWRITE)
    fb.reload_tables(session)
    left = {k for k in _statuses(session) if k[1] in ("extra_fp", GLOBAL_FP, "extra_blocks")}
    ok = (all(g["code"] == "LCS_OK" for g in gone) and absent["code"] == "LCS_NOT_FOUND"
          and locked["code"] == "LCS_READ_ONLY" and not left and _items(session, fp, "footprints") == base_items
          and (_statuses(session).get((fp, "footprints"), {}).get("status") == "LLS_LOADED"))
    rows.append(row("DeleteLibraryTableEntry", "rows removed from all three tables", ok,
                    f"codes {[g['code'] for g in gone]}, again {absent['code']}, read-only table {locked['code']}; "
                    f"KiCad lists none of them any more ({sorted(left)})",
                    control="'footprints' is still LOADED with its items"))


def _chooser_rows(uia, window: dict) -> list[str]:
    """The rows KiCad's own library tree shows (a library row, then its items), through UI Automation."""
    rows = uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "TreeItem"}})["controls"]
    return [r["name"][:-2] if r["name"].endswith(" *") else r["name"] for r in rows]


def search_phase(session, rows: list[dict], uia, editor: str, kind: str, queries: list[str]) -> None:
    """SearchLibraries against KiCad itself: each query is typed into the library tree's own filter box of the
    Footprint or Symbol Editor (the chooser filter, EDA_COMBINED_MATCHER) and the filtered tree rows are read
    back; the tool's hits must be exactly those rows. A query nothing matches shows no rows in either."""
    from kicad_mcp.coverage.live_hotkeys import HOSTED

    host_title, label, _prefix = HOSTED[editor]  # the host editor's own menu item opens the library editor
    host, _ = hotkeys.canvas_of(session, host_title)
    opener = next(i for i in hotkeys.menu_items(host) if i["label"] == label)
    hotkeys.user32.PostMessageW(host, hotkeys.WM_COMMAND, opener["id"], 0)
    time.sleep(6)
    title = "Footprint Editor" if editor == "fpedit" else "Symbol Editor"
    window = {"name": title, "match": "contains"}
    edit = {"by": "path", "name": "*", "name_match": "any", "control_type": "Edit"}
    libraries = {r["nickname"] for r in fb.get_library_table(session, kind, "LTS_BOTH")["rows"]}
    checks, details = [], []
    for query in ["", *queries]:
        uia.call("set-value", {"window": window, "selector": edit, "value": query})
        time.sleep(1.5)
        shown = _chooser_rows(uia, window)
        kicad = sorted(n for n in shown if n not in libraries)
        ours = sorted(i["entry_name"] for i in fb.search_libraries(session, kind, query)["items"])
        checks.append(kicad == ours and (query != "" or len(kicad) > 0))
        details.append(f"{query!r}: KiCad's filtered tree {len(kicad)} items, tool {len(ours)}"
                       + ("" if kicad == ours else f" MISMATCH kicad-only {sorted(set(kicad) - set(ours))[:4]} "
                                                   f"tool-only {sorted(set(ours) - set(kicad))[:4]}"))
    uia.call("set-value", {"window": window, "selector": edit, "value": ""})
    rows.append(row("SearchLibraries", f"{kind} hits equal the rows of KiCad's own library-tree filter", all(checks),
                    "; ".join(details),
                    control="the empty filter shows every item in both; a query nothing matches (the last) shows none "
                            "in both"))


def _zoom(uia) -> float | None:
    text = bar_status(uia).get("zoom", "")
    match = re.search(r"([\d.]+)", text)
    return float(match.group(1)) if match else None


def _selection(session) -> list[str]:
    got = api.call(session, "GetSelection", {})["response"].get("items", [])
    return sorted(i.get("id", {}).get("value", "") for i in got)


def _shot(frame: int):
    return hotkeys.settled_capture(frame, timeout_s=6.0)


def focus_pcb_phase(session, rows: list[dict], frame: int, uia) -> None:
    footprints = api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"]["items"]

    def ref_of(item: dict) -> str:
        return item["reference_field"]["text"]["text"]["text"]

    def pos(item: dict) -> tuple[int, int]:
        return int(item["position"].get("x_nm", 0)), int(item["position"].get("y_nm", 0))

    a = min(footprints, key=lambda i: pos(i)[0] + pos(i)[1])
    b = max(footprints, key=lambda i: pos(i)[0] + pos(i)[1])
    with_pads = next(i for i in footprints if any(s["@type"].endswith("types.Pad") for s in i["definition"]["items"]))
    pad = next(s for s in with_pads["definition"]["items"] if s["@type"].endswith("types.Pad"))
    api.call(session, "RunAction", {"action": "common.Control.zoomFitScreen"})
    time.sleep(1.0)
    z0, shot0 = _zoom(uia), _shot(frame)
    api.call(session, "Ping", {})  # control: an unrelated call
    time.sleep(0.5)
    z_ping, shot_ping = _zoom(uia), _shot(frame)
    fa = fb.focus_on_item(session, {"footprint": {"reference": ref_of(a)}})
    time.sleep(1.2)
    za, shot_a, sel_a = _zoom(uia), _shot(frame), _selection(session)
    fb_ = fb.focus_on_item(session, {"footprint": {"reference": ref_of(b)}})
    time.sleep(1.2)
    zb, shot_b, sel_b = _zoom(uia), _shot(frame), _selection(session)
    fp_ = fb.focus_on_item(session, {"pad": {"reference": ref_of(with_pads), "number": pad["number"]}})
    time.sleep(1.2)
    zp, shot_p, sel_p = _zoom(uia), _shot(frame), _selection(session)
    try:
        fb.focus_on_item(session, {"footprint": {"reference": "NO-SUCH-REF"}})
        missing = "no error"
    except Exception as exc:  # noqa: BLE001
        missing = type(exc).__name__
    time.sleep(0.5)
    zm, shot_m = _zoom(uia), _shot(frame)
    ok = (z0 is not None and z0 == z_ping and shot0 == shot_ping  # the control changes nothing
          and za is not None and za > z0 and shot_a != shot0 and sel_a == [a["id"]["value"]]
          and shot_b != shot_a and sel_b == [b["id"]["value"]]
          and zp is not None and zp > zb and sel_p == [pad["id"]["value"]] and shot_p != shot_b
          and missing != "no error" and zm == zp and shot_m == shot_p)
    rows.append(row("FocusOnItem", "footprint and pad: zoom, view and selection", ok,
                    f"fit-screen zoom {z0}; footprint {ref_of(a)}: zoom {za}, selection {sel_a}; footprint "
                    f"{ref_of(b)}: zoom {zb}, view differs from the first: {shot_b != shot_a}, selection {sel_b}; pad "
                    f"{ref_of(with_pads)}.{pad['number']}: zoom {zp}, selection {sel_p}; unknown reference: {missing}",
                    control=f"Ping changed neither zoom ({z_ping}) nor pixels; the failed focus left zoom {zm} and the "
                            f"view as it was",
                    messages=[fa["message"], fb_["message"], fp_["message"]]))
    # highlight=False moves the view but keeps the selection
    api.call(session, "ClearSelection", {})
    api.call(session, "AddToSelection", {"items": [{"value": a["id"]["value"]}]})
    before = _selection(session)
    fb.focus_on_item(session, {"footprint": {"reference": ref_of(b)}}, highlight=False)
    time.sleep(1.0)
    rows.append(row("FocusOnItem", "highlight=False keeps the selection", _selection(session) == before and
                    _shot(frame) != shot_m, f"selection stayed {before}", control="view moved to the other footprint"))


def paint_phase(session, rows: list[dict], frame: int, canvas: int) -> None:
    counter = PaintCounter(session.pid)
    watched = _descendants(canvas) | {canvas, frame}
    try:
        time.sleep(1.5)
        counter.reset()
        api.call(session, "Ping", {})
        api.call(session, "GetVersion", {})
        api.call(session, "GetSelection", {})
        time.sleep(2.0)
        idle = counter.counts(watched)
        counter.reset()
        outcome = fb.refresh_editor(session, "FT_PCB_EDITOR")
        time.sleep(2.0)
        refreshed = counter.counts(watched)
    finally:
        counter.close()
    other = fb.refresh_editor(session, "FT_SCHEMATIC_EDITOR")
    unsupported = fb.refresh_editor(session, "FT_SPICE_SIMULATOR")
    ok = (outcome["refreshed"] and idle["all"] == 0 and refreshed["all"] > 0
          and other["refreshed"] is False and unsupported["refreshed"] is False)
    rows.append(row("RefreshEditor", "the board canvas repaints after the call, not after unrelated calls", ok,
                    f"paint calls (BeginPaint) in the editor process: {idle['all']} during Ping/GetVersion/GetSelection, "
                    f"{refreshed['all']} after RefreshEditor; a frame that is not open is reported "
                    f"({other['reason']})",
                    control="Ping, GetVersion and GetSelection in the same window of time paint nothing"))


def _strip_stackup(text: str) -> str:
    begin, end = fb._block_span(text, "stackup")
    return text[:begin] + text[end:]


def stackup_phase(session, rows: list[dict]) -> None:
    original = api.call(session, "GetBoardStackup", {})["response"]["stackup"]
    layers_before = api.call(session, "GetBoardEnabledLayers", {})["response"]
    api.call(session, "SaveDocument", {})  # KiCad's own formatting, so the before/after file compare is like for like
    file_before = _strip_stackup(session.document.read_text(encoding="utf-8"))
    changed = copy.deepcopy(original)
    for layer in changed["layers"]:
        if layer["type"] == "BSLT_DIELECTRIC":
            layer["thickness"] = {"value_nm": "800000"}
            layer["dielectric"]["layer"][0]["thickness"] = {"value_nm": "800000"}
            layer["dielectric"]["layer"][0]["material_name"] = "Isola 370HR"
            layer["dielectric"]["layer"][0]["epsilon_r"] = 4.04
        elif layer["layer"] == "BL_F_Cu":
            layer["thickness"] = {"value_nm": "70000"}
    changed["finish"] = {"type_name": "ENIG"}
    changed["impedance"] = {"is_controlled": True}
    changed["edge"] = {"connector": {"type": "BECT_BEVELED"}, "plating": {"has_edge_plating": True}}
    got = fb.update_board_stackup(session, changed)["stackup"]
    again = api.call(session, "GetBoardStackup", {})["response"]["stackup"]
    layers_after = api.call(session, "GetBoardEnabledLayers", {})["response"]
    file_after = _strip_stackup(session.document.read_text(encoding="utf-8"))

    def by_layer(stackup: dict) -> dict:
        return {(layer["layer"], i): layer for i, layer in enumerate(stackup["layers"])}

    dielectric = next(layer for layer in got["layers"] if layer["type"] == "BSLT_DIELECTRIC")
    front = next(layer for layer in got["layers"] if layer["layer"] == "BL_F_Cu")
    untouched = [(k, v) for k, v in by_layer(original).items() if v["type"] in ("BSLT_COPPER", "BSLT_SOLDERMASK")
                 and v["layer"] != "BL_F_Cu"]
    same_rest = all(by_layer(got).get(k) == v for k, v in untouched)
    try:
        fb.update_board_stackup(session, {**changed, "layers": [x for x in changed["layers"]
                                                                if x["layer"] != "BL_B_Cu"]})
        refused = "no error"
    except Exception as exc:  # noqa: BLE001
        refused = type(exc).__name__
    conds = {
        "readback equals a fresh GetBoardStackup": got == again,
        "dielectric thickness": dielectric["thickness"]["value_nm"] == "800000",
        "dielectric material": dielectric["dielectric"]["layer"][0]["material_name"] == "Isola 370HR",
        "dielectric epsilon_r": dielectric["dielectric"]["layer"][0]["epsilon_r"] == 4.04,
        "F.Cu thickness": front["thickness"]["value_nm"] == "70000",
        "finish": got["finish"]["type_name"] == "ENIG",
        "impedance": got["impedance"].get("is_controlled") is True,
        "edge connector": got["edge"]["connector"]["type"] == "BECT_BEVELED",
        "edge plating": got["edge"]["plating"]["has_edge_plating"] is True,
        "other layers unchanged": same_rest,
        "board file outside the stackup unchanged": file_before == file_after,
        "enabled layers unchanged": layers_after == layers_before,
        "missing copper layer refused": refused != "no error",
    }
    ok = all(conds.values())
    failed = [k for k, v in conds.items() if not v]
    rows.append(row("UpdateBoardStackup", "thicknesses, material, finish and edge settings change", ok,
                    f"dielectric {original['layers'][4]['thickness'].get('value_nm')} -> "
                    f"{dielectric['thickness']['value_nm']} nm, F.Cu 35000 -> {front['thickness']['value_nm']}, finish "
                    f"{original['finish'].get('type_name')} -> {got['finish']['type_name']}, edge plating "
                    f"{got['edge']['plating'].get('has_edge_plating')}; a stackup missing a copper layer: {refused}; failed checks: {failed}",
                    control="the other copper and mask layers, the enabled layers and every line of the board file "
                            "outside the stackup block are unchanged; the return value equals a fresh GetBoardStackup"))
    restored = fb.update_board_stackup(session, original)["stackup"]
    rows.append(row("UpdateBoardStackup", "sending the original back restores it", restored == original,
                    "readback after restoring equals the stackup read before any change",
                    control="the changed stackup differed from the original"))


def pcb_session(project: Path, rows: list[dict], tables: bool = True) -> None:
    session = GuiSession(next(project.glob("*.kicad_pcb")))
    session.start()
    try:
        if tables:
            table_phase(session, rows)  # restarts the editor; do everything that needs a fixed process after it
        frame, canvas = hotkeys.canvas_of(session, "PCB Editor")
        hotkeys.settled_capture(frame, timeout_s=15.0)
        uia = session.attach_uia()
        focus_pcb_phase(session, rows, frame, uia)
        paint_phase(session, rows, frame, canvas)
        stackup_phase(session, rows)
        search_phase(session, rows, uia, "fpedit", "LT_FOOTPRINT",
                     ["0402", "*_c", "d_ 0402", "?005*", "footprints", "creative mask", "/^r_/", "smd",
                      "zzz-no-such-part-zzz"])
    finally:
        session.close(discard=True)


def sch_session(project: Path, rows: list[dict]) -> None:
    session = GuiSession(next(project.glob("*.kicad_sch")))
    session.start()
    try:
        work = session.candidate.work_dir
        kind = "LT_SYMBOL"
        before = _items(session, kind, "RobotProtos")
        listed_before = _statuses(session)  # KiCad's own listing before the add: Copy is not there
        shutil.copy(work / "RobotProtos.kicad_sym", work / "Copy.kicad_sym")
        added = fb.add_library_table_entry(session, {"nickname": "Copy", "uri": "${KIPRJMOD}/Copy.kicad_sym",
                                                     "type": kind, "description": "symbol copy"}, "LTS_PROJECT")
        fb.reload_tables(session)
        status = _wait_loaded(session, (kind, "Copy"))
        items = _items(session, kind, "Copy")
        search = sorted(i["entry_name"] for i in fb.search_libraries(session, kind, "1v8")["items"]
                        if i["library_nickname"] == "Copy")
        removed = fb.delete_library_table_entry(session, kind, "Copy", "LTS_PROJECT")
        fb.reload_tables(session)
        left = ("LT_SYMBOL", "Copy") in _statuses(session)
        rows.append(row("AddLibraryTableEntry", "a symbol library row is loaded by the schematic editor",
                        added["code"] == "LCS_OK" and (kind, "Copy") not in listed_before
                        and status.get("status") == "LLS_LOADED" and items == before
                        and search == sorted(i["entry_name"] for i in fb.search_libraries(session, kind, "1v8")["items"]
                                             if i["library_nickname"] == "RobotProtos") and "+1V8" in search,
                        f"before the add KiCad's own listing had no Copy (libraries: {sorted(k[1] for k in listed_before)}); after "
                        f"the reload it lists symbol library Copy {status.get('status')} with {len(items)} symbols (the source "
                        f"has {len(before)}); SearchLibraries finds {search} in it",
                        control=f"the new nickname was absent before and present after; RobotProtos still lists "
                                f"{len(before)} symbols"))
        rows.append(row("DeleteLibraryTableEntry", "a symbol library row is removed", removed["code"] == "LCS_OK" and not left,
                        f"after delete and reload KiCad no longer lists Copy ({left})", control="RobotProtos still listed"))
        # FocusOnItem in the schematic editor
        frame, _canvas = hotkeys.canvas_of(session, "Schematic Editor")
        hotkeys.settled_capture(frame, timeout_s=15.0)
        symbols = api.call(session, "GetItems", {"types": ["KOT_SCH_SYMBOL"]})["response"]["items"]
        refs = [(s["reference_field"]["text"]["text"], s) for s in symbols if s.get("reference_field")]
        shot0 = _shot(frame)
        first, last = refs[0], refs[-1]
        fb.focus_on_item(session, {"footprint": {"reference": first[0]}})
        time.sleep(1.5)
        shot_a, sel_a = _shot(frame), _selection(session)
        fb.focus_on_item(session, {"footprint": {"reference": last[0]}})
        time.sleep(1.5)
        shot_b, sel_b = _shot(frame), _selection(session)
        hierarchy = api.call(session, "GetSchematicHierarchy", {})["response"]["top_level_sheets"][0]["path"]
        fb.focus_on_item(session, {"sheet_path": hierarchy}, highlight=False)
        time.sleep(1.5)
        shot_s = _shot(frame)
        rows.append(row("FocusOnItem", "schematic symbols and a sheet", shot_a != shot0 and sel_a == [first[1]["id"]["value"]]
                        and shot_b != shot_a and sel_b == [last[1]["id"]["value"]] and shot_s != shot_b,
                        f"symbol {first[0]}: view changed, selection {sel_a}; symbol {last[0]}: view moved again, "
                        f"selection {sel_b}; the sheet path fits the whole sheet (view changed again)",
                        control="the second focus replaced the first selection"))
        # RefreshEditor in the schematic editor: the first use binds a key (restarts the editor); then count paints
        fb.refresh_editor(session, "FT_SCHEMATIC_EDITOR")
        frame, canvas = hotkeys.canvas_of(session, "Schematic Editor")
        hotkeys.settled_capture(frame, timeout_s=15.0)
        counter = PaintCounter(session.pid)
        try:
            time.sleep(1.5)
            counter.reset()
            api.call(session, "Ping", {})
            time.sleep(2.0)
            idle = counter.counts(_descendants(canvas) | {canvas, frame})
            counter.reset()
            done = fb.refresh_editor(session, "FT_SCHEMATIC_EDITOR")
            time.sleep(2.0)
            after = counter.counts(_descendants(canvas) | {canvas, frame})
        finally:
            counter.close()
        rows.append(row("RefreshEditor", "the schematic canvas repaints after the call", done["refreshed"]
                        and idle["all"] == 0 and after["all"] > 0,
                        f"paint calls in the schematic editor process: {idle['all']} idle, {after['all']} after the call",
                        control="Ping paints nothing"))
        uia = session.attach_uia()
        search_phase(session, rows, uia, "symedit", kind, ["1v8", "gnd", "*v*", "?3v3", "units=2", "robotprotos",
                                                         "power vin", "zzz-no-such-part-zzz"])
    finally:
        session.close(discard=True)


def evidence(inventory: dict, rows: list[dict]) -> dict:
    ok = {c: [r for r in rows if r["item"] == f"api:{c}"] for c in COMMANDS}
    passed = sorted(f"api:{c}" for c, rs in ok.items() if rs and all(r["outcome"] == "ok" for r in rs))
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "fallbacks-live", "passed": passed, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True, help="folder holding stickhub-fixture")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="pcb,sch")
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    rows: list[dict] = []
    which = args.only.split(",")
    try:
        if "pcb" in which:
            pcb_session(args.fixtures / PCB_FIXTURE, rows)
        if "pcb-view" in which:
            pcb_session(args.fixtures / PCB_FIXTURE, rows, tables=False)
        if "sch" in which:
            sch_session(args.fixtures / PCB_FIXTURE, rows)
    finally:
        args.out.write_text(json.dumps(evidence(inventory, rows), indent=1) + "\n", encoding="utf-8")
    for r in rows:
        print(r["outcome"], r["item"], r["case"], r["detail"][:220])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
