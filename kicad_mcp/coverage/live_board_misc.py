"""Live proof for assorted board-editor actions, each with a precondition built through the API and an
effect read back.

One KiCad GUI session (private desktop) serves a copy of the test board. For each action the module builds
what a person would have (selects the right item through the API, deletes a track so a ratsnest exists,
moves the pointer over an item, adds a second layer pair to the project...), runs the action (RunAction for
actions without a parameter, the action's own private-profile hotkey otherwise) and reads back one of:

* an API item field or board origin (positions, counts, hashes);
* the text the editor paints (message panel, popups), read by hooking wx's DrawText in the session process;
* a window the action opened (named, then closed);
* for a pure view toggle only: the editor window's pixels against a control key that is bound to nothing,
  with a round trip (toggle, toggle back, original pixels again).

An action no user can trigger, or that has no handler in this build, is not faked: it is reported as an
``upstream_gaps`` entry with the source that shows it.

    python -m kicad_mcp.coverage.live_board_misc --inventory inv.json --project <dir> --out ev.json [--only a,b]
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call

import argparse
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import time

from kicad_mcp.ipc import api, hotkeys
from kicad_mcp.ipc.gui_session import GuiSession

MM = 1_000_000
WM_MOUSEMOVE = 0x0200
E = "pcbnew.EditorControl."
WHITE = bytes([255, 255, 255])
C = "pcbnew.Control."


def _items(session, kind: str) -> list[dict]:
    return api.call(session, "GetItems", {"types": [kind]})["response"].get("items", [])


def _net(item: dict) -> str:
    return item.get("net", {}).get("name", "") or ""


class Env:
    """The session plus the helpers every case uses."""

    def __init__(self, session: GuiSession, assigned: dict[str, tuple[str, int]]):
        self.s, self.assigned = session, assigned
        self.frame, self.canvas = hotkeys.canvas_of(session, "PCB Editor")
        self._hook = None

    @property
    def hook(self):
        if self._hook is None:
            from kicad_mcp.coverage.text_hook import TextHook

            self._hook = TextHook(self.s.pid)
        return self._hook

    def close(self) -> None:
        if self._hook is not None:
            with contextlib.suppress(Exception):
                self._hook.close()
            self._hook = None
        if getattr(self, "_cursor", None) is not None:
            with contextlib.suppress(Exception):
                self._cursor.close()
            self._cursor = None

    @property
    def cursor(self):
        """In-process hook that makes KiCad see the mouse at a canvas point (see cursor_hook)."""
        if getattr(self, "_cursor", None) is None:
            from kicad_mcp.coverage.cursor_hook import CursorHook

            self._cursor = CursorHook(self.s.pid)
        return self._cursor

    def mouse(self, x: int, y: int, settle: float = 0.5) -> None:
        """Put the editor's pointer at canvas client point (x, y): the cursor hook and a mouse move."""
        self.cursor.place(self.canvas, x, y)
        hotkeys.user32.PostMessageW(self.canvas, WM_MOUSEMOVE, 0, (y << 16) | x)
        time.sleep(settle)

    def canvas_size(self) -> tuple[int, int]:
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(self.canvas, hotkeys.ctypes.byref(rect))
        return rect.right, rect.bottom

    def calibrate(self, centre_nm: tuple[int, int], offsets_mm: tuple[float, float] = (-3.0, 3.0)):
        """A function mapping board nm to canvas client pixels for the current view. Two marker vias are put at
        known points around ``centre_nm`` (which must be in view), found by the pixels they change, and removed."""
        template = _items(self.s, "KOT_PCB_VIA")[0]
        found = []
        left, top = _canvas_origin(self)
        width, height = self.canvas_size()
        region = (left, top, left + width, top + height)
        for off in offsets_mm:
            via = json.loads(json.dumps(template))
            via.pop("id", None)
            via.pop("parent", None)
            via.pop("net", None)
            pos = (centre_nm[0] + int(off * MM), centre_nm[1] + int(off * MM))
            via["position"] = {"x_nm": pos[0], "y_nm": pos[1]}
            via["pad_stack"]["copper_layers"][0]["size"] = {"x_nm": 1_500_000, "y_nm": 1_500_000}
            before = self.shot()
            made = api.call(self.s, "CreateItems", {"items": [via]})["response"]
            ids = [i["item"]["id"] for i in made.get("created_items", []) if i.get("item", {}).get("id")]
            time.sleep(0.8)
            after = self.shot()
            if ids:
                api.call(self.s, "DeleteItems", {"item_ids": ids})
            found.append((pos, _diff_centre(before, after, region)))
        (p1, q1), (p2, q2) = found
        ax = (q2[0] - q1[0]) / (p2[0] - p1[0])
        ay = (q2[1] - q1[1]) / (p2[1] - p1[1])

        def to_pixels(x_nm: int, y_nm: int) -> tuple[int, int]:
            return (round(q1[0] + ax * (x_nm - p1[0])) - left, round(q1[1] + ay * (y_nm - p1[1])) - top)

        return to_pixels

    def idle(self) -> None:
        """Escape until the editor answers the API again (a tool another case started has ended)."""
        for _ in range(12):
            try:
                api.call(self.s, "GetVersion", {})
                return
            except Exception:  # noqa: BLE001
                hotkeys.press(self.canvas, hotkeys.VK_ESCAPE)
                time.sleep(0.5)

    def refresh(self) -> None:
        self.frame, self.canvas = hotkeys.canvas_of(self.s, "PCB Editor")

    def text(self) -> set[str]:
        return self.hook.painted(self.frame)

    def key(self, name: str, settle: float = 1.0) -> None:
        hotkeys.press(self.canvas, self.assigned[name][1])
        time.sleep(settle)

    def run(self, name: str, settle: float = 1.0) -> str:
        reply = api.call(self.s, "RunAction", {"action": name})["response"].get("status", "")
        time.sleep(settle)
        return reply

    def select(self, *ids: str) -> None:
        api.call(self.s, "ClearSelection", {})
        if ids:
            api.call(self.s, "AddToSelection", {"items": [{"value": i} for i in ids]})

    def selection(self) -> list[dict]:
        return api.call(self.s, "GetSelection", {})["response"].get("items", [])

    def zoom_selection(self) -> None:
        api.call(self.s, "RunAction", {"action": "common.Control.zoomFitSelection"})
        time.sleep(0.8)

    def windows(self) -> dict[int, str]:
        return {h: t for h, t in hotkeys._visible(self.s).items() if h != self.frame}

    def escape(self, n: int = 3) -> None:
        for _ in range(n):
            hotkeys.press(self.canvas, hotkeys.VK_ESCAPE)
            time.sleep(0.3)

    def close_new(self, before: dict[int, str]) -> list[str]:
        titles = []
        for _ in range(5):
            new = {h: t for h, t in self.windows().items() if h not in before}
            if not new:
                break
            for hwnd, title in new.items():
                titles.append(title or "(untitled)")
                hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
            time.sleep(0.5)
        return titles

    def park_pointer_on(self, item_id: str) -> None:
        """Centre the view on an item and put the editor's pointer over it: a click at the canvas centre
        (it selects the item or opens a "Clarify Selection" menu; Escape closes that menu and the pointer
        stays where it was)."""
        from kicad_mcp.coverage.live_tools import click

        self.select(item_id)
        self.zoom_selection()
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(self.canvas, hotkeys.ctypes.byref(rect))
        click(self.canvas, rect.right // 2, rect.bottom // 2)
        time.sleep(0.5)
        self.escape(2)
        for _ in range(10):  # a menu or tool still up keeps the API busy
            try:
                api.call(self.s, "GetVersion", {})
                break
            except Exception:  # noqa: BLE001
                self.escape(1)

    def shot(self):
        return hotkeys.settled_capture(self.frame)

    def quiet(self) -> bool:
        return hotkeys.window_is_quiet(self.s, self.canvas, self.frame)


def _diff_centre(a, b, region: tuple[int, int, int, int]) -> tuple[float, float]:
    """Centre, in window pixels, of the pixels that differ between two captures of one window inside
    ``region`` (left, top, right, bottom): the canvas, so panels that redraw meanwhile do not count."""
    width, xs, ys = a.width, 0, 0
    count = 0
    left, top, right, bottom = region
    for y in range(top, bottom):
        row_a, row_b = a.raw[y * width * 4:(y + 1) * width * 4], b.raw[y * width * 4:(y + 1) * width * 4]
        if row_a == row_b:
            continue
        for x in range(left, right):
            if row_a[x * 4:x * 4 + 4] != row_b[x * 4:x * 4 + 4]:
                xs += x
                ys += y
                count += 1
    if not count:
        raise RuntimeError("the marker did not change any pixel")
    return xs / count, ys / count


def _canvas_origin(env: Env) -> tuple[int, int]:
    """Where the canvas's client area starts inside the frame's captured window image."""
    window = hotkeys._Rect()
    hotkeys.user32.GetWindowRect(env.frame, hotkeys.ctypes.byref(window))
    point = hotkeys.wt.POINT(0, 0)
    hotkeys.user32.ClientToScreen(hotkeys.wt.HWND(env.canvas), hotkeys.ctypes.byref(point))
    return point.x - window.left, point.y - window.top


def _row(name: str, outcome: str, detail: str, **extra) -> dict:
    return {"action": name, "outcome": outcome, "detail": detail, **extra}


# ---- view toggles: pixels, with a control and a round trip ---------------------------------------------

def _toggle_pair(env: Env, name: str, again: str | None = None, prep=None) -> dict:
    """Run a toggle, then toggle back. Passes when the window changed, changed back to the original, and
    a key bound to nothing leaves the window alone."""
    again = again or name
    before = env.shot()
    env.key(name, 1.2)
    after = env.shot()
    env.key(again, 1.2)
    back = env.shot()
    quiet = env.quiet()  # last: its Escape would also clear a net highlight the toggle works on
    changed, restored = before != after, before == back
    row = _row(name, "no_effect", f"window changed {changed}, restored by the second toggle {restored}, control quiet {quiet}")
    if changed and restored and quiet:
        row["outcome"] = "ok"
    return row


@cleanup_after_call
def case_showRatsnest(env: Env) -> dict:
    row = _toggle_pair(env, C + "showRatsnest")
    row["detail"] = "global ratsnest toggled and toggled back (toolbar button state and ratsnest); " + row["detail"]
    return row


@cleanup_after_call
def case_ratsnestLineMode(env: Env) -> dict:
    row = _toggle_pair(env, C + "ratsnestLineMode")
    row["detail"] = "curved-ratsnest toggled and toggled back; " + row["detail"]
    return row


# ---- board origin ---------------------------------------------------------------------------------------

@cleanup_after_call
def case_drillSetOrigin(env: Env) -> dict:
    """The API's SetBoardOrigin(drill) runs drillSetOrigin with the point as its parameter."""
    before = api.call(env.s, "GetBoardOrigin", {"type": "BOT_DRILL"})["response"]
    target = {"x_nm": 12345000, "y_nm": 23456000}
    if (int(before.get("x_nm", 0)), int(before.get("y_nm", 0))) == (target["x_nm"], target["y_nm"]):
        target = {"x_nm": 7000000, "y_nm": 8000000}
    api.call(env.s, "SetBoardOrigin", {"type": "BOT_DRILL", "origin": target})
    time.sleep(1.5)
    after = api.call(env.s, "GetBoardOrigin", {"type": "BOT_DRILL"})["response"]
    ok = (int(after.get("x_nm", 0)), int(after.get("y_nm", 0))) == (target["x_nm"], target["y_nm"]) and after != before
    return _row(E + "drillSetOrigin", "ok" if ok else "no_effect",
                f"drill/place origin read back through the API: {before} -> {after}")


# ---- nets -----------------------------------------------------------------------------------------------

def _unroute_net(env: Env, net: str) -> int:
    """Delete every track and via of one net, so its pads show a ratsnest."""
    ids = [i["id"] for kind in ("KOT_PCB_TRACE", "KOT_PCB_ARC", "KOT_PCB_VIA") for i in _items(env.s, kind) if _net(i) == net]
    for start in range(0, len(ids), 200):
        api.call(env.s, "DeleteItems", {"item_ids": ids[start:start + 200]})
    return len(ids)


@cleanup_after_call
def case_highlightNet(env: Env) -> dict:
    pad = next(i for i in _items(env.s, "KOT_PCB_TRACE") if _net(i) == "GND")
    env.park_pointer_on(pad["id"]["value"])
    env.select()
    before = env.text()
    reply = env.run(E + "highlightNet")
    after = env.text()
    gained = sorted(after - before)
    ok = {"Net Name", "Net Code"} <= after and "Net Name" not in before
    return _row(E + "highlightNet", "ok" if ok else "no_effect",
                f"pointer parked over a track, RunAction {reply}; message panel now shows the net: {gained[:12]}")


@cleanup_after_call
def case_toggleNetHighlight(env: Env) -> dict:
    """After a highlight, the toggle switches it off and on again: the window changes and returns."""
    case_highlightNet(env)  # a net is highlighted (Escape, run between cases, clears it)
    env.run("common.Control.zoomFitScreen", 1.5)  # the whole board in view: a highlight dims most of it
    row = _toggle_pair(env, E + "toggleNetHighlight")
    row["detail"] = "net highlight switched off and on again; " + row["detail"]
    return row


def _ratsnest_net(env: Env, net: str = "/D+"):
    """A net with no copper left, its pads selected: its ratsnest is the only change on a fitted board."""
    _unroute_net(env, net)
    pads = [i["id"]["value"] for i in _items(env.s, "KOT_PCB_PAD") if _net(i) == net]
    env.run("common.Control.zoomFitScreen", 1.5)
    env.select(*pads[:2])
    return pads


@cleanup_after_call
def case_hideNet_showNet(env: Env) -> list[dict]:
    """hideNet takes the net of the selected pads out of the ratsnest, showNet puts it back."""
    pads = _ratsnest_net(env)
    base = env.shot()
    env.key(E + "hideNet", 1.2)
    hidden = env.shot()
    selected = len(env.selection())
    env.key(E + "showNet", 1.2)
    shown = env.shot()
    env.select()
    quiet = env.quiet()
    common = f"{len(pads)} pads of net /D+ unrouted, 2 selected ({selected} still selected); control quiet {quiet}"
    return [_row(E + "hideNet", "ok" if base != hidden and quiet else "no_effect",
                 f"ratsnest of the selected pads' net hidden: window changed {base != hidden}; {common}"),
            _row(E + "showNet", "ok" if hidden != shown and shown == base and quiet else "no_effect",
                 f"ratsnest shown again: changed {hidden != shown}, original pixels restored {shown == base}; {common}")]


# ---- windows an action opens ------------------------------------------------------------------------------

def _opens(env: Env, name: str, expect: str, trigger: str = "key", select: list[str] | None | bool = None,
           wait: float = 25.0) -> dict:
    """Trigger an action and wait for a window titled ``expect``; the window is closed afterwards."""
    before = env.windows()
    if select is not False:
        env.select(*(select or []))
    if trigger == "key":
        env.key(name, 0.2)
    else:
        api.call(env.s, "RunAction", {"action": name})
    deadline = time.monotonic() + wait
    new: dict[int, str] = {}
    while time.monotonic() < deadline:
        new = {h: t for h, t in env.windows().items() if h not in before}
        if any(expect in t for t in new.values()):
            break
        time.sleep(0.4)
    labels = {h: [hotkeys.window_text(k[0]) for k in hotkeys._children(h) if k[3] and hotkeys.window_text(k[0])][:6]
              for h in new}
    titles = env.close_new(before)
    env.escape(2)
    ok = any(expect in t for t in titles)
    return _row(name, "ok" if ok else "no_effect", f"window opened: {titles} {list(labels.values())[:1]}; wanted '{expect}'")


@cleanup_after_call
def case_CompareBoardWithHistory(env: Env) -> dict:
    return _opens(env, "pcbnew.InspectionTool.CompareBoardWithHistory", "Compare Files")


@cleanup_after_call
def case_groupProperties(env: Env) -> dict:
    """A group's properties open through the board editor's Properties action, which runs groupProperties."""
    group = _items(env.s, "KOT_PCB_GROUP")[0]["id"]["value"]
    row = _opens(env, "pcbnew.InteractiveEdit.properties", "Group Properties", "ra", [group])
    row["action"] = "common.Groups.groupProperties"
    row["detail"] += "; a group selected and the board editor's Properties action run, which calls groupProperties with the group"
    return row


@cleanup_after_call
def case_matchPropertiesSettings(env: Env) -> dict:
    """The settings dialog opens from inside the Match Properties tool (one source item selected)."""
    track = _items(env.s, "KOT_PCB_TRACE")[5]["id"]["value"]
    env.select(track)
    env.key("pcbnew.MatchProperties.match", 1.5)  # the tool's own key
    row = _opens(env, "pcbnew.MatchProperties.settings", "Match Properties Settings", "key", False, 10)
    env.escape(4)
    row["detail"] += "; with the Match Properties tool active on a selected track"
    return row


@cleanup_after_call
def case_PushPadSettings(env: Env) -> dict:
    pad = next(i for i in _items(env.s, "KOT_PCB_PAD") if i["pad_stack"]["layers"][0] == "BL_F_Cu")["id"]["value"]
    return _opens(env, "pcbnew.PadTool.PushPadSettings", "Push Pad Properties", "ra", [pad], 10)


# ---- editing, read back through the API -----------------------------------------------------------------------

def _pad_size(pad: dict) -> tuple:
    copper = pad["pad_stack"]["copper_layers"][0]
    return copper["shape"], copper["size"]["x_nm"], copper["size"]["y_nm"]


@cleanup_after_call
def case_ApplyPadSettings(env: Env) -> dict:
    """Copy one pad's settings, select a pad of another size, paste: the second pad takes the first one's."""
    pads = [p for p in _items(env.s, "KOT_PCB_PAD") if p["pad_stack"]["layers"][0] == "BL_F_Cu" and p["type"] == "PT_SMD"]
    source = pads[0]
    target = next(p for p in pads if _pad_size(p) != _pad_size(source))
    env.select(source["id"]["value"])
    env.run("pcbnew.PadTool.CopyPadSettings", 0.5)
    env.select(target["id"]["value"])
    env.run("pcbnew.PadTool.ApplyPadSettings", 1.0)
    after = api.call(env.s, "GetItemsById", {"items": [target["id"]]})["response"]["items"][0]
    ok = _pad_size(after) == _pad_size(source) != _pad_size(target)
    return _row("pcbnew.PadTool.ApplyPadSettings", "ok" if ok else "no_effect",
                f"pad size read back: {_pad_size(target)} -> {_pad_size(after)} (copied from {_pad_size(source)})")


def _fy(footprint: dict) -> int:
    return int(footprint["position"]["y_nm"])


@cleanup_after_call
def case_distributeVerticallyCenters(env: Env) -> dict:
    fps = [f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f.get("locked") == "LS_UNLOCKED"]
    fps = sorted(fps[:60], key=_fy)
    low, high = fps[0], fps[-1]
    middle = min(fps[1:-1], key=lambda f: abs(_fy(f) - (_fy(low) + _fy(high)) // 2) * -1)  # farthest from the even spot
    env.select(low["id"]["value"], middle["id"]["value"], high["id"]["value"])
    before = {f["id"]["value"]: _fy(f) for f in (low, middle, high)}
    env.run("pcbnew.AlignAndDistribute.distributeVerticallyCenters", 1.2)
    got = {i: _fy(f) for i, f in ((i, api.call(env.s, "GetItemsById", {"items": [{"value": i}]})["response"]["items"][0])
                                  for i in before)}
    moved = [i for i in before if got[i] != before[i]]
    return _row("pcbnew.AlignAndDistribute.distributeVerticallyCenters", "ok" if moved else "no_effect",
                f"three footprints selected; y (nm) before {list(before.values())} after {list(got.values())}")


@cleanup_after_call
def case_zoneFillDirty(env: Env) -> dict:
    """A new copper zone is unfilled; filling the dirty zones fills it."""
    from kicad_mcp.coverage.live_zones import _created, _square

    created = api.call(env.s, "CreateItems", {"items": [_square(300, 10, 20, 1)]})["response"]
    ids = _created(created)
    zone = api.call(env.s, "GetItemsById", {"items": [{"value": ids[0]}]})["response"]["items"][0]
    before = bool(zone.get("filled"))
    env.run("pcbnew.ZoneFiller.zoneFillDirty", 4.0)
    for _ in range(20):
        zone = api.call(env.s, "GetItemsById", {"items": [{"value": ids[0]}]})["response"]["items"][0]
        if zone.get("filled"):
            break
        time.sleep(1)
    after = bool(zone.get("filled"))
    return _row("pcbnew.ZoneFiller.zoneFillDirty", "ok" if after and not before else "no_effect",
                f"new unfilled copper zone, filled {before} -> {after} ({len(zone.get('filled_polygons', []))} fill polygons)")


# ---- phase 2: actions that need the pointer, a tool, or a prepared project --------------------------------

_MEMO: dict[str, str] = {}


def _nodes(zone: dict) -> list[tuple[int, int]]:
    return [(int(n["point"]["x_nm"]), int(n["point"]["y_nm"])) for n in zone["outline"]["polygons"][0]["outline"]["nodes"]]


def _zone(env: Env, zone_id: str) -> dict:
    return api.call(env.s, "GetItemsById", {"items": [{"value": zone_id}]})["response"]["items"][0]


def _square_zones(env: Env) -> list[dict]:
    zones = [z for z in _items(env.s, "KOT_PCB_ZONE") if len(_nodes(z)) == 4]
    return sorted(zones, key=lambda z: z["id"]["value"])


def _zone_view(env: Env, zone: dict):
    """Fit one zone in the view and return the nm -> canvas pixel map for it."""
    nodes = _nodes(zone)
    centre = (sum(n[0] for n in nodes) // len(nodes), sum(n[1] for n in nodes) // len(nodes))
    env.select(zone["id"]["value"])
    env.zoom_selection()
    env.select()
    return env.calibrate(centre)


def _on_marker(env: Env, point: tuple[int, int], radius: int = 45) -> tuple[int, int]:
    """The selected shape's edit point (a white dot) nearest ``point``, in canvas pixels. The map from board
    coordinates is a few pixels out, and a point is only hovered within a few pixels of its centre."""
    shot = env.shot()
    left, top = _canvas_origin(env)
    width = shot.width
    best, best_d = None, None
    for y in range(point[1] - radius, point[1] + radius):
        for x in range(point[0] - radius, point[0] + radius):
            if all(shot.raw[((top + y + dy) * width + left + x + dx) * 4:((top + y + dy) * width + left + x + dx) * 4 + 3] == WHITE
                   for dy in range(5) for dx in range(0, 5, 2)):
                d = (x - point[0]) ** 2 + (y - point[1]) ** 2
                if best_d is None or d < best_d:
                    best, best_d = (x + 2, y + 2), d
    return best or point


@cleanup_after_call
def case_removeCorner(env: Env) -> dict:
    zone = _square_zones(env)[0]
    to_px = _zone_view(env, zone)
    corner = _nodes(zone)[2]
    env.select(zone["id"]["value"])
    env.mouse(*_on_marker(env, to_px(*corner)), 0.8)
    env.key("pcbnew.PointEditor.removeCorner", 1.5)
    after = _nodes(_zone(env, zone["id"]["value"]))
    return _row("pcbnew.PointEditor.removeCorner", "ok" if len(after) == 3 and corner not in after else "no_effect",
                f"zone selected, pointer over its corner {corner}; corners {len(_nodes(zone))} -> {len(after)}")


@cleanup_after_call
def case_chamferCorner(env: Env) -> dict:
    zone = _square_zones(env)[-1]
    _MEMO["chamfered"] = zone["id"]["value"]
    to_px = _zone_view(env, zone)
    before = _nodes(zone)
    corner = before[1]
    env.select(zone["id"]["value"])
    env.mouse(*_on_marker(env, to_px(*corner)), 0.8)
    env.key("pcbnew.PointEditor.chamferCorner", 2.0)
    after = _nodes(_zone(env, zone["id"]["value"]))
    return _row("pcbnew.PointEditor.chamferCorner", "ok" if len(after) == len(before) + 1 and corner not in after else "no_effect",
                f"zone selected, pointer over corner {corner}; corners {len(before)} -> {len(after)}")


@cleanup_after_call
def case_moveMidpoint(env: Env) -> dict:
    zone = _zone(env, _MEMO["chamfered"])  # the chamfer case left it with five corners; any edge has a midpoint
    to_px = _zone_view(env, zone)
    nodes = _nodes(zone)
    a, b = nodes[0], nodes[1]  # the bottom edge: the top of the view sits under the editor's info bar
    mid = ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)
    env.select(zone["id"]["value"])
    target = _on_marker(env, to_px(*mid))
    env.mouse(*target, 0.8)
    env.mouse(*target, 0.5)
    row = _opens(env, "pcbnew.InteractiveEdit.moveMidpoint", "Move Midpoint to Location", "key", False, 8)
    row["detail"] += f"; zone selected, pointer over the midpoint {mid} of one edge"
    return row


@cleanup_after_call
def case_editVertices(env: Env) -> dict:
    shape = next(s for s in _items(env.s, "KOT_PCB_SHAPE") if s.get("shape") and ("polygon" in s["shape"] or "rectangle" in s["shape"]))
    return _opens(env, "pcbnew.InteractiveEdit.editVertices", "Edit Vertices", "key", [shape["id"]["value"]], 10)


@cleanup_after_call
def case_deleteFull(env: Env) -> dict:
    """Delete Full Track takes the whole run of connected segments; plain Delete takes the one selected."""
    tracks = [t for t in _items(env.s, "KOT_PCB_TRACE") if _net(t) == "/D-"]
    before = len(_items(env.s, "KOT_PCB_TRACE"))
    env.select(tracks[len(tracks) // 2]["id"]["value"])
    env.key("pcbnew.InteractiveEdit.deleteFull", 2.0)
    after = len(_items(env.s, "KOT_PCB_TRACE"))
    return _row("pcbnew.InteractiveEdit.deleteFull", "ok" if before - after > 1 else "no_effect",
                f"one track segment of net /D- selected; track count {before} -> {after} ({before - after} removed, a plain Delete removes 1)")


@cleanup_after_call
def case_autoplaceOffboard(env: Env) -> dict:
    fp = next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f.get("locked") == "LS_UNLOCKED" and f["layer"] == "BL_F_Cu")
    start = dict(fp["position"])
    fp["position"] = {"x_nm": 400_000_000, "y_nm": 100_000_000}
    api.call(env.s, "UpdateItems", {"items": [fp]})
    env.run("pcbnew.Autoplacer.autoplaceOffboard", 8.0)
    for _ in range(30):
        try:
            got = api.call(env.s, "GetItemsById", {"items": [fp["id"]]})["response"]["items"][0]["position"]
            break
        except Exception:  # noqa: BLE001 - the placer keeps the editor busy while it works
            time.sleep(2)
    moved = (int(got["x_nm"]), int(got["y_nm"]))
    return _row("pcbnew.Autoplacer.autoplaceOffboard", "ok" if moved[0] < 300_000_000 else "no_effect",
                f"a footprint moved to x=400 mm (off the board) through the API; its position after the action: {moved} (was {start})")


@cleanup_after_call
def case_layerPairPresetCycle(env: Env) -> dict:
    """With two layer pair presets in the project, the cycle changes the layer-pair indicator on the toolbar;
    cycling again returns it. Only the indicator's pixels may change."""
    shots = []
    for _ in range(4):
        env.key(C + "layerPairPresetCycle", 1.5)
        shots.append(env.shot())
    box = _diff_box(shots[0], shots[1])
    quiet = env.quiet()
    alternates = bool(box and not _same_inside(shots[0], shots[1], box) and _same_inside(shots[0], shots[2], box)
                      and _same_inside(shots[1], shots[3], box))
    return _row(C + "layerPairPresetCycle", "ok" if alternates and quiet else "no_effect",
                f"two layer pair presets in the project; over four presses the layer-pair indicator (pixels {box}) alternated between two looks "
                f"(1st = 3rd, 2nd = 4th, 1st != 2nd): {alternates}; control quiet {quiet}")


def _same_inside(a, b, box: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = box
    width = a.width
    return all(a.raw[(y * width + left) * 4:(y * width + right + 1) * 4] == b.raw[(y * width + left) * 4:(y * width + right + 1) * 4]
               for y in range(top, bottom + 1))


def _diff_box(a, b) -> tuple[int, int, int, int] | None:
    width = a.width
    xs, ys = [], []
    for y in range(a.height):
        ra, rb = a.raw[y * width * 4:(y + 1) * width * 4], b.raw[y * width * 4:(y + 1) * width * 4]
        if ra != rb:
            for x in range(width):
                if ra[x * 4:x * 4 + 4] != rb[x * 4:x * 4 + 4]:
                    xs.append(x)
                    ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


CASES = {
    "common.Groups.groupProperties": case_groupProperties,
    "pcbnew.MatchProperties.settings": case_matchPropertiesSettings,
    "pcbnew.PadTool.ApplyPadSettings": case_ApplyPadSettings,
    "pcbnew.AlignAndDistribute.distributeVerticallyCenters": case_distributeVerticallyCenters,
    "pcbnew.ZoneFiller.zoneFillDirty": case_zoneFillDirty,
    E + "drillSetOrigin": case_drillSetOrigin,
    E + "highlightNet": case_highlightNet,
    E + "toggleNetHighlight": case_toggleNetHighlight,
    C + "showRatsnest": case_showRatsnest,
    C + "ratsnestLineMode": case_ratsnestLineMode,
    E + "hideNet": case_hideNet_showNet,  # last: it deletes a net's copper
}
# Actions this build cannot run, with the source that shows it.
GAPS: dict[str, str] = {
    E + "rescueAutosave": "declared in pcbnew/tools/board_editor_control.h (RescueAutosave) but never defined or bound "
                          "with Go(): the File > Rescue menu item and RunAction reach no handler",
    "pcbnew.InteractiveSelection.SelectOnSheet": "PCB_SELECTION_TOOL::selectSheetContents dereferences a wxString* "
                          "parameter and nothing in the tree posts selectOnSheetFromEeschema; RunAction passes a bool, "
                          "so the call crashes the editor",
}



# ---- settings: the editor writes them when it closes, so close it as a person does and read the file --------

def graceful_close(env: Env, wait_s: float = 40.0) -> bool:
    """Close the editor window (WM_CLOSE, as the window's close button does) and wait for the process to end,
    which is when KiCad saves its settings. Returns True if it ended by itself."""
    hotkeys.user32.PostMessageW(env.frame, hotkeys.WM_CLOSE, 0, 0)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not env.s.kicad.alive():
            return True
        time.sleep(0.5)
    return False


def _toolbar_only_change(env: Env, before, after) -> tuple[bool, tuple | None]:
    """True when every changed pixel lies outside the canvas (in the toolbars and panels)."""
    box = _diff_box(before, after)
    left, top = _canvas_origin(env)
    width, height = env.canvas_size()
    if box is None:
        return False, None
    outside = box[3] < top or box[1] > top + height or box[2] < left or box[0] > left + width
    return outside, box


def settings_run(inventory: dict, project: Path, name: str, preseed: dict | None, presses: list[str],
                 run_actions: list[str], expect: dict[str, object], credit: dict[str, str], toolbar: str | None = None) -> list[dict]:
    """One session: start with ``preseed`` in pcbnew.json, press keys / run actions, close, read pcbnew.json.
    ``credit`` maps an action to the settings key that must have the expected value."""
    all_actions = [a["name"] for a in inventory["actions"]]
    env = start_session(project, presses, all_actions, None, preseed)
    config = env.s.profile / "config" / env.s._settings_version() / "pcbnew.json"
    rows: list[dict] = []
    toolbar_note, toolbar_ok = "", False
    try:
        for key in presses:
            before = env.shot()
            env.key(key, 1.5)
            after = env.shot()
            if key == toolbar:
                only, box = _toolbar_only_change(env, before, after)
                toolbar_note = f"; the line-mode toolbar group changed (pixels {box}, all outside the canvas: {only})"
                toolbar_ok = only
        for action in run_actions:
            env.run(action, 1.0)
        ended = graceful_close(env)
        data = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    finally:
        env.close()
        env.s.close(discard=True)

    def dig(dotted: str):
        node = data
        for part in dotted.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return node

    for action, key in credit.items():
        value = dig(key)
        want = expect[key]
        start = preseed.get(key) if preseed else None
        row = _row(action, "ok" if value == want and value != start and ended else "no_effect",
                   f"{key} in pcbnew.json after the editor closed on its own ({ended}): {value!r} "
                   f"(it started as {start if start is not None else 'the default'!r}, expected {want!r}); {name}")
        if action.endswith("angleSnapModeChanged"):
            row["outcome"] = "ok" if row["outcome"] == "ok" and toolbar_note and toolbar_ok else "no_effect"
            row["detail"] += toolbar_note
        rows.append(row)
    return rows


def settings_phase(inventory: dict, project: Path) -> list[dict]:
    rows: list[dict] = []
    angle = "editing.pcb_angle_snap_mode"
    arc = "editing.arc_edit_mode"
    show = "pcb_display.show_constraints"
    rows += settings_run(inventory, project, "angle snap, arc mode and constraints from the defaults", None,
                         [E + "lineMode45", "pcbnew.PointEditor.arcKeepRadius"],
                         [E + "angleSnapModeChanged", "pcbnew.ConstraintEditor.showConstraints"],
                         {angle: 1, arc: 2, show: True},
                         {E + "lineMode45": angle, E + "angleSnapModeChanged": angle, "pcbnew.PointEditor.arcKeepRadius": arc,
                          "pcbnew.ConstraintEditor.showConstraints": show}, toolbar=E + "lineMode45")
    rows += settings_run(inventory, project, "arc mode from keep-radius", {arc: 2},
                         ["pcbnew.PointEditor.arcKeepEndpoint"], [], {arc: 1},
                         {"pcbnew.PointEditor.arcKeepEndpoint": arc})
    rows += settings_run(inventory, project, "arc mode from keep-endpoints", {arc: 1},
                         ["pcbnew.PointEditor.arcKeepCenter"], [], {arc: 0},
                         {"pcbnew.PointEditor.arcKeepCenter": arc})
    return rows


ICON_HOOK = r"""
const f = Module.getExportByName('kicommon.dll', '?KiDisabledBitmapBundleDef@@YA?AVwxBitmapBundle@@W4BITMAPS@@H@Z');
let icons = [];
Interceptor.attach(f, { onEnter(args) { icons.push(args[1].toInt32()); } });  // args[0]: the returned bundle
rpc.exports = { take() { const out = icons; icons = []; return out; } };
"""


def angle_snap_phase(inventory: dict, project: Path) -> list[dict]:
    """angleSnapModeChanged re-selects the line-mode toolbar group's button from the angle-snap setting
    (BOARD_EDITOR_CONTROL::OnAngleSnapModeChanged, pcbnew/tools/board_editor_control.cpp:353; the toolbar takes
    the action's icon through KiDisabledBitmapBundleDef, common/tool/action_toolbar.cpp:639). An in-process hook
    on that exported function records the icon each run asks for. Two sessions start from different modes
    (pcbnew.json editing.pcb_angle_snap_mode 0 and 2); in each, running the action by name asks for that mode's
    icon, and an unrelated action (zoom redraw) asks for none."""
    import frida

    all_actions = [a["name"] for a in inventory["actions"]]
    action = E + "angleSnapModeChanged"
    seen: dict[int, dict] = {}
    for mode in (0, 2):
        env = start_session(project, [], all_actions, None, {"editing.pcb_angle_snap_mode": mode})
        attached = script = None
        try:
            attached = frida.attach(env.s.pid)
            script = attached.create_script(ICON_HOOK)
            script.load()
            env.run("common.Control.zoomRedraw", 1.5)
            control = script.exports_sync.take()
            env.run(action, 1.5)
            icons = script.exports_sync.take()
            seen[mode] = {"control": control, "icons": icons}
        finally:
            with contextlib.suppress(Exception):
                script.unload()
                attached.detach()
            env.close()
            env.s.close(discard=True)
    a, b = seen.get(0, {}), seen.get(2, {})
    ok = (not a.get("control") and not b.get("control") and len(a.get("icons", [])) >= 1 and len(b.get("icons", [])) >= 1
          and set(a["icons"]) != set(b["icons"]))
    return [_row(action, "ok" if ok else "no_effect",
                 f"toolbar icon asked for when the action runs, by angle-snap mode: mode 0 {a.get('icons')}, mode 2 {b.get('icons')} "
                 f"(the group's button follows the setting); an unrelated action in the same sessions asked for "
                 f"{a.get('control')} / {b.get('control')}")]


def router_mode_phase(inventory: dict, project: Path) -> list[dict]:
    """The router mode keys (their own parameter is the mode, so they are pressed, not run by name): each session
    starts from a known mode in pcbnew.json (tools.pns.mode: 0 highlight, 1 shove, 2 walk around) and the
    saved mode is read back after the editor closes on its own."""
    router, mode = "pcbnew.InteractiveRouter.", "tools.pns.mode"
    rows: list[dict] = []
    for start, action, want in ((2, "ShoveMode", 1), (1, "HighlightMode", 0), (0, "WalkaroundMode", 2)):
        rows += settings_run(inventory, project, f"router mode from {start}", {mode: start}, [router + action], [],
                             {mode: want}, {router + action: mode})
    return rows


def _canvas_diff_count(env: Env, a, b, cyan: bool = False) -> int:
    """Canvas pixels that differ between two captures; with ``cyan`` only those that are ratsnest-blue in ``b``."""
    left, top = _canvas_origin(env)
    width, height = env.canvas_size()
    count = 0
    for y in range(top, top + height):
        ra, rb = a.raw[y * a.width * 4:(y + 1) * a.width * 4], b.raw[y * a.width * 4:(y + 1) * a.width * 4]
        if ra != rb:
            for x in range(left, left + width):
                if ra[x * 4:x * 4 + 4] != rb[x * 4:x * 4 + 4]:
                    blue, green, red = rb[x * 4], rb[x * 4 + 1], rb[x * 4 + 2]
                    if not cyan or (blue > 180 and green > 120 and red < 120):
                        count += 1
    return count


@cleanup_after_call
def case_localRatsnestTool(env: Env) -> dict:
    """With the global ratsnest off and a net unrouted, a plain click on one of its pads shows no ratsnest line;
    the same click with the Local Ratsnest tool active does."""
    from kicad_mcp.coverage.live_tools import click

    _unroute_net(env, "/D+")
    pads = [p for p in _items(env.s, "KOT_PCB_PAD") if _net(p) == "/D+"]
    env.key(C + "showRatsnest", 1.2)  # global ratsnest off
    env.select(*[p["id"]["value"] for p in pads])
    env.zoom_selection()
    env.select()
    centre = (sum(int(p["position"]["x_nm"]) for p in pads) // len(pads), sum(int(p["position"]["y_nm"]) for p in pads) // len(pads))
    to_px = env.calibrate(centre, (-1.0, 1.0))
    x, y = to_px(int(pads[0]["position"]["x_nm"]), int(pads[0]["position"]["y_nm"]))
    env.mouse(x, y, 0.6)
    click(env.canvas, x, y)
    time.sleep(1.0)
    plain = env.shot()
    env.escape(2)
    env.select()
    env.key(C + "localRatsnestTool", 1.0)
    env.mouse(x, y, 0.6)
    click(env.canvas, x, y)
    time.sleep(1.2)
    tool = env.shot()
    changed = _canvas_diff_count(env, plain, tool)
    cyan = _canvas_diff_count(env, plain, tool, cyan=True)
    env.escape(3)
    return _row(C + "localRatsnestTool", "ok" if cyan > 50 else "no_effect",
                f"global ratsnest off, net /D+ unrouted, a click on its pad: {changed} canvas pixels differ between a plain click and a click with the "
                f"Local Ratsnest tool active, {cyan} of them ratsnest-blue (the pad's ratsnest line appears)")


def _passive_text(env: Env, clear: bool = True) -> set[str]:
    """Strings the editor painted since the last call, without forcing a repaint (popups and panels included)."""
    hook = env.hook
    with hook._lock:
        found = {t.strip() for t in hook._texts if t.strip()}
        if clear:
            hook._texts.clear()
    return found


@cleanup_after_call
def case_skip(env: Env) -> dict:
    """Two footprints selected, Move Individually started over the first, Skip: the first stays where it was,
    the tool moves on to the second (the Properties panel switches to it), and a click places it."""
    from kicad_mcp.coverage.live_tools import click

    fps = [f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f.get("locked") == "LS_UNLOCKED" and f["layer"] == "BL_F_Cu"]
    first, second = fps[3], fps[-3]
    position = lambda f: (int(f["position"]["x_nm"]), int(f["position"]["y_nm"]))  # noqa: E731
    pa, pb = position(first), position(second)
    env.select(first["id"]["value"], second["id"]["value"])
    env.zoom_selection()
    env.select()
    to_px = env.calibrate(((pa[0] + pb[0]) // 2, (pa[1] + pb[1]) // 2), (-1.0, 1.0))
    x, y = to_px(*pa)
    env.select(first["id"]["value"], second["id"]["value"])  # selection order is the tool's order
    env.mouse(x, y, 0.8)
    _passive_text(env)
    env.key("pcbnew.InteractiveMove.moveIndividually", 1.5)
    env.mouse(x + 60, y + 40, 0.8)
    before = _passive_text(env)
    env.key("pcbnew.InteractiveEdit.skip", 1.5)
    env.mouse(x + 80, y + 60, 0.8)
    after = _passive_text(env)
    click(env.canvas, x + 80, y + 60)
    time.sleep(1.5)
    now = [position(api.call(env.s, "GetItemsById", {"items": [f["id"]]})["response"]["items"][0]) for f in (first, second)]
    switched = bool(after - before)
    ok = now[0] == pa and now[1] != pb and switched
    return _row("pcbnew.InteractiveEdit.skip", "ok" if ok else "no_effect",
                f"Move Individually over two footprints, then Skip: the first stayed at {pa} ({now[0] == pa}), the second moved {pb} -> {now[1]}; "
                f"the Properties panel switched items ({len(after - before)} new strings)")


# ---- other frames of the same editor process: footprint viewer, footprint editor -----------------------------

def _frame_env(env: Env, title: str) -> Env:
    """An Env for another top-level frame of the session (same process, same hotkeys)."""
    other = Env.__new__(Env)
    other.s, other.assigned, other._hook, other._cursor = env.s, env.assigned, env._hook, None
    other.frame, other.canvas = hotkeys.canvas_of(env.s, title)
    return other


def _wait_frame(env: Env, title: str, seconds: float = 30.0) -> Env:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            frame_env = _frame_env(env, title)
            hotkeys.settled_capture(frame_env.frame, timeout_s=10.0)
            return _frame_env(env, title)
        except RuntimeError:
            time.sleep(1)
    raise RuntimeError(f"no '{title}' frame")


def _footprint_names(env: Env) -> list[str]:
    return sorted(t for t in env.text() if t.startswith(("1005_C", "1006_C", "1608_C", "1P-", "2012_C", "CP_", "D_0402")))


@cleanup_after_call
def case_nextPreviousFootprint(env: Env) -> list[dict]:
    """The footprint viewer (View > Footprint Library Browser) shows the footprint the next/previous action selects."""
    item = next(i for i in hotkeys.menu_items(env.frame) if i["path"][-1] == "Footprint Library Browser")
    hotkeys.user32.PostMessageW(env.frame, hotkeys.WM_COMMAND, item["id"], 0)
    viewer = _wait_frame(env, "Footprint Library Browser")
    start = _footprint_names(viewer)
    viewer.key(C + "nextFootprint", 2.0)
    after_next = _footprint_names(viewer)
    viewer.key(C + "nextFootprint", 2.0)
    after_next2 = _footprint_names(viewer)
    viewer.key(C + "previousFootprint", 2.0)
    after_prev = _footprint_names(viewer)
    hotkeys.user32.PostMessageW(viewer.frame, hotkeys.WM_CLOSE, 0, 0)
    time.sleep(1.5)
    note = f"footprint viewer shows {start} -> next {after_next} -> next {after_next2} -> previous {after_prev}"
    return [_row(C + "nextFootprint", "ok" if start and after_next != start and after_next2 not in (start, after_next) else "no_effect", note),
            _row(C + "previousFootprint", "ok" if after_next2 and after_prev != after_next2 and after_prev == after_next else "no_effect", note)]


def _close_footprint_editor(env: Env, editor: Env) -> None:
    hotkeys.user32.PostMessageW(editor.frame, hotkeys.WM_CLOSE, 0, 0)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for hwnd, title in env.windows().items():
            if title == "Confirmation":
                for child in hotkeys._children(hwnd):
                    if hotkeys.window_text(child[0]) == "Discard Changes":
                        hotkeys.user32.PostMessageW(child[0], 0xF5, 0, 0)  # BM_CLICK
        if not any("Footprint Editor" in t for t in env.windows().values()):
            return
        time.sleep(0.5)


@cleanup_after_call
def case_explodeRecombinePad(env: Env) -> list[dict]:
    """In the footprint editor a pad is clicked (the pointer on it) and exploded into graphic shapes: the editor goes
    into Pad Edit Mode and says so; recombine ends it."""
    from kicad_mcp.coverage.live_hotkeys import open_hosted
    from kicad_mcp.coverage.live_tools import click

    open_hosted(env.s, "fpedit")
    editor = _wait_frame(env, "Footprint Editor")
    shot = editor.shot()
    left, top = _canvas_origin(editor)
    width, height = editor.canvas_size()
    xs, ys = [], []
    for y in range(top, top + height, 2):  # the pad: a block of front-copper red in the left half of the canvas
        row = shot.raw[y * shot.width * 4:(y + 1) * shot.width * 4]
        for x in range(left, left + width // 2, 2):
            if row[x * 4 + 2] > 180 and row[x * 4 + 1] < 80 and row[x * 4] < 80:
                xs.append(x - left)
                ys.append(y - top)
    px, py = sum(xs) // len(xs), sum(ys) // len(ys)
    editor.mouse(px, py, 0.6)
    click(editor.canvas, px, py)
    time.sleep(1.0)
    before_children = hotkeys.visible_children(editor.frame)
    title_before = hotkeys.window_text(editor.frame)
    editor.key("pcbnew.PadTool.explodePad", 2.0)
    shown = hotkeys.new_messages(editor.frame, before_children)
    title_exploded = hotkeys.window_text(editor.frame)
    exploded = any("Pad Edit Mode" in m for m in shown)
    editor.key("pcbnew.PadTool.recombinePad", 2.0)
    gone = not any("Pad Edit Mode" in hotkeys.window_text(k[0]) for k in hotkeys._children(editor.frame) if k[3])
    _close_footprint_editor(env, editor)
    detail = f"pad of footprint 1005_C clicked in the footprint editor; after explode the editor says {shown} (title {title_before!r} -> {title_exploded!r})"
    return [_row("pcbnew.PadTool.explodePad", "ok" if exploded else "no_effect", detail),
            _row("pcbnew.PadTool.recombinePad", "ok" if exploded and gone else "no_effect",
                 detail + "; after recombine the Pad Edit Mode message is gone: " + str(gone))]


@cleanup_after_call
def case_swapGateNets(env: Env) -> dict:
    """A footprint with two gates: one pad of each gate selected, the gates' nets swap (pads 1,2 <-> 3,4)."""
    pads = {p.get("number", ""): p for p in _items(env.s, "KOT_PCB_PAD") if p["parent"]["value"] == _MEMO["gate_footprint"]}
    before = {n: _net(pads[n]) for n in "1234"}
    env.select(pads["1"]["id"]["value"], pads["3"]["id"]["value"])  # selection order: gate A first
    env.key("pcbnew.InteractiveEdit.swapGateNets", 0.5)
    asked = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not asked:  # the editor asks how far the swap reaches
        for hwnd, title in env.windows().items():
            if title == "Swap Gate Nets":
                for child in hotkeys._children(hwnd):
                    if hotkeys.window_text(child[0]) == "Swap All Connected Pads":
                        hotkeys.user32.PostMessageW(child[0], 0xF5, 0, 0)  # BM_CLICK
                        asked = True
        time.sleep(0.5)
    time.sleep(2.0)
    after = {}
    for number in "1234":
        item = api.call(env.s, "GetItemsById", {"items": [pads[number]["id"]]})["response"]["items"][0]
        after[number] = _net(item)
    ok = after["1"] == before["3"] and after["3"] == before["1"] and after["2"] == before["4"] and after["4"] == before["2"] and before["1"] != before["3"]
    return _row("pcbnew.InteractiveEdit.swapGateNets", "ok" if ok else "no_effect",
                f"a four-pad connector with gates (1,2) and (3,4); the editor asked how far to swap ({asked}); pad nets {before} -> {after}")


@cleanup_after_call
def case_generators(env: Env) -> list[dict]:
    """A tuning pattern and a via-stitching area are made through the API. Update All Tuning Patterns rebuilds the
    pattern's tracks (new track ids) and leaves the stitching area alone; Rebuild All Generators fills the stitching
    area with vias; Exclude From Stitching removes one of them and records its cell."""
    import math

    def length(track: dict) -> float:
        return math.hypot(int(track["end"]["x_nm"]) - int(track["start"]["x_nm"]), int(track["end"]["y_nm"]) - int(track["start"]["y_nm"]))

    tracks = sorted((t for t in _items(env.s, "KOT_PCB_TRACE") if t["layer"] == "BL_F_Cu" and _net(t)), key=length, reverse=True)
    base = tracks[0]
    pattern = {"@type": "type.googleapis.com/kiapi.board.types.TuningPattern", "layer": "BL_F_Cu", "origin": base["start"], "end": base["end"],
               "mode": "TPM_SINGLE", "settings": {"initial_side": "TPMS_LEFT", "corner_style": "TPCS_ROUNDED", "corner_radius_percent": 100,
                                                  "max_amplitude": {"value_nm": 2 * MM}, "min_amplitude": {"value_nm": 200_000},
                                                  "min_spacing": {"value_nm": 600_000}},
               "target_mode": "TPTM_LENGTH", "target_length": {"opt": int(length(base) * 1.5)}}
    via = json.loads(json.dumps(_items(env.s, "KOT_PCB_VIA")[0]))
    for key in ("id", "parent", "@type"):
        via.pop(key, None)
    outline = [(142, 80), (158, 80), (158, 96), (142, 96)]  # inside the front GND zone, where stitching vias fit
    stitch = {"@type": "type.googleapis.com/kiapi.board.types.ViaStitchArea", "layer": "BL_F_Cu",
              "outline": {"polygons": [{"outline": {"closed": True, "nodes": [{"point": {"x_nm": x * MM, "y_nm": y * MM}} for x, y in outline]}}]},
              "pitch": {"value_nm": 2 * MM}, "layout": "VSL_PLAIN", "mode": "VSM_STITCH", "grid_settings": {"origin_offset": {}},
              "net": {"name": "GND"}, "via_template": via}
    api.call(env.s, "CreateItems", {"items": [pattern, stitch]})

    def generators() -> dict:
        found = {"tuning": [], "stitch": []}
        for g in _items(env.s, "KOT_PCB_GENERATOR"):
            found["tuning" if "TuningPattern" in g["@type"] else "stitch"].append(g)
        return found

    def members(kind: str) -> list[str]:
        return sorted(m["value"] for g in generators()[kind] for m in g.get("members", []))

    time.sleep(1.5)
    stitch0 = members("stitch")  # the area fills as soon as it exists
    api.call(env.s, "DeleteItems", {"item_ids": [{"value": stitch0[0]}]})  # a via goes missing from the area
    tuning0, stitch_gap = members("tuning"), members("stitch")
    env.key("pcbnew.Generator.regenerateAllTuning", 3.0)
    tuning1, stitch1 = members("tuning"), members("stitch")
    env.key("pcbnew.Generator.regenerateAll", 5.0)
    stitch2 = members("stitch")
    vias_before = len(_items(env.s, "KOT_PCB_VIA"))
    target = generators()["stitch"][0]
    before_cells = target.get("grid_settings", {}).get("excluded_cells", [])
    env.select(stitch2[0])
    env.key("pcbnew.Generator.excludeStitchVia", 3.0)
    after = generators()["stitch"][0]
    after_cells = after.get("grid_settings", {}).get("excluded_cells", [])
    vias_after = len(_items(env.s, "KOT_PCB_VIA"))
    return [
        _row("pcbnew.Generator.regenerateAllTuning", "ok" if tuning0 and tuning1 and tuning0 != tuning1 and len(stitch1) == len(stitch_gap) else "no_effect",
             f"tuning pattern on a {length(base) / MM:.1f} mm track: its track ids {tuning0} -> {tuning1}; the stitching area, one via short, was left alone "
             f"({len(stitch_gap)} -> {len(stitch1)} vias)"),
        _row("pcbnew.Generator.regenerateAll", "ok" if len(stitch_gap) == len(stitch0) - 1 and len(stitch2) == len(stitch0) else "no_effect",
             f"via-stitching area with one via deleted: {len(stitch1)} vias -> {len(stitch2)} after Rebuild All Generators (it had {len(stitch0)})"),
        _row("pcbnew.Generator.excludeStitchVia", "ok" if len(after.get("members", [])) == len(stitch2) - 1 and len(after_cells) == len(before_cells) + 1 and vias_after == vias_before - 1 else "no_effect",
             f"one stitching via selected: members {len(stitch2)} -> {len(after.get('members', []))}, excluded cells {len(before_cells)} -> {len(after_cells)}, board vias {vias_before} -> {vias_after}"),
    ]


def _dialog(env: Env, title: str, seconds: float = 20.0) -> int | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for hwnd, name in env.windows().items():
            if name == title:
                return hwnd
        time.sleep(0.4)
    return None


@cleanup_after_call
def case_showWizards(env: Env) -> dict:
    """The footprint editor's Create Footprint opens the Footprint Wizard frame, which asks for a wizard at once;
    with that dialog closed, Show Wizards Selector opens it again."""
    from kicad_mcp.coverage.live_hotkeys import open_hosted

    open_hosted(env.s, "fpedit")
    editor = _wait_frame(env, "Footprint Editor")
    editor.key("pcbnew.ModuleEditor.createFootprint", 0.5)
    first = _dialog(env, "Footprint Wizards")
    if first is None:
        return _row("pcbnew.FpWizard.showWizards", "no_effect", "the Footprint Wizard frame did not open")
    hotkeys.user32.PostMessageW(first, hotkeys.WM_CLOSE, 0, 0)
    time.sleep(2)
    wizard = _frame_env(env, "Footprint Wizard")
    wizard.key("pcbnew.FpWizard.showWizards", 0.5)
    again = _dialog(env, "Footprint Wizards", 15.0)
    if again is not None:
        hotkeys.user32.PostMessageW(again, hotkeys.WM_CLOSE, 0, 0)
        time.sleep(1.5)
    hotkeys.user32.PostMessageW(wizard.frame, hotkeys.WM_CLOSE, 0, 0)
    time.sleep(2)
    _close_footprint_editor(env, editor)
    return _row("pcbnew.FpWizard.showWizards", "ok" if again is not None else "no_effect",
                "Footprint Editor > Create Footprint opened the Footprint Wizard frame and its wizard list; that list was closed and Show Wizards "
                f"Selector opened it again: {again is not None}")


CASES2 = {
    "pcbnew.InspectionTool.CompareBoardWithHistory": case_CompareBoardWithHistory,
    "pcbnew.Autoplacer.autoplaceOffboard": case_autoplaceOffboard,
    "pcbnew.InteractiveEdit.deleteFull": case_deleteFull,
    "pcbnew.InteractiveEdit.editVertices": case_editVertices,
    "pcbnew.PointEditor.removeCorner": case_removeCorner,
    "pcbnew.PointEditor.chamferCorner": case_chamferCorner,
    "pcbnew.InteractiveEdit.moveMidpoint": case_moveMidpoint,
    "pcbnew.InteractiveEdit.skip": case_skip,
    C + "localRatsnestTool": case_localRatsnestTool,
    "pcbnew.InteractiveEdit.swapGateNets": case_swapGateNets,
    "pcbnew.Generator.regenerateAllTuning": case_generators,
    "pcbnew.FpWizard.showWizards": case_showWizards,
    C + "nextFootprint": case_nextPreviousFootprint,
    "pcbnew.PadTool.explodePad": case_explodeRecombinePad,
    C + "layerPairPresetCycle": case_layerPairPresetCycle,
}
# ---- phase 4: interactive tools reached only through the canvas -----------------------------------------

def _view_on_first_footprint(env: Env):
    """Zoom on the first footprint and return (its position in nm, board nm -> canvas pixels)."""
    footprint = _items(env.s, "KOT_PCB_FOOTPRINT")[0]
    centre = (int(footprint["position"]["x_nm"]), int(footprint["position"]["y_nm"]))
    env.select(footprint["id"]["value"])
    env.zoom_selection()
    env.select()
    return centre, env.calibrate(centre, (-1.0, 1.0))


@cleanup_after_call
def case_pickerTool(env: Env) -> dict:
    """The generic picker (common.InteractivePicker.pickerTool) is what every "click a point" tool runs: Set Grid
    Origin hands it a click handler. A plain click leaves the grid origin alone; with the picker running, the same
    click moves the grid origin there (read back through the API)."""
    from kicad_mcp.coverage.live_tools import click

    centre, to_px = _view_on_first_footprint(env)
    target = (centre[0] + 2 * MM, centre[1] + 1 * MM)
    x, y = to_px(*target)
    origin = lambda: api.call(env.s, "GetBoardOrigin", {"type": "BOT_GRID"})["response"]  # noqa: E731
    before = origin()
    env.mouse(x, y, 0.4)
    click(env.canvas, x, y)
    env.escape(2)
    env.idle()
    plain = origin()
    env.key("common.Control.gridSetOrigin", 1.0)
    env.mouse(x, y, 0.4)
    click(env.canvas, x, y)
    time.sleep(1.0)
    env.idle()
    after = origin()
    got = (int(after.get("x_nm", 0)), int(after.get("y_nm", 0)))
    near = abs(got[0] - target[0]) <= MM and abs(got[1] - target[1]) <= MM
    ok = plain == before and near and after != before
    return _row("common.InteractivePicker.pickerTool", "ok" if ok else "no_effect",
                f"grid origin {before or '(0,0)'} -> after a plain click {plain or '(0,0)'} (unchanged {plain == before}); with the picker "
                f"running (Set Grid Origin) the same click at board point {target} -> origin {after} (within 1 mm, grid snap: {near})")


def _dump(folder: Path, **shots) -> None:
    """Debugging aid: write captures as PNG files (KICAD_AI_DUMP=<folder>)."""
    from PIL import Image

    folder.mkdir(parents=True, exist_ok=True)
    for name, shot in shots.items():
        Image.frombuffer("RGBA", (shot.width, shot.height), bytes(shot.raw), "raw", "BGRA", 0, 1).save(folder / f"{name}.png")


def _band_counts(env: Env, a_img, b_img, p: tuple[int, int], q: tuple[int, int], reach: int = 50) -> tuple[int, int]:
    """Differing canvas pixels within ``reach`` of the segment p-q, and elsewhere."""
    left, top = _canvas_origin(env)
    width, height = env.canvas_size()
    (px, py), (qx, qy) = p, q
    dx, dy = qx - px, qy - py
    length2 = dx * dx + dy * dy or 1
    inside = outside = 0
    for y in range(height):
        wy = top + y
        ra, rb = a_img.raw[wy * a_img.width * 4:(wy + 1) * a_img.width * 4], b_img.raw[wy * a_img.width * 4:(wy + 1) * a_img.width * 4]
        if ra == rb:
            continue
        for x in range(width):
            wx = left + x
            if ra[wx * 4:wx * 4 + 3] != rb[wx * 4:wx * 4 + 3]:
                t = max(0.0, min(1.0, ((x - px) * dx + (y - py) * dy) / length2))
                cx, cy = px + t * dx - x, py + t * dy - y
                if cx * cx + cy * cy <= reach * reach:
                    inside += 1
                else:
                    outside += 1
    return inside, outside


def _status_texts(env: Env) -> set[str]:
    """The board editor's status-bar and panel texts (UI Automation); the active tool's name is one of them."""
    if getattr(env, "_uia", None) is None:
        env._uia = env.s.attach_uia()
    window = {"name": "PCB Editor", "match": "contains"}
    texts = env._uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                                  "control_type": "Text"}})["controls"]
    return {t["name"].strip() for t in texts}


@cleanup_after_call
def case_measureTool(env: Env) -> dict:
    """Measure: the status bar names the active tool ("Measure Tool") only while it runs, and the same click at A and
    move to B draw far more pixels along A-B with it (the ruler) than between two runs with no tool."""
    from kicad_mcp.coverage.live_tools import click

    width, height = env.canvas_size()
    a, b = (width // 3, height // 2), (2 * width // 3, height // 2 + 40)
    env.select()

    def gesture():
        env.mouse(*a, 0.3)
        click(env.canvas, *a)
        env.mouse(*b, 1.2)
        return env.shot()

    plain = gesture()
    env.escape(2)
    env.idle()
    plain2 = gesture()
    idle_named = "Measure Tool" in _status_texts(env)
    env.escape(2)
    env.idle()
    noise, _ = _band_counts(env, plain, plain2, a, b)
    env.key("common.Interactive.measureTool", 1.0)
    ruler = gesture()
    active_named = "Measure Tool" in _status_texts(env)
    inside, outside = _band_counts(env, plain2, ruler, a, b)
    if os.environ.get("KICAD_AI_DUMP"):
        _dump(Path(os.environ["KICAD_AI_DUMP"]), plain2=plain2, ruler=ruler)
    env.escape(3)
    env.idle()
    after_named = "Measure Tool" in _status_texts(env)
    ok = active_named and not idle_named and not after_named and inside > 1000 and inside > 2 * noise
    return _row("common.Interactive.measureTool", "ok" if ok else "no_effect",
                f"status bar names 'Measure Tool': with no tool {idle_named}, after its key {active_named}, after Escape {after_named}; "
                f"click at {a} and move to {b}: {noise} pixels near the A-B segment differ between two runs with no tool, "
                f"{inside} with Measure active (the ruler; {outside} elsewhere, the board's own redraw)")


def _dialog_texts(env: Env, title: str) -> list[str]:
    if getattr(env, "_uia", None) is None:
        env._uia = env.s.attach_uia()
    window = {"name": title, "match": "contains"}
    texts = env._uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                                  "control_type": "Text"}})["controls"]
    return [t["name"] for t in texts]


def _wait_window(env: Env, title: str, present: bool = True, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(title in t for t in env.windows().values()) == present:
            return True
        time.sleep(0.3)
    return False


@cleanup_after_call
def case_selectPointInteractively(env: Env) -> dict:
    """Position Relative's "Select Point..." button runs pcbnew.Picker.selectPointInteractively: the dialog hides,
    a click on the canvas picks the point, and the dialog comes back naming that point as its reference."""
    from kicad_mcp.coverage.live_tools import click

    title = "Position Relative To Reference Item"
    centre, to_px = _view_on_first_footprint(env)
    footprint = _items(env.s, "KOT_PCB_FOOTPRINT")[0]
    env.select(footprint["id"]["value"])
    env.key("pcbnew.PositionRelative.positionRelative", 1.5)
    if not _wait_window(env, title):
        return _row("pcbnew.Picker.selectPointInteractively", "no_effect", "Position Relative did not open")
    before = [t for t in _dialog_texts(env, title) if t.startswith("Reference")]
    target = (_on_grid(centre[0] + 2 * MM), _on_grid(centre[1] - 1 * MM))  # the picker snaps to the 0.5 mm grid
    x, y = to_px(*target)
    env._uia.call("invoke", {"window": {"name": title, "match": "contains"},
                             "selector": {"by": "path", "name": "Select Point...", "control_type": "Button"}})
    hidden = _wait_window(env, title, present=False, timeout=4)
    env.mouse(x, y, 0.5)
    click(env.canvas, x, y)
    back = _wait_window(env, title, timeout=6)
    after = [t for t in _dialog_texts(env, title) if t.startswith("Reference")] if back else []
    for hwnd, text in env.windows().items():
        if title in text:
            hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
    env.escape(2)
    env.idle()
    want = (target[0] / MM, target[1] / MM)
    got = [tuple(float(v) for v in re.findall(r"(-?\d+\.\d+) mm", t)) for t in after if "selected point" in t]
    # within one 0.5 mm grid step: pixel calibration can land the click a step off
    named = any(len(g) == 2 and abs(g[0] - want[0]) <= 0.51 and abs(g[1] - want[1]) <= 0.51 for g in got)
    return _row("pcbnew.Picker.selectPointInteractively", "ok" if hidden and back and named and before != after else "no_effect",
                f"Position Relative open: {before}; 'Select Point...' hid it ({hidden}); a click at board point {want} mm brought it "
                f"back ({back}) reading {after} (within one 0.5 mm grid step of the click: {named})")


@cleanup_after_call
def case_pickerSubTool(env: Env) -> dict:
    """Move With Reference picks its reference point with the picker sub-tool, then moves the selection with that
    point under the pointer: the footprint moves by (target - picked point), not by (target - its own anchor)."""
    from kicad_mcp.coverage.live_tools import click

    centre, to_px = _view_on_first_footprint(env)
    footprint = _items(env.s, "KOT_PCB_FOOTPRINT")[0]
    fid = footprint["id"]["value"]
    start = (int(footprint["position"]["x_nm"]), int(footprint["position"]["y_nm"]))
    ref = (_on_grid(start[0] + 1 * MM), _on_grid(start[1] + 1 * MM))  # the picker snaps to the 0.5 mm grid
    goal = (ref[0] + 3 * MM, ref[1] + 2 * MM)
    env.select(fid)
    env.mouse(*to_px(*start), 0.3)
    env.key("pcbnew.InteractiveMove.moveWithReference", 1.0)
    env.mouse(*to_px(*ref), 0.4)
    click(env.canvas, *to_px(*ref))  # the sub-picker takes this click as the reference point
    time.sleep(0.8)
    mid = next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"]["value"] == fid) if not _api_refused(env) else None
    for frac in (0.3, 0.6, 1.0):
        env.mouse(*to_px(int(ref[0] + (goal[0] - ref[0]) * frac), int(ref[1] + (goal[1] - ref[1]) * frac)), 0.3)
    click(env.canvas, *to_px(*goal))
    time.sleep(1.0)
    env.escape(2)
    env.idle()
    moved = next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"]["value"] == fid)
    end = (int(moved["position"]["x_nm"]), int(moved["position"]["y_nm"]))
    shift = ((end[0] - start[0]) / MM, (end[1] - start[1]) / MM)
    anchor_shift = ((goal[0] - start[0]) / MM, (goal[1] - start[1]) / MM)  # what a move with no reference would give
    ok = abs(shift[0] - 3) <= 0.51 and abs(shift[1] - 2) <= 0.51         and max(abs(shift[0] - anchor_shift[0]), abs(shift[1] - anchor_shift[1])) > 0.9
    return _row("common.InteractivePicker.pickerSubTool", "ok" if ok else "no_effect",
                f"footprint at {start}; Move With Reference: reference picked (sub-picker click) at {ref}, placed at reference + (3, 2) mm: "
                f"moved by {shift} mm (expected (3, 2) within one 0.5 mm grid step: target minus the picked point; a move from the footprint's own "
                f"anchor would give {anchor_shift})"
                + ("" if mid is None else "; still unmoved after the reference click"))


def _on_grid(value_nm: int, grid_nm: int = MM // 2) -> int:
    return round(value_nm / grid_nm) * grid_nm


def _api_refused(env: Env) -> bool:
    try:
        api.call(env.s, "GetVersion", {})
        _items(env.s, "KOT_PCB_FOOTPRINT")
        return False
    except Exception:  # noqa: BLE001
        return True


def _pad_dims(pad: dict) -> tuple[int, int]:
    size = pad["pad_stack"]["copper_layers"][0]["size"]
    return int(size["x_nm"]), int(size["y_nm"])


@cleanup_after_call
def case_PushPadSettings_applied(env: Env) -> dict:
    """Push Pad Settings copies one pad's properties to the other pads of its footprint (its dialog's OK). One SMD
    pad of a connector is made larger through the API first; the others keep their size until the push."""
    pads = [p for p in _items(env.s, "KOT_PCB_PAD") if p.get("type") == "PT_SMD"]
    groups: dict[str, list[dict]] = {}
    for pad in pads:
        groups.setdefault(json.dumps(pad.get("parent", {})), []).append(pad)
    group = next(g for g in groups.values() if len(g) >= 3 and len({_pad_dims(p) for p in g}) == 1)
    source, others = group[0], group[1:]
    old = _pad_dims(source)
    new = (old[0] + 200_000, old[1] + 200_000)
    # a pad is edited through its footprint (UpdateItems ignores a pad sent on its own)
    footprint = next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"] == source.get("parent", {}).get("id", source.get("parent")))

    def edit(node) -> bool:
        if isinstance(node, dict):
            if node.get("id", {}).get("value") == source["id"]["value"] and "pad_stack" in node:
                node["pad_stack"]["copper_layers"][0]["size"] = {"x_nm": new[0], "y_nm": new[1]}
                return True
            return any(edit(v) for v in node.values())
        if isinstance(node, list):
            return any(edit(v) for v in node)
        return False

    found = edit(footprint)
    if not found:
        return _row("pcbnew.PadTool.PushPadSettings", "error", "the pad was not found inside its footprint")
    api.call(env.s, "UpdateItems", {"items": [footprint]})
    time.sleep(0.8)
    src_now = next(_pad_dims(p) for p in _items(env.s, "KOT_PCB_PAD") if p["id"]["value"] == source["id"]["value"])
    ids = [p["id"]["value"] for p in others]
    sizes = lambda: [_pad_dims(p) for p in _items(env.s, "KOT_PCB_PAD") if p["id"]["value"] in ids]  # noqa: E731
    before = sizes()
    env.select(source["id"]["value"])
    title = "Push Pad Properties"
    env.key("pcbnew.PadTool.PushPadSettings", 1.5)
    opened = _wait_window(env, title)
    pressed = ""
    if opened:
        if getattr(env, "_uia", None) is None:
            env._uia = env.s.attach_uia()
        buttons = env._uia.call("state", {"window": {"name": title, "match": "contains"},
                                         "selector": {"by": "path", "name": "*", "name_match": "any", "control_type": "Button"}})["controls"]
        pressed = next((b["name"] for b in buttons if "Current" in b["name"] or b["name"] == "OK"), "")
        env._uia.call("invoke", {"window": {"name": title, "match": "contains"},
                                 "selector": {"by": "path", "name": pressed, "control_type": "Button"}})
        _wait_window(env, title, present=False)
    env.escape(2)
    env.idle()
    after = sizes()
    ok = opened and src_now == new and set(before) == {old} and after and set(after) == {new}
    return _row("pcbnew.PadTool.PushPadSettings", "ok" if ok else "no_effect",
                f"one pad of a {len(group)}-pad footprint enlarged to {new} nm (read back {src_now}); its {len(others)} siblings {sorted(set(before))} before; "
                f"Push Pad Properties opened ({opened}), '{pressed}' pressed; siblings after: {sorted(set(after))}")




def _demos() -> Path:
    from kicad_mcp.coverage.fixtures import demos_dir

    return demos_dir()


def _press_click(env: Env, x: int, y: int) -> None:
    """A left click on the canvas with the cursor hook reporting the pointer there and the button held."""
    point = (y << 16) | x
    env.mouse(x, y, 0.2)
    env.cursor.button(True)
    hotkeys.user32.PostMessageW(env.canvas, 0x0201, 1, point)
    time.sleep(0.2)
    env.cursor.button(False)
    hotkeys.user32.PostMessageW(env.canvas, 0x0202, 0, point)
    time.sleep(0.4)


def _drop(env: Env, hwnd: int, paths: list[Path]) -> None:
    from kicad_mcp.coverage.drop_hook import DropHook

    hook = DropHook(env.s.pid)
    try:
        hook.drop(hwnd, [str(p) for p in paths])
    finally:
        hook.close()


@cleanup_after_call
def case_DdAppendBoard(env: Env) -> dict:
    """A board file dropped on the board editor is appended (Drag and Drop: append board): its footprints arrive on
    the pointer and a click places them. A dropped text file (an extension the editor does not take) is the control."""

    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-drop-"))
    board = scratch / "dropped.kicad_pcb"
    shutil.copy2(_demos() / "ecc83" / "ecc83-pp.kicad_pcb", board)
    note = scratch / "note.txt"
    note.write_text("not a board", encoding="utf-8")
    count = lambda: len(_items(env.s, "KOT_PCB_FOOTPRINT"))  # noqa: E731
    start = count()
    _drop(env, env.frame, [note])
    time.sleep(2.0)
    env.idle()
    plain = count()
    width, height = env.canvas_size()
    env.mouse(width // 2, height // 2, 0.3)
    _drop(env, env.frame, [board])
    time.sleep(4.0)
    mapping = "Edit Mapping of Imported Layers"  # the dropped board's layers are matched to this one's: accept
    asked = _wait_window(env, mapping, timeout=6)
    if asked:
        if getattr(env, "_uia", None) is None:
            env._uia = env.s.attach_uia()
        env._uia.call("invoke", {"window": {"name": mapping, "match": "contains"},
                                 "selector": {"by": "path", "name": "OK", "control_type": "Button"}})
        _wait_window(env, mapping, present=False)
        time.sleep(2.0)
    # the appended items follow the pointer (the move tool): a click, with the button reported held, places them
    for step in (10, 25, 40):
        env.mouse(width // 2 + step, height // 2 + step, 0.3)
    _press_click(env, width // 2 + 40, height // 2 + 40)
    time.sleep(2.0)
    after = None
    for _ in range(10):
        try:
            after = count()
            break
        except Exception:  # noqa: BLE001 - still placing
            _press_click(env, width // 2 + 40, height // 2 + 40)
            time.sleep(1.0)
    theirs = board.read_text(encoding="utf-8").count("\n\t(footprint ")
    ok = plain == start and after - start == theirs and theirs > 0
    return _row("pcbnew.Control.DdAppendBoard", "ok" if ok else "no_effect",
                f"footprints {start}; a dropped .txt file: {plain}; KiCad's ecc83-pp board ({theirs} footprints) dropped on the frame "
                f"(its layer mapping accepted: {asked}) and placed with a click: {after}")


@cleanup_after_call
def case_ddImportFootprint(env: Env) -> dict:
    """A footprint file dropped on the footprint editor is imported into it: the editor's title names it."""
    from kicad_mcp.coverage.live_hotkeys import open_hosted

    source = _demos() / "cm5_minima" / "CM5IO.pretty" / "BatteryHolder_Keystone_3034_1x20mm.kicad_mod"
    scratch = Path(scoped_mkdtemp(prefix="kicad-ai-tools-dropfp-"))
    footprint = scratch / "DroppedTestFootprint.kicad_mod"
    footprint.write_text(source.read_text(encoding="utf-8").replace('"BatteryHolder_Keystone_3034_1x20mm"',
                                                                    '"DroppedTestFootprint"', 1), encoding="utf-8")
    open_hosted(env.s, "fpedit")
    editor = _wait_frame(env, "Footprint Editor")
    try:
        def title() -> str:  # the frame title and the text the editor paints (its message panel names the footprint)
            return hotkeys.window_text(editor.frame) + " | " + " | ".join(sorted(editor.text()))
        before = title()
        note = scratch / "note.txt"
        note.write_text("x", encoding="utf-8")
        _drop(env, editor.frame, [note])
        time.sleep(2.0)
        plain = title()
        _drop(env, editor.frame, [footprint])
        time.sleep(4.0)
        if os.environ.get("KICAD_AI_DUMP"):
            _dump(Path(os.environ["KICAD_AI_DUMP"]), fpdrop=editor.shot())
        after = title()
        ok = "DroppedTestFootprint" not in before and "DroppedTestFootprint" not in plain and "DroppedTestFootprint" in after
        return _row("pcbnew.Control.ddImportFootprint", "ok" if ok else "no_effect",
                    f"footprint editor paints the name DroppedTestFootprint: at first {'DroppedTestFootprint' in before}, after a dropped "
                    f".txt {'DroppedTestFootprint' in plain}, after DroppedTestFootprint.kicad_mod dropped on it "
                    f"{'DroppedTestFootprint' in after} (message panel: Footprint Name)")
    finally:
        _close_footprint_editor(env, editor)


def _erased(env: Env, a, b) -> int:
    """Canvas pixels that were drawn in ``a`` and are background in ``b`` (the background is the commonest colour)."""
    import numpy as np

    left, top = _canvas_origin(env)
    width, height = env.canvas_size()

    def canvas(shot):
        image = np.frombuffer(bytes(shot.raw), dtype=np.uint8).reshape(shot.height, shot.width, 4)[..., :3]
        return image[top:top + height, left:left + width]

    ca, cb = canvas(a), canvas(b)
    colours, counts = np.unique(ca[::9, ::9].reshape(-1, 3), axis=0, return_counts=True)
    background = colours[counts.argmax()]
    drawn_a = np.any(ca != background, axis=2)
    blank_b = np.all(cb == background, axis=2)
    return int(np.count_nonzero(drawn_a & blank_b))


@cleanup_after_call
def case_rehatchShapes(env: Env) -> dict:
    """rehatchShapes is posted after every board commit (board_commit.cpp:795) and is the only thing that redoes a
    hatched shape's hatching when *another* item changes (PCB_CONTROL::RehatchShapes, pcb_control.cpp:3189; the
    hatching is knocked out around text and shapes on its layer, PCB_SHAPE::getHatchingKnockouts). A hatched
    rectangle is made; a text committed beside it erases none of its hatch lines; the same text moved onto it (a
    commit of the text alone) erases hatch lines around it."""
    pcb = "type.googleapis.com/kiapi.board.types."
    footprints = _items(env.s, "KOT_PCB_FOOTPRINT")
    cx = max(int(f["position"]["x_nm"]) for f in footprints) + 30 * MM
    cy = min(int(f["position"]["y_nm"]) for f in footprints) + 10 * MM
    rect = {"@type": pcb + "BoardGraphicShape", "layer": "BL_Dwgs_User",
            "shape": {"rectangle": {"top_left": {"x_nm": cx - 8 * MM, "y_nm": cy - 8 * MM},
                                    "bottom_right": {"x_nm": cx + 8 * MM, "y_nm": cy + 8 * MM}},
                      "attributes": {"stroke": {"width": {"value_nm": 300000}}, "fill": {"fill_type": "GFT_HATCH"}}}}
    made = api.call(env.s, "CreateItems", {"items": [rect]})["response"]
    rid = next(i["item"]["id"]["value"] for i in made.get("created_items", []) if i.get("item", {}).get("id"))
    env.select(rid)
    env.zoom_selection()
    env.select()
    env.run("common.Control.zoomOutCenter", 1.0)  # the rectangle and the spot beside it in view
    before = env.shot()
    text = {"@type": pcb + "BoardText", "layer": "BL_Dwgs_User",
            "text": {"text": "XX", "position": {"x_nm": cx + 12 * MM, "y_nm": cy}}}
    made = api.call(env.s, "CreateItems", {"items": [text]})["response"]
    tid = made["created_items"][0]["item"]
    time.sleep(1.5)
    beside = env.shot()
    moved = api.call(env.s, "GetItemsById", {"items": [tid["id"]]})["response"]["items"][0]
    moved["text"]["position"] = {"x_nm": cx, "y_nm": cy}
    api.call(env.s, "UpdateItems", {"items": [moved]})
    time.sleep(1.5)
    inside = env.shot()
    erased_beside, erased_inside = _erased(env, before, beside), _erased(env, beside, inside)
    shape = next(s for s in _items(env.s, "KOT_PCB_SHAPE") if s["id"]["value"] == rid)
    hatched = shape.get("shape", {}).get("attributes", {}).get("fill", {}).get("fill_type")
    ok = hatched == "GFT_HATCH" and erased_beside < 20 and erased_inside > 200
    return _row("pcbnew.Control.rehatchShapes", "ok" if ok else "no_effect",
                f"hatched rectangle ({hatched}) on User.Drawings; a text committed beside it erased {erased_beside} drawn canvas "
                f"pixels; the text alone moved onto the rectangle's centre (its own commit): {erased_inside} drawn pixels became "
                f"background (the rectangle's hatch knocked out around it, i.e. the rectangle re-hatched after another item's commit)")


CHAIN_NET = "/D+"


def prepare_chain(work_dir: Path) -> None:
    """A net chain over /D+ whose terminal A is the net's first pad (pcbnew's (net_chains ...) block, parsed by
    PCB_IO_KICAD_SEXPR_PARSER::parseNET_CHAINS_SECTION)."""
    board = next(work_dir.glob("*.kicad_pcb"))
    text = board.read_text(encoding="utf-8")
    first = re.search(r'\(net \d+ "' + re.escape(CHAIN_NET) + r'"\)[\s\S]*?\(uuid "([0-9a-f-]+)"\)', text[text.index("(footprint"):])
    block = f'\t(net_chains (net_chain (name "CHAIN1") (members (net "{CHAIN_NET}")) (terminal_pad "{first.group(1)}")))\n'
    text = text.replace("\t(embedded_fonts no)\n)", block + "\t(embedded_fonts no)\n)", 1)
    board.write_text(text, encoding="utf-8")


def _terminal_pads(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    start = text.find("(net_chains")
    return re.findall(r'\(terminal_pad "([0-9a-f-]+)"\)', text[start:]) if start >= 0 else []


def _menu_path(env: Env, labels: list[str]) -> list[list[str]]:
    """Walk an open context menu by keys, as a person does: Down to each label and Right into it (Return on the
    last). Returns the entries seen at each level."""
    from kicad_mcp.coverage.live_designblocks import Run as BlockRun

    seen = []
    known: list[int] = []
    for depth, label in enumerate(labels):
        menus = []
        for _ in range(20):
            menus = [w["hwnd"] for w in env.s.kicad.desktop.windows()
                     if w["pid"] == env.s.pid and w["visible"] and w["class"] == "#32768"]
            if [m for m in menus if m not in known]:
                break
            time.sleep(0.25)
        if not [m for m in menus if m not in known]:
            break
        menu = next(m for m in menus if m not in known)  # the submenu that just opened
        known.append(menu)
        entries = BlockRun.popup_entries(menu)
        seen.append(entries)
        selectable = [e for e in entries if e]
        index = next((i for i, e in enumerate(selectable) if e.replace("&", "").split("\t")[0].strip().startswith(label)), None)
        if index is None:
            hotkeys.press(menu, hotkeys.VK_ESCAPE)
            break
        # a menu opened by the mouse has nothing highlighted; a submenu opened by Right starts on its first entry
        for _ in range(index + 1 if depth == 0 else index):
            hotkeys.press(menu, 0x28)  # Down
            time.sleep(0.25)
        hotkeys.press(menu, 0x0D if depth == len(labels) - 1 else 0x27)  # Return on the last, Right into a submenu
        time.sleep(1.0)
    return seen


@cleanup_after_call
def case_setTerminalPad(env: Env) -> dict:
    """With the /D+ net chain highlighted (its key over one of the chain's pads), another /D+ pad's context menu
    (Net Chains... > Set terminal pad > Terminal B) sets that pad as the chain's terminal B: the saved board's
    (net_chains ...) block names it. The board saved before the menu names only terminal A (the control)."""
    board = Path(env.s.document)
    pads = [p for p in _items(env.s, "KOT_PCB_PAD") if _net(p) == CHAIN_NET]
    first = _terminal_pads(board)
    target = next(p for p in pads if p["id"]["value"] not in first)
    centre = (int(target["position"]["x_nm"]), int(target["position"]["y_nm"]))
    owner = target.get("parent", {})
    env.select(next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"] == owner.get("id", owner))["id"]["value"])
    env.zoom_selection()  # the pad's footprint fills the view (a pad alone zooms too far for calibration)
    env.select()
    to_px = env.calibrate(centre, (-1.0, 1.0))
    x, y = to_px(*centre)
    env.run("common.Control.save", 2.0)
    before = _terminal_pads(board)
    env.mouse(x, y, 0.5)
    env.key("pcbnew.EditorControl.highlightNetChain", 1.2)
    point = (y << 16) | x
    env.select(target["id"]["value"])  # the pad itself (a click would take its footprint); a right-click on it keeps it
    env.mouse(x, y, 0.3)
    env.cursor.right_button(True)
    hotkeys.user32.PostMessageW(env.canvas, 0x0204, 2, point)
    time.sleep(0.15)
    env.cursor.right_button(False)
    hotkeys.user32.PostMessageW(env.canvas, 0x0205, 0, point)
    time.sleep(1.0)
    seen = _menu_path(env, ["Net Chains", "Set terminal pad", "Terminal B"])
    time.sleep(1.0)
    env.escape(2)
    env.idle()
    env.run("common.Control.save", 2.0)
    after = _terminal_pads(board)
    # the saved block before the menu is the control (KiCad drops a terminal pad read from the file at its next
    # save: NETINFO_LIST rebuilds clear the pad pointers and only the parser re-resolves them)
    ok = target["id"]["value"] not in before and target["id"]["value"] in after
    return _row("pcbnew.EditorControl.setTerminalPad", "ok" if ok else "no_effect",
                f"net chain CHAIN1 over {CHAIN_NET} (terminal A {first} in the file); saved terminal pads before the menu {before}; chain highlighted with its key over pad "
                f"{target['id']['value']}, then its context menu walked {[[e for e in level if e][:6] for level in seen]}; saved "
                f"terminal pads after {after}")


def prepare_chain_netlist(work_dir: Path) -> None:
    """The schematic gains a net chain over /D+ from J1 pin 3 to U1 pin 30 (its (net_chain ...) form,
    sch_io_kicad_sexpr.cpp:581), and kicad-cli exports the project's netlist, which carries the chain and its
    terminal pins (netlist_exporter_xml.cpp:1388), to chain.net beside the board."""
    import subprocess

    from kicad_mcp.ipc.session import find_kicad_cli

    sheet = next(p for p in work_dir.glob("*.kicad_sch") if p.stem == next(work_dir.glob("*.kicad_pro")).stem)
    text = sheet.read_text(encoding="utf-8")
    end = text.rstrip().rfind(")")
    sheet.write_text(text[:end] + f'\t(net_chain "CHAIN1" (from "J1" "3") (to "U1" "30") (nets "{CHAIN_NET}"))\n' + text[end:],
                     encoding="utf-8")
    subprocess.run([find_kicad_cli(), "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(work_dir / "chain.net"),
                    str(sheet)], capture_output=True, timeout=300, creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    if "(terminal_pins" not in (work_dir / "chain.net").read_text(encoding="utf-8"):
        raise RuntimeError("the exported netlist has no net chain terminals")


def _pad_uuid(env: Env, ref: str, number: str) -> str:
    """The /D+ pad with this number (J1-3, D23-2 and U1-30 are the net's only pads; ``ref`` names it in the report)."""
    return next(p["id"]["value"] for p in _items(env.s, "KOT_PCB_PAD") if _net(p) == CHAIN_NET and p.get("number") == number)


def _aim(env: Env, to_px, target_nm: tuple[int, int], tries: int = 8) -> tuple[int, int]:
    """Put the pointer on a board point, corrected by the status bar's cursor readout ("X 163.6000  Y 109.9800", mm):
    the marker calibration can be half a grid step off, too much for a small pad."""
    x, y = to_px(*target_nm)
    probe = to_px(target_nm[0] + MM, target_nm[1] + MM)
    scale = ((probe[0] - x) / MM, (probe[1] - y) / MM)  # pixels per nm on each axis
    for _ in range(tries):
        env.mouse(x + 3, y + 3, 0.2)  # a real move each time, so the readout follows
        env.mouse(x, y, 1.0)
        shown = next((t for t in _status_texts(env) if t.startswith("X ") and " Y " in t), "")
        numbers = re.findall(r"-?\d+\.\d+", shown)
        if len(numbers) < 2:
            break
        dx, dy = target_nm[0] - float(numbers[0]) * MM, target_nm[1] - float(numbers[1]) * MM
        if abs(dx) < 0.05 * MM and abs(dy) < 0.05 * MM:
            break
        x, y = round(x + dx * scale[0]), round(y + dy * scale[1])
    return x, y


@cleanup_after_call
def case_setTerminalPad_netlist(env: Env) -> dict:
    """Terminal pads exist in a session once a netlist update sets them (board_netlist_updater.cpp:2712; pads read
    from a board file lose them at load, see case_setTerminalPad). The netlist with the /D+ chain (J1-3 to U1-30) is
    imported (Import Netlist > Update PCB), then D23 pad 2's context menu Net Chains... > Set terminal pad >
    Terminal B makes it terminal B: the saved board's chain names D23-2 in place of U1-30."""
    board = Path(env.s.document)
    netlist = board.parent / "chain.net"
    title = "Import Netlist"
    env.key("pcbnew.EditorControl.importNetlist", 2.0)
    if not _wait_window(env, title):
        return _row("pcbnew.EditorControl.setTerminalPad", "no_effect", "Import Netlist did not open")
    if getattr(env, "_uia", None) is None:
        env._uia = env.s.attach_uia()
    window = {"name": title, "match": "exact"}
    edits = env._uia.call("state", {"window": window, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                                  "control_type": "Edit"}})["controls"]
    # the file box is the dialog's first text box (the report pane below it is the other)
    env._uia.call("set-value", {"window": window, "selector": {"by": "automation_id", "automation_id": edits[0]["automation_id"]},
                                "value": str(netlist)})
    time.sleep(0.5)
    env._uia.call("invoke", {"window": window, "selector": {"by": "path", "name": "Update PCB", "control_type": "Button"}})
    time.sleep(6.0)
    for hwnd, text in env.windows().items():  # its Close button shares its name with the title bar's
        if text == title:
            hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
    _wait_window(env, title, present=False)
    env.idle()
    j1, u1, d23 = _pad_uuid(env, "J1", "3"), _pad_uuid(env, "U1", "30"), _pad_uuid(env, "D23", "2")
    env.run("common.Control.save", 2.0)
    imported = _terminal_pads(board)
    target = next(p for p in _items(env.s, "KOT_PCB_PAD") if p["id"]["value"] == d23)
    centre = (int(target["position"]["x_nm"]), int(target["position"]["y_nm"]))
    owner = target.get("parent", {})
    env.select(next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"] == owner.get("id", owner))["id"]["value"])
    env.zoom_selection()
    env.select()
    to_px = env.calibrate(centre, (-1.0, 1.0))
    x, y = _aim(env, to_px, centre)
    aimed = next((t for t in _status_texts(env) if t.startswith("X ")), "")
    env.key("pcbnew.EditorControl.highlightNetChain", 1.2)
    if os.environ.get("KICAD_AI_DUMP"):
        _dump(Path(os.environ["KICAD_AI_DUMP"]), highlighted=env.shot())
        print("aimed", aimed, centre, flush=True)
    env.select(d23)
    env.mouse(x, y, 0.3)
    point = (y << 16) | x
    env.cursor.right_button(True)
    hotkeys.user32.PostMessageW(env.canvas, 0x0204, 2, point)
    time.sleep(0.15)
    env.cursor.right_button(False)
    hotkeys.user32.PostMessageW(env.canvas, 0x0205, 0, point)
    time.sleep(1.0)
    seen = _menu_path(env, ["Net Chains", "Set terminal pad", "Terminal B"])
    time.sleep(1.0)
    env.escape(2)
    env.idle()
    env.run("common.Control.save", 2.0)
    after = _terminal_pads(board)
    ok = sorted(imported) == sorted([j1, u1]) and sorted(after) == sorted([j1, d23])
    return _row("pcbnew.EditorControl.setTerminalPad", "ok" if ok else "no_effect",
                f"netlist with net chain CHAIN1 (/D+, J1-3 to U1-30) imported: saved terminal pads {imported} (J1-3 {j1}, U1-30 {u1}); "
                f"chain highlighted over D23-2 ({d23}), its context menu walked {[[e for e in level if e][:6] for level in seen]}; "
                f"saved terminal pads after: {after}")


CASES8 = {"pcbnew.EditorControl.setTerminalPad": case_setTerminalPad_netlist}
KEYS8 = ["pcbnew.EditorControl.highlightNetChain", "pcbnew.EditorControl.importNetlist"]


@cleanup_after_call
def case_updateLocalRatsnest(env: Env) -> dict:
    """While a selection is moved, the move tool posts updateLocalRatsnest with the movement
    (edit_tool_move_fct.cpp:1649); its handler computes the selection's own ratsnest
    (BOARD_INSPECTION_TOOL::UpdateLocalRatsnest, board_inspection_tool.cpp:3054), drawn even with the global
    ratsnest off. With /D+ unrouted and the global ratsnest off, the selected connector shows no ratsnest-blue lines;
    moved by the move tool, it does."""
    _unroute_net(env, "/D+")
    pads = [p for p in _items(env.s, "KOT_PCB_PAD") if _net(p) == "/D+"]
    owner = pads[0].get("parent", {})
    footprint = next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"] == owner.get("id", owner))
    env.key(C + "showRatsnest", 1.2)  # global ratsnest off
    env.select(footprint["id"]["value"])
    env.zoom_selection()
    env.run("common.Control.zoomOutCenter", 1.0)
    centre = (int(footprint["position"]["x_nm"]), int(footprint["position"]["y_nm"]))
    to_px = env.calibrate(centre, (-1.0, 1.0))
    env.select(footprint["id"]["value"])
    time.sleep(1.0)
    x, y = to_px(*centre)
    env.mouse(x, y, 0.6)
    still = env.shot()
    env.key("pcbnew.InteractiveMove.move", 1.0)
    for step in range(1, 6):
        env.mouse(x + 12 * step, y + 8 * step, 0.4)
    moving = env.shot()
    lines = _canvas_diff_count(env, still, moving, cyan=True)
    env.escape(3)
    env.idle()
    after = next(f for f in _items(env.s, "KOT_PCB_FOOTPRINT") if f["id"] == footprint["id"])
    unmoved = after["position"] == footprint["position"]
    ok = lines > 50 and unmoved
    return _row("pcbnew.Control.updateLocalRatsnest", "ok" if ok else "no_effect",
                f"global ratsnest off, net /D+ unrouted, its connector selected: no move -> moving it with the move tool (pointer "
                f"carried 60 px): {lines} ratsnest-blue canvas pixels appear (the selection's own ratsnest); Escape put it back "
                f"({unmoved})")


CASES6 = {
    "pcbnew.Picker.selectPointInteractively": case_selectPointInteractively,
    "common.InteractivePicker.pickerSubTool": case_pickerSubTool,
    "pcbnew.PadTool.PushPadSettings": case_PushPadSettings_applied,
    "pcbnew.Control.DdAppendBoard": case_DdAppendBoard,
    "pcbnew.Control.ddImportFootprint": case_ddImportFootprint,
    "pcbnew.Control.rehatchShapes": case_rehatchShapes,
    "pcbnew.Control.updateLocalRatsnest": case_updateLocalRatsnest,
}
KEYS6 = ["pcbnew.PositionRelative.positionRelative", "pcbnew.InteractiveMove.moveWithReference",
         "pcbnew.PadTool.PushPadSettings", C + "showRatsnest", "pcbnew.InteractiveMove.move"]


CASES4 = {
    "common.InteractivePicker.pickerTool": case_pickerTool,
    "common.Interactive.measureTool": case_measureTool,
}
KEYS4 = [*CASES4, "common.Control.gridSetOrigin"]


# Interactive tools (no RunAction route) this module proves by driving them on the canvas; the gate routes them here
TOOL_CASES = [C + "localRatsnestTool", *CASES4]
GAPS2: dict[str, str] = {}
KEYS2 = [*CASES2, C + "showRatsnest", "pcbnew.InteractiveMove.moveIndividually", "pcbnew.InteractiveEdit.skip",
         "pcbnew.InteractiveEdit.swapGateNets", E + "setTerminalPad", "pcbnew.Generator.regenerateAll",
         "pcbnew.Generator.regenerateAllTuning", "pcbnew.Generator.excludeStitchVia",
         "pcbnew.PadTool.explodePad", "pcbnew.PadTool.recombinePad", C + "nextFootprint", C + "previousFootprint",
         "pcbnew.ModuleEditor.createFootprint", "pcbnew.FpWizard.showWizards"]


def _add_gate_units(board: Path) -> None:
    """Give one four-pad connector two gates (pins 1,2 and 3,4), as a multi-gate part's footprint carries them,
    so Swap Gate Nets has gates to swap. The footprint is the first JST_SH_SM04B of the board file."""
    import re

    text = board.read_text(encoding="utf-8")
    start = text.index('(footprint "footprints:JST_SH_SM04B')
    uuid = re.search(r'\n\t\t\(uuid "([0-9a-f-]+)"\)', text[start:]).group(1)
    sheet = text.index("(sheetfile", start)
    line_end = text.index("\n", sheet)
    units = '\n\t\t(units (unit (name "A") (pins "1" "2")) (unit (name "B") (pins "3" "4")))'
    board.write_text(text[:line_end] + units + text[line_end:], encoding="utf-8")
    _MEMO["gate_footprint"] = uuid


def prepare_main(work_dir: Path) -> None:
    """What a few actions need before the editor starts: two layer pair presets (the project file's
    ``board.layer_pairs``) and a local-history repository holding a snapshot of the board."""
    import subprocess

    pro = next(work_dir.glob("*.kicad_pro"))
    data = json.loads(pro.read_text(encoding="utf-8"))
    data.setdefault("board", {})["layer_pairs"] = [
        {"topLayer": 0, "bottomLayer": 2, "enabled": True, "name": "outer"},
        {"topLayer": 4, "bottomLayer": 6, "enabled": True, "name": "inner"}]
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _add_gate_units(next(work_dir.glob("*.kicad_pcb")))
    history = work_dir / ".history"
    history.mkdir(exist_ok=True)
    for name in (next(work_dir.glob("*.kicad_pcb")).name, pro.name):
        shutil.copy2(work_dir / name, history / name)
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.name=kicad-ai-tools", "-c", "user.email=kicad-ai-tools@localhost",
                  "commit", "-q", "-m", "snapshot of the test board"]):
        subprocess.run(["git", "-C", str(history), *args], check=True, capture_output=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)


def start_session(project: Path, keys: list[str], all_actions: list[str], prepare=None, settings: dict | None = None) -> Env:
    session = GuiSession(next(project.glob("*.kicad_pcb")))
    config = session.profile / "config" / session._settings_version()
    assigned = hotkeys.write_hotkeys(config, all_actions, keys[:len(hotkeys.KEYS)])
    if prepare:
        prepare(session.candidate.work_dir)
    if settings:  # pcbnew.json values the profile starts with (dotted keys)
        config.mkdir(parents=True, exist_ok=True)
        document: dict = {"meta": {"version": 5}}
        for dotted, value in settings.items():
            *parents, leaf = dotted.split(".")
            node = document
            for part in parents:
                node = node.setdefault(part, {})
            node[leaf] = value
        (config / "pcbnew.json").write_text(json.dumps(document, indent=2), encoding="utf-8")
    session.start()
    time.sleep(3)
    frame, _ = hotkeys.canvas_of(session, "PCB Editor")
    hotkeys.settled_capture(frame, timeout_s=15.0)
    return Env(session, assigned)


@cleanup_after_call
def sweep(inventory: dict, project: Path, only: list[str] | None = None, phase: int = 1) -> dict:
    all_actions = [a["name"] for a in inventory["actions"]]
    cases, gap_table = {1: (CASES, GAPS), 2: (CASES2, GAPS2), 4: (CASES4, {}), 6: (CASES6, {}), 8: (CASES8, {})}.get(phase, (CASES2, GAPS2))
    names = [n for n in cases if not only or n in only]
    rows: list[dict] = []
    if phase in (3, 5, 7):
        rows = {3: settings_phase, 5: router_mode_phase, 7: angle_snap_phase}[phase](inventory, project)
        return _evidence(inventory, rows, {}, None)
    keys = [*names, E + "showNet", "pcbnew.MatchProperties.match"] if phase == 1 else KEYS4 if phase == 4 else KEYS6 if phase == 6 else KEYS8 if phase == 8 else KEYS2
    env = start_session(project, keys, all_actions, prepare_chain_netlist if phase == 8 else prepare_main)
    try:
        for name in names:
            try:
                env.idle()
                got = cases[name](env)
                rows.extend(got if isinstance(got, list) else [got])
            except Exception as exc:  # noqa: BLE001
                rows.append(_row(name, "error", f"{type(exc).__name__}: {exc}"))
            with contextlib.suppress(Exception):
                env.escape(2)
    finally:
        env.close()
        env.s.close(discard=True)
    return _evidence(inventory, rows, gap_table, only)


def _evidence(inventory: dict, rows: list[dict], gap_table: dict[str, str], only: list[str] | None) -> dict:
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    gaps = sorted(f"action:{n}" for n in gap_table if not only or n in only)
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "board-misc-live", "passed": passed,
            "upstream_gaps": gaps, "gap_reasons": {f"action:{n}": gap_table[n] for n in gap_table}, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="")
    parser.add_argument("--phase", type=int, default=1)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project,
                   [n for n in args.only.split(",") if n] or None, args.phase)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
