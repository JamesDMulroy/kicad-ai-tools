"""Live proof for the design-block tools, driven in the editors and read back from the library on disk.

A design block is a folder in a ``.kicad_blocks`` library (a schematic, a board or both, plus a JSON
sheet of metadata). The schematic and board editors list the libraries in a "Design Blocks" panel; the
actions here either save, update or delete a block (the panel's selected row is what they act on) or are
interactive tools that place a block, a sheet or a drawing on the canvas. Each step selects the panel row
a person would click, presses the action's own key (the session profile's hotkey), completes the dialog
it opens through UI Automation, and then compares what is on disk: the block library's files, and the
schematic or board saved afterwards. An action passes only when the files hold what it makes.

    python -m kicad_mcp.coverage.live_designblocks --inventory inv.json --project <dir> --editor eeschema --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import hashlib
import json
from pathlib import Path
import re
import time

from kicad_mcp.coverage.live_hotkeys import FRAMES, _input_for
from kicad_mcp.ipc import hotkeys

SCH = "eeschema.SchDesignBlockControl."
PCB = "pcbnew.PcbDesignBlockControl."
SCH_DRAW = "eeschema.InteractiveDrawing."
PANEL = {"eeschema": SCH + "showDesignBlockPanel", "pcbnew": PCB + "showDesignBlockPanel"}
SAVE = "common.Control.save"
LIBRARY = "test"
BLOCKS = "test.kicad_blocks"

# Interactive tools this module proves by driving them on the canvas and reading the saved file back
# (the gate routes these names here); each value says what the saved file must gain.
CASES: dict[str, str] = {
    "eeschema.InteractiveDrawing.placeDesignBlock": "the saved schematic holds the block's symbols",
    "eeschema.InteractiveDrawing.placeLinkedDesignBlock": "the saved schematic holds another copy of the linked block",
    "eeschema.InteractiveDrawing.saveToLinkedDesignBlock": "the linked block's schematic in the library is rewritten",
    "eeschema.InteractiveDrawing.drawSheetFromDesignBlock": "the saved schematic holds a sheet made from the block",
    "eeschema.InteractiveDrawing.drawSheetFromFile": "the saved schematic holds a sheet made from the file",
    "eeschema.InteractiveDrawing.importSheet": "the saved schematic holds the imported sheet's symbols",
    "eeschema.InteractiveDrawing.placeSheetPin": "the saved schematic holds the sheet pins",
    "eeschema.InteractiveDrawing.syncSheetPins": "the synchronize-pins window opens for the sheet",
    "eeschema.InteractiveDrawing.syncAllSheetsPins": "the synchronize-pins window opens for every sheet",
    "eeschema.InteractiveDrawing.placeNextSymbolUnit": "the saved schematic holds the symbol's next unit",
    "eeschema.InteractiveDrawingLineWireBus.unfoldBus": "the saved schematic holds a bus entry",
    "eeschema.EditorControl.importGraphics": "the saved schematic holds the imported drawing",
    "pcbnew.InteractiveDrawing.placeDesignBlock": "the saved board holds the block's footprints",
    "pcbnew.InteractiveDrawing.placeLinkedDesignBlock": "the saved board holds another copy of the linked block",
    "pcbnew.InteractiveDrawing.saveToLinkedDesignBlock": "the linked block's board in the library is rewritten",
    "pcbnew.InteractiveDrawing.applyDesignBlockLayout": "the linked group on the saved board takes the block's layout",
    "pcbnew.InteractiveDrawing.placeImportedGraphics": "the saved board holds the imported drawing",
}


def _tree_item(name: str) -> dict:
    return {"by": "path", "name": name, "control_type": "TreeItem"}


def _control(name: str, control_type: str) -> dict:
    return {"by": "path", "name": name, "control_type": control_type}


def _digest(folder: Path) -> dict[str, str]:
    """Every file under the folder, with a hash of its content."""
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def _start_editor(editor: str, document: Path, all_actions: list[str], batch: list[str], settings: dict):
    """The editor on a private desktop with one hotkey per action of ``batch`` and the given settings files
    ({file name: JSON}) already in its profile, so a preference can be set before the editor reads it."""
    from kicad_mcp.ipc.gui_session import GuiSession

    session = GuiSession(document)
    config = session.profile / "config" / session._settings_version()
    assigned = hotkeys.write_hotkeys(config, all_actions, batch)
    for name, content in settings.items():
        (config / name).write_text(json.dumps(content, indent=2), encoding="utf-8")
    session.start()
    title = FRAMES[editor][1]
    frame, _ = hotkeys.canvas_of(session, title)
    hotkeys.settled_capture(frame, timeout_s=15.0)
    frame, canvas = hotkeys.canvas_of(session, title)
    return session, assigned, frame, canvas


@temporary_owner(cleanup='close', allocating=('__init__',))
class Run:
    """One editor session with the Design Blocks panel open, and the helpers the steps share."""

    def __init__(self, inventory: dict, project: Path, editor: str, actions: list[str], settings: dict | None = None):
        self.editor = editor
        self.names = {a["name"] for a in inventory["actions"]}
        self.title = FRAMES[editor][1]
        self.window = {"name": self.title, "match": "contains"}
        document = _input_for(editor, project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-blocks-")))
        batch = [a for a in dict.fromkeys([PANEL[editor], SAVE, *actions]) if a in self.names]
        self.session, self.assigned, self.frame, self.canvas = _start_editor(
            editor, document, [a["name"] for a in inventory["actions"]], batch, settings or {})
        self.uia = self.session.attach_uia()
        self.work = Path(self.session.document).parent
        self.blocks = self.work / BLOCKS
        self.out = Path(scoped_mkdtemp(prefix="kicad-ai-tools-blocks-out-"))
        self.notes: list[str] = []

    # -- input
    def press(self, action: str) -> None:
        hotkeys.press(self.canvas, self.assigned[action][1])

    def warm_up(self) -> None:
        """The canvas follows the mouse only after it has seen it move."""
        for step in range(10):
            hotkeys.user32.PostMessageW(self.canvas, 0x0200, 0, ((200 + 15 * step) << 16) | (200 + 25 * step))
            time.sleep(0.1)
        self.focus_canvas()

    def focus_canvas(self) -> None:
        """A click on an empty corner of the canvas gives it the focus its tools need before they take a key
        (a panel control that was just used keeps it otherwise)."""
        self.click_at(40, 40)
        hotkeys.press(self.canvas, hotkeys.VK_ESCAPE)
        time.sleep(0.5)

    def open_panel(self) -> None:
        self.press(PANEL[self.editor])
        time.sleep(3)
        self.focus_canvas()

    def save(self) -> None:
        self.press(SAVE)
        time.sleep(2.0)

    def item_ids(self, kind: str) -> list[str]:
        from kicad_mcp.ipc import api

        items = api.call(self.session, "GetItems", {"types": [kind]})["response"].get("items", [])
        return [i["id"]["value"] for i in items if i.get("id", {}).get("value")]

    def select_item(self, kind: str, index: int = 0) -> str | None:
        """Select one item of a kind through the API (what a click on it does for the action's input)."""
        from kicad_mcp.ipc import api

        items = api.call(self.session, "GetItems", {"types": [kind]})["response"].get("items", [])
        ids = [i["id"]["value"] for i in items if i.get("id", {}).get("value")]
        if len(ids) <= index:
            return None
        hotkeys.select(self.session, ids[index])
        return ids[index]

    # -- panel
    def rows(self) -> list[str]:
        panel = self.uia.call("state", {"window": self.window, "selector": {
            "by": "path", "name": "*", "name_match": "any", "control_type": "TreeItem"}})["controls"]
        return [c["name"] for c in panel]

    def pick(self, name: str) -> None:
        """Click the panel row (expanding its library first when the row is inside one)."""
        if name not in self.rows():
            self.uia.call("expand", {"window": self.window, "selector": _tree_item(LIBRARY)})
            time.sleep(1.0)
        self.uia.call("select", {"window": self.window, "selector": _tree_item(name)})
        time.sleep(0.6)

    def set_check(self, label: str, want: bool) -> None:
        """Set one of the panel's check boxes the way a click would."""
        selector = _control(label, "CheckBox")
        state = self.uia.call("state", {"window": self.window, "selector": selector})["controls"][0]
        if bool(state.get("toggle")) != want:
            self.uia.call("invoke", {"window": self.window, "selector": selector})
            time.sleep(0.5)
        state = self.uia.call("state", {"window": self.window, "selector": selector})["controls"][0]
        if bool(state.get("toggle")) != want:
            raise RuntimeError(f"check box '{label}' would not change")
        self.focus_canvas()

    # -- dialogs
    def new_windows(self, before: set[int]) -> dict[int, str]:
        return {h: t for h, t in hotkeys._visible(self.session).items() if h not in before and t}

    def fill(self, title: str, fields: dict[str, str] | None = None, button: str = "OK") -> bool:
        """Fill a dialog's edits (by label) and press its button; False when it is not on screen."""
        window = {"name": title, "match": "exact"}
        for _ in range(20):
            try:
                self.uia.call("find", {"window": window})
                break
            except Exception:  # noqa: BLE001 - not open yet
                time.sleep(0.5)
        else:
            return False
        for label, value in (fields or {}).items():
            self.uia.call("set-value", {"window": window, "selector": _control(label, "Edit"), "value": value})
        self.uia.call("invoke", {"window": window, "selector": _control(button, "Button")})
        time.sleep(1.5)
        return True

    def choose_file(self, title: str, path: Path) -> str:
        """Type a path into a common file dialog and press its default button (OK, by command: an Open
        dialog does not expose that button to UI Automation)."""
        window = {"name": title, "match": "exact"}
        self.uia.call("set-value", {"window": window, "selector": _control("File name:", "Edit"), "value": str(path)})
        hwnd = next(h for h, t in hotkeys._visible(self.session).items() if t == title)
        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_COMMAND, 1, 0)
        return f"chose {path.name}"

    def settle(self, before: set[int], fields: dict[str, str] | None = None, files: dict[str, Path] | None = None,
               forms: dict[str, dict[str, str]] | None = None) -> tuple[list[str], list[str]]:
        """Answer the dialogs a step opens, one after another (a confirmation, then a properties form):
        the block properties form gets ``fields`` and OK, a confirmation its affirmative button, anything
        else is closed. Returns the titles seen and what was done."""
        from kicad_mcp.coverage.live_libtree import _describe

        seen, done = [], []
        for _ in range(5):
            fresh = self.new_windows(before)
            if not fresh:
                break
            for hwnd, title in fresh.items():
                seen.append(title)
                if title == "Design Block Properties":
                    done.append(f"{title}: filled {sorted(fields or {})}, OK" if self.fill(title, fields) else f"{title}: not filled")
                    continue
                if forms and title in forms:
                    done.append(f"{title}: filled {sorted(forms[title])}, OK" if self.fill(title, forms[title])
                                else f"{title}: not filled")
                    continue
                if files and title in files:
                    done.append(f"{title}: {self.choose_file(title, files[title])}")
                    time.sleep(1.5)
                    continue
                self.notes.append(f"'{title}': {_describe(self.uia, title)}")
                for label in ("Yes", "Overwrite", "Continue Load", "OK", "Delete", "Continue"):
                    try:
                        self.uia.call("invoke", {"window": {"name": title, "match": "exact"},
                                                 "selector": _control(label, "Button")})
                        done.append(f"{title}: {label}")
                        break
                    except Exception:  # noqa: BLE001
                        continue
                else:
                    hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
                    done.append(f"{title}: closed")
                time.sleep(1.5)
            time.sleep(1.0)
        return seen, done

    @staticmethod
    def popup_entries(menu_window: int) -> list[str]:
        """The labels of a popup menu's entries (the menu handle is asked of the menu window itself)."""
        import ctypes

        user32 = hotkeys.user32
        user32.SendMessageW.restype = ctypes.c_void_p
        handle = ctypes.c_void_p(user32.SendMessageW(menu_window, 0x1E1, 0, 0))  # MN_GETHMENU
        user32.GetMenuItemCount.argtypes = [ctypes.c_void_p]
        user32.GetMenuStringW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int, ctypes.c_uint]
        labels = []
        for index in range(max(user32.GetMenuItemCount(handle), 0)):
            buffer = ctypes.create_unicode_buffer(128)
            user32.GetMenuStringW(handle, index, buffer, 128, 0x400)
            labels.append(buffer.value)
        return labels

    def choose_popup_item(self, down: int = 1) -> bool:
        """Pick an entry of the context menu a tool opened, by keys sent to the menu itself (Down, Return)."""
        for _ in range(20):
            menus = [w["hwnd"] for w in self.session.kicad.desktop.windows()
                     if w["pid"] == self.session.pid and w["visible"] and w["class"] == "#32768"]
            if menus:
                break
            time.sleep(0.3)
        else:
            return False
        self.notes.append(f"menu entries: {self.popup_entries(menus[0])}")
        for _ in range(down):
            hotkeys.press(menus[0], 0x28)
            time.sleep(0.3)
        hotkeys.press(menus[0], 0x0D)
        time.sleep(1.0)
        return True

    # -- canvas
    def centre(self) -> tuple[int, int]:
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(self.canvas, hotkeys.ctypes.byref(rect))
        return rect.right // 2, rect.bottom // 2

    def move_to(self, x: int, y: int) -> None:
        for step in range(1, 5):  # the item follows the mouse in a few moves, as a hand would
            hotkeys.user32.PostMessageW(self.canvas, 0x0200, 0, ((y - 8 * (4 - step)) << 16) | (x - 10 * (4 - step)))
            time.sleep(0.1)

    def click_at(self, x: int, y: int) -> None:
        self.move_to(x, y)
        point = (y << 16) | x
        hotkeys.user32.PostMessageW(self.canvas, 0x0201, 1, point)  # WM_LBUTTONDOWN
        time.sleep(0.12)  # a click held for a moment is seen as a click
        hotkeys.user32.PostMessageW(self.canvas, 0x0202, 0, point)  # WM_LBUTTONUP
        time.sleep(1.0)

    def document_text(self) -> str:
        return Path(self.session.document).read_text(encoding="utf-8")

    def close(self) -> None:
        self.session.close(discard=True)


def _row(action: str, outcome: str, detail: str, **extra) -> dict:
    return {"action": action, "outcome": outcome, "detail": detail, **extra}


# ---- schematic: the panel's own actions ----------------------------------------------------------

def schematic_block_control(run: Run) -> list[dict]:
    return block_control(run, SCH, "Sheet", "KOT_SCH_SYMBOL")


def board_block_control(run: Run) -> list[dict]:
    return block_control(run, PCB, "Board", "KOT_PCB_FOOTPRINT")


def block_control(run: Run, ns: str, whole: str, kind: str) -> list[dict]:
    """Save, update and delete blocks through the panel; ``whole`` is Sheet or Board, ``kind`` what a selection holds."""
    rows: list[dict] = []
    first, second = "blk_sheet", "blk_sel"

    def step(action: str, selection, fields: dict[str, str] | None, check) -> None:
        """Select, press, answer the dialogs, then compare the block library with ``check``."""
        row = _row(action, "no_effect", "")
        try:
            selection()
            before_files, windows = _digest(run.blocks), set(hotkeys._visible(run.session))
            run.press(action)
            time.sleep(2.0)
            opened, closed = run.settle(windows, fields)
            after_files = _digest(run.blocks)
            verdict = check(before_files, after_files)
            row["dialogs"] = [*opened, *closed]
            row["notes"], run.notes = run.notes, []
            row["rows_after"] = run.rows()
            if verdict:
                row.update(outcome="ok", detail=verdict)
            else:
                row["detail"] = f"block library unchanged ({len(after_files)} files)"
        except Exception as exc:  # noqa: BLE001
            row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
        rows.append(row)

    def made(name: str):
        def check(before, after):
            new = sorted(f for f in after if f not in before)
            return f"new block files {new}" if any(f.startswith(f"{name}.kicad_block") for f in new) else ""
        return check

    def changed(name: str):
        def check(before, after):
            diff = sorted(f for f in after if f in before and before[f] != after[f] and f.startswith(f"{name}.kicad_block"))
            return f"block files rewritten {diff}" if diff else ""
        return check

    def removed(name: str):
        def check(before, after):
            gone = sorted(f for f in before if f not in after and f.startswith(f"{name}.kicad_block"))
            return f"block files deleted {gone}" if gone else ""
        return check

    library = lambda: run.pick(LIBRARY)  # noqa: E731
    save_whole = ns + ("saveSheetAsDesignBlock" if whole == "Sheet" else "saveBoardAsDesignBlock")
    update_whole = ns + f"updateDesignBlockFrom{whole}"
    step(save_whole, library, {"Name:": first}, made(first))
    step(ns + "saveSelectionAsDesignBlock",
         lambda: (run.select_item(kind), run.pick(LIBRARY)), {"Name:": second}, made(second))
    step(ns + "updateDesignBlockFromSelection",
         lambda: (run.select_item(kind, 3), run.pick(first)), None, changed(first))
    step(update_whole, lambda: run.pick(second), None, changed(second))
    step(ns + "deleteDesignBlock", lambda: run.pick(second), None, removed(second))
    return rows


def count(text: str, token: str) -> int:
    return len(re.findall(token, text))


# What the saved schematic gains, per kind of placement.
SYMBOLS, SHEETS, GROUPS = r'\(lib_id "', r"\n\t\(sheet\n", r'\n\t\(group "'


def tool_step(run: Run, action: str, drive, tokens: tuple[str, ...], setup=None, fields=None, files=None,
              library: bool = False, dialogs_first: bool = False, window: bool = False, attempts: int = 2, changed: bool = False,
              forms: dict[str, dict[str, str]] | None = None, presave: bool = True) -> dict:
    """Run an interactive tool: set up its inputs, press its key, drive the canvas, answer its dialogs, save,
    and require the saved schematic to hold more of every token than before."""
    row = _row(action, "no_effect", "", route="tool-driver-file")
    try:
        if setup:
            setup()
        tries = 0
        for _ in range(attempts):  # a tool that never started (the canvas lost its focus) is pressed once more
            tries += 1
            if presave or tries > 1:
                run.save()
            before, library_before = run.document_text(), _digest(run.blocks)
            windows = set(hotkeys._visible(run.session))
            run.press(action)
            time.sleep(1.5)
            early: tuple[list[str], list[str]] = ([], [])
            if dialogs_first:  # the tool asks for its file before it follows the mouse
                early = run.settle(windows, fields, files, forms)
                windows = set(hotkeys._visible(run.session))
            drive()
            opened, done = run.settle(windows, fields, files, forms)
            opened, done = [*early[0], *opened], [*early[1], *done]
            for _ in range(2):
                hotkeys.press(run.canvas, hotkeys.VK_ESCAPE)
                time.sleep(0.4)
            run.settle(windows, fields, files, forms)
            run.save()
            after = run.document_text()
            gained = {t: (count(before, t), count(after, t)) for t in tokens}
            row["dialogs"], row["notes"], run.notes = [*opened, *done], run.notes, []
            library_after = _digest(run.blocks)
            rewritten = sorted(f for f in library_after if library_before.get(f) not in (None, library_after[f]))
            if library and rewritten:
                row.update(outcome="ok", detail=f"block library rewritten {rewritten}")
            elif changed and before != after:
                row.update(outcome="ok", detail=f"saved file changed ({len(before)} -> {len(after)} bytes)")
            elif window and [t for t in row["dialogs"] if ": " not in t]:
                row.update(outcome="ok", detail=f"opened {[t for t in row['dialogs'] if ': ' not in t]}")
            elif tokens and all(b < a for b, a in gained.values()):
                row.update(outcome="ok", detail=f"saved schematic gained {gained}")
            else:
                row["detail"] = (f"saved schematic did not gain {gained}" if before != after
                                 else "saved schematic unchanged")
            if row["outcome"] == "ok":
                break
            run.focus_canvas()
        row["attempts"] = tries
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    return row


def schematic_tools(run: Run) -> list[dict]:
    rows: list[dict] = []
    ensure_block(run, "blk_sheet")
    block_sch = run.blocks / "blk_sheet.kicad_block" / "blk_sheet.kicad_sch"
    cx, cy = run.centre()
    place = SCH_DRAW + "placeDesignBlock"

    def corner_to_corner() -> None:
        # the first click makes the placement tool hand over to the sheet tool, the next two are the sheet's corners
        run.click_at(cx - 150, cy - 100)
        time.sleep(1.0)
        run.click_at(cx - 150, cy - 100)
        run.click_at(cx + 150, cy + 100)

    def options(group: bool, sheet: bool):
        def setup():
            run.set_check("Place as group", group)
            run.set_check("Place as sheet", sheet)
            run.pick("blk_sheet")
        return setup

    rows.append(tool_step(run, place, lambda: run.click_at(cx, cy), (SYMBOLS,), setup=options(False, False)))
    # a block placed as a group stays linked to its library block
    rows.append(tool_step(run, place, lambda: run.click_at(cx + 120, cy + 120), (GROUPS,), setup=options(True, False)))
    select_group = lambda: run.select_item("KOT_SCH_GROUP")  # noqa: E731
    rows.append(tool_step(run, SCH_DRAW + "saveToLinkedDesignBlock", lambda: None, (), setup=select_group, library=True))
    rows.append(tool_step(run, SCH_DRAW + "placeLinkedDesignBlock", lambda: run.click_at(cx - 120, cy + 120), (SYMBOLS,),
                          setup=select_group))
    # as a sheet: the tool draws a sheet from the block (click, click, then the sheet's own dialog)
    rows.append(tool_step(run, place, corner_to_corner, (SHEETS,), setup=options(False, True), fields={}))
    rows[-1]["action"] = SCH_DRAW + "drawSheetFromDesignBlock"
    rows.append(tool_step(run, SCH_DRAW + "importSheet", lambda: run.click_at(cx, cy - 150), (SYMBOLS,),
                          setup=options(False, False), files={"Choose Schematic": block_sch}, dialogs_first=True))
    rows.append(tool_step(run, SCH_DRAW + "importSheet", corner_to_corner, (SHEETS,),
                          setup=options(False, True), files={"Choose Schematic": run.work / "child.kicad_sch"}, fields={},
                          dialogs_first=True))
    rows[-1]["action"] = SCH_DRAW + "drawSheetFromFile"
    return rows


UNITS = r'\(lib_id "fixture-lib:TwoUnit"'
GRAPHICS = r"\((?:polyline|polygon|rectangle)"
RULE_AREAS = r"\(rule_area\b"
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="20mm" height="10mm" viewBox="0 0 20 10">'
       '<rect x="1" y="1" width="18" height="8" fill="none" stroke="black" stroke-width="0.3"/></svg>')
BUS_ENTRIES = r"\(bus_entry\b"
PINS = r'\(pin "SIG_'


def schematic_pin_tools(run: Run) -> list[dict]:
    """A sheet drawn from a file whose hierarchical labels have no pins yet, and the tools that make pins."""
    rows: list[dict] = []
    cx, cy = run.centre()
    child = run.work / "child.kicad_sch"
    run.set_check("Place as group", False)
    run.set_check("Place as sheet", True)

    def draw() -> None:
        run.click_at(cx - 150, cy - 100)
        time.sleep(1.0)
        run.click_at(cx - 150, cy - 100)
        run.click_at(cx + 150, cy + 100)

    rows.append(tool_step(run, SCH_DRAW + "importSheet", draw, (SHEETS,), files={"Choose Schematic": child},
                          fields={}, dialogs_first=True))
    rows[-1]["action"] = SCH_DRAW + "drawSheetFromFile"
    rows[-1]["pins_after_draw"] = count(run.document_text(), PINS)
    sheet_a = run.item_ids("KOT_SCH_SHEET")
    select_sheet = lambda: run.select_item("KOT_SCH_SHEET")  # noqa: E731
    rows.append(tool_step(run, SCH_DRAW + "autoplaceAllSheetPins", lambda: None, (PINS,), setup=select_sheet))

    def draw_second() -> None:
        run.click_at(cx + 200, cy - 100)
        time.sleep(1.0)
        run.click_at(cx + 200, cy - 100)
        run.click_at(cx + 350, cy)

    rows.append(tool_step(run, SCH_DRAW + "importSheet", draw_second, (SHEETS,), files={"Choose Schematic": child},
                          fields={}, dialogs_first=True))
    rows[-1]["action"] = SCH_DRAW + "drawSheetFromFile"
    second = [i for i in run.item_ids("KOT_SCH_SHEET") if i not in sheet_a]

    def edge(y: int) -> None:
        run.click_at(cx + 350, cy + y)
        time.sleep(0.8)
        run.click_at(cx + 350, cy + y)

    rows.append(tool_step(run, SCH_DRAW + "placeSheetPin", lambda: edge(-50), (PINS,),
                          setup=lambda: hotkeys.select(run.session, second[0])))
    rows.append(tool_step(run, SCH_DRAW + "syncSheetPins", lambda: None, (), setup=select_sheet, window=True))
    rows.append(tool_step(run, SCH_DRAW + "syncAllSheetsPins", lambda: None, (), setup=select_sheet, window=True))
    return rows


def schematic_drawing_tools(run: Run) -> list[dict]:
    from kicad_mcp.ipc import api

    rows: list[dict] = []
    cx, cy = run.centre()
    # a bus with a labelled range: the tool offers its members, then draws a wire from the chosen one
    bus = "aaaaaaaa-0000-4000-8000-000000000001"
    zoom = "common.Control.zoomFitSelection"

    def at_bus() -> None:
        # an edit makes the editor rebuild its connectivity, so the bus knows its members
        api.call(run.session, "PlaceSymbolFromLibrary", {
            "lib_id": {"library_nickname": "fixture-lib", "entry_name": "LED"},
            "position": {"x_nm": 40_000_000, "y_nm": 40_000_000}, "orientation": "SSO_0", "unit": {"unit": 1}})
        time.sleep(2)
        hotkeys.select(run.session, bus)
        run.press(zoom)
        time.sleep(1.5)
        run.move_to(cx, cy)
        time.sleep(0.5)

    def unfold() -> None:
        row_ok = run.choose_popup_item(1)
        run.notes.append(f"bus member menu answered: {row_ok}")
        run.click_at(cx + 40, cy + 40)
        run.press("common.Interactive.finish")  # the wire ends on the finish key
        time.sleep(1.0)

    rows.append(tool_step(run, "eeschema.InteractiveDrawingLineWireBus.unfoldBus", unfold, (BUS_ENTRIES,), setup=at_bus, presave=False, attempts=1))
    # the next unit of a multi-unit symbol: place unit 1 from the library, select it, ask for the next
    before_ids = run.item_ids("KOT_SCH_SYMBOL")
    api.call(run.session, "PlaceSymbolFromLibrary", {
        "lib_id": {"library_nickname": "fixture-lib", "entry_name": "TwoUnit"},
        "position": {"x_nm": 30_000_000, "y_nm": 30_000_000}, "orientation": "SSO_0", "unit": {"unit": 1}})
    placed = [i for i in run.item_ids("KOT_SCH_SYMBOL") if i not in before_ids]
    rows.append(tool_step(run, SCH_DRAW + "placeNextSymbolUnit", lambda: run.click_at(cx, cy + 60), (UNITS,),
                          setup=lambda: hotkeys.select(run.session, placed[0])))
    # an outline is finished by its own action: three corners, then close
    def outline() -> None:
        for x, y in ((cx - 200, cy - 200), (cx - 120, cy - 200), (cx - 160, cy - 140)):
            run.click_at(x, y)
        run.press(SCH_DRAW + "closeOutline")
        time.sleep(1.0)

    rows.append(tool_step(run, SCH_DRAW + "drawRuleArea", outline, (RULE_AREAS,)))
    rows[-1]["action"] = SCH_DRAW + "closeOutline"
    svg = run.out / "fixture.svg"
    svg.write_text(SVG, encoding="utf-8")
    rows.append(tool_step(run, "eeschema.EditorControl.importGraphics", lambda: run.click_at(cx - 250, cy + 150),
                          (GRAPHICS,), forms={"Import Vector Graphics File": {"File:": str(svg)}}, dialogs_first=True))
    return rows


def ensure_block(run: Run, name: str, ns: str = SCH, kind: str = "KOT_SCH_SYMBOL") -> None:
    """A block named ``name`` holding the first item of a kind, saved the way a person does."""
    if (run.blocks / f"{name}.kicad_block").exists():
        return
    run.select_item(kind)
    run.pick(LIBRARY)
    windows = set(hotkeys._visible(run.session))
    run.press(ns + "saveSelectionAsDesignBlock")
    time.sleep(2.0)
    run.settle(windows, {"Name:": name})


# ---- driver --------------------------------------------------------------------------------------

SCH_CONTROL = [SCH + n for n in ("saveSheetAsDesignBlock", "saveSelectionAsDesignBlock",
                                  "updateDesignBlockFromSelection", "updateDesignBlockFromSheet",
                                  "deleteDesignBlock")]
# Preferences a suite needs set before the editor starts: its sheets keep their pins unplaced, so the pin tools
# have labels to place.
SETTINGS = {"schematic_pin_tools": {"eeschema.json": {"meta": {"version": 4},
                                                         "drawing": {"auto_place_sheet_pins": False}}}}
PCB_CONTROL = [PCB + n for n in ("saveBoardAsDesignBlock", "saveSelectionAsDesignBlock",
                                  "updateDesignBlockFromSelection", "updateDesignBlockFromBoard", "deleteDesignBlock")]
FOOTPRINTS, BOARD_GROUPS = r'\n\t\(footprint "', r"\n\t\(group\b"
BOARD_GRAPHICS = r"\n\t\(gr_(?:poly|line|rect)\b"
PCB_DRAW = "pcbnew.InteractiveDrawing."


def board_tools(run: Run) -> list[dict]:
    rows: list[dict] = []
    ensure_block(run, "blk_sel", PCB, "KOT_PCB_FOOTPRINT")
    cx, cy = run.centre()
    place = PCB_DRAW + "placeDesignBlock"

    def options(group: bool):
        def setup():
            run.set_check("Place as group", group)
            run.pick("blk_sel")
        return setup

    rows.append(tool_step(run, place, lambda: run.click_at(cx, cy), (FOOTPRINTS,), setup=options(False)))
    svg = run.out / "fixture.svg"
    svg.write_text(SVG, encoding="utf-8")
    rows.append(tool_step(run, PCB_DRAW + "placeImportedGraphics", lambda: run.click_at(cx - 250, cy - 150),
                          (BOARD_GRAPHICS,), forms={"Import Vector Graphics File": {"File:": str(svg)}},
                          dialogs_first=True))
    return rows


def _footprint_positions(run: Run, ids: list[str]) -> dict[str, tuple[int, int]]:
    from kicad_mcp.ipc import api

    items = api.call(run.session, "GetItemsById", {"items": [{"value": i} for i in ids]})["response"]["items"]
    return {i["id"]["value"]: (int(i["position"].get("x_nm", 0)), int(i["position"].get("y_nm", 0))) for i in items}


def _move_footprint(run: Run, ident: str, dx: int) -> None:
    from kicad_mcp.ipc import api

    item = api.call(run.session, "GetItemsById", {"items": [{"value": ident}]})["response"]["items"][0]
    pos = item["position"]
    item["position"] = {"x_nm": str(int(pos.get("x_nm", 0)) + dx), "y_nm": pos.get("y_nm", "0")}
    api.call(run.session, "UpdateItems", {"items": [item]})
    time.sleep(1.0)


def _select_only(run: Run, ident: str) -> list[str]:
    """Select one item through the API and read the editor's selection back (retrying), so a tool is only
    pressed when the item is the whole selection."""
    from kicad_mcp.ipc import api

    selected: list[str] = []
    for _ in range(4):
        run.focus_canvas()
        hotkeys.select(run.session, ident)
        time.sleep(1.0)
        items = api.call(run.session, "GetSelection", {})["response"].get("items", [])
        selected = [i.get("id", {}).get("value") for i in items]
        if selected == [ident]:
            break
    return selected


def _texts(run: Run) -> list[str]:
    """The texts on the editor window (status bar, info bar) as UI Automation sees them."""
    try:
        controls = run.uia.call("state", {"window": run.window, "selector": {
            "by": "path", "name": "*", "name_match": "any", "control_type": "Text"}})["controls"]
        return sorted({c["name"].strip() for c in controls if c["name"].strip()})
    except Exception as exc:  # noqa: BLE001
        return [f"unreadable ({type(exc).__name__})"]


def _library_offsets(run: Run, name: str) -> list[str]:
    """The footprint positions (the ``(at x y`` line of each footprint) in a block's board file."""
    text = (run.blocks / f"{name}.kicad_block" / f"{name}.kicad_pcb").read_text(encoding="utf-8")
    return re.findall(r"\(translate ([-0-9.]+ [-0-9.]+)\)", text)


def board_linked_tools(run: Run) -> list[dict]:
    """Linked design blocks on the board. The board places a block as a group only when the block holds at
    least two items (pcb_control.cpp: ``groupableCount >= 2``), so the block is three footprints. The group
    stays linked to its block; each tool is then proven against a footprint moved inside the group through
    the API: apply-layout puts it back where the block has it, save-to-linked writes the move into the
    library, and place-linked puts another copy of the block on the board. A control (the move left alone,
    key not pressed) must show no such change."""
    from kicad_mcp.ipc import api

    rows: list[dict] = []
    cx, cy = run.centre()
    delta = 8_000_000
    ids = run.item_ids("KOT_PCB_FOOTPRINT")
    api.call(run.session, "ClearSelection", {})
    api.call(run.session, "AddToSelection", {"items": [{"value": i} for i in ids[:3]]})
    run.pick(LIBRARY)
    windows = set(hotkeys._visible(run.session))
    run.press(PCB + "saveSelectionAsDesignBlock")
    time.sleep(2.0)
    run.settle(windows, {"Name:": "blk_three"})
    run.save()
    base_offsets = _library_offsets(run, "blk_three")

    run.set_check("Place as group", True)
    run.pick("blk_three")
    before_fp, before_groups = set(run.item_ids("KOT_PCB_FOOTPRINT")), set(run.item_ids("KOT_PCB_GROUP"))
    place = _row("(setup) " + PCB_DRAW + "placeDesignBlock as group", "no_effect", "", route="tool-driver-file")
    windows = set(hotkeys._visible(run.session))
    run.press(PCB_DRAW + "placeDesignBlock")
    time.sleep(1.5)
    run.click_at(cx + 150, cy + 100)
    time.sleep(2.0)
    place["dialogs"], _ = run.settle(windows)
    hotkeys.press(run.canvas, hotkeys.VK_ESCAPE)
    time.sleep(1.0)
    rows.append(place)
    groups = [g for g in run.item_ids("KOT_PCB_GROUP") if g not in before_groups]
    members = [f for f in run.item_ids("KOT_PCB_FOOTPRINT") if f not in before_fp]
    place["group"], place["members"] = groups, members
    place.update(outcome="ok" if groups and len(members) >= 3 else "no_effect",
                 detail=f"placed as a group: {len(groups)} new group, {len(members)} new footprints")
    if not groups or len(members) < 2:
        return rows
    group, member = groups[0], members[0]

    # apply layout: the member moved away from the block's layout is put back
    row = _row(PCB_DRAW + "applyDesignBlockLayout", "no_effect", "", route="tool-driver-file")
    try:
        at_block = _footprint_positions(run, [member])[member]
        _move_footprint(run, member, delta)
        moved = _footprint_positions(run, [member])[member]
        row["selection"] = _select_only(run, group)
        control = _footprint_positions(run, [member])[member]  # selecting the group alone changes nothing
        windows = set(hotkeys._visible(run.session))
        run.press(PCB_DRAW + "applyDesignBlockLayout")
        time.sleep(3.0)
        row["dialogs"], _ = run.settle(windows)
        row["notes"], run.notes = run.notes, []
        row["texts"] = _texts(run)
        row["all_members"] = _footprint_positions(run, members)
        applied = _footprint_positions(run, [member])[member]
        row["positions"] = {"at_block": at_block, "moved": moved, "control": control, "after_apply": applied}
        if moved != at_block and control == moved and applied != moved and abs(applied[0] - at_block[0]) < 1000:
            row.update(outcome="ok", detail=f"member moved {delta} nm off the block layout stayed there with the group "
                                            f"selected (control), then the apply key put it back: {moved} -> {applied}")
        else:
            row["detail"] = f"member {at_block} -> moved {moved} -> control {control} -> after key {applied}"
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    rows.append(row)

    # save to the linked block: the member's move is written into the library's board file
    row = _row(PCB_DRAW + "saveToLinkedDesignBlock", "no_effect", "", route="tool-driver-file")
    try:
        _move_footprint(run, member, delta)
        row["selection"] = _select_only(run, group)
        control = _library_offsets(run, "blk_three")  # the move alone does not reach the library
        control_digest = _digest(run.blocks)
        windows = set(hotkeys._visible(run.session))
        run.press(PCB_DRAW + "saveToLinkedDesignBlock")
        time.sleep(3.0)
        row["dialogs"], _ = run.settle(windows)
        row["notes"], run.notes = run.notes, []
        after_offsets = _library_offsets(run, "blk_three")
        after_digest = _digest(run.blocks)
        row["offsets"] = {"base": base_offsets, "control": control, "after": after_offsets}
        rewritten = sorted(f for f in after_digest if control_digest.get(f) not in (None, after_digest[f]))
        if control == base_offsets and after_offsets != base_offsets and rewritten:
            row.update(outcome="ok", detail=f"the library's board file {rewritten} holds the member's move after the key "
                                            f"(footprint positions {base_offsets} -> {after_offsets}); unchanged before it")
        else:
            row["detail"] = f"library positions base {base_offsets} control {control} after {after_offsets}"
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    rows.append(row)

    # place linked: another copy of the block (three footprints and a linked group) lands on the board
    row = _row(PCB_DRAW + "placeLinkedDesignBlock", "no_effect", "", route="tool-driver-file")
    try:
        row["selection"] = _select_only(run, group)
        fp_before, groups_before = len(run.item_ids("KOT_PCB_FOOTPRINT")), len(run.item_ids("KOT_PCB_GROUP"))
        windows = set(hotkeys._visible(run.session))
        run.press(PCB_DRAW + "placeLinkedDesignBlock")
        time.sleep(1.5)
        run.click_at(cx - 150, cy + 100)
        time.sleep(2.0)
        row["dialogs"], _ = run.settle(windows)
        for _ in range(2):
            hotkeys.press(run.canvas, hotkeys.VK_ESCAPE)
            time.sleep(0.4)
        fp_after, groups_after = len(run.item_ids("KOT_PCB_FOOTPRINT")), len(run.item_ids("KOT_PCB_GROUP"))
        row["counts"] = {"footprints": [fp_before, fp_after], "groups": [groups_before, groups_after]}
        detail = f"footprints {fp_before} -> {fp_after}, groups {groups_before} -> {groups_after}"
        if fp_after - fp_before >= 3 and groups_after == groups_before + 1:
            row.update(outcome="ok", detail=detail)
        else:
            row["detail"] = detail
    except Exception as exc:  # noqa: BLE001
        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
    rows.append(row)
    return rows


SUITES = {
    "pcbnew": [(board_block_control, PCB_CONTROL),
               (board_tools, [PCB + "saveSelectionAsDesignBlock", PCB_DRAW + "placeDesignBlock",
                              PCB_DRAW + "placeImportedGraphics"]),
               (board_linked_tools, [PCB + "saveSelectionAsDesignBlock", PCB_DRAW + "placeDesignBlock",
                                     PCB_DRAW + "saveToLinkedDesignBlock", PCB_DRAW + "placeLinkedDesignBlock",
                                     PCB_DRAW + "applyDesignBlockLayout"])],
    "eeschema": [(schematic_drawing_tools, ["common.Control.zoomFitSelection", "common.Interactive.finish", "eeschema.InteractiveDrawingLineWireBus.unfoldBus",
                                            SCH_DRAW + "placeNextSymbolUnit", SCH_DRAW + "drawRuleArea",
                                            SCH_DRAW + "closeOutline", "eeschema.EditorControl.importGraphics"]),
                 (schematic_block_control, SCH_CONTROL),
                 (schematic_pin_tools, [SCH_DRAW + n for n in ("importSheet", "placeSheetPin", "autoplaceAllSheetPins",
                                                               "syncSheetPins", "syncAllSheetsPins")]),
                 (schematic_tools, [SCH + "saveSelectionAsDesignBlock", SCH_DRAW + "placeDesignBlock",
                                    SCH_DRAW + "placeLinkedDesignBlock", SCH_DRAW + "saveToLinkedDesignBlock",
                                    SCH_DRAW + "importSheet"])],
}


CHAINED = {"board_linked_tools"}


@cleanup_after_call
def sweep(inventory: dict, project: Path, editor: str, only: str | None = None) -> dict:
    rows: list[dict] = []
    for suite, actions in SUITES[editor]:
        if only and only not in suite.__name__:
            continue
        # a suite whose steps depend on each other (the linked-block chain) is run again, in a fresh editor, when
        # a step fails for lack of input (the canvas dropped the placing click); every attempt is kept in the rows
        tries = 3 if suite.__name__ in CHAINED else 1
        for attempt in range(1, tries + 1):
            run = Run(inventory, project, editor, actions, SETTINGS.get(suite.__name__))
            part: list[dict] = []
            try:
                run.warm_up()
                run.open_panel()
                part = suite(run)
            except Exception as exc:  # noqa: BLE001
                part = [_row(suite.__name__, "error", f"{type(exc).__name__}: {exc}")]
            finally:
                run.close()
            for row in part:
                row["suite_attempt"] = attempt
            rows += part
            if all(r["outcome"] == "ok" for r in part):
                break
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok" and not r["action"].startswith("(setup)")})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": f"designblocks-{editor}", "passed": passed,
            "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--editor", choices=sorted(SUITES), required=True)
    parser.add_argument("--only", help="run only the suites whose function name contains this")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.editor, args.only)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:160], row.get("dialogs", ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
