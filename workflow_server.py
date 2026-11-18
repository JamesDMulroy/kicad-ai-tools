"""Compatibility shim: the server lives in ``kicad_mcp.workflow_server`` (run ``python -m kicad_mcp.workflow_server``)."""
from kicad_mcp.workflow_server import *  # noqa: F401,F403
from kicad_mcp.workflow_server import create_server, main  # noqa: F401

if __name__ == "__main__":
    main()
