"""
Configuration settings for the KiCad MCP server.

KiCad locations come from kicad_env (environment, the per-machine config written by
``python -m kicad_mcp.setup``, then platform discovery). Nothing is guessed, and a configuration
problem never breaks this import: it is logged and kept in KICAD_CONFIG_ERROR.

Module Variables:
    KICAD_USER_DIR (str): User's KiCad documents directory
    KICAD_APP_PATH (str): KiCad application installation path
    KICAD_EXTENSIONS (Dict[str, str]): KiCad file extension mappings
    TIMEOUT_CONSTANTS (Dict[str, float]): Operation timeout values in seconds

Platform Support:
    - Windows: discovery from KiCad's uninstall registry entries, then PATH (tested)
    - macOS / Linux: kicad-cli on PATH only (untested stubs); no guessed install folders

Dependencies:
    - os: File system operations and environment variables
"""

import os

# Platform-specific KiCad installation and user directory paths
# These paths are used for finding KiCad resources and user projects
from kicad_mcp.adaptive_editor import kicad_env as _kicad_env

# KiCad's install root and the user's KiCad documents folder come from kicad_env: environment
# (KICAD_APP_PATH / KICAD_USER_DIR), the per-machine config written by `python -m kicad_mcp.setup`, then
# platform discovery. Nothing is guessed. Problems are logged once with the action to take; the
# package still imports, and KiCad-backed tools report the same error when called.
import logging as _logging

KICAD_CONFIG_ERROR = ""


def _safe(get, default):
    """Resolve one setting; a bad per-machine config is logged once and never breaks this import."""
    global KICAD_CONFIG_ERROR
    try:
        return get()
    except Exception as exc:  # KiCadNotFound, ConfiguredPathError, ConfigError
        message = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
        if message != KICAD_CONFIG_ERROR:
            KICAD_CONFIG_ERROR = message
            _logging.getLogger(__name__).warning("KiCad configuration problem: %s", message)
        return default


KICAD_APP_PATH = _safe(lambda: str(_kicad_env.kicad_app_root()), "")
KICAD_USER_DIR = _safe(lambda: str(_kicad_env.setting("kicad_user_dir", "KICAD_USER_DIR")
                                   or (_kicad_env.documents_dir() / "KiCad")),
                       os.environ.get("KICAD_USER_DIR", ""))


# KiCad file extension mappings for project file identification
# Used by file discovery and validation functions
KICAD_EXTENSIONS = {
    "project": ".kicad_pro",
    "pcb": ".kicad_pcb",
    "schematic": ".kicad_sch",
    "design_rules": ".kicad_dru",
    "worksheet": ".kicad_wks",
    "footprint": ".kicad_mod",
    "netlist": "_netlist.net",
    "kibot_config": ".kibot.yaml",
}

# Operation timeout values in seconds for external process management
# Prevents hanging operations and provides user feedback
TIMEOUT_CONSTANTS = {
    "kicad_cli_version_check": 10.0,  # Timeout for KiCad CLI version checks
    "kicad_cli_export": 30.0,  # Timeout for KiCad CLI export operations
    "application_open": 10.0,  # Timeout for opening applications (e.g., KiCad)
    "subprocess_default": 30.0,  # Default timeout for subprocess operations
}
