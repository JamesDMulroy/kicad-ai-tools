"""
KiCad AI Tools.

Model Context Protocol (MCP) tools that let an AI assistant work with KiCad projects without touching the
user's desktop. Start the server with ``kicad-ai-tools-launcher`` (see the README).
"""
import importlib
from importlib.metadata import PackageNotFoundError, version


def __getattr__(name):
    """Expose the configuration values as package attributes without importing them eagerly."""
    config = importlib.import_module(".config", __name__)
    if hasattr(config, name):
        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


try:
    __version__ = version("kicad-ai-tools")
except PackageNotFoundError:
    __version__ = "0.0.0"
__description__ = "Model Context Protocol tools for KiCad"

__all__ = ["__version__", "__description__"]
