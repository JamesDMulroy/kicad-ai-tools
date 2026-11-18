"""Controlled live proofs for the library-tree and file actions of the symbol and footprint editors.

Every step runs its action AND a control that differs only in the action (the key bound to nothing, a
dialog cancelled, a file with an unsupported extension), and credits the action only when the observed
state moved with the action and stayed put with the control. What is read back:

* the library tree through UI Automation (rows, order, each library's expanded state, the ``*`` marker);
* the library files and library table on disk (hash and content);
* the control that takes keyboard focus (an in-process ``SetFocus`` hook: the key presses on a private
  desktop have no real focus, so the editor's own request for focus is what is observed);
* dialogs the action opens (named windows, completed through UI Automation like a person would);
* files dropped on the frame (a ``WM_DROPFILES`` message built inside the session's own process).

    python -m kicad_mcp.coverage.live_libctl --inventory inv.json --project <dir> --phase symedit --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import contextlib
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time

from kicad_mcp.coverage.live_hotkeys import FRAMES, _input_for, _start
from kicad_mcp.coverage.live_libtree import TREE_ITEM, _complete, _control, _select
from kicad_mcp.coverage.live_libtree import _base as _star_free
from kicad_mcp.ipc import hotkeys

COMMON = "common.Control."
SYM = "eeschema.SymbolLibraryControl."
FP = "pcbnew.ModuleEditor."
CONTROL_VK = hotkeys.CONTROL_KEY[1]  # a key bound to nothing
VK_RIGHT = 0x27

SYMBOL_HEADER = """(kicad_symbol_lib
\t(version 20251024)
\t(generator "kicad_symbol_editor")
\t(generator_version "10.0")
"""


def _symbol(name: str) -> str:
    fields = "".join(f'\t\t(property "{key}" "{value}"\n\t\t\t(at 0 0 0)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n'
                     f'\t\t\t\t)\n\t\t\t\t(hide yes)\n\t\t\t)\n\t\t)\n'
                     for key, value in (("Reference", "U"), ("Value", name), ("Footprint", ""), ("Datasheet", ""),
                                        ("Description", "")))
    return (f'\t(symbol "{name}"\n\t\t(exclude_from_sim no)\n\t\t(in_bom yes)\n\t\t(on_board yes)\n{fields}\t)\n')


def library_text(*names: str) -> str:
    return SYMBOL_HEADER + "".join(_symbol(n) for n in names) + ")\n"


def make_fixture(source: Path) -> Path:
    """A copy of the project with a second symbol library (``alpha``), so pinning has an order to change."""
    root = Path(scoped_mkdtemp(prefix="kicad-ai-tools-libctl-"))
    project = root / "proj"
    shutil.copytree(source, project)
    (project / "alpha.kicad_sym").write_text(library_text("AlphaOne", "AlphaTwo"), encoding="utf-8")
    table = project / "sym-lib-table"
    text = table.read_text(encoding="utf-8")
    text = text.replace("(sym_lib_table\n\t(version 7)\n",
                        '(sym_lib_table\n\t(version 7)\n\t(lib (name "alpha")(type "KiCad")(uri "${KIPRJMOD}/alpha.kicad_sym")(options "")(descr "second library"))\n', 1)
    table.write_text(text, encoding="utf-8")
    return project


def _base(name: str) -> str:
    """A tree row without the modified marker and the pin star."""
    return _star_free(name).removeprefix("☆ ")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


@temporary_owner(cleanup='close', allocating=('__init__',))
class Run:
    """One editor session with the helpers every step uses."""

    def __init__(self, inventory: dict, project: Path, editor: str, batch: list[str]):
        self.editor = editor
        self.names = {a["name"] for a in inventory["actions"]}
        self.out = Path(scoped_mkdtemp(prefix="kicad-ai-tools-libctl-out-"))
        document = _input_for(editor, project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-libctl-doc-")))
        self.session, self.assigned, self.frame, self.canvas = _start(
            editor, document, [a["name"] for a in inventory["actions"]], batch)
        self.window = {"name": FRAMES[editor][1], "match": "contains"}
        self.uia = self.session.attach_uia()
        self.work = Path(self.session.document).parent
        self.notes: list[str] = []

    # -- reading
    def tree(self) -> list[str]:
        return [c["name"] for c in self.tree_state()]

    def tree_state(self) -> list[dict]:
        for attempt in range(8):  # the editor answers late while a dialog is closing
            try:
                return self.uia.call("state", {"window": self.window, "selector": TREE_ITEM})["controls"]
            except Exception:  # noqa: BLE001
                if attempt == 7:
                    raise
                time.sleep(1.5)

    def windows(self) -> dict[int, str]:
        return hotkeys._visible(self.session)

    def tree_window(self) -> int:
        trees = [c for c in hotkeys._children(self.frame) if c[3] and ("DataView" in c[1])]
        return max(trees, key=lambda c: c[2])[0]

    def key(self, action: str) -> int:
        return self.assigned[action][1]

    # -- driving
    def press_on(self, hwnd: int, action: str | None) -> None:
        hotkeys.press(hwnd, self.key(action) if action else CONTROL_VK)
        time.sleep(1.2)

    def select(self, name: str) -> None:
        for attempt in range(4):
            try:
                _select(self.uia, self.window, name)
                return
            except Exception as exc:  # noqa: BLE001
                self.notes.append(f"select '{name}' attempt {attempt}: {type(exc).__name__}; rows {self.tree()[:6]}")
                if attempt == 3:
                    raise
                time.sleep(2.0)

    def row(self, base: str) -> str:
        rows = self.tree()
        found = next((n for n in rows if _base(n) == base), None)
        if found is None:
            raise RuntimeError(f"no tree row '{base}' among {rows}")
        return found

    def settle(self, before: dict[int, str], answers: dict[str, object] | None = None, accept: bool = True,
               wait_s: float = 20.0) -> tuple[list[str], list[str]]:
        """Finish the dialogs the step opened (file name, OK, a confirmation); returns titles and what was done.
        ``answers`` maps a title to a callable(title) -> note for dialogs that need a specific answer."""
        seen: list[str] = []
        done: list[str] = []
        deadline = time.monotonic() + wait_s  # a dialog may take a few seconds to show
        while time.monotonic() < deadline and not any(h not in before and t for h, t in self.windows().items()):
            time.sleep(0.5)
        for _ in range(6):
            fresh: dict[int, str] = {}
            for _poll in range(10):  # a follow-up dialog (the symbol chooser, a confirmation) shows a moment later
                fresh = {h: t for h, t in self.windows().items() if h not in before and t and t not in seen}
                if fresh:
                    break
                time.sleep(0.5)
            if not fresh:
                break
            for hwnd, title in fresh.items():
                seen.append(title)
                handler = next((v for k, v in sorted((answers or {}).items(), key=lambda kv: -len(kv[0])) if title.startswith(k)), None)
                if handler is not None:
                    try:
                        done.append(handler(title))
                    except Exception as exc:  # noqa: BLE001
                        done.append(f"'{title}': {type(exc).__name__}: {str(exc)[:160]}")
                    time.sleep(1.5)
                    continue
                if not accept:
                    try:
                        done.append(_dialog_button(self, title, "Cancel"))
                    except Exception:  # noqa: BLE001
                        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_COMMAND, 2, 0)  # IDCANCEL
                        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
                        done.append(f"'{title}': closed")
                    time.sleep(1.5)
                    continue
                try:
                    note = _complete(self.uia, hwnd, title, "x", self.out, self.notes)
                except Exception as exc:  # noqa: BLE001
                    note = f"could not complete '{title}': {type(exc).__name__}"
                done.append(note or f"'{title}': left")
                time.sleep(1.5)
        return seen, done

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.session.close(discard=True)


def _row(action: str, outcome: str, detail: str, **extra) -> dict:
    return {"action": action, "outcome": outcome, "detail": detail, **extra}


def _dialog_button(run: Run, title: str, label: str) -> str:
    run.uia.call("invoke", {"window": {"name": title, "match": "exact"}, "selector": _control(label, "Button")})
    return f"'{title}': {label}"


def _dialog_text(run: Run, title: str, value: str, label: str = "OK") -> str:
    """Type into a dialog's only text box and press a button."""
    window = {"name": title, "match": "exact"}
    run.uia.call("set-value", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any", "control_type": "Edit"}, "value": value})
    time.sleep(0.4)
    return _dialog_button(run, title, label)


# ------------------------------------------------------------------------------------------------ steps

def step_search(run: Run, focus) -> dict:
    """libraryTreeSearch: the editor asks for keyboard focus on the library tree's search box."""
    action = COMMON + "libraryTreeSearch"
    search = next(c for c in hotkeys._children(run.frame) if c[1] == "Edit" and c[3])[0]
    tree = run.tree_window()
    focus.focus(tree)  # a person is in the tree, not the search box
    run.press_on(run.canvas, None)
    control_calls = focus.set_calls(0.5)
    focus.focus(tree)
    run.press_on(run.canvas, action)
    calls = focus.set_calls(1.0)
    ok = (search in calls) and (search not in control_calls)
    return _row(action, "ok" if ok else "no_effect",
                f"the editor called SetFocus on the library tree's search box (window {search:#x}, class Edit, "
                f"child of the tree panel) after the key; the control key made calls {[hex(c) for c in control_calls]}"
                if ok else f"calls after the key {[hex(c) for c in calls]}, control {[hex(c) for c in control_calls]}, search box {search:#x}")


def popup(run: Run, hwnd: int, label: str | None, wait_s: float = 8.0, right: bool = False) -> list[str]:
    """Left-click a control that opens a popup menu, then choose the entry called ``label`` with the keys a
    person would use (Down to it, Return) or, with ``label`` None, close the menu with Escape. Returns the
    menu's entries."""
    import ctypes

    user32 = hotkeys.user32
    rect = hotkeys._Rect()
    user32.GetClientRect(hwnd, ctypes.byref(rect))
    point = ((rect.bottom // 2) << 16) | (rect.right // 2)
    down, up, held = (0x0204, 0x0205, 2) if right else (0x0201, 0x0202, 1)  # WM_RBUTTON* or WM_LBUTTON*
    user32.PostMessageW(hwnd, 0x0200, 0, point)
    time.sleep(0.1)
    user32.PostMessageW(hwnd, down, held, point)
    time.sleep(0.12)
    user32.PostMessageW(hwnd, up, 0, point)
    deadline = time.monotonic() + wait_s
    menus: list[int] = []
    while time.monotonic() < deadline and not menus:
        menus = [w["hwnd"] for w in run.session.kicad.desktop.windows()
                 if w["pid"] == run.session.pid and w["visible"] and w["class"] == "#32768"]
        time.sleep(0.25)
    if not menus:
        return []
    from kicad_mcp.coverage.live_designblocks import Run as BlockRun

    entries = BlockRun.popup_entries(menus[0])
    if label is None:
        hotkeys.press(menus[0], hotkeys.VK_ESCAPE)
    else:
        selectable = [e for e in entries if e]
        index = next((i for i, e in enumerate(selectable) if e.replace("&", "").split("	")[0].strip().lower().startswith(label.lower())), None)
        if index is None:
            raise RuntimeError(f"no '{label}' among the menu entries {entries}")
        for _ in range(index + 1):
            hotkeys.press(menus[0], 0x28)
            time.sleep(0.3)
        hotkeys.press(menus[0], 0x0D)
    time.sleep(1.2)
    return entries


def tree_buttons(run: Run) -> dict[str, int]:
    """The library tree's search-row controls: the sort/settings button is the one right of the box."""
    import ctypes

    def rect(h):
        r = hotkeys._Rect()
        hotkeys.user32.GetWindowRect(h, ctypes.byref(r))
        return r

    edit = next(c for c in hotkeys._children(run.frame) if c[1] == "Edit" and c[3])[0]
    er = rect(edit)
    near = [(h, rect(h)) for h, cls, _a, vis in hotkeys._children(run.frame)
            if vis and cls == "wxWindowNR" and abs(rect(h).top - er.top) < 40 and rect(h).left > er.right and 10 < rect(h).bottom - rect(h).top < 80]
    gear = max(near, key=lambda x: x[1].left)[0]
    return {"search": edit, "gear": gear}


def step_collapse(run: Run) -> dict:
    """collapseAll: the entry the tree's settings menu carries for the action, chosen like a person does,
    collapses every library row (the tree's own rows are read back through UI Automation)."""
    action = COMMON + "collapseAll"
    gear = tree_buttons(run)["gear"]
    tree = run.tree_window()
    run.select("fixture-lib")
    hotkeys.press(tree, VK_RIGHT)  # expand the second library too, so there is something to collapse
    time.sleep(1.0)
    before = run.tree()
    popup(run, gear, None)  # control: the menu opened and closed without a choice
    control = run.tree()
    entries = popup(run, gear, "Collapse All")
    after = run.tree()
    ok = bool(entries) and control == before and len(after) < len(before) and len(after) == 2
    return _row(action, "ok" if ok else "no_effect",
                f"tree rows {len(before)} -> {len(after)} after choosing 'Collapse All' in the tree's settings menu "
                f"(entries {entries}); the same menu closed without a choice left {len(control)} rows; rows left {after}",
                before=before[:6], after=after)


def step_unpin(run: Run) -> dict:
    """unpinLibrary: a pinned library goes back to its alphabetical place (pinned libraries sit on top)."""
    action = COMMON + "unpinLibrary"
    pin = COMMON + "pinLibrary"
    order0 = [_base(n) for n in run.tree()]
    stars0 = [n for n in run.tree() if n.startswith("☆")]
    last = order0[-1]
    run.select(run.row(last))
    run.press_on(run.canvas, action)  # control: nothing is pinned yet
    order_control = [_base(n) for n in run.tree()]
    run.select(run.row(last))
    run.press_on(run.canvas, pin)
    order_pinned = [_base(n) for n in run.tree()]
    stars = [n for n in run.tree() if n.startswith("☆")]
    run.select(run.row(last))
    run.press_on(run.canvas, action)
    order_after = [_base(n) for n in run.tree()]
    ok = (order_control == order0 and order_pinned[0] == last and order_pinned != order0 and order_after == order0 and not stars0 and stars == ["☆ " + last])
    return _row(action, "ok" if ok else "no_effect",
                f"library order {order0} unchanged by unpin on an unpinned library; pin moved '{last}' to the top "
                f"{order_pinned} with a pin star on its row ({stars}); unpin put it back {order_after}" if ok
                else f"order {order0}; control {order_control}; pinned {order_pinned}; after unpin {order_after}")


def file_dialog(run: Run, title: str, path: Path) -> str:
    """Complete a file dialog the way a person does: the file name typed in, the default button pressed."""
    from kicad_mcp.coverage.live_libtree import DEFAULT_BUTTON, FILE_NAME

    window = {"name": title, "match": "exact"}
    run.uia.call("set-value", {"window": window, "selector": FILE_NAME, "value": str(path)})
    hwnd = next(h for h, t in run.windows().items() if t == title)
    try:
        run.uia.call("invoke", {"window": window, "selector": DEFAULT_BUTTON})
    except Exception:  # noqa: BLE001 - an Open dialog does not expose its default button: press OK as its own command
        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_COMMAND, 1, 0)
    return f"'{title}': chose {path.name}"


def library_files(run: Run) -> dict[str, str]:
    return {p.name: _sha(p) for p in sorted(run.work.glob("*.kicad_sym"))}


def expand_library(run: Run, name: str) -> None:
    run.select(run.row(name))
    hotkeys.press(run.tree_window(), VK_RIGHT)
    time.sleep(1.0)


def step_rename(run: Run) -> dict:
    """renameFootprint (the symbol editor's Rename Symbol): the dialog opens; cancelled it changes nothing,
    answered with a new name the symbol's tree row carries that name (and the old one is gone)."""
    action = SYM + "renameFootprint"
    expand_library(run, "fixture-lib")
    old, new = "LED", "LED_RENAMED"
    before = [_base(n) for n in run.tree()]
    run.select(run.row(old))
    windows = run.windows()
    run.press_on(run.canvas, action)
    seen, done = run.settle(windows, {"Change Symbol Name": lambda t: _dialog_button(run, t, "Cancel")})
    control = [_base(n) for n in run.tree()]
    run.select(run.row(old))
    windows = run.windows()
    run.press_on(run.canvas, action)
    seen2, done2 = run.settle(windows, {"Change Symbol Name": lambda t: _dialog_text(run, t, new)})
    after = [_base(n) for n in run.tree()]
    ok = (bool(seen) and bool(seen2) and control == before and old in before and old not in after and new in after
          and len(after) == len(before))
    return _row(action, "ok" if ok else "no_effect",
                f"'Change Symbol Name' opened ({done}); cancelled, the tree kept '{old}' (rows unchanged); answered with "
                f"'{new}' ({done2}), row '{old}' gone and '{new}' present, {len(after)} rows" if ok
                else f"dialogs {seen}/{seen2} {done}/{done2}; before {before}; control {control}; after {after}")


def import_dialog(run: Run, title: str) -> str:
    """The symbol chooser of an import: every symbol selected (when it offers that) and Import pressed."""
    from kicad_mcp.coverage.live_libtree import _describe

    notes = [f"dialog shows: {_describe(run.uia, title)}"]
    try:
        notes.append(_dialog_button(run, title, "Select All"))
    except Exception:  # noqa: BLE001
        notes.append("no Select All button")
    time.sleep(0.5)
    try:
        notes.append(_dialog_button(run, title, "Import"))
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Import failed: {exc}")
    return "; ".join(notes)


def step_import(run: Run) -> dict:
    """importSymbol: choosing a library file in the dialogs adds that file's symbol to the library tree
    (a cancelled file dialog adds nothing)."""
    action = SYM + "importSymbol"
    source = run.out / "import-me.kicad_sym"
    source.write_text(library_text("ImportedProbe"), encoding="utf-8")
    lib = "fixture-lib"
    before = [_base(n) for n in run.tree()]
    run.select(run.row(lib))
    windows = run.windows()
    run.press_on(run.canvas, action)
    seen, done = run.settle(windows, accept=False)  # control: the file dialog is closed without a file
    control = [_base(n) for n in run.tree()]
    run.select(run.row(lib))
    windows = run.windows()
    run.press_on(run.canvas, action)
    seen2, done2 = run.settle(windows, {
        "Import Symbol": lambda t: file_dialog(run, t, source),
        "Import Symbols from": lambda t: import_dialog(run, t)})
    after = [_base(n) for n in run.tree()]
    ok = bool(seen) and control == before and "ImportedProbe" not in before and "ImportedProbe" in after
    return _row(action, "ok" if ok else "no_effect",
                f"file dialog {seen} closed without a file: tree unchanged; chosen {source.name} ({done2}): the tree gained "
                f"'ImportedProbe' ({len(before)} -> {len(after)} rows)" if ok
                else f"dialogs {seen}/{seen2} {done2}; before {len(before)}; control {len(control)}; after {after[-8:]}")


def step_save_all(run: Run) -> dict:
    """saveAll: the library files change on disk only when the action runs (they hold the renamed and the
    imported symbol afterwards), and the tree's modified marker goes."""
    action = COMMON + "saveAll"
    files0 = library_files(run)
    marks0 = [n for n in run.tree() if n.endswith(" *")]
    run.press_on(run.canvas, None)
    time.sleep(1.0)
    control = library_files(run)
    windows = run.windows()
    run.press_on(run.canvas, action)
    seen, done = run.settle(windows)
    time.sleep(2.0)
    files1 = library_files(run)
    marks1 = [n for n in run.tree() if n.endswith(" *")]
    text = (run.work / "fixture-lib.kicad_sym").read_text(encoding="utf-8")
    changed = sorted(n for n in files1 if files1[n] != files0.get(n))
    ok = (control == files0 and bool(marks0) and not marks1 and "fixture-lib.kicad_sym" in changed
          and '"LED_RENAMED"' in text and '"ImportedProbe"' in text)
    return _row(action, "ok" if ok else "no_effect",
                f"library files unchanged by the control key; after Save All {changed} changed on disk and fixture-lib.kicad_sym "
                f"holds the renamed and the imported symbol; modified markers {marks0[:2]} -> {marks1} (dialogs {seen} {done})" if ok
                else f"control same {control == files0}; marks {marks0} -> {marks1}; changed {changed}; dialogs {seen} {done}; "
                     f"renamed in file {'LED_RENAMED' in text}, imported {'ImportedProbe' in text}")


HEADER_ITEM = {"by": "path", "name": "*", "name_match": "any", "control_type": "HeaderItem"}


def header_names(run: Run) -> list[str]:
    for _attempt in range(6):
        try:
            return [c["name"] for c in run.uia.call("state", {"window": run.window, "selector": HEADER_ITEM})["controls"]]
        except Exception:  # noqa: BLE001
            time.sleep(1.5)
    return []


def step_columns(run: Run) -> dict:
    """selectColumns: the tree header's context menu carries the action ('Select Columns...'); choosing it opens
    the 'Select Columns' dialog, and the columns picked there are the columns the tree then shows."""
    from kicad_mcp.coverage.live_libtree import _describe

    action = COMMON + "selectColumns"
    header = next(c for c in hotkeys._children(run.frame) if c[1] == "SysHeader32" and c[3])[0]
    from kicad_mcp.coverage.cursor_hook import CursorHook

    cursor = CursorHook(run.session.pid)  # KiCad reads the mouse position to find the header cell that was clicked
    cursor.place(header, 30, 8)
    before = header_names(run)
    windows = run.windows()
    popup(run, header, None, right=True)  # control: the menu opened and closed without a choice
    control_dialogs = [t for h, t in run.windows().items() if h not in windows and t]
    entries = popup(run, header, "Select Columns", right=True)
    seen, done = run.settle(windows, {"Select Columns": lambda t: columns_dialog(run, t)})
    cursor.close()
    after = header_names(run)
    ok = (bool(entries) and not control_dialogs and bool(seen) and len(before) == 2 and len(after) == 1)
    return _row(action, "ok" if ok else "no_effect",
                f"header menu {entries}; closed without a choice no dialog opened; choosing it opened {seen} ({done}); "
                f"tree header columns {before} -> {after}" if ok
                else f"entries {entries}; control dialogs {control_dialogs}; dialogs {seen} {done}; header {before} -> {after}; "
                     f"{_describe(run.uia, seen[0]) if seen else ''}")


def dialog_controls(run: Run, title: str, types=("Button", "ListItem", "List", "CheckBox", "Edit", "Text")) -> list[dict]:
    window = {"name": title, "match": "exact"}
    found = []
    for control_type in types:
        with contextlib.suppress(Exception):
            found += run.uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                                          "control_type": control_type}})["controls"]
    return found


def columns_dialog(run: Run, title: str) -> str:
    """Move the Description column from the Enabled list to the Available one ('<') and press OK."""
    window = {"name": title, "match": "exact"}
    run.uia.call("select", {"window": window, "selector": _control("Description", "ListItem")})
    time.sleep(0.4)
    run.uia.call("invoke", {"window": window, "selector": _control("<", "Button")})
    time.sleep(0.6)
    return "moved 'Description' out of the Enabled list; " + _dialog_button(run, title, "OK")


DROP_SCRIPT = r"""
const k = Process.getModuleByName('kernel32.dll'), u = Process.getModuleByName('user32.dll');
const GlobalAlloc = new NativeFunction(k.getExportByName('GlobalAlloc'), 'pointer', ['uint', 'size_t']);
const GlobalLock = new NativeFunction(k.getExportByName('GlobalLock'), 'pointer', ['pointer']);
const GlobalUnlock = new NativeFunction(k.getExportByName('GlobalUnlock'), 'int', ['pointer']);
const PostMessageW = new NativeFunction(u.getExportByName('PostMessageW'), 'int', ['pointer', 'uint', 'pointer', 'pointer']);
// What Explorer sends when a file is dropped on a window: WM_DROPFILES with an HDROP (a DROPFILES block in
// global memory: file-name offset, drop point, non-client flag, wide flag, then the double-null list).
rpc.exports = { drop(hwnd, path) {
    const h = GlobalAlloc(0x42, 20 + (path.length + 2) * 2);
    const p = GlobalLock(h);
    p.writeU32(20); p.add(4).writeS32(10); p.add(8).writeS32(10); p.add(12).writeU32(0); p.add(16).writeU32(1);
    p.add(20).writeUtf16String(path);
    GlobalUnlock(h);
    return PostMessageW(ptr(hwnd), 0x233, h, ptr(0));
} };
"""


class FileDrop:
    """Drops a file on a window of the session's own KiCad process, as the shell would (WM_DROPFILES)."""

    def __init__(self, pid: int):
        import frida

        self._session = frida.attach(pid)
        self._script = self._session.create_script(DROP_SCRIPT)
        self._script.load()

    def drop(self, hwnd: int, path: Path) -> None:
        self._script.exports_sync.drop(hex(hwnd), str(path))

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()


def step_drop(run: Run) -> dict:
    """ddaddLibrary: a library file dropped on the editor is added to the project's library table and shows
    in the tree; a dropped file of another kind is ignored (the control)."""
    action = COMMON + "ddaddLibrary"
    table = run.work / "sym-lib-table"
    stray = run.out / "notes.txt"
    stray.write_text("not a library", encoding="utf-8")
    library = run.out / "dropped.kicad_sym"
    library.write_text(library_text("DroppedOne"), encoding="utf-8")
    drop = FileDrop(run.session.pid)
    try:
        before, table0 = run.tree(), table.read_text(encoding="utf-8")
        windows = run.windows()
        drop.drop(run.frame, stray)
        time.sleep(3)
        control, table_control = run.tree(), table.read_text(encoding="utf-8")
        drop.drop(run.frame, library)
        seen, done = run.settle(windows, wait_s=6)
        time.sleep(2)
    finally:
        drop.close()
    after, table1 = run.tree(), table.read_text(encoding="utf-8")
    ok = (control == before and table_control == table0 and "dropped" not in table0 and "dropped" in table1
          and any(_base(n) == "dropped" for n in after) and not any(_base(n) == "dropped" for n in before))
    return _row(action, "ok" if ok else "no_effect",
                f"a dropped .txt left the tree and sym-lib-table unchanged; the dropped dropped.kicad_sym added library 'dropped' to the "
                f"tree ({len(before)} -> {len(after)} rows) and to the project's sym-lib-table on disk (dialogs {seen} {done})" if ok
                else f"tree {before} -> {control} -> {after}; table has 'dropped': {'dropped' in table1}; dialogs {seen} {done}")


def guarded(rows: list[dict], name: str, step, *args) -> None:
    try:
        rows.append(step(*args))
    except Exception as exc:  # noqa: BLE001
        import traceback

        rows.append(_row(name, "error", f"{type(exc).__name__}: {exc} @ {traceback.format_exc().splitlines()[-4:-2]}"))


@cleanup_after_call
def run_symedit(inventory: dict, project: Path, only: set[str]) -> dict:
    from kicad_mcp.coverage.focus_hook import FocusHook

    names = [COMMON + "libraryTreeSearch", COMMON + "collapseAll", COMMON + "pinLibrary", COMMON + "unpinLibrary",
             COMMON + "saveAll", SYM + "renameFootprint", SYM + "importSymbol", SYM + "duplicateSymbol"]
    run = Run(inventory, make_fixture(project), "symedit", names)
    rows: list[dict] = []
    focus = None
    try:
        rows.append(_row("setup", "info", f"tree at start: {run.tree()[:5]}"))
        if not only or "libraryTreeSearch" in only:
            focus = FocusHook(run.session.pid)
            rows.append(step_search(run, focus))
            focus.close()
            focus = None
        if not only or "collapseAll" in only:
            guarded(rows, "collapseAll", step_collapse, run)
        if not only or "unpinLibrary" in only:
            guarded(rows, "unpinLibrary", step_unpin, run)
        if not only or "renameFootprint" in only:
            guarded(rows, "renameFootprint", step_rename, run)
        if not only or "importSymbol" in only:
            guarded(rows, "importSymbol", step_import, run)
        if not only or "selectColumns" in only:
            guarded(rows, "selectColumns", step_columns, run)
        if not only or "saveAll" in only:
            guarded(rows, "saveAll", step_save_all, run)
        if not only or "ddaddLibrary" in only:
            guarded(rows, "ddaddLibrary", step_drop, run)
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("error", "error", f"{type(exc).__name__}: {exc}"))
    finally:
        if focus is not None:
            with contextlib.suppress(Exception):
                focus.close()
        run.close()
    return rows


def pretty_files(run: Run) -> dict[str, str]:
    return {p.name: _sha(p) for p in sorted(run.work.glob("*.pretty/*.kicad_mod"))}


@cleanup_after_call
def run_fpedit(inventory: dict, project: Path, only: set[str]) -> list[dict]:
    """copyFootprint: KiCad copies into its own buffer (not the clipboard). Proof: a paste before any copy makes
    nothing; after copying footprint A a paste makes A_copy on disk (same pads as A); after copying B, B_copy."""
    action = FP + "copyFootprint"
    run = Run(inventory, project, "fpedit", [FP + "copyFootprint", FP + "pasteFootprint"])
    rows: list[dict] = []
    try:
        rows.append(_row("setup", "info", f"tree at start: {run.tree()[:6]}"))
        names = [_base(n) for n in run.tree()]
        library, first, second = names[0], names[1], names[2]
        copy, paste = FP + "copyFootprint", FP + "pasteFootprint"

        def press(name: str) -> None:
            run.press_on(run.canvas, name)
            time.sleep(1.5)
            run.settle(run.windows(), wait_s=2)

        run.select(run.row(library))
        files0, tree0 = pretty_files(run), [_base(n) for n in run.tree()]
        press(paste)  # control: nothing was copied yet
        files_control, tree_control = pretty_files(run), [_base(n) for n in run.tree()]
        run.select(run.row(first))
        press(copy)
        files_copied = pretty_files(run)  # the copy itself writes nothing: it is KiCad's internal buffer
        run.select(run.row(library))
        press(paste)
        files1 = pretty_files(run)
        new1 = sorted(set(files1) - set(files0))
        run.select(run.row(second))
        press(copy)
        run.select(run.row(library))
        press(paste)
        files2 = pretty_files(run)
        new2 = sorted(set(files2) - set(files1))
        text_first = next(p for p in run.work.glob("*.pretty/" + first + ".kicad_mod")).read_text(encoding="utf-8")
        copy_path = next(iter(run.work.glob(f"*.pretty/{new1[0]}"))) if new1 else None
        text_copy = copy_path.read_text(encoding="utf-8") if copy_path else ""
        same_pads = bool(copy_path) and text_first.count("(pad ") == text_copy.count("(pad ") > 0
        ok = (files_control == files0 and tree_control == tree0 and files_copied == files0
              and new1 == [first + "_copy.kicad_mod"] and new2 == [second + "_copy.kicad_mod"] and same_pads)
        rows.append(_row(action, "ok" if ok else "no_effect",
                         f"paste before any copy made no file and no tree row; after copying '{first}' the copy alone wrote nothing "
                         f"and a paste made {new1} ({text_copy.count('(pad ')} pads, as '{first}'); after copying '{second}' a paste "
                         f"made {new2}: the buffer holds what was last copied (KiCad's internal buffer, not the OS clipboard)" if ok
                         else f"control files same {files_control == files0}, tree same {tree_control == tree0}; new after copy+paste {new1}; "
                              f"after second {new2}; pads same {same_pads}"))
    except Exception as exc:  # noqa: BLE001
        rows.append(_row("copyFootprint", "error", f"{type(exc).__name__}: {exc}"))
    finally:
        run.close()
    return rows


PREVIEW_SCRIPT = r"""
let clears = 0, adds = 0;
const g = Process.getModuleByName('kigal.dll');
g.enumerateExports().forEach(e => {
    if (/^\?ClearPreview@VIEW@KIGFX/.test(e.name)) Interceptor.attach(e.address, { onEnter() { clears++; } });
    if (/^\?AddToPreview@VIEW@KIGFX/.test(e.name)) Interceptor.attach(e.address, { onEnter() { adds++; } });
});
rpc.exports = { reset() { clears = 0; adds = 0; }, counts() { return [clears, adds]; } };
"""


class PreviewProbe:
    """Counts how often the session's editor clears and refills the canvas preview layer (in its own process)."""

    def __init__(self, pid: int):
        import frida

        self._session = frida.attach(pid)
        self._script = self._session.create_script(PREVIEW_SCRIPT)
        self._script.load()

    def reset(self) -> None:
        self._script.exports_sync.reset()

    def counts(self) -> tuple[int, int]:
        clears, adds = self._script.exports_sync.counts()
        return int(clears), int(adds)

    def close(self) -> None:
        try:
            self._script.unload()
        finally:
            self._session.detach()


@cleanup_after_call
def run_sheet(inventory: dict, project: Path, only: set[str]) -> list[dict]:
    """cursorClick and refreshPreview, on a blank sheet with wires drawn by the wire tool (the wire tool's own
    drawing helpers). The mouse position is told to KiCad in its own process (cursor hook)."""
    import uuid

    from kicad_mcp.coverage import live_wiretool as wt
    from kicad_mcp.coverage.cursor_hook import CursorHook
    from kicad_mcp.coverage.live_tools import click
    from kicad_mcp.ipc import api

    del project
    click_action, refresh_action = COMMON + "cursorClick", COMMON + "refreshPreview"
    root = Path(scoped_mkdtemp(prefix="kicad-ai-tools-libctl-sheet-")) / "blank"
    root.mkdir()
    (root / "blank.kicad_sch").write_text(wt.BLANK_SCH.format(uuid=uuid.uuid4()), encoding="utf-8")
    (root / "blank.kicad_pro").write_text("{}", encoding="utf-8")
    names = [a["name"] for a in inventory["actions"]]
    from kicad_mcp.coverage.live_hotkeys import _start

    session, assigned, _frame, canvas = _start("eeschema", root / "blank.kicad_sch", names,
                                               [wt.TOOL, wt.FINISH, click_action, refresh_action])
    hook = CursorHook(session.pid)
    rows: list[dict] = []
    quiet = hotkeys.CONTROL_KEY[1]

    def selection() -> list[str]:
        return sorted(i["id"]["value"] for i in api.call(session, "GetSelection", {})["response"].get("items", []))

    try:
        # a wire to click on: drawn from (420, 300) to (580, 400)
        wires = wt._draw(session, canvas, hook, assigned, (420, 300), None)
        hotkeys.press(canvas, hotkeys.VK_ESCAPE)
        time.sleep(0.5)
        if not wires:
            raise RuntimeError("the wire tool drew nothing")
        # -- cursorClick: the selection tool takes the key as the click that ends a press (it only acts while a
        # mouse press is pending: the 'disambiguation' timer a button press starts), so the button goes down on
        # the wire, the key is pressed, and the button is released afterwards
        try:
            def press_and(key: int, x: int, y: int) -> list[str]:
                api.call(session, "ClearSelection", {})
                wt._move(canvas, hook, x, y, (x + 20, y + 20))
                point = (y << 16) | x
                hotkeys.user32.PostMessageW(canvas, 0x0201, 1, point)  # left button down, held
                time.sleep(0.08)
                hotkeys.press(canvas, key)
                time.sleep(0.12)
                seen = selection()
                hotkeys.user32.PostMessageW(canvas, 0x0202, 0, point)  # released
                time.sleep(0.8)
                hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                time.sleep(0.5)
                return seen

            over_empty = press_and(assigned[click_action][1], 900, 650)
            control = press_and(quiet, 420, 300)
            picked = []
            for _attempt in range(3):  # a press and a key a few milliseconds apart: retried if the key lands late
                picked = press_and(assigned[click_action][1], 420, 300)
                if picked:
                    break
            ok = not over_empty and not control and len(picked) >= 1
            rows.append(_row(click_action, "ok" if ok else "no_effect",
                             f"with a mouse press pending: over empty sheet the action selected nothing; over a wire an unbound key "
                             f"selected nothing; the action selected {len(picked)} item(s) ({picked[:1]}), the drawn wire" if ok
                             else f"over empty {over_empty}, control {control}, picked {picked}"))
        except Exception as exc:  # noqa: BLE001
            rows.append(_row(click_action, "error", f"{type(exc).__name__}: {exc}"))
        # -- refreshPreview: the wire tool rebuilds its on-canvas preview (it clears the view's preview layer and
        # adds the preview wires again). The preview layer is not readable from outside, so the two calls are
        # counted inside the session's own process (kigal's VIEW::ClearPreview / AddToPreview).
        try:
            probe = PreviewProbe(session.pid)
            try:
                x, y = 420, 520
                hotkeys.press(canvas, assigned[wt.TOOL][1])
                time.sleep(0.6)
                wt._move(canvas, hook, x, y)
                hook.place(canvas, x, y)
                click(canvas, x, y)
                wt._move(canvas, hook, x + 100, y + 60, (x, y))
                time.sleep(2.5)
                trace = []
                for label, key in (("idle", None), ("unbound key", quiet), ("action", assigned[refresh_action][1]),
                                   ("unbound key", quiet), ("action", assigned[refresh_action][1]), ("idle", None)):
                    probe.reset()
                    if key is not None:
                        hotkeys.press(canvas, key)
                    time.sleep(1.5)
                    trace.append((label, probe.counts()))
                    time.sleep(1.0)
                print("TRACE", trace, flush=True)
                control = [c for lbl, c in trace if lbl != "action"]
                acted = [c for lbl, c in trace if lbl == "action"]
            finally:
                probe.close()
                for _ in range(2):
                    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                    time.sleep(0.3)
            ok = all(c == (0, 0) for c in control) and all(c[0] >= 1 and c[1] >= 1 for c in acted)
            rows.append(_row(refresh_action, "ok" if ok else "no_effect",
                             f"with the wire tool drawing, an unbound key made (ClearPreview, AddToPreview) calls {control}; the action made "
                             f"{acted}: the tool rebuilt its preview" if ok
                             else f"control {control}, action {acted}"))
        except Exception as exc:  # noqa: BLE001
            rows.append(_row(refresh_action, "error", f"{type(exc).__name__}: {exc}"))
    finally:
        hook.close()
        session.close(discard=True)
    return rows


VIEW_SCRIPT = r"""
let items = 0, passes = 0;
const g = Process.getModuleByName('kigal.dll');
g.enumerateExports().forEach(e => {
    if (/^\?Update@VIEW@KIGFX/.test(e.name)) Interceptor.attach(e.address, { onEnter() { items++; } });
    if (/^\?UpdateItems@VIEW@KIGFX/.test(e.name)) Interceptor.attach(e.address, { onEnter() { passes++; } });
});
rpc.exports = { reset() { items = 0; passes = 0; }, counts() { return [items, passes]; } };
"""


class ViewProbe(PreviewProbe):
    """Counts the canvas view's per-item updates and its update passes (in the session's own process)."""

    def __init__(self, pid: int):
        import frida

        self._session = frida.attach(pid)
        self._script = self._session.create_script(VIEW_SCRIPT)
        self._script.load()


def step_find(run: Run) -> dict:
    """updateFind: with the Find dialog open and a search string set, the action makes the editor re-mark the
    found items (each matching item is updated on the canvas view, then the view's update pass runs). Counted
    inside the session's own process: an unbound key makes none, the action updates the matching items, and with a
    string nothing matches it runs the pass but updates no item."""
    action = COMMON + "updateFind"
    windows = run.windows()
    hotkeys.press(run.canvas, run.key("common.Interactive.find"))
    deadline = time.monotonic() + 20
    title = ""
    while time.monotonic() < deadline and not title:
        title = next((t for h, t in run.windows().items() if h not in windows and t == "Find"), "")
        time.sleep(0.5)
    if not title:
        return _row(action, "no_effect", "the Find dialog did not open")
    from kicad_mcp.coverage.focus_hook import FocusHook

    probe = ViewProbe(run.session.pid)
    focus = FocusHook(run.session.pid)  # the keys go to the drawing canvas, the control a person's hand is on
    focus.focus(run.canvas)
    result: dict[str, tuple[int, int]] = {}
    try:
        dialog = next(h for h, t in run.windows().items() if t == title)

        def settle_text(text: str) -> None:
            """Type the string into the Find box a key at a time, as a person does."""
            edit = next(c[0] for c in hotkeys._children(dialog) if c[1] == "Edit" and c[3])
            hotkeys.user32.SendMessageW(edit, 0x000C, 0, "")  # WM_SETTEXT: the box is emptied first
            for ch in text:
                hotkeys.user32.PostMessageW(edit, 0x0102, ord(ch), 1)  # WM_CHAR
                time.sleep(0.2)
            time.sleep(4.0)  # the editor re-marks the items for the new string by itself

        def measure(label: str, key: int) -> None:
            time.sleep(1.0)
            focus.focus(run.canvas)  # the dialog's SetFocus calls moved the hook's focus to it
            probe.reset()
            hotkeys.press(run.canvas, key)
            time.sleep(2.0)
            result[label] = probe.counts()

        text = "R"
        for text in ("R", "C", "V", "1"):
            settle_text(text)
            measure("idle-free unbound key", CONTROL_VK)
            measure("action, matching string", run.key(action))
            measure("unbound key again", CONTROL_VK)
            if result["action, matching string"][0] >= 1:
                break
        settle_text("zzzqqq")
        measure("action, no match", run.key(action))
        with contextlib.suppress(Exception):
            _dialog_button(run, title, "Close")
    finally:
        probe.close()
        focus.close()
    control = [result["idle-free unbound key"], result["unbound key again"]]
    matched, nothing = result["action, matching string"], result["action, no match"]
    ok = all(c == (0, 0) for c in control) and matched[0] >= 1 and matched[1] >= 1 and nothing[0] == 0 and nothing[1] >= 1
    return _row(action, "ok" if ok else "no_effect",
                f"(canvas item updates, update passes): unbound key {control}; the action with '{text}' set in the Find dialog {matched} "
                f"(the found items were re-marked); the action with a string nothing matches {nothing} (pass only)" if ok
                else f"counts {result}")


@cleanup_after_call
def run_find(inventory: dict, project: Path, only: set[str]) -> list[dict]:
    run = Run(inventory, project, "eeschema", ["common.Interactive.find", COMMON + "updateFind"])
    rows: list[dict] = []
    try:
        guarded(rows, "updateFind", step_find, run)
    finally:
        run.close()
    return rows


def step_preferences(run: Run) -> dict:
    """updatePreferences: the measure tool, while it is active, re-reads the display preferences and updates its
    ruler on the canvas view (the one view update its handler makes). Counted in the session's own process: an
    unbound key makes none, the action makes at least one."""
    action = COMMON + "updatePreferences"
    tool = COMMON + "measureTool" if (COMMON + "measureTool") in run.names else "common.Interactive.measureTool"
    probe = ViewProbe(run.session.pid)
    result: dict[str, tuple[int, int]] = {}
    try:
        hotkeys.press(run.canvas, run.key(tool))  # the measure tool is active
        time.sleep(6.0)
        for label, key in (("first unbound key", CONTROL_VK), ("idle", None), ("unbound key", CONTROL_VK), ("action", run.key(action)),
                           ("unbound key again", CONTROL_VK), ("action again", run.key(action)),
                           ("idle again", None)):
            time.sleep(1.0)
            probe.reset()
            if key is not None:
                hotkeys.press(run.canvas, key)
            time.sleep(1.5)
            result[label] = probe.counts()
        hotkeys.press(run.canvas, hotkeys.VK_ESCAPE)
    finally:
        probe.close()
    control = [result["idle"], result["unbound key"], result["unbound key again"], result["idle again"]]
    acted = [result["action"], result["action again"]]
    ok = all(c[0] == 0 for c in control) and all(c[0] >= 1 for c in acted)
    return _row(action, "ok" if ok else "no_effect",
                f"(canvas item updates, passes) with the measure tool active: idle and unbound keys {control}; the action {acted}: "
                f"the tool updated its ruler (the first key pressed after the tool starts also updates once, {result['first unbound key']}: "
                f"not scored)" if ok else f"counts {result}")


@cleanup_after_call
def run_prefs(inventory: dict, project: Path, only: set[str]) -> list[dict]:
    tool = "common.Interactive.measureTool"
    run = Run(inventory, project, "pcbnew", [tool, COMMON + "updatePreferences"])
    rows: list[dict] = []
    try:
        guarded(rows, "updatePreferences", step_preferences, run)
    finally:
        run.close()
    return rows


def z_depth(hwnd: int) -> int:
    """How many top-level windows are above ``hwnd`` in the desktop's z-order (0 = on top)."""
    user32 = hotkeys.user32
    user32.GetWindow.restype = hotkeys.wt.HWND
    user32.GetWindow.argtypes = [hotkeys.wt.HWND, hotkeys.wt.UINT]
    depth, current = 0, user32.GetWindow(hwnd, 3)  # GW_HWNDPREV
    while current and depth < 500:
        depth += 1
        current = user32.GetWindow(current, 3)
    return depth


@temporary_owner(cleanup='close', allocating=('__init__',))
class ManagerRun:
    """The project manager (kicad) on a private desktop, with the editors it opens."""

    def __init__(self, inventory: dict, project: Path, batch: list[str]):
        from kicad_mcp.coverage import live_external as le

        self.le = le
        self.names = [a["name"] for a in inventory["actions"]]
        document = _input_for("kicad", project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-libctl-doc-")))
        self.session, self.assigned, self.frame, self.canvas = _start("kicad", document, self.names, batch)
        self.work = Path(self.session.document).parent
        self.out = Path(scoped_mkdtemp(prefix="kicad-ai-tools-libctl-out-"))

    def key(self, action: str) -> int:
        return self.assigned[action][1]

    def tree_rows(self) -> list[str]:
        trees = [c for c in hotkeys._children(self.frame) if c[1] == "SysTreeView32" and c[3]]
        return [t for _h, t in self.le._tree_items(max(trees, key=lambda c: c[2])[0])]

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.session.close(discard=True)


def step_refresh(run: ManagerRun) -> dict:
    """zoomRedraw ('Refresh'): in the project manager it rebuilds the project tree from the project folder, so a
    folder the person expanded is shown collapsed again (the control key leaves it expanded)."""
    action = COMMON + "zoomRedraw"
    trees = [c for c in hotkeys._children(run.frame) if c[1] == "SysTreeView32" and c[3]]
    tree = max(trees, key=lambda c: c[2])[0]
    send = run.le._send_message()

    def folder_state() -> tuple[bool | None, int]:
        items = run.le._tree_items(tree)
        handle = next((h for h, t in items if t == "footprints.pretty"), None)
        if handle is None:
            return None, len(items)
        return bool((send(tree, 0x1127, handle, 0x20) or 0) & 0x20), len(items)  # TVM_GETITEMSTATE, TVIS_EXPANDED

    handle = next((h for h, t in run.le._tree_items(tree) if t == "footprints.pretty"), None)
    start = folder_state()
    send(tree, 0x1102, 2, handle)  # TVM_EXPAND, TVE_EXPAND
    time.sleep(2)
    expanded = folder_state()
    hotkeys.press(run.canvas, CONTROL_VK)
    time.sleep(3)
    control = folder_state()
    item = next(i for i in hotkeys.menu_items(run.frame) if i["path"][-1].replace("&", "").split("	")[0] == "Refresh")
    hotkeys.user32.PostMessageW(run.frame, hotkeys.WM_COMMAND, item["id"], 0)  # View > Refresh, the action's menu item
    time.sleep(4)
    after = folder_state()
    ok = start[0] is False and expanded[0] is True and control[0] is True and after[0] is False
    return _row(action, "ok" if ok else "no_effect",
                f"the project tree's 'footprints.pretty' folder: collapsed, expanded by hand, still expanded after the control key, "
                f"collapsed again after View > Refresh, the action's menu item (the tree was rebuilt from the project folder; {after[1]} rows)" if ok
                else f"folder expanded state {start} -> {expanded} -> control {control} -> after {after}")


def step_manager_raise(run: ManagerRun) -> dict:
    """showProjectManager: with the project manager running, the action from an editor raises the manager above
    it (z-order read back; the control key changes nothing)."""
    action = COMMON + "showProjectManager"
    menu = next(i for i in hotkeys.menu_items(run.frame) if i["path"][-1].replace("&", "") == "Schematic Editor")
    hotkeys.user32.PostMessageW(run.frame, hotkeys.WM_COMMAND, menu["id"], 0)
    editor = canvas = 0
    for _ in range(60):
        time.sleep(1)
        with contextlib.suppress(Exception):
            editor, canvas = hotkeys.canvas_of(run.session, "Schematic Editor")
            break
    if not editor:
        return _row(action, "no_effect", "the schematic editor did not open from the manager")
    hotkeys.settled_capture(editor, timeout_s=20.0)
    time.sleep(3)
    manager, editor_depth = run.frame, z_depth(editor)
    manager_depth = z_depth(manager)
    hotkeys.press(canvas, CONTROL_VK)
    time.sleep(2)
    control = (z_depth(manager), z_depth(editor))
    hotkeys.press(canvas, run.key(action))
    time.sleep(3)
    after = (z_depth(manager), z_depth(editor))
    ok = bool(editor_depth < manager_depth and control == (manager_depth, editor_depth) and after[0] < after[1])
    return _row(action, "ok" if ok else "no_effect",
                f"the editor was above the manager in z-order (depth editor {editor_depth}, manager {manager_depth}); the control key "
                f"changed nothing; after the action the manager is above the editor (depth manager {after[0]}, editor {after[1]})" if ok
                else f"before editor {editor_depth} manager {manager_depth}; control {control}; after {after}")


@cleanup_after_call
def run_manager(inventory: dict, project: Path, only: set[str]) -> list[dict]:
    run = ManagerRun(inventory, project, [COMMON + "zoomRedraw", COMMON + "showProjectManager"])
    rows: list[dict] = []
    try:
        if not only or "zoomRedraw" in only:
            guarded(rows, "zoomRedraw", step_refresh, run)
        if not only or "showProjectManager" in only:
            guarded(rows, "showProjectManager", step_manager_raise, run)
    finally:
        run.close()
    return rows


def popup_at(run: Run, hwnd: int, x: int, y: int, wait_s: float = 8.0) -> list[str]:
    """Right-click ``hwnd`` at client (x, y) and return the context menu's entries, then close it with Escape."""
    user32 = hotkeys.user32
    point = (y << 16) | x
    user32.PostMessageW(hwnd, 0x0200, 0, point)
    time.sleep(0.1)
    user32.PostMessageW(hwnd, 0x0204, 2, point)
    time.sleep(0.12)
    user32.PostMessageW(hwnd, 0x0205, 0, point)
    deadline = time.monotonic() + wait_s
    menus: list[int] = []
    while time.monotonic() < deadline and not menus:
        menus = [w["hwnd"] for w in run.session.kicad.desktop.windows()
                 if w["pid"] == run.session.pid and w["visible"] and w["class"] == "#32768"]
        time.sleep(0.25)
    if not menus:
        return []
    from kicad_mcp.coverage.live_designblocks import Run as BlockRun

    entries = [e for e in BlockRun.popup_entries(menus[0]) if e]
    hotkeys.press(menus[0], hotkeys.VK_ESCAPE)
    time.sleep(1.0)
    return entries


def step_update_menu(run: Run) -> dict:
    """updateMenu: the library tree shows its context menu "dirty" (TOOL_MENU::ShowContextMenu() without a
    selection, common/widgets/lib_tree.cpp:1054); the menu's entries are only evaluated against what is under the
    pointer when it opens, by ACTION_MENU::OnMenuEvent running ACTIONS::updateMenu (common/tool/action_menu.cpp:421,
    SELECTION_TOOL::UpdateMenu). A right-click on empty tree space and one on a library row therefore give two
    different, non-empty menus."""
    import ctypes

    action = "common.Interactive.updateMenu"
    popup(run, tree_buttons(run)["gear"], "Collapse All")  # rows: the libraries only, at the top of the tree
    tree = run.tree_window()
    rect = hotkeys._Rect()
    hotkeys.user32.GetClientRect(tree, ctypes.byref(rect))
    rows = run.tree()
    blank = popup_at(run, tree, rect.right // 3, rect.bottom - 12)
    found = {}
    for y in range(12, min(rect.bottom // 2, 260), 18):  # the first row is under the header, somewhere near the top
        entries = popup_at(run, tree, rect.right // 3, y)
        if entries and entries != blank:
            found = {"y": y, "entries": entries}
            break
    ok = bool(blank) and bool(found)
    return _row(action, "ok" if ok else "no_effect",
                f"tree rows {rows}; right-click on empty tree space: menu {blank}; right-click on a row (y={found.get('y')}): menu "
                f"{found.get('entries')} (evaluated when it opened: the entries differ with what is under the pointer)")


@cleanup_after_call
def run_menus(inventory: dict, project: Path, only: set[str]) -> list[dict]:
    run = Run(inventory, make_fixture(project), "symedit", [COMMON + "collapseAll"])
    rows: list[dict] = []
    try:
        guarded(rows, "updateMenu", step_update_menu, run)
    finally:
        run.close()
    return rows


@cleanup_after_call
def sweep(inventory: dict, project: Path, phase: str, only: set[str]) -> dict:
    rows = {"symedit": run_symedit, "fpedit": run_fpedit, "sheet": run_sheet, "manager": run_manager, "find": run_find, "prefs": run_prefs, "menus": run_menus}[phase](inventory, project, only)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": f"libctl-{phase}", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.phase,
                   {o for o in args.only.split(",") if o})
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
