"""Coverage gate: every function in a KiCad build's inventory must be reachable AND verified live.

Each inventory item gets exactly one state:

- ``verified``   a route reaches it and a live test passed against this exact KiCad commit;
- ``unverified`` a route reaches it, but no passing live evidence for this commit yet;
- ``missing``    no route reaches it (for an API command KiCad declares but does not implement, the
                 route is an upstream patch, so it is reported as ``upstream_gap``).

The gate passes only when every item is ``verified``. A new KiCad release therefore fails the gate
until each function it added has a route and a passing live test; nothing can be listed as
"out of scope".

Routes are families, not hand-kept per-item lists, so they follow the inventory automatically:

- ``api``    every handled API command, through the schema-validated generic API tool;
- ``action`` every action RunAction can run by name (no parameter, not an interactive tool);
- ``cli``    every kicad-cli command, through the typed CLI tool.

Actions that need a parameter or are interactive have no route until a typed tool (or an upstream
RunAction-with-parameters patch) covers them; TYPED_ROUTES lists those as they are built.

Evidence is a JSON file written by the live test run: {"commit": ..., "passed": [item ids]}.

An ``upstream_gap`` action stays a gap (it does nothing in KiCad itself). When the function it names is covered
by another MCP tool, the evidence's optional ``function_covered`` map {item id: "tool + proof"} is carried to the
report as a note next to the gap; it never changes a state.

    python -m kicad_mcp.coverage.gate --inventory inv.json [--evidence ev.json] [--report out.md]
"""
from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

# Item id -> typed MCP tool that reaches it. Filled in as typed tools land; each needs live evidence.
TYPED_ROUTES: dict[str, str] = {}


def item_ids(inventory: dict) -> list[tuple[str, str]]:
    """(item id, family) for every function in the inventory."""
    items = [(f"api:{c['request']}", "api") for c in inventory["api_commands"]]
    items += [(f"action:{a['name']}", "action") for a in inventory["actions"]]
    items += [(f"cli:{c}", "cli") for c in inventory["cli_commands"]]
    items += [(f"program:{p}", "program") for p in inventory.get("programs", [])]
    return items


def typed_routes() -> dict[str, str]:
    """Interactive tools reached by the API command that makes their result (coverage.live_typed), or, where
    the frame has no API, by driving the tool on its canvas and reading the saved file back."""
    from kicad_mcp.coverage import live_typed

    routes = {f"action:{name}": "kicad_api_call" for name in (
        *live_typed.SCHEMATIC_ACTIONS, *live_typed.BOARD_ACTIONS,
        *live_typed.GUI_BOARD_ACTIONS, *live_typed.GUI_SCHEMATIC_ACTIONS)}
    # Interactive tools of frames with no API, driven on the canvas and read back from the saved file
    from kicad_mcp.coverage import live_pleditor

    routes.update({f"action:{live_pleditor.PREFIX}{case}": "tool_driver:live_pleditor" for case in live_pleditor.CASES})
    from kicad_mcp.coverage import live_statusbar

    routes.update({f"action:{name}": "tool_driver:live_statusbar" for name in live_statusbar.TOOL_CASES})
    from kicad_mcp.coverage import live_designblocks

    routes.update({f"action:{name}": "tool_driver:live_designblocks" for name in live_designblocks.CASES})
    from kicad_mcp.coverage import live_board_misc

    routes.update({f"action:{name}": "tool_driver:live_board_misc" for name in live_board_misc.TOOL_CASES})
    # API commands KiCad declares but does not implement: MCP tools that do what the proto describes
    from kicad_mcp.coverage import fallback_routes

    routes.update({f"api:{command}": f"fallback:{tool}" for command, tool in fallback_routes.ROUTES.items()})
    return {**routes, **TYPED_ROUTES}


def route_for(item: str, inventory_index: dict[str, dict]) -> str | None:
    typed = typed_routes()
    if item in typed:
        return f"typed:{typed[item]}"
    family, _, key = item.partition(":")
    entry = inventory_index.get(item, {})
    if family == "api":
        return "api:kicad_api_call" if entry.get("status") == "handled" else None
    if family == "action":
        return "action:kicad_run_action" if entry.get("runnable") else None
    if family == "cli":
        return "cli:kicad_cli_run"
    if family == "program":
        return "uia:program_session"  # driven through UI Automation on a private desktop (live_programs)
    return None


def evaluate(inventory: dict, evidence: dict | None) -> dict:
    commit = inventory["source_commit"]
    if evidence and evidence.get("commit") != commit:
        raise SystemExit(f"evidence is for commit {evidence.get('commit')}, inventory is {commit}")
    passed = set(evidence.get("passed", [])) if evidence else set()
    covered = dict(evidence.get("function_covered", {})) if evidence else {}
    reported_gaps = set(evidence.get("upstream_gaps", [])) if evidence else set()

    index: dict[str, dict] = {f"api:{c['request']}": c for c in inventory["api_commands"]}
    index.update({f"action:{a['name']}": a for a in inventory["actions"]})

    rows = []
    for item, family in item_ids(inventory):
        route = route_for(item, index)
        if route is None:
            upstream = family == "api" and index.get(item, {}).get("status") == "declared_only"
            state = "upstream_gap" if upstream else "missing"
        elif item in passed:
            state = "verified"
        else:
            # The build itself refuses it ("no longer supported"): a gap upstream, not ours to fake.
            state = "upstream_gap" if item in reported_gaps else "unverified"
        rows.append({"item": item, "family": family, "route": route, "state": state})
        if state == "upstream_gap" and item in covered:
            rows[-1]["function_covered"] = covered[item]

    states = collections.Counter(r["state"] for r in rows)
    by_family = collections.defaultdict(collections.Counter)
    for r in rows:
        by_family[r["family"]][r["state"]] += 1
    return {
        "kicad_version": inventory["kicad_version"],
        "commit": commit,
        "total": len(rows),
        "states": dict(states),
        "by_family": {k: dict(v) for k, v in by_family.items()},
        "passed": states["verified"] == len(rows),
        "rows": rows,
    }


def report_markdown(result: dict) -> str:
    lines = [f"# KiCad coverage: {result['kicad_version']} ({result['commit'][:10]})", "",
             f"Gate: **{'PASS' if result['passed'] else 'FAIL'}** — "
             f"{result['states'].get('verified', 0)} of {result['total']} functions verified live.", "",
             "| Family | verified | unverified | missing | upstream_gap |", "|---|---|---|---|---|"]
    for family, counts in sorted(result["by_family"].items()):
        lines.append(f"| {family} | " + " | ".join(str(counts.get(s, 0)) for s in
                                                    ("verified", "unverified", "missing", "upstream_gap"))
                     + " |")
    for state in ("missing", "upstream_gap"):
        items = [r["item"] for r in result["rows"] if r["state"] == state]
        if items:
            notes = {r["item"]: r.get("function_covered") for r in result["rows"] if r["state"] == state}
            lines += ["", f"## {state} ({len(items)})", ""] + [
                f"- `{i}`" + (f" — function covered: {notes[i]}" if notes.get(i) else "") for i in items]
    return "\n".join(lines) + "\n"


def merge_evidence(paths: list[Path]) -> dict:
    """One evidence record from several runs of the same KiCad commit (API sweep, action chunks, ...)."""
    records = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    commits = {r["commit"] for r in records}
    if len(commits) != 1:
        raise SystemExit(f"evidence from different KiCad commits: {sorted(commits)}")
    return {"commit": commits.pop(), "passed": sorted({i for r in records for i in r.get("passed", [])}),
            "upstream_gaps": sorted({i for r in records for i in r.get("upstream_gaps", [])}),
            "function_covered": {i: why for r in records for i, why in r.get("function_covered", {}).items()}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, nargs="*", default=[])
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    evidence = merge_evidence(args.evidence) if args.evidence else None
    result = evaluate(inventory, evidence)
    if args.report:
        args.report.write_text(report_markdown(result), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("kicad_version", "total", "states", "by_family", "passed")}))
    for r in result["rows"]:
        if r.get("function_covered"):
            print(f"{r['item']}: upstream_gap, function covered: {r['function_covered']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
