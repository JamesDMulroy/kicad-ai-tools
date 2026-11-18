"""
KiCad MCP Server.

A Model Context Protocol (MCP) server for KiCad electronic design automation (EDA) files.
"""
import importlib


def __getattr__(name):
    """Load public helpers without eagerly importing every optional tool."""
    if name in {"create_server", "add_cleanup_handler", "run_cleanup_handlers", "shutdown_server"}:
        return getattr(importlib.import_module(".server", __name__), name)
    if name in {"kicad_lifespan", "KiCadAppContext"}:
        return getattr(importlib.import_module(".context", __name__), name)
    config = importlib.import_module(".config", __name__)
    if hasattr(config, name):
        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__version__ = "0.1.0"
__author__ = "Lama Al Rajih"
__description__ = "Model Context Protocol server for KiCad on Mac, Windows, and Linux"

__all__ = [
    # Package metadata
    "__version__",
    "__author__",
    "__description__",

    # Server creation / shutdown helpers
    "create_server",
    "add_cleanup_handler",
    "run_cleanup_handlers",
    "shutdown_server",

    # Lifespan / context helpers
    "kicad_lifespan",
    "KiCadAppContext",
]
