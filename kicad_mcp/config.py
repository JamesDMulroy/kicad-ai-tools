"""
Configuration settings for the KiCad MCP server.

KiCad locations come from kicad_env (environment, the per-machine config written by
``python -m kicad_mcp.setup``, then platform discovery). Nothing is guessed, and a configuration
problem never breaks this import: it is logged and kept in KICAD_CONFIG_ERROR.

Module Variables:
    system (str): Operating system name from platform.system()
    KICAD_USER_DIR (str): User's KiCad documents directory
    KICAD_APP_PATH (str): KiCad application installation path
    ADDITIONAL_SEARCH_PATHS (List[str]): Additional project search locations
    KICAD_PYTHON_BASE (str): KiCad Python framework base path (macOS only)
    KICAD_EXTENSIONS (Dict[str, str]): KiCad file extension mappings
    DATA_EXTENSIONS (List[str]): Recognized data file extensions
    CIRCUIT_DEFAULTS (Dict[str, Union[float, List[float]]]): Default circuit parameters
    COMMON_LIBRARIES (Dict[str, Dict[str, Dict[str, str]]]): Component library mappings
    DEFAULT_FOOTPRINTS (Dict[str, List[str]]): Default footprint suggestions per component
    TIMEOUT_CONSTANTS (Dict[str, float]): Operation timeout values in seconds
    PROGRESS_CONSTANTS (Dict[str, int]): Progress reporting percentage values
    DISPLAY_CONSTANTS (Dict[str, int]): UI display configuration values

Platform Support:
    - Windows: discovery from KiCad's uninstall registry entries, then PATH (tested)
    - macOS / Linux: kicad-cli on PATH only (untested stubs); no guessed install folders

Dependencies:
    - os: File system operations and environment variables
    - platform: Operating system detection
"""

import os
import platform

# Determine operating system for platform-specific configuration
# Returns 'Darwin' (macOS), 'Windows', 'Linux', or other
system = platform.system()

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

# Project search paths: KICAD_SEARCH_PATHS (comma-separated) or the per-machine config's
# "project_search_paths" list. No home-folder guesses are added.
ADDITIONAL_SEARCH_PATHS = []
_configured_paths = _safe(lambda: _kicad_env.setting("project_search_paths", "KICAD_SEARCH_PATHS"),
                          os.environ.get("KICAD_SEARCH_PATHS", "")) or []
if isinstance(_configured_paths, str):
    _configured_paths = _configured_paths.split(",")
for path in _configured_paths:
    expanded_path = os.path.expanduser(str(path).strip())
    if expanded_path and os.path.exists(expanded_path):  # only existing directories
        ADDITIONAL_SEARCH_PATHS.append(expanded_path)

# Base path to KiCad's Python framework for API access
# macOS bundles Python framework within the application
if system == "Darwin":  # macOS
    KICAD_PYTHON_BASE = os.path.join(
        KICAD_APP_PATH, "Contents/Frameworks/Python.framework/Versions"
    )
else:
    # Linux/Windows use system Python or require dynamic detection
    KICAD_PYTHON_BASE = ""  # Will be determined dynamically in python_path.py


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

# Additional data file extensions that may be part of KiCad projects
# Includes manufacturing files, component data, and export formats
DATA_EXTENSIONS = [
    ".csv",  # BOM or other data
    ".pos",  # Component position file
    ".net",  # Netlist files
    ".zip",  # Gerber files and other archives
    ".drl",  # Drill files
]

# Default parameters for circuit creation and component placement
# Values in mm unless otherwise specified, following KiCad conventions
CIRCUIT_DEFAULTS = {
    "grid_spacing": 1.0,  # Default grid spacing in mm for user coordinates
    "component_spacing": 10.16,  # Default component spacing in mm
    "wire_width": 6,  # Default wire width in KiCad units (0.006 inch)
    "text_size": [1.27, 1.27],  # Default text size in mm
    "pin_length": 2.54,  # Default pin length in mm
}

# Predefined component library mappings for quick circuit creation
# Maps common component types to their KiCad library and symbol names
# Organized by functional categories: basic, power, connectors
COMMON_LIBRARIES = {
    "basic": {
        "resistor": {"library": "Device", "symbol": "R"},
        "capacitor": {"library": "Device", "symbol": "C"},
        "inductor": {"library": "Device", "symbol": "L"},
        "led": {"library": "Device", "symbol": "LED"},
        "diode": {"library": "Device", "symbol": "D"},
    },
    "power": {
        "vcc": {"library": "power", "symbol": "VCC"},
        "gnd": {"library": "power", "symbol": "GND"},
        "+5v": {"library": "power", "symbol": "+5V"},
        "+3v3": {"library": "power", "symbol": "+3V3"},
        "+12v": {"library": "power", "symbol": "+12V"},
        "-12v": {"library": "power", "symbol": "-12V"},
    },
    "connectors": {
        "conn_2pin": {"library": "Connector", "symbol": "Conn_01x02_Male"},
        "conn_4pin": {"library": "Connector_Generic", "symbol": "Conn_01x04"},
        "conn_8pin": {"library": "Connector_Generic", "symbol": "Conn_01x08"},
    },
}

# Suggested footprints for common components, ordered by preference
# SMD variants listed first, followed by through-hole alternatives
DEFAULT_FOOTPRINTS = {
    "R": [
        "Resistor_SMD:R_0805_2012Metric",
        "Resistor_SMD:R_0603_1608Metric",
        "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal",
    ],
    "C": [
        "Capacitor_SMD:C_0805_2012Metric",
        "Capacitor_SMD:C_0603_1608Metric",
        "Capacitor_THT:C_Disc_D5.0mm_W2.5mm_P5.00mm",
    ],
    "LED": ["LED_SMD:LED_0805_2012Metric", "LED_THT:LED_D5.0mm"],
    "D": ["Diode_SMD:D_SOD-123", "Diode_THT:D_DO-35_SOD27_P7.62mm_Horizontal"],
}

# Operation timeout values in seconds for external process management
# Prevents hanging operations and provides user feedback
TIMEOUT_CONSTANTS = {
    "kicad_cli_version_check": 10.0,  # Timeout for KiCad CLI version checks
    "kicad_cli_export": 30.0,  # Timeout for KiCad CLI export operations
    "application_open": 10.0,  # Timeout for opening applications (e.g., KiCad)
    "subprocess_default": 30.0,  # Default timeout for subprocess operations
}

# Progress percentage milestones for long-running operations
# Provides consistent progress reporting across different tools
PROGRESS_CONSTANTS = {
    "start": 10,  # Initial progress percentage
    "detection": 20,  # Progress after CLI detection
    "setup": 30,  # Progress after setup complete
    "processing": 50,  # Progress during processing
    "finishing": 70,  # Progress when finishing up
    "validation": 90,  # Progress during validation
    "complete": 100,  # Progress when complete
}

# User interface display configuration values
# Controls how much information is shown in previews and summaries
DISPLAY_CONSTANTS = {
    "bom_preview_limit": 20,  # Maximum number of BOM items to show in preview
}
