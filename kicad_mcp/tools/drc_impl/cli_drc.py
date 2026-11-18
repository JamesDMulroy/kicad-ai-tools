"""
Design Rule Check (DRC) implementation using KiCad command-line interface.
"""
import logging
import os
import json
import tempfile
from typing import Dict, Any, Optional
from mcp.server.fastmcp import Context

from kicad_mcp.utils.secure_subprocess import run_contained

log = logging.getLogger(__name__)  # never print(): stdout carries the MCP stdio protocol

async def run_drc_via_cli(pcb_file: str, ctx: Context = None) -> Dict[str, Any]:
    """Run DRC using KiCad command line tools.
    
    Args:
        pcb_file: Path to the PCB file (.kicad_pcb)
        ctx: MCP context for progress reporting
        
    Returns:
        Dictionary with DRC results
    """
    results = {
        "success": False,
        "method": "cli",
        "pcb_file": pcb_file
    }
    
    try:
        # Create a temporary directory for the output
        with tempfile.TemporaryDirectory() as temp_dir:
            # Output file for DRC report
            output_file = os.path.join(temp_dir, "drc_report.json")
            
            # Find kicad-cli executable
            kicad_cli, cli_error = resolve_kicad_cli()
            if not kicad_cli:
                log.warning("kicad-cli unavailable: %s", cli_error)
                results["error"] = f"kicad-cli unavailable: {cli_error}"
                return results
            
            # Report progress 
            if ctx:
                await ctx.report_progress(50, 100)
                await ctx.info("Running DRC using KiCad CLI...")
            
            # Build the DRC command
            cmd = [
                kicad_cli, 
                "pcb", 
                "drc",
                "--format", "json",
                "--output", output_file,
                pcb_file
            ]
            
            log.info("Running command: %s", " ".join(cmd))
            process = run_contained(cmd, timeout=600, text=True, encoding="utf-8", errors="replace")  # hidden, contained
            
            # Check if the command was successful
            if process.returncode != 0:
                log.warning("DRC command failed with code %s", process.returncode)
                log.warning("Error output: %s", process.stderr)
                results["error"] = f"DRC command failed: {process.stderr}"
                return results
            
            # Check if the output file was created
            if not os.path.exists(output_file):
                log.info("DRC report file not created")
                results["error"] = "DRC report file not created"
                return results
            
            # Read the DRC report
            with open(output_file, 'r') as f:
                try:
                    drc_report = json.load(f)
                except json.JSONDecodeError:
                    log.info("Failed to parse DRC report JSON")
                    results["error"] = "Failed to parse DRC report JSON"
                    return results
            
            # Process the DRC report
            violations = drc_report.get("violations", [])
            violation_count = len(violations)
            log.info("DRC completed with %d violations", violation_count)
            if ctx:
                await ctx.report_progress(70, 100)
                await ctx.info(f"DRC completed with {violation_count} violations")
            
            # Categorize violations by type
            error_types = {}
            for violation in violations:
                error_type = violation.get("message", "Unknown")
                if error_type not in error_types:
                    error_types[error_type] = 0
                error_types[error_type] += 1
            
            # Create success response
            results = {
                "success": True,
                "method": "cli",
                "pcb_file": pcb_file,
                "total_violations": violation_count,
                "violation_categories": error_types,
                "violations": violations
            }
            
            if ctx:
                await ctx.report_progress(90, 100)
            return results
            
    except Exception as e:
        logging.getLogger(__name__).error("Error in CLI DRC: %s", e, exc_info=True)
        results["error"] = f"Error in CLI DRC: {str(e)}"
        return results


def resolve_kicad_cli() -> tuple[Optional[str], Optional[str]]:
    """``(kicad-cli, None)`` from kicad_env.resolve_kicad_cli (env, per-machine config, discovery incl. PATH),
    else ``(None, reason)``. No fallback after an explicit setting or a config error: the reason names it."""
    from kicad_mcp.adaptive_editor import kicad_env
    cli, error = kicad_env.resolve_kicad_cli()
    if error:
        return None, error
    if not os.access(cli, os.X_OK):
        return None, f"kicad-cli at {cli} is not executable"
    return str(cli), None


def find_kicad_cli() -> Optional[str]:
    """kicad-cli, or None with the reason logged (see resolve_kicad_cli)."""
    cli, error = resolve_kicad_cli()
    if error:
        log.warning("kicad-cli unavailable: %s", error)
    return cli
