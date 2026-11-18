"""Live proof for the router's in-route actions, read back from the board through the API.

Each case routes on a multi-layer board (KiCad's RoyalBlue54L Feather demo: six copper layers) as a person does:
the router is started (its key), the pointer is put on the canvas with the in-process cursor hook, clicks fix
segments, and the action's own key is pressed mid-route. Escape ends the route and keeps what was fixed. The
tracks and vias the route left on the board are then read through the API and compared with a control route
made by the same gesture without the action (or with the plain through-via key). The router runs in its
highlight-collisions mode (tools.pns.mode 0, preset before the editor starts) so obstacles never bend a route.

    python -m kicad_mcp.coverage.live_router --inventory inv.json --project <royalblue dir> --out ev.json
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
import os
from pathlib import Path
import time

from kicad_mcp.coverage.live_board_misc import (
    MM,
    Env,
    _dump,
    _items,
    _row,
    _status_texts,
    start_session,
)
from kicad_mcp.ipc import api, hotkeys

WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
R = "pcbnew.InteractiveRouter."
ROUTE = R + "SingleTrack"
CYCLE = "pcbnew.Control.layerPairPresetCycle"
IN2 = "pcbnew.Control.layerInner2"
ARC = "pcbnew.InteractiveDrawing.arc"
KINDS = ("KOT_PCB_TRACE", "KOT_PCB_ARC", "KOT_PCB_VIA", "KOT_PCB_SHAPE")


def prepare(work_dir: Path) -> None:
    """A layer pair preset F.Cu/In1.Cu (blind and micro vias need a pair that is not the outer layers) and a
    microvia stack preset F.Cu -> In2.Cu (Place Via Stack does nothing without one), in the project file."""
    pro = next(work_dir.glob("*.kicad_pro"))
    data = json.loads(pro.read_text(encoding="utf-8"))
    board = data.setdefault("board", {})
    board["layer_pairs"] = [{"topLayer": 0, "bottomLayer": 4, "enabled": True, "name": "F-In1"}]
    board.setdefault("design_settings", {})["track_widths"] = [0.0, 0.4, 0.6]  # for the width notification
    board["design_settings"]["via_stack_presets"] = [
        {"name": "uvia-F-In2", "start_layer": "F.Cu", "end_layer": "In2.Cu", "staggered": False, "via_size": 0.25,
         "via_drill": 0.1, "use_netclass": False, "filled": True, "capped": False, "pitch": 0.0}]
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")


class Router:
    def __init__(self, env: Env):
        self.env = env
        self.to_px = None
        self.origin = (0, 0)

    def snapshot(self) -> dict[str, dict]:
        for attempt in range(12):  # a tool still holding the canvas makes KiCad refuse item requests
            try:
                return {i["id"]["value"]: i for kind in KINDS for i in _items(self.env.s, kind)}
            except api.ApiCallError:
                if attempt == 11:
                    if os.environ.get("KICAD_AI_DUMP"):
                        _dump(Path(os.environ["KICAD_AI_DUMP"]), busy=self.env.shot())
                        print("windows:", self.env.windows())
                    raise
                # a properties window the finishing double click opened on the new track (it opens late)
                for hwnd, title in self.env.windows().items():
                    if title.endswith("Properties"):
                        hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
                        self.opened = [*getattr(self, "opened", []), title]
                self.env.escape(1)
                time.sleep(0.5)
        return {}

    def new_items(self, before: dict[str, dict]) -> list[dict]:
        return [i for k, i in self.snapshot().items() if k not in before]

    def view(self, centre_nm: tuple[int, int], box: tuple = ((-4, -4), (52, 36))) -> None:
        """Centre the view on ``centre_nm`` at a routing zoom and calibrate board nm -> canvas pixels."""
        template = _items(self.env.s, "KOT_PCB_VIA")[0]
        corners = []
        for dx, dy in box:  # the area the routes use, with a margin
            via = json.loads(json.dumps(template))
            for key in ("id", "parent", "net"):
                via.pop(key, None)
            via["position"] = {"x_nm": centre_nm[0] + dx * MM, "y_nm": centre_nm[1] + dy * MM}
            corners.append(via)
        made = api.call(self.env.s, "CreateItems", {"items": corners})["response"]
        ids = [i["item"]["id"]["value"] for i in made.get("created_items", []) if i.get("item", {}).get("id")]
        self.env.select(*ids)
        self.env.zoom_selection()
        self.env.select()
        if ids:
            api.call(self.env.s, "DeleteItems", {"item_ids": [{"value": i} for i in ids]})
        self.to_px = self.env.calibrate(centre_nm, (-1.0, 1.0))
        self.origin = centre_nm

    def px(self, dx_mm: float, dy_mm: float) -> tuple[int, int]:
        return self.to_px(self.origin[0] + int(dx_mm * MM), self.origin[1] + int(dy_mm * MM))

    def at(self, dx_mm: float, dy_mm: float, press: bool = True, double: bool = False) -> None:
        x, y = self.px(dx_mm, dy_mm)
        self.env.mouse(x, y, 0.4)
        if double:
            point = (y << 16) | x
            for message in (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_LBUTTONUP):
                down = message != WM_LBUTTONUP
                self.env.cursor.button(down)
                hotkeys.user32.PostMessageW(self.env.canvas, message, 1 if down else 0, point)
                time.sleep(0.08)
            time.sleep(0.8)
        elif press:
            # KiCad's tool dispatcher reads the button state as well as the message: the hook reports it held
            point = (y << 16) | x
            self.env.cursor.button(True)
            hotkeys.user32.PostMessageW(self.env.canvas, WM_LBUTTONDOWN, 1, point)
            time.sleep(0.25)
            self.env.cursor.button(False)
            hotkeys.user32.PostMessageW(self.env.canvas, WM_LBUTTONUP, 0, point)
            time.sleep(0.6)

    def route(self, steps, shift: float = 0.0, tool: str = ROUTE) -> list[dict]:
        """Run one route: ``steps`` is a list of ("click"|"dbl"|"move", dx, dy) / ("key", action) / ("wait", s) /
        ("status",). A double click commits the route; Escape (after the steps) aborts what is still unfinished.
        Returns the tracks and vias left on the board."""
        env = self.env
        env.idle()
        before = self.snapshot()
        # every route gets its own place (``shift`` mm to the right): a route laid over an earlier one's tracks
        # would be merged into them
        steps = [(st[0], st[1] + shift, st[2]) if st[0] in ("click", "dbl", "move") else st for st in steps]
        # The route key starts routing where the pointer is (a hotkey primes the router with its position), so
        # the first point is given by the key, not a click.
        _, x0, y0 = steps[0]
        self.at(x0, y0, press=False)
        env.key(tool, 1.0)
        windows_at_start = env.windows()
        dump = os.environ.get("KICAD_AI_DUMP")
        self.count = getattr(self, "count", 0) + 1
        for index, step in enumerate(steps[1:]):
            if dump:
                _dump(Path(dump), **{f"r{self.count}-{index}": env.shot()})
            if step[0] == "click":
                self.at(step[1], step[2])
            elif step[0] == "dbl":  # a double click ends the route there and commits it
                windows = env.windows()
                self.at(step[1], step[2], double=True)
                # its second click can reach the selection tool on the new track: its properties window opens
                self.opened = env.close_new(windows)
            elif step[0] == "status":  # record the status-bar texts (the active tool's name is one)
                self.status = _status_texts(env)
            elif step[0] == "popup":  # the layer list the last key opened: click the row of layer ``step[1]``
                popup = {}
                for _ in range(10):
                    popup = {h: t for h, t in env.windows().items() if h not in windows_at_start}
                    if popup:
                        break
                    time.sleep(0.3)
                self.popups = [*getattr(self, "popups", []), *popup.values()]
                for hwnd in popup:
                    self.picked = _click_layer_row(env, hwnd, step[1])
                time.sleep(1.0)
            elif step[0] == "move":
                self.at(step[1], step[2], press=False)
            elif step[0] == "key":
                env.key(step[1], 1.0)
            elif step[0] == "wait":
                time.sleep(step[1])
        env.escape(3)
        env.idle()
        return self.new_items(before)


COPPER = ["F.Cu", *(f"In{i}.Cu" for i in range(1, 7)), "B.Cu"]


def _click_layer_row(env: Env, dialog: int, layer: str) -> str:
    """Click a layer's row in the Select Layer list (its left grid holds the copper layers in board order, the
    current layer left out when the via cannot start and end on it). The row height is the grid's height over its
    row count; the readback (the layer the route continues on) is what tells whether the right row was hit."""
    grids = [c for c in _descendants(dialog) if hotkeys.user32.GetParent(ctypes.c_void_p(c)) == dialog]
    rects = []
    for grid in grids:
        rect = hotkeys._Rect()
        hotkeys.user32.GetWindowRect(ctypes.c_void_p(grid), ctypes.byref(rect))
        rects.append((rect.left, grid, rect))
    _, grid, rect = min((r for r in rects if r[2].bottom - r[2].top > 100), key=lambda r: r[0])
    cells = [c for c in _descendants(grid) if hotkeys.user32.GetParent(ctypes.c_void_p(c)) == grid]
    area = max(cells, key=lambda c: _area(c))
    crect = hotkeys._Rect()
    hotkeys.user32.GetClientRect(ctypes.c_void_p(area), ctypes.byref(crect))
    rows = round(crect.bottom / 45.4) or 1
    layers = COPPER if rows >= len(COPPER) else COPPER[1:]
    row = layers.index(layer)
    height = crect.bottom / len(layers)
    x, y = 40, int(row * height + height / 2)
    env.cursor.place(area, x, y)
    point = (y << 16) | x
    hotkeys.user32.PostMessageW(area, 0x0200, 0, point)
    time.sleep(0.2)
    env.cursor.button(True)
    hotkeys.user32.PostMessageW(area, WM_LBUTTONDOWN, 1, point)
    time.sleep(0.15)
    env.cursor.button(False)
    hotkeys.user32.PostMessageW(area, WM_LBUTTONUP, 0, point)
    return f"row {row} of {len(layers)} ({layer})"


def _area(hwnd: int) -> int:
    rect = hotkeys._Rect()
    hotkeys.user32.GetClientRect(ctypes.c_void_p(hwnd), ctypes.byref(rect))
    return rect.right * rect.bottom


def _descendants(hwnd: int) -> list[int]:
    found: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(lambda h, _: found.append(h) or True)
    hotkeys.user32.EnumChildWindows(ctypes.c_void_p(hwnd), proc, None)
    return found


def _summary(items: list[dict]) -> str:
    tracks = [i for i in items if "start" in i]
    vias = [i for i in items if "position" in i]
    via_text = ", ".join(f"{v.get('type', '?')}@{_via_layers(v)}" for v in vias)
    return f"{len(tracks)} track segments, {len(vias)} vias" + (f" ({via_text})" if vias else "")


def _via_layers(via: dict) -> str:
    stack = via.get("pad_stack", {})
    layers = stack.get("layers") or []
    if layers:
        return f"{layers[0]}..{layers[-1]}"
    drill = stack.get("drill", {})
    return f"{drill.get('start_layer', '?')}..{drill.get('end_layer', '?')}"


def _segments(items: list[dict]) -> set[tuple]:
    return {(int(i["start"]["x_nm"]) // 10000, int(i["start"]["y_nm"]) // 10000, int(i["end"]["x_nm"]) // 10000,
             int(i["end"]["y_nm"]) // 10000) for i in items if "start" in i}


def _vias(items: list[dict]) -> list[dict]:
    return [i for i in items if "position" in i and "start" not in i]


def _touches(items: list[dict], origin: tuple[int, int], point_mm: tuple[float, float], tol_mm: float = 0.4) -> bool:
    x, y = origin[0] + point_mm[0] * MM, origin[1] + point_mm[1] * MM
    ends = [e for i in items if "start" in i for e in (i["start"], i["end"])]
    return any(abs(int(e["x_nm"]) - x) <= tol_mm * MM and abs(int(e["y_nm"]) - y) <= tol_mm * MM for e in ends)


def free_space(r: Router) -> list[dict]:
    """Routes that start in empty space (no net), right of the board, where nothing is in the way."""
    rows = []
    o = r.origin
    # Undo Last Segment: fixed at (3,0), (3,3), (6,3); the route is then ended by a double click at (3,3). Without
    # the undo the route runs back from (6,3); with it, the (3,3)-(6,3) segment is gone.
    base = [("click", 0, 0), ("click", 3, 0), ("click", 3, 3), ("click", 6, 3), ("move", 6, 4)]
    control = r.route([*base, ("dbl", 4, 6)])
    undone = r.route([*base, ("key", R + "UndoLastSegment"), ("move", 4, 5), ("dbl", 4, 6)], shift=10)
    ok = _touches(control, o, (6, 3)) and bool(undone) and not _touches(undone, o, (16, 3)) and _touches(undone, o, (13, 3)) and _touches(undone, o, (14, 6))
    rows.append(_row(R + "UndoLastSegment", "ok" if ok else "no_effect",
                     f"route fixed at (3,0),(3,3),(6,3) mm, ended by a double click at (4,6): {_summary(control)}, reaching (6,3) "
                     f"{_touches(control, o, (6, 3))}; with Undo Last Segment pressed before the double click: {_summary(undone)}, "
                     f"reaching (6,3) {_touches(undone, o, (16, 3))} (this route laid 10 mm to the right)"))
    # Switch Posture: a diagonal target ended by a double click, with and without the posture switched
    control = r.route([("click", 0, 8), ("move", 4, 10), ("wait", 0.3), ("dbl", 4, 10)])
    switched = r.route([("click", 0, 8), ("move", 4, 10), ("key", R + "SwitchPosture"), ("wait", 0.5), ("dbl", 4, 10)],
                       shift=10)
    moved = {(a - 1000, b, c - 1000, d) for a, b, c, d in _segments(switched)}  # back by the 10 mm shift
    ok = bool(_segments(control)) and bool(moved) and _segments(control) != moved         and _touches(control, o, (4, 10)) and _touches(switched, o, (14, 10))
    rows.append(_row(R + "SwitchPosture", "ok" if ok else "no_effect",
                     f"route from (0,8) to (4,10) mm: segments {sorted(_segments(control))}; with Switch Posture pressed: "
                     f"{sorted(moved)} (laid 10 mm to the right, shown shifted back: same ends, the corner on the other side)"))
    # Cancel Current Item: the route is dropped and the router stays active (status bar), so the double click that
    # would have committed it only starts and ends an empty route
    control = r.route([("click", 0, 14), ("click", 3, 14), ("move", 3, 16), ("dbl", 5, 16)])
    cancelled = r.route([("click", 0, 14), ("click", 3, 14), ("move", 3, 16), ("key", R + "CancelCurrentItem"),
                         ("status",), ("dbl", 5, 16)], shift=10)
    active = "Route Single Track" in r.status
    ok = bool(control) and not cancelled and active
    rows.append(_row(R + "CancelCurrentItem", "ok" if ok else "no_effect",
                     f"route fixed at (0,14),(3,14), ended by a double click at (5,16): {_summary(control)}; with Cancel Current "
                     f"Item pressed before the double click: {_summary(cancelled)}, the router still active (status bar names "
                     f"'Route Single Track': {active})"))
    # Vias: the through via (PlaceVia) is the control for blind, micro and the via stack; the layer pair preset
    # is F.Cu/In1.Cu and the stack preset F.Cu -> In2.Cu
    want = {"PlaceVia": ["VT_THROUGH"], "PlaceBlindVia": ["VT_BLIND"], "PlaceMicroVia": ["VT_MICRO"],
            "PlaceViaStack": ["VT_MICRO", "VT_MICRO"]}
    # control: the same route with no key leaves tracks only, all on F.Cu, and no via
    baseline = r.route([("click", 0, 16), ("move", 3, 16), ("wait", 0.5), ("click", 3, 16), ("move", 5, 17), ("dbl", 5, 17)])
    base_ok = bool(baseline) and not _vias(baseline) and {t.get("layer") for t in baseline if "start" in t} == {"BL_F_Cu"}
    r.baseline = f"the same route without a key: {_summary(baseline)}, layers {sorted({t.get('layer', '') for t in baseline if 'start' in t})}"
    for name, row_y in (("PlaceVia", 20), ("PlaceBlindVia", 24), ("PlaceMicroVia", 28), ("PlaceViaStack", 32)):
        got = r.route([("click", 0, row_y), ("move", 3, row_y), ("key", R + name), ("wait", 0.5), ("click", 3, row_y),
                       ("move", 5, row_y + 1), ("dbl", 5, row_y + 1)])
        types = sorted(v.get("type", "") for v in _vias(got))
        ok = types == want[name] and base_ok
        if name == "PlaceViaStack":  # two stacked microvias at one point, F.Cu-In1.Cu and In1.Cu-In2.Cu
            spans = sorted(_via_layers(v) for v in _vias(got))
            ok = ok and spans == ["BL_F_Cu..BL_In1_Cu", "BL_In1_Cu..BL_In2_Cu"]
        rows.append(_row(R + name, "ok" if ok else "no_effect",
                         f"route on F.Cu with the key pressed mid-route, then fixed and ended: {_summary(got)} (expected via types "
                         f"{want[name]}); control: {r.baseline}"))
    return rows


def _delete(r: Router, items: list[dict]) -> None:
    ids = [{"value": i["id"]["value"]} for i in items]
    for start in range(0, len(ids), 200):
        api.call(r.env.s, "DeleteItems", {"item_ids": ids[start:start + 200]})
    time.sleep(0.5)


def _pad_pair(env: Env) -> tuple[dict, dict]:
    """Two front SMD pads of a two-pad net, 1.5 to 12 mm apart."""
    by_net: dict[str, list[dict]] = {}
    for pad in _items(env.s, "KOT_PCB_PAD"):
        net = pad.get("net", {}).get("name", "")
        if net:
            by_net.setdefault(net, []).append(pad)
    for _name, pads in sorted(by_net.items()):
        if len(pads) != 2 or any(p.get("type") != "PT_SMD" or p["pad_stack"]["layers"][0] != "BL_F_Cu" for p in pads):
            continue
        sizes = [p["pad_stack"]["copper_layers"][0]["size"] for p in pads]
        if any(min(int(z["x_nm"]), int(z["y_nm"])) < 400000 for z in sizes):  # big enough to hit at routing zoom
            continue
        a, b = pads
        dx = int(b["position"]["x_nm"]) - int(a["position"]["x_nm"])
        dy = int(b["position"]["y_nm"]) - int(a["position"]["y_nm"])
        if 1.5 * MM <= (dx * dx + dy * dy) ** 0.5 <= 12 * MM:
            return a, b
    raise RuntimeError("no two-pad front SMD net 1.5-12 mm apart")


def other_end(r: Router) -> list[dict]:
    """Attempt Finish and Continue From End need a route with an unrouted other end: a two-pad net is unrouted and
    routed from its first pad."""
    from kicad_mcp.coverage.live_board_misc import _unroute_net

    rows = []
    a, b = _pad_pair(r.env)
    net = a["net"]["name"]
    _unroute_net(r.env, net)
    bx = (int(b["position"]["x_nm"]) - int(a["position"]["x_nm"])) / MM
    by = (int(b["position"]["y_nm"]) - int(a["position"]["y_nm"])) / MM
    r.view((int(a["position"]["x_nm"]), int(a["position"]["y_nm"])),
           ((min(0, bx) - 4, min(0, by) - 4), (max(0, bx) + 4, max(0, by) + 4)))
    # a route here must end without DRC violations (Attempt Finish and the finishing double click refuse to
    # commit one), so the board's other copper is cleared and the router walks around what is left (pads)
    _delete(r, [i for kind in ("KOT_PCB_TRACE", "KOT_PCB_ARC", "KOT_PCB_VIA", "KOT_PCB_ZONE") for i in _items(r.env.s, kind)])
    r.env.key(R + "WalkaroundMode", 1.0)
    o, half = r.origin, (bx / 3, by / 3)
    # Attempt Finish: the route is completed to the other pad and committed; without it Escape drops the route
    control = r.route([("click", 0, 0), ("move", *half), ("wait", 0.5)])
    finished = r.route([("click", 0, 0), ("move", *half), ("key", R + "AttemptFinish"), ("wait", 1.0)])
    ok = not control and _touches(finished, o, (0, 0), 0.6) and _touches(finished, o, (bx, by), 0.6)
    rows.append(_row(R + "AttemptFinish", "ok" if ok else "no_effect",
                     f"net {net} unrouted, a route started on its pad at {a['position']} and moved a third of the way to the other "
                     f"pad: ended with Escape it leaves {_summary(control)}; with Attempt Finish pressed: {_summary(finished)}, "
                     f"from pad to pad {ok}"))
    _delete(r, finished)
    # Continue From End: routing moves to the other pad, so the finishing double click near it makes a track from it
    q = (bx + 1.5, by + 1.5)
    control = r.route([("click", 0, 0), ("move", *half), ("wait", 0.3), ("dbl", *q)])
    from_a = _touches(control, o, (0, 0), 0.6)
    _delete(r, control)
    switched = r.route([("click", 0, 0), ("move", *half), ("key", R + "ContinueFromEnd"), ("wait", 0.8), ("move", *q),
                        ("dbl", *q)])
    ok = from_a and bool(switched) and _touches(switched, o, (bx, by), 0.6) and not _touches(switched, o, (0, 0), 0.6)
    rows.append(_row(R + "ContinueFromEnd", "ok" if ok else "no_effect",
                     f"route started on pad A, ended by a double click at {q} mm: {_summary(control)} starting at pad A {from_a}; "
                     f"with Continue From End pressed first: {_summary(switched)}, starting at the other pad "
                     f"{_touches(switched, o, (bx, by), 0.6)} and touching pad A {_touches(switched, o, (0, 0), 0.6)}"))
    _delete(r, switched)
    return rows


def pick_layer(r: Router) -> list[dict]:
    """Select Layer and Place Via: the key opens the layer list mid-route; In2.Cu is picked there with its
    layer key. The via then ends on In2.Cu (the plain via keys go to In1.Cu, the layer pair's other layer)."""
    rows = []
    # control: the same route with no key, laid in the same column, stays on F.Cu with no via
    baseline = r.route([("click", 0, 15), ("move", 3, 15), ("wait", 0.5), ("click", 3, 15), ("move", 5, 16), ("dbl", 5, 16)], shift=40)
    base_layers = sorted({t.get("layer", "") for t in baseline if "start" in t})
    base_ok = bool(baseline) and not _vias(baseline) and base_layers == ["BL_F_Cu"]
    for name, row_y, kind in (("SelLayerAndPlaceVia", 0, "VT_THROUGH"), ("SelLayerAndPlaceBlindVia", 5, "VT_BLIND"),
                              ("SelLayerAndPlaceMicroVia", 10, "VT_MICRO")):
        r.popups = []
        got = r.route([("click", 0, row_y), ("move", 3, row_y), ("key", R + name), ("popup", "In2.Cu"), ("wait", 0.5),
                       ("click", 3, row_y), ("move", 5, row_y + 1), ("dbl", 5, row_y + 1)], shift=40)
        vias = _vias(got)
        after = sorted({t.get("layer", "") for t in got if "start" in t})
        ok = [v.get("type") for v in vias] == [kind] and "BL_In2_Cu" in after and base_ok
        rows.append(_row(R + name, "ok" if ok else "no_effect",
                         f"key pressed mid-route on F.Cu opened {r.popups or 'no window'}; In2.Cu picked there ({getattr(r, 'picked', '')}); "
                         f"the route left {_summary(got)}, track layers {after}; control (no key): {_summary(baseline)}, layers {base_layers}"))
    return rows


def _width(track: dict) -> int:
    width = track.get("width", {})
    return int(width.get("value_nm", 0)) if isinstance(width, dict) else int(width or 0)


def sizes(r: Router) -> list[dict]:
    """Track Width Increase mid-route sets the next predefined width (0.4 mm) and posts trackViaSizeChanged; the
    router's handler for that notification applies it to the route, so the committed head has the new width."""
    steps = [("click", 0, 20), ("click", 3, 20), ("move", 5, 21)]
    control = r.route([*steps, ("wait", 0.5), ("dbl", 5, 21)], shift=30)
    widened = r.route([*steps, ("key", "pcbnew.EditorControl.trackWidthInc"), ("wait", 0.8), ("move", 5, 21.5),
                       ("dbl", 5, 21)], shift=40)
    before = sorted({_width(t) for t in control if "start" in t})
    after = sorted({_width(t) for t in widened if "start" in t})
    ok = bool(before) and 400_000 not in before and 400_000 in after
    return [_row("pcbnew.EditorControl.trackViaSizeChanged", "ok" if ok else "no_effect",
                 f"route fixed at (3,20) and ended at (5,21) mm: track widths {before} nm; with Track Width Increase pressed after "
                 f"the fixed point (the next predefined width, 0.4 mm, then the notification the router applies): {after} nm")]


def arcs(r: Router) -> list[dict]:
    """Switch Arc Posture while drawing an arc (centre, then start, then end): the arc is drawn the other way round
    the centre, so its midpoint is on the opposite side. Same clicks without the key are the control."""
    steps = [("click", 0, 26), ("click", 3, 26), ("move", 0, 29), ("wait", 0.4)]
    control = r.route([*steps, ("click", 0, 29)], shift=30, tool=ARC)
    flipped = r.route([*steps, ("key", "common.InteractiveDrawing.arcPosture"), ("wait", 0.5), ("click", 0, 29)],
                      shift=40, tool=ARC)

    def mid(items: list[dict], shift: float):
        arc = next((i["shape"]["arc"] for i in items if isinstance(i.get("shape", {}).get("arc"), dict)), None)
        if not arc:
            return None
        return (round((int(arc["mid"]["x_nm"]) - r.origin[0]) / MM - shift, 2), round((int(arc["mid"]["y_nm"]) - r.origin[1]) / MM, 2))

    a, b = mid(control, 30), mid(flipped, 40)
    ok = a is not None and b is not None and abs(a[0] - b[0]) + abs(a[1] - b[1]) > 3
    return [_row("common.InteractiveDrawing.arcPosture", "ok" if ok else "no_effect",
                 f"arc centred at (0,26) mm from (3,26) to (0,29): midpoint {a} (relative mm); with Switch Arc Posture pressed before "
                 f"the last click: midpoint {b} (the other way round the centre)")]


def sweep(inventory: dict, project: Path, only: list[str] | None = None) -> dict:
    all_actions = [a["name"] for a in inventory["actions"]]
    keys = [ROUTE, CYCLE, IN2, R + "WalkaroundMode", "pcbnew.EditorControl.trackWidthInc", ARC,
            "common.InteractiveDrawing.arcPosture", *(R + n for n in ("UndoLastSegment", "SwitchPosture", "CancelCurrentItem", "AttemptFinish",
                                             "ContinueFromEnd", "PlaceVia", "PlaceBlindVia", "PlaceMicroVia",
                                             "PlaceViaStack", "SelLayerAndPlaceVia", "SelLayerAndPlaceBlindVia",
                                             "SelLayerAndPlaceMicroVia"))]
    env = start_session(project, keys, all_actions, prepare, {"tools.pns.mode": 0})
    rows: list[dict] = []
    try:
        r = Router(env)
        env.key(CYCLE, 1.0)  # the F.Cu/In1.Cu layer pair preset
        footprints = _items(env.s, "KOT_PCB_FOOTPRINT")
        right = max(int(f["position"]["x_nm"]) for f in footprints) + 15 * MM
        top = min(int(f["position"]["y_nm"]) for f in footprints)
        r.view((right, top))
        parts = [p for p in (free_space, pick_layer, sizes, arcs, other_end) if p.__name__ in os.environ.get("KICAD_AI_PARTS", p.__name__)]
        for part in parts:
            try:
                rows += part(r)
            except Exception as exc:  # noqa: BLE001
                rows.append(_row(R + part.__name__, "error", f"{type(exc).__name__}: {exc}"))
    finally:
        env.close()
        with contextlib.suppress(Exception):
            env.s.close(discard=True)
    rows = [row for row in rows if not only or row["action"] in only]
    passed = sorted({f"action:{row['action']}" for row in rows if row["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "router-live", "passed": passed, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project,
                   [n for n in args.only.split(",") if n] or None)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:300])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
