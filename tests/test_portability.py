"""The repository carries no machine-specific paths, user names or company names, and no guessed home
folders ("nothing should ever be hardcoded"): every location comes from the environment, the per-machine
config written by setup, or discovery. Scans everything that is shipped or shared: the package, tests,
scripts, docs, the README and configuration files."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {".py", ".pyw", ".json", ".txt", ".toml", ".md", ".yaml", ".yml", ".ps1", ".sha256", ".cfg",
                 ".ini", ".example", ".html", ".kicad_pro", ".kicad_pcb", ".kicad_sch", ".kicad_dru",
                 ".kicad_prl", ".kicad_mod", ".csv", ".bat", ".cmd", ".sh"}
SKIP_DIRS = {".git", ".venv", ".venv310", ".tmp", "__pycache__", ".pytest_cache", ".ruff_cache", "htmlcov", "node_modules",
             "evidence"}   # live-run evidence: the home directory is already redacted to "~"
SKIP_FILES = {"uv.lock", ".portability-denylist"}


def _denylist():
    words = [w.strip() for w in os.environ.get("PORTABILITY_DENYLIST", "").split(",") if w.strip()]
    local = Path(__file__).resolve().parent.parent / ".portability-denylist"
    if local.is_file():
        words += [w.strip() for w in local.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]
    return words or ["example-private-name"]


FORBIDDEN = {
    "drive-letter path": re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]{1,2}[A-Za-z0-9]"),
    # a concrete user folder; documentation placeholders ("username", "<name>") are examples, not locations
    "home-directory path": re.compile(r"[\\/](?:Users|home)[\\/](?!<|username\b|you\b)[A-Za-z0-9_.-]+[\\/]", re.I),
    # Private names to keep out of the tree come from an untracked deny list (one word per line in
    # .portability-denylist, or comma-separated in PORTABILITY_DENYLIST); the public default is a neutral example.
    "personal or company name": re.compile(r"\b(?:" + "|".join(map(re.escape, _denylist())) + r")\b", re.I),
    # any folder built from the home directory is a guess unless explicitly justified below
    "home-relative folder guess": re.compile(r"(?:Path\.home\(\)|expanduser\([\"']~)\s*/?\s*[\"']?\.?[A-Za-z]"),
    "home-relative path": re.compile(r"(?<![\w.])~[\\/][\w.]"),
}
# A line carrying this marker is an OS-defined location (the macOS folders, the XDG defaults) or a synthetic
# test path, justified on that line itself; nothing else may build a folder from the home directory.
LINE_MARKER = "portability-ok:"
# (file, label) pairs that are not locations: tests that feed deliberately hostile or fake paths, this
# guard's own pattern list, and the internal plan that names the canon it follows.
ALLOWED: set[tuple[str, str]] = {
    ("tests/test_full_capability_tools.py", "drive-letter path"),
    ("tests/test_l4.py", "drive-letter path"),
    ("tests/test_plugin_inspect.py", "drive-letter path"),
    ("tests/test_source_search.py", "drive-letter path"),
    ("tests/test_adaptive_tools.py", "drive-letter path"),
    ("tests/test_boardtooling_workflow.py", "drive-letter path"),
    # Claude Desktop's own macOS config/log locations, documented for troubleshooting (product facts)
    ("docs/troubleshooting.md", "home-relative path"),
    (".github/ISSUE_TEMPLATE/bug_report.md", "home-relative path"),
    ("tests/test_portability.py", "personal or company name"),
    ("LICENSE", "personal or company name"),  # the copyright holders the license names
    ("README.md", "drive-letter path"),  # placeholder install paths in the client-config examples
}


def _files():
    for path in ROOT.rglob("*"):
        rel = path.relative_to(ROOT)
        if not path.is_file() or any(part in SKIP_DIRS for part in rel.parts) or path.name in SKIP_FILES:
            continue
        if path.suffix in TEXT_SUFFIXES or path.name.startswith(".") or not path.suffix:  # dotfiles, LICENSE, Makefile
            yield rel.as_posix(), path.read_text(encoding="utf-8", errors="replace")


def test_repository_has_no_machine_paths_or_names():
    problems = []
    for rel, text in _files():
        for label, pattern in FORBIDDEN.items():
            if (rel, label) in ALLOWED:
                continue
            for m in pattern.finditer(text):
                end = text.find("\n", m.end())
                if LINE_MARKER in text[text.rfind("\n", 0, m.start()) + 1:end if end >= 0 else len(text)]:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line}: {label}: {m.group(0)!r}")
    assert not problems, "\n".join(problems[:60])


def test_plugin_identifier_is_neutral_and_valid_for_kicad():
    import json
    manifest = json.loads((ROOT / "kicad_mcp/adaptive_editor/plugin/plugin.json").read_text(encoding="utf-8"))
    ident = manifest["identifier"]
    assert ident.startswith("local.")  # reserved-for-local-use TLD: claims no domain
    assert re.search(r"[\w\d]{2,}\.[\w\d]+\.[\w\d]+", ident)  # KiCad's API_PLUGIN::IsValidIdentifier


def test_every_setting_is_documented():
    from kicad_mcp.adaptive_editor import kicad_env
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    docs = (ROOT / "docs/configuration.md").read_text(encoding="utf-8")
    missing = []
    for key, env, _meaning in kicad_env.SETTINGS:
        if f"# {env}=" not in example:
            missing.append(f".env.example: {env}")
        if f"| `{key}` | `{env}`" not in docs:
            missing.append(f"docs/configuration.md: {key} / {env}")
    assert not missing, missing
