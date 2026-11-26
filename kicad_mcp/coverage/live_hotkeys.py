"""Live sweep of editor actions RunAction cannot reach, through private-profile hotkeys.

For each batch of up to len(hotkeys.KEYS) actions of one editor: write the session profile's
user.hotkeys (one plain key per action, none for every other action), start the editor on a private
desktop, press each key on its canvas, and record what the action visibly did (a window it opened, a
document change, a canvas change). An action counts as verified only with an observed effect; one that
ran with nothing observable is reported ``no_effect`` and stays unverified.

    python -m kicad_mcp.coverage.live_hotkeys --inventory inv.json --project <dir> --editor eeschema --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import json
from pathlib import Path
import time

from kicad_mcp.coverage import clipboard
from kicad_mcp.coverage.live_actions import EXTERNAL
from kicad_mcp.ipc import hotkeys
from kicad_mcp.ipc.gui_session import GuiSession

FRAMES = {"eeschema": ("*.kicad_sch", "Schematic Editor"), "gerbview": ("gerber", "Gerber Viewer"),
          "plEditor": ("wks", "Drawing Sheet Editor"), "kicad": ("*.kicad_pro", "KiCad"),
          "3DViewer": ("*.kicad_pcb", "3D Viewer"), "cvpcb": ("*.kicad_sch", "Assign Footprints"),
          "fpedit": ("*.kicad_pcb", "Footprint Editor"), "symedit": ("*.kicad_sch", "Symbol Editor"),
          "sim": ("*.kicad_sch", "Simulator"), "pcbnew": ("*.kicad_pcb", "PCB Editor")}
# Frames opened from a host editor's menu: frame -> (host title, menu label that opens it, action prefix)
HOSTED = {"fpedit": ("PCB Editor", "footprint editor", "pcbnew."),
          "symedit": ("Schematic Editor", "symbol editor", "eeschema."),
          "sim": ("Schematic Editor", "simulator", "eeschema.")}
SETTLE = {"3DViewer": 2.5, "kicad": 1.5, "cvpcb": 1.2}  # seconds an action needs to show (3D views animate)
ZOOM_SELECTION = "common.Control.zoomFitSelection"
ENTER_SHEET = "eeschema.NavigateTool.enterSheet"
OPENER = "eeschema.EditorControl.assignFootprints"  # opens the footprint assigner from the schematic
MINIMAL_WKS = """(kicad_wks (version 20231118) (generator "kicad-ai-tools")
  (setup (textsize 1.5 1.5) (linewidth 0.15) (textlinewidth 0.15))
  (rect (name "") (start 10 10) (end 20 20))
)
"""


FRAMES_TITLE = {"schematic": "Schematic Editor", "pcb": "PCB Editor"}
# Item kinds an action may need selected, tried in order until the action shows an effect.
SELECTIONS = {"schematic": [None, "KOT_SCH_SYMBOL", "KOT_SCH_LINE", "KOT_SCH_GLOBAL_LABEL"],
              "pcb": [None, "KOT_PCB_FOOTPRINT", "KOT_PCB_TRACE", "KOT_PCB_ZONE"]}
# Actions that act on the item under the cursor (net highlight, next net item, ...): the item is selected,
# the view is centred on it and the mouse is moved to the middle of the canvas, over the item.
CURSOR_SELECTIONS = {"schematic": ["KOT_SCH_LINE@cursor", "KOT_SCH_SYMBOL@cursor"],
                     "pcb": ["KOT_PCB_TRACE@cursor", "KOT_PCB_FOOTPRINT@cursor"]}
WM_MOUSEMOVE = 0x0200


# Keys a person presses first for an action to have something to act on (unmeasured), e.g. a net must be
# highlighted before "clear highlight" or "next net item" can do anything.
PRESTEPS = {
    "eeschema.InteractiveSelection.SelectNode": ["<escape>"],  # nothing selected: the action selects the node
    "eeschema.EditorControl.clearHighlight": ["eeschema.EditorControl.highlightNet"],
    "eeschema.EditorControl.nextNetItem": ["eeschema.EditorControl.showNetNavigator", "eeschema.EditorControl.highlightNet"],
    "eeschema.EditorControl.previousNetItem": ["eeschema.EditorControl.showNetNavigator",
                                                "eeschema.EditorControl.highlightNet"],
}


def _place_cursor_on_selection(session, canvas: int) -> None:
    """Zoom to the selection (it ends up centred) and park the mouse in the middle of the canvas."""
    import contextlib

    from kicad_mcp.ipc import api

    if getattr(session, "zoom_key", None):  # schematic: no RunAction handler, the action has a reserved key
        hotkeys.press(canvas, session.zoom_key)
    else:
        with contextlib.suppress(Exception):
            api.call(session, "RunAction", {"action": ZOOM_SELECTION})
    time.sleep(0.8)
    # A posted mouse *move* alone leaves KiCad's cursor elsewhere, but a posted click lands exactly where it
    # is sent (measured on the board: a click at the canvas middle after zoom-fit selects that item), so the
    # item is clicked: this also puts the cursor on it.
    from kicad_mcp.coverage.live_tools import click

    rect = hotkeys._Rect()
    hotkeys.user32.GetClientRect(canvas, hotkeys.ctypes.byref(rect))
    cx, cy = rect.right // 2, rect.bottom // 2
    # KiCad re-reads the screen cursor when it handles the key; inside this session's process only, it is
    # told the mouse stays where the sweep put it (cursor_hook). Without Frida the posted moves are all.
    if not hasattr(session, "cursor_hook"):
        try:
            from kicad_mcp.coverage.cursor_hook import CursorHook

            session.cursor_hook = CursorHook(session.pid)
        except Exception:  # noqa: BLE001
            session.cursor_hook = None
    if session.cursor_hook:
        session.cursor_hook.place(canvas, cx, cy)
    # The canvas follows the mouse only once it has seen it move (measured in the drawing-sheet editor: a
    # first click without earlier motion lands where the cursor last was), so the mouse travels in first.
    for step in range(10, 0, -1):
        hotkeys.user32.PostMessageW(canvas, 0x0200, 0, ((cy - 8 * step) << 16) | (cx - 12 * step))  # WM_MOUSEMOVE
        time.sleep(0.08)
    click(canvas, cx, cy)
    time.sleep(0.4)


def _input_for(editor: str, project: Path, scratch: Path) -> Path:
    """The file each frame opens: a document of the test project, or one made from it."""
    import subprocess
    import sys

    pattern = FRAMES[editor][0]
    if pattern == "gerber":
        from kicad_mcp.ipc.session import find_kicad_cli

        out = scratch / "gerbers"
        subprocess.run([find_kicad_cli(), "pcb", "export", "gerbers", "-o", str(out) + "/",
                        str(next(project.glob("*.kicad_pcb")))], capture_output=True, timeout=300,
                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        return max(out.glob("*F_Cu*"), key=lambda f: f.stat().st_size)
    if pattern == "wks":
        path = scratch / "coverage.kicad_wks"
        path.write_text(MINIMAL_WKS, encoding="utf-8")
        return path
    for pro in project.glob("*.kicad_pro"):  # a project's own root sheet, not whichever sub-sheet sorts first
        own = project / (pro.stem + pattern[1:])
        if own.exists():
            return own
    return next(project.glob(pattern))


def _start(editor: str, document: Path, all_actions: list[str], batch: list[str], enter_sheet: bool = False,
           library_tree: bool = False, common_settings: dict | None = None):
    from kicad_mcp.ipc import api
    from kicad_mcp.ipc.gui_session import FrameSession

    title = FRAMES[editor][1]
    if editor in ("gerbview", "plEditor", "kicad"):
        exe = {"gerbview": "gerbview.exe", "plEditor": "pl_editor.exe", "kicad": "kicad.exe"}[editor]
        session = FrameSession(document, exe)
    else:
        session = GuiSession(document)
    config = session.profile / "config" / session._settings_version()
    extra = [OPENER] if editor == "cvpcb" else []
    if editor == "eeschema" and len(batch) < len(hotkeys.KEYS) and ZOOM_SELECTION not in batch:
        extra = [ZOOM_SELECTION, ENTER_SHEET]  # the schematic editor has no RunAction handler, so zoom by key
    extra += [p for name in batch for p in PRESTEPS.get(name, [])
              if not p.startswith("<") and p not in batch and p not in extra]
    assigned = hotkeys.write_hotkeys(config, all_actions, batch + extra)
    if common_settings:  # e.g. the program "Edit in a Text Editor" uses, set in the session's own profile
        import json as _json

        path = config / "kicad_common.json"
        # the profile's own defaults (API server on, no first-run prompts) are kept; see hidden_desktop
        settings = _json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
            "api": {"enable_server": True},
            "do_not_show_again": {"data_collection_prompt": True, "update_check_prompt": True},
            "meta": {"version": 3}}
        for dotted, value in common_settings.items():
            *parents, leaf = dotted.split(".")
            node = settings
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = value
        path.write_text(_json.dumps(settings, indent=2), encoding="utf-8")
    session.start()
    session.zoom_key = assigned[ZOOM_SELECTION][1] if ZOOM_SELECTION in assigned else None
    session.enter_key = assigned[ENTER_SHEET][1] if ENTER_SHEET in assigned else None
    if editor == "3DViewer":  # the 3D viewer is opened from the board editor
        api.call(session, "RunAction", {"action": "common.Control.show3DViewer"})
        time.sleep(3)
    if editor == "cvpcb":  # the footprint assigner is opened from the schematic editor
        sch_frame, _ = hotkeys.canvas_of(session, "Schematic Editor")
        hotkeys.settled_capture(sch_frame, timeout_s=15.0)
        _, sch_canvas = hotkeys.canvas_of(session, "Schematic Editor")
        hotkeys.press(sch_canvas, assigned[OPENER][1])
        time.sleep(4)
    if editor in HOSTED:
        open_hosted(session, editor)
    if editor == "pcbnew":  # every inner copper layer exists, so each layer action has its layer
        import contextlib

        from kicad_mcp.coverage.live_actions import enable_all_copper

        with contextlib.suppress(Exception):
            enable_all_copper(session)
    frame, _ = hotkeys.canvas_of(session, title)
    hotkeys.settled_capture(frame, timeout_s=15.0)  # the editor's first paint and layout have finished
    frame, canvas = hotkeys.canvas_of(session, title)  # the drawing panel, once laid out
    if library_tree:
        _select_library_row(session, frame)
    if enter_sheet:  # work inside a child sheet, so navigation has somewhere to go
        _enter_child_sheet(session, frame)
        frame, canvas = hotkeys.canvas_of(session, title)
        hotkeys.settled_capture(frame, timeout_s=15.0)
    return session, assigned, frame, canvas


def _enter_child_sheet(session, frame: int) -> None:
    """Go to the first child sheet through the Schematic Hierarchy panel, as a person does: its tree
    takes Down (to the child row) and Return."""
    trees = [c for c in hotkeys._children(frame) if "TreeView" in c[1] and c[3]]
    if not trees:
        raise RuntimeError("--enter-sheet: no hierarchy tree in the schematic editor")
    tree = max(trees, key=lambda c: c[2])[0]
    for vk in (0x28, 0x0D):
        hotkeys.press(tree, vk)
        time.sleep(0.5)
    time.sleep(3)


def _select_library_row(session, frame: int) -> None:
    """Select a row of the library tree (symbol or footprint editor), as a person does: focus the tree
    and press Down, so tree actions have a library item to act on."""
    classes = sorted({c[1] for c in hotkeys._children(frame) if c[3]})
    trees = [c for c in hotkeys._children(frame) if c[3] and ("DataView" in c[1] or "TreeView" in c[1])]
    if not trees:
        raise RuntimeError(f"--tree: no library tree among {classes}")
    tree = max(trees, key=lambda c: c[2])[0]
    hotkeys.user32.SetFocus(tree)
    for vk in (0x24, 0x27, 0x28, 0x28):  # Home (first library), Right (expand), Down, Down (its second symbol)
        hotkeys.press(tree, vk)
        time.sleep(0.5)


def open_hosted(session, editor: str) -> None:
    """Open a hosted frame the way a person does (the host editor's own menu item); a library editor
    then opens one of the project's own library items, so its actions have something to act on."""
    from kicad_mcp.coverage.live_payloads import Context, _lib_id
    from kicad_mcp.ipc import api

    host_title, label, _ = HOSTED[editor]
    host, _ = hotkeys.canvas_of(session, host_title)
    opener = next((i for i in hotkeys.menu_items(host) if i["label"] == label), None)
    if opener is None:
        raise RuntimeError(f"no '{label}' menu item in the {host_title}")
    hotkeys.user32.PostMessageW(host, hotkeys.WM_COMMAND, opener["id"], 0)
    time.sleep(5)
    if editor in ("fpedit", "symedit"):
        kind = "footprint" if editor == "fpedit" else "symbol"
        payload = {"type": "DOCTYPE_FOOTPRINT" if kind == "footprint" else "DOCTYPE_SYMBOL",
                   "identifier": _lib_id(Context(session), kind)}
        for attempt in range(6):  # the library editor's API handler registers a little after its window shows
            try:
                api.call(session, "OpenLibraryItem", payload)
                break
            except Exception:  # noqa: BLE001
                if attempt == 5:
                    raise
                time.sleep(4)
        time.sleep(2)


def _first_of(session, kind: str | None) -> str | None:
    if kind is None:
        return None
    from kicad_mcp.ipc import api

    try:
        items = api.call(session, "GetItems", {"types": [kind]})["response"].get("items", [])
    except Exception:  # noqa: BLE001
        return None
    return next((i["id"]["value"] for i in items if i.get("id", {}).get("value")), None)


@cleanup_after_call
def sweep(inventory: dict, project: Path, editor: str, start: int = 0, count: int | None = None,
          only: list[str] | None = None, with_cursor: bool = False, enter_sheet: bool = False,
          library_tree: bool = False) -> dict:

    document = _input_for(editor, project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-frames-")))
    all_actions = [a["name"] for a in inventory["actions"]]
    prefix = HOSTED[editor][2] if editor in HOSTED else f"{editor}."
    # Every frame also takes the shared common.* actions: many act only in some frames (a library tree,
    # a plot, text), so each is tried wherever it is still unproven.
    prefixes = (prefix, "common.")
    targets = [a["name"] for a in inventory["actions"]
               if a["name"].startswith(prefixes) and a["runnable"] and not EXTERNAL.match(a["name"].rsplit(".", 1)[-1])]
    if only:
        targets = [t for t in targets if t in set(only)]
    targets = targets[start:None if count is None else start + count]
    rows: list[dict] = []
    size = len(hotkeys.KEYS)
    for first in range(0, len(targets), size):
        batch = targets[first:first + size]
        pending = list(batch)
        while pending:
            session, assigned, frame, canvas = _start(editor, document, all_actions, batch, enter_sheet, library_tree)
            kinds = SELECTIONS.get(getattr(session, "kind", ""), [None]) if editor in ("eeschema", "pcbnew") else [None]
            if with_cursor and editor in ("eeschema", "pcbnew"):
                kinds = CURSOR_SELECTIONS.get(getattr(session, "kind", ""), [None])
            try:
                while pending:
                    name = pending.pop(0)
                    if not session.kicad.alive():
                        rows.append({"action": name, "outcome": "error", "detail": "editor exited", "clipboard_restored": None})
                        break
                    row = {"action": name, "outcome": "no_effect", "key": assigned[name][0], "detail": ""}
                    for kind in kinds:  # nothing selected first, then one item of each kind
                        hotkeys.select(session, _first_of(session, (kind or "").split("@")[0] or None))
                        if kind and kind.endswith("@cursor"):
                            _place_cursor_on_selection(session, canvas)
                        for first in PRESTEPS.get(name, []):
                            hotkeys.press(canvas, hotkeys.VK_ESCAPE if first == "<escape>" else assigned[first][1])
                            time.sleep(1.0)
                        with clipboard.preserved() as clip:  # the owner's clipboard text is restored on exit
                            observed = hotkeys.dispatch(session, canvas, assigned[name][1], frame,
                                                        settle_s=SETTLE.get(editor, 0.8))
                            clip.capture()
                        if clip.changed and (row.get("clipboard_restored") is not False):  # any failed restore sticks
                            row["clipboard_restored"] = clip.restored
                        if clip.new_content and clip.restored is True:
                            kind_seen = "image" if clip.has_image else "text"
                            observed = {**observed, "effects": [*observed["effects"],
                                        f"clipboard written ({kind_seen}, sequence {clip.before_sequence}->{clip.after_sequence}, owner text restored: {clip.restored})"]}
                        if observed["effects"] == ["editor window changed"] and not hotkeys.window_is_quiet(
                                session, canvas, frame, SETTLE.get(editor, 0.8)):  # pixels alone, and noisy
                            observed = {"effects": [], "messages": observed["messages"]}
                            row["noisy"] = "the window also changed on a key bound to nothing"
                        if observed["effects"]:
                            row.update(outcome="ok", detail="; ".join(observed["effects"]),
                                       selected=kind or "nothing")
                            break
                        if not session.healthy():
                            break
                    hotkeys.select(session, None)
                    rows.append(row)
                    if not session.healthy():
                        rows[-1]["detail"] += "; editor restarted after"
                        break
            finally:
                session.close(discard=True)
    passed = sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--editor", choices=sorted(FRAMES), default="eeschema")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int)
    parser.add_argument("--names", help="comma list of full action names to run (and only those)")
    parser.add_argument("--enter-sheet", action="store_true", help="enter the first sheet symbol before pressing")
    parser.add_argument("--tree", action="store_true", help="select a row of the library tree first")
    parser.add_argument("--cursor", action="store_true", help="select an item, centre it and put the mouse over it")
    parser.add_argument("--only-from", type=Path, help="evidence file: retry only its non-ok actions")
    parser.add_argument("--skip-passed", type=Path, nargs="*", default=[],
                        help="evidence files whose passed actions are not run again")
    args = parser.parse_args(argv)
    only = None
    if args.only_from:
        only = [r["action"] for r in json.loads(args.only_from.read_text(encoding="utf-8"))["rows"]
                if r["outcome"] != "ok"]
    if args.skip_passed:
        passed = {i for f in args.skip_passed for i in json.loads(f.read_text(encoding="utf-8")).get("passed", [])}
        inventory_names = [a["name"] for a in json.loads(args.inventory.read_text(encoding="utf-8"))["actions"]]
        keep = [n for n in inventory_names if f"action:{n}" not in passed]
        only = [n for n in (only or keep) if n in set(keep)]
    if args.names:
        only = args.names.split(",")
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.editor,
                   args.start, args.count, only, with_cursor=args.cursor, enter_sheet=args.enter_sheet, library_tree=args.tree)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
