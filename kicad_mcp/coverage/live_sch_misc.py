"""Live proofs for assorted schematic-editor actions, each read back from the saved sheet or the editor.

One KiCad GUI session (private desktop) serves a small schematic this module writes for the case. The action
is triggered by its private-profile hotkey (or the way a person reaches it), and the result is read back from
the saved .kicad_sch (the editor saves through its own Save key), the API, or UI Automation. A control run
without the action (an unbound key in its place) gives the reference the readback must differ from.

    python -m kicad_mcp.coverage.live_sch_misc --inventory inv.json --out ev.json [--only a,b]
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from kicad_mcp.coverage.fixtures import demos_dir, fixture
from kicad_mcp.coverage.live_board_misc import Env, _dump, _row
from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import GuiSession

E = "eeschema.EditorControl."
SAVE = "common.Control.save"
ZOOM_SEL = "common.Control.zoomFitSelection"
ZOOM_FIT = "common.Control.zoomFitScreen"
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
TITLE = "Schematic Editor"
PROJECT = "t"


# ---- a schematic written by hand ------------------------------------------------------------------------

def _u() -> str:
    return str(uuid.uuid4())


FONT = "(effects (font (size 1.27 1.27)))"


def lib_symbol(name: str, pins: list[tuple[str, float, float, int]], body: tuple[float, float, float, float]) -> str:
    """A library symbol with the given pins ((number, x, y, angle): the connection point and the direction
    the pin points toward the body) and a rectangle body (x1, y1, x2, y2)."""
    x1, y1, x2, y2 = body
    pin_text = "\n".join(
        f'      (pin passive line (at {x} {y} {a}) (length 2.54) (name "{n}" {FONT}) (number "{n}" {FONT}))'
        for n, x, y, a in pins)
    return f"""    (symbol "{name}"
      (pin_numbers (hide yes)) (pin_names (offset 0))
      (exclude_from_sim no) (in_bom yes) (on_board yes)
      (property "Reference" "U" (at 0 {y1 + 2.54} 0) {FONT})
      (property "Value" "{name.split(':')[1]}" (at 0 {y2 - 2.54} 0) {FONT})
      (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))
      (symbol "{name.split(':')[1]}_0_1"
        (rectangle (start {x1} {y1}) (end {x2} {y2}) (stroke (width 0.254) (type default)) (fill (type background))))
      (symbol "{name.split(':')[1]}_1_1"
{pin_text})
      (embedded_fonts no))"""


def placed(lib_id: str, ref: str, x: float, y: float, pins: list[str], root: str, angle: int = 0) -> tuple[str, dict]:
    """A placed symbol; returns its text and {pin number: uuid}."""
    ids = {p: _u() for p in pins}
    pin_text = "\n".join(f'    (pin "{p}" (uuid "{ids[p]}"))' for p in pins)
    return f"""  (symbol (lib_id "{lib_id}") (at {x} {y} {angle}) (unit 1)
    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no) (uuid "{_u()}")
    (property "Reference" "{ref}" (at {x} {y - 5.08} 0) {FONT})
    (property "Value" "{lib_id.split(':')[1]}" (at {x} {y + 5.08} 0) {FONT})
    (property "Footprint" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))
    (property "Datasheet" "" (at {x} {y} 0) (effects (font (size 1.27 1.27)) (hide yes)))
{pin_text}
    (instances (project "{PROJECT}" (path "/{root}" (reference "{ref}") (unit 1)))))""", ids


def wire(x1: float, y1: float, x2: float, y2: float) -> str:
    return (f'  (wire (pts (xy {x1} {y1}) (xy {x2} {y2})) (stroke (width 0) (type default)) (uuid "{_u()}"))')


def sheet_text(root: str, libs: list[str], body: list[str]) -> str:
    return (f'(kicad_sch (version 20250114) (generator "eeschema") (generator_version "10.99")\n'
            f'  (uuid "{root}") (paper "A4")\n  (lib_symbols\n' + "\n".join(libs) + "\n  )\n"
            + "\n".join(body) + '\n  (sheet_instances (path "/" (page "1")))\n)\n')


def write_project(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{PROJECT}.kicad_pro").write_text("{}", encoding="utf-8")
    (folder / f"{PROJECT}.kicad_sch").write_text(text, encoding="utf-8")
    return folder / f"{PROJECT}.kicad_sch"


def chain_fixture(folder: Path) -> tuple[Path, dict]:
    """J1 -wire- R1 -wire- J2 on one line: the two wires are two nets bridged by the resistor, so KiCad offers
    a potential net chain from J1:1 to J2:1. Pin uuids are known, so the API can select the two pins."""
    root = _u()
    libs = [lib_symbol("T:CONN_R", [("1", 5.08, 0, 180)], (-2.54, 1.27, 2.54, -1.27)),
            lib_symbol("T:CONN_L", [("1", -5.08, 0, 0)], (-2.54, 1.27, 2.54, -1.27)),
            lib_symbol("T:RES", [("1", -5.08, 0, 0), ("2", 5.08, 0, 180)], (-2.54, 1.27, 2.54, -1.27))]
    j1, p1 = placed("T:CONN_R", "J1", 50.8, 50.8, ["1"], root)
    r1, pr = placed("T:RES", "R1", 76.2, 50.8, ["1", "2"], root)
    j2, p2 = placed("T:CONN_L", "J2", 101.6, 50.8, ["1"], root)
    j3, p3 = placed("T:CONN_R", "J3", 50.8, 101.6, ["1"], root)
    j4, p4 = placed("T:CONN_L", "J4", 101.6, 101.6, ["1"], root)
    body = [j1, r1, j2, j3, j4, wire(55.88, 50.8, 71.12, 50.8), wire(81.28, 50.8, 96.52, 50.8),
            wire(55.88, 101.6, 96.52, 101.6)]  # the last wire is a net no chain covers
    return write_project(folder, sheet_text(root, libs, body)), {"J1.1": p1["1"], "J2.1": p2["1"], "R1.1": pr["1"],
                                                                "R1.2": pr["2"], "J3.1": p3["1"], "J4.1": p4["1"]}


# ---- the session ----------------------------------------------------------------------------------------

class SchEnv(Env):
    def __init__(self, session: GuiSession, assigned: dict[str, tuple[str, int]]):
        self.s, self.assigned = session, assigned
        self.title = TITLE
        self.frame, self.canvas = hotkeys.canvas_of(session, TITLE)
        self._hook = None
        self.shots = 0

    def refresh(self) -> None:
        self.frame, self.canvas = hotkeys.canvas_of(self.s, self.title)

    def dump(self, label: str) -> None:
        folder = os.environ.get("KICAD_AI_DUMP")
        if folder:
            self.shots += 1
            with contextlib.suppress(Exception):
                _dump(Path(folder), **{f"{self.shots:02d}-{label}": self.shot()})

    def idle(self) -> None:
        for _ in range(12):
            try:
                api.call(self.s, "GetVersion", {})
                return
            except Exception:  # noqa: BLE001
                hotkeys.press(self.canvas, hotkeys.VK_ESCAPE)
                time.sleep(0.5)

    def select_ids(self, *ids: str) -> list[dict]:
        api.call(self.s, "ClearSelection", {})
        if ids:
            return api.call(self.s, "AddToSelection", {"items": [{"value": i} for i in ids]})["response"].get("items", [])
        return []

    def saved(self) -> str:
        """The sheet as the editor writes it when it saves (its own Save key)."""
        self.idle()
        stamp = self.s.document.stat().st_mtime_ns
        self.key(SAVE, 0.5)
        for _ in range(30):
            time.sleep(0.3)
            if self.s.document.stat().st_mtime_ns != stamp:
                break
        time.sleep(0.3)
        return self.s.document.read_text(encoding="utf-8")

    def click(self, x: int, y: int, double: bool = False) -> None:
        point = (y << 16) | x
        self.mouse(x, y, 0.3)
        seq = (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_LBUTTONUP) if double else (WM_LBUTTONDOWN, WM_LBUTTONUP)
        for message in seq:
            down = message != WM_LBUTTONUP
            self.cursor.button(down)
            hotkeys.user32.PostMessageW(self.canvas, message, 1 if down else 0, point)
            time.sleep(0.1)
        time.sleep(0.7)

    def uia(self):
        if getattr(self, "_uia", None) is None:
            self._uia = self.s.attach_uia()
        return self._uia

    def texts(self, window: str | None = None, control_type: str = "Text") -> list[str]:
        window = window or self.title
        out = self.uia().call("state", {"window": {"name": window, "match": "contains"},
                                        "selector": {"by": "path", "name": "*", "name_match": "any",
                                                     "control_type": control_type}})["controls"]
        return [t["name"].strip() for t in out]


def start(document: Path, keys: list[str], all_actions: list[str], settings: dict | None = None) -> SchEnv:
    session = GuiSession(document)
    config = session.profile / "config" / session._settings_version()
    assigned = hotkeys.write_hotkeys(config, all_actions, keys[:len(hotkeys.KEYS)])
    if settings:
        config.mkdir(parents=True, exist_ok=True)
        data: dict = {"meta": {"version": 5}}
        for dotted, value in settings.items():
            *parents, leaf = dotted.split(".")
            node = data
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = value
        (config / "eeschema.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    session.start()
    print('session pid', session.pid, flush=True)
    time.sleep(3)
    frame, _ = hotkeys.canvas_of(session, TITLE)
    hotkeys.settled_capture(frame, timeout_s=15.0)
    return SchEnv(session, assigned)


def net_chains(text: str) -> list[str]:
    """The (net_chain ...) blocks of a saved sheet."""
    blocks, depth, start_at = [], 0, None
    for match in re.finditer(r"[()]|\"(?:[^\"\\]|\\.)*\"", text):
        token = match.group(0)
        if token == "(":
            if depth == 0 or start_at is not None:
                pass
            depth += 1
            if text.startswith("(net_chain", match.start()):
                start_at, base = match.start(), depth
        elif token == ")":
            if start_at is not None and depth == base:
                blocks.append(re.sub(r"\s+", " ", text[start_at:match.end()]))
                start_at = None
            depth -= 1
    return blocks


# ---- cases -----------------------------------------------------------------------------------------------

def _set_text_dialog(env: SchEnv, title_part: str, value: str | None, button: str = "OK", wait: float = 15.0) -> str:
    """Complete a wx text-entry dialog (wxGetTextFromUser): type ``value`` and press OK (or Cancel)."""
    deadline = time.monotonic() + wait
    title = ""
    while time.monotonic() < deadline:
        new = {h: t for h, t in hotkeys._visible(env.s).items() if h != env.frame and title_part in t}
        if new:
            title = next(iter(new.values()))
            break
        time.sleep(0.3)
    if not title:
        return ""
    uia = env.uia()
    window = {"name": title, "match": "exact"}
    if value is not None:
        done = False
        for selector in ({"by": "path", "name": "*", "name_match": "any", "control_type": "Edit"},):
            try:
                found = uia.call("state", {"window": window, "selector": selector})["controls"]
            except Exception:  # noqa: BLE001
                found = []
            for control in found[:1]:
                uia.call("set-value", {"window": window, "value": value,
                                       "selector": {"by": "path", "name": control["name"], "control_type": "Edit"}})
                done = True
        if not done:
            raise RuntimeError(f"no text field in '{title}'")
    uia.call("invoke", {"window": window, "selector": {"by": "path", "name": button, "control_type": "Button"}})
    time.sleep(1.0)
    return title


@cleanup_after_call
def case_netchain(env: SchEnv, pins: dict) -> list[dict]:
    """createNetChainBetweenPins, nameNetChain, highlightNetChain on the potential chain J1:1 - R1 - J2:1."""
    rows = []
    NAME1, NAME2 = "CHAIN_A", "CHAIN_B"
    base = net_chains(env.saved())
    # control: two pins selected, an unbound key pressed in place of the action
    env.select_ids(pins["J1.1"], pins["J2.1"])
    selected = env.selection()
    hotkeys.press(env.canvas, hotkeys.CONTROL_KEY[1])
    time.sleep(1.0)
    control = net_chains(env.saved())
    env.dump("pins-selected")
    # the action: it zooms to the pins, highlights the potential chain and asks for the chain's name
    env.key(E + "createNetChainBetweenPins", 0.5)
    title = _set_text_dialog(env, "Create Net Chain", NAME1)
    env.dump("after-create")
    after = net_chains(env.saved())
    row = _row(E + "createNetChainBetweenPins", "no_effect", "", dialog=title, selected=len(selected),
               control=control, chains=after)
    if not base and not control and any(NAME1 in c for c in after):
        row.update(outcome="ok", detail=f"with J1:1 and J2:1 selected the key opened '{title}', accepted name {NAME1}; the "
                                         f"saved sheet then holds {after} (none before or after the unbound-key control)")
    rows.append(row)

    # nameNetChain: a pin of the chain selected; the key asks for a new name (dialog preset to the old one)
    env.select_ids(pins["J1.1"])
    hotkeys.press(env.canvas, hotkeys.CONTROL_KEY[1])
    time.sleep(0.8)
    control2 = net_chains(env.saved())
    env.key(E + "nameNetChain", 0.5)
    title2 = _set_text_dialog(env, "Name Net Chain", NAME2)
    after2 = net_chains(env.saved())
    row = _row(E + "nameNetChain", "no_effect", "", dialog=title2, control=control2, chains=after2)
    if any(NAME1 in c for c in control2) and not any(NAME2 in c for c in control2) and any(NAME2 in c for c in after2):
        row.update(outcome="ok", detail=f"with J1:1 selected the key opened '{title2}' and renamed the chain {NAME1} -> "
                                         f"{NAME2}; the saved sheet: {after2} (the control kept {NAME1})")
    rows.append(row)

    rows += _highlight_chain(env, NAME2)
    rows += _net_items(env, pins)
    return rows


def _wires(env: SchEnv) -> list[dict]:
    return sorted(api.call(env.s, "GetItems", {"types": ["KOT_SCH_LINE"]})["response"].get("items", []),
                  key=lambda w: (int(w["start"]["y_nm"]), int(w["start"]["x_nm"])))


def _centre_on(env: SchEnv, item_id: str) -> tuple[int, int]:
    """Centre the view on one item (zoom to it) and put the pointer in the middle of the canvas, over it."""
    env.select_ids(item_id)
    env.key(ZOOM_SEL, 1.0)
    env.select_ids()
    width, height = env.canvas_size()
    cx, cy = width // 2, height // 2
    for step in range(10, 0, -1):  # the canvas follows the pointer once it has seen it move
        env.mouse(cx - 12 * step, cy - 8 * step, 0.06)
    env.mouse(cx, cy, 0.8)
    return cx, cy


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = hotkeys._Rect()
    hotkeys.user32.GetWindowRect(hwnd, hotkeys.ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _sel_ids(env: SchEnv) -> list[str]:
    return [i.get("id", {}).get("value", "?") for i in env.selection()]


def _nav_tree(env: SchEnv) -> int:
    """The Net Navigator's tree (a generic wx tree: no UI Automation, but it takes posted mouse and key messages). Of the three
    left-docked panes tall enough to hold a tree, the middle one by position (the properties panel is above, the hierarchy
    below)."""
    cands = sorted((_rect(h)[1], h) for h, cls, _a, vis in hotkeys._children(env.frame)
                   if vis and cls == "wxWindowNR" and _rect(h)[0] < 20 and 300 < _rect(h)[3] - _rect(h)[1] < 500
                   and _rect(h)[1] > 700)
    if len(cands) < 3:
        raise RuntimeError(f"navigator tree not found among {cands}")
    return cands[1][1]


def _tree_click(env: SchEnv, tree: int, x: int, y: int, double: bool = False) -> None:
    point = (y << 16) | x
    env.cursor.place(tree, x, y)
    hotkeys.user32.PostMessageW(tree, 0x0200, 0, point)
    time.sleep(0.3)
    seq = (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_LBUTTONUP) if double else (WM_LBUTTONDOWN, WM_LBUTTONUP)
    for message in seq:
        down = message != WM_LBUTTONUP
        env.cursor.button(down)
        hotkeys.user32.PostMessageW(tree, message, 1 if down else 0, point)
        time.sleep(0.1)
    env.cursor.button(False)
    time.sleep(1.0)


def _who(env: SchEnv) -> tuple[str, ...]:
    """The reference designators named by the message panel: which symbol's pin is selected."""
    return tuple(t for t in env.texts() if re.search(r"\bJ\d+\b", t))


def _net_items(env: SchEnv, pins: dict) -> list[dict]:
    """With a net highlighted and the Net Navigator showing its items, the navigator's selected row is the starting point: Next
    Net Item moves it (and the canvas selection) to the following item, Previous Net Item back."""
    wire = _wires(env)[-1]["id"]["value"]  # the lone wire J3 - J4: its net is in no chain
    _centre_on(env, wire)
    env.key(E + "clearHighlight", 0.8)
    env.key(E + "showNetNavigator", 2.5)
    env.key(E + "highlightNet", 1.5)
    tree = _nav_tree(env)
    _tree_click(env, tree, 58, 83)               # the Root sheet's + box
    _tree_click(env, tree, 150, 83, double=True)  # its label (toggles again if the first click already opened it)
    for vk in (0x28, 0x27, 0x28):                # Down, Right, Down: onto the first item row
        hotkeys.press(tree, vk)
        time.sleep(0.8)
    env.select_ids(pins["J3.1"])                 # the same item on the canvas: the actions work on the canvas selection
    time.sleep(1.0)
    env.dump("navigator")
    start = _sel_ids(env)
    hotkeys.press(env.canvas, hotkeys.CONTROL_KEY[1])
    time.sleep(1.2)
    control = _sel_ids(env)
    env.key(E + "nextNetItem", 1.5)
    nxt = _sel_ids(env)
    env.dump("after-next")
    env.key(E + "previousNetItem", 1.5)
    prev = _sel_ids(env)
    env.dump("after-prev")
    detail = {"start": start, "control": control, "next": nxt, "previous": prev, "texts": env.texts()}
    rows = []
    moved = start == [pins["J3.1"]] and control == start and nxt == [pins["J4.1"]]
    rows.append(_row(E + "nextNetItem", "ok" if moved else "no_effect",
                     f"J3 pin 1 selected on the highlighted net with its navigator row current: selection {start}; an unbound key left it {control}; Next Net Item moved it to {nxt} (J4 pin 1)"
                     if moved else "", **detail))
    back = moved and prev == start
    rows.append(_row(E + "previousNetItem", "ok" if back else "no_effect",
                     f"after Next Net Item the selection was {nxt}; Previous Net Item returned it to {prev}" if back else "",
                     **detail))
    return rows


def _status(env: SchEnv) -> list[str]:
    return [t for t in env.texts() if "Net chain" in t or "Highlighted net" in t]


def _highlight_chain(env: SchEnv, chain: str) -> list[dict]:
    wire = _wires(env)[0]["id"]["value"]
    _centre_on(env, wire)
    env.key(E + "clearHighlight", 0.8)
    clean = _status(env)
    hotkeys.press(env.canvas, hotkeys.CONTROL_KEY[1])
    time.sleep(1.0)
    control = _status(env)
    env.dump("chain-control")
    env.key(E + "highlightNet", 1.2)
    plain = env.texts()
    env.key(E + "clearHighlight", 0.8)
    env.key(E + "highlightNetChain", 1.2)
    got = _status(env)
    env.dump("chain-highlighted")
    row = _row(E + "highlightNetChain", "no_effect", "", before=clean, control=control, after=got, all_texts=env.texts(), plain_highlight=plain)
    if not clean and not control and any("Net chain members" in t and "Net-(J1-Pad1)" in t and "Net-(J2-Pad1)" in t
                                         for t in got):
        row.update(outcome="ok", detail=f"with the pointer over a wire of chain {chain} the key made the status bar read "
                                         f"{got} (blank before and after the unbound-key control)")
    return [row]


def blank_fixture(folder: Path) -> tuple[Path, dict]:
    return write_project(folder, sheet_text(_u(), [], [])), {}


def _bitmaps(env: SchEnv) -> int:
    return len(api.call(env.s, "GetItems", {"types": ["KOT_SCH_BITMAP"]})["response"].get("items", []))


@cleanup_after_call
def case_ddAddImage(env: SchEnv, _extra: dict) -> list[dict]:
    """A .png dropped on the schematic editor (Explorer's drag and drop, posted inside the session) runs ddAddImage:
    the image follows the pointer and a click places it. A dropped .txt, which the editor does not take, is the control."""
    from PIL import Image

    from kicad_mcp.coverage.drop_hook import DropHook

    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-ddimg-"))
    picture = scratch / "dropped.png"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(picture)
    note = scratch / "note.txt"
    note.write_text("not an image", encoding="utf-8")
    start = _bitmaps(env)
    hook = DropHook(env.s.pid)
    try:
        hook.drop(env.frame, [str(note)])
        time.sleep(2.0)
        env.idle()
        plain = _bitmaps(env)
        width, height = env.canvas_size()
        env.mouse(width // 2, height // 2, 0.3)
        hook.drop(env.frame, [str(picture)])
        time.sleep(3.0)
    finally:
        hook.close()
    for step in (10, 25, 40):
        env.mouse(width // 2 + step, height // 2 + step, 0.3)
    env.dump("image-on-pointer")
    env.click(width // 2 + 40, height // 2 + 40)
    time.sleep(1.5)
    env.idle()
    after = _bitmaps(env)
    saved = env.saved().count("(image")
    ok = plain == start and after == start + 1 and saved == 1
    return [_row(E + "ddAddImage", "ok" if ok else "no_effect",
                 f"images on the sheet: {start} at first, {plain} after a dropped .txt, {after} after dropped.png was dropped and "
                 f"a click placed it (saved sheet holds {saved} image block)")]


def _approach(env: SchEnv, x: int, y: int) -> None:
    """Move the pointer to canvas point (x, y) in steps: the canvas follows it once it has seen it move."""
    for step in range(8, 0, -1):
        env.mouse(x - 14 * step, y - 9 * step, 0.05)
    env.mouse(x, y, 0.5)


def calibrate(env: SchEnv):
    """A map from schematic mils to canvas pixels for the current view, read from the status bar's X/Y readout
    at two pointer positions (the editor reports where it takes the pointer to be)."""
    width, height = env.canvas_size()
    marks = []
    for px, py in ((width // 4, height // 4), (3 * width // 4, 3 * height // 4)):
        _approach(env, px, py)
        found = None
        for text in env.texts():
            m = re.fullmatch(r"X (-?[\d.]+)\s+Y (-?[\d.]+)", text)
            if m:
                found = (float(m.group(1)), float(m.group(2)))
        if not found:
            raise RuntimeError("no X/Y readout in the status bar")
        marks.append(((px, py), found))
    (p1, m1), (p2, m2) = marks
    ax, ay = (p2[0] - p1[0]) / (m2[0] - m1[0]), (p2[1] - p1[1]) / (m2[1] - m1[1])

    def to_px(mx: float, my: float) -> tuple[int, int]:
        return round(p1[0] + (mx - m1[0]) * ax), round(p1[1] + (my - m1[1]) * ay)

    return to_px


def refine_on_handle(env: SchEnv, x: int, y: int, radius: int = 60) -> tuple[int, int]:
    """The edit handle (a small black square) nearest canvas point (x, y): the status bar's coordinates are snapped to the
    grid, so the calibrated point can be tens of pixels off a handle that is only hit within a few. The pointer is first
    parked in a corner of the canvas so its crosshair is out of the way."""
    from kicad_mcp.coverage.live_board_misc import _canvas_origin

    width, height = env.canvas_size()
    _approach(env, width - 20, height - 20)
    shot = env.shot()
    left, top = _canvas_origin(env)
    raw, stride = shot.raw, shot.width * 4

    def dark(px: int, py: int) -> bool:
        i = (top + py) * stride + (left + px) * 4
        return raw[i] < 50 and raw[i + 1] < 50 and raw[i + 2] < 50

    best, best_d = None, None
    for py in range(max(2, y - radius), min(height - 3, y + radius)):
        for px in range(max(2, x - radius), min(width - 3, x + radius)):
            if sum(dark(px + dx, py + dy) for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2)) >= 9:
                d = (px - x) ** 2 + (py - y) ** 2
                if best_d is None or d < best_d:
                    best, best_d = (px, py), d
    return best or (x, y)


def polyline_fixture(folder: Path) -> tuple[Path, dict]:
    """One open five-corner polyline on an empty sheet."""
    pts = [(50.8, 50.8), (76.2, 50.8), (76.2, 76.2), (63.5, 88.9), (50.8, 76.2)]
    body = ["  (polyline (pts " + " ".join(f"(xy {x} {y})" for x, y in pts)
            + f') (stroke (width 0.254) (type default)) (fill (type none)) (uuid "{_u()}"))']
    return write_project(folder, sheet_text(_u(), [], body)), {"pts": pts}


def _polylines(text: str) -> list[list[tuple[float, float]]]:
    out = []
    for block in re.findall(r"\(polyline\s*\(pts(.*?)\)\s*\(stroke", text, re.S):
        out.append([(float(a), float(b)) for a, b in re.findall(r"\(xy ([-\d.]+) ([-\d.]+)\)", block)])
    return out


@cleanup_after_call
def case_removeCorner(env: SchEnv, extra: dict) -> list[dict]:
    """With a polyline selected and the pointer on one of its corner handles, Remove Corner deletes that corner."""
    shape = api.call(env.s, "GetItems", {"types": ["KOT_SCH_SHAPE"]})["response"]["items"][0]["id"]["value"]
    before = _polylines(env.saved())
    env.select_ids(shape)
    env.key(ZOOM_SEL, 1.2)
    to_px = calibrate(env)
    corner = extra["pts"][2]
    x, y = to_px(corner[0] / 0.0254, corner[1] / 0.0254)
    x, y = refine_on_handle(env, x, y)
    _approach(env, x, y)
    env.dump("on-corner")
    hotkeys.press(env.canvas, hotkeys.CONTROL_KEY[1])
    time.sleep(1.0)
    control = _polylines(env.s.document.read_text(encoding="utf-8"))  # nothing saved since: the file as it was
    env.key("eeschema.PointEditor.removeCorner", 1.5)
    after = _polylines(env.saved())
    env.dump("after-remove")
    gone = bool(after) and len(after[0]) == len(before[0]) - 1 and corner not in after[0]
    ok = control == before and gone
    detail = (f"polyline of {len(before[0])} corners; the unbound-key control left {len(control[0])}; Remove Corner with the "
              f"pointer on corner {corner} mm left {len(after[0]) if after else None}") if ok else ""
    return [_row("eeschema.PointEditor.removeCorner", "ok" if ok else "no_effect", detail,
                 before=before, control=control, after=after, mapped=[x, y])]


def swap_fixture(folder: Path) -> tuple[Path, dict]:
    root = _u()
    libs = [lib_symbol("T:IC", [("1", -5.08, 2.54, 0), ("2", -5.08, -2.54, 0), ("3", 5.08, 0, 180)],
                       (-2.54, 5.08, 2.54, -5.08))]
    ic, pins = placed("T:IC", "U1", 76.2, 76.2, ["1", "2", "3"], root)
    return write_project(folder, sheet_text(root, libs, [ic])), pins


def _pin_positions(text: str) -> dict[str, tuple[float, float]]:
    """Library pin number -> position as saved (the symbol's pins)."""
    return {m.group(3): (float(m.group(1)), float(m.group(2)))
            for m in re.finditer(r'\(pin\s+passive\s+line\s*\(at\s+([-\d.]+)\s+([-\d.]+)\s+[-\d.]+\).*?\(number\s+"(\w+)"',
                                 text, re.S)}


@cleanup_after_call
def case_swapPins(env: SchEnv, pins: dict) -> list[dict]:
    """Two pins of one symbol selected, Swap Pins (enabled by the unconstrained-pin-swaps preference) exchanges their
    positions: read back from the saved symbol."""
    before = _pin_positions(env.saved())
    selected = env.select_ids(pins["1"], pins["2"])
    hotkeys.press(env.canvas, hotkeys.CONTROL_KEY[1])
    time.sleep(1.0)
    control = _pin_positions(env.saved())
    env.key("eeschema.InteractiveEdit.swapPins", 1.5)
    after = _pin_positions(env.saved())
    swapped = after.get("1") == before.get("2") and after.get("2") == before.get("1") and after.get("3") == before.get("3")
    ok = control == before and swapped and before.get("1") != before.get("2")
    detail = (f"pins 1 and 2 selected ({len(selected)} items): saved positions {before} unchanged by the control {control}; "
              f"Swap Pins gave {after}") if ok else ""
    return [_row("eeschema.InteractiveEdit.swapPins", "ok" if ok else "no_effect", detail,
                 before=before, control=control, after=after, selected=len(selected))]




def hier_fixture(folder: Path) -> tuple[Path, dict]:
    import shutil

    shutil.copytree(os.environ.get("KICAD_AI_HIER") or fixture("hier-fixture"), folder,
                    ignore=shutil.ignore_patterns("*.kicad_pcb", "*.lck", "~*"))
    child = (folder / "ampli_ht.kicad_sch").read_text(encoding="utf-8")
    wire = re.search(r'\(wire.*?\(uuid "([0-9a-f-]+)"', child, re.S)
    return folder / "complex_hierarchy.kicad_sch", {"child_wire": wire.group(1)}


def _sheet_now(env: SchEnv) -> str:
    env.s._document_spec = None
    return " ".join(str(env.s.document_spec().sheet_path).split())


def _focus(env: SchEnv, item_id: str) -> None:
    """FocusOnItems with the document named without a sheet path (the item's own sheet is looked up)."""
    from google.protobuf import json_format

    env.s._document_spec = None
    spec = json_format.MessageToDict(env.s.document_spec(), preserving_proto_field_name=True)
    spec.pop("sheet_path", None)
    api.call(env.s, "FocusOnItems", {"document": spec, "items": [{"value": item_id}]})
    env.s._document_spec = None


@cleanup_after_call
def case_changeSheet(env: SchEnv, extra: dict) -> list[dict]:
    """The API's FocusOnItems on an item of another sheet runs changeSheet with that sheet's path (api_handler_sch.cpp:1029):
    the editor's current sheet changes. FocusOnItems on an item of the current sheet is the control."""
    env.s._document_spec = None
    root_wires = api.call(env.s, "GetItems", {"types": ["KOT_SCH_LINE"]})["response"]["items"]
    start = _sheet_now(env)
    _focus(env, root_wires[0]["id"]["value"])
    time.sleep(1.5)
    control = _sheet_now(env)
    _focus(env, extra["child_wire"])
    time.sleep(2.0)
    env.dump("child-sheet")
    after = _sheet_now(env)
    ok = start == control and after != start
    detail = (f"current sheet path {start!r} unchanged ({control!r}) by FocusOnItems on a root item; FocusOnItems on a wire of "
              f"ampli_ht.kicad_sch moved the editor to sheet {after!r} (a bare key press has no sheet-path parameter: "
              "sch_navigate_tool.cpp:239 wxCHECK returns, so the API is the reachable route)") if ok else ""
    return [_row("eeschema.NavigateTool.changeSheet", "ok" if ok else "no_effect", detail,
                 start=start, control=control, after=after)]


WIRE = "eeschema.InteractiveDrawingLineWireBus."
POSTURE = WIRE + "switchPosture"
UNDO_SEGMENT = WIRE + "undoLastSegment"


def _wire_set(env: SchEnv) -> set[tuple]:
    env.idle()
    out = set()
    for w in api.call(env.s, "GetItems", {"types": ["KOT_SCH_LINE"]})["response"].get("items", []):
        a, b = (int(w["start"]["x_nm"]), int(w["start"]["y_nm"])), (int(w["end"]["x_nm"]), int(w["end"]["y_nm"]))
        out.add(tuple(sorted([a, b])))
    return out


def _shape(wires: set[tuple]) -> list[tuple]:
    """Wires relative to their own top-left corner, in mils, so drawings at different places compare."""
    if not wires:
        return []
    ox = min(p[0] for w in wires for p in w)
    oy = min(p[1] for w in wires for p in w)
    return sorted((round((a[0] - ox) / 25400), round((a[1] - oy) / 25400), round((b[0] - ox) / 25400),
                   round((b[1] - oy) / 25400)) for a, b in wires)


def _draw(env: SchEnv, to_px, start: tuple[int, int], steps: list) -> list[tuple]:
    """One drawing, the way a person does it: the pointer put at ``start`` (mils), the Draw Wires key (the tool
    starts at the pointer), then the steps ("move"|"click"|"dbl", x, y mils) and ("key", vk). Returns the new wires."""
    before = _wire_set(env)
    _approach(env, *to_px(*start))
    env.key(WIRE + "drawWires", 0.8)
    for step in steps:
        if step[0] == "key":
            hotkeys.press(env.canvas, step[1])
            time.sleep(0.7)
            continue
        x, y = to_px(step[1], step[2])
        env.mouse(x, y, 0.4)
        if step[0] == "click":
            env.click(x, y)
        elif step[0] == "dbl":
            known = set(hotkeys._visible(env.s))
            env.click(x, y, double=True)
            for hwnd in hotkeys._visible(env.s):  # a properties window the double click opened
                if hwnd not in known and hwnd != env.frame:
                    hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
    env.escape(2)
    return _shape(_wire_set(env) - before)


@cleanup_after_call
def case_wire_keys(env: SchEnv, _extra: dict) -> list[dict]:
    """switchPosture flips which way the wire's corner turns; undoLastSegment takes back the segment a click fixed.
    Each is the same drawing three times: twice with an unbound key where the action's key goes (the controls must
    agree), once with the action's key; the committed wires are read back through the API."""
    to_px = calibrate(env)
    quiet = hotkeys.CONTROL_KEY[1]
    rows = []
    for name, vk in ((POSTURE, env.assigned[POSTURE][1]), (UNDO_SEGMENT, env.assigned[UNDO_SEGMENT][1])):
        shapes = []
        for index, key in enumerate((quiet, quiet, vk)):
            x0 = 1500 + 1300 * index
            y0 = 1500 if name == POSTURE else 3500
            if name == POSTURE:
                steps = [("move", x0 + 500, y0 + 300), ("key", key), ("move", x0 + 600, y0 + 300), ("dbl", x0 + 600, y0 + 300)]
            else:
                steps = [("move", x0 + 400, y0 + 300), ("click", x0 + 400, y0 + 300), ("move", x0 + 700, y0 + 500),
                         ("key", key), ("move", x0 + 800, y0 + 500), ("dbl", x0 + 800, y0 + 500)]
            shapes.append(_draw(env, to_px, (x0, y0), steps))
            print(name.rsplit(".", 1)[1], index, shapes[-1], flush=True)
            env.dump(f"{name.rsplit('.', 1)[1]}-{index}")
        first, second, got = shapes
        ok = bool(first) and first == second and bool(got) and got != first
        detail = f"the same drawing: two controls (unbound key) committed {first}; with the action's key {got}" if ok else ""
        rows.append(_row(name, "ok" if ok else "no_effect", detail, control=first, control_repeat=second, wires=got))
    return rows


SYM_TITLE = "Symbol Editor"
SYNC = "eeschema.SymbolLibraryControl.toggleSyncedPinsMode"
DELETE = "common.Interactive.delete"


def start_symedit(document: Path, keys: list[str], all_actions: list[str]) -> SchEnv:
    """The symbol editor, opened from the schematic editor's menu as a person does (live_hotkeys._start)."""
    from kicad_mcp.coverage.live_hotkeys import _start

    session, assigned, _frame, _canvas = _start("symedit", document, all_actions, keys)
    print("session pid", session.pid, flush=True)
    env = SchEnv(session, assigned)
    env.title = SYM_TITLE
    env.refresh()
    return env


def sync_fixture(folder: Path) -> tuple[Path, dict]:
    """A project whose own library holds a two-unit symbol: pins A, B, C sit at the same place with the same name,
    type and body style in both units, so synchronized pin editing treats each pair as one pin."""
    def unit(n: int) -> str:
        pins = "\n".join(
            f'        (pin passive line (at -5.08 {y} 0) (length 2.54) (name "{nm}" {FONT}) (number "{n}{nm}" {FONT}))'
            for nm, y in (("A", 5.08), ("B", 0), ("C", -5.08)))
        return f'      (symbol "MULTI_{n}_1"\n{pins})'

    lib = (f'(kicad_symbol_lib (version 20241209) (generator "kicad_symbol_editor") (generator_version "10.99")\n'
           f'  (symbol "MULTI" (pin_names (offset 0.5)) (exclude_from_sim no) (in_bom yes) (on_board yes)\n'
           f'    (property "Reference" "U" (at 0 8 0) {FONT})\n'
           f'    (property "Value" "MULTI" (at 0 -8 0) {FONT})\n'
           f'    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
           f'    (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) (hide yes)))\n'
           f'    (symbol "MULTI_0_1" (rectangle (start -2.54 7.62) (end 2.54 -7.62) (stroke (width 0.254) (type default))'
           f' (fill (type background))))\n{unit(1)}\n{unit(2)}\n    (embedded_fonts no)))\n')
    document, _ = blank_fixture(folder)
    (folder / f"{PROJECT}.kicad_sym").write_text(lib, encoding="utf-8")
    (folder / "sym-lib-table").write_text(
        '(sym_lib_table\n  (version 7)\n  (lib (name "t") (type "KiCad") (uri "${KIPRJMOD}/t.kicad_sym") (options "") (descr ""))\n)\n',
        encoding="utf-8")
    return document, {}


def _lib_pins(env: SchEnv) -> dict[str, list[str]]:
    """Unit -> pin names in the library file as saved."""
    text = (env.s.document.parent / f"{PROJECT}.kicad_sym").read_text(encoding="utf-8")
    out: dict[str, list[str]] = {}
    for match in re.finditer(r'\(symbol "MULTI_(\d+)_1"(.*?)(?=\n\s*\(symbol |\n\s*\(embedded_fonts|\Z)', text, re.S):
        out[match.group(1)] = re.findall(r'\(name\s+"(\w+)"', match.group(2))
    return out


def calibrate_fit(env: SchEnv, points: int = 12):
    """Mils -> canvas pixels from a least-squares fit over several pointer positions: the status bar's X/Y readout is
    snapped to the grid, and a fit over many positions averages that rounding out."""
    width, height = env.canvas_size()
    rows = []
    for i in range(points):
        px = int(width * (0.15 + 0.7 * ((i * 5) % points) / (points - 1)))
        py = int(height * (0.15 + 0.7 * ((i * 7) % points) / (points - 1)))
        _approach(env, px, py)
        for text in env.texts():
            m = re.fullmatch(r"X (-?[\d.]+)\s+Y (-?[\d.]+)", text)
            if m:
                rows.append((px, py, float(m.group(1)), float(m.group(2))))
    if len(rows) < 6:
        raise RuntimeError("too few coordinate readouts")

    def fit(pixel: int, mil: int) -> tuple[float, float]:
        xs = [r[2 + mil] for r in rows]
        ys = [r[pixel] for r in rows]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
        return slope, my - slope * mx

    (ax, bx), (ay, by) = fit(0, 0), fit(1, 1)
    return (lambda mx_, my_: (round(ax * mx_ + bx), round(ay * my_ + by))), (ax, ay)


@cleanup_after_call
def case_syncPins(env: SchEnv, _extra: dict) -> list[dict]:
    """Synchronized Pins Mode decides whether deleting a pin also deletes its twin in the other unit
    (symbol_editor_edit_tool.cpp DoDelete). With the mode on (the default) deleting pin A of unit 1 removes A from both
    units; after the action's key the mode is off and deleting pin B of unit 1 leaves unit 2's B. Read from the saved library."""
    api.call(env.s, "OpenLibraryItem", {"type": "DOCTYPE_SYMBOL", "identifier": {"library_nickname": "t", "entry_name": "MULTI"}})
    time.sleep(3)
    env.refresh()
    env.dump("opened")
    detail = {}
    env.key(SAVE, 1.5)
    start = _lib_pins(env)

    to_px, (ax, ay) = calibrate_fit(env)
    ox, oy = to_px(0, 0)
    rows_y = {"A": oy - round(200 * abs(ay)), "B": oy, "C": oy + round(200 * abs(ay))}

    def delete(name: str) -> dict:
        # a real click on the pin (its middle) selects it; the Delete key then deletes the selection
        x, y = ox + round(-150 * ax), rows_y[name]
        _approach(env, x, y)
        env.click(x, y)
        env.dump(f"selected-{name}")
        env.key(DELETE, 1.2)
        env.key(SAVE, 1.5)
        return _lib_pins(env)

    first = delete("A")                  # mode on (default)
    env.key(SYNC, 1.2)                   # the action under test
    second = delete("B")                 # mode off
    on_lost = {u: sorted(set(start[u]) - set(first.get(u, []))) for u in start}
    off_lost = {u: sorted(set(first[u]) - set(second.get(u, []))) for u in first}
    ok = (on_lost.get("1") == ["A"] and on_lost.get("2") == ["A"]
          and off_lost.get("1") == ["B"] and off_lost.get("2") == [])
    msg = (f"saved library pins per unit {start}; with the mode on (default) deleting A in unit 1 gave {first} (A gone from "
           f"both units); after Synchronized Pins Mode was pressed deleting B in unit 1 gave {second} (B kept in unit 2)")
    return [_row(SYNC, "ok" if ok else "no_effect", msg if ok else "", start=start, mode_on=first, mode_off=second, **detail)]


SIM_RUN = "eeschema.Simulation.runSimulation"
SIM_STOP = "eeschema.Simulation.stopSimulation"
SIM_PROBE = "eeschema.Simulation.probe"
SIM_SAVE = "eeschema.Simulation.saveWorkbook"
SIM_TRAN_LONG = ".tran 1u 100m"


def sim_fixture(folder: Path) -> tuple[Path, dict]:
    """KiCad's rectifier simulation demo, its transient stretched so a run lasts long enough to be stopped."""
    import shutil

    shutil.copytree(demos_dir() / "simulation" / "rectifier", folder, ignore=shutil.ignore_patterns("*.lck", "~*"))
    for path in [*folder.glob("*.kicad_sch"), *folder.glob("*.wbk")]:
        path.write_text(path.read_text(encoding="utf-8").replace(".tran 1u 10m", SIM_TRAN_LONG), encoding="utf-8")
    return next(folder.glob("*.kicad_sch")), {}


def start_sim(document: Path, keys: list[str], all_actions: list[str]) -> SchEnv:
    """The simulator, opened from the schematic editor's menu (as live_hotkeys does) with a longer wait for its frame."""
    session = GuiSession(document)
    config = session.profile / "config" / session._settings_version()
    assigned = hotkeys.write_hotkeys(config, all_actions, keys[:len(hotkeys.KEYS)])
    session.start()
    print("session pid", session.pid, flush=True)
    env = SchEnv(session, assigned)
    hotkeys.settled_capture(env.frame, timeout_s=15.0)
    opener = next((i for i in hotkeys.menu_items(env.frame) if i["label"] == "simulator"), None)
    if opener is None:
        raise RuntimeError("no simulator menu item")
    hotkeys.user32.PostMessageW(env.frame, hotkeys.WM_COMMAND, opener["id"], 0)
    for _ in range(40):
        time.sleep(1.5)
        with contextlib.suppress(RuntimeError):
            env.sim_frame, env.sim_canvas = hotkeys.canvas_of(session, "Simulator")
            break
    else:
        raise RuntimeError("the simulator did not open")
    time.sleep(2)
    hotkeys.settled_capture(env.sim_frame, timeout_s=15.0)
    return env


def _sim_menu(env: SchEnv, labels: dict, name: str) -> str:
    """Trigger a simulator action through its menu item (or its key when the menu has none)."""
    label = labels.get(name, "")
    item = next((i for i in hotkeys.menu_items(env.sim_frame) if label and i["label"] == label), None)
    if item is not None:
        hotkeys.user32.PostMessageW(env.sim_frame, hotkeys.WM_COMMAND, item["id"], 0)
        return "menu: " + " > ".join(item["path"])
    hotkeys.press(env.sim_frame, env.assigned[name][1])  # a key on the frame itself: the key on the largest child does not reach the dispatcher
    return f"key {env.assigned[name][0]}"


def _console(env: SchEnv) -> str:
    """The simulator's console text (ngspice's output), the longest Edit control of the frame."""
    win = {"name": "Simulator", "match": "contains"}
    edits = env.uia().call("state", {"window": win, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                                 "control_type": "Edit"}})["controls"]
    best = ""
    for e in edits:
        with contextlib.suppress(Exception):
            value = env.uia().call("read-value", {"window": win, "selector": {
                "by": "automation_id", "automation_id": str(e.get("automation_id")), "control_type": "Edit"}}).get("value") or ""
            if len(value) > len(best):
                best = value
    return best


def _rows(text: str) -> int | None:
    found = re.findall(r"No\. of Data Rows : (\d+)", text)
    return int(found[-1]) if found else None


def _run_until_done(env: SchEnv, labels: dict, limit: float) -> tuple[float, int | None, str]:
    """Run the simulation and wait for ngspice's closing line; returns (seconds, rows, console)."""
    before = len(re.findall(r"No\. of Data Rows", _console(env)))
    start = time.monotonic()
    _sim_menu(env, labels, SIM_RUN)
    while time.monotonic() - start < limit:
        time.sleep(2)
        text = _console(env)
        if len(re.findall(r"No\. of Data Rows", text)) > before:
            return time.monotonic() - start, _rows(text), text
    return limit, None, _console(env)


def _refs(text: str) -> list[float]:
    """ngspice prints 'Reference value : t' as the transient advances: the simulated times reached."""
    return [float(v) for v in re.findall(r"Reference value :\s+([-+\d.e]+)", text)]


@cleanup_after_call
def case_sim_stop(env: SchEnv, _extra: dict) -> list[dict]:
    """Stop Simulation halts a running ngspice job. Three runs of the same transient: a control that runs to the end
    (ngspice's closing data-row line, last simulated time = the end), one stopped part-way (the console shows progress
    but no closing line and stops at an earlier simulated time), and a repeat of the control (the same trigger without
    the stop completes again, so the stopped run did start and was cut short by the stop)."""
    labels = env.labels
    full_seconds, full_rows, control_text = _run_until_done(env, labels, 150)
    control_end = (_refs(control_text) or [0])[-1]
    print("control run", full_seconds, full_rows, control_end, flush=True)
    _sim_menu(env, labels, SIM_RUN)
    wait = max(1.5, full_seconds * 0.3)
    time.sleep(wait)
    route = _sim_menu(env, labels, SIM_STOP)
    time.sleep(max(8.0, full_seconds * 1.5))
    stopped_text = _console(env)
    fresh = stopped_text[len(control_text):] if stopped_text.startswith(control_text) else stopped_text
    stopped_refs = _refs(fresh)
    closed = _rows(fresh) is not None
    env.dump("stopped")
    print("stopped run", closed, stopped_refs[-1:] , control_end, len(fresh), flush=True)
    again_seconds, again_rows, _ = _run_until_done(env, labels, 150)
    started = bool(stopped_refs)
    ok = (full_rows is not None and started and not closed and stopped_refs[-1] < 0.9 * control_end
          and again_rows == full_rows)
    detail = (f"control run: {full_rows} data rows, simulated time reached {control_end:g} s in {full_seconds:.0f} s; a run "
              f"stopped through {route} after {wait:.1f} s reached {stopped_refs[-1]:g} s with no closing data-row line; the "
              f"same run again with no stop completed with {again_rows} rows") if ok else ""
    return [_row(SIM_STOP, "ok" if ok else "no_effect", detail, full_rows=full_rows, control_end=control_end,
                 stopped_refs=stopped_refs[-3:], closed=closed, again_rows=again_rows, console_tail=fresh[-300:])]


def _workbook_signals(env: SchEnv, labels: dict) -> list[str]:
    """Save the simulator's workbook through its own menu item and list the plotted signals it holds."""
    stamp = max((f.stat().st_mtime_ns for f in env.s.document.parent.glob("*.wbk")), default=0)
    _sim_menu(env, labels, SIM_SAVE)
    for _ in range(20):
        time.sleep(0.4)
        if max((f.stat().st_mtime_ns for f in env.s.document.parent.glob("*.wbk")), default=0) != stamp:
            break
    text = next(env.s.document.parent.glob("*.wbk")).read_text(encoding="utf-8")
    return sorted(re.findall(r'"signal":\s*"([^"]*)"', text))


@cleanup_after_call
def case_sim_probe(env: SchEnv, _extra: dict) -> list[dict]:
    """The Probe tool (Simulation > Probe Schematic) adds the clicked net's voltage to the plot. After a finished run, a
    wire is clicked in the schematic editor first without the tool (control: the workbook's signals stay), then with
    the tool active; the saved workbook then lists one more signal. Wires are tried until one is a net not yet plotted."""
    labels = env.labels
    seconds, rows, _ = _run_until_done(env, labels, 150)
    if rows is None:
        raise RuntimeError("the simulation did not finish")
    start = _workbook_signals(env, labels)
    wires = api.call(env.s, "GetItems", {"types": ["KOT_SCH_LINE"]})["response"].get("items", [])
    tried = []
    for wire in wires:
        wid = wire["id"]["value"]
        env.select_ids(wid)
        env.key(ZOOM_SEL, 1.0)
        env.select_ids()
        width, height = env.canvas_size()
        _approach(env, width // 2, height // 2)
        env.click(width // 2, height // 2)           # control: the same click with no probe tool
        env.escape(2)
        control = _workbook_signals(env, labels)
        env.select_ids()
        route = _sim_menu(env, labels, SIM_PROBE)
        time.sleep(1.5)
        _approach(env, width // 2, height // 2)
        env.dump("probe-on-wire")
        env.click(width // 2, height // 2)
        time.sleep(1.5)
        env.escape(2)
        after = _workbook_signals(env, labels)
        tried.append((wid, len(control), len(after)))
        if control == start and len(after) == len(start) + 1:
            gained = sorted(set(after) - set(start))
            return [_row(SIM_PROBE, "ok",
                         f"after a finished run the workbook held {start}; a click on a wire without the tool left it so; with "
                         f"the probe tool started through {route} a click on a wire added {gained}",
                         start=start, control=control, after=after, wires_tried=len(tried))]
    return [_row(SIM_PROBE, "no_effect", "", start=start, tried=tried)]


SELECT_ALL = "common.Interactive.selectAll"
UNSELECT_ALL = "common.Interactive.unselectAll"
SELECT_ON_PCB = "eeschema.EditorControl.selectOnPCB"
SELECT_ON_SCH = "pcbnew.InteractiveSelection.SelectOnSchematic"


def cross_fixture(folder: Path) -> tuple[Path, dict]:
    import shutil

    shutil.copytree(fixture("ecc83-fixture"), folder, ignore=shutil.ignore_patterns("*.lck", "~*", "*-backups"))
    return next(folder.glob("*.kicad_pro")), {}


def start_manager(project: Path, keys: list[str], all_actions: list[str]) -> SchEnv:
    """The project manager (one process serving both editors) with the schematic and board editors opened from its own
    menu, and implicit cross-probing on selection switched off in both editors' preferences, so only the explicit
    'Select on ...' actions can carry a selection across."""
    from kicad_mcp.ipc.gui_session import FrameSession

    session = FrameSession(project, "kicad.exe")
    config = session.profile / "config" / session._settings_version()
    assigned = hotkeys.write_hotkeys(config, all_actions, keys[:len(hotkeys.KEYS)])
    for name, version in (("eeschema.json", 5), ("pcbnew.json", 5)):
        (config / name).write_text(json.dumps({"meta": {"version": version}, "cross_probing": {"on_selection": False}}),
                                   encoding="utf-8")
    session.start()
    print("session pid", session.pid, flush=True)
    try:
        return _open_editors(session, assigned)
    except BaseException:
        session.close(discard=True)
        raise


def _open_editors(session, assigned: dict) -> SchEnv:
    manager, _ = hotkeys.canvas_of(session, "KiCad")
    time.sleep(3)
    for label, title in (("schematic editor", "Schematic Editor"), ("pcb editor", "PCB Editor")):
        item = next((i for i in hotkeys.menu_items(manager) if i["label"] == label), None)
        if item is None:
            raise RuntimeError(f"no '{label}' menu item in the project manager")
        for _attempt in range(4):  # the command is posted again if the editor has not shown after a while
            hotkeys.user32.PostMessageW(manager, hotkeys.WM_COMMAND, item["id"], 0)
            if any(title in t for _ in range(10) for t in (time.sleep(1.5) or hotkeys._visible(session)).values()):
                break
        time.sleep(2)
    env = SchEnv.__new__(SchEnv)
    env.s, env.assigned, env._hook, env.shots, env.title = session, assigned, None, 0, TITLE
    pcb = SchEnv.__new__(SchEnv)
    pcb.s, pcb.assigned, pcb._hook, pcb.shots, pcb.title = session, assigned, None, 0, "PCB Editor"
    for editor in (env, pcb):
        for _ in range(60):
            try:
                editor.refresh()
                break
            except RuntimeError:
                time.sleep(2)
        else:
            raise RuntimeError(f"{editor.title} did not open; windows {hotkeys._visible(session)}")
        hotkeys.settled_capture(editor.frame, timeout_s=20.0)
    env.pcb = pcb
    return env


def _panel(editor: SchEnv) -> list[str]:
    """The editor's message-panel and status texts that name a selection."""
    return [t for t in editor.texts() if t]


@cleanup_after_call
def case_cross_select(env: SchEnv, _extra: dict) -> list[dict]:
    """Select on PCB / Select on Schematic carry the current selection to the other editor even with implicit
    cross-probing off. Select all, a control key (nothing may arrive), then the action (the other editor's selection
    appears). The other editor's message panel (UI Automation texts) is the readback."""
    sch, pcb = env, env.pcb
    rows = []
    for source, target, action, name in ((sch, pcb, SELECT_ON_PCB, SELECT_ON_PCB), (pcb, sch, SELECT_ON_SCH, SELECT_ON_SCH)):
        hotkeys.press(target.canvas, hotkeys.VK_ESCAPE)
        time.sleep(0.5)
        base = _panel(target)
        source.key(UNSELECT_ALL, 0.5)
        source.key(SELECT_ALL, 1.5)
        hotkeys.press(source.canvas, hotkeys.CONTROL_KEY[1])
        time.sleep(1.5)
        control = _panel(target)
        source.dump(f"{name.rsplit('.', 1)[1]}-source")
        source.key(action, 2.0)
        after = _panel(target)
        target.dump(f"{name.rsplit('.', 1)[1]}-target")
        count = lambda texts: next((t for t in texts if re.fullmatch(r"(No|\d+) objects? selected", t)), None)  # noqa: E731
        ok = (count(base) == "No objects selected" and count(control) == "No objects selected"
              and (count(after) or "").endswith("objects selected") and count(after) != "No objects selected")
        detail = (f"with everything selected in the {source.title} (implicit cross-probing off in both editors) an unbound key "
                  f"left the {target.title} message panel at '{count(control)}'; {name.rsplit('.', 1)[1]} made it '{count(after)}'"
                  ) if ok else ""
        rows.append(_row(name, "ok" if ok else "no_effect", detail, base=base, control=control, after=after))
        source.key(UNSELECT_ALL, 0.5)
        target.key(UNSELECT_ALL, 0.5)
    return rows


CASES = {
    "ddimage": (blank_fixture, case_ddAddImage, [SAVE]),
    "removecorner": (polyline_fixture, case_removeCorner, ["eeschema.PointEditor.removeCorner", SAVE, ZOOM_SEL]),
    "swappins": (swap_fixture, case_swapPins, ["eeschema.InteractiveEdit.swapPins", SAVE],
                 {"input.allow_unconstrained_pin_swaps": True}),
    "changesheet": (hier_fixture, case_changeSheet, []),
    "symsync": (sync_fixture, case_syncPins, [SYNC, DELETE, SAVE]),
    "crossprobe": (cross_fixture, case_cross_select, [SELECT_ALL, UNSELECT_ALL, SELECT_ON_PCB, SELECT_ON_SCH]),
    "simprobe": (sim_fixture, case_sim_probe, [SIM_RUN, SIM_PROBE, SIM_SAVE, ZOOM_SEL]),
    "simstop": (sim_fixture, case_sim_stop, [SIM_RUN, SIM_STOP, SIM_PROBE, SIM_SAVE]),
    "wirekeys": (blank_fixture, case_wire_keys, [WIRE + "drawWires", POSTURE, UNDO_SEGMENT]),
    "netchain": (chain_fixture, case_netchain,
                 [E + "createNetChainBetweenPins", E + "nameNetChain", E + "highlightNetChain", E + "highlightNet",
                  E + "clearHighlight", E + "nextNetItem", E + "previousNetItem", E + "showNetNavigator",
                  SAVE, ZOOM_SEL, ZOOM_FIT]),
}


@cleanup_after_call
def sweep(inventory: dict, only: list[str] | None) -> dict:
    all_actions = [a["name"] for a in inventory["actions"]]
    rows: list[dict] = []
    for name, (make, run, keys, *prefs) in CASES.items():
        if only and name not in only:
            continue
        folder = Path(scoped_mkdtemp(prefix="kicad-ai-tools-schmisc-")) / name
        document, extra = make(folder)
        if name.startswith("cross"):
            env = start_manager(document, keys, all_actions)
        elif name.startswith("sim"):
            env = start_sim(document, keys, all_actions)
            env.labels = {a["name"]: hotkeys.normalize(a.get("friendly_name") or "") for a in inventory["actions"]}
        elif name.startswith("sym"):
            env = start_symedit(document, keys, all_actions)
        else:
            env = start(document, keys, all_actions, prefs[0] if prefs else None)
        try:
            env.idle()
            got = run(env, extra)
            rows.extend(got if isinstance(got, list) else [got])
        except Exception as exc:  # noqa: BLE001
            import traceback

            rows.append(_row(name, "error", f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-1500:]))
        finally:
            env.close()
            env.s.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "sch-misc-live", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), [n for n in args.only.split(",") if n] or None)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:200])
        if row.get("trace"):
            print(row["trace"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
