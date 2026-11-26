"""Typed MCP surface for the shared BoardTooling workflow."""
from __future__ import annotations

from typing import Any, Literal
from uuid import UUID
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, FiniteFloat

from kicad_mcp.utils import boardtooling as shared
from kicad_mcp.utils.workflow_jobs import WorkflowJobs


class PlacementMove(BaseModel):
    footprint_uuid: UUID
    dx_mm: FiniteFloat
    dy_mm: FiniteFloat


def register_workflow_tools(mcp: FastMCP, jobs: WorkflowJobs | None = None) -> None:
    def start_job(operation: str, arguments: dict) -> dict[str, Any]:
        if jobs is None:
            raise RuntimeError("Durable workflow jobs require the managed stdio server")
        return jobs.start(operation, arguments)

    @mcp.tool()
    def boardtooling_capabilities() -> dict[str, Any]:
        """Report installed identities and the shared expert design contract."""
        return shared.capabilities()

    @mcp.tool()
    def inspect_board_bundle(project_path: str) -> dict[str, Any]:
        """Inspect the complete exact project bundle and return its revision digest."""
        return shared.inspect_bundle(project_path)

    @mcp.tool()
    def inspect_board_courtyards(project_path: str) -> dict[str, Any]:
        """Read-only structural courtyard inventory for research, even with incomplete 3D libraries.

        Returns conservative graphic bounding boxes and board SHA256; never acceptance evidence.
        Complete bundle, placement, gate and release requirements are unchanged.
        """
        return shared.inspect_courtyards(project_path)

    @mcp.tool()
    def inspect_library_courtyards(footprint_paths: list[str]) -> dict[str, Any]:
        """Read exact standalone .kicad_mod courtyard boxes and hashes for research.

        Does not change boards, validate pin mapping, or provide acceptance evidence.
        """
        from kicad_mcp.courtyard_research import library_inventory
        return library_inventory(footprint_paths, shared._bundle()._sexpr)

    @mcp.tool()
    def board_design_profiles() -> dict[str, Any]:
        """Read the maintained expert profile and rule catalog."""
        return shared.design_profiles()

    @mcp.tool()
    def create_board_contract_draft(project_path: str, expected_bundle_digest: str,
                                    profile: str, variant: str) -> dict[str, Any]:
        """Create only a draft contract on the exact current bundle and variant."""
        return shared.create_contract_draft(project_path, expected_bundle_digest, profile, variant)

    @mcp.tool()
    def evaluate_board_contract(project_path: str, expected_bundle_digest: str,
                                gate_report_path: str | None = None,
                                attestation_paths: list[str] | None = None) -> dict[str, Any]:
        """Run the maintained contract evaluator and return source-bound requirements."""
        return shared.evaluate_contract(project_path, expected_bundle_digest,
                                        gate_report_path, attestation_paths)

    @mcp.tool()
    def plan_board_placement(project_path: str, expected_bundle_digest: str,
                             contract_sha256: str, variant: str,
                             operation: Literal["inspect_placement", "plan_placement"],
                             moves: list[PlacementMove]) -> dict[str, Any]:
        """Ask the shared contract API for a typed, exact-UUID movement plan; no apply."""
        return shared.plan_placement(project_path, expected_bundle_digest,
                                     contract_sha256, variant, operation,
                                     [move.model_dump(mode="json") for move in moves])

    @mcp.tool()
    def evaluate_board_gates(project_path: str, expected_bundle_digest: str,
                             variant: str, rules_path: str,
                             evidence_dir: str) -> dict[str, Any]:
        """Synchronous compatibility gate sweep; use start_board_gate_job for durable long work."""
        return shared.evaluate_gates(project_path, expected_bundle_digest,
                                     variant, rules_path, evidence_dir)

    @mcp.tool()
    def query_board_scene(project_path: str, expected_bundle_digest: str,
                          bbox_mm: list[float], margin_mm: float = 0.5) -> dict[str, Any]:
        """Synchronous read-only scene query; use start_board_scene_job for durable work."""
        return shared.query_scene(project_path, expected_bundle_digest, bbox_mm, margin_mm)

    @mcp.tool()
    def board_release_preview(project_path: str, expected_bundle_digest: str,
                              variant: str, fab: str, output_dir: str,
                              gate_report_path: str, attestation_paths: list[str],
                              rules_path: str | None = None,
                              fab_notes: str | None = None,
                              assembly: bool = False) -> dict[str, Any]:
        """Synchronous release preview; use start_board_release_preview_job for durable work."""
        return shared.release(project_path, expected_bundle_digest, variant, fab,
                              output_dir, rules_path=rules_path, fab_notes=fab_notes,
                              assembly=assembly, gate_report=gate_report_path,
                              attestation_paths=attestation_paths)

    @mcp.tool()
    def board_release_finalize(project_path: str, expected_bundle_digest: str,
                               variant: str, fab: str, preview_dir: str,
                               visual_review: str, evidence_dir: str) -> dict[str, Any]:
        """Synchronous finalization; use start_board_release_finalize_job for durable work."""
        return shared.release(project_path, expected_bundle_digest, variant, fab,
                              evidence_dir, finalize_preview=preview_dir,
                              visual_review=visual_review)

    @mcp.tool()
    def native_board_interaction_capability(operation: str) -> dict[str, Any]:
        """Whether an interactive board operation is supported, and through which route.

        footprint_drag / edge_move (alias edge_shove): supported by the adaptive engine (adaptive_* tools,
        file level) and the AdaptiveEditor IPC plugin (in the PCB Editor). canvas_mouse_tracking: not
        supported (needs native pcbnew code). Anything else: not supported."""
        from kicad_mcp.tools.adaptive_tools import capability_report
        aliases = {"edge_shove": "edge_move", "edge_drag": "edge_move", "board_edge_move": "edge_move",
                   "footprint_shove": "footprint_drag"}
        report = capability_report(probe_engine=False)
        op = aliases.get(operation, operation)
        entry = report["operations"].get(op)
        if entry is None:
            return {"supported": False, "operation": operation,
                    "reason": "unknown interactive operation", "known": sorted(report["operations"])}
        out = {"operation": operation, **entry}
        if entry["supported"] and not (report["engine_root"] and report["kicad_python"]):
            out.update(supported=False, reason="adaptive engine or KiCad python.exe not found",
                       engine_root=report["engine_root"], kicad_python=report["kicad_python"])
        return out

    @mcp.tool()
    def start_board_bundle_job(project_path: str) -> dict[str, Any]:
        """Queue an exact bundle inspection with a durable request and result."""
        return start_job("inspect_bundle", {"project": project_path})

    @mcp.tool()
    def start_board_contract_draft_job(project_path: str, expected_bundle_digest: str,
                                       profile: str, variant: str) -> dict[str, Any]:
        """Queue draft-only contract authoring against the expected source revision."""
        return start_job("create_contract_draft", {"project": project_path,
            "expected_digest": expected_bundle_digest, "profile": profile, "variant": variant})

    @mcp.tool()
    def start_board_contract_evaluation_job(project_path: str, expected_bundle_digest: str,
                                             gate_report_path: str | None = None,
                                             attestation_paths: list[str] | None = None) -> dict[str, Any]:
        """Queue source-bound shared contract evaluation without claiming design acceptance."""
        return start_job("evaluate_contract", {"project": project_path,
            "expected_digest": expected_bundle_digest, "gate_report": gate_report_path,
            "attestation_paths": attestation_paths or []})

    @mcp.tool()
    def start_board_placement_plan_job(project_path: str, expected_bundle_digest: str,
                                        contract_sha256: str, variant: str,
                                        operation: Literal["inspect_placement", "plan_placement"],
                                        moves: list[PlacementMove]) -> dict[str, Any]:
        """Queue typed UUID/mm planning; native application remains unavailable."""
        return start_job("plan_placement", {"project": project_path,
            "expected_digest": expected_bundle_digest, "contract_sha256": contract_sha256,
            "variant": variant, "operation": operation,
            "moves": [move.model_dump(mode="json") for move in moves]})

    @mcp.tool()
    def start_board_gate_job(project_path: str, expected_bundle_digest: str,
                             variant: str, rules_path: str) -> dict[str, Any]:
        """Queue the full shared gate sweep into this job's unique evidence directory."""
        return start_job("evaluate_gates", {"project": project_path,
            "expected_digest": expected_bundle_digest, "variant": variant,
            "rules_path": rules_path})

    @mcp.tool()
    def start_board_scene_job(project_path: str, expected_bundle_digest: str,
                              bbox_mm: list[FiniteFloat], margin_mm: FiniteFloat = 0.5) -> dict[str, Any]:
        """Queue a read-only native scene query for the exact project revision."""
        return start_job("query_scene", {"project": project_path,
            "expected_digest": expected_bundle_digest, "bbox_mm": bbox_mm,
            "margin_mm": margin_mm})

    @mcp.tool()
    def start_board_release_preview_job(project_path: str, expected_bundle_digest: str,
                                         variant: str, fab: Literal["jlcpcb", "pcbway", "generic"],
                                         gate_report_path: str, attestation_paths: list[str],
                                         rules_path: str | None = None,
                                         fab_notes: str | None = None,
                                         assembly: bool = False) -> dict[str, Any]:
        """Queue strict contract-bound release preview in a unique job directory."""
        return start_job("release_preview", {"project": project_path,
            "expected_digest": expected_bundle_digest, "variant": variant, "fab": fab,
            "rules_path": rules_path, "fab_notes": fab_notes, "assembly": assembly,
            "gate_report": gate_report_path, "attestation_paths": attestation_paths})

    @mcp.tool()
    def start_board_release_finalize_job(project_path: str, expected_bundle_digest: str,
                                          variant: str, fab: Literal["jlcpcb", "pcbway", "generic"],
                                          preview_dir: str, visual_review: str) -> dict[str, Any]:
        """Queue finalization of a matching preview with a new attempt directory."""
        return start_job("release_finalize", {"project": project_path,
            "expected_digest": expected_bundle_digest, "variant": variant, "fab": fab,
            "preview_dir": preview_dir, "visual_review": visual_review})

    @mcp.tool()
    def board_workflow_job_status(job_id: UUID) -> dict[str, Any]:
        """Read durable status and result across MCP sessions without adopting work."""
        if jobs is None:
            raise RuntimeError("Durable workflow jobs require the managed stdio server")
        return jobs.status(str(job_id))

    @mcp.tool()
    def cancel_board_workflow_job(job_id: UUID) -> dict[str, Any]:
        """Cancel only a job owned by this exact server session."""
        if jobs is None:
            raise RuntimeError("Durable workflow jobs require the managed stdio server")
        return jobs.cancel(str(job_id))
