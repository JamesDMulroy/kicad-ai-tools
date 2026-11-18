"""Library tables and library search done on the table files themselves.

KiCad's API declares GetLibraryTable, AddLibraryTableEntry, UpdateLibraryTableEntry,
DeleteLibraryTableEntry and SearchLibraries but no build implements them. This module gives the same
functions by other means: it reads and writes the s-expression table files (``sym-lib-table``,
``fp-lib-table``, ``design-block-lib-table``) in KiCad's own format, and enumerates a library's items
from its files. Request and response shapes follow the proto messages (``common/commands/library_commands.proto``).

Only tables of a session are touched: the project table is the candidate copy's, the global table is the
session profile's private one (never the owner's real configuration).
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import tempfile
from typing import Any

TYPES = {"LT_SYMBOL": ("sym-lib-table", "sym_lib_table"),
         "LT_FOOTPRINT": ("fp-lib-table", "fp_lib_table"),
         "LT_DESIGN_BLOCK": ("design-block-lib-table", "design_block_lib_table")}
SCOPES = ("LTS_GLOBAL", "LTS_PROJECT", "LTS_BOTH")
DEFAULT_VERSION = "7"
OPTION_SEP = "|"
_VAR = re.compile(r"\$\{([^}]+)\}|\$\(([^)]+)\)")


class LibraryTableError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# --- s-expressions -------------------------------------------------------------------------------

def tokenize(text: str) -> list[Any]:
    """Nested lists of atoms; a quoted string is a ``str`` subclass marked quoted."""
    pos, stack, out = 0, [], []
    current: list[Any] = out
    while pos < len(text):
        c = text[pos]
        if c in " \t\r\n":
            pos += 1
        elif c == "(":
            node: list[Any] = []
            current.append(node)
            stack.append(current)
            current = node
            pos += 1
        elif c == ")":
            if not stack:
                raise LibraryTableError("LCS_ERROR", "unbalanced ')' in library table")
            current = stack.pop()
            pos += 1
        elif c == '"':
            pos += 1
            buf = []
            while pos < len(text) and text[pos] != '"':
                if text[pos] == "\\" and pos + 1 < len(text):
                    pos += 1
                    buf.append({"n": "\n", "r": "\r"}.get(text[pos], text[pos]))
                else:
                    buf.append(text[pos])
                pos += 1
            if pos >= len(text):
                raise LibraryTableError("LCS_ERROR", "unterminated string in library table")
            pos += 1
            current.append(Quoted("".join(buf)))
        else:
            end = pos
            while end < len(text) and text[end] not in " \t\r\n()":
                end += 1
            current.append(text[pos:end])
            pos = end
    if stack:
        raise LibraryTableError("LCS_ERROR", "unbalanced '(' in library table")
    return out


class Quoted(str):
    """A string that was quoted in the file."""


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r") + '"'


def _atom(node: Any) -> str:
    if isinstance(node, Quoted):
        return quote(node)
    if isinstance(node, list):
        return "(" + " ".join(_atom(n) for n in node) + ")"
    return str(node)


# --- options -------------------------------------------------------------------------------------

def parse_options(text: str) -> dict[str, str]:
    """KiCad's ``name=value|name2`` option string (``\\|`` escapes the separator in a value)."""
    props: dict[str, str] = {}
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        pair = []
        while pos < len(text):
            if text[pos] == "\\" and pos + 1 < len(text) and text[pos + 1] == OPTION_SEP:
                pair.append(OPTION_SEP)
                pos += 2
            elif text[pos] == OPTION_SEP:
                pos += 1
                break
            else:
                pair.append(text[pos])
                pos += 1
        joined = "".join(pair)
        if joined:
            name, eq, value = joined.partition("=")
            props[name] = value if eq else ""
    return props


def format_options(options: dict[str, str]) -> str:
    parts = []
    for name in sorted(options):
        value = options[name]
        parts.append(name + ("=" + value.replace(OPTION_SEP, "\\" + OPTION_SEP) if value else ""))
    return OPTION_SEP.join(parts)


# --- tables --------------------------------------------------------------------------------------

class Table:
    """One library table file: its rows, in file order, and the file's own version."""

    def __init__(self, path: Path, kind: str):
        self.path, self.kind = Path(path), kind
        self.version = DEFAULT_VERSION
        self.rows: list[dict[str, Any]] = []
        self.valid, self.error = True, None
        self.exists = self.path.exists()
        if self.exists:
            self._read()

    def _read(self) -> None:
        try:
            tree = tokenize(self.path.read_text(encoding="utf-8"))
            root = next(n for n in tree if isinstance(n, list))
            if not root or root[0] != TYPES[self.kind][1]:
                raise LibraryTableError("LCS_ERROR", f"{self.path.name} is not a {TYPES[self.kind][1]}")
            for child in root[1:]:
                if not isinstance(child, list) or not child:
                    continue
                if child[0] == "version" and len(child) > 1:
                    self.version = str(child[1])
                elif child[0] == "lib":
                    self.rows.append(_row_of(child))
        except (OSError, StopIteration, LibraryTableError) as exc:
            self.valid, self.error = False, str(getattr(exc, "message", exc) or "empty or unreadable table")
            self.rows = []

    def find(self, nickname: str) -> int:
        return next((i for i, r in enumerate(self.rows) if r["name"] == nickname), -1)

    def text(self) -> str:
        lines = [f"({TYPES[self.kind][1]}", f"  (version {self.version})"]
        for row in self.rows:
            uri = row["uri"].replace("\\", "/")
            parts = (f"(name {quote(row['name'])})(type {quote(row['type'])})(uri {quote(uri)})"
                     f"(options {quote(row['options'])})(descr {quote(row['descr'])})")
            if row["disabled"]:
                parts += "(disabled)"
            if row["hidden"]:
                parts += "(hidden)"
            parts += "".join(_atom(x) for x in row["extra"])
            lines.append(f"  (lib {parts})")
        return "\n".join(lines) + "\n)\n"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(self.text())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self.exists = True


def _row_of(node: list[Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"name": "", "type": "KiCad", "uri": "", "options": "", "descr": "",
                           "disabled": False, "hidden": False, "extra": []}
    for child in node[1:]:
        if not isinstance(child, list) or not child:
            continue
        key = child[0]
        if key in ("name", "type", "uri", "options", "descr"):
            row[key] = str(child[1]) if len(child) > 1 else ""
        elif key in ("disabled", "hidden"):
            row[key] = len(child) < 2 or str(child[1]).lower() not in ("no", "false", "0")
        else:
            row["extra"].append(child)
    return row


def entry_of(row: dict[str, Any], kind: str, scope: str, writable: bool) -> dict[str, Any]:
    """The row as a ``LibraryTableEntry`` (JSON form of the proto)."""
    entry: dict[str, Any] = {"nickname": row["name"], "uri": row["uri"], "type": kind, "scope": scope,
                             "writable": writable}
    if row["descr"]:
        entry["description"] = row["descr"]
    if row["options"]:
        entry["options"] = parse_options(row["options"])
    if row["disabled"]:
        entry["disabled"] = True
    if row["hidden"]:
        entry["hidden"] = True
    return entry


class TableSet:
    """Where a session's tables live: the candidate project folder and the profile's config folder."""

    def __init__(self, project_dir: Path, config_dir: Path | None, app_root: Path | None = None):
        self.project_dir, self.config_dir, self.app_root = Path(project_dir), config_dir, app_root

    @classmethod
    def of(cls, session) -> TableSet:
        config = None
        profile = getattr(session, "profile", None)
        if profile is not None:
            config = Path(profile) / "config" / session._settings_version()
        return cls(session.candidate.work_dir, config, getattr(session, "app_root", None))

    def path(self, kind: str, scope: str) -> Path:
        name = TYPES[kind][0]
        if scope == "LTS_PROJECT":
            return self.project_dir / name
        if self.config_dir is None:
            raise LibraryTableError("LCS_ERROR", "this session has no private global tables "
                                                 "(open a GUI session to use the global scope)")
        return self.config_dir / name

    def table(self, kind: str, scope: str) -> Table:
        return Table(self.path(kind, scope), kind)

    # variable expansion for the "substituted" form of a URI
    def expand(self, uri: str) -> str:
        def sub(match: re.Match) -> str:
            name = match.group(1) or match.group(2)
            if name == "KIPRJMOD":
                return str(self.project_dir)
            if name in os.environ:
                return os.environ[name]
            for suffix, folder in (("SYMBOL_DIR", "symbols"), ("FOOTPRINT_DIR", "footprints"),
                                   ("3DMODEL_DIR", "3dmodels"), ("DESIGN_BLOCK_DIR", "design_blocks"),
                                   ("TEMPLATE_DIR", "template")):
                if name.startswith("KICAD") and name.endswith(suffix) and self.app_root:
                    return str(self.app_root / "share" / "kicad" / folder)
            return match.group(0)
        return _VAR.sub(sub, uri)


def _check_kind(kind: str) -> str:
    if kind not in TYPES:
        raise LibraryTableError("LCS_ERROR", f"library type must be one of {sorted(TYPES)}, not {kind!r}")
    return kind


def _check_scope(scope: str, allow_both: bool) -> str:
    if scope not in SCOPES or (scope == "LTS_BOTH" and not allow_both):
        raise LibraryTableError("LCS_ERROR", "scope must be LTS_GLOBAL or LTS_PROJECT"
                                             + (" (or LTS_BOTH)" if allow_both else "; LTS_BOTH is invalid here"))
    return scope


def _writable(tables: TableSet, uri: str) -> bool:
    path = Path(tables.expand(uri))
    if not path.exists():
        return False
    return os.access(path, os.W_OK)


def _table_info(table: Table, scope: str) -> dict[str, Any]:
    info: dict[str, Any] = {"path": str(table.path), "scope": scope, "valid": table.valid}
    if table.error:
        info["error_message"] = table.error
    return info


def get_library_table(tables: TableSet, type: str, scope: str = "LTS_BOTH",
                      substituted: bool = False) -> dict[str, Any]:
    """GetLibraryTable: ``{"tables": [LibraryTable], "rows": [LibraryTableEntry]}`` (project rows first)."""
    kind, scope = _check_kind(type), _check_scope(scope, True)
    out_tables, rows = [], []
    for one in (("LTS_PROJECT", "LTS_GLOBAL") if scope == "LTS_BOTH" else (scope,)):
        try:
            table = tables.table(kind, one)
        except LibraryTableError:
            if scope == "LTS_BOTH":
                continue
            raise
        out_tables.append(_table_info(table, one))
        for row in table.rows:
            entry = entry_of(row, kind, one, _writable(tables, row["uri"]))
            if substituted:
                entry["uri"] = tables.expand(entry["uri"])
            rows.append(entry)
    return {"tables": out_tables, "rows": rows}


def _row_from_entry(entry: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    row = base or {"name": "", "type": "KiCad", "uri": "", "options": "", "descr": "",
                   "disabled": False, "hidden": False, "extra": []}
    row = dict(row)
    row["name"] = entry.get("nickname", row["name"])
    if "uri" in entry:
        row["uri"] = entry["uri"]
    if "description" in entry:
        row["descr"] = entry["description"]
    if "options" in entry:
        row["options"] = format_options(entry["options"])
    row["disabled"] = bool(entry.get("disabled", row["disabled"]))
    row["hidden"] = bool(entry.get("hidden", row["hidden"]))
    return row


def _status(code: str, message: str | None = None, table: Table | None = None, scope: str | None = None) -> dict:
    out: dict[str, Any] = {"code": code}
    if message:
        out["error_message"] = message
    if table is not None and code == "LCS_OK":
        out["table"] = _table_info(table, scope or "")
    return out


def _mutable(tables: TableSet, entry_type: str, scope: str) -> tuple[Table, str]:
    kind, scope = _check_kind(entry_type), _check_scope(scope, False)
    table = tables.table(kind, scope)
    if not table.valid:
        raise LibraryTableError("LCS_ERROR", f"{table.path} cannot be parsed: {table.error}")
    if table.exists and not os.access(table.path, os.W_OK):
        raise LibraryTableError("LCS_READ_ONLY", f"{table.path} is read-only")
    return table, scope


def add_library_table_entry(tables: TableSet, entry: dict[str, Any], scope: str) -> dict[str, Any]:
    """AddLibraryTableEntry: append a row. LCS_ALREADY_EXISTS if the nickname is taken."""
    try:
        if not entry.get("nickname") or not entry.get("uri"):
            raise LibraryTableError("LCS_ERROR", "an entry needs a nickname and a uri")
        table, scope = _mutable(tables, entry.get("type", ""), scope)
        if table.find(entry["nickname"]) >= 0:
            return _status("LCS_ALREADY_EXISTS", f"{entry['nickname']!r} is already in {table.path.name}")
        table.rows.append(_row_from_entry(entry))
        table.save()
        return _status("LCS_OK", table=table, scope=scope)
    except LibraryTableError as exc:
        return _status(exc.code, exc.message)


def update_library_table_entry(tables: TableSet, entry: dict[str, Any], scope: str) -> dict[str, Any]:
    """UpdateLibraryTableEntry: replace the row with this nickname (fields the entry omits are kept)."""
    try:
        table, scope = _mutable(tables, entry.get("type", ""), scope)
        index = table.find(entry.get("nickname", ""))
        if index < 0:
            return _status("LCS_NOT_FOUND", f"{entry.get('nickname')!r} is not in {table.path.name}")
        table.rows[index] = _row_from_entry(entry, table.rows[index])
        table.save()
        return _status("LCS_OK", table=table, scope=scope)
    except LibraryTableError as exc:
        return _status(exc.code, exc.message)


def delete_library_table_entry(tables: TableSet, type: str, nickname: str, scope: str) -> dict[str, Any]:
    """DeleteLibraryTableEntry: remove the row; the library's files are left alone."""
    try:
        table, scope = _mutable(tables, type, scope)
        index = table.find(nickname)
        if index < 0:
            return _status("LCS_NOT_FOUND", f"{nickname!r} is not in {table.path.name}")
        del table.rows[index]
        table.save()
        return _status("LCS_OK", table=table, scope=scope)
    except LibraryTableError as exc:
        return _status(exc.code, exc.message)


# --- search --------------------------------------------------------------------------------------

_PROP = re.compile(r'\(property\s+"(Description|ki_keywords|ki_description|Footprint)"\s+"((?:[^"\\]|\\.)*)"')


def _symbol_items(path: Path) -> list[dict[str, str]]:
    """Top-level symbols of a .kicad_sym with their description and keywords."""
    text = path.read_text(encoding="utf-8", errors="replace")
    items = []
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'(?m)^(?:\t| {2})\(symbol\s+"((?:[^"\\]|\\.)*)"', text)]
    for i, (start, name) in enumerate(starts):
        if re.search(r"_\d+_\d+$", name):
            continue  # a unit of the previous symbol, not a symbol
        end = starts[i + 1][0] if i + 1 < len(starts) else len(text)
        body = text[start:end]
        props = dict(_PROP.findall(body))
        units = {int(m.group(1)) for m in re.finditer(r'\(symbol\s+"' + re.escape(name) + r'_(\d+)_\d+"', text) if int(m.group(1))}
        items.append({"name": name, "description": props.get("Description", props.get("ki_description", "")),
                      "keywords": props.get("ki_keywords", ""), "footprint": props.get("Footprint", ""),
                      "units": str(len(units) or 1)})
    return items


def _footprint_items(path: Path) -> list[dict[str, str]]:
    items = []
    for mod in sorted(path.glob("*.kicad_mod")):
        head = mod.read_text(encoding="utf-8", errors="replace")[:4000]
        descr = re.search(r'\(descr\s+"((?:[^"\\]|\\.)*)"', head)
        tags = re.search(r'\(tags\s+"((?:[^"\\]|\\.)*)"', head)
        items.append({"name": mod.stem, "description": descr.group(1) if descr else "",
                      "keywords": tags.group(1) if tags else ""})
    return items


def _block_items(path: Path) -> list[dict[str, str]]:
    return [{"name": d.stem, "description": "", "keywords": ""} for d in sorted(path.glob("*.kicad_block"))]


def library_items(path: Path, kind: str) -> list[dict[str, str]]:
    if kind == "LT_SYMBOL":
        if path.is_file():
            return _symbol_items(path)
        return [i for f in sorted(path.glob("*.kicad_sym")) for i in _symbol_items(f)] if path.is_dir() else []
    if not path.is_dir():
        return []
    return _footprint_items(path) if kind == "LT_FOOTPRINT" else _block_items(path)


def _wildcard_regex(term: str) -> str:
    """KiCad's wildcard pattern as a regex (``?`` any character, ``*`` any run, the rest literal)."""
    return "".join("." if c == "?" else ".*" if c == "*" else re.escape(c) for c in term)


def term_matches(term: str, text: str) -> bool:
    """One chooser term against one search text (both lower case): EDA_COMBINED_MATCHER's library-item
    context, i.e. an explicit regex (``^...$`` or ``/.../``), the wildcard form, or a plain substring,
    each found anywhere in the text. (Its relational matcher, ``key<value``, is not applied.)"""
    if term in text:
        return True
    patterns = [_wildcard_regex(term)]
    if term.startswith("^") and term.endswith("$"):
        patterns.append(term)
    elif term.startswith("/"):
        patterns.append(term[1:-1] if term.endswith("/") and len(term) > 1 else term[1:])
    for pattern in patterns:
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            continue
    return False


def search_terms(nickname: str, item: dict[str, str], kind: str) -> list[str]:
    """The texts KiCad's chooser matches a query against (lib_symbol.cpp, footprint.cpp, design_block.cpp):
    library nickname, name, ``lib:name``, each keyword, the keywords, the description, and for a symbol its
    footprint field and ``units=N``."""
    terms = [nickname, item["name"], f"{nickname}:{item['name']}", *item["keywords"].split(), item["keywords"],
             item["description"]]
    if kind == "LT_SYMBOL":
        terms += [item.get("footprint", ""), f"units={item.get('units', '1')}"]
    return [t.strip().lower() for t in terms if t.strip()]


def matches(query: str, terms: list[str]) -> bool:
    """The chooser filter: every whitespace-separated term of the query must match some search text."""
    return all(any(term_matches(q, t) for t in terms) for q in query.lower().split())


def search_libraries(tables: TableSet, type: str, query: str, names_only: bool = False,
                     libraries: list[str] | None = None) -> dict[str, Any]:
    """SearchLibraries: ``{"items": [LibraryIdentifier]}`` for every enabled library of the type in the
    project and global tables (a project row hides a global row with the same nickname), filtered as
    KiCad's library chooser filters (see ``search_terms`` and ``term_matches``). ``names_only`` matches
    the item name alone instead."""
    kind = _check_kind(type)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for scope in ("LTS_PROJECT", "LTS_GLOBAL"):
        try:
            table = tables.table(kind, scope)
        except LibraryTableError:
            continue
        for row in table.rows:
            if row["name"] in seen:
                continue
            seen.add(row["name"])
            if row["disabled"] or row["type"] not in ("KiCad", "Legacy") or (libraries and row["name"] not in libraries):
                continue
            for item in library_items(Path(tables.expand(row["uri"])), kind):
                texts = [item["name"].lower()] if names_only else search_terms(row["name"], item, kind)
                if not query or matches(query, texts):
                    items.append({"library_nickname": row["name"], "entry_name": item["name"]})
    return {"items": items}
