"""Release hygiene guard: shipped text carries no internal process references; dead modules stay deleted."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INTERNAL = [r"(?i)not published", r"(?i)unpublished", r"AdaptiveEditor/", r"(?i)\bthe lead\b",
            r"(?i)\blead decision\b", r"(?i)owner direction", r"(?i)owner-approved", r"(?i)owner-directed",
            r"(?i)as the task specified", r"\bHaiku\b", r"(?i)development (receipt|evidence)",
            r"(?i)\bstate on 20\d\d", r"(?i)\(approved 20\d\d"]
DATED = [r"\b20\d\d-\d\d-\d\d\b"]
TEXT_GROUPS = {
    "shim": (["silent_stdio.pyw"], [r"machine-config"]),
    "job_breakaway": (["kicad_mcp/adaptive_editor/adaptive_engine/job.py"], [r"_SILENT_BREAKAWAY_OK\s*\|"]),
    "regex_backspace": (["kicad_mcp/coverage/live_sch_misc.py"], ["\x08"]),
    "tests_scratch": (["tests/test_generated_schematic.py", "tests/test_pcb_schematic_sync.py"], [r"\.tmp/tests"]),
    "l4_spec": (["docs/L4-UI-BROKER-SPEC.md"], INTERNAL + DATED + [r"\bB[1-6]\b", "§"]),
    "capability_docs": (["docs/KICAD-FULL-CAPABILITY.md", "docs/SOURCE-SEARCH-AND-TRUST-BOUNDARY.md",
                         "docs/adaptive-editor/ADAPTIVE-ENGINE-SPEC.md"],
                        INTERNAL + DATED + [r"\bR25\b", r"\bF0\d\b"]),
    "coverage_doc": (["docs/IPC-API-COVERAGE.md"], INTERNAL + DATED + [r"(?i)owner's real files"]),
    "plugin_docs": (["docs/adaptive-editor/LIVE-PLUGIN-SPEC.md", "docs/TEMP-STORAGE.md", "docs/schematic-tooling.md"],
                    INTERNAL + DATED + [r"(?i)owner requirements", r"(?i)owner'?s? desktop", r"(?i)owner-desktop"]),
    "evidence_comments": (["kicad_mcp/tools/full_capability_tools.py", "kicad_mcp/utils/plugin_catalog.py",
                           "kicad_mcp/utils/plugin_inspect.py"], INTERNAL),
    "engine_paths": (["kicad_mcp/tools/adaptive_tools.py", "kicad_mcp/l4/hidden_desktop.py",
                      "kicad_mcp/adaptive_editor/plugin/panel.py"], INTERNAL),
    "directives": (["kicad_mcp/launcher.py", "kicad_mcp/coverage/live_programs.py",
                    "kicad_mcp/coverage/clipboard.py"], INTERNAL),
    "registry_msg": (["kicad_mcp/l4/broker.py"], INTERNAL),
    "readme": (["README.md", "SECURITY.md"], [r"outside the safety model described below"]),
    "troubleshooting": (["docs/troubleshooting.md"],
                        [r"main\.py", r"KiCad 9", r"uv --directory", r"(?i)macOS", r"pcbnew_cli"]),
}
REQUIRED = {
    "readme": [("README.md", r"kicad\.org/download"), ("README.md", r"docs/troubleshooting\.md"),
               ("README.md", r"PowerShell"), ("SECURITY.md", r"ADAPTIVE_KICAD_HEADLESS_DIR")],
    "troubleshooting": [("docs/troubleshooting.md", r"kicad_mcp\.setup --check"),
                        ("docs/troubleshooting.md", r"KICAD_CLI_PATH")],
}
ABSENT = {"dead_validation": ["kicad_mcp/tools/validation_tools.py", "kicad_mcp/utils/boundary_validator.py"],
          "upstream_server": ["kicad_mcp/server.py", "main.py", "kicad_mcp/context.py", "kicad_mcp/resources",
                              "kicad_mcp/prompts", "kicad_mcp/tools/drc_impl", "kicad_mcp/utils/env.py",
                              "docs/development.md", "docs/bom_guide.md"]}


def _hits(rel: str, patterns: list[str]) -> list[str]:
    path = ROOT / rel
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    out = []
    for pattern in patterns:
        for m in re.finditer(pattern, text):
            out.append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {pattern}")
    return out


@pytest.mark.parametrize("group", sorted(TEXT_GROUPS))
def test_text_group(group: str) -> None:
    files, banned = TEXT_GROUPS[group]
    problems = [h for rel in files for h in _hits(rel, banned)]
    for rel, pattern in REQUIRED.get(group, []):
        if not re.search(pattern, (ROOT / rel).read_text(encoding="utf-8")):
            problems.append(f"{rel}: missing {pattern}")
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("group", sorted(ABSENT))
def test_absent(group: str) -> None:
    left = [rel for rel in ABSENT[group] if (ROOT / rel).exists()]
    assert not left, left
