"""MCP tools for KiCad actions that are dead inside KiCad itself, whose FUNCTION exists elsewhere.

Seven actions of the KiCad build are declared but cannot be reached: no tool transition binds them, or
their handler is never posted, never defined, or needs a parameter nothing can give. The coverage gate
keeps them ``upstream_gap`` (the action itself does nothing). Each function they name does exist in
KiCad's UI or files, and the owner requires every KiCad function to be usable, so each gets a real tool
here that does that function by the route KiCad itself uses:

- ``3DViewer.Control.showBoundingBoxes``: the Appearance panel's "Model Bounding Boxes" toggle of the 3D
  viewer (``set_3d_model_bounding_boxes``); the state is read back from the viewer's own Preferences page.
- ``common.Embed.extractFile`` / ``common.Embed.removeFile``: the Embedded Files panel's Extract and
  Remove (``embedded_files``, ``extract_embedded_file``, ``remove_embedded_file``). A board goes through
  the API's Get/SetEmbeddedFiles; a schematic through its file (decoded exactly as KiCad does: base64,
  then zstd), with the editor reloading the changed file.
- ``eeschema.EditorControl.generateBOMExternal``: run an external BOM generator on the schematic's
  intermediate netlist, as the BOM generator dialog does (``run_bom_generator``).
- ``eeschema.InteractiveSelection.SyncSelection``: the API's SyncSelection with symbol/pin items
  (``sync_selection``), which the schematic editor handles like a cross-probe from the board.
- ``pcbnew.EditorControl.rescueAutosave``: recover the editor's document from the autosave file KiCad
  left beside it (``recover_autosave``), as the recovery dialog's Restore does.
- ``pcbnew.InteractiveSelection.SelectOnSheet``: select every footprint of a schematic sheet
  (``select_items_on_sheet``), through the API's SyncSelection, which runs KiCad's own selection code.

Every function takes a session (a GuiSession for the editor-bound ones) and returns JSON-ready dicts;
``register_function_fallback_tools`` wraps them with a session id, like ``fallback_tools``.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import base64
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from kicad_mcp.ipc import api
from kicad_mcp.ipc.session import SessionError

EMBED_TYPES = {"EFT_OTHER": "other", "EFT_FONT": "font", "EFT_MODEL": "model", "EFT_WORKSHEET": "worksheet",
               "EFT_DATASHEET": "datasheet", "EFT_UNKNOWN": "other"}
WM_COMMAND, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0111, 0x0201, 0x0202
BBOX_CONTROL_ID = 1618  # the wx id of the Appearance panel's "Model Bounding Boxes" row
CREATE_NO_WINDOW = 0x08000000
MAX_EMBEDDED_BYTES = 256 * 1024 * 1024


# --- embedded files --------------------------------------------------------------------------------

def zstd_decompress(data: bytes, *, limit: int = MAX_EMBEDDED_BYTES) -> bytes:
    try:
        import zstandard
    except ImportError:
        try:
            from compression import zstd  # Python 3.14+
        except ImportError as exc:
            raise SessionError("decoding embedded files needs the 'zstandard' package (pip install zstandard)") from exc
        result = zstd.ZstdDecompressor().decompress(data, max_length=limit + 1)
        if len(result) > limit:
            raise SessionError(f"embedded file expands beyond {limit} bytes; refusing")
        return result
    with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as reader:
        chunks = []
        total = 0
        while chunk := reader.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise SessionError(f"embedded file expands beyond {limit} bytes; refusing")
            chunks.append(chunk)
        return b"".join(chunks)


def zstd_compress(data: bytes, level: int = 15) -> bytes:
    try:
        import zstandard
    except ImportError:
        try:
            from compression import zstd
        except ImportError as exc:
            raise SessionError("encoding embedded files needs the 'zstandard' package (pip install zstandard)") from exc
        return zstd.compress(data, level=level)
    return zstandard.ZstdCompressor(level=level).compress(data)


def decode_payload(encoded: bytes | str) -> bytes:
    """What KiCad stores for an embedded file (base64 text of a zstd frame) back to the file's bytes."""
    text = encoded.decode("ascii") if isinstance(encoded, bytes) else encoded
    return zstd_decompress(base64.b64decode("".join(text.split())))


def encode_payload(raw: bytes) -> bytes:
    return base64.b64encode(zstd_compress(raw))


def _block_end(text: str, start: int) -> int:
    """Index just past the s-expression opened at ``start`` (strings and |...| data blocks are skipped)."""
    depth, pos, quote, pipe = 0, start, False, False
    while pos < len(text):
        c = text[pos]
        if quote:
            if c == "\\":
                pos += 1
            elif c == '"':
                quote = False
        elif pipe:
            pipe = c != "|"
        elif c == '"':
            quote = True
        elif c == "|":
            pipe = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    raise SessionError("unbalanced s-expression in the document")


def _top_level_block(text: str, token: str) -> tuple[int, int] | None:
    """Span of the ``(token ...)`` block that is a direct child of the document's root expression."""
    depth, pos, quote, pipe = 0, 0, False, False
    while pos < len(text):
        c = text[pos]
        if quote:
            if c == "\\":
                pos += 1
            elif c == '"':
                quote = False
        elif pipe:
            pipe = c != "|"
        elif c == '"':
            quote = True
        elif c == "|":
            pipe = True
        elif c == "(":
            depth += 1
            if depth == 2 and re.match(rf"\({re.escape(token)}[\s)]", text[pos:pos + len(token) + 2]):
                return pos, _block_end(text, pos)
        elif c == ")":
            depth -= 1
        pos += 1
    return None


_FILE = re.compile(r'\(file\s*\(name\s+"((?:[^"\\]|\\.)*)"\)\s*\(type\s+(\w+)\)\s*(?:\(data\s*\|(.*?)\|\s*\)\s*)?'
                   r'\(checksum\s+"([0-9A-Fa-f]*)"\)\s*\)', re.S)


def parse_embedded_files(text: str) -> list[dict[str, Any]]:
    """The files of the document's own ``(embedded_files ...)`` block: name, type, data (KiCad's base64 of a
    zstd frame, or None when the file omits it) and checksum. Blocks inside footprints are not included."""
    span = _top_level_block(text, "embedded_files")
    if span is None:
        return []
    block = text[span[0]:span[1]]
    return [{"name": re.sub(r"\\(.)", r"\1", m.group(1)), "type": m.group(2), "data": m.group(3),
             "checksum": m.group(4), "span": (span[0] + m.start(), span[0] + m.end())}
            for m in _FILE.finditer(block)]


_WINDOWS_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\..*)?$", re.I)


def _safe_name(name: str) -> str:
    """The leaf of an embedded file's name. The name comes from the document (untrusted): drive-relative
    names (``C:x``), alternate data streams (``a:b``), control characters and Windows device names are refused,
    so the target can never leave ``dest_dir``."""
    leaf = re.split(r"[\\/]", name)[-1].strip()
    if (not leaf or leaf in (".", "..") or ":" in leaf or any(ord(c) < 32 for c in leaf)
            or _WINDOWS_RESERVED.match(leaf) or leaf.endswith((".", " "))):
        raise SessionError(f"embedded file name {name!r} is not usable as a file name")
    return leaf


def _board_embedded(session) -> list[dict[str, Any]]:
    got = api.call(session, "GetEmbeddedFiles", {})["response"].get("files", [])
    return [{"name": f["name"], "type": EMBED_TYPES.get(f.get("type", "EFT_OTHER"), "other"),
             "data": base64.b64decode(f.get("data", "")).decode("ascii"), "checksum": f.get("data_hash", "")}
            for f in got]


def _document_embedded(session) -> list[dict[str, Any]]:
    api.call(session, "SaveDocument", {})  # the file is what the editor holds now
    return parse_embedded_files(Path(session.document).read_text(encoding="utf-8"))


def _files(session) -> tuple[list[dict[str, Any]], str]:
    if session.kind == "pcb":
        return _board_embedded(session), "api GetEmbeddedFiles"
    return _document_embedded(session), "document file"


def embedded_files(session) -> dict[str, Any]:
    """The Embedded Files panel's list: name, type, stored size, decoded size and KiCad's checksum."""
    rows, source = _files(session)
    out = []
    for f in rows:
        raw = decode_payload(f["data"]) if f["data"] else None
        out.append({"name": f["name"], "type": f["type"], "checksum": f["checksum"],
                    "size": None if raw is None else len(raw), "sha256": None if raw is None else hashlib.sha256(raw).hexdigest()})
    return {"files": out, "source": source}


def extract_embedded_file(session, name: str, dest_dir: str, overwrite: bool = False) -> dict[str, Any]:
    """The panel's Extract: decode embedded file ``name`` and write it into ``dest_dir``."""
    rows, source = _files(session)
    match = next((f for f in rows if f["name"] == name), None)
    if match is None:
        raise SessionError(f"no embedded file {name!r}; there are {[f['name'] for f in rows]}")
    if not match["data"]:
        raise SessionError(f"embedded file {name!r} carries no data in this document")
    raw = decode_payload(match["data"])
    dest = Path(dest_dir).resolve()
    target = dest / _safe_name(name)
    if target.resolve().parent != dest:  # containment: an odd or linked leaf must not escape dest_dir
        raise SessionError(f"embedded file name {name!r} resolves outside {dest}")
    if target.exists() and not overwrite:
        raise SessionError(f"{target} exists; pass overwrite=True to replace it")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)
    return {"name": name, "path": str(target), "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "checksum": match["checksum"], "source": source}


def remove_embedded_file(session, name: str) -> dict[str, Any]:
    """The panel's Remove: take embedded file ``name`` out of the document. The editor's own state is read
    back afterwards (a board: GetEmbeddedFiles; a schematic: the file KiCad saves after reloading)."""
    if session.kind == "pcb":
        files = api.call(session, "GetEmbeddedFiles", {})["response"].get("files", [])
        kept = [f for f in files if f["name"] != name]
        if len(kept) == len(files):
            raise SessionError(f"no embedded file {name!r}; there are {[f['name'] for f in files]}")
        api.call(session, "SetEmbeddedFiles", {"files": {"files": kept}})
    else:
        try:
            via = _remove_through_setup_panel(session, name)
        except Exception as exc:  # noqa: BLE001 - the panel could not be driven; fall back to the file
            via = f"file edit (the Embedded Files panel could not be driven: {type(exc).__name__}: {str(exc)[:120]})"
            _remove_by_file_edit(session, name)
        remaining, source = _files(session)
        return {"removed": name, "remaining": [f["name"] for f in remaining], "source": source, "mechanism": via}
    remaining, source = _files(session)
    return {"removed": name, "remaining": [f["name"] for f in remaining], "source": source}


def _remove_by_file_edit(session, name: str) -> None:
    """Fallback: rewrite the saved schematic without the file and have the editor reload it."""
    api.call(session, "SaveDocument", {})
    path = Path(session.document)
    text = path.read_text(encoding="utf-8")
    rows = parse_embedded_files(text)
    victim = next((f for f in rows if f["name"] == name), None)
    if victim is None:
        raise SessionError(f"no embedded file {name!r}; there are {[f['name'] for f in rows]}")
    if len(rows) == 1:
        block_begin, block_end = _top_level_block(text, "embedded_files")
        line = text.rfind("\n", 0, block_begin) + 1
        text = text[:line] + text[block_end:].lstrip(" \t\r\n")
    else:
        begin, end = victim["span"]
        text = text[:begin].rstrip(" \t\r\n") + "\n" + text[end:].lstrip(" \t")
    path.write_text(text, encoding="utf-8", newline="\n")
    api.call(session, "RevertDocument", {})
    time.sleep(2.0)


def _left(hwnd: int) -> int:
    from kicad_mcp.ipc import hotkeys

    rect = hotkeys._Rect()
    hotkeys.user32.GetWindowRect(hwnd, hotkeys.ctypes.byref(rect))
    return rect.left


def _setup_panel_remove_once(session, name: str, names: list[str], row_height: int) -> None:
    """One pass through Schematic Setup > Schematic Data > Embedded Files: click the file's row (rows are in name
    order, ``row_height`` pixels each, below the header), press the panel's Remove button
    (PANEL_EMBEDDED_FILES::onDeleteEmbeddedFile), then OK."""
    import ctypes

    from kicad_mcp.ipc import hotkeys

    user32 = hotkeys.user32
    frame = hotkeys.canvas_of(session, "Schematic Editor")[0]
    item = next(i for i in hotkeys.menu_items(frame) if i["label"] == "schematic setup")
    user32.PostMessageW(frame, WM_COMMAND, item["id"], 0)
    dialog = {"name": "Schematic Setup", "match": "contains"}
    uia = session.attach_uia()
    for _ in range(40):
        try:
            uia.call("find", {"window": dialog})
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    else:
        raise SessionError("Schematic Setup did not open")
    time.sleep(1.0)
    try:
        uia.call("select", {"window": dialog, "selector": {"by": "path", "name": "Embedded Files", "control_type": "TreeItem"}})
        time.sleep(1.5)
        top = next(w["hwnd"] for w in session.kicad.desktop.windows()
                   if w["pid"] == session.pid and w["visible"] and w["title"] == "Schematic Setup")

        def ids(title: str) -> list[int]:
            got = uia.call("state", {"window": dialog, "selector": {"by": "path", "name": title, "control_type": "Pane"}})
            return [int(c["automation_id"]) for c in got["controls"]]

        by_id = {ctypes.c_int(user32.GetDlgCtrlID(h)).value: h for h, _c, _a, _v in hotkeys._children(top)}
        grid = by_id[ids("GridWindow")[0]]
        buttons = sorted((by_id[i] for i in ids("StdBitmapButton")), key=_left)
        remove = buttons[1]  # the panel's buttons are Add, Remove
        row = (int((names.index(name) + 0.5) * row_height) << 16) | 40
        user32.PostMessageW(grid, WM_LBUTTONDOWN, 1, row)
        time.sleep(0.1)
        user32.PostMessageW(grid, WM_LBUTTONUP, 0, row)
        time.sleep(0.6)
        point = (6 << 16) | 6
        user32.PostMessageW(remove, WM_LBUTTONDOWN, 1, point)
        time.sleep(0.1)
        user32.PostMessageW(remove, WM_LBUTTONUP, 0, point)
        time.sleep(1.0)
    finally:
        uia.call("invoke", {"window": dialog, "selector": {"by": "path", "name": "OK", "control_type": "Button"}})
        time.sleep(1.5)


@cleanup_after_call
def _remove_through_setup_panel(session, name: str) -> str:
    """Remove through the Schematic Setup panel, checking the editor's own state before anything is kept: after
    OK the schematic is saved as a scratch copy (SaveCopyOfDocument) and read; when the wrong row was removed the
    editor reloads the untouched file and the pass is repeated with another row height."""
    import ctypes

    from kicad_mcp.ipc import hotkeys

    names = sorted(f["name"] for f in _document_embedded(session))
    if name not in names:
        raise SessionError(f"no embedded file {name!r}; there are {names}")
    frame = hotkeys.canvas_of(session, "Schematic Editor")[0]
    # A wrong row is undone by reloading the saved file, which would also drop unsaved edits: refuse instead.
    if hotkeys.window_text(frame).lstrip().startswith("*"):
        raise SessionError("the schematic has unsaved changes; save it first (a misplaced click is undone by reloading "
                           "the saved file, which would discard them)")
    scale = hotkeys.user32.GetDpiForWindow(ctypes.c_void_p(frame)) / 96.0

    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-embcheck-")) / "check.kicad_sch"
    want = [n for n in names if n != name]
    tried: list[int] = []
    for base in (24, 28, 32, 36, 20, 40, 44, 48):
        height = int(base * scale)
        tried.append(height)
        _setup_panel_remove_once(session, name, names, height)
        api.call(session, "SaveCopyOfDocument", {"path": str(scratch), "options": {"overwrite": True}})
        left = sorted(f["name"] for f in parse_embedded_files(scratch.read_text(encoding="utf-8")))
        scratch.unlink(missing_ok=True)
        if left == want:
            api.call(session, "SaveDocument", {})
            return f"Schematic Setup > Embedded Files > Remove (UI Automation), then OK (row height {height}px)"
        api.call(session, "RevertDocument", {})  # the wrong row went: back to the saved file, try another height
        time.sleep(2.0)
    raise SessionError(f"the Embedded Files panel never removed exactly {name!r} (row heights tried: {tried})")


# --- external BOM generator ------------------------------------------------------------------------

def _kicad_python(session) -> str:
    """KiCad's own interpreter for a leading ``python``; never this server's interpreter as a fallback."""
    app_root = getattr(session, "app_root", "") or ""
    if app_root:
        root = Path(app_root)
        for candidate in (root / "bin" / "python.exe", root / "bin" / "python3"):
            if candidate.exists():
                return str(candidate)
    raise SessionError("KiCad's own python was not found next to this installation; name the interpreter in "
                       "the command explicitly")


def expand_generator_command(template: str, netlist: Path, output_base: Path, project_dir: Path) -> str:
    """KiCad's BOM generator placeholders: %I intermediate netlist, %O output path without extension,
    %B output base name, %P project directory (NETLIST_EXPORTER_BASE::MakeCommandLine)."""
    out = str(output_base)
    if not out.startswith("\\\\"):
        out = out.replace("\\", "/")  # KiCad's own Windows rule for %O
    values = {"P": str(project_dir), "B": output_base.name, "I": str(netlist), "O": out}
    # one pass: a value that itself contains "%I" or similar is inserted as it is, never expanded again
    return re.sub(r"%([PBIO])", lambda m: values[m.group(1)], template)


def _split_command(line: str) -> list[str]:
    """Split a generator command line the way a Windows program's argv parser does: whitespace outside quotes
    separates arguments, a double-quoted part (anywhere in an argument, as in --opt="a b") keeps its spaces and
    loses its quotes, and backslashes stay as they are (Windows paths). A single quote opens a quoted part only
    at the start of an argument, so a name such as O'Brien is not split."""
    tokens: list[str] = []
    current: list[str] = []
    quote = ""
    started = False
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
            else:
                current.append(ch)
        elif ch == '"' or (ch == "'" and not current):
            quote, started = ch, True
        elif ch.isspace():
            if started:
                tokens.append("".join(current))
            current, started = [], False
        else:
            current.append(ch)
            started = True
    if quote:
        raise SessionError("the generator command has an unclosed quote")
    if started:
        tokens.append("".join(current))
    return tokens


def generator_argv(template: str, netlist: Path, output_base: Path, project_dir: Path) -> list[str]:
    """The generator's argv: the template is split into arguments first and placeholders are filled in per
    argument, so a quote or space in a path can never split or add an argument."""
    return [expand_generator_command(token, netlist, output_base, project_dir) for token in _split_command(template)]


def run_bom_generator(session, command: str, output_base: str | None = None, allow_execute: bool = False,
                      timeout_s: int = 120) -> dict[str, Any]:
    """Run an external BOM generator the way the BOM generator dialog does. ``command`` is the dialog's
    command line (``python "bom_csv_grouped_by_value.py" "%I" "%O.csv"``); the intermediate netlist
    (kicad-cli's python-bom export, the same GNL_OPT_BOM netlist the dialog writes) replaces %I. A
    leading ``python`` runs KiCad's own interpreter. The generator is a program of the caller's choosing,
    so ``allow_execute=True`` is required."""
    from kicad_mcp.utils import secure_subprocess

    if not allow_execute:
        raise SessionError("run_bom_generator starts the generator program the command names; pass allow_execute=True")
    # allow_execute is chosen by the AI client, so it is not consent: running an arbitrary program also needs
    # the host's own opt-in, set by the person who configures the server.
    if os.environ.get("KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR", "").strip() != "1":
        raise SessionError("run_bom_generator is disabled on this host; the person running the server can enable "
                           "it by setting KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR=1 in the server's environment")
    if session.kind != "schematic":
        raise SessionError("a BOM generator works on a schematic session")
    api.call(session, "SaveDocument", {})
    schematic = Path(session.document)
    project_dir = schematic.parent
    base = Path(output_base) if output_base else project_dir / schematic.stem
    base.parent.mkdir(parents=True, exist_ok=True)
    netlist = base.parent / f"{schematic.stem}.xml"
    exported = secure_subprocess.run_contained(
        [session.cli, "sch", "export", "python-bom", "-o", str(netlist), str(schematic)], timeout=timeout_s, text=True)
    if exported.returncode != 0 or not netlist.exists():
        raise SessionError(f"kicad-cli could not write the netlist: {(exported.stderr or exported.stdout).strip()[:300]}")
    line = expand_generator_command(command, netlist, base, project_dir)
    argv = generator_argv(command, netlist, base, project_dir)
    if not argv:
        raise SessionError("the generator command is empty")
    if argv[0].lower() in ("python", "python3", "python.exe"):
        argv[0] = _kicad_python(session)
    started = time.time()
    done = secure_subprocess.run_contained(argv, cwd=str(project_dir), timeout=timeout_s, text=True)
    produced = sorted(str(p) for p in base.parent.glob(f"{base.name}*")
                      if p.is_file() and p != netlist and p.stat().st_mtime >= started - 2)
    return {"command": line, "returncode": done.returncode, "stdout": (done.stdout or "")[-2000:],
            "stderr": (done.stderr or "")[-2000:], "netlist": str(netlist), "outputs": produced,
            "ok": done.returncode == 0 and bool(produced)}


# --- selection -------------------------------------------------------------------------------------

def _ref_of(item: dict[str, Any]) -> str | None:
    text = item.get("reference_field", {}).get("text", {}).get("text")
    if isinstance(text, dict):  # a footprint's field nests one level deeper than a symbol's
        text = text.get("text")
    return text if isinstance(text, str) else None


def selection(session) -> list[dict[str, Any]]:
    """The editor's selection, as KiCad reports it: kind, id and (footprints, symbols) the reference."""
    items = api.call(session, "GetSelection", {})["response"].get("items", [])
    return [{"kind": i["@type"].rsplit(".", 1)[-1], "id": i.get("id", {}).get("value", ""), "reference": _ref_of(i)}
            for i in items]


def sync_selection(session, references: list[str] | None = None, pads: list[dict[str, str]] | None = None,
                   mode: Literal["SSM_ITEMS_ONLY", "SSM_ITEMS_AND_NETS"] = "SSM_ITEMS_ONLY",
                   focus_reference: str | None = None) -> dict[str, Any]:
    """Make the editor's selection the given items (what the other editor sends when the user selects there):
    ``references`` name footprints (board) or symbols (schematic); ``pads`` are ``{"reference", "number"}``.
    ``SSM_ITEMS_AND_NETS`` also selects what is connected, by KiCad's own rules. Returns KiCad's status and the
    selection read back."""
    items: list[dict[str, Any]] = [{"footprint": {"reference": r}} for r in references or []]
    items += [{"pad": {"reference": p["reference"], "number": p["number"]}} for p in pads or []]
    if not items:
        raise SessionError("give references and/or pads to select")
    request: dict[str, Any] = {"context": "SSC_EXPLICIT", "mode": mode, "items": items}
    if focus_reference:
        request["focus_item"] = {"footprint": {"reference": focus_reference}}
    status = api.call(session, "SyncSelection", request)["response"]
    return {"status": status.get("status"), "message": status.get("message", ""), "selected": selection(session)}


def board_sheets(session) -> list[dict[str, Any]]:
    """The schematic sheets the board's footprints come from: path (the sheet's uuid path, root omitted as
    KiCad stores it on footprints, "/" for the root sheet), sheet name, sheet file and footprint references."""
    if session.kind != "pcb":
        raise SessionError("sheets are listed from a board session")
    sheets: dict[str, dict[str, Any]] = {}
    for fp in api.call(session, "GetItems", {"types": ["KOT_PCB_FOOTPRINT"]})["response"].get("items", []):
        path = [e["value"] for e in fp.get("symbol_path", {}).get("path", [])]
        key = "/" + "/".join(path[:-1]) if len(path) > 1 else "/"
        entry = sheets.setdefault(key, {"path": key, "name": fp.get("symbol_sheet_name", ""),
                                        "file": fp.get("symbol_sheet_filename", ""), "references": []})
        ref = _ref_of(fp)
        if ref:
            entry["references"].append(ref)
    return sorted(sheets.values(), key=lambda s: s["path"])


def select_items_on_sheet(session, sheet: str, include_connections: bool = True) -> dict[str, Any]:
    """Select all footprints of one schematic sheet (KiCad's "select all items on sheet"). ``sheet`` is the
    sheet's uuid path ("/00000000-...-4b3a1333"; "/" for the root sheet) or its name ("/ampli_ht_vertical/" or
    "ampli_ht_vertical"); a sheet file name works when only one sheet uses that file. The previous selection
    is replaced. Like KiCad's selectAllItemsOnSheet, KiCad's own connection selection runs too
    (``include_connections``, the default: tracks and vias of the nets that lie wholly inside the selected
    footprints); ``False`` selects the footprints only."""
    sheets = board_sheets(session)
    want = sheet.strip()
    norm = want.strip("/")
    hits = [s for s in sheets if s["path"].strip("/") == norm and (norm or s["path"] == "/")]
    if not hits:
        hits = [s for s in sheets if s["name"].strip("/") == norm and norm]
    if not hits:
        hits = [s for s in sheets if s["file"] == want]
    if not hits:
        raise SessionError(f"no sheet {sheet!r} on this board; sheets: "
                           f"{[(s['path'], s['name'], s['file']) for s in sheets]}")
    if len(hits) > 1:
        raise SessionError(f"{sheet!r} names {len(hits)} sheets; use one of the paths "
                           f"{[s['path'] for s in hits]}")
    target = hits[0]
    done = sync_selection(session, target["references"],
                          mode="SSM_ITEMS_AND_NETS" if include_connections else "SSM_ITEMS_ONLY")
    return {**done, "sheet": {k: target[k] for k in ("path", "name", "file")}, "footprints": len(target["references"])}


# --- autosave recovery -----------------------------------------------------------------------------

AUTOSAVE_PREFIX = "_autosave-"


def find_autosave(session) -> dict[str, Any] | None:
    """The autosave file KiCad left for the session's document, if it is newer than the document and differs:
    ``_autosave-<name>`` beside the document (backup location "project folder"), or the same name under an
    ``autosave`` folder of the session's profile (location "user folder")."""
    document = Path(session.document)
    spots = [document.with_name(AUTOSAVE_PREFIX + document.name)]
    for root, _dirs, files in _walk(Path(session.profile)):
        if document.name in files and "autosave" in Path(root).parts:
            spots.append(Path(root) / document.name)
    for spot in spots:  # KiCad: stale when the document is missing, or the autosave is newer and differs
        if spot.is_file() and (not document.exists() or (spot.stat().st_mtime > document.stat().st_mtime
                                                         and spot.read_bytes() != document.read_bytes())):
            return {"path": str(spot), "modified": spot.stat().st_mtime, "size": spot.stat().st_size}
    return None


def _walk(root: Path):
    import os

    return os.walk(root) if root.exists() else iter(())


def recovered_file_name(document: Path, stamp: float) -> Path:
    """KiCad's buildRecoveredFileName: ``<name>.recovered.<YYYY-mm-dd_HHMMSS>`` (+ ``.<n>`` while taken)."""
    text = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(stamp))
    target = document.with_name(f"{document.stem}.recovered.{text}{document.suffix}")
    seq = 1
    while target.exists():
        target = document.with_name(f"{document.stem}.recovered.{text}.{seq}{document.suffix}")
        seq += 1
    return target


def recover_autosave(session, keep: Literal["restore", "keep_both"] = "restore", backup: bool = False) -> dict[str, Any]:
    """The recovery dialog's Restore Auto-saved / Keep Both (EDA_BASE_FRAME::CheckForAutosaveFiles).
    ``restore`` copies the autosave over the document and deletes it, exactly as KiCad does (the previous
    saved content is discarded; with ``backup=True``, an addition of ours, it is first kept beside the document
    as ``<name>.before-recovery``), then the editor reloads. ``keep_both`` writes the autosave as
    ``<name>.recovered.<time>`` (KiCad's name, the autosave's time), deletes the autosave and leaves the
    editor on the current document. Nothing changes when no autosave newer than the document exists."""
    found = find_autosave(session)
    if found is None:
        return {"recovered": False, "reason": "no autosave file newer than the document"}
    document, autosave = Path(session.document), Path(found["path"])
    if keep == "keep_both":
        target = recovered_file_name(document, autosave.stat().st_mtime)
        shutil.copyfile(autosave, target)
        autosave.unlink()
        return {"recovered": True, "mode": keep, "path": str(target), "autosave": found["path"]}
    result: dict[str, Any] = {"recovered": True, "mode": keep, "document": str(document), "autosave": found["path"]}
    if backup:
        kept = document.with_name(document.name + ".before-recovery")
        shutil.copy2(document, kept)
        result["backup"] = str(kept)
    shutil.copyfile(autosave, document)
    autosave.unlink()
    api.call(session, "RevertDocument", {})
    time.sleep(2.0)
    return result


# --- 3D viewer: model bounding boxes ---------------------------------------------------------------

def _viewer_frame(session, timeout_s: float = 60.0) -> int:
    from kicad_mcp.ipc import hotkeys

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            return hotkeys.canvas_of(session, "3D Viewer")[0]
        except RuntimeError:
            time.sleep(0.5)
    raise SessionError("the 3D viewer did not open")


def _bbox_preference(session, uia, frame: int) -> bool:
    """The 3D Viewer > Realtime Renderer preference page's "Show model bounding boxes", read from the viewer's
    open Preferences dialog (which shows the live setting), then cancelled."""
    from kicad_mcp.ipc import hotkeys

    dialog = {"name": "Preferences", "match": "contains"}
    item = next(i for i in hotkeys.menu_items(frame) if i["label"] == "preferences")
    hotkeys.user32.PostMessageW(frame, WM_COMMAND, item["id"], 0)
    for _ in range(40):
        try:
            uia.call("find", {"window": dialog})
            break
        except Exception:  # noqa: BLE001 - not open yet
            time.sleep(0.3)
    else:
        raise SessionError("the viewer's Preferences dialog did not open")
    try:
        time.sleep(1.0)
        uia.call("select", {"window": dialog, "selector": {"by": "path", "name": "Realtime Renderer", "control_type": "TreeItem"}})
        time.sleep(1.0)
        state = uia.call("state", {"window": dialog, "selector": {
            "by": "path", "name": "Show model bounding boxes", "control_type": "CheckBox"}})["controls"][0]
        return state.get("toggle") == 1  # UIA ToggleState: 0 off, 1 on
    finally:
        uia.call("invoke", {"window": dialog, "selector": {"by": "path", "name": "Cancel", "control_type": "Button"}})
        time.sleep(0.8)


def set_3d_model_bounding_boxes(session, enabled: bool | None = None) -> dict[str, Any]:
    """The 3D viewer Appearance panel's "Model Bounding Boxes" toggle: opens the viewer from the board editor if
    it is not open, sets the toggle to ``enabled`` (None only reads it) and returns the state before and after,
    each read from the viewer's Preferences dialog. Board GUI session."""
    from kicad_mcp.ipc import hotkeys

    if session.kind != "pcb":
        raise SessionError("the 3D viewer belongs to a board session")
    try:
        frame = hotkeys.canvas_of(session, "3D Viewer")[0]
    except RuntimeError:
        api.call(session, "RunAction", {"action": "common.Control.show3DViewer"})
        frame = _viewer_frame(session)
        time.sleep(5.0)
    uia = session.attach_uia()
    before = _bbox_preference(session, uia, frame)
    if enabled is not None and enabled != before:
        panel = next(h for h, cls, _a, _v in hotkeys._children(frame)
                     if cls == "wxWindowNR" and hotkeys.user32.GetDlgCtrlID(h) == BBOX_CONTROL_ID)
        toggle = hotkeys._children(panel)[0][0]  # the toggle's bitmap, which takes the click
        point = (6 << 16) | 6
        hotkeys.user32.PostMessageW(toggle, WM_LBUTTONDOWN, 1, point)
        time.sleep(0.1)
        hotkeys.user32.PostMessageW(toggle, WM_LBUTTONUP, 0, point)
        time.sleep(2.0)
    after = _bbox_preference(session, uia, frame)
    return {"before": before, "after": after, "changed": before != after}


# --- MCP registration ------------------------------------------------------------------------------

def register_function_fallback_tools(mcp: FastMCP) -> None:
    from kicad_mcp.tools import ipc_tools

    def run(session_id: str, fn, *args, **kwargs) -> dict[str, Any]:
        try:
            return {"status": "ok", **fn(ipc_tools._get(session_id), *args, **kwargs)}
        except Exception as exc:  # noqa: BLE001 - every failure is a structured answer
            return ipc_tools._error(exc)

    @mcp.tool()
    def kicad_embedded_files(session_id: str) -> dict[str, Any]:
        """The Embedded Files panel's list for the session's document (Board/Schematic Setup > Embedded Files):
        name, type, decoded size, sha256 and KiCad's checksum of each embedded file. Boards are read from the
        editor through the API, schematics from the saved file."""
        return run(session_id, embedded_files)

    @mcp.tool()
    def kicad_extract_embedded_file(session_id: str, name: str, dest_dir: str, overwrite: bool = False
                                    ) -> dict[str, Any]:
        """The Embedded Files panel's Extract (common.Embed.extractFile, which KiCad declares but never
        binds): decode embedded file ``name`` (base64, then zstd, as KiCad stores it) and write it into
        ``dest_dir``. Returns the path, size and sha256."""
        return run(session_id, extract_embedded_file, name, dest_dir, overwrite)

    @mcp.tool()
    def kicad_remove_embedded_file(session_id: str, name: str) -> dict[str, Any]:
        """The Embedded Files panel's Remove (common.Embed.removeFile, which KiCad binds but cannot run from
        outside the panel): take embedded file ``name`` out of the document and report what the editor
        holds afterwards. A board changes in the editor (undo history is kept by KiCad as for any API
        edit); a schematic's file is rewritten and reloaded. Promote the session to keep the change."""
        return run(session_id, remove_embedded_file, name)

    @mcp.tool()
    def kicad_run_bom_generator(session_id: str, command: str, allow_execute: bool = False,
                                output_base: str | None = None) -> dict[str, Any]:
        """Run an external BOM generator on the schematic (eeschema.EditorControl.generateBOMExternal, which
        KiCad declares but never binds). ``command`` is the BOM generator dialog's command line, e.g.
        ``python "/path/to/bom_csv_grouped_by_value.py" "%I" "%O.csv"``: %I the intermediate netlist
        (written for you), %O the output path without extension (default: beside the schematic), %B its base
        name, %P the project folder. A leading ``python`` is KiCad's own interpreter. Starts a program of your
        choosing, so allow_execute=True is required. Returns the exit code, output and the files produced."""
        return run(session_id, run_bom_generator, command, output_base, allow_execute)

    @mcp.tool()
    def kicad_sync_selection(session_id: str, references: list[str] | None = None,
                             pads: list[dict[str, str]] | None = None,
                             mode: Literal["SSM_ITEMS_ONLY", "SSM_ITEMS_AND_NETS"] = "SSM_ITEMS_ONLY",
                             focus_reference: str | None = None) -> dict[str, Any]:
        """Make the editor's selection match a list, as the other editor does when the user selects there
        (eeschema.InteractiveSelection.SyncSelection): ``references`` are footprints (board) or symbols
        (schematic), ``pads`` are {"reference","number"}. Returns KiCad's status and the selection read back."""
        return run(session_id, sync_selection, references, pads, mode, focus_reference)

    @mcp.tool()
    def kicad_board_sheets(session_id: str) -> dict[str, Any]:
        """The schematic sheets the board's footprints belong to: uuid path, sheet name, file and footprints."""
        try:
            return {"status": "ok", "sheets": board_sheets(ipc_tools._get(session_id))}
        except Exception as exc:  # noqa: BLE001
            return ipc_tools._error(exc)

    @mcp.tool()
    def kicad_select_items_on_sheet(session_id: str, sheet: str, include_connections: bool = True
                                    ) -> dict[str, Any]:
        """Select every footprint of a schematic sheet (pcbnew.InteractiveSelection.SelectOnSheet, which KiCad
        binds but never posts). ``sheet``: the sheet's uuid path or name (see kicad_board_sheets). The previous
        selection is replaced. include_connections (the default, as KiCad's own action) also selects the
        tracks/vias of nets wholly inside the sheet's footprints; False selects footprints only. Board GUI session."""
        return run(session_id, select_items_on_sheet, sheet, include_connections)

    @mcp.tool()
    def kicad_recover_autosave(session_id: str, keep: Literal["restore", "keep_both"] = "restore",
                              backup: bool = False) -> dict[str, Any]:
        """Recover the document from the autosave file KiCad left beside it (pcbnew.EditorControl.rescueAutosave,
        which KiCad declares but never defines): the recovery dialog's Restore Auto-saved (the document is
        replaced and the autosave deleted, as KiCad does; backup=True, our addition, keeps the previous file as
        <name>.before-recovery) or Keep Both (written as <name>.recovered.<time>, KiCad's name). Changes nothing
        when there is no autosave newer than the document."""
        return run(session_id, recover_autosave, keep, backup)

    @mcp.tool()
    def kicad_3d_model_bounding_boxes(session_id: str, enabled: bool | None = None) -> dict[str, Any]:
        """The 3D viewer's Model Bounding Boxes toggle (3DViewer.Control.showBoundingBoxes, which KiCad declares
        but never binds). Opens the viewer from the board editor if needed, sets the toggle (omit ``enabled``
        to only read it) and returns the state before and after as the viewer's Preferences show it.
        Board GUI session."""
        return run(session_id, set_3d_model_bounding_boxes, enabled)
