"""Generate docs/OPERATION-MATRIX.md (F01): every KiCad operation this server exposes, by domain.

kicad-cli rows come from the typed registry (kicad_mcp/data/kicad-cli-10.0.5-tree.json) and the run-level
status in kicad_mcp/tools/cli_tools.py; the other routes come from ROUTES below, each citing its evidence.
Operations in the review's domains that no route covers are listed as `unavailable`, never omitted.

    python scripts/gen_operation_matrix.py          # rewrite the doc
    python scripts/gen_operation_matrix.py --check  # exit 1 if the doc is stale (used by the tests)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from kicad_mcp.tools import cli_tools  # noqa: E402
from kicad_mcp.utils import cli_registry  # noqa: E402

DOC = ROOT / "docs" / "OPERATION-MATRIX.md"

DOMAINS = ("Project/template/variant", "Schematic", "PCB edit", "Routing/tuning", "Zones/outline",
           "Settings/stackup/DRU", "Libraries", "Import/export/manufacturing", "ERC/DRC/parity", "Jobs/cancel")


def cli_domain(name: str) -> str:
    if name in ("sch erc", "pcb drc"):
        return "ERC/DRC/parity"
    if name.startswith(("fp ", "sym ")):
        return "Libraries"
    if name.startswith("jobset"):
        return "Jobs/cancel"
    if name in ("version",) or name.endswith(" upgrade"):
        return "Project/template/variant"
    return "Import/export/manufacturing"


def cli_mutation(cmd) -> str:
    if cmd.classification == "read_only":
        return "writes a new output file/dir only" if cmd.output_kind in ("file", "dir") else "none"
    return f"{cmd.classification}: needs allow_mutation, verified backup first"


# (domain, operation, route/tool, typed schema, mutation boundary, status, evidence)
ROUTES = [
    ("Project/template/variant", "bundle inspect (exact source digest)", "inspect_board_bundle / start_board_bundle_job",
     "typed", "none", "supported", "tests/test_boardtooling_workflow.py"),
    ("Project/template/variant", "contract draft / variant binding", "create_board_contract_draft",
     "typed", "writes a draft contract only", "supported", "tests/test_boardtooling_workflow.py"),
    ("Project/template/variant", "project create from template", "-", "-", "-", "unavailable",
     "no typed tool; kicad-cli has no template command"),
    ("Schematic", "symbol / wire / net / annotation editing", "-", "-", "-", "unavailable",
     "KiCad 10.0.5 exposes no schematic editing through kicad-cli or a typed tool here"),
    ("Schematic", "PCB synchronisation (update PCB from schematic)", "-", "-", "-", "unavailable",
     "GUI-only in 10.0.5; not in the L4 registry"),
    ("PCB edit", "scene query (items, positions)", "query_board_scene / start_board_scene_job",
     "typed", "none", "supported", "tests/test_boardtooling_workflow.py"),
    ("PCB edit", "placement plan (contract-checked)", "plan_board_placement", "typed", "none (plan only)",
     "supported", "tests/test_boardtooling_workflow.py"),
    ("PCB edit", "adaptive footprint drag (cascade/explicit, frozen, priority, eligibility, stop-at-limit)",
     "adaptive_plan/preview/apply/undo", "typed (ADAPTIVE-ENGINE-SPEC s3)",
     "board replaced only by adaptive_apply (sha256-guarded, backup; adaptive_undo restores)", "supported",
     "tests/test_adaptive_tools.py live round trip; 235 engine/adapter/installer tests (kicad_mcp/adaptive_editor/tests)"),
    ("PCB edit", "adaptive drag inside the running PCB Editor", "AdaptiveEditor IPC plugin 'Adaptive Drag'",
     "panel controls", "one KiCad commit = one undo step", "supported",
     "kicad_mcp/adaptive_editor/tests_live/run_plugin_live.py"),
    ("PCB edit", "live canvas cursor-tracking adaptation", "-", "-", "-", "unavailable",
     "10.0.5 IPC does not render uncommitted changes (probe_commit_visibility.py); needs native code"),
    ("PCB edit", "open and save board in pcbnew (UI fallback)", "l4_run pcb_open_and_save", "typed (registry)",
     "clone only; original byte-identical", "supported",
     "live-verified (tests/test_l4.py); registry approved by kicad_mcp/l4/registry/APPROVED.sha256"),
    ("Routing/tuning", "copper leg re-aim during adaptive moves (no rerouting)", "adaptive_* / plugin",
     "typed", "as adaptive_apply", "supported", "engine copper tests; run_plugin_live.py (2 legs re-aimed)"),
    ("Routing/tuning", "tuned chains (serpentines) translate rigidly", "adaptive_* / plugin", "typed",
     "as adaptive_apply", "supported", "engine tests (docs/adaptive-editor/ADAPTIVE-ENGINE-SPEC.md s4)"),
    ("Routing/tuning", "ordinary routing / differential pairs / length tuning", "-", "-", "-", "unavailable",
     "no router exposed; kicad-cli has none"),
    ("Zones/outline", "adaptive board-edge move (grow/shrink, pushes parts)", "adaptive_* (edge driver) / plugin "
     "'Adaptive Edge Move'", "typed", "as adaptive_apply / one KiCad commit", "supported",
     "run_plugin_live.py (edge to x=148 mm, one undo restores)"),
    ("Zones/outline", "zone refill", "adaptive_preview (candidate refill); plugin apply(refill=True)", "typed",
     "candidate copy / separate KiCad operation", "supported", "kicad_mcp/adaptive_editor/tests/test_adapter_session.py"),
    ("Zones/outline", "zone / teardrop / outline authoring", "-", "-", "-", "unavailable", "no typed tool"),
    ("Settings/stackup/DRU", "read rules/netclasses (bundle capture)", "inspect_board_bundle", "typed", "none",
     "supported", "tests/test_boardtooling_workflow.py"),
    ("Settings/stackup/DRU", "read copper-layer count via Board Setup (UI fallback)",
     "l4_run pcb_board_setup_read", "typed (registry)", "none (read-only, Cancel)", "supported",
     "live-verified (tests/test_l4.py: selects the Physical Stackup page, reads 2 on a 2-layer board); "
     "registry approved by kicad_mcp/l4/registry/APPROVED.sha256"),
    ("Settings/stackup/DRU", "edit stackup / custom rules", "-", "-", "-", "unavailable", "no typed tool"),
    ("Import/export/manufacturing", "Gerber export through the Plot dialog (UI fallback)",
     "l4_run pcb_export_gerbers_dialog", "typed (registry)", "clone only", "supported",
     "live-verified (tests/test_l4.py, 9 Gerbers); registry approved by kicad_mcp/l4/registry/APPROVED.sha256"),
    ("Import/export/manufacturing", "release preview / finalize", "board_release_preview / board_release_finalize",
     "typed", "evidence dir only", "supported", "tests/test_boardtooling_workflow.py"),
    ("ERC/DRC/parity", "contract gate evaluation", "evaluate_board_gates / evaluate_board_contract", "typed",
     "none", "supported", "tests/test_boardtooling_workflow.py"),
    ("ERC/DRC/parity", "DRC identity comparison before/after an edit", "adaptive_preview", "typed",
     "candidate copy only", "supported", "kicad_mcp/adaptive_editor/tests/test_adapter_session.py"),
    ("Jobs/cancel", "async jobs with status and cancel", "start_*_job / board_workflow_job_status / "
     "cancel_board_workflow_job", "typed", "job dir only", "supported", "tests/test_workflow_jobs.py"),
    ("Jobs/cancel", "capability reports", "kicad_capability_report / adaptive_capability / "
     "native_board_interaction_capability", "typed", "none", "supported", "tests/test_full_capability_tools.py"),
    ("Libraries", "symbol / footprint / 3D library editing", "-", "-", "-", "unavailable",
     "only the kicad-cli fp/sym commands below"),
]


def rows():
    out = []
    for cmd in cli_registry.list_commands():
        status, reason = cli_tools.command_run_status(cmd.name)
        evidence = reason or ("integration-tested subset / registry-validated (tests/test_cli_tools.py, "
                              "tests/test_full_capability_tools.py)")
        out.append((cli_domain(cmd.name), f"`kicad-cli {cmd.name}` - {cmd.summary}", "kicad_cli_run",
                    "typed (registry options)", cli_mutation(cmd), status, evidence))
    out.extend(ROUTES)
    order = {d: i for i, d in enumerate(DOMAINS)}
    return sorted(out, key=lambda r: (order[r[0]], r[5] != "supported", r[1]))


def render() -> str:
    rs = rows()
    counts: dict[str, int] = {}
    for r in rs:
        counts[r[5]] = counts.get(r[5], 0) + 1
    lines = ["# KiCad 10.0.5 operation matrix (F01)", "",
             "Generated by `scripts/gen_operation_matrix.py`; do not edit by hand (`--check` detects drift).",
             "Status words: `supported` (typed route with test evidence), `callable_unverified`, `blocked`",
             "(route exists but is refused until a stated condition), `unsupported`, `unavailable` (no route).",
             "Evidence cites the tests or live runs that back the row; enumerating an operation is not a claim",
             "that every option of it was exercised.", "",
             "Totals: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" ({len(rs)} rows).", ""]
    for domain in DOMAINS:
        lines += [f"## {domain}", "", "| Operation | Route / tool | Schema | Mutation boundary | Status | Evidence |",
                  "|---|---|---|---|---|---|"]
        for r in (r for r in rs if r[0] == domain):
            lines.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r[1:]) + " |")
        lines.append("")
    return "\n".join(lines)


def main(argv) -> int:
    text = render()
    if "--check" in argv:
        current = DOC.read_text(encoding="utf-8") if DOC.is_file() else ""
        if current != text:
            print("docs/OPERATION-MATRIX.md is stale; run scripts/gen_operation_matrix.py")
            return 1
        print("up to date")
        return 0
    DOC.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {DOC} ({len(rows())} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
