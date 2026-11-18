"""Where the live drivers find their inputs, without machine-specific paths.

- KiCad's own demo projects ship with the KiCad install that serves the API: ``<install>/share/kicad/demos``
  next to the kicad-cli the session uses (``$KICAD_CLI`` or the newest install found).
- Prepared fixture projects (copies of demos with extra content) live in one folder named by
  ``$KICAD_AI_FIXTURES`` (default: ``.tmp/fixtures`` in the checkout).
"""
from __future__ import annotations

import os
from pathlib import Path


def demos_dir() -> Path:
    """KiCad's demo projects for the install the API session uses."""
    from kicad_mcp.ipc.session import find_kicad_cli

    cli = Path(find_kicad_cli())
    return cli.parent.parent / "share" / "kicad" / "demos"


def fixture(name: str) -> Path:
    """A prepared fixture project folder by name (e.g. ``hier-fixture``)."""
    root = os.environ.get("KICAD_AI_FIXTURES") or str(Path(__file__).resolve().parents[2] / ".tmp" / "fixtures")
    return Path(root) / name
