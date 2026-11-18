"""Live proof for symbol-editor mode toggles through their own menu item, read back as the item's check mark.

KiCad's ECC83 demo symbol (two units) is opened in the symbol editor; each action's menu item is clicked
(WM_COMMAND with its id) and passes when its check mark flips.

    python -m kicad_mcp.coverage.live_symmenu --inventory inv.json --project <ecc83 demo dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import time

from kicad_mcp.coverage.live_hotkeys import _start
from kicad_mcp.ipc import api, hotkeys

CASES = ["eeschema.SymbolLibraryControl.toggleSyncedPinsMode"]


@cleanup_after_call
def sweep(inventory: dict, project: Path) -> dict:
    meta = {a["name"]: a for a in inventory["actions"]}
    cases = [c for c in CASES if c in meta]
    fixture = Path(scoped_mkdtemp(prefix="kicad-ai-tools-symmenu-")) / project.name
    shutil.copytree(project, fixture)
    pro = next(fixture.glob("*.kicad_pro"))
    session, _assigned, frame, _canvas = _start("symedit", fixture / (pro.stem + ".kicad_sch"),
                                                [a["name"] for a in inventory["actions"]], cases)
    rows = []
    try:
        api.call(session, "OpenLibraryItem", {"type": "DOCTYPE_SYMBOL",
                                              "identifier": {"library_nickname": pro.stem, "entry_name": "ECC83"}})
        time.sleep(3)
        frame, _canvas = hotkeys.canvas_of(session, "Symbol Editor")
        for name in cases:
            label = hotkeys.normalize(meta[name].get("friendly_name") or "")
            items = hotkeys.menu_items(frame)
            item = next((i for i in items if i["label"] == label), None)
            if item is None:  # the menu may word it differently: the one item naming the same thing
                words = [w for w in label.split() if len(w) > 4]
                close = [i for i in items if words and all(w[:6] in i["label"] for w in words[:1])]
                row_candidates = [i["label"] for i in close]
                item = close[0] if len(close) == 1 else None
            row = {"action": name, "outcome": "no_effect", "detail": "", "label": label}
            if item is None and name.endswith("toggleSyncedPinsMode"):
                # a toolbar-only toggle: it flips the editor's saved "sync pins" setting; the editor is closed
                # the normal way (it saves its settings into the session's own profile) and the file is read
                settings = session.profile / "config" / session._settings_version() / "symbol_editor.json"
                hotkeys.press(_canvas, _assigned[name][1])
                time.sleep(1.0)
                hotkeys.user32.PostMessageW(frame, hotkeys.WM_CLOSE, 0, 0)
                time.sleep(4)
                value = None
                if settings.exists():
                    text = json.loads(settings.read_text(encoding="utf-8"))
                    value = text.get("synchronized_pins_mode") if isinstance(text, dict) else None
                    if value is None:
                        flat = json.dumps(text)
                        row["settings_excerpt"] = [k for k in flat.split('"') if "sync" in k.lower()][:5]
                row["sync_setting_after"] = value
                if value is False:  # default true: one press turns it off
                    row.update(outcome="ok", detail="the symbol editor saved its pin-sync setting false after one "
                                                    "press (default true)")
            elif item is None:
                row["detail"] = f"no menu item '{label}' (similar: {locals().get('row_candidates')})"
            else:
                before = item.get("checked")
                hotkeys.user32.PostMessageW(frame, hotkeys.WM_COMMAND, item["id"], 0)
                time.sleep(1.2)
                after = next((i.get("checked") for i in hotkeys.menu_items(frame) if i["id"] == item["id"]), None)
                row["checked"] = [before, after]
                if before is not None and after is not None and before != after:
                    row.update(outcome="ok", detail=f"menu {' > '.join(item['path'])}: check mark {before} -> {after}")
            rows.append(row)
            print(row["outcome"], name, row["detail"], flush=True)
    finally:
        session.close(discard=True)
    passed = sorted({f"action:{r['action']}" for r in rows if r["outcome"] == "ok"})
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "symedit-menu-check", "passed": passed, "rows": rows}


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
