"""Live proofs for three 3D viewer actions: reload board, set pivot, show model bounding boxes.

* ``3DViewer.Control.reloadBoard``: the board is changed through the API (a footprint moved in the board
  editor). The 3D viewer is marked dirty by the edit (``PCB_EDIT_FRAME::OnModify`` -> ``Update3DView(true,
  live_refresh)``) but, with the live refresh off, does not rebuild its scene. The viewer's own progress
  reporter ("Build board outline", "Load OpenGL: ...", "Load completed in N s" in its status bar, read through
  UI Automation) is watched: quiet for the whole control window after the edit, a full rebuild once the key is
  pressed, and the rendered view then differs from before the edit.
* ``3DViewer.Control.pivotCenter``: the camera pivot jumps to the board point under the pointer. The camera's
  view matrix is read back through the viewport store (the viewer's "Save viewport..." keeps the camera view
  matrix, written to ``3d_viewer.json`` when the viewer closes): unchanged while the pointer only moves
  (control), translated toward the pointer by the key, and translated the other way by the key with the
  pointer on the other side of the board.
* ``3DViewer.Control.showBoundingBoxes``: audited from source. The action has no tool transition
  (``EDA_3D_CONTROLLER::setTransitions`` binds every other visibility action to ``ToggleVisibility`` but not
  this one, eda_3d_controller.cpp:483-489), so no key, menu or ``RunAction`` can reach its handler
  (eda_3d_controller.cpp:272). Live control: the neighbouring bound action ``showNavigator`` flips its setting
  (on by default, off after the key) in ``3d_viewer.json`` under the same conditions, ``showBoundingBoxes`` does not.

    python -m kicad_mcp.coverage.live_3dreload --inventory inv.json --project <dir with 3D models> --out ev.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import re
import time

from kicad_mcp.coverage.live_hotkeys import _first_of, _start
from kicad_mcp.ipc import api, hotkeys

RELOAD = "3DViewer.Control.reloadBoard"
PIVOT = "3DViewer.Control.pivotCenter"
BBOX = "3DViewer.Control.showBoundingBoxes"
NAVIGATOR = "3DViewer.Control.showNavigator"
WINDOW = {"name": "3D Viewer", "match": "contains"}
ACTIVITY = re.compile(r"^(Build |Create |Load OpenGL|Loading|Building hit-test|Load completed)")


class Viewer:
    """The 3D viewer (opened from the board editor) on a private desktop, with one hotkey per action."""

    def __init__(self, inventory: dict, project: Path, batch: list[str]):
        self.session, self.assigned, self.frame, self.canvas = _start(
            "3DViewer", next(project.glob("*.kicad_pcb")), [a["name"] for a in inventory["actions"]], batch)
        self.uia = self.session.attach_uia()
        self.config = self.session.profile / "config" / self.session._settings_version()
        self.hook = None
        self.saved = 0

    def press(self, action: str) -> None:
        hotkeys.press(self.canvas, self.assigned[action][1])

    def texts(self) -> list[str]:
        controls = self.uia.call("state", {"window": WINDOW, "selector": {
            "by": "path", "name": "*", "name_match": "any", "control_type": "Text"}})["controls"]
        return [c["name"] for c in controls if c["name"].strip()]

    def activity(self) -> list[str]:
        return [t for t in self.texts() if ACTIVITY.match(t)]

    def watch(self, seconds: float, until=None, step: float = 0.3) -> list[str]:
        """The distinct activity texts seen over ``seconds`` (stopping early when ``until(seen)`` holds)."""
        seen: list[str] = []
        end = time.time() + seconds
        while time.time() < end:
            for text in self.activity():
                if text not in seen:
                    seen.append(text)
            if until and until(seen):
                break
            time.sleep(step)
        return seen

    def size(self) -> tuple[int, int]:
        rect = hotkeys._Rect()
        hotkeys.user32.GetClientRect(self.canvas, hotkeys.ctypes.byref(rect))
        return rect.right, rect.bottom

    def point(self, x: int, y: int) -> None:
        """The pointer is at the canvas point (x, y): the session's own cursor reading and posted moves."""
        if self.hook is None:
            from kicad_mcp.coverage.cursor_hook import CursorHook

            self.hook = CursorHook(self.session.pid)
        self.hook.place(self.canvas, x, y)
        for step in range(4):
            hotkeys.user32.PostMessageW(self.canvas, 0x0200, 0, (y << 16) | (x - 8 * (3 - step)))
            time.sleep(0.1)

    def save_viewport(self, name: str) -> None:
        """Keep the camera as a named viewport: the viewports box (the last combo box of the viewer) is moved to
        its "Save viewport..." entry with the Down key, and the name typed into the dialog it opens."""
        combos = [c[0] for c in hotkeys._children(self.frame) if c[1] == "ComboBox"]
        # the box is put on its separator (third from the end, no event), then Down moves it to "Save viewport..."
        count = hotkeys.user32.SendMessageW(combos[-1], 0x146, 0, 0)  # CB_GETCOUNT
        hotkeys.user32.SendMessageW(combos[-1], 0x14E, count - 3, 0)  # CB_SETCURSEL
        time.sleep(0.3)
        hotkeys.press(combos[-1], 0x28)
        dialog = {"name": "Save Viewport", "match": "exact"}
        for _ in range(20):
            try:
                self.uia.call("find", {"window": dialog})
                break
            except Exception:  # noqa: BLE001 - not open yet
                time.sleep(0.3)
        else:
            raise RuntimeError("the Save Viewport dialog did not open")
        for _ in range(30):  # the dialog may still be laying out its controls
            try:
                self.uia.call("set-value", {"window": dialog, "selector": {
                    "by": "path", "name": "Viewport name:", "control_type": "Edit"}, "value": name})
                self.uia.call("invoke", {"window": dialog, "selector": {
                    "by": "path", "name": "OK", "control_type": "Button"}})
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
                if not self.uia.call("dump-tree", {"window": dialog, "max_depth": 2})["windows"]:
                    break  # the dialog closed while OK was being invoked
        else:
            tree = self.uia.call("dump-tree", {"window": dialog, "max_depth": 5})
            raise RuntimeError(f"the Save Viewport dialog could not be filled: {json.dumps(tree)[:300]} "
                               f"windows {sorted(hotkeys._visible(self.session).values())}")
        time.sleep(1.0)
        self.saved += 1

    def close_cleanly(self) -> None:
        """Close the viewer, then the board editor, the normal way; the editors save their settings on the way out."""
        import psutil

        hotkeys.user32.PostMessageW(self.frame, hotkeys.WM_CLOSE, 0, 0)
        time.sleep(3)
        pcb, _ = hotkeys.canvas_of(self.session, "PCB Editor")
        hotkeys.user32.PostMessageW(pcb, hotkeys.WM_CLOSE, 0, 0)
        for _ in range(60):
            if not psutil.pid_exists(self.session.pid):
                break
            time.sleep(1)

    def settings(self) -> dict:
        path = self.config / "3d_viewer.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def project(self) -> dict:
        """The project file (the viewer keeps the saved viewports there, written when the project closes)."""
        merged: dict = {}
        for pro in Path(self.session.document).parent.glob("*.kicad_pro"):
            merged[pro.name] = json.loads(pro.read_text(encoding="utf-8"))
        return merged

    def close(self) -> None:
        if self.hook:
            with contextlib.suppress(Exception):
                self.hook.close()
        self.session.close(discard=True)


def _result(inventory: dict, route: str, rows: list[dict], gaps: dict[str, str] | None = None) -> dict:
    out = {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
           "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "route": route,
           "passed": [f"action:{r['action']}" for r in rows if r["outcome"] == "ok"], "rows": rows}
    if gaps:
        out["upstream_gaps"] = [f"action:{a}" for a in gaps]
        out["gap_reasons"] = {f"action:{a}": why for a, why in gaps.items()}
    return out


# ---- reloadBoard ---------------------------------------------------------------------------------------------

REBUILD = ("Build ", "Create ", "Load OpenGL", "Loading")
ROTATE = "3DViewer.Control.rotateXclockwise"


def _rebuilt(seen: list[str]) -> bool:
    return any(t.startswith(REBUILD) for t in seen)


def reload_case(inventory: dict, project: Path) -> dict:
    """Clean state, nothing pending: the viewer is left alone (control A), then a key that only moves the view is
    pressed (control B: it repaints but must not rebuild), then the reload key. The viewer's own progress texts
    ("Build board outline", "Load OpenGL: ...", "Load completed in N s") are the readback. A second phase moves a
    footprint through the API and shows the viewer does not follow it until the key is pressed."""
    row = {"action": RELOAD, "outcome": "no_effect", "detail": ""}
    viewer = Viewer(inventory, project, [RELOAD, ROTATE])
    try:
        loaded = viewer.watch(240, until=lambda seen: any(t.startswith("Load completed") for t in seen))
        row["first_load"] = loaded
        time.sleep(15)  # the background hit-test build that follows the load finishes
        idle = viewer.watch(12)
        viewer.press(ROTATE)
        rotated = viewer.watch(12)
        row["control_idle"], row["control_rotate_key"] = idle, rotated
        viewer.press(RELOAD)
        built = viewer.watch(240, until=lambda seen: any(t.startswith("Load completed") for t in seen))
        row["after_reload_key"] = built
        clean_ok = (not _rebuilt(idle) and not _rebuilt(rotated) and _rebuilt(built)
                    and any(t.startswith("Load completed") for t in built))
        time.sleep(15)
        # phase two: the board changes under the viewer
        ident = _first_of(viewer.session, "KOT_PCB_FOOTPRINT")
        item = api.call(viewer.session, "GetItemsById", {"items": [{"value": ident}]})["response"]["items"][0]
        pos = item["position"]
        item["position"] = {"x_nm": str(int(pos.get("x_nm", 0)) + 15_000_000), "y_nm": pos.get("y_nm", "0")}
        api.call(viewer.session, "UpdateItems", {"items": [item]})
        edited_quiet = viewer.watch(15)
        viewer.press(RELOAD)
        edited_built = viewer.watch(240, until=lambda seen: any(t.startswith("Load completed") for t in seen))
        row["after_board_edit_no_key"], row["after_board_edit_key"] = edited_quiet, edited_built
        if clean_ok:
            row.update(outcome="ok", detail=(
                f"with nothing pending the viewer stayed quiet when left alone ({idle}) and after a view-only key "
                f"({rotated}); the reload key made it rebuild its scene ({built}). After a footprint was moved through "
                f"the API it stayed quiet ({edited_quiet}) until the key, which rebuilt it ({edited_built})"))
        else:
            row["detail"] = f"idle {idle}; view key {rotated}; reload key {built}"
    except Exception as exc:  # noqa: BLE001
        import traceback

        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-600:])
    finally:
        viewer.close()
    print(row["outcome"], RELOAD, row["detail"], flush=True)
    return row


# ---- pivotCenter ---------------------------------------------------------------------------------------------

def _viewports(settings: dict) -> dict[str, list[float]]:
    """name -> the saved camera view matrix, wherever the settings file keeps the list."""
    found: dict[str, list[float]] = {}

    def walk(node):
        if isinstance(node, dict):
            if "name" in node and "xx" in node:  # the project file's layout: one key per matrix entry
                found[node["name"]] = [float(node[a + b]) for a in "xyzw" for b in "xyzw"]
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(settings)
    return found


def _flatten(value):
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    else:
        yield value


def pivot_case(inventory: dict, project: Path) -> dict:
    row = {"action": PIVOT, "outcome": "no_effect", "detail": ""}
    viewer = Viewer(inventory, project, [PIVOT])
    try:
        # mouse moves are ignored while the scene is still loading (eda_3d_canvas.cpp OnMouseMove)
        loaded = viewer.watch(240, until=lambda seen: any(t.startswith("Load completed") for t in seen))
        row["first_load"] = loaded
        time.sleep(4)
        width, height = viewer.size()
        centre = (width // 2, height // 2)
        viewer.point(*centre)
        time.sleep(2)
        viewer.save_viewport("v0_start")
        viewer.point(width // 2 + width // 12, height // 2 + height // 12)  # right of and below centre, over the board; nothing pressed
        time.sleep(2)
        viewer.save_viewport("v1_pointer_only")
        viewer.press(PIVOT)
        time.sleep(5)  # the camera glides to the new pivot
        viewer.save_viewport("v2_pivot_right")
        viewer.point(width // 2 - width // 12, height // 2 - height // 12)
        time.sleep(2)
        viewer.press(PIVOT)
        time.sleep(5)
        viewer.save_viewport("v3_pivot_left")
        viewer.close_cleanly()
        saved = _viewports(viewer.project())
        row["board_keys"] = sorted(next(iter(viewer.project().values()), {}).get("board", {}))
        row["viewports"] = saved
        if len(saved) < 4:
            row["detail"] = f"the viewer saved only {sorted(saved)}"
            return row
        v0, v1, v2, v3 = (saved[n] for n in ("v0_start", "v1_pointer_only", "v2_pivot_right", "v3_pivot_left"))
        moved = [i for i, (a, b) in enumerate(zip(v1, v2)) if abs(a - b) > 1e-4]
        row["changed_entries"] = {"pointer_only": [i for i, (a, b) in enumerate(zip(v0, v1)) if abs(a - b) > 1e-4],
                                  "first_pivot": moved,
                                  "second_pivot": [i for i, (a, b) in enumerate(zip(v2, v3)) if abs(a - b) > 1e-4]}
        # rotation block (first three rows' 3x3) must stay equal; only the translation (last column/row) moves
        step_right = [b - a for a, b in zip(v1, v2)]
        step_left = [b - a for a, b in zip(v2, v3)]
        opposite = any(abs(r) > 1e-3 and abs(lf) > 1e-3 and r * lf < 0 for r, lf in zip(step_right, step_left))
        if v0 == v1 and moved and set(moved) <= {12, 13, 14} and opposite:
            row.update(outcome="ok", detail=(
                "viewport camera matrices: unchanged by moving the pointer alone (v0 == v1, control); the key with the "
                f"pointer right of and below centre translated the camera (matrix entries {moved}, the rotation block unchanged); "
                "the key with the pointer left of and above centre translated it the opposite way"))
        else:
            row["detail"] = (f"v0==v1 {v0 == v1}, first pivot changed {moved}, opposite directions {opposite}")
    except Exception as exc:  # noqa: BLE001
        import traceback

        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-600:])
    finally:
        viewer.close()
    print(row["outcome"], PIVOT, row["detail"], row.get("trace", ""), flush=True)
    return row


# ---- showBoundingBoxes -----------------------------------------------------------------------------------------

BBOX_GAP = (
    "3DViewer.Control.showBoundingBoxes is declared (3d-viewer/3d_viewer/tools/eda_3d_actions.cpp:363-369, no hotkey) "
    "and its handler branch exists (eda_3d_controller.cpp:272, inside EDA_3D_CONTROLLER::ToggleVisibility), but "
    "EDA_3D_CONTROLLER::setTransitions (eda_3d_controller.cpp:419-490) binds ToggleVisibility to showTHT, showSMD, "
    "showVirtual, showNotInPosFile, showDNP and showNavigator (lines 483-489) and never to showBBoxes, so the tool "
    "framework has no transition for the action's event: no key, menu entry or RunAction reaches the handler. The "
    "only other references are the check-mark condition (eda_3d_viewer_frame.cpp:293, 325) and the appearance "
    "panel's row label (appearance_controls_3D.cpp:127), which does not call the action; the Model Bounding Boxes "
    "row there toggles the layer directly. Live control (evidence rows): in the same session the bound neighbour "
    "showNavigator flips render.show_navigator (default on) to off in 3d_viewer.json after a normal close, showBoundingBoxes leaves "
    "render.opengl_show_model_bbox untouched.")


def bbox_case(inventory: dict, project: Path) -> dict:
    row = {"action": BBOX, "outcome": "no_effect", "detail": ""}
    viewer = Viewer(inventory, project, [BBOX, NAVIGATOR])
    try:
        viewer.watch(240, until=lambda seen: any(t.startswith("Load completed") for t in seen))
        time.sleep(4)
        viewer.press(NAVIGATOR)
        time.sleep(2)
        viewer.press(BBOX)
        time.sleep(2)
        viewer.close_cleanly()
        settings = viewer.settings()
        render = settings.get("render", {})
        row["settings"] = {"show_navigator": render.get("show_navigator"),
                           "opengl_show_model_bbox": render.get("opengl_show_model_bbox")}
        navigator_flipped = render.get("show_navigator") is False  # on by default (eda_3d_viewer_settings.cpp:380-381)
        bbox_flipped = render.get("opengl_show_model_bbox") is True
        row["navigator_flipped"], row["bbox_flipped"] = navigator_flipped, bbox_flipped
        if bbox_flipped:
            row.update(outcome="ok", detail="the setting flipped after the key")
        elif navigator_flipped:
            row.update(outcome="upstream_gap", detail=BBOX_GAP)
        else:
            row["detail"] = f"neither setting changed ({row['settings']}): the control did not work, no verdict"
    except Exception as exc:  # noqa: BLE001
        import traceback

        row.update(outcome="error", detail=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-600:])
    finally:
        viewer.close()
    print(row["outcome"], BBOX, row["detail"][:200], row.get("settings"), flush=True)
    return row


CASES = {"reload": reload_case, "pivot": pivot_case, "bbox": bbox_case}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--only", choices=sorted(CASES))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    rows = [case(inventory, args.project) for name, case in CASES.items() if not args.only or args.only == name]
    gaps = {r["action"]: BBOX_GAP for r in rows if r["outcome"] == "upstream_gap"}
    args.out.write_text(json.dumps(_result(inventory, "3d-viewer", rows, gaps), indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
