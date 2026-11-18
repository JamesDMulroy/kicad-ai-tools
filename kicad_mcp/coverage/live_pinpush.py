"""Live proof for the symbol editor's "push pin" actions (apply one pin's length / name size / number size
to every pin of the symbol), read back through the API.

The fixture is KiCad's ECC83 demo library with one pin made different (longer, larger texts). That pin is
selected in the symbol editor, the action's key is pressed, and the symbol's pins are read back: every other
pin must now carry the selected pin's value.

    python -m kicad_mcp.coverage.live_pinpush --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time

from kicad_mcp.coverage.cursor_hook import CursorHook
from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.coverage.live_tools import click
from kicad_mcp.ipc import api, hotkeys

WINDOW = {"name": "Symbol Editor", "match": "contains"}

REMOVE_CORNER = "eeschema.PointEditor.removeCorner"
STACK = "eeschema.InteractiveEdit.convertStackedPins"
CASES = {STACK: "stack", REMOVE_CORNER: "corner", "eeschema.PinEditing.pushPinLength": "length", "eeschema.PinEditing.pushPinNameSize": "name",
         "eeschema.PinEditing.pushPinNumSize": "number"}


def _document(session) -> dict:
    docs = api.call(session, "GetOpenDocuments", {"type": "DOCTYPE_SYMBOL"})["response"].get("documents", [])
    return docs[0]


def _pins(session) -> list[dict]:
    items = api.call(session, "GetItems", {"header": {"document": _document(session)}, "types": []}
                     )["response"].get("items", [])
    return [i for i in items if i.get("@type", "").endswith("Pin")]


def _square(session) -> dict | None:
    """The fixture's four-corner polyline."""
    items = api.call(session, "GetItems", {"header": {"document": _document(session)}, "types": []}
                     )["response"].get("items", [])
    for item in items:
        for poly in item.get("shape", {}).get("polygon", {}).get("polygons", []):
            if len(poly.get("outline", {}).get("nodes", [])) >= 4:
                return item
    return None


def _square_any(session, ident: str) -> dict:
    return api.call(session, "GetItemsById", {"header": {"document": _document(session)},
                                              "items": [{"value": ident}]})["response"]["items"][0]


def _nodes(item: dict) -> list[tuple[float, float]]:
    nodes = item["shape"]["polygon"]["polygons"][0]["outline"]["nodes"]
    return [(int(n["point"].get("x_nm", 0)) / 1e6, int(n["point"].get("y_nm", 0)) / 1e6) for n in nodes]


def _value(pin: dict, what: str):
    """The pin's length, name text size or number text size, as the API reports it."""
    if what == "length":
        return json.dumps(pin.get("length"), sort_keys=True)
    key = "name" if what == "name" else "number"
    for field in (f"{key}_text_size", f"{key}_size", f"{key}_text"):
        if field in pin:
            return json.dumps(pin[field], sort_keys=True)
    return json.dumps({k: v for k, v in pin.items() if key in k}, sort_keys=True)


def _cursor_mm(uia) -> tuple[float, float] | None:
    texts = uia.call("state", {"window": WINDOW, "selector": {"by": "path", "name": "*", "name_match": "any",
                                                             "control_type": "Text"}})["controls"]
    for t in texts:
        parts = t["name"].split()
        if len(parts) >= 4 and parts[0] == "X" and parts[2] == "Y":
            return float(parts[1]), float(parts[3])
    return None


def _point(canvas: int, hook: CursorHook, x: int, y: int) -> None:
    for step in range(6, 0, -1):
        hotkeys.user32.PostMessageW(canvas, 0x0200, 0, ((y - 5 * step) << 16) | (x - 5 * step))
        time.sleep(0.05)
    hook.place(canvas, x, y)
    hotkeys.user32.PostMessageW(canvas, 0x0200, 0, (y << 16) | x)
    time.sleep(0.5)


def _units_per_mm(uia) -> float:
    texts = {t["name"].strip() for t in uia.call("state", {"window": WINDOW, "selector": {
        "by": "path", "name": "*", "name_match": "any", "control_type": "Text"}})["controls"]}
    if "mils" in texts:
        return 1000 / 25.4
    if "in" in texts:
        return 1 / 25.4
    return 1.0


def calibrate(uia, canvas: int, hook: CursorHook):
    """Pixel -> drawing position, from the status bar's cursor readout at two points (in the units it shows);
    returns the inverse, taking millimetres -> client pixel."""
    per_mm = _units_per_mm(uia)
    _point(canvas, hook, 200, 200)
    a = _cursor_mm(uia)
    _point(canvas, hook, 600, 500)
    b = _cursor_mm(uia)
    if not a or not b or a[0] == b[0] or a[1] == b[1]:
        raise RuntimeError(f"status bar cursor readout not usable: {a} {b}")
    sx, sy = (b[0] - a[0]) / 400, (b[1] - a[1]) / 300
    return lambda mx, my: (int(round(200 + (mx * per_mm - a[0]) / sx)), int(round(200 + (my * per_mm - a[1]) / sy)))


@cleanup_after_call
def sweep(inventory: dict, project: Path) -> dict:
    names = {a["name"] for a in inventory["actions"]}
    cases = [c for c in CASES if c in names]
    fixture = Path(scoped_mkdtemp(prefix="kicad-ai-tools-pins-")) / project.name
    shutil.copytree(project, fixture)
    pro = next(fixture.glob("*.kicad_pro"))
    session, assigned, _frame, canvas = _start("symedit", fixture / (pro.stem + ".kicad_sch"),
                                               [a["name"] for a in inventory["actions"]], cases)
    rows = []
    try:
        api.call(session, "OpenLibraryItem", {"type": "DOCTYPE_SYMBOL",
                                              "identifier": {"library_nickname": pro.stem, "entry_name": "ECC83"}})
        time.sleep(3)
        _frame, canvas = hotkeys.canvas_of(session, "Symbol Editor")
        hook = CursorHook(session.pid)
        try:
            from kicad_mcp.coverage.text_hook import TextHook

            text = TextHook(session.pid)
        except Exception:  # noqa: BLE001
            text = None
        to_pixel = calibrate(session.attach_uia(), canvas, hook)
        pins = _pins(session)
        sample = pins[0] if pins else {}
        source = next((p for p in pins if '"G"' in json.dumps(p) and "3810000" in json.dumps(p.get("length"))), None)
        for name in cases:
            what = CASES[name]
            row = {"action": name, "outcome": "no_effect", "detail": ""}
            try:
                if name == STACK:
                    if source is None:
                        raise RuntimeError("fixture pin not found")
                    stacked = [p for p in pins if json.dumps(p.get("position")) == json.dumps(source.get("position"))]
                    if len(stacked) < 2:
                        row.update(outcome="skipped", detail="the fixture has no stacked pins (run on ecc83-stack)")
                    else:
                        hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                        end = source.get("position", {})
                        px, py = to_pixel(int(end.get("x_nm", 0)) / 1e6 + 1.9, int(end.get("y_nm", 0)) / 1e6)
                        _point(canvas, hook, px, py)
                        windows = set(hotkeys._visible(session))
                        click(canvas, px, py)
                        time.sleep(0.8)
                        if [h for h in hotkeys._visible(session) if h not in windows]:  # "which one?": the first
                            hotkeys.press(canvas, 0x0D)
                            time.sleep(0.6)
                        before_n = len(_pins(session))
                        hotkeys.press(canvas, assigned[name][1])
                        time.sleep(1.2)
                        after_pins = _pins(session)
                        numbers = sorted(json.dumps(p.get("number", p.get("number_text", ""))) for p in after_pins)
                        row["pins"] = [before_n, len(after_pins)]
                        if len(after_pins) < before_n:
                            row.update(outcome="ok", detail=f"the pins stacked at the selected pin's place were merged into one: pins "
                                                            f"{before_n} -> {len(after_pins)}")
                        row["numbers"] = numbers[:12]
                elif name == REMOVE_CORNER:
                    square = _square(session)
                    if square is None:
                        raise RuntimeError("fixture polyline not found")
                    corners = _nodes(square)
                    (ax, ay), (bx, by) = corners[0], corners[1]
                    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                    mx, my = to_pixel((ax + bx) / 2, (ay + by) / 2)  # the middle of an edge selects it
                    _point(canvas, hook, mx, my)
                    click(canvas, mx, my)
                    time.sleep(0.8)
                    row["selected_panel"] = sorted(text.painted(_frame))[:30] if text else None
                    row["status_at_edge"] = _cursor_mm(session.attach_uia())
                    cx, cy = to_pixel(bx, by)  # then the mouse rests on a corner
                    _point(canvas, hook, cx, cy)
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(1.2)
                    after = _square(session)
                    row["corners"] = [corners, _nodes(after) if after else None]
                    left = len(_nodes(_square_any(session, square["id"]["value"])))
                    if left < len(corners):
                        row.update(outcome="ok", detail=f"polyline corners {len(corners)} -> {left} (corner at "
                                                        f"{bx:g},{by:g} mm under the cursor)")
                elif source is None:
                    row.update(outcome="error", detail=f"fixture pin not found; a pin reads {json.dumps(sample)[:300]}")
                else:
                    # the symbol editor has no selection API: click the pin's body, as a person selects it
                    hotkeys.press(canvas, hotkeys.VK_ESCAPE)
                    end = source.get("position", {})
                    px, py = to_pixel(int(end.get("x_nm", 0)) / 1e6 + 1.9, int(end.get("y_nm", 0)) / 1e6)
                    _point(canvas, hook, px, py)
                    click(canvas, px, py)
                    time.sleep(0.6)
                    row["click"] = {"pin_mm": [int(end.get("x_nm", 0)) / 1e6, int(end.get("y_nm", 0)) / 1e6],
                                    "pixel": [px, py], "status_after": _cursor_mm(session.attach_uia()),
                                    "panel": sorted(text.painted(_frame))[:25] if text else None}
                    want = _value(source, what)
                    before = sum(_value(p, what) == want for p in _pins(session))
                    hotkeys.press(canvas, assigned[name][1])
                    time.sleep(1.2)
                    after_pins = _pins(session)
                    after = sum(_value(p, what) == want for p in after_pins)
                    row["matching"] = [before, after, len(after_pins)]
                    if after > before:
                        row.update(outcome="ok", detail=f"pins with the selected pin's {what}: {before} -> {after} "
                                                        f"of {len(after_pins)}")
            except Exception as exc:  # noqa: BLE001
                row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}")
            rows.append(row)
            print(row["outcome"], name, row["detail"][:200], flush=True)
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "pinpush-api", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
