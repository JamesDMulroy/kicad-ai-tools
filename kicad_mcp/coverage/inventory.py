"""Inventory of every function a KiCad build exposes, generated from that build's own sources.

Three sources, each read from the exact commit of the installed KiCad, never from a hand-kept list:

- API commands: every ``registerHandler<Request, Response>`` in ``*/api/api_handler*.cpp``, plus every
  request message declared in ``api/proto/**/*_commands.proto`` (a declared request with no handler is
  listed as ``declared_only``).
- Editor actions: every ``TOOL_ACTION_ARGS()`` definition, with its name, scope, flags and parameter
  type. Each action carries its own parameter value, which run-by-name uses (TOOL_ACTION::MakeEvent), so
  ``runnable`` is every action that is not an interactive tool (AF_ACTIVATE, which waits for the mouse).
  ``caller_parameter`` marks an action whose built-in value is only a placeholder for data a caller
  must supply (a pointer, a *_PARAMS struct): run by name, it does nothing useful.
- kicad-cli commands: the installed binary's own ``--help`` tree.
- Programs with neither actions nor an API: each PCB calculator panel (``AddCalculator`` in
  pcb_calculator_frame.cpp) and each bitmap2component output format (``OUTPUT_FMT_ID``).

    python -m kicad_mcp.coverage.inventory --source <kicad checkout> --cli <kicad-cli> --out inv.json
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path
import re
import subprocess
import sys

EDITOR_SOURCE_DIRS = ("common", "eeschema", "pcbnew", "gerbview", "pagelayout_editor", "kicad", "cvpcb",
                      "3d-viewer", "include")

_HANDLER_RE = re.compile(r"registerHandler\s*<\s*([\w:]+)\s*,\s*([\w:]+)\s*>")
_PROTO_MESSAGE_RE = re.compile(r"^message\s+(\w+)", re.MULTILINE)
# "repeated kiapi.common.types.SaveOptions options = 3;" -> SaveOptions (last dotted part of the type)
_PROTO_FIELD_TYPE_RE = re.compile(
    r"(?:^|[{;])\s*(?:repeated\s+|optional\s+)?(?:\w+\.)*(\w+)\s+\w+\s*=\s*\d+", re.MULTILINE)
_ACTION_START = "TOOL_ACTION_ARGS()"
_NAME_RE = re.compile(r'\.Name\(\s*"([^"]+)"\s*\)')
_SCOPE_RE = re.compile(r"\.Scope\(\s*(AS_\w+)\s*\)")
_FLAGS_RE = re.compile(r"\.Flags\(\s*([^)]*?)\s*\)")
_PARAM_RE = re.compile(r"\.Parameter\s*(?:<\s*(.+?)\s*>)?\s*\(")
_FRIENDLY_RE = re.compile(r'\.FriendlyName\(\s*(?:_\(\s*)?"((?:[^"\\]|\\.)*)"')
_TOOLTIP_RE = re.compile(r'\.Tooltip\(\s*(?:_\(\s*)?"((?:[^"\\]|\\.)*)"')
_CLI_SUBCOMMANDS_RE = re.compile(r"^Subcommands:\s*$", re.MULTILINE)
_CLI_USAGE_CHOICES_RE = re.compile(r"\{([\w,-]+)\}")


@dataclass(frozen=True)
class ApiCommand:
    request: str
    response: str
    handler: str  # source file that registers it, or "" when declared_only
    status: str   # "handled" | "declared_only"


@dataclass(frozen=True)
class EditorAction:
    name: str
    editor: str           # name prefix: eeschema, pcbnew, common, ...
    scope: str
    flags: str
    parameter: str | None  # C++ parameter type, "" when untyped, None when the action takes none
    friendly_name: str
    tooltip: str
    source: str           # file:line
    runnable: bool        # run by name works: not an interactive tool (its own parameter value is used)
    caller_parameter: bool = False  # its parameter is a placeholder for caller data (pointer, *_PARAMS)


@dataclass
class Inventory:
    kicad_version: str
    source_commit: str
    api_commands: list[ApiCommand] = field(default_factory=list)
    actions: list[EditorAction] = field(default_factory=list)
    cli_commands: list[str] = field(default_factory=list)
    programs: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "kicad_version": self.kicad_version,
            "source_commit": self.source_commit,
            "counts": {
                "api_commands": len(self.api_commands),
                "api_handled": sum(c.status == "handled" for c in self.api_commands),
                "actions": len(self.actions),
                "actions_runnable_by_name": sum(a.runnable for a in self.actions),
                "cli_commands": len(self.cli_commands),
                "programs": len(self.programs),
            },
            "api_commands": [asdict(c) for c in self.api_commands],
            "actions": [asdict(a) for a in self.actions],
            "cli_commands": self.cli_commands,
            "programs": self.programs,
        }


def _short(type_name: str) -> str:
    return type_name.split("::")[-1]


def scan_api(source: Path) -> list[ApiCommand]:
    """Handled commands from the handlers, plus requests declared in the protos but not handled."""
    handled: dict[str, ApiCommand] = {}
    for path in sorted(source.glob("*/api/api_handler*.cpp")):
        rel = path.relative_to(source).as_posix()
        for request, response in _HANDLER_RE.findall(path.read_text(encoding="utf-8", errors="replace")):
            key = _short(request)
            # The same request may be served by several editors (schematic and board both save, etc.).
            prior = handled.get(key)
            handlers = f"{prior.handler}, {rel}" if prior and rel not in prior.handler else rel
            handled[key] = ApiCommand(key, _short(response), handlers, "handled")

    proto_files = sorted((source / "api" / "proto").rglob("*_commands.proto"))
    if not handled or not proto_files:
        # An empty side means an incomplete checkout, never "nothing to cover".
        raise SystemExit(f"incomplete KiCad source at {source}: {len(handled)} handlers, "
                         f"{len(proto_files)} command protos")
    responses = {c.response for c in handled.values()}
    # A helper type (SaveOptions, ItemStatus, ...) is a field inside other messages; a command never is.
    all_proto = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                          for p in (source / "api" / "proto").rglob("*.proto"))
    embedded = set(_PROTO_FIELD_TYPE_RE.findall(all_proto))
    declared: dict[str, ApiCommand] = {}
    for path in proto_files:
        text = path.read_text(encoding="utf-8", errors="replace")
        for message in _PROTO_MESSAGE_RE.findall(text):
            if (message in handled or message in responses or message.endswith("Response")
                    or message in embedded):
                continue
            declared[message] = ApiCommand(message, "", "", "declared_only")
    return sorted([*handled.values(), *declared.values()], key=lambda c: c.request)


def _balanced_span(text: str, open_index: int) -> int:
    """Index just past the parenthesis that closes the one at ``open_index`` (strings respected)."""
    depth = 0
    i = open_index
    in_string = False
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError(f"unbalanced parentheses from offset {open_index}")


def _action_from(body: str, source: str) -> EditorAction | None:
    name = _NAME_RE.search(body)
    if not name:
        return None
    flags = (_FLAGS_RE.search(body).group(1) if _FLAGS_RE.search(body) else "AF_NONE").strip()
    param = _PARAM_RE.search(body)
    parameter = None if not param else (param.group(1) or "")
    friendly = _FRIENDLY_RE.search(body)
    tooltip = _TOOLTIP_RE.search(body)
    interactive = "AF_ACTIVATE" in flags
    return EditorAction(
        name=name.group(1),
        editor=name.group(1).split(".", 1)[0],
        scope=_SCOPE_RE.search(body).group(1) if _SCOPE_RE.search(body) else "AS_CONTEXT",
        flags=flags,
        parameter=parameter,
        friendly_name=friendly.group(1) if friendly else "",
        tooltip=tooltip.group(1) if tooltip else "",
        source=source,
        runnable=not interactive,
        caller_parameter=bool(parameter) and (parameter.rstrip().endswith("*") or parameter.endswith("PARAMS")),
    )


# KiCad marks the bare tool-activation registrations ("3DViewer.Control", "cvpcb.Control") with this comment:
# no description, no menu, no hotkey, "not supposed to be shown anywhere". They register a tool, not an action.
_HIDDEN_NOTE = "No description, it is not supposed to be shown anywhere"


def scan_actions(source: Path) -> list[EditorAction]:
    """Every TOOL_ACTION defined in the editors' C++ sources (tests and third-party code excluded)."""
    actions: dict[str, EditorAction] = {}
    for top in EDITOR_SOURCE_DIRS:
        for path in sorted((source / top).rglob("*")):
            if path.suffix not in (".cpp", ".h") or "/qa/" in path.as_posix():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            start = 0
            while (hit := text.find(_ACTION_START, start)) != -1:
                open_paren = text.rfind("(", 0, hit)  # the TOOL_ACTION( that wraps the args
                end = _balanced_span(text, open_paren)
                if end <= hit:
                    # That "(" closed before the args (TOOL_ACTION_ARGS() used outside a TOOL_ACTION(...)
                    # wrapper); read only the builder chain itself, and never let the scan move backwards.
                    end = _balanced_span(text, hit + len(_ACTION_START) - 2)
                    while text.startswith(".", end) or text[end:end + 1].isspace():
                        dot = text.find("(", end)
                        if dot == -1 or not text[end:dot].strip().startswith("."):
                            break
                        end = _balanced_span(text, dot)
                line = text.count("\n", 0, hit) + 1
                line_start = text.rfind("\n", 0, open_paren) + 1
                if re.match(r"[ \t]+TOOL_ACTION\s+\w+\s*$", text[line_start:open_paren]):
                    # a local TOOL_ACTION built inside a function (e.g. the hotkey store's "common.Control.Fake",
                    # made only to look up a section name): not an action anything can run
                    start = max(end, hit + len(_ACTION_START))
                    continue
                if _HIDDEN_NOTE in text[max(0, open_paren - 160):open_paren]:
                    start = max(end, hit + len(_ACTION_START))  # a tool registration, upstream says no action
                    continue
                action = _action_from(text[hit:end], f"{path.relative_to(source).as_posix()}:{line}")
                if action and action.name not in actions:
                    actions[action.name] = action
                start = max(end, hit + len(_ACTION_START))
    return sorted(actions.values(), key=lambda a: a.name)


def _run_cli(cli: str, words: list[str]) -> str:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    run = subprocess.run([cli, *words], capture_output=True, text=True, timeout=60, creationflags=flags)
    return run.stdout + run.stderr


_CALCULATOR_RE = re.compile(r'AddCalculator\(\s*new\s+\w+\([^)]*\)\s*,\s*_\(\s*"([^"]+)"')
_BITMAP_FMT_RE = re.compile(r"enum\s+OUTPUT_FMT_ID\s*\{([^}]*)\}", re.S)


def scan_programs(source: Path) -> list[str]:
    """Functions of KiCad programs that have no actions and no API: calculator panels, bitmap formats."""
    found: list[str] = []
    frame = source / "pcb_calculator" / "pcb_calculator_frame.cpp"
    if frame.exists():
        found += [f"pcb_calculator:{name}" for name in _CALCULATOR_RE.findall(frame.read_text(encoding="utf-8"))]
    header = source / "bitmap2component" / "bitmap2component.h"
    if header.exists():
        block = _BITMAP_FMT_RE.search(header.read_text(encoding="utf-8"))
        if block:
            names = [re.sub(r"//.*", "", n).strip().rstrip(",") for n in block.group(1).splitlines()]
            found += [f"bitmap2component:{n}" for n in names if n]
    if not found:
        raise SystemExit(f"incomplete KiCad source at {source}: no calculator or bitmap2component sources")
    return sorted(found)


def _cli_help(cli: str, words: list[str]) -> str:
    return _run_cli(cli, [*words, "--help"])


def scan_cli(cli: str) -> list[str]:
    """Every leaf command in the installed kicad-cli, found by walking its own --help output."""
    leaves: list[str] = []

    def walk(words: list[str]) -> None:
        text = _cli_help(cli, words)
        usage = text.split("\n", 1)[0]
        choices = _CLI_USAGE_CHOICES_RE.search(usage) if _CLI_SUBCOMMANDS_RE.search(text) else None
        if not choices:
            if words:
                leaves.append(" ".join(words))
            return
        for sub in choices.group(1).split(","):
            walk([*words, sub])

    walk([])
    return sorted(leaves)


def cli_version(cli: str) -> tuple[str, str]:
    """(version, full commit hash) of the installed binary, as it reports them itself."""
    about = _run_cli(cli, ["version", "--format", "about"])
    match = re.search(r"^Version:\s*([^,\s]+)", about, re.MULTILINE)
    version = match.group(1) if match else _run_cli(cli, ["version"]).strip()
    return version, _run_cli(cli, ["version", "--format", "commit"]).strip()


def build(source: Path, cli: str, commit: str) -> Inventory:
    """Refuses a source checkout that is not the installed binary's own commit."""
    version, binary_commit = cli_version(cli)
    if binary_commit != commit:
        raise SystemExit(f"source commit {commit} is not the installed KiCad's ({version}, {binary_commit})")
    return Inventory(version, commit, scan_api(source), scan_actions(source), scan_cli(cli), scan_programs(source))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--source", type=Path, required=True, help="KiCad source checkout at the commit")
    parser.add_argument("--commit", required=True, help="full commit hash of that checkout")
    parser.add_argument("--cli", required=True, help="the installed kicad-cli")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    inventory = build(args.source, args.cli, args.commit)
    args.out.write_text(json.dumps(inventory.to_json(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(inventory.to_json()["counts"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
