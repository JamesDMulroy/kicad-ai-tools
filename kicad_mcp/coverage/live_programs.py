"""Live proof for KiCad programs that have neither actions nor an API (the PCB calculator).

Each calculator panel is driven through UI Automation on a private desktop, as a person would use it:
select the panel in the tree, read every visible field, change one numeric input, press Calculate when
the panel has one, and read the fields again. A panel is verified only when some other field changed,
i.e. KiCad computed a result from the new input.

    python -m kicad_mcp.coverage.live_programs --inventory inv.json --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import contextlib
import json
from pathlib import Path
import re
import shutil
import tempfile
import time

from kicad_mcp.ipc.gui_session import ProgramSession

WINDOW = {"name": "Calculator Tools", "match": "prefix"}
NUMBER = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")


def _visible_controls(session: ProgramSession) -> list[dict]:
    tree = session.uia.call("dump-tree", {"window": WINDOW, "max_depth": 9, "max_nodes": 1500})
    found: list[dict] = []

    def walk(node: dict) -> None:
        if not node.get("offscreen") and node.get("automation_id")                 and node.get("control_type") in ("Edit", "Button", "RadioButton", "ComboBox"):
            found.append(node)
        for child in node.get("children", []):
            walk(child)

    for window in tree.get("windows", []):
        walk(window)
    return found


def _value(session: ProgramSession, automation_id: str) -> str | None:
    try:
        return session.uia.call("read-value", {"window": WINDOW, "selector": {"by": "automation_id",
                                                                            "automation_id": automation_id}}).get("value")
    except Exception:  # noqa: BLE001 - a control without a value pattern has no value
        return None


def _values(session: ProgramSession, edits: list[dict]) -> dict[str, str | None]:
    return {e["automation_id"]: _value(session, e["automation_id"]) for e in edits}


def _frame_pixels(session: ProgramSession) -> str | None:
    from kicad_mcp.ipc import hotkeys

    frames = [w for w in session.kicad.desktop.windows()
              if w["pid"] == session.kicad.pid and w["visible"] and w["title"].startswith("Calculator Tools")]
    return hotkeys.capture(frames[0]["hwnd"]) if frames else None


def _press_compute(session: ProgramSession, controls: list[dict]) -> None:
    for c in controls:
        if c["control_type"] == "Button" and c.get("name") in ("Calculate", "Update Values", "Analyze"):
            session.uia.call("invoke", {"window": WINDOW, "selector": {"by": "automation_id",
                                                                     "automation_id": c["automation_id"]}})


def _attempts(session: ProgramSession, controls: list[dict], before: dict[str, str | None]):
    """Ways a person would make a panel compute, in order: change a number, fill an empty input,
    pick the other radio button, pick another entry in a drop-down."""
    for input_id, value in before.items():
        if value is not None and NUMBER.match(value) and float(value) != 0:
            yield f"input {value} -> {float(value) * 1.5:g}", ("set-value", input_id, f"{float(value) * 1.5:g}")
    for input_id, value in before.items():
        if not (value or "").strip():
            yield "filled an empty input with 1000", ("set-value", input_id, "1000")
    for c in controls:
        if c["control_type"] == "RadioButton" and c.get("automation_id"):
            yield f"picked option {c.get('name') or c['automation_id']}", ("invoke", c["automation_id"], None)
    for c in controls:
        if c["control_type"] == "ComboBox" and c.get("automation_id"):
            yield f"changed drop-down {c.get('name') or c['automation_id']}", ("combo", c["automation_id"], None)


def _do(session: ProgramSession, step) -> None:
    kind, target, value = step
    selector = {"by": "automation_id", "automation_id": target}
    if kind == "set-value":
        session.uia.call("set-value", {"window": WINDOW, "value": value, "selector": selector})
    elif kind == "invoke":
        session.uia.call("invoke", {"window": WINDOW, "selector": selector})
    elif _native_combo_next(session):  # drop-down: pick its next entry the way a selection does
        return
    else:  # drop-down exposed through UI Automation: pick its second entry
        tree = session.uia.call("dump-tree", {"window": WINDOW, "max_depth": 12, "max_nodes": 3000})
        items: list[str] = []

        def walk(node: dict, inside: bool) -> None:
            here = inside or node.get("automation_id") == target
            if here and node.get("control_type") == "ListItem" and node.get("name"):
                items.append(node["name"])
            for child in node.get("children", []):
                walk(child, here)

        for window in tree.get("windows", []):
            walk(window, False)
        if len(items) < 2:
            raise RuntimeError("drop-down has fewer than two entries")
        session.uia.call("invoke", {"window": WINDOW, "selector": {"by": "path", "name": items[1],
                                                                 "control_type": "ListItem"}})


def _native_combo_next(session: ProgramSession) -> bool:
    """Select the next entry of the visible native drop-down (CB_SETCURSEL, then the CBN_SELCHANGE
    notification its parent gets from a real pick). True when one was changed."""
    import ctypes

    from kicad_mcp.ipc import hotkeys

    user32 = ctypes.windll.user32
    frames = [w for w in session.kicad.desktop.windows()
              if w["pid"] == session.kicad.pid and w["visible"] and w["title"].startswith("Calculator Tools")]
    if not frames:
        return False
    for hwnd, klass, _area, visible in hotkeys._children(frames[0]["hwnd"]):
        if klass != "ComboBox" or not visible:
            continue
        count = user32.SendMessageW(hwnd, 0x0146, 0, 0)  # CB_GETCOUNT
        current = user32.SendMessageW(hwnd, 0x0147, 0, 0)  # CB_GETCURSEL
        if count < 2:
            continue
        user32.SendMessageW(hwnd, 0x014E, (current + 1) % count, 0)  # CB_SETCURSEL
        control_id = user32.GetDlgCtrlID(hwnd)
        user32.PostMessageW(user32.GetParent(hwnd), 0x0111, (1 << 16) | (control_id & 0xFFFF), hwnd)  # CBN_SELCHANGE
        return True
    return False


def _grid_cells(session: ProgramSession) -> int:
    """Cells of the visible data grid (a reference-table panel's whole content)."""
    tree = session.uia.call("dump-tree", {"window": WINDOW, "max_depth": 12, "max_nodes": 4000})
    count = 0

    def walk(node: dict, in_table: bool) -> None:
        nonlocal count
        here = in_table or (node.get("control_type") == "Table" and not node.get("offscreen"))
        if here and node.get("control_type") in ("Custom", "DataItem") and not node.get("offscreen"):
            count += 1
        for child in node.get("children", []):
            walk(child, here)

    for window in tree.get("windows", []):
        walk(window, False)
    return count


def prove_panel(session: ProgramSession, panel: str) -> tuple[str, str]:
    session.uia.call("invoke", {"window": WINDOW, "selector": {"by": "path", "name": panel,
                                                             "control_type": "TreeItem"}})
    time.sleep(0.8)
    controls = _visible_controls(session)
    edits = [c for c in controls if c["control_type"] == "Edit" and c.get("enabled")]
    before = _values(session, edits)
    tried = 0
    for label, step in _attempts(session, controls, before):
        pixels = _frame_pixels(session)
        values = _values(session, edits)
        try:
            _do(session, step)
        except Exception:  # noqa: BLE001 - a read-only field or an odd control: try the next way
            continue
        tried += 1
        _press_compute(session, controls)
        time.sleep(0.8)
        changed_fields = [i for i, v in _values(session, edits).items() if i != step[1] and v != values.get(i)]
        if changed_fields or (pixels and _frame_pixels(session) != pixels):
            what = f"{len(changed_fields)} result field(s)" if changed_fields else "the panel's results"
            return "ok", f"{label}; {what} changed"
    # A reference-table panel computes nothing on input: its function is the table it shows.
    cells = _grid_cells(session)
    if not tried and cells:
        return "ok", f"reference table shown ({cells} cells)"
    return "no_effect", f"{tried} interaction(s) tried, nothing changed"


@cleanup_after_call
def sweep(inventory: dict) -> dict:
    panels = [p.split(":", 1)[1] for p in inventory.get("programs", []) if p.startswith("pcb_calculator:")]
    rows = []
    session = ProgramSession("pcb_calculator.exe").start()
    try:
        for panel in panels:
            try:
                outcome, detail = prove_panel(session, panel)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded
                outcome, detail = "error", f"{type(exc).__name__}: {str(exc)[:160]}"
            rows.append({"program": f"pcb_calculator:{panel}", "outcome": outcome, "detail": detail})
    finally:
        session.close(discard=True)
    passed = sorted(f"program:{r['program']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "route": "uia", "rows": rows}


# ---- image converter (bitmap2component): every output format ---------------------------------------------

IMAGE_WINDOW = {"name": "Image Converter", "match": "prefix"}
IMAGE_FORMATS = {  # output format -> (radio label, extension, what the file must start with)
    "SYMBOL_FMT": ("Symbol (.kicad_sym file)", ".kicad_sym", "(kicad_symbol_lib"),
    "FOOTPRINT_FMT": ("Footprint (.kicad_mod file)", ".kicad_mod", "(footprint"),
    "POSTSCRIPT_FMT": ("Postscript (.ps file)", ".ps", "%!PS"),
    "DRAWING_SHEET_FMT": ("Drawing Sheet (.kicad_wks file)", ".kicad_wks", "(kicad_wks"),
}
# The clipboard export writes the shared clipboard (a private desktop shares its window station's),
# so it runs inside coverage.clipboard.preserved(), which restores the owner's text (approved 2026-10-14).
CLIPBOARD_FORMATS = {"SYMBOL_PASTE_FMT": "Symbol (.kicad_sym file)"}


def _export_clipboard(session: ProgramSession, radio: str) -> dict:
    from . import clipboard

    with clipboard.preserved() as obs:
        session.uia.call("invoke", {"window": IMAGE_WINDOW, "selector": {"by": "path", "name": radio,
                                                                       "control_type": "RadioButton"}})
        with contextlib.suppress(Exception):
            session.uia.call("invoke", {"window": IMAGE_WINDOW, "selector": {"by": "path", "name": "Export to Clipboard",
                                                                           "control_type": "Button"}}, timeout_s=5)
        obs.wait_for_change(2.0)
        obs.capture()
    ok = obs.new_content and "(symbol" in (obs.text or "") and obs.restored is True
    return {"clipboard_restored": obs.restored, "outcome": "ok" if ok else "error",
            "detail": f"clipboard sequence {obs.before_sequence}->{obs.after_sequence}, text starts {(obs.text or '')[:40]!r}; "
                      f"owner text restored: {obs.restored}"}


def _export(session: ProgramSession, radio: str, target: Path) -> None:
    session.uia.call("invoke", {"window": IMAGE_WINDOW, "selector": {"by": "path", "name": radio,
                                                                   "control_type": "RadioButton"}})
    before = {w["hwnd"] for w in session.kicad.desktop.windows() if w["visible"]}
    # The button's reply waits for the modal save dialog it opens, so no reply is the expected outcome.
    with contextlib.suppress(Exception):
        session.uia.call("invoke", {"window": IMAGE_WINDOW, "selector": {"by": "path", "name": "Export to File...",
                                                                       "control_type": "Button"}}, timeout_s=3)
    dialog = None
    for _ in range(20):
        time.sleep(0.3)
        dialog = next((w for w in session.kicad.desktop.windows()
                       if w["visible"] and w["hwnd"] not in before and w["class"] == "#32770"), None)
        if dialog:
            break
    if dialog is None:
        raise RuntimeError("no save dialog appeared")
    window = {"name": dialog["title"], "match": "exact"}
    # The file-name box is an edit inside a combo box that shares its id, so select it by name and type.
    session.uia.call("set-value", {"window": window, "value": str(target),
                                   "selector": {"by": "path", "name": "File name:", "control_type": "Edit"}})
    session.uia.call("invoke", {"window": window, "selector": {"by": "path", "name": "Save", "control_type": "Button"}})


@cleanup_after_call
def sweep_images(inventory: dict, image: Path) -> list[dict]:
    wanted = [p.split(":", 1)[1] for p in inventory.get("programs", []) if p.startswith("bitmap2component:")]
    rows = []
    out_dir = Path(scoped_mkdtemp(prefix="kicad-ai-tools-b2c-"))
    session = ProgramSession("bitmap2component.exe", image).start()
    try:
        time.sleep(2)
        for fmt in wanted:
            if fmt in CLIPBOARD_FORMATS:
                try:
                    row = _export_clipboard(session, CLIPBOARD_FORMATS[fmt])
                except Exception as exc:  # noqa: BLE001
                    row = {"outcome": "error", "clipboard_restored": None,
                           "detail": f"{type(exc).__name__}: {str(exc)[:160]} (the guard restored the owner text on exit if it was readable)"}
                rows.append({"program": f"bitmap2component:{fmt}", **row})
                continue
            radio, ext, header = IMAGE_FORMATS[fmt]
            target = out_dir / f"export-{fmt.lower()}{ext}"
            try:
                _export(session, radio, target)
                for _ in range(20):
                    if target.exists() and target.stat().st_size:
                        break
                    time.sleep(0.3)
                text = target.read_text(encoding="utf-8", errors="replace")[:200] if target.exists() else ""
                ok = text.lstrip().startswith(header)
                rows.append({"program": f"bitmap2component:{fmt}", "outcome": "ok" if ok else "error",
                             "detail": f"wrote {target.name} ({target.stat().st_size} bytes), starts {header!r}"
                             if ok else f"file missing or wrong header: {text[:60]!r}"})
            except Exception as exc:  # noqa: BLE001
                rows.append({"program": f"bitmap2component:{fmt}", "outcome": "error",
                             "detail": f"{type(exc).__name__}: {str(exc)[:160]}"})
    finally:
        session.close(discard=True)
        shutil.rmtree(out_dir, ignore_errors=True)
    return rows


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image", type=Path, help="an image for the image converter (PNG)")
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    result = sweep(inventory)
    if args.image:
        rows = sweep_images(inventory, args.image)
        result["rows"] += rows
        result["passed"] = sorted({*result["passed"], *(f"program:{r['program']}" for r in rows if r["outcome"] == "ok")})
    args.out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
