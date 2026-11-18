"""
Analysis and validation tools for KiCad projects.
"""
import os
from typing import Dict, Any
from mcp.server.fastmcp import FastMCP

from kicad_mcp.utils.file_utils import get_project_files


def register_analysis_tools(mcp: FastMCP) -> None:
    """Register analysis and validation tools with the MCP server.
    
    Args:
        mcp: The FastMCP server instance
    """
    
    @mcp.tool()
    def validate_project(project_path: str) -> Dict[str, Any]:
        """Structural file/JSON check only. Never design, DRC, or release signoff."""
        if not os.path.exists(project_path):
            return {"valid": False, "scope": "structural_only", "design_signoff": False,
                    "error": f"Project not found: {project_path}"}
        
        issues = []
        files = get_project_files(project_path)
        
        # Check for essential files
        if "pcb" not in files:
            issues.append("Missing PCB layout file")
        
        if "schematic" not in files:
            issues.append("Missing schematic file")
        
        # Validate project file
        try:
            with open(project_path, 'r') as f:
                import json
                json.load(f)
        except json.JSONDecodeError:
            issues.append("Invalid project file format (JSON parsing error)")
        except Exception as e:
            issues.append(f"Error reading project file: {str(e)}")
        
        return {
            "valid": len(issues) == 0,
            "scope": "structural_only",
            "design_signoff": False,
            "path": project_path,
            "issues": issues if issues else None,
            "files_found": list(files.keys())
        }

