"""Registry of the kicad-cli 10.0.5 leaf commands, parsed from the captured ``--help`` tree.

The registry is read-only data: ``kicad_mcp/data/kicad-cli-10.0.5-tree.json`` holds the help
text of every kicad-cli node, and this module turns the 43 leaf commands (nodes with no
subcommands) into ``Command``/``Option`` records. ``validate_options`` is the only way this
module produces argv tokens: it accepts nothing that is not a declared option of the command.

Conventions:

* An option is identified by its canonical long flag, the longest ``--`` flag on its help line
  (``--footprint`` for ``--fp, --footprint``). The other ``--`` flags are ``aliases`` and a
  single-dash flag is ``short``. Argv always uses the canonical long flag, never a short flag.
* ``-h, --help`` is excluded everywhere. ``pcb render`` reuses ``-h`` for ``--height``, so that
  short flag is dropped (``short`` is ``None``) and ``-h`` is never accepted as a key.
* An option takes a value only when its help carries ``[nargs=0..1]`` and is repeatable only
  when it carries ``[may be repeated]``. Choices are recorded only where the help lists them.
* Known limit: values that start with ``-`` (negative numbers such as ``render --rotate
  -45,0,45``) are refused with code ``leading_dash``. Whether kicad-cli accepts the
  ``--opt=value`` form is unverified, so no value that could be read as a flag reaches argv.
* Positional arguments (INPUT_FILE, INPUT_FILE_OR_DIR) and path policy are the caller's job;
  ``validate_options`` covers options only.
"""
from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "CliRegistryError",
    "Command",
    "Option",
    "Positional",
    "EXPECTED_LEAF_COUNT",
    "TREE_PATH",
    "get_command",
    "list_commands",
    "load_registry",
    "validate_options",
]

TREE_FILE_NAME = "kicad-cli-10.0.5-tree.json"
TREE_PATH = Path(__file__).resolve().parent.parent / "data" / TREE_FILE_NAME
ROOT_NODE = "(root)"
EXPECTED_LEAF_COUNT = 43

MAX_VALUE_CHARS = 4096

# Commands that rewrite or create a KiCad design file as their purpose.
_MUTATING_COMMANDS = frozenset(
    {"fp upgrade", "sym upgrade", "sch upgrade", "pcb upgrade", "pcb import"}
)
# Per-command options that write back to the input design.
_WRITES_INPUT_OPTIONS: dict[str, frozenset[str]] = {
    "pcb drc": frozenset({"--save-board"}),
}
# The help prints a bare integer default for these, but they accept fractional values.
_FLOAT_OPTIONS = frozenset(
    {
        "--scale",
        "--track-width-correction",
        "--x-scale-factor",
        "--y-scale-factor",
        "--zoom",
        "--light-side-elevation",
    }
)
_POSITIONAL_KINDS = {
    "INPUT_FILE": "file",
    "INPUT_FILE_OR_DIR": "file_or_dir",
    "INPUT_DIR": "dir",
}

_SECTION_RE = re.compile(r"^(Positional arguments|Optional arguments|Subcommands):\s*$")
# Flag column: one or more flags separated by commas; the description follows after 2+ spaces.
# Leading indentation varies (gencad uses three spaces), so only "whitespace then a dash" counts.
_OPTION_RE = re.compile(
    r"^\s+(-{1,2}[A-Za-z0-9][\w-]*(?:\s*,\s*-{1,2}[A-Za-z0-9][\w-]*)*)(?:\s{2,}(.*))?$"
)
_POSITIONAL_RE = re.compile(r"^\s+(\S+)(?:\s{2,}(.*))?$")
_USAGE_OPTION_RE = re.compile(r"\[(--[A-Za-z0-9][\w-]*)(?:\s+([^\]\s]+))?\]")
_DEFAULT_RE = re.compile(r"\[default:\s?(.*)\]\s*$")
_NARGS_MARK = "[nargs=0..1]"
_REPEAT_MARK = "[may be repeated]"

_CHOICES_OPTIONS_RE = re.compile(
    r"\b(?:valid\s+options|options)\b\s*(?:are\b\s*)?:?\s*(.+)$", re.IGNORECASE
)
_CHOICES_FORMAT_RE = re.compile(r"\bformat(?:\s+hint)?:\s*(.+)$", re.IGNORECASE)
_CHOICES_PAREN_RE = re.compile(r"\(([\w-]+(?:\s*,\s*[\w-]+)+)\)\s*$")
_CHOICE_SPLIT_RE = re.compile(r"\s*,\s*(?:or\s+)?|\s+or\s+")
_CHOICE_ITEM_RE = re.compile(r"[\w.+-]+")

_INT_RE = re.compile(r"-?[0-9]+")
_FLOAT_RE = re.compile(r"-?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)")
_DEFINE_VAR_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:-]*=.*", re.DOTALL)


class CliRegistryError(Exception):
    """Raised for registry load problems and for any option set the registry refuses."""

    def __init__(self, message: str, *, code: str = "invalid", option: str | None = None,
                 command: str | None = None):
        super().__init__(message)
        self.code = code
        self.option = option
        self.command = command


@dataclass(frozen=True, eq=False)
class Option:
    long: str
    short: str | None
    aliases: tuple[str, ...]
    takes_value: bool
    repeatable: bool
    default: Any
    choices: tuple[str, ...] | None
    value_type: str  # string | int | float | bool
    help: str
    metavar: str | None = None
    writes_input: bool = False
    deprecated: bool = False

    @property
    def spellings(self) -> tuple[str, ...]:
        names = [self.long, *self.aliases]
        if self.short:
            names.append(self.short)
        return tuple(names)

    def to_dict(self) -> dict[str, Any]:
        return {
            "long": self.long,
            "short": self.short,
            "aliases": list(self.aliases),
            "takes_value": self.takes_value,
            "repeatable": self.repeatable,
            "default": self.default,
            "choices": list(self.choices) if self.choices is not None else None,
            "value_type": self.value_type,
            "help": self.help,
            "metavar": self.metavar,
            "writes_input": self.writes_input,
            "deprecated": self.deprecated,
        }


@dataclass(frozen=True, eq=False)
class Positional:
    name: str | None
    kind: str  # file | dir | file_or_dir | none


@dataclass(frozen=True, eq=False)
class Command:
    name: str
    argv_prefix: list[str]
    summary: str
    positional: Positional
    options: tuple[Option, ...]
    classification: str  # read_only | mutating
    mutating_options: tuple[str, ...]
    output_kind: str | None  # file | dir | file_or_dir | None
    deprecated: bool
    usage: str
    _lookup: dict[str, Option] = field(default_factory=dict, repr=False)

    @property
    def group(self) -> str:
        return self.name.split(" ", 1)[0]

    @property
    def mutating(self) -> bool:
        return self.classification == "mutating"

    @property
    def may_write_input(self) -> bool:
        return self.mutating or bool(self.mutating_options)

    def find_option(self, key: Any) -> Option | None:
        """Resolve a key given by long, alias or short name, with or without leading dashes."""
        if not isinstance(key, str):
            return None
        text = key.strip()
        bare = text.lstrip("-")
        if not bare or len(text) - len(bare) > 2:
            return None
        return self._lookup.get(bare.replace("_", "-"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv_prefix": list(self.argv_prefix),
            "summary": self.summary,
            "positional": {"name": self.positional.name, "kind": self.positional.kind},
            "options": [option.to_dict() for option in self.options],
            "classification": self.classification,
            "mutating_options": list(self.mutating_options),
            "output_kind": self.output_kind,
            "deprecated": self.deprecated,
        }


# --- help parsing -----------------------------------------------------------------------------


def _unquote(raw: str) -> tuple[str, bool]:
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1], True
    return raw, False


def _infer_value_type(long: str, takes_value: bool, default_raw: str | None) -> str:
    if not takes_value:
        return "bool"
    if long in _FLOAT_OPTIONS:
        return "float"
    if default_raw is None:
        return "string"
    inner, quoted = _unquote(default_raw)
    if quoted:
        return "string"
    if re.fullmatch(r"-?[0-9]+", inner):
        return "int"
    if re.fullmatch(r"-?[0-9]+\.[0-9]+", inner):
        return "float"
    if inner in ("true", "false"):
        return "bool"
    return "string"


def _typed_default(raw: str | None, value_type: str, repeatable: bool) -> Any:
    if raw is None:
        return None
    if repeatable and raw == "{}":
        return []
    inner, _quoted = _unquote(raw)
    if value_type == "int":
        return int(inner)
    if value_type == "float":
        return float(inner)
    if value_type == "bool":
        return inner == "true"
    return inner


def _parse_choices(help_text: str) -> tuple[str, ...] | None:
    candidate = None
    for pattern in (_CHOICES_OPTIONS_RE, _CHOICES_FORMAT_RE):
        found = pattern.search(help_text)
        if found:
            candidate = found.group(1)
            break
    if candidate is None:
        found = _CHOICES_PAREN_RE.search(help_text)
        if found:
            candidate = found.group(1)
    if candidate is None:
        return None
    candidate = re.split(r"\s+\(", candidate)[0]  # drop "(default: x)" tails
    candidate = re.split(r"\.(?:\s|$)", candidate)[0]  # drop trailing prose after the list
    items = [item.strip().strip("\"'") for item in _CHOICE_SPLIT_RE.split(candidate)]
    if len(items) < 2 or any(not _CHOICE_ITEM_RE.fullmatch(item) for item in items):
        return None  # e.g. "..., or user-defined preset name" is not a closed list
    return tuple(items)


def _build_option(command: str, flags_text: str, description: str, usage_meta: dict[str, str | None]
                  ) -> Option | None:
    flags = [part.strip() for part in flags_text.split(",")]
    if "--help" in flags:
        return None
    longs = [flag for flag in flags if flag.startswith("--")]
    shorts = [flag for flag in flags if not flag.startswith("--")]
    if not longs:
        raise CliRegistryError(f"{command}: option line has no long flag: {flags_text!r}",
                               code="parse_error", command=command)
    if len(shorts) > 1:
        raise CliRegistryError(f"{command}: option line has several short flags: {flags_text!r}",
                               code="parse_error", command=command)
    long = max(longs, key=len)
    aliases = tuple(flag for flag in longs if flag != long)
    # "-h" belongs to --help; pcb render also uses it for --height. Never expose it.
    short = shorts[0] if shorts and shorts[0] != "-h" else None

    text = description.strip()
    repeatable = _REPEAT_MARK in text
    body = text.replace(_REPEAT_MARK, "").rstrip()
    takes_value = _NARGS_MARK in body
    default_match = _DEFAULT_RE.search(body)
    default_raw = default_match.group(1) if default_match else None
    help_text = body[: body.index(_NARGS_MARK)].strip() if takes_value else body.strip()

    value_type = _infer_value_type(long, takes_value, default_raw)
    metavar = next((usage_meta[flag] for flag in longs if usage_meta.get(flag)), None)
    return Option(
        long=long,
        short=short,
        aliases=aliases,
        takes_value=takes_value,
        repeatable=repeatable,
        default=_typed_default(default_raw, value_type, repeatable) if takes_value else None,
        choices=_parse_choices(help_text) if takes_value and value_type != "bool" else None,
        value_type=value_type,
        help=help_text,
        metavar=metavar if takes_value else None,
        writes_input=long in _WRITES_INPUT_OPTIONS.get(command, frozenset()),
        deprecated=help_text.lower().startswith("deprecated"),
    )


def _output_kind(options: Sequence[Option]) -> str | None:
    for option in options:
        if option.long != "--output":
            continue
        text = option.help.lower()
        if text.startswith("output file or directory"):
            return "file_or_dir"
        if text.startswith("output directory"):
            return "dir"
        if text.startswith("output file"):
            return "file"
    return None


def _parse_command(name: str, help_text: str) -> Command:
    lines = help_text.splitlines()
    if not lines or not lines[0].startswith("Usage:"):
        raise CliRegistryError(f"{name}: help text does not start with 'Usage:'",
                               code="parse_error", command=name)
    usage = lines[0].strip()
    usage_meta = {found.group(1): found.group(2) for found in _USAGE_OPTION_RE.finditer(usage)}

    section = "summary"
    summary_parts: list[str] = []
    positional_lines: list[str] = []
    entries: list[list[Any]] = []  # [flags_text, [description parts]]
    for line in lines[1:]:
        header = _SECTION_RE.match(line)
        if header:
            section = header.group(1)
            continue
        stripped = line.strip()
        if not stripped:
            if section != "summary":
                section = "end"
            continue
        if section == "summary":
            summary_parts.append(stripped)
        elif section == "Positional arguments":
            positional_lines.append(line)
        elif section == "Optional arguments":
            found = _OPTION_RE.match(line)
            if found:
                entries.append([found.group(1), [found.group(2) or ""]])
            elif entries:
                entries[-1][1].append(stripped)  # continuation of the previous option
            else:
                raise CliRegistryError(f"{name}: unexpected line before first option: {line!r}",
                                       code="parse_error", command=name)

    options: list[Option] = []
    seen: set[str] = set()
    for flags_text, parts in entries:
        option = _build_option(name, flags_text, " ".join(p.strip() for p in parts if p.strip()),
                               usage_meta)
        if option is None:
            continue
        if option.long in seen:
            raise CliRegistryError(f"{name}: option {option.long} is declared twice",
                                   code="parse_error", command=name)
        seen.add(option.long)
        options.append(option)

    if positional_lines:
        found = _POSITIONAL_RE.match(positional_lines[0])
        if found is None or len(positional_lines) != 1:
            raise CliRegistryError(f"{name}: cannot parse positional arguments {positional_lines!r}",
                                   code="parse_error", command=name)
        kind = _POSITIONAL_KINDS.get(found.group(1))
        if kind is None:
            raise CliRegistryError(f"{name}: unknown positional {found.group(1)!r}",
                                   code="parse_error", command=name)
        positional = Positional(found.group(1), kind)
    else:
        positional = Positional(None, "none")

    lookup: dict[str, Option] = {}
    for option in options:
        for spelling in option.spellings:
            bare = spelling.lstrip("-")
            if lookup.get(bare, option) is not option:
                raise CliRegistryError(f"{name}: spelling {spelling!r} is ambiguous",
                                       code="parse_error", command=name)
            lookup[bare] = option

    summary = " ".join(summary_parts)
    return Command(
        name=name,
        argv_prefix=name.split(),
        summary=summary,
        positional=positional,
        options=tuple(options),
        classification="mutating" if name in _MUTATING_COMMANDS else "read_only",
        mutating_options=tuple(o.long for o in options if o.writes_input),
        output_kind=_output_kind(options),
        deprecated=summary.lower().startswith("no longer supported"),
        usage=usage,
        _lookup=lookup,
    )


def _parse_tree(tree: Mapping[str, Any]) -> dict[str, Command]:
    if not isinstance(tree, Mapping) or ROOT_NODE not in tree:
        raise CliRegistryError(f"tree has no {ROOT_NODE!r} node", code="tree_error")
    for name, node in tree.items():
        if (not isinstance(node, Mapping) or not isinstance(node.get("help"), str)
                or not isinstance(node.get("subcommands"), list)):
            raise CliRegistryError(f"tree node {name!r} lacks help text or a subcommands list",
                                   code="tree_error")
    for name, node in tree.items():
        for sub in node["subcommands"]:
            child = sub if name == ROOT_NODE else f"{name} {sub}"
            if child not in tree:
                raise CliRegistryError(f"tree node {name!r} lists missing subcommand {sub!r}",
                                       code="tree_error")
    leaf_names = [name for name, node in tree.items()
                  if name != ROOT_NODE and not node["subcommands"]]
    if len(leaf_names) != EXPECTED_LEAF_COUNT:
        raise CliRegistryError(
            f"expected exactly {EXPECTED_LEAF_COUNT} kicad-cli leaf commands, found {len(leaf_names)}",
            code="tree_error")
    commands = {name: _parse_command(name, tree[name]["help"]) for name in leaf_names}
    for name in _MUTATING_COMMANDS:
        if name not in commands:
            raise CliRegistryError(f"mutating command {name!r} is not in the registry",
                                   code="tree_error")
    for name, longs in _WRITES_INPUT_OPTIONS.items():
        present = {o.long for o in commands[name].options} if name in commands else set()
        if not longs <= present:
            raise CliRegistryError(f"writes-input option table names unknown options for {name!r}",
                                   code="tree_error")
    return commands


def load_registry(path: str | Path | None = None) -> dict[str, Command]:
    """Parse the captured help tree (default: the packaged 10.0.5 file) into leaf commands."""
    source = Path(path) if path is not None else TREE_PATH
    try:
        tree = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CliRegistryError(f"cannot load kicad-cli help tree {source}: {exc}",
                               code="tree_error") from exc
    return _parse_tree(tree)


_registry_cache: dict[str, Command] | None = None
_registry_lock = threading.Lock()


def _registry() -> dict[str, Command]:
    global _registry_cache
    with _registry_lock:
        if _registry_cache is None:
            _registry_cache = load_registry()
        return _registry_cache


# --- public lookup ----------------------------------------------------------------------------


def list_commands() -> list[Command]:
    """All 43 leaf commands in help-tree order."""
    return list(_registry().values())


def _normalize_name(name: Any) -> str:
    if isinstance(name, (list, tuple)):
        text = " ".join(str(part) for part in name)
    elif isinstance(name, str):
        text = name
    else:
        raise CliRegistryError("command name must be a string or a list of words",
                               code="unknown_command")
    return " ".join(text.replace("_", "-").lower().split())


def get_command(name: Any) -> Command:
    """Return the leaf command for a space-joined path such as ``pcb export pos``."""
    normalized = _normalize_name(name)
    command = _registry().get(normalized)
    if command is None:
        raise CliRegistryError(f"unknown kicad-cli leaf command {normalized!r}",
                               code="unknown_command", command=normalized or None)
    return command


# --- option validation ------------------------------------------------------------------------


def _fail(code: str, message: str, command: Command, option: Option | str | None) -> CliRegistryError:
    long = option.long if isinstance(option, Option) else option
    return CliRegistryError(f"{command.name}: {message}", code=code, option=long, command=command.name)


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _normalize_value(command: Command, option: Option, value: Any) -> str:
    vt = option.value_type
    if vt == "string":
        if not isinstance(value, str):
            raise _fail("wrong_type", f"{option.long} expects a string, got {type(value).__name__}",
                        command, option)
        text = value
    elif vt == "int":
        if isinstance(value, bool):
            raise _fail("wrong_type", f"{option.long} expects an integer, got a boolean",
                        command, option)
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, str) and _INT_RE.fullmatch(value):
            text = str(int(value))
        else:
            raise _fail("wrong_type", f"{option.long} expects an integer, got {_shown(value)}",
                        command, option)
    elif vt == "float":
        if isinstance(value, bool):
            raise _fail("wrong_type", f"{option.long} expects a number, got a boolean",
                        command, option)
        if isinstance(value, int):
            text = str(value)
        elif isinstance(value, float) and math.isfinite(value):
            text = repr(value)
        elif isinstance(value, str) and _FLOAT_RE.fullmatch(value) and math.isfinite(float(value)):
            text = repr(float(value))
        else:
            raise _fail("wrong_type", f"{option.long} expects a finite number, got {_shown(value)}",
                        command, option)
    else:  # bool carried as a true/false value (for example --sort-asc)
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, str) and value.lower() in ("true", "false"):
            text = value.lower()
        else:
            raise _fail("wrong_type", f"{option.long} expects true or false, got {_shown(value)}",
                        command, option)

    if any(ch in text for ch in ("\x00", "\r", "\n")):
        raise _fail("invalid_value", f"{option.long} value contains NUL, CR or LF", command, option)
    if len(text) > MAX_VALUE_CHARS:
        raise _fail("invalid_value", f"{option.long} value exceeds {MAX_VALUE_CHARS} characters",
                    command, option)
    if option.choices is not None and text not in option.choices:
        raise _fail("invalid_choice",
                    f"{option.long} must be one of {list(option.choices)}, got {_shown(text)}",
                    command, option)
    if text.lstrip().startswith("-"):
        raise _fail("leading_dash",
                    f"{option.long} value {_shown(text)} starts with '-'; such values are refused "
                    "because they could be read as a flag", command, option)
    if option.long == "--define-var" and not _DEFINE_VAR_RE.fullmatch(text):
        raise _fail("invalid_value", f"{option.long} values must look like KEY=VALUE, got {_shown(text)}",
                    command, option)
    return text


def _emit(command: Command, option: Option, value: Any) -> list[str]:
    if not option.takes_value:
        if value is True:
            return [option.long]
        if value is False:
            return []
        raise _fail("bool_with_value",
                    f"{option.long} is a boolean flag and takes no value, got {_shown(value)}",
                    command, option)
    if option.repeatable:
        if isinstance(value, (list, tuple)):
            values = list(value)
        elif isinstance(value, (set, frozenset, dict, Mapping)):
            raise _fail("wrong_type", f"{option.long} expects a string or a list of strings",
                        command, option)
        else:
            values = [value]
    else:
        if isinstance(value, (list, tuple, set, frozenset, dict, Mapping)):
            raise _fail("not_repeatable", f"{option.long} may not be repeated or given a list",
                        command, option)
        values = [value]
    tokens: list[str] = []
    for item in values:
        if item is None:
            raise _fail("wrong_type", f"{option.long} list contains None", command, option)
        tokens.extend([option.long, _normalize_value(command, option, item)])
    return tokens


def validate_options(command: Command | str, options: Mapping[str, Any] | None) -> list[str]:
    """Validate ``options`` against a leaf command and return normalized argv tokens.

    Keys may be long, alias or short names, with or without leading dashes (underscores stand in
    for hyphens). Flags take ``True``/``False``; value options take one value, repeatable options a
    list. ``None`` means "not given". Tokens are emitted in the command's declaration order with
    the canonical long flag and a separate value token. The positional input is not included.
    Raises ``CliRegistryError`` (see its ``code``) for unknown options, wrong types, bad choices,
    lists for non-repeatable options, booleans given a value, duplicates and unsafe values.
    """
    cmd = command if isinstance(command, Command) else get_command(command)
    if options is None:
        options = {}
    if not isinstance(options, Mapping):
        raise _fail("wrong_type", "options must be a mapping of option name to value", cmd, None)

    chosen: dict[str, Any] = {}
    for key, value in options.items():
        option = cmd.find_option(key)
        if option is None:
            raise _fail("unknown_option", f"unknown option {_shown(key)}", cmd, None)
        if value is None:
            continue
        if option.long in chosen:
            raise _fail("duplicate_option",
                        f"{option.long} was given more than once (aliases count as the same option)",
                        cmd, option)
        chosen[option.long] = value

    tokens: list[str] = []
    for option in cmd.options:
        if option.long in chosen:
            tokens.extend(_emit(cmd, option, chosen[option.long]))
    return tokens
