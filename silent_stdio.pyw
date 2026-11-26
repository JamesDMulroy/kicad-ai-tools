"""Windows entry point for the MCP stdio server from a source checkout, under pythonw.exe (no console).

Equivalent to ``pythonw.exe -m kicad_mcp.launcher`` for an installed package; ``python -m kicad_mcp.setup``
prints the exact client entry for this machine. The launcher starts the real server only while the
session is using it; the MCP client closes stdin to end this process.
"""
import os
import sys
from pathlib import Path

if __name__ == "__main__":
    if os.name != "nt" or Path(sys.executable).name.lower() != "pythonw.exe":
        raise SystemExit("Run this with pythonw.exe (or use: python -m kicad_mcp.launcher)")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from kicad_mcp.launcher import pythonw_main
    raise SystemExit(pythonw_main())
