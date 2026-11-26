"""Live sweep of every editor action RunAction can reach, in a GUI editor on a private desktop.

Only the board editor registers RunAction in the API (KiCad 11 nightlies), so actions run in a board
GuiSession; an action another frame owns comes back RAS_INVALID and is reported as unreachable by
RunAction, with its editor named, rather than counted.

Per action: RunAction, then
- any window it opened on the private desktop (a dialog) is closed with WM_CLOSE, i.e. Cancel, and
  recorded: the action ran, its input step is UI;
- if the editor stays busy (an interactive tool waiting for the mouse) it is cancelled, else the
  session is restarted;
- the user-desktop tripwire is checked: a window reaching the user's screen kills the session.

Actions that hand off to another program (browser, Explorer, an external text editor, the calculator)
would open windows outside this process tree, where the tripwire cannot see them; they are never run
and are listed as ``external`` so they are covered by their own route instead.

Evidence: an action is ``passed`` only with an observed effect, as on every other route: a window it
opened, a change in the document (an API item hash), or a change in the editor window. RAS_OK alone only
says KiCad found and dispatched the action (an action of another frame is accepted and does nothing), so
it is reported ``accepted_no_effect`` and stays unverified. Selection-dependent actions are retried with a
footprint, a track and a zone selected.

    python -m kicad_mcp.coverage.live_actions --inventory inv.json --project <dir> --out evidence.json
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import json
from pathlib import Path
import re
import time

from kicad_mcp.ipc import api
from kicad_mcp.ipc.client import Client
from kicad_mcp.ipc.gui_session import GuiSession

WM_CLOSE = 0x0010
# The last segment of actions that start another program or open a URL, a folder or a shell handler.
EXTERNAL = re.compile(r"^(help|donate|reportBug|getInvolved|gettingStarted|print|openDirectory|openWithTextEditor|"
                      r"pluginsShowFolder|OpenProjectDirectory|OpenDemoProject|showCalculatorTools|"
                      r"showDatasheet|\w*(Website|Url|URL|Folder|Explorer|Browser|TextEditor))$")
RUNACTION_EDITORS = ("pcbnew", "common")  # frames whose API handler serves RunAction


def _visible(session: GuiSession) -> dict[int, str]:
    return {w["hwnd"]: w["title"] for w in session.kicad.desktop.windows()
            if w["visible"] and w["pid"] in {session.pid, *session.process.pids()}}


def _close_new_windows(session: GuiSession, before: dict[int, str]) -> list[str]:
    closed = []
    for _ in range(5):  # a dialog may open a second one; a few rounds settle it
        new = {h: t for h, t in _visible(session).items() if h not in before}
        if not new:
            break
        for hwnd, title in new.items():
            ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed.append(title or "(untitled)")
        time.sleep(0.4)
    return closed


def _busy(session: GuiSession) -> bool:
    probe = Client(session.socket_url, session.schema, "kicad-ai-tools.probe", timeout_ms=1500)
    try:
        probe.send(session.schema.new("kiapi.common.commands.Ping"))
        return False
    except (api.ApiCallError, ConnectionError):
        return True
    finally:
        probe.close()


def _settle(session: GuiSession, before: dict[int, str], seconds: float = 8.0) -> tuple[list[str], bool]:
    """Close whatever the action opened until KiCad answers pings again; (closed titles, responsive)."""
    closed: list[str] = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        closed += _close_new_windows(session, before)
        if not _busy(session):
            return closed, True
        with contextlib.suppress(api.ApiCallError, ConnectionError):
            Client(session.socket_url, session.schema, "kicad-ai-tools.cancel", timeout_ms=1000).send(
                api.build_request(session.schema, "RunAction", {"action": "common.Interactive.cancel"}))
        time.sleep(0.3)
    return closed, False


BOARD_SELECTIONS = [None, "KOT_PCB_FOOTPRINT", "KOT_PCB_TRACE", "KOT_PCB_ZONE"]
# Editor state the GUI API reports: an action that changes only this (active layer, visible layers,
# appearance, selection) has a real effect a window capture cannot see.
STATE_COMMANDS = ("GetActiveLayer", "GetVisibleLayers", "GetBoardEditorAppearanceSettings")


def editor_state(session: GuiSession) -> str | None:
    import hashlib
    import json as _json

    parts = {}
    for command in STATE_COMMANDS:
        try:
            parts[command] = api.call(session, command, {})["response"]
        except Exception:  # noqa: BLE001 - a busy editor reports nothing; that is not a change
            return None
    try:
        selection = api.call(session, "GetSelection", {})["response"].get("items", [])
        parts["selection"] = sorted(i.get("id", {}).get("value", "") for i in selection)
    except Exception:  # noqa: BLE001
        return None
    return hashlib.sha256(_json.dumps(parts, sort_keys=True).encode()).hexdigest()


def enable_all_copper(session: GuiSession) -> None:
    """A 32-copper-layer candidate, so layer actions for every inner layer have a layer to act on."""
    current = api.call(session, "GetBoardEnabledLayers", {})["response"]
    layers = list(dict.fromkeys([*current.get("layers", []), *[f"BL_In{i}_Cu" for i in range(1, 31)]]))
    api.call(session, "SetBoardEnabledLayers", {"copper_layer_count": 32, "layers": layers})


def _prepare(session: GuiSession, name: str) -> None:
    """Context an action needs to show its effect: a layer switch starts from another layer; a cursor
    action needs the pointer inside the canvas so its crosshair is drawn."""
    import ctypes

    from kicad_mcp.ipc import hotkeys

    last = name.rsplit(".", 1)[-1]
    if name.startswith("pcbnew.Control.layer"):
        start = "BL_B_Cu" if last in ("layerTop", "layerNext", "layerPrev") else "BL_F_Cu"
        with contextlib.suppress(api.ApiCallError):
            api.call(session, "SetActiveLayer", {"layer": start})
    if last.startswith("cursor") or "Crosshairs" in last:
        _, canvas = hotkeys.canvas_of(session, "PCB Editor")
        ctypes.windll.user32.PostMessageW(canvas, 0x0200, 0, (300 << 16) | 400)  # WM_MOUSEMOVE into the canvas
        time.sleep(0.3)


def _frame(session: GuiSession) -> int:
    cached = getattr(session, "_board_frame", None)
    if cached is None:
        from kicad_mcp.ipc import hotkeys

        cached, _ = hotkeys.canvas_of(session, "PCB Editor")
        session._board_frame = cached
    return cached


def run_action(session: GuiSession, name: str, baseline: dict[int, str] | None = None,
               retry: bool = True) -> tuple[str, str]:
    """Run the action, with nothing selected and then with each kind of item selected, until it shows an
    effect; ``ok`` only with an effect."""
    from kicad_mcp.coverage.live_hotkeys import _first_of
    from kicad_mcp.ipc import hotkeys

    last = ("error", "not run")
    for kind in BOARD_SELECTIONS:
        hotkeys.select(session, _first_of(session, kind))
        frame = _frame(session)
        children = hotkeys.visible_children(frame)
        pixels, document = hotkeys.capture(frame), hotkeys.document_hash(session)
        _prepare(session, name)
        state = editor_state(session)
        outcome, detail = _run_once(session, name, baseline, retry)
        effects = []
        after_state = editor_state(session)
        if state and after_state and state != after_state:
            effects.append("editor state changed")
        if "closed [" in detail:
            effects.append("opened a window")
        after_document = hotkeys.document_hash(session)
        if document and after_document and document != after_document:
            effects.append("document changed")
        if hotkeys.capture(frame) != pixels:
            effects.append("editor window changed")
        messages = hotkeys.new_messages(frame, children)
        effects = hotkeys.judge(effects, messages)
        if messages:
            detail = f"{detail}; message {messages}"
        hotkeys.select(session, None)
        if outcome == "ok" and effects:
            return "ok", f"{detail}; {', '.join(effects)}; selected {kind or 'nothing'}"
        last = ("accepted_no_effect", detail) if outcome == "ok" else (outcome, detail)
        if outcome in ("invalid", "error") or not session.healthy():
            break
    return last


def _run_once(session: GuiSession, name: str, baseline: dict[int, str] | None = None,
              retry: bool = True) -> tuple[str, str]:
    """Send the action with a short timeout: KiCad answers RunAction only after a dialog it opened closes,
    so no answer means a dialog is up; it is closed (Cancel) and the action counts as run. A dialog the
    previous action opened late is cleared first, and an action that meets a busy editor is retried once."""
    if baseline is not None:
        _settle(session, baseline, seconds=4.0)
    before = _visible(session)
    sender = Client(session.socket_url, session.schema, "kicad-ai-tools.action", timeout_ms=2500)
    status, note = "", ""
    try:
        status = sender.send(api.build_request(session.schema, "RunAction", {"action": name})).status
        status = session.schema.pool.FindEnumTypeByName(
            "kiapi.common.commands.RunActionStatus").values_by_number[status].name
    except api.ApiCallError as exc:
        note = f"{exc.status}: {exc}"
    except ConnectionError:
        note = "no answer while a dialog was open"
    finally:
        sender.close()
    time.sleep(0.2)
    closed, responsive = _settle(session, before)
    session.kicad._tripwire(0.1)  # raises (and kills) if anything reached the user's desktop
    detail = "; ".join(x for x in (status, note, f"closed {closed}" if closed else "") if x)
    if retry and note.startswith("AS_BUSY"):
        return _run_once(session, name, baseline, retry=False)
    if status == "RAS_OK" or (note.startswith("no answer") and closed and responsive):
        return "ok", detail
    if status == "RAS_INVALID":
        return "invalid", detail
    return "error", detail + ("" if responsive else "; editor stayed busy")


def sweep(inventory: dict, project: Path, start: int = 0, count: int | None = None,
          skip: set[str] | None = None) -> dict:
    actions = [a for a in inventory["actions"] if a["runnable"]]
    rows: list[dict] = []
    reachable = []
    for action in actions:
        text = f"{action['name']} {action['friendly_name']} {action['tooltip']}"
        if EXTERNAL.match(action["name"].rsplit(".", 1)[-1]):
            rows.append({"action": action["name"], "outcome": "external", "detail": text})
        elif action["editor"] not in RUNACTION_EDITORS:
            rows.append({"action": action["name"], "outcome": "no_runaction",
                         "detail": f"the {action['editor']} frame has no RunAction handler in the API"})
        elif f"action:{action['name']}" not in (skip or set()):
            reachable.append(action["name"])

    if start or count is not None:  # a chunk: only its slice of the reachable actions, no static rows
        reachable = reachable[start:None if count is None else start + count]
        rows = [] if start else rows
    board = next(project.glob("*.kicad_pcb"))
    launched: list[dict] = []  # every editor this sweep started, to prove each one ended

    owned_dirs: list[Path] = []  # every candidate and profile folder, to sweep escaped relaunches at the end
    swept_since = time.time()  # no process created before the sweep began can be one of its escapees
    app_roots: set[Path] = set()  # the KiCad installation(s) the sweep's editors ran from
    evidence: dict[int, tuple[list[Path], Path, float]] = {}  # pid -> its folders, install and launch time

    def start() -> GuiSession:
        before = time.time()  # the editor is created after this: evidence for the survivor check
        started = GuiSession(board).start()
        # keep the board as it is if refused; layer actions beyond its layers then show no effect
        with contextlib.suppress(api.ApiCallError):
            enable_all_copper(started)
        owned_dirs.extend([started.candidate.work_dir, started.profile])
        app_roots.add(started.app_root)
        launched.append({"pid": started.pid, "at": time.strftime("%H:%M:%S"), "after": len(rows)})
        evidence[started.pid] = ([started.candidate.work_dir, started.profile], started.app_root, before)
        return started

    session = start()
    baseline = _visible(session)  # the editor's own windows; anything beyond these is a dialog
    try:
        for name in reachable:
            outcome, detail = run_action(session, name, baseline)
            rows.append({"action": name, "outcome": outcome, "detail": detail})
            if not session.healthy() or _busy(session) or outcome == "error":
                # An error may leave the frame refusing RunAction while still answering pings and document
                # queries (a picker or dialog the sweep could not see); a fresh editor isolates the next one.
                session.close(discard=True)
                session = start()
                baseline = _visible(session)
                rows[-1]["detail"] += "; session restarted after"
    finally:
        session.close(discard=True)
        survivors = _stop_survivors(launched, evidence)
        from kicad_mcp.ipc.session import stop_strays, stray_image_root

        # relaunched outside any job: proven ours by image, creation time and their folders
        escaped = [pid for root in app_roots
                   for pid in stop_strays(*owned_dirs, image_root=stray_image_root(root), since=swept_since)]
        if escaped:
            survivors.append({"escaped_pids": escaped})
    passed = sorted(f"action:{r['action']}" for r in rows if r["outcome"] == "ok")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "rows": rows,
            "sessions": launched, "survivors": survivors}


def _stop_survivors(launched: list[dict], evidence: dict[int, tuple[list[Path], Path, float]]) -> list[dict]:
    """Any editor this sweep launched that is still running after its session closed: reported, then
    stopped. The recorded PID alone is not trusted (it may have been reused): the same checks as every other
    stray stop apply, read through the handle that terminates it (KiCad image, created after the launch began,
    command line naming that session's own folders). Only the process itself is stopped, never a tree."""
    from kicad_mcp.ipc.session import stop_strays, stray_image_root

    survivors = []
    for entry in launched:
        if entry["pid"] not in evidence:
            continue
        folders, root, since = evidence[entry["pid"]]
        if stop_strays(*folders, image_root=stray_image_root(root), since=since, pids=[entry["pid"]]):
            survivors.append(entry)
    return survivors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int)
    parser.add_argument("--skip-passed", type=Path, nargs="*", default=[],
                        help="evidence files whose passed actions are not run again")
    args = parser.parse_args(argv)
    skip = {i for f in args.skip_passed for i in json.loads(f.read_text(encoding="utf-8")).get("passed", [])}
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.start, args.count, skip)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
