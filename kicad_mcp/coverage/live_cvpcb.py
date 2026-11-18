"""Live proof for the footprint assigner (cvpcb), read back through UI Automation.

The assigner has no drawing canvas and no API of its own; its toolbar is not exposed to UI Automation, but
its three lists are (components, libraries, footprints). Each action gets the context a person would set up
(a component and a footprint selected, an association removed first, ...) through UIA selection, keyboard
focus goes to the list a person would be in, and the action's own key (the session profile's hotkey) is
pressed there. It passes only with an observed effect: the lists' rows, selection or focus changed, a
window opened, or the schematic changed (API item hash).

    python -m kicad_mcp.coverage.live_cvpcb --inventory inv.json --project <dir> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
import json
from pathlib import Path
import tempfile
import time

from kicad_mcp.coverage.live_hotkeys import _input_for, _start
from kicad_mcp.ipc import hotkeys

WINDOW = {"name": "Assign Footprints", "match": "exact"}
ROW = {"by": "path", "name": "*", "name_match": "any", "control_type": "ListItem"}
LIST = {"by": "path", "name": "*", "name_match": "any", "control_type": "List"}

# The order matters: later actions use the state earlier ones leave (an association removed, a filter on).
ORDER = ["cvpcb.Control.changeFocusRight", "cvpcb.Control.changeFocusLeft",
         "cvpcb.Control.FilterFPbyLibrary", "cvpcb.Control.FilterFPByPin", "cvpcb.Control.FilterFPbyFPFilters",
         "cvpcb.Association.Delete", "cvpcb.Control.GotoNextNA", "cvpcb.Control.GotoPreviousNA",
         "cvpcb.Association.Delete#plain-value", "cvpcb.Association.AutoAssociate", "cvpcb.Association.Delete#again", "cvpcb.Association.Associate",
         "cvpcb.Control.SaveAssociationsToFile", "cvpcb.Association.DeleteAll",
         "cvpcb.Association.Associate#again", "cvpcb.Control.SaveAssociationsToSchematic",
         "cvpcb.Control.ShowFootprintViewer"]
# Auto-association needs an equivalence (value -> footprint) for an unassigned component: the fixture
# project lists one. KiCad turns spaces in an equivalence's value into underscores before comparing it with
# the symbol's value (auto_associate.cpp), so only a value without spaces can ever match (upstream bug);
# the component unassigned for it has such a value.
EQU_NAME = "kicad-ai-tools.equ"


def lists(uia) -> list[dict]:
    """The assigner's three lists with their rows' text and selection, and which list has focus."""
    found = uia.call("state", {"window": WINDOW, "selector": LIST})["controls"]
    rows = uia.call("state", {"window": WINDOW, "selector": ROW})["controls"]
    return [{"lists": [(c["automation_id"], c["focused"]) for c in found],
             "rows": [(r["name"], r.get("selected", False)) for r in rows]}]


def _list_ids(uia) -> dict[str, str]:
    """automation id of the component, library and footprint list, told apart by their rows."""
    tree = uia.call("dump-tree", {"window": WINDOW, "max_depth": 3, "max_nodes": 3000})["windows"][0]
    ids = {}
    for child in tree.get("children", []):
        if child.get("control_type") != "List":
            continue
        names = [g.get("name", "") for g in child.get("children", []) if g.get("control_type") == "ListItem"]
        if not names:
            continue
        if any(" - " in n for n in names[:5]):
            ids["components"] = child["automation_id"]
        elif all(":" in n for n in names[:5]):
            ids["footprints"] = child["automation_id"]
        else:
            ids["libraries"] = child["automation_id"]
    return ids


def _select_row(uia, prefix: str) -> None:
    uia.call("select", {"window": WINDOW, "selector": {"by": "path", "name": prefix, "name_match": "prefix",
                                                      "control_type": "ListItem"}})
    time.sleep(0.4)


def _rows(uia) -> list[str]:
    return [r["name"] for r in uia.call("state", {"window": WINDOW, "selector": ROW})["controls"]]


def _value(row: str) -> str:
    return row.split(" - ", 1)[-1].split(" : ", 1)[0].strip()


def _prepare(uia, name: str, ids: dict[str, str], step: str = "") -> str:
    """Set the context ``name`` needs; return the automation id of the list a person would press it in."""
    rows = _rows(uia)
    components = [r for r in rows if " - " in r]
    footprints = [r for r in rows if ":" in r and " - " not in r]
    last = name.rsplit(".", 1)[-1]
    # The deleted (unassigned) component is the second one, so "next" from the first and "previous" from
    # the last both have somewhere to go.
    if components and last == "GotoNextNA":
        _select_row(uia, components[0][:12])
    elif components and last == "GotoPreviousNA":
        _select_row(uia, components[-1][:12])
    elif step.endswith("#plain-value"):
        plain = [c for c in components if " " not in _value(c) and c.rstrip().endswith(tuple("abcdefghijklmnopqrstuvwxyz0123456789"))]
        if plain:
            _select_row(uia, plain[0][:12])
    elif len(components) > 1 and last in ("Delete", "Associate"):
        _select_row(uia, components[1][:12])
    elif components and last in ("FilterFPByPin", "FilterFPbyFPFilters"):
        _select_row(uia, components[0][:12])
    if last == "Associate" and footprints:  # a footprint other than the component's current one
        current = components[1].split(" : ", 1)[-1].strip() if len(components) > 1 else ""
        other = [f for f in footprints if not current or not f.strip().endswith(current)]
        _select_row(uia, (other or footprints)[-1][:12])
    if last == "FilterFPbyLibrary":
        library = [r for r in rows if r not in components and r not in footprints]
        if library:
            _select_row(uia, library[-1])
    target = ids.get("footprints") if last in ("Associate",) else ids.get("components")
    return target or next(iter(ids.values()))


def _fixture(project: Path) -> Path:
    """A copy of ``project`` whose project file lists an equivalence (value -> footprint) for every
    value that has a single footprint in the schematic."""
    import re
    import shutil

    copy = Path(scoped_mkdtemp(prefix="kicad-ai-tools-cvpcb-fixture-")) / project.name
    shutil.copytree(project, copy, ignore=shutil.ignore_patterns("~*", "*.lck", "*-backups"))
    schematic = next(copy.glob("*.kicad_sch"))
    text = schematic.read_text(encoding="utf-8")
    pairs: dict[str, set[str]] = {}
    for block in re.split(r"\(symbol\s+\(lib_id", text)[1:]:
        value = re.search(r'\(property "Value" "([^"]+)"', block)
        footprint = re.search(r'\(property "Footprint" "([^"]+)"', block)
        if value and footprint and footprint.group(1):
            pairs.setdefault(value.group(1), set()).add(footprint.group(1))
    lines = [f"'{v}' '{next(iter(f))}'" for v, f in sorted(pairs.items()) if len(f) == 1]
    if lines:
        (copy / EQU_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")
        pro = next(copy.glob("*.kicad_pro"))
        settings = json.loads(pro.read_text(encoding="utf-8"))
        # absolute: the session's candidate copy carries only KiCad's own files
        settings.setdefault("cvpcb", {})["equivalence_files"] = [(copy / EQU_NAME).as_posix()]
        pro.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return copy


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


@cleanup_after_call
def sweep(inventory: dict, project: Path, only: set[str] | None = None) -> dict:
    project = _fixture(project)
    document = _input_for("cvpcb", project, Path(scoped_mkdtemp(prefix="kicad-ai-tools-cvpcb-")))
    all_actions = [a["name"] for a in inventory["actions"]]
    steps = [n for n in ORDER if n.split("#")[0] in set(all_actions) and (only is None or n.split("#")[0] in only)]
    targets = list(dict.fromkeys(n.split("#")[0] for n in steps))
    rows: list[dict] = []
    session, assigned, frame, _canvas = _start("cvpcb", document, all_actions, targets)
    try:
        uia = session.attach_uia()
        ids = _list_ids(uia)
        for step in steps:
            name = step.split("#")[0]
            row = {"action": name, "step": step, "outcome": "no_effect", "key": assigned[name][0], "detail": ""}
            try:
                target = _prepare(uia, name, ids, step)
                uia.call("focus", {"window": WINDOW, "selector": {"by": "automation_id", "automation_id": target}})
                time.sleep(0.3)
                before, windows = lists(uia), hotkeys._visible(session)
                document_before = hotkeys.document_hash(session)
                focus_before, file_before = hotkeys.focused(frame), _file_hash(session.document)
                hotkeys.press(hotkeys.focused(frame), assigned[name][1])
                time.sleep(3.0 if "Save" in name or "Auto" in name else 1.5)
                opened = [t or "(untitled)" for h, t in hotkeys._visible(session).items() if h not in windows]
                for hwnd in [h for h in hotkeys._visible(session) if h not in windows]:
                    hotkeys.user32.PostMessageW(hwnd, hotkeys.WM_CLOSE, 0, 0)
                time.sleep(0.5)
                after = lists(uia)
                document_after = hotkeys.document_hash(session)
                focus_after, file_after = hotkeys.focused(frame), _file_hash(session.document)
                effects = []
                if focus_before != focus_after:
                    effects.append(f"keyboard focus moved {focus_before:#x} -> {focus_after:#x}")
                if file_before != file_after:
                    effects.append("schematic file on disk changed")
                if opened:
                    effects.append(f"opened {opened}")
                if before[0]["rows"] != after[0]["rows"]:
                    changed = [a for a, b in zip(after[0]["rows"], before[0]["rows"]) if a != b]
                    if len(after[0]["rows"]) != len(before[0]["rows"]):
                        effects.append(f"list rows {len(before[0]['rows'])} -> {len(after[0]['rows'])}")
                    elif any(a[0] != b[0] for a, b in zip(after[0]["rows"], before[0]["rows"])):
                        effects.append(f"row text changed: {[c[0] for c in changed][:2]}")
                    else:
                        effects.append(f"selection moved to {[c[0] for c in changed if c[1]][:1]}")
                if before[0]["lists"] != after[0]["lists"]:
                    effects.append(f"focus {before[0]['lists']} -> {after[0]['lists']}")
                if document_before and document_after and document_before != document_after:
                    effects.append("schematic changed")
                if effects:
                    row.update(outcome="ok", detail="; ".join(effects))
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
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": "cvpcb-uia", "passed": passed, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    for row in result["rows"]:
        print(row["outcome"], row["action"], row["detail"][:110])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
