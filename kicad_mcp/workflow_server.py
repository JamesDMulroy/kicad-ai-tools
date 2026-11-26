"""The kicad-ai-tools MCP server (stdio). Normally started on demand by ``kicad_mcp.launcher``.

This server exposes only the BoardTooling workflow surface, the narrow,
read-only KiCad 10.0.5 capability/source/plugin-inventory tools, and the
registry-validated kicad-cli tools (kicad_cli_commands, kicad_cli_describe,
kicad_cli_run). kicad_cli_run runs real kicad-cli through the contained hidden
runner; its mutating commands need an explicit allow_mutation flag and make a
verified backup first. It also registers the L4 UI-broker tools (l4_operations,
l4_run, l4_record_identifiers): UI fallback on a private desktop for operations in
a closed, owner-approved registry; refused unless the registry sha256 is approved.
The adaptive tools (adaptive_capability/plan/preview/apply/undo) run the AdaptiveEditor
engine under KiCad's python.exe through the same contained hidden runner.
Optional legacy BOM/dataframe, project-discovery and
editing tools are not imported or enabled. This limits the client surface; it
is not an OS security boundary.
"""
import logging
import os
from mcp.server.fastmcp import FastMCP

from kicad_mcp.adaptive_editor import kicad_env

from kicad_mcp.tools.adaptive_tools import register_adaptive_tools
from kicad_mcp.tools.cli_tools import register_cli_tools
from kicad_mcp.tools.fallback_tools import register_fallback_tools
from kicad_mcp.tools.full_capability_tools import register_full_capability_tools
from kicad_mcp.tools.function_fallbacks import register_function_fallback_tools
from kicad_mcp.tools.ipc_tools import register_ipc_tools
from kicad_mcp.tools.l4_tools import register_l4_tools
from kicad_mcp.tools.workflow_tools import register_workflow_tools
from kicad_mcp.utils.secure_subprocess import close_owned_processes
from kicad_mcp.utils.workflow_jobs import WorkflowJobs
from kicad_mcp.tools.schematic_tools import register_schematic_tools


def create_server():
    server = FastMCP("kicad-ai-tools")
    jobs = WorkflowJobs()
    server._workflow_jobs = jobs
    register_workflow_tools(server, jobs)
    register_schematic_tools(server, jobs)
    register_full_capability_tools(server)
    register_cli_tools(server)
    register_l4_tools(server)
    register_adaptive_tools(server)
    register_ipc_tools(server)
    register_fallback_tools(server)
    register_function_fallback_tools(server)
    return server


def main():
    logging.basicConfig(level=logging.INFO)  # logging goes to stderr; stdout carries the MCP protocol
    try:
        cli = kicad_env.kicad_cli()
    except Exception as exc:  # not found, a wrong explicit setting, or an unreadable per-machine config
        # The server still starts: tools that need KiCad report this same, actionable error when called.
        logging.warning("KiCad unavailable (%s): %s", getattr(exc, "code", type(exc).__name__), exc)
    else:
        os.environ["PATH"] = str(cli.parent) + os.pathsep + os.environ.get("PATH", "")
    from kicad_mcp.utils.temp_cleanup import sweep_stale
    logging.info("Stale temporary cleanup: %s", sweep_stale())
    server = create_server()
    try:
        server.run(transport="stdio")
    finally:
        server._workflow_jobs.close()
        close_owned_processes()


if __name__ == "__main__":
    main()
