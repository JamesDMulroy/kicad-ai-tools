"""Live sweep of every API command in an inventory, against headless sessions on a test project.

Each handled command is sent once to a schematic session and once to a board session (both candidate
copies), with the payload from PAYLOADS when the command needs arguments, else an empty request (the
session's document is filled in automatically). Outcomes per command:

- ``ok``          KiCad answered AS_OK on some session: written to the evidence as verified;
- ``bad_request`` reached the handler but needs a better payload (add one to PAYLOADS);
- ``unhandled``   no session type served it;
- ``error``       anything else (message kept).

Only ``ok`` becomes evidence, so the gate counts nothing that did not actually run.

    python -m kicad_mcp.coverage.live_api --inventory inv.json --project <dir> --out evidence.json
"""
from __future__ import annotations

import argparse
import contextlib
import json
from pathlib import Path
import time

from kicad_mcp.coverage.live_payloads import (
    AFTER,
    GUI_LAST,
    GUI_ORDER,
    LAST,
    PAYLOADS,
    RESTART_AFTER,
    Context,
    _lib_id,
)
from kicad_mcp.ipc import api
from kicad_mcp.ipc.session import HeadlessSession, SessionError

# Commands never sent by the sweep, with the reason (each still needs its own targeted live test).
SKIP: dict[str, str] = {}


def _attempt(session: HeadlessSession, ctx: Context, name: str) -> tuple[str, str]:
    builder = PAYLOADS.get(name)
    try:
        payload = builder(ctx) if builder else None
        if builder and payload is None:
            return "not_applicable", f"not a {ctx.kind} command"
        result = api.call(session, name, payload)
        ctx.responses[name] = result["response"]
        return "ok", result["response_type"]
    except api.ApiCallError as exc:
        if exc.status in ("AS_UNHANDLED", "unknown_command") or "no handler available" in str(exc):
            return "unhandled", str(exc)
        if exc.status in ("AS_BAD_REQUEST", "bad_request"):
            return "bad_request", str(exc)
        return "error", f"{exc.status}: {exc}"
    except Exception as exc:  # noqa: BLE001 - a sweep records every failure and keeps going
        return "error", f"{type(exc).__name__}: {exc}"


def ordered(commands: list[str]) -> list[str]:
    """Everything else first (sorted), then the session-changing commands in LAST order."""
    last = [name for name in LAST if name in commands]
    return sorted(name for name in commands if name not in last) + last


def _pass(factory, documents: dict[str, Path], commands: list[str], results: dict[str, dict], label: str) -> None:
    """Run ``commands`` on a session of each document; restart whenever a command leaves it unhealthy."""
    for kind, document in documents.items():
        session = factory(document).start()
        ctx = Context(session)
        try:
            for name in commands:
                if name in SKIP:
                    results[name][f"{kind}{label}"] = ("skipped", SKIP[name])
                    continue
                results[name][f"{kind}{label}"] = _attempt(session, ctx, name)
                for follow, payload in AFTER.get(name, []):
                    with contextlib.suppress(api.ApiCallError):
                        api.call(session, follow, payload)
                if name in RESTART_AFTER or not session.healthy():
                    # The command ended the server or closed its document (CloseAllDocuments, ...):
                    # later commands get a fresh session, so one command never masks another.
                    session.close(discard=True)
                    session = factory(document).start()
                    ctx = Context(session)
        finally:
            session.close(discard=True)


def _open_library_item(board: Path) -> tuple[str, str]:
    """OpenLibraryItem is served by an open library editor; in the GUI no API command opens one, so the
    footprint editor is opened the way a person would (the board editor's Tools menu) first."""
    from kicad_mcp.coverage.live_menus import _open

    session, _frame, _canvas = _open("fpedit", board, [])
    try:
        library = _lib_id(Context(session), "footprint")
        api.call(session, "OpenLibraryItem", {"type": "DOCTYPE_FOOTPRINT", "identifier": library})
        time.sleep(2)
        request = api.build_request(session.schema, "GetOpenDocuments", {"type": "DOCTYPE_FOOTPRINT"})
        names = [d.lib_id.entry_name for d in session.client.send(request).documents]
        if library["entry_name"] in names:
            return "ok", f"opened {library['entry_name']} in the footprint editor"
        return "error", f"footprint editor shows {names}, not {library['entry_name']}"
    except Exception as exc:  # noqa: BLE001
        return "error", f"{type(exc).__name__}: {exc}"
    finally:
        session.close(discard=True)


def sweep(inventory: dict, project: Path, gui: bool = True) -> dict:
    commands = ordered([c["request"] for c in inventory["api_commands"] if c["status"] == "handled"])
    documents = {"sch": next(project.glob("*.kicad_sch")), "pcb": next(project.glob("*.kicad_pcb"))}
    results: dict[str, dict] = {name: {} for name in commands}
    _pass(HeadlessSession, documents, commands, results, "")
    if gui:
        # Whatever did not run headless gets the real editor on a private desktop (GUI-only commands).
        from kicad_mcp.ipc.gui_session import GuiSession

        remaining = [n for n in commands if not any(o == "ok" for o, _ in results[n].values())]
        first = [n for n in GUI_ORDER if n in remaining]
        last = [n for n in GUI_LAST if n in remaining]
        middle = [n for n in remaining if n not in first and n not in last]
        _pass(GuiSession, documents, first + middle + last, results, "-gui")
        if "OpenLibraryItem" in results and not any(o == "ok" for o, _ in results["OpenLibraryItem"].values()):
            results["OpenLibraryItem"]["fpedit-gui"] = _open_library_item(documents["pcb"])

    rows, passed = [], []
    for name, by_kind in results.items():
        outcomes = {kind: outcome for kind, (outcome, _) in by_kind.items()}
        best = next((o for o in ("ok", "bad_request", "error", "skipped", "unhandled", "not_applicable")
                     if o in outcomes.values()), "unhandled")
        rows.append({"command": name, "outcome": best,
                     "detail": {kind: detail for kind, (_, detail) in by_kind.items()}})
        if best == "ok":
            passed.append(f"api:{name}")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": sorted(passed), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True, help="folder with a .kicad_sch and .kicad_pcb")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    try:
        result = sweep(inventory, args.project)
    except SessionError as exc:
        raise SystemExit(str(exc)) from None
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
