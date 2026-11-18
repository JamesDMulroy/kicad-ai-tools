"""Print the package hash that live evidence records as ``source_sha256.package_tree``.

    python scripts/source_digest.py

The same algorithm as ``kicad_mcp/adaptive_editor/tests_live/_harness.source_digest``: sha256 over every
packaged ``.py``, ``.json`` and ``.sha256`` file (path and content hash, sorted), so a reviewer can check
that an evidence record was produced by exactly the bytes under review.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def source_digest(root: Path = ROOT) -> dict:
    package = root / "kicad_mcp"
    files = sorted(p for p in package.rglob("*")
                   if p.is_file() and "__pycache__" not in p.parts and p.suffix in (".py", ".json", ".sha256"))
    digest = hashlib.sha256()
    for p in files:
        digest.update(p.relative_to(root).as_posix().encode() + b"\0" + hashlib.sha256(p.read_bytes()).digest())
    return {"package_tree": digest.hexdigest(), "files": len(files)}


if __name__ == "__main__":
    print(json.dumps(source_digest()))
