"""Live proof for the library editors' tree and symbol/footprint actions, read back through UI Automation.

The symbol and footprint editors expose their library tree to UI Automation (one TreeItem per library and
symbol or footprint). Each step selects the tree row a person would click (SelectionItemPattern), presses
the action's own key (the session profile's hotkey), and compares what a person would see: the tree's
items, the symbol or footprint the editor has open (read through the API), named windows it opened, the
files on disk, menu check marks, and, for view toggles and tab switches, the editor window's pixels
against a quiet control key. Clipboard actions run inside the clipboard guard (the owner's text is
restored right after). A file dialog an action opens is completed through UI Automation (file name typed,
Save or Open invoked) so the file the action makes can be checked on disk; a confirmation is answered the
way a person would.

    python -m kicad_mcp.coverage.live_libtree --inventory inv.json --project <dir> --editor symedit --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time

from kicad_mcp.coverage import clipboard
from kicad_mcp.coverage.live_hotkeys import FRAMES, _input_for, _start
from kicad_mcp.ipc import hotkeys

TREE_ITEM = {"by": "path", "name": "*", "name_match": "any", "control_type": "TreeItem"}
SYM = "eeschema.SymbolLibraryControl."
FP = "pcbnew.ModuleEditor."

# (step, which tree row to select first: "other" = a library item that is not open, "copy" = the duplicate
# made earlier, "lib" = the library itself, None = leave the selection). Order matters: later steps use
# what earlier ones made. nextSymbol and previousSymbol crash the symbol editor (upstream: its control
# casts the frame to the library browser's class), so they are pressed in the Symbol Library Browser.
STEPS = {
    "symedit": [
        (SYM + "showElectricalTypes", None),
        (SYM + "showPinNumbers", None),
        (SYM + "toggleSyncedPinsMode", None),
        (SYM + "duplicateSymbol", "other"),
        (SYM + "copySymbol+" + SYM + "pasteSymbol", "other"),
        (SYM + "editSymbol", "other"),  # a modified symbol keeps its tab, so this one opens a second tab
        (SYM + "nextSymbolTab", None),
        (SYM + "deleteSymbol", "copy"),
        (SYM + "renameFootprint", "other"),
        (SYM + "updateSymbolFields", "derived"),
        (SYM + "exportSymbol", "other"),
        (SYM + "exportSymbolAsSVG", "other"),
        (SYM + "importSymbol", "lib"),
        (SYM + "flattenSymbol", "flat"),
        (SYM + "saveLibraryAs", "lib"),
        (SYM + "saveLibraryAsPacked", "lib"),
        (SYM + "saveLibraryAsUnpacked", "lib"),
    ],
    "symview": [
        (SYM + "showPinNumbers", None),
        (SYM + "nextSymbol", None),
        (SYM + "previousSymbol", None),
    ],
    "fpedit": [
        (FP + "footprintProperties", None),  # editing the open footprint keeps its tab...
        (FP + "editFootprint", "other"),  # ...so this one opens a second tab
        (FP + "nextFootprintTab", None),
        (FP + "prevFootprintTab", None),
        (FP + "duplicateFootprint", "other"),
        (FP + "copyFootprint+" + FP + "pasteFootprint", "other"),
        (FP + "cutFootprint+" + FP + "pasteFootprint", "copy"),
        (FP + "deleteFootprint", "copy"),
        (FP + "newFootprint", "lib"),
        (FP + "exportFootprint", "other"),
        (FP + "exportFootprintAsSVG", "other"),
        (FP + "importFootprint", "lib"),
        (FP + "CompareFpLibraryWithFile", "other"),
        (FP + "closeFootprintTab", None),
    ],
}
FRAME_OF = {"symview": "symedit"}
VIEWER_TITLE = "Library Browser"
PIXEL_ACTIONS = ("showElectricalTypes", "showPinNumbers", "toggleSyncedPinsMode", "nextSymbolTab",
                 "nextSymbol", "previousSymbol", "nextFootprintTab", "prevFootprintTab")
# The file a file dialog is given (name inside the run's output folder), per action; "import" ones open
# the file the matching export wrote.
SAVE_AS = {"exportSymbol": "exported.kicad_sym", "exportSymbolAsSVG": "exported-symbol.svg",
           "saveLibraryAs": "library-copy.kicad_sym", "saveLibraryAsPacked": "library-packed.kicad_sym",
           "saveLibraryAsUnpacked": "library-unpacked", "exportFootprint": "exported.kicad_mod",
           "exportFootprintAsSVG": "exported-footprint.svg"}
OPEN_FROM = {"importSymbol": "exportSymbol", "importFootprint": "exportFootprint"}
# A folder an action is pointed at: a copy of the project's own footprint library, made before the run.
OPEN_FOLDER = {"CompareFpLibraryWithFile": "compare.pretty"}
SETUP_SYMBOL, DERIVED, FLATTEN = "TwoUnit", "TwoUnit_D", "TwoUnit_E"  # the fixture's two-unit symbol and one derived from it
ZOOM = "common.Control.zoomFitObjects"  # fits the open symbol to the canvas so pin text is big enough to see
# Answers a dialog needs in order (its title starts with the key).
SEQUENCES = {"Import Symbols from": ("Select All", "Import")}
# Actions that work on the symbol open in the editor: that symbol is opened first (setup, not the proof).
OPEN_FIRST = {"updateSymbolFields": "TwoUnit_D"}
# Keys pressed before an action so it has somewhere to go (previous needs the list moved on first).
PRIME = {"previousSymbol": (SYM + "nextSymbol",) * 2}
# Keys that change the open symbol first (select all, mirror), so its tab stays when another opens.
DIRTY = ("common.Interactive.selectAll", "eeschema.InteractiveEdit.mirrorH")
DIRTY_BEFORE = ("editSymbol",)
CONFIRM = ("Yes", "OK", "Delete", "Continue")


def _base(name: str) -> str:
    """A tree row without the modified-marker KiCad appends once its library has unsaved changes."""
    return name[:-2] if name.endswith(" *") else name


def _open_items(session, editor: str) -> str:
    """The library item(s) the editor has open, as the API reports them (its title does not name them)."""
    from kicad_mcp.ipc import api

    try:
        docs = api.call(session, "GetOpenDocuments",
                        {"type": "DOCTYPE_SYMBOL" if editor == "symedit" else "DOCTYPE_FOOTPRINT"})["response"]
    except Exception:  # noqa: BLE001 - busy in a dialog
        return "?"
    return ", ".join(f"{d.get('lib_id', {}).get('library_nickname', '')}:{d.get('lib_id', {}).get('entry_name', '')}"
                     for d in docs.get("documents", []))


def _tree(uia, window: dict) -> list[str]:
    return [c["name"] for c in uia.call("state", {"window": window, "selector": TREE_ITEM})["controls"]]


def _files(*folders: Path) -> dict[str, tuple[int, str]]:
    """Every real file in the folders (editor lock and autosave files left out): size and content hash."""
    found = {}
    for folder in folders:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and not path.name.startswith(("_autosave", "~")) and not path.name.endswith((".lck", ".lock")):
                found[str(path)] = (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return found


def _selected(uia, window: dict) -> list[str]:
    """The list rows the Symbol Library Browser shows as selected."""
    rows = uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                               "control_type": "ListItem"}})["controls"]
    return [c["name"] for c in rows if c.get("selected")]


def _press(canvas: int, vk: int, focus_click: bool) -> None:
    """Press a key on the canvas; the Symbol Library Browser takes only every other key unless its canvas
    was clicked first, so there a click (a person's own way of focusing it) comes before the key."""
    if focus_click:
        from kicad_mcp.coverage.live_tools import click

        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(canvas, hotkeys.ctypes.byref(rect))
        click(canvas, rect.right // 2, rect.bottom // 2)
        time.sleep(0.4)
    hotkeys.press(canvas, vk)


def _select(uia, window: dict, name: str) -> None:
    uia.call("select", {"window": window, "selector": {"by": "path", "name": name, "control_type": "TreeItem"}})
    time.sleep(0.5)


def _control(name: str, control_type: str) -> dict:
    return {"by": "path", "name": name, "control_type": control_type}


FILE_NAME = {"by": "path", "name": "File name:", "control_type": "Edit"}  # the common file dialog's name box
DEFAULT_BUTTON = {"by": "automation_id", "automation_id": "1", "control_type": "Button"}  # its Save or Open button


def _describe(uia, title: str) -> str:
    """What an unhandled dialog shows (its texts and buttons), so the evidence says what it was."""
    def walk(node, found):
        if node["control_type"] in ("Text", "Button", "Edit") and node["name"] and not node["name"].startswith(("Close", "Help")):
            found.append(f"{node['control_type']}:{node['name']}")
        for child in node.get("children", []):
            walk(child, found)
        return found

    try:
        tree = uia.call("dump-tree", {"window": {"name": title, "match": "exact"}, "max_depth": 6})
        return "; ".join(dict.fromkeys(i for w in tree["windows"] for i in walk(w, [])))[:400]
    except Exception as exc:  # noqa: BLE001
        return f"unreadable ({type(exc).__name__})"


def _first(uia, window: dict, selectors: list[dict]) -> dict | None:
    """The first selector that matches exactly one control."""
    for selector in selectors:
        try:
            if len(uia.call("state", {"window": window, "selector": selector})["controls"]) == 1:
                return selector
        except Exception:  # noqa: BLE001
            continue
    return None


def _complete(uia, hwnd: int, title: str, action: str, out: Path, notes: list[str]) -> str:
    """Finish a dialog the action opened, as a person would: a file or folder dialog gets the name and
    Save, Open or Select Folder, a confirmation gets its affirmative button. Returns what was done
    ('' when nothing was)."""
    window = {"name": title, "match": "exact"}
    leaf = action.rsplit(".", 1)[-1]
    name_box = _first(uia, window, [FILE_NAME, _control("Folder:", "Edit")])
    if name_box is None:
        notes.append(f"'{title}': {_describe(uia, title)}")
        for key, labels in SEQUENCES.items():
            if title.startswith(key):
                for label in labels:
                    uia.call("invoke", {"window": window, "selector": _control(label, "Button")})
                    time.sleep(0.6)
                return f"answered '{title}' with {', '.join(labels)}"
        for label in CONFIRM:
            try:
                uia.call("invoke", {"window": window, "selector": _control(label, "Button")})
                return f"answered '{title}' with {label}"
            except Exception:  # noqa: BLE001
                continue
        return ""
    if leaf in SAVE_AS:
        target = out / SAVE_AS[leaf]
        if name_box["name"] == "Folder:":
            target.mkdir(exist_ok=True)
    elif leaf in OPEN_FOLDER:
        target = out / OPEN_FOLDER[leaf]
    elif leaf in OPEN_FROM and (out / SAVE_AS[OPEN_FROM[leaf]]).exists():
        target = out / SAVE_AS[OPEN_FROM[leaf]]
    else:
        return ""
    button = _first(uia, window, [DEFAULT_BUTTON, _control("Save", "Button"), _control("Select Folder", "Button")])
    uia.call("set-value", {"window": window, "selector": name_box, "value": str(target)})
    if button is None:  # an Open dialog does not expose its default button: press OK as its own command
        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_COMMAND, 1, 0)
    else:
        uia.call("invoke", {"window": window, "selector": button})
    return f"completed '{title}' with {target.name}"


def _menu_checks(frame: int) -> dict[str, bool]:
    return {" > ".join(i["path"]): hotkeys.checked(i) for i in hotkeys.menu_items(frame)}


def _open_viewer(session, frame: int) -> tuple[int, int]:
    """The Symbol Library Browser, opened from the symbol editor's View menu."""
    item = next((i for i in hotkeys.menu_items(frame) if i["label"] == "symbol library browser"), None)
    if item is None:
        raise RuntimeError("no 'Symbol Library Browser' menu item in the symbol editor")
    hotkeys.user32.PostMessageW(frame, hotkeys.WM_COMMAND, item["id"], 0)
    time.sleep(4)
    viewer, canvas = hotkeys.canvas_of(session, VIEWER_TITLE)
    hotkeys.settled_capture(viewer, timeout_s=15.0)
    return viewer, canvas


@cleanup_after_call
def sweep(inventory: dict, project: Path, editor: str, only: str | None = None) -> dict:
    host = FRAME_OF.get(editor, editor)
    document = _input_for(host, project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-libtree-")))
    out = Path(scoped_mkdtemp(prefix="kicad-ai-tools-libtree-out-"))
    all_actions = [a["name"] for a in inventory["actions"]]
    names = set(all_actions)
    steps = [(s, sel) for s, sel in STEPS[editor] if all(a in names for a in s.split("+")) and (not only or any(o in s for o in only.split(",")))]
    batch = list(dict.fromkeys([*(p for s, _ in steps for p in PRIME.get(s.rsplit(".", 1)[-1], ())),
                                *(a for s, _ in steps for a in s.split("+"))]))
    if editor == "symedit" and ZOOM in names:
        batch.append(ZOOM)
    if editor == "symedit" and all(a in names for a in DIRTY):
        batch.extend(DIRTY)
    window = {"name": FRAMES[host][1], "match": "contains"}
    rows: list[dict] = []
    viewer_window = {"name": VIEWER_TITLE, "match": "contains"}
    session, assigned, frame, canvas = _start(host, document, all_actions, batch)
    try:
        uia = session.attach_uia()
        work = Path(session.document).parent
        if editor == "symview":
            frame, canvas = _open_viewer(session, frame)
        start_tree = [] if editor == "symview" else _tree(uia, window)
        library = _base(start_tree[0]) if start_tree else ""
        items = [_base(i) for i in start_tree[1:]]
        made: list[str] = []
        if editor == "fpedit":
            import shutil

            shutil.copytree(next(work.glob("*.pretty")), out / OPEN_FOLDER["CompareFpLibraryWithFile"])
        if editor == "symedit" and SETUP_SYMBOL in items:  # open the two-unit symbol (synced pins need units)
            from kicad_mcp.ipc import api

            lib = {"library_nickname": library, "entry_name": SETUP_SYMBOL}
            api.call(session, "OpenLibraryItem", {"type": "DOCTYPE_SYMBOL", "identifier": lib})
            time.sleep(2)
            if ZOOM in assigned:
                hotkeys.press(canvas, assigned[ZOOM][1])
            hotkeys.settled_capture(frame, timeout_s=10.0)

        def pick(base: str) -> str | None:
            return next((n for n in _tree(uia, window) if _base(n) == base), None)

        for step, selection in steps:
            parts = step.split("+")
            row = {"action": parts[-1], "step": step, "outcome": "no_effect", "detail": ""}
            try:
                open_name = _open_items(session, host)
                wanted = None
                if selection == "other":
                    wanted = next((i for i in items if i not in open_name), items[0] if items else None)
                elif selection == "derived":
                    wanted = DERIVED
                elif selection == "flat":
                    wanted = FLATTEN
                elif selection == "copy" and made:
                    wanted = made[-1]
                elif selection == "lib":
                    wanted = library
                row_name = pick(wanted) if wanted else None
                if row_name:
                    _select(uia, window, row_name)
                first = OPEN_FIRST.get(parts[-1].rsplit(".", 1)[-1])
                if first:
                    from kicad_mcp.ipc import api

                    api.call(session, "OpenLibraryItem", {"type": "DOCTYPE_SYMBOL", "identifier": {
                        "library_nickname": library, "entry_name": first}})
                    time.sleep(2)
                if parts[-1].rsplit(".", 1)[-1] in DIRTY_BEFORE and DIRTY[0] in assigned:
                    for key in DIRTY:
                        hotkeys.press(canvas, assigned[key][1])
                        time.sleep(1.0)
                    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                    time.sleep(0.5)
                if editor == "symview":  # the browser repaints into a new canvas once a symbol loads
                    frame, canvas = hotkeys.canvas_of(session, VIEWER_TITLE)
                for prime in PRIME.get(parts[-1].rsplit(".", 1)[-1], ()):
                    _press(canvas, assigned[prime][1], editor == "symview")
                    time.sleep(1.0)
                view_before = _selected(uia, viewer_window) if editor == "symview" else []
                before_tree = [] if editor == "symview" else _tree(uia, window)
                before_open = _open_items(session, host)
                before_files, windows = _files(work, out), hotkeys._visible(session)
                before_checks, pixels = _menu_checks(frame), hotkeys.settled_capture(frame)
                clip = None
                if len(parts) == 2:  # copy or cut, then paste, inside the clipboard guard
                    with clipboard.preserved() as obs:
                        hotkeys.press(canvas, assigned[parts[0]][1])
                        obs.wait_for_change(2.0)
                        obs.capture()
                        copied = obs.new_content  # the copy's (or cut's) own effect, before any paste
                        hotkeys.press(canvas, assigned[parts[1]][1])
                        time.sleep(0.8)
                        obs.capture()
                    clip = {"changed": obs.changed, "restored": obs.restored, "copied_before_paste": copied}
                else:
                    _press(canvas, assigned[parts[0]][1], editor == "symview")
                time.sleep(2.0)
                opened, done, dialogs = [], [], []
                for _ in range(3):  # a dialog may lead to another (a file name, then a confirmation)
                    fresh = {h: t for h, t in hotkeys._visible(session).items() if h not in windows}
                    fresh = {h: t for h, t in fresh.items() if not t or t not in opened}
                    if not fresh:
                        break
                    for hwnd, title in fresh.items():
                        opened.append(title or "(untitled)")
                        if title:
                            try:
                                note = _complete(uia, hwnd, title, parts[-1], out, dialogs)
                            except Exception as exc:  # noqa: BLE001
                                note = f"could not complete '{title}': {type(exc).__name__}: {str(exc)[:200]}"
                            if note:
                                done.append(note)
                    time.sleep(2.0)
                after_pixels = hotkeys.capture(frame)
                view_after = _selected(uia, viewer_window) if editor == "symview" else []
                for hwnd in [h for h in hotkeys._visible(session) if h not in windows]:
                    hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
                time.sleep(0.8)
                after_tree = [] if editor == "symview" else _tree(uia, window)
                after_open = _open_items(session, host)
                effects = []
                before_bases, after_bases = {_base(b) for b in before_tree}, {_base(a) for a in after_tree}
                added = [n for n in after_tree if _base(n) not in before_bases]
                removed = [n for n in before_tree if _base(n) not in after_bases]
                if added:
                    effects.append(f"tree gained {added}")
                    made.extend(_base(n) for n in added)
                if removed:
                    effects.append(f"tree lost {removed}")
                if not added and not removed and after_tree != before_tree:
                    effects.append(f"tree rows changed {sorted(set(after_tree) ^ set(before_tree))}")
                if opened:
                    effects.append(f"opened {opened}")
                if dialogs:
                    row["dialogs"] = dialogs
                effects.extend(done)
                if "?" not in (before_open, after_open) and before_open != after_open:
                    effects.append(f"editor now has '{after_open}' open (was '{before_open}')")
                after_files = _files(work, out)
                new = sorted(Path(f).name for f in after_files if f not in before_files)
                changed = sorted(Path(f).name for f in after_files if f in before_files and after_files[f] != before_files[f])
                if new:
                    effects.append(f"new files {new}")
                if changed:
                    effects.append(f"files changed on disk {changed}")
                after_checks = _menu_checks(frame)
                flipped = [k for k in after_checks if k in before_checks and after_checks[k] != before_checks[k]]
                if flipped:
                    effects.append(f"menu check mark flipped: {flipped}")
                if view_before != view_after:
                    effects.append(f"browser selection moved from {view_before} to {view_after}")
                if parts[-1].rsplit(".", 1)[-1] in PIXEL_ACTIONS:
                    differs = pixels != after_pixels
                    quiet = differs and hotkeys.window_is_quiet(session, canvas, frame)
                    row["pixels"] = {"changed": differs, "control_quiet": quiet}
                    if differs and quiet:
                        effects.append("editor window changed (control key quiet)")
                if clip is not None:
                    row["clipboard"] = clip
                    if not clip["restored"]:
                        effects = []
                        row["detail"] = "clipboard not restored: not counted"
                if effects:
                    row.update(outcome="ok", detail="; ".join(effects))
                if len(parts) == 2 and clip and clip["copied_before_paste"] and clip["restored"]:
                    # credited for its own effect: new clipboard content right after it, before the paste
                    rows.append({"action": parts[0], "step": step, "outcome": "ok",
                                 "detail": "clipboard written by the action itself (read before the paste); "
                                           f"owner text restored; the paste then produced: {row['detail'] or 'nothing'}"})
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            if not session.healthy():
                rows[-1]["detail"] += "; editor restarted after"
                break
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": f"libtree-uia-{editor}", "passed": passed,
            "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--editor", choices=sorted(STEPS), required=True)
    parser.add_argument("--only", help="run only the steps whose text contains this")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.editor, args.only)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["step"], row["detail"][:200], row.get("pixels", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
