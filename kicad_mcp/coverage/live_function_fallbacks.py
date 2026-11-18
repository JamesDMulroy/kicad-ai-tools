"""Live proof for the MCP tools that do the FUNCTION of seven dead KiCad actions.

Seven TOOL_ACTIONs of the build are dead inside KiCad (never bound, never posted, never defined, or given no
usable parameter), so the gate keeps them ``upstream_gap``. Each names a function KiCad does have elsewhere;
``kicad_mcp.tools.function_fallbacks`` gives each one a tool. This driver runs the real editors on private
desktops and proves every tool with a readback from KiCad itself and a control:

- 3DViewer.Control.showBoundingBoxes: the Appearance panel toggle; the viewer's own Preferences page shows the
  setting off, on, unchanged by a repeat, off again; ``3d_viewer.json`` (written by the viewer on exit) holds it.
- common.Embed.extractFile / removeFile: files added through the API (a board) or written into the schematic
  file (a schematic) are listed, extracted byte for byte and removed; the file KiCad itself saves is parsed and
  decoded independently of the tool, and the other embedded file stays intact.
- eeschema.EditorControl.generateBOMExternal: a generator script runs on the intermediate netlist; its output is
  compared with the symbols KiCad reports through the API.
- eeschema.InteractiveSelection.SyncSelection: the API command with real items selects exactly them in the
  schematic editor (GetSelection), replaces the previous selection, and a clear empties it.
- pcbnew.EditorControl.rescueAutosave: the autosave file is found and restored; the editor shows the recovered
  edit (GetItems) after showing the original, and the file on disk is the autosave's bytes.
- pcbnew.InteractiveSelection.SelectOnSheet: the footprints of a hierarchical sheet are selected in the board
  editor; the expected set is computed from the board file independently.

The two HPGL CLI commands are removed from KiCad: their refusal text is recorded, nothing is faked.

    python -m kicad_mcp.coverage.live_function_fallbacks --inventory inv.json --fixtures <dir> --out ev.json \
        [--only embed-pcb,autosave,sch,hier,3d,hpgl]
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import base64
import contextlib
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import time

from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import GuiSession
from kicad_mcp.tools import function_fallbacks as ff

BBOX = "3DViewer.Control.showBoundingBoxes"
EXTRACT = "common.Embed.extractFile"
REMOVE = "common.Embed.removeFile"
BOM = "eeschema.EditorControl.generateBOMExternal"
SYNC = "eeschema.InteractiveSelection.SyncSelection"
RESCUE = "pcbnew.EditorControl.rescueAutosave"
ONSHEET = "pcbnew.InteractiveSelection.SelectOnSheet"
ACTIONS = (BBOX, EXTRACT, REMOVE, BOM, SYNC, RESCUE, ONSHEET)
HPGL = ("pcb export hpgl", "sch export hpgl")
TOOLS = {
    BBOX: "kicad_3d_model_bounding_boxes (function_fallbacks.set_3d_model_bounding_boxes)",
    EXTRACT: "kicad_extract_embedded_file (function_fallbacks.extract_embedded_file)",
    REMOVE: "kicad_remove_embedded_file (function_fallbacks.remove_embedded_file)",
    BOM: "kicad_run_bom_generator (function_fallbacks.run_bom_generator)",
    SYNC: "kicad_sync_selection (function_fallbacks.sync_selection; API SyncSelection with items)",
    RESCUE: "kicad_recover_autosave (function_fallbacks.recover_autosave)",
    ONSHEET: "kicad_select_items_on_sheet (function_fallbacks.select_items_on_sheet)",
}
PCB_FIXTURE, HIER_FIXTURE, VIEW_FIXTURE = "stickhub-fixture", "hier-fixture", "stickhub-3d"
PHASES = ("bom-dialog", "embed-pcb", "autosave", "autosave-ui", "autosave-real", "sch", "hier", "3d", "hpgl")

GENERATOR = '''"""CSV BOM generator for the BOM generator dialog's command line: python gen.py "%I" "%O.csv"
(grouped by value like KiCad's bom_csv_grouped_by_value, but dependency free)."""
import csv
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
rows = []
for comp in root.iter("comp"):
    fields = {f.get("name"): (f.text or "") for f in comp.iter("field")}
    rows.append((comp.get("ref"), (comp.findtext("value") or ""), (comp.findtext("footprint") or "")))
with open(sys.argv[2], "w", newline="", encoding="utf-8") as out:
    writer = csv.writer(out)
    writer.writerow(["Reference", "Value", "Footprint"])
    writer.writerows(sorted(rows))
'''


def row(action: str, case: str, ok: bool, detail: str, **evidence) -> dict:
    return {"action": action, "item": f"action:{action}", "case": case, "outcome": "ok" if ok else "fail",
            "detail": detail, **evidence}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64_payload(raw: bytes) -> str:
    """An EmbeddedFile.data in API JSON: the JSON base64 of KiCad's base64 text of the zstd frame."""
    return base64.b64encode(ff.encode_payload(raw)).decode()


def refuses(fn, *args, **kwargs) -> str | None:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - the refusal is what the control looks for
        return f"{type(exc).__name__}: {str(exc)[:160]}"
    return None


def positions(session) -> dict[str, int]:
    items = api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"]["items"]
    return {ff._ref_of(i): int(i["position"]["x_nm"]) for i in items}


def refs_of(selected: list[dict]) -> list[str]:
    return sorted(s["reference"] for s in selected if s["reference"])


# --- embedded files, board ---------------------------------------------------------------------------

def embed_pcb_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    session = GuiSession(next((fixtures / PCB_FIXTURE).glob("*.kicad_pcb")))
    session.start()
    try:
        raw_note = b"embedded note for the panel\n" * 40
        raw_bin = hashlib.sha256(b"seed").digest() * 90
        none = ff.embedded_files(session)["files"]
        api.call(session, "AddEmbeddedFiles", {"files": {"files": [
            {"name": "note.txt", "type": "EFT_OTHER", "data": b64_payload(raw_note)},
            {"name": "data.bin", "type": "EFT_OTHER", "data": b64_payload(raw_bin)}]}})
        listed = {f["name"]: f for f in ff.embedded_files(session)["files"]}
        # the file KiCad itself writes, parsed and decoded without the tool
        api.call(session, "SaveDocument", {})
        saved = ff.parse_embedded_files(Path(session.document).read_text(encoding="utf-8"))
        by_saved = {f["name"]: f for f in saved}
        via_file = {n: ff.decode_payload(f["data"]) for n, f in by_saved.items() if f["data"]}
        dest = scratch / "extracted"
        got = ff.extract_embedded_file(session, "note.txt", str(dest))
        got_bin = ff.extract_embedded_file(session, "data.bin", str(dest))
        again = refuses(ff.extract_embedded_file, session, "note.txt", str(dest))
        missing = refuses(ff.extract_embedded_file, session, "nothing.txt", str(dest))
        ok = (none == [] and set(listed) == {"note.txt", "data.bin"}
              and listed["note.txt"]["size"] == len(raw_note) and listed["note.txt"]["sha256"] == sha(raw_note)
              and (dest / "note.txt").read_bytes() == raw_note and (dest / "data.bin").read_bytes() == raw_bin
              and got["sha256"] == sha(raw_note) and got_bin["sha256"] == sha(raw_bin)
              and via_file == {"note.txt": raw_note, "data.bin": raw_bin}
              and by_saved["note.txt"]["checksum"] == listed["note.txt"]["checksum"] and again and missing)
        rows.append(row(EXTRACT, "a board's embedded files are listed and extracted byte for byte", ok,
                        f"before adding: {len(none)} files; after: {sorted(listed)}; extracted note.txt {got['size']} B "
                        f"sha256 {got['sha256'][:12]}, data.bin {got_bin['size']} B, both equal to the bytes embedded; "
                        f"the board file KiCad saved decodes (base64, zstd) to the same bytes and its checksum "
                        f"{by_saved['note.txt']['checksum']} equals the one KiCad reported",
                        control=f"no files before the add; a second extract without overwrite refused ({again}); a "
                                f"missing name refused ({missing})"))
        done = ff.remove_embedded_file(session, "note.txt")
        api.call(session, "SaveDocument", {})
        after = ff.parse_embedded_files(Path(session.document).read_text(encoding="utf-8"))
        left = {f["name"]: ff.decode_payload(f["data"]) for f in after}
        gone = refuses(ff.remove_embedded_file, session, "note.txt")
        ok = (done["remaining"] == ["data.bin"] and left == {"data.bin": raw_bin} and gone
              and ff.embedded_files(session)["files"][0]["sha256"] == sha(raw_bin))
        rows.append(row(REMOVE, "a board's embedded file is removed and the other stays intact", ok,
                        f"after the remove KiCad reports {done['remaining']} (GetEmbeddedFiles); the board file KiCad then "
                        f"saved holds {sorted(left)} and data.bin still decodes to its original {len(raw_bin)} bytes",
                        control=f"note.txt was present before; removing it again is refused ({gone}); data.bin untouched"))
    finally:
        session.close(discard=True)


# --- autosave ----------------------------------------------------------------------------------------

def autosave_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    session = GuiSession(next((fixtures / PCB_FIXTURE).glob("*.kicad_pcb")))
    session.start()
    try:
        document = Path(session.document)
        autosave = document.with_name(ff.AUTOSAVE_PREFIX + document.name)
        original_bytes = document.read_bytes()
        before = positions(session)
        ref = sorted(before)[0]
        items = api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"]["items"]
        fp = next(i for i in items if ff._ref_of(i) == ref)
        fp["position"]["x_nm"] = str(int(fp["position"]["x_nm"]) + 2_000_000)
        api.call(session, "UpdateItems", {"items": [fp]})
        moved = positions(session)[ref]
        # No autosave appears by itself on a private desktop (its editor frame is never the active window, which
        # KiCad's autosave timer requires: observed none in 40 s), so the file KiCad would have left is written by
        # KiCad's own serializer. SaveCopyOfDocument does not touch the open document.
        staged = scratch / "autosave-source.kicad_pcb"
        api.call(session, "SaveCopyOfDocument", {"path": str(staged), "options": {"overwrite": True}})
        autosave_bytes = staged.read_bytes()
        # the crash: the editor comes back on the saved file. (With the autosave already beside the board, KiCad's
        # own open path shows its recovery dialog, which blocks the API, so the file is put in place afterwards.)
        api.call(session, "RevertDocument", {})
        time.sleep(2.5)
        reverted = positions(session)[ref]
        shutil.copyfile(staged, autosave)
        none_yet = ff.find_autosave(session)
        found = none_yet is not None
        got = ff.recover_autosave(session)
        time.sleep(1.0)
        recovered = positions(session)[ref]
        no_backup = not document.with_name(document.name + ".before-recovery").exists() and "backup" not in got
        again = ff.recover_autosave(session)
        ok = (before[ref] == reverted and moved == before[ref] + 2_000_000 and found and got["recovered"]
              and recovered == moved and document.read_bytes() == autosave_bytes and not autosave.exists()
              and no_backup and again["recovered"] is False)
        rows.append(row(RESCUE, "the document is recovered from its autosave file", ok,
                        f"{ref} x: saved file {before[ref]} nm; unsaved edit {moved} (+2 mm), autosave file written; after the "
                        f"crash (revert) the editor shows {reverted}; recover_autosave found {Path(none_yet['path']).name}; "
                        f"afterwards the editor shows {recovered}, the document file is the autosave's {len(autosave_bytes)} "
                        f"bytes and the autosave is gone; no backup is kept (KiCad's Restore keeps none; backup=True is opt-in)",
                        control=f"before the recovery the editor was back on {reverted}; a second recover_autosave finds "
                                f"nothing ({again['reason']}) and changes nothing"))
        # keep both: a newer autosave, the editor stays on the current document
        fp2 = next(i for i in api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"]["items"]
                   if ff._ref_of(i) == ref)
        fp2["position"]["x_nm"] = str(int(fp2["position"]["x_nm"]) + 1_000_000)
        api.call(session, "UpdateItems", {"items": [fp2]})
        time.sleep(1.1)  # a newer mtime than the document
        api.call(session, "SaveCopyOfDocument", {"path": str(autosave), "options": {"overwrite": True}})
        second_bytes = autosave.read_bytes()
        doc_before, editor_before = document.read_bytes(), positions(session)[ref]
        stamp = autosave.stat().st_mtime
        kept = ff.recover_autosave(session, keep="keep_both")
        want_name = f"{document.stem}.recovered.{time.strftime('%Y-%m-%d_%H%M%S', time.localtime(stamp))}{document.suffix}"
        ok = (kept["recovered"] and Path(kept["path"]).read_bytes() == second_bytes and document.read_bytes() == doc_before
              and positions(session)[ref] == editor_before and not autosave.exists()
              and Path(kept["path"]).name == want_name)
        rows.append(row(RESCUE, "keep both writes the recovered copy and leaves the document alone", ok,
                        f"recovered copy {Path(kept['path']).name} (KiCad's buildRecoveredFileName form) equals the autosave ({len(second_bytes)} bytes); the document "
                        f"file and the editor (x {editor_before}) are unchanged",
                        control="document bytes and editor position compared before and after"))
        # opt-in backup on Restore
        fp3 = next(i for i in api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"]["items"]
                   if ff._ref_of(i) == ref)
        fp3["position"]["x_nm"] = str(int(fp3["position"]["x_nm"]) + 500_000)
        api.call(session, "UpdateItems", {"items": [fp3]})
        time.sleep(1.1)
        api.call(session, "SaveCopyOfDocument", {"path": str(autosave), "options": {"overwrite": True}})
        before_doc = document.read_bytes()
        opted = ff.recover_autosave(session, backup=True)
        rows.append(row(RESCUE, "backup=True keeps the replaced file (opt-in; not KiCad behaviour)",
                        opted["recovered"] and Path(opted["backup"]).read_bytes() == before_doc,
                        f"{Path(opted['backup']).name} holds the document as it was before the restore",
                        control="the default call above kept no backup"))
        # stale: an autosave older than the document is not offered (the KiCad-side readback is merged in below)
        stale = document.with_name(ff.AUTOSAVE_PREFIX + document.name)
        stale.write_bytes(original_bytes)
        import os

        os.utime(stale, (document.stat().st_mtime - 60, document.stat().st_mtime - 60))
        doc_before = document.read_bytes()
        nothing = ff.recover_autosave(session)
        stale.unlink()
        rows.append(row(RESCUE, "an autosave older than the document is ignored", nothing["recovered"] is False
                        and document.read_bytes() == doc_before, f"tool: {nothing['reason']}; document unchanged",
                        control="the file existed and differed from the document"))
    finally:
        session.close(discard=True)


# --- schematic: selection sync, BOM generator, embedded files ----------------------------------------

def _sch_block(files: dict[str, bytes]) -> str:
    out = ["\t(embedded_files"]
    for name, raw in files.items():
        data = ff.encode_payload(raw).decode()
        lines = [data[i:i + 76] for i in range(0, len(data), 76)]
        out += ["\t\t(file", f'\t\t\t(name "{name}")', "\t\t\t(type other)", "\t\t\t(data",
                *[("|" if n == 0 else "") + line + ("|" if n == len(lines) - 1 else "") for n, line in enumerate(lines)],
                "\t\t\t)", f'\t\t\t(checksum "{sha(raw)}")', "\t\t)"]  # a 64 digit sha256, the older file format
    out.append("\t)")
    return "\n".join(out) + "\n"


def sch_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    session = GuiSession(next((fixtures / PCB_FIXTURE).glob("*.kicad_sch")))
    raw_a, raw_b = b"schematic embedded text\n" * 30, hashlib.sha256(b"sch").digest() * 70
    schematic = Path(session.document)
    text = schematic.read_text(encoding="utf-8")
    end = text.rstrip().rfind(")")
    schematic.write_text(text[:end] + _sch_block({"sch-note.txt": raw_a, "second.bin": raw_b}) + text[end:], encoding="utf-8")
    session.start()
    try:
        # --- SyncSelection
        symbols = api.call(session, "GetItems", {"types": ["KOT_SCH_SYMBOL"]})["response"]["items"]
        by_ref = {ff._ref_of(s): s for s in symbols if ff._ref_of(s) and not ff._ref_of(s).startswith("#")}
        names = sorted(by_ref)
        first, second = names[:2], names[5:8]
        api.call(session, "ClearSelection", {})
        empty = ff.selection(session)
        got = ff.sync_selection(session, first)
        replaced = ff.sync_selection(session, second)
        nets = ff.sync_selection(session, second[:1], mode="SSM_ITEMS_AND_NETS")
        api.call(session, "ClearSelection", {})
        cleared = ff.selection(session)
        ids = {s["id"] for s in replaced["selected"]}
        ok = (empty == [] and got["status"] == "CPS_OK" and refs_of(got["selected"]) == sorted(first)
              and refs_of(replaced["selected"]) == sorted(second) and ids == {by_ref[r]["id"]["value"] for r in second}
              and refs_of(nets["selected"]) and second[0] in refs_of(nets["selected"]) and cleared == [])
        rows.append(row(SYNC, "symbols named by reference become the schematic selection", ok,
                        f"nothing selected; sync {first} -> KiCad selects {refs_of(got['selected'])}; sync {second} replaces it with "
                        f"{refs_of(replaced['selected'])} (ids match the symbols' ids); sync with nets selects "
                        f"{len(nets['selected'])} items including {second[0]}; ClearSelection leaves {len(cleared)}",
                        control="the selection was empty before the first sync; the second sync replaced the first; a clear empties it"))
        # --- BOM generator
        script = scratch / "gen_bom.py"
        script.write_text(GENERATOR, encoding="utf-8")
        refused = refuses(ff.run_bom_generator, session, f'python "{script}" "%I" "%O.csv"')
        out_base = scratch / "bom" / "stickhub"
        # the dialog writes WriteNetListFile(NET_TYPE_BOM, GNL_OPT_BOM | GNL_ALL) with NETLIST_EXPORTER_XML; the CLI's
        # python-bom writes the same exporter with GNL_OPT_BOM (WriteNetlist ORs in GNL_ALL: netlist_exporter_xml.cpp:64)
        compare = scratch / "bom-compare"
        compare.mkdir()
        api.call(session, "SaveDocument", {})
        for args, outname in ((["python-bom"], "py.xml"), (["netlist", "--format", "kicadxml"], "net.xml")):
            subprocess.run([session.cli, "sch", "export", *args, "-o", str(compare / outname), str(session.document)],
                           capture_output=True, text=True, timeout=120, creationflags=0x08000000)
        py_xml, net_xml = (compare / "py.xml").read_bytes(), (compare / "net.xml").read_bytes()
        same_netlist = py_xml == net_xml
        done = ff.run_bom_generator(session, f'python "{script}" "%I" "%O.csv"', str(out_base), allow_execute=True)
        csv_rows = [line.split(",") for line in Path(done["outputs"][0]).read_text(encoding="utf-8").splitlines()[1:]] \
            if done["outputs"] else []
        want = {ff._ref_of(s) for s in symbols if ff._ref_of(s) and not ff._ref_of(s).startswith("#")
                and not s.get("attributes", {}).get("exclude_from_bill_of_materials")}
        got_refs = {r[0] for r in csv_rows}
        bad = ff.run_bom_generator(session, 'python "' + str(scratch / "missing.py") + '" "%I" "%O.csv"',
                                   str(scratch / "bom" / "bad"), allow_execute=True)
        ok = (refused is not None and done["ok"] and got_refs == want and len(want) > 20 and bool(py_xml)
              and done["returncode"] == 0 and bad["returncode"] != 0 and not bad["ok"])
        rows.append(row(BOM, "an external generator turns the schematic's netlist into a BOM", ok,
                        f"command {done['command'][-60:]}: exit {done['returncode']}, wrote {[Path(p).name for p in done['outputs']]}; "
                        f"the {len(csv_rows)} CSV rows carry exactly the {len(want)} references the API reports for the "
                        f"schematic's symbols (power symbols and exclude-from-BOM parts left out); difference: {sorted(want ^ got_refs)}. "
                        f"Intermediate netlist: the tool uses python-bom, which by source is the dialog's own exporter and "
                        f"options (netlist_exporter_xml.cpp:64 ORs GNL_ALL into GNL_OPT_BOM; dialog_bom.cpp:294 passes "
                        f"GNL_OPT_BOM|GNL_ALL); against `sch export netlist --format kicadxml` the files are "
                        f"{'byte-identical' if same_netlist else 'different (' + str(len(py_xml)) + ' vs ' + str(len(net_xml)) + ' bytes)'}",
                        control=f"without allow_execute the call is refused ({refused}); a generator that does not exist exits "
                                f"{bad['returncode']} and is reported not ok"))
        # --- embedded files in the schematic
        listed = {f["name"]: f for f in ff.embedded_files(session)["files"]}
        dest = scratch / "sch-extracted"
        got_a = ff.extract_embedded_file(session, "sch-note.txt", str(dest))
        again_sch = refuses(ff.extract_embedded_file, session, "sch-note.txt", str(dest))
        missing_sch = refuses(ff.extract_embedded_file, session, "nothing.txt", str(dest))
        gone = ff.remove_embedded_file(session, "second.bin")  # the second row: the cursor is moved down to it
        api.call(session, "SaveDocument", {})
        left = ff.parse_embedded_files(schematic.read_text(encoding="utf-8"))
        left_raw = {f["name"]: ff.decode_payload(f["data"]) for f in left}
        missing = refuses(ff.remove_embedded_file, session, "second.bin")
        rows.append(row(EXTRACT, "a schematic's embedded file is extracted byte for byte",
                        set(listed) == {"sch-note.txt", "second.bin"} and (dest / "sch-note.txt").read_bytes() == raw_a
                        and listed["second.bin"]["sha256"] == sha(raw_b) and len(listed["sch-note.txt"]["checksum"]) == 32
                        and again_sch is not None and missing_sch is not None,
                        f"the schematic editor kept both files it loaded ({sorted(listed)}) and re-saved them with its own "
                        f"checksum {listed['sch-note.txt']['checksum']}; extract wrote {got_a['size']} bytes equal to what was embedded",
                        control=f"the checksum KiCad saved is its 32 digit MurmurHash, not the 64 digit sha256 the file was written with; a second "
                                f"extract without overwrite is refused ({again_sch}); a missing name is refused ({missing_sch})"))
        rows.append(row(REMOVE, "a schematic's embedded file is removed and the other stays intact",
                        gone["remaining"] == ["sch-note.txt"] and left_raw == {"sch-note.txt": raw_a} and missing is not None
                        and gone["mechanism"].startswith("Schematic Setup"),
                        f"mechanism: {gone['mechanism']}; the file KiCad then saves holds {sorted(left_raw)}; sch-note.txt decodes to its original "
                        f"{len(raw_a)} bytes",
                        control=f"removing the missing name again is refused ({missing})"))
    finally:
        session.close(discard=True)


# --- hierarchical sheets -------------------------------------------------------------------------------

def file_sheets(board: Path) -> dict[str, list[str]]:
    """{sheet path: references} from the board file alone (each footprint's own ``(path ...)``)."""
    text = board.read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for block in text.split("\n\t(footprint ")[1:]:
        path = re.search(r'\n\t\t\(path "([^"]*)"\)', block)
        ref = re.search(r'\(property "Reference" "([^"]*)"', block)
        if path and ref:
            parts = path.group(1).rstrip("/").split("/")[:-1]
            out.setdefault("/".join(parts) or "/", []).append(ref.group(1))
    return out


def hier_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    session = GuiSession(next((fixtures / HIER_FIXTURE).glob("*.kicad_pcb")))
    session.start()
    try:
        expected = file_sheets(Path(session.document))
        sheets = ff.board_sheets(session)
        api.call(session, "ClearSelection", {})
        empty = ff.selection(session)
        non_root = sorted(p for p in expected if p != "/")
        first, second = non_root[0], non_root[1]
        vertical = ff.select_items_on_sheet(session, first, include_connections=False)
        sel_v = ff.selection(session)
        ff.select_items_on_sheet(session, second, include_connections=False)
        sel_h = ff.selection(session)
        ff.select_items_on_sheet(session, "/", include_connections=False)
        sel_r = ff.selection(session)
        by_name = ff.select_items_on_sheet(session, vertical["sheet"]["name"], include_connections=False)
        before_bad = refs_of(ff.selection(session))
        unknown = refuses(ff.select_items_on_sheet, session, "/no/such/sheet")
        ambiguous = refuses(ff.select_items_on_sheet, session, vertical["sheet"]["file"])
        unchanged = refs_of(ff.selection(session)) == before_bad
        with_nets = ff.select_items_on_sheet(session, first)  # the default, as KiCad's own action
        kinds = {k: sum(1 for s in with_nets["selected"] if s["kind"] == k) for k in {s["kind"] for s in with_nets["selected"]}}
        ok = (empty == [] and {s["kind"] for s in sel_v} == {"FootprintInstance"}
              and refs_of(sel_v) == sorted(expected[first]) and len(sel_v) == 29
              and refs_of(sel_h) == sorted(expected[second]) and not set(refs_of(sel_v)) & set(refs_of(sel_h))
              and refs_of(sel_r) == sorted(expected["/"]) and refs_of(by_name["selected"]) == sorted(expected[first])
              and unknown and ambiguous and unchanged
              and kinds.get("FootprintInstance") == 29 and kinds.get("Track", 0) > 0)
        rows.append(row(ONSHEET, "the footprints of a hierarchical sheet are selected", ok,
                        f"sheet {first[-8:]} ({vertical['sheet']['name']}): KiCad selects {len(sel_v)} footprints equal to the "
                        f"{len(expected[first])} whose (path) in the board file lies on that sheet; sheet {second[-8:]} replaces "
                        f"them with its own {len(sel_h)} (disjoint); the root sheet selects {len(sel_r)}; the name "
                        f"{vertical['sheet']['name']!r} reaches the same set; by default (as KiCad's selectAllItemsOnSheet, which always selects "
                        f"connections) KiCad also selects {kinds}; include_connections=False gave footprints only",
                        control=f"nothing was selected before; an unknown sheet is refused ({unknown}); a file used by two sheets is "
                                f"refused as ambiguous ({ambiguous}); neither changes the selection ({unchanged})"))
        del sheets
    finally:
        session.close(discard=True)


# --- 3D viewer ------------------------------------------------------------------------------------------

def view_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    import psutil

    session = GuiSession(next((fixtures / VIEW_FIXTURE).glob("*.kicad_pcb")))
    settings = session.profile / "config" / session._settings_version() / "3d_viewer.json"
    closed = False
    try:
        session.start()
        start = ff.set_3d_model_bounding_boxes(session, None)
        on = ff.set_3d_model_bounding_boxes(session, True)
        repeat = ff.set_3d_model_bounding_boxes(session, True)
        off = ff.set_3d_model_bounding_boxes(session, False)
        back_on = ff.set_3d_model_bounding_boxes(session, True)
        frame, _ = hotkeys.canvas_of(session, "3D Viewer")
        pid = session.pid
        hotkeys.user32.PostMessageW(frame, hotkeys.WM_CLOSE, 0, 0)  # the viewer writes its settings on the way out
        time.sleep(3)
        pcb, _ = hotkeys.canvas_of(session, "PCB Editor")
        hotkeys.user32.PostMessageW(pcb, hotkeys.WM_CLOSE, 0, 0)
        for _ in range(60):
            if not psutil.pid_exists(pid):
                break
            time.sleep(1)
        closed = True
        saved = json.loads(settings.read_text(encoding="utf-8")).get("render", {}).get("opengl_show_model_bbox") \
            if settings.exists() else None
        ok = (start["before"] is False and on["after"] is True and on["changed"] and repeat["changed"] is False
              and repeat["after"] is True and off["after"] is False and off["changed"] and back_on["after"] is True
              and saved is True)
        rows.append(row(BBOX, "the Appearance panel toggle sets Model Bounding Boxes", ok,
                        f"the viewer's Preferences show the setting {start['before']} -> {on['after']} after the toggle, unchanged "
                        f"by a repeat, {off['after']} after the toggle back, {back_on['after']} again; 3d_viewer.json, written by the "
                        f"viewer on exit, holds render.opengl_show_model_bbox = {saved}",
                        control="the setting was off at start (a repeat request changes nothing; each toggle flips it)"))
    finally:
        session.close(discard=True)
        del closed




# --- the BOM generator dialog's own intermediate netlist ------------------------------------------------

COPY_SCRIPT = """import shutil, sys
open(sys.argv[2] + ".argv.txt", "w", encoding="utf-8").write("\\n".join(sys.argv))
shutil.copy(sys.argv[1], sys.argv[2])
"""


def _volatile(line: str) -> bool:
    return any(t in line for t in ("<date>", "<tool>"))


def bom_dialog_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    """Tools > Generate Legacy Bill of Materials (DIALOG_BOM) in a hidden schematic session, with a generator whose command
    copies %I to a known file; Generate runs KiCad's own WriteNetListFile(NET_TYPE_BOM, GNL_OPT_BOM|GNL_ALL). The copy is
    compared with the intermediate netlist the tool hands its generator."""
    import sys

    session = GuiSession(next((fixtures / PCB_FIXTURE).glob("*.kicad_sch")))
    work = Path(session.document).parent
    script = work / "copy_netlist.py"
    script.write_text(COPY_SCRIPT, encoding="utf-8")
    out = work / "dialog-copy.xml"
    command = f'"{sys.executable}" "{script}" "%I" "%P/dialog-copy.xml"'
    config = session.profile / "config" / session._settings_version()
    config.mkdir(parents=True, exist_ok=True)
    (config / "eeschema.json").write_text(json.dumps({"meta": {"version": 4}, "bom": {
        "selected_plugin": "copy_netlist",
        "plugins": [{"name": "copy_netlist", "path": str(script), "command": command}]}}), encoding="utf-8")
    session.start()
    try:
        frame = hotkeys.canvas_of(session, "Schematic Editor")[0]
        before = {w["hwnd"] for w in session.kicad.desktop.windows() if w["pid"] == session.pid}
        item = next(i for i in hotkeys.menu_items(frame) if "legacy bill of materials" in i["label"])
        hotkeys.user32.PostMessageW(frame, 0x0111, item["id"], 0)
        dialog = None
        for _ in range(40):
            time.sleep(0.5)
            dialog = next((w for w in session.kicad.desktop.windows() if w["pid"] == session.pid and w["visible"]
                           and w["hwnd"] not in before and w["title"]), None)
            if dialog:
                break
        pressed = bool(dialog) and _press_dialog_button(session, dialog["title"], "Generate")
        for _ in range(60):
            if out.exists() and out.with_name(out.name + ".argv.txt").exists():
                break
            time.sleep(0.5)
        time.sleep(1.0)
        if dialog:
            _press_dialog_button(session, dialog["title"], "Close")
        dialog_copy = out.read_bytes() if out.exists() else b""
        argv = out.with_name(out.name + ".argv.txt").read_text(encoding="utf-8").split("\n") if out.exists() else []
        # the tool's own intermediate netlist, on the same session and file
        done = ff.run_bom_generator(session, f'"{sys.executable}" "{script}" "%I" "%P/tool-copy.xml"',
                                    str(work / "tool-out"), allow_execute=True)
        tool_copy = (work / "tool-copy.xml").read_bytes() if (work / "tool-copy.xml").exists() else b""
        tool_argv = (work / "tool-copy.xml.argv.txt").read_text(encoding="utf-8").split("\n") if tool_copy else []
        a_lines = [ln for ln in dialog_copy.decode("utf-8", "replace").splitlines() if not _volatile(ln)]
        b_lines = [ln for ln in tool_copy.decode("utf-8", "replace").splitlines() if not _volatile(ln)]
        import difflib

        diff = [d for d in difflib.unified_diff(a_lines, b_lines, lineterm="", n=0) if not d.startswith(("---", "+++", "@@"))]
        ok = (bool(dialog) and pressed and bool(dialog_copy) and len(a_lines) > 100 and not diff and done["returncode"] == 0
              and len(argv) == 3 and argv[1].endswith("StickHub.xml") and len(tool_argv) == 3)
        rows.append(row(BOM, "the BOM dialog's own intermediate netlist equals the tool's", ok,
                        f"Tools > Generate Legacy Bill of Materials opened a dialog titled {dialog['title'] if dialog else None!r}; "
                        f"Generate ran KiCad's command (argv[1] = {argv[1][-40:] if len(argv) > 1 else None!r}, output {argv[2][-25:] if len(argv) > 2 else None!r}) "
                        f"and the copied %I has {len(a_lines)} lines outside the date/tool header; the tool's %I "
                        f"({tool_argv[1][-40:] if len(tool_argv) > 1 else None!r}) has {len(b_lines)}; differing lines: {len(diff)} "
                        f"{diff[:4]}; the dialog's command ran through wxExecute on the private desktop (nothing reached the owner's screen: "
                        f"the session's tripwire watches it)",
                        control="the date and tool header lines are ignored; the copy script also logs the argv KiCad passed (%I, %O/%P expanded)"))
    finally:
        session.close(discard=True)


# --- KiCad's own recovery prompt and a real autosave ---------------------------------------------------

def _start_in_thread(session):
    import threading

    box: dict = {}

    def target() -> None:
        try:
            session.start()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


def _prompt_windows(session) -> list[str]:
    if session.kicad is None or getattr(session.kicad, "desktop", None) is None:
        return []
    return [w["title"] for w in session.kicad.desktop.windows()
            if w["pid"] == session.pid and w["visible"] and w["title"] and "PCB Editor" not in w["title"]]


def _press_dialog_button(session, title: str, label: str) -> bool:
    """BM_CLICK on the button of the window titled ``title`` whose text is ``label`` (the Auto-Save Recovery dialog
    is small, and UI Automation does not list its clipped buttons)."""
    import ctypes

    top = next((w["hwnd"] for w in session.kicad.desktop.windows() if w["pid"] == session.pid and w["title"] == title), None)
    if top is None:
        return False
    rect = hotkeys._Rect()  # the dialog opens too narrow for its last two buttons (zero width): make it wider
    hotkeys.user32.GetWindowRect(top, ctypes.byref(rect))
    hotkeys.user32.SetWindowPos(top, 0, 0, 0, rect.right - rect.left + 500, rect.bottom - rect.top, 0x0002 | 0x0004 | 0x0010)
    time.sleep(1.0)
    for hwnd, cls, _area, _visible in hotkeys._children(top):
        text = ctypes.create_unicode_buffer(128)
        hotkeys.user32.GetWindowTextW(hwnd, text, 128)
        if cls == "Button" and text.value.replace("&", "") == label:
            hotkeys.user32.PostMessageW(hwnd, 0x00F5, 0, 0)  # BM_CLICK
            return True
    return False


def _prompt_case(src: Path, newer: bool, answer: str | None) -> dict:
    """Open the board with an autosave beside it; watch KiCad's windows while it loads; answer the prompt if it shows."""
    import os

    session = GuiSession(src)
    document = Path(session.document)
    auto = document.with_name(ff.AUTOSAVE_PREFIX + document.name)
    board = document.read_bytes()
    auto.write_bytes(board + b"\n")  # differs from the board
    when = time.time() + 5 if newer else document.stat().st_mtime - 120
    os.utime(auto, (when, when))
    auto_bytes, auto_mtime = auto.read_bytes(), auto.stat().st_mtime
    thread, box = _start_in_thread(session)
    out: dict = {"title": None, "seen": [], "pressed": False}
    try:
        deadline = time.time() + 150
        while thread.is_alive() and time.time() < deadline:
            time.sleep(1.0)
            titles = _prompt_windows(session)
            out["seen"] += [t for t in titles if t not in out["seen"]]
            out["title"] = out["title"] or next((t for t in titles if t.lower() not in ("kicad", "load pcb")), None)
            if out["title"] and answer and not out["pressed"]:
                out["pressed"] = _press_dialog_button(session, out["title"], answer)
        thread.join(timeout=150)
        out["ready"] = session.client is not None and "error" not in box
        out["error"] = str(box.get("error", ""))
        out["recovered"] = sorted(p.name for p in document.parent.glob(f"{document.stem}.recovered.*{document.suffix}"))
        out["autosave_left"] = auto.exists()
        out["board_unchanged"] = document.read_bytes() == board
        out["board_is_autosave"] = document.read_bytes() == auto_bytes
        out["stamp"] = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(auto_mtime))
        if out["recovered"]:
            out["recovered_is_autosave"] = (document.parent / out["recovered"][0]).read_bytes() == auto_bytes
    finally:
        session.close(discard=True)
    return out


def autosave_ui_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    """KiCad's own Auto-Save Recovery prompt: a newer, differing autosave raises it (answered Keep Both, then Restore
    in a second session); an older one raises nothing. The tool's results are compared with what KiCad did."""
    src = next((fixtures / PCB_FIXTURE).glob("*.kicad_pcb"))
    both = _prompt_case(src, True, "Keep Both")
    ok = (both["title"] == "Auto-Save Recovery" and both["pressed"] and len(both["recovered"]) == 1
          and both["recovered"][0] == f"StickHub.recovered.{both['stamp']}.kicad_pcb" and both["recovered_is_autosave"]
          and not both["autosave_left"] and both["board_unchanged"] and both["ready"])
    rows.append(row(RESCUE, "KiCad's own Keep Both names the copy as the tool does", ok,
                    f"opening the board with a newer, differing autosave raised a window titled {both['title']!r} (windows seen "
                    f"{both['seen']}); Keep Both made KiCad write {both['recovered']} (the autosave's time {both['stamp']}), "
                    f"delete the autosave and leave the board file unchanged; the name equals recovered_file_name's "
                    f"[pressed={both['pressed']}, autosave left={both['autosave_left']}, board unchanged={both['board_unchanged']}, "
                    f"ready={both['ready']}, error={both['error']!r}, files={sorted(p.name for p in Path(src).parent.iterdir())[:0]}]",
                    control="an older autosave raises no prompt (a later row); the tool's keep_both wrote the same name form"))
    restore = _prompt_case(src, True, "Restore Auto-saved")
    ok = (restore["title"] == "Auto-Save Recovery" and restore["pressed"] and restore["board_is_autosave"]
          and not restore["autosave_left"] and not restore["recovered"] and restore["ready"])
    rows.append(row(RESCUE, "KiCad's own Restore replaces the board with the autosave, as the tool does", ok,
                    "Restore Auto-saved made the board file equal the autosave's bytes, deleted the autosave, wrote no other "
                    "file and no backup (the tool's restore does the same)",
                    control="the board file differed from the autosave before the answer (the autosave was written with an extra line)"))
    older = _prompt_case(src, False, None)
    ok = (older["ready"] and older["title"] is None and older["autosave_left"] and older["board_unchanged"]
          and not older["recovered"])
    rows.append(row(RESCUE, "KiCad itself ignores an autosave older than the board", ok,
                    f"the board opened with the editor's API ready, windows seen while loading: {older['seen']} (no recovery "
                    f"prompt); the older autosave is still on disk and the board untouched",
                    control="the newer autosave in the first row raised the Auto-Save Recovery prompt"))


def autosave_real_phase(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    """Ask KiCad for a real autosave: interval at its minimum (1 s), file-format autosave, a board edit through the API."""
    session = GuiSession(next((fixtures / PCB_FIXTURE).glob("*.kicad_pcb")))
    config = session.profile / "config" / session._settings_version()
    config.mkdir(parents=True, exist_ok=True)
    (config / "kicad_common.json").write_text(json.dumps({
        "api": {"enable_server": True}, "do_not_show_again": {"data_collection_prompt": True, "update_check_prompt": True},
        "meta": {"version": 3}, "system": {"local_history_debounce": 1}, "auto_backup": {"enabled": True, "format": 1}}))
    session.start()
    try:
        work = Path(session.document).parent
        fp = api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"]["items"][0]
        fp["position"]["x_nm"] = str(int(fp["position"]["x_nm"]) + 1_000_000)
        api.call(session, "UpdateItems", {"items": [fp]})
        frame, canvas = hotkeys.canvas_of(session, "PCB Editor")
        uia = session.attach_uia()
        cid = hotkeys.user32.GetDlgCtrlID(canvas)
        with contextlib.suppress(Exception):
            uia.call("focus", {"window": {"name": "PCB Editor", "match": "contains"},
                               "selector": {"by": "automation_id", "automation_id": str(cid)}})
        found: list[str] = []
        for _ in range(24):
            time.sleep(5)
            found = sorted(p.name for p in work.rglob("*") if p.name.startswith(ff.AUTOSAVE_PREFIX))
            if found:
                break
        rows.append({"action": RESCUE, "item": f"action:{RESCUE}", "case": "KiCad's own autosave timer on a private desktop",
                     "outcome": "info", "detail": f"interval 1 s (the minimum), file-format autosave, board edited through the API "
                     f"(OnModify), canvas focused through UI Automation: {'autosave file(s) written: ' + str(found) if found else 'no _autosave-* file in 120 s'}",
                     "autosave_written": bool(found)})
    finally:
        session.close(discard=True)


# --- HPGL (removed from KiCad) --------------------------------------------------------------------------

def hpgl_rows(fixtures: Path, rows: list[dict], scratch: Path) -> None:
    from kicad_mcp.ipc.session import find_kicad_cli

    cli = find_kicad_cli()
    board = next((fixtures / PCB_FIXTURE).glob("*.kicad_pcb"))
    sch = next((fixtures / PCB_FIXTURE).glob("*.kicad_sch"))

    def run(args: list[str]):
        return subprocess.run([cli, *args], capture_output=True, text=True, timeout=300, creationflags=0x08000000)

    for command, document in (("pcb", board), ("sch", sch)):
        out_dir = scratch / f"hpgl-{command}"
        out_dir.mkdir()
        done = run([command, "export", "hpgl", "-o", str(out_dir), str(document)])
        wrote = sorted(p.name for p in out_dir.rglob("*") if p.is_file())
        ctl_dir = scratch / f"svg-{command}"
        ctl_dir.mkdir()
        if command == "pcb":
            ctl = run(["pcb", "export", "svg", "--layers", "F.Cu", "-o", str(ctl_dir / "out.svg"), str(document)])
        else:
            ctl = run(["sch", "export", "svg", "-o", str(ctl_dir), str(document)])
        ctl_files = sorted(p.name for p in ctl_dir.rglob("*.svg") if p.stat().st_size > 0)
        text = (done.stdout + done.stderr).strip()
        ok = "no longer supported" in text and not wrote and ctl.returncode == 0 and bool(ctl_files)
        rows.append({"item": f"cli:{command} export hpgl", "action": f"{command} export hpgl", "case": "KiCad refuses it",
                     "outcome": "removed_upstream" if ok else "fail", "returncode": done.returncode,
                     "detail": f"{text[:200]}; output files written by the hpgl call: {wrote or 'none'}",
                     "control": f"{command} export svg of the same file exits {ctl.returncode} and writes {ctl_files}"})


TOOL_OLDER = "an autosave older than the document is ignored"
KICAD_OLDER = "KiCad itself ignores an autosave older than the board"


def merge_older(rows: list[dict]) -> list[dict]:
    """One row for the older-autosave rule: the tool's result and KiCad's own readback (no prompt, autosave kept)."""
    mine = next((r for r in rows if r.get("case") == TOOL_OLDER), None)
    kicad = next((r for r in rows if r.get("case") == KICAD_OLDER), None)
    if mine is None or kicad is None:
        return rows
    mine["outcome"] = "ok" if mine["outcome"] == "ok" and kicad["outcome"] == "ok" else "fail"
    mine["detail"] += f"; KiCad: {kicad['detail']}"
    mine["control"] += f"; {kicad['control']}"
    return [r for r in rows if r is not kicad]


def function_covered(rows: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for action in ACTIONS:
        mine = [r for r in rows if r.get("action") == action]
        if mine and all(r["outcome"] in ("ok", "info") for r in mine) and any(r["outcome"] == "ok" for r in mine):
            out[f"action:{action}"] = f"{TOOLS[action]}: " + " | ".join(r["case"] for r in mine)
    return out


def evidence(inventory: dict, rows: list[dict]) -> dict:
    import os

    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                          cwd=Path(__file__).resolve().parents[2], creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip()
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "function-fallbacks-live", "repo_head": head,
            "passed": [],  # the dead actions stay upstream_gap: this evidence only records the function covered
            "upstream_gaps": sorted([f"action:{a}" for a in ACTIONS] + [f"cli:{c}" for c in HPGL]),
            "function_covered": function_covered(rows), "rows": rows}


PHASE_FUNCTIONS = {"bom-dialog": bom_dialog_phase, "embed-pcb": embed_pcb_phase, "autosave": autosave_phase, "autosave-ui": autosave_ui_phase,
                   "autosave-real": autosave_real_phase, "sch": sch_phase, "hier": hier_phase,
                   "3d": view_phase, "hpgl": hpgl_rows}
PHASE_ACTIONS = {"bom-dialog": {BOM}, "embed-pcb": {EXTRACT, REMOVE}, "autosave": {RESCUE}, "autosave-ui": {RESCUE}, "autosave-real": {RESCUE}, "sch": {SYNC, BOM, EXTRACT, REMOVE},
                 "hier": {ONSHEET}, "3d": {BBOX}, "hpgl": set(HPGL)}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    import tempfile

    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True, help="folder holding the fixture projects")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default=",".join(PHASES))
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    wanted = args.only.split(",")
    kept = json.loads(args.out.read_text(encoding="utf-8"))["rows"] if args.out.exists() else []
    rows = [r for r in kept if r.get("phase") not in wanted]
    scratch = Path(scoped_mkdtemp(prefix="function-fallbacks-"))
    try:
        for phase in wanted:
            new: list[dict] = []
            (scratch / phase).mkdir()
            try:
                PHASE_FUNCTIONS[phase](args.fixtures, new, scratch / phase)
            except Exception as exc:  # noqa: BLE001 - a crashed phase is a failed row, not a lost run
                new.append({"action": sorted(PHASE_ACTIONS[phase])[0], "item": f"action:{sorted(PHASE_ACTIONS[phase])[0]}",
                            "case": f"phase {phase} crashed", "outcome": "fail", "detail": f"{type(exc).__name__}: {exc}"})
            for r in new:
                r["phase"] = phase
            rows += new
            rows = merge_older(rows)
            args.out.write_text(json.dumps(evidence(inventory, rows), indent=1) + "\n", encoding="utf-8")
            for r in new:
                print(r["outcome"], r["item"], "|", r["case"], "|", r["detail"][:260])
    finally:
        with contextlib.suppress(Exception):
            shutil.rmtree(scratch, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
