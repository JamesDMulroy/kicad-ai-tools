"""MCP tools for the L4 isolated UI broker (docs/L4-UI-BROKER-SPEC.md).

Three tools: ``l4_operations`` (registry facts and runnable operations), ``l4_run`` (run one registered
operation on a clone of a project, on a private desktop) and ``l4_record_identifiers`` (record UI
identifiers over three launches; writes only into the evidence directory). Callers supply an operation
name, typed values and a project directory; they can never name a dialog, control, menu or coordinate.

All three refuse unless the registry's sha256 is in KICAD_MCP_L4_APPROVED_REGISTRY_SHA256 (owner
approval; ``l4_record_identifiers`` does not need one because it only observes and writes a candidate).
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from kicad_mcp.l4 import broker


def run_l4_operations(registry_path: str | None = None) -> dict[str, Any]:
    """Registry facts (sha256, approval state, schema validity) and, if approved, the operations."""
    return broker.list_operations(registry_path)


def run_l4_run(operation: str, project_dir: str, params: dict[str, Any] | None = None,
               **overrides: Any) -> dict[str, Any]:
    """Run one registered operation. ``overrides`` (registry_path, evidence_root, ...) exist for tests only."""
    return broker.ui_fallback(operation, project_dir, params, **overrides)


def run_l4_record_identifiers(project_dir: str, **overrides: Any) -> dict[str, Any]:
    """Record identifiers over three launches. ``overrides`` exist for tests only and are not MCP parameters."""
    return broker.record_identifiers(project_dir, **overrides)


def register_l4_tools(mcp: FastMCP) -> None:
    @mcp.tool()
    def l4_operations() -> dict[str, Any]:
        """List the L4 UI-fallback operations the closed registry allows, with their typed parameters.

        Reports the registry's sha256 and whether it is owner-approved (env
        KICAD_MCP_L4_APPROVED_REGISTRY_SHA256). An unapproved, unreadable or invalid registry yields
        ``status: refused`` and no operations. Launches nothing.
        """
        return run_l4_operations()

    @mcp.tool()
    def l4_run(operation: str, project_dir: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one registered L4 operation on a task-owned CLONE of ``project_dir`` (absolute, synthetic projects only).

        pcbnew runs on a private Win32 desktop (never the owner's), driven only through UI Automation
        patterns, under a monitor that kills it if any of its windows reaches the user's desktop. The
        original project is never touched (its hashes are compared before and after). Unknown operations,
        unknown or ill-typed parameters, an unapproved registry sha256 or a KiCad build different from the
        registry's are refused before anything is launched. Blocks until the run ends (minutes); the
        evidence directory is always kept and reported.
        """
        return run_l4_run(operation, project_dir, params)

    @mcp.tool()
    def l4_record_identifiers(project_dir: str) -> dict[str, Any]:
        """Record UI identifiers for the initial operations over three independent hidden-desktop launches.

        An identifier is admitted only if it resolves to the same control in all three launches; unstable
        ones are dropped and reported. The candidate registry and its sha256 are written to the evidence
        directory for owner approval; nothing is written over the shipped registry and nothing is approved
        by this call. Blocks for minutes.
        """
        return run_l4_record_identifiers(project_dir)
