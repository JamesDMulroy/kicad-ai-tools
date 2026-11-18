"""L3 tier D1: offline, non-executing inspection of plugin packages.

Design reference: the project's internal plugin and UI isolation design (not published), sections
A2, A3 and A3c (D1 offline closure check).

ABSOLUTE RULE: nothing from an inspected package is ever imported, compiled,
executed or launched.  This module only

* reads bytes (``open(..., "rb")``, ``zipfile`` member reads in memory),
* hashes them with ``hashlib``,
* parses decoded text as data (``json``, line parsing, ``email.parser`` for
  wheel ``METADATA`` headers), and
* calls ``ast.parse`` on decoded source text (parsing only, never
  ``compile``/``exec``/``eval`` of package content).

There is no network access, no subprocess, no extraction to disk and no
``sys.path`` change.  Static hints are hints, not a safety verdict.

Public API: :class:`PluginInspectError`, :func:`inspect_package`,
:func:`closure_check`.
"""

# covers: L3-2
from __future__ import annotations

import ast
import email.parser
import hashlib
import json
import os
import re
import stat
import struct
from typing import Any
import zipfile
import zlib

# ---------------------------------------------------------------------------
# Limits (module-level defaults; every public function takes keyword overrides)
# ---------------------------------------------------------------------------

MAX_TOTAL_BYTES = 256 * 1024 * 1024  # total uncompressed bytes of an inspected package
MAX_MEMBER_BYTES = 64 * 1024 * 1024  # uncompressed bytes of one member/file
MAX_MEMBERS = 5000  # member count of an inspected package
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024  # size of the archive file itself
MAX_CENTRAL_DIRECTORY_BYTES = 8 * 1024 * 1024  # zip central directory size
MAX_TEXT_BYTES = 2 * 1024 * 1024  # largest member retained in memory for text analysis
MAX_RETAINED_BYTES = 32 * 1024 * 1024  # total bytes retained in memory for text analysis
MAX_WHEEL_MEMBERS = 50_000  # member count tolerated in a dependency wheel
READ_CHUNK = 64 * 1024  # bounded read size

PINNED_KICAD_PREFIX = "10.0"
STATIC_HINTS_NOTE = "static hints, not a safety verdict"
PRESENT_NOT_EXECUTED = "present, not executed"
MAX_IPC_ACTIONS = 200
MAX_ENTRY_POINTS = 200

_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_IMPORT_HINTS = {
    "urllib": "network_import",
    "requests": "network_import",
    "socket": "network_import",
    "http": "network_import",
    "subprocess": "subprocess_import",
    "ctypes": "ctypes_import",
}
_HOOK_BASENAMES = frozenset(
    {
        "setup.py",
        "pyproject.toml",
        "setup.cfg",
        "entry_points.txt",
        "sitecustomize.py",
        "usercustomize.py",
    }
)
_SO_RE = re.compile(r"\.so(\.[0-9]+)*$")

# File-dialog call names (A3 "file-dialog paths"), matched on dotted call names from ast only.
_TK_DIALOG_FUNCTIONS = frozenset(
    {
        "askopenfilename",
        "askopenfilenames",
        "askopenfile",
        "askopenfiles",
        "asksaveasfilename",
        "asksaveasfile",
        "askdirectory",
    }
)
_TK_DIALOG_CLASSES = frozenset({"Open", "SaveAs", "Directory"})  # tkinter.filedialog.<Class>
_WX_DIALOG_NAMES = frozenset(
    {"FileDialog", "DirDialog", "FileSelector", "DirSelector", "LoadFileSelector", "SaveFileSelector"}
)
_PCBNEW_DIALOG_HELPERS = frozenset(
    {"getopenfilename", "getsavefilename", "getdirectory", "openfiledialog", "savefiledialog", "choosefile"}
)


class PluginInspectError(Exception):
    """Raised for invalid inputs (containment violations, bad arguments).

    ``code`` is a stable machine-readable code: ``quarantine_root_invalid``, ``path_invalid``,
    ``path_not_relative``, ``path_traversal``, ``path_outside_quarantine``, ``path_not_found``,
    ``path_wrong_kind``, ``wheel_outside_quarantine``, ``wheel_invalid``, ``wheels_invalid`` or
    ``limit_invalid`` (default ``plugin_inspect_error``).
    """

    def __init__(self, message: str, code: str = "plugin_inspect_error"):
        super().__init__(message)
        self.code = code


class _Reject(Exception):
    """Internal: carries one or more structured archive rejections."""

    def __init__(self, rejections: list[dict[str, Any]]):
        super().__init__("; ".join(f"{r['reason']}: {r['member']}" for r in rejections))
        self.rejections = rejections


_CAP_REASONS = frozenset(
    {"archive_too_large", "central_directory_too_large", "zip64_not_supported"}
)


def _rejection_code(reason: str) -> str:
    """``cap_exceeded`` for size/count caps, otherwise ``archive_member_rejected``."""
    if reason in _CAP_REASONS or (reason.startswith("over_") and reason.endswith("cap")):
        return "cap_exceeded"
    return "archive_member_rejected"


def _rej(member: str | None, reason: str, detail: str = "") -> dict[str, Any]:
    return {"member": member, "reason": reason, "code": _rejection_code(reason), "detail": detail}


def _known_local_appdata() -> str:
    """The OS LocalAppData known folder; "" when unavailable (then the root is refused, never guessed)."""
    from kicad_mcp.adaptive_editor import kicad_env
    folder = kicad_env.windows_known_folder(kicad_env.FOLDERID_LOCAL_APPDATA)
    return str(folder) if folder else ""


def resolve_quarantine_root(env: Any = None) -> tuple[str, str]:
    """Host-configured quarantine root as ``(root, source)``, created if absent.

    ``KICAD_MCP_QUARANTINE_ROOT`` if set and non-empty (source ``KICAD_MCP_QUARANTINE_ROOT``), else
    ``<LOCALAPPDATA>/kicad-mcp/quarantine`` (source ``LOCALAPPDATA_default``; the OS LocalAppData
    known folder when ``LOCALAPPDATA`` is empty, and ``quarantine_root_invalid`` if that fails too).  Raises ``quarantine_root_invalid`` when the root is not an
    absolute path, cannot be created or is not a directory.
    """
    environ = os.environ if env is None else env
    configured = environ.get("KICAD_MCP_QUARANTINE_ROOT")
    if not configured and env is None:  # the per-machine config applies to the real process environment only
        from kicad_mcp.adaptive_editor import kicad_env
        try:
            stored = kicad_env.load_config().get("quarantine_root")
        except kicad_env.ConfigError as exc:
            raise PluginInspectError(f"quarantine root unavailable: {exc}", "quarantine_root_invalid") from exc
        if stored:
            return _checked_root(str(stored), "per_machine_config")
    if configured:
        root, source = configured, "KICAD_MCP_QUARANTINE_ROOT"
    else:
        base = environ.get("LOCALAPPDATA") or _known_local_appdata()
        root = os.path.join(base, "kicad-mcp", "quarantine") if base else ""
        source = "LOCALAPPDATA_default"
    return _checked_root(root, source)


def _checked_root(root: Any, source: str) -> tuple[str, str]:
    if not isinstance(root, str) or "\x00" in root or not os.path.isabs(root):
        raise PluginInspectError("quarantine root must be an absolute text path", "quarantine_root_invalid")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        raise PluginInspectError(f"quarantine root is unusable: {exc}", "quarantine_root_invalid") from exc
    if not os.path.isdir(root):
        raise PluginInspectError("quarantine root is not a directory", "quarantine_root_invalid")
    return os.path.abspath(root), source


# ---------------------------------------------------------------------------
# Containment
# ---------------------------------------------------------------------------


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _is_link_like(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or _is_reparse(st)


def _within(candidate: str, root: str) -> bool:
    """True when ``candidate`` is a strict descendant of ``root``."""
    c = os.path.normcase(candidate)
    r = os.path.normcase(root)
    return c != r and c.startswith(r.rstrip("\\/") + os.sep)


def _prepare_root(quarantine_root: Any) -> tuple[str, str]:
    try:
        root = os.fspath(quarantine_root)
    except TypeError as exc:
        raise PluginInspectError("quarantine_root must be a path", "quarantine_root_invalid") from exc
    if not isinstance(root, str) or not root or "\x00" in root:
        raise PluginInspectError("quarantine_root must be a non-empty text path", "quarantine_root_invalid")
    try:
        st = os.stat(root)
    except OSError as exc:
        raise PluginInspectError(f"quarantine_root is not accessible: {exc}", "quarantine_root_invalid") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise PluginInspectError("quarantine_root is not an existing directory", "quarantine_root_invalid")
    return os.path.abspath(root), os.path.realpath(root)


def _resolve_inside(path: Any, root_abs: str, root_real: str, *, wheel: bool = False) -> str:
    """Resolve a root-relative ``path`` strictly inside the quarantine root, no links, no '..'.

    Absolute, drive, UNC and rooted paths are refused (``path_not_relative``); the quarantine root
    is the only base.  With ``wheel=True`` an escape is reported as ``wheel_outside_quarantine``.
    """
    outside = "wheel_outside_quarantine" if wheel else "path_outside_quarantine"
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise PluginInspectError("path must be a path string", "path_invalid") from exc
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise PluginInspectError("path must be a non-empty text path without NUL", "path_invalid")
    if raw.startswith(("/", "\\")) or _DRIVE_RE.match(raw) or os.path.isabs(raw):
        raise PluginInspectError(
            "path must be relative to the quarantine root (no absolute, drive, UNC or rooted paths)",
            "path_not_relative",
        )
    if ".." in re.split(r"[\\/]+", raw):
        raise PluginInspectError("path contains a '..' segment", "path_traversal")
    cand_abs = os.path.abspath(os.path.join(root_abs, raw))
    base = None
    for option in (root_abs, root_real):
        if _within(cand_abs, option):
            base = option
            break
    if base is None:
        raise PluginInspectError("path is not strictly inside the quarantine root", outside)
    try:
        rel = os.path.relpath(cand_abs, base)
    except ValueError as exc:
        raise PluginInspectError("path is not strictly inside the quarantine root", outside) from exc
    current = base
    for part in rel.split(os.sep):
        current = os.path.join(current, part)
        try:
            st = os.lstat(current)
        except OSError as exc:
            raise PluginInspectError(f"path does not exist or is not accessible: {exc}", "path_not_found") from exc
        if _is_link_like(st):
            raise PluginInspectError("path traverses a symlink, junction or reparse point", outside)
    if not _within(os.path.realpath(current), root_real):
        raise PluginInspectError("path resolves outside the quarantine root", outside)
    return current


# ---------------------------------------------------------------------------
# Member name rules (shared by archives and directories)
# ---------------------------------------------------------------------------


def _is_device_name(segment: str) -> bool:
    stem = segment.split(".", 1)[0].rstrip(" ").upper()
    return stem in _DEVICE_NAMES


def _name_problem(name: str) -> str | None:
    """Return a reason code when ``name`` is not an acceptable member name."""
    if "\x00" in name:
        return "nul"
    if name.startswith("//") or name.startswith("\\\\"):
        return "unc"
    if "\\" in name:
        return "backslash"
    if name.startswith("/"):
        return "absolute"
    if _DRIVE_RE.match(name):
        return "drive_letter"
    body = name[:-1] if name.endswith("/") else name
    if body == "":
        return "empty_name"
    for segment in body.split("/"):
        if segment == "":
            return "empty_segment"
        if segment == "..":
            return "dotdot"
        if segment == ".":
            return "dot_segment"
        if ":" in segment:
            return "colon_in_name"
        if _is_device_name(segment):
            return "device_name"
        if segment.endswith((".", " ")):
            return "trailing_dot_or_space"
    return None


def _norm_key(name: str) -> str:
    body = name[:-1] if name.endswith("/") else name
    return body.casefold()


def _namespace_problems(items: list[tuple[str, bool]]) -> list[dict[str, Any]]:
    """Duplicate (case-insensitive) and directory-vs-file clashes."""
    problems: list[dict[str, Any]] = []
    seen: dict[str, tuple[str, bool]] = {}
    for name, is_dir in items:
        key = _norm_key(name)
        previous = seen.get(key)
        if previous is None:
            seen[key] = (name, is_dir)
        elif previous[1] != is_dir:
            problems.append(_rej(name, "dir_file_clash", f"clashes with {previous[0]!r}"))
        else:
            problems.append(_rej(name, "duplicate_name", f"duplicates {previous[0]!r}"))
    file_keys = {key for key, (_, is_dir) in seen.items() if not is_dir}
    for key, (name, _) in seen.items():
        parts = key.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in file_keys:
                problems.append(_rej(name, "dir_file_clash", "a parent path is also a file"))
                break
    return problems


# ---------------------------------------------------------------------------
# Bounded reading / collection
# ---------------------------------------------------------------------------


class _Budget:
    def __init__(self, max_total: int, max_member: int):
        self.max_total = max_total
        self.max_member = max_member
        self.read = 0
        self.retained = 0
        self.declared = 0


class _Collected:
    def __init__(self) -> None:
        self.files: list[dict[str, Any]] = []
        self.dirs: set[str] = set()
        self.texts: dict[str, bytes] = {}
        self.text_skipped: dict[str, str] = {}


def _wants_text(path: str) -> bool:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if low.endswith((".py", ".pyw", ".pth")):
        return True
    if base in ("requirements.txt", "pyproject.toml", "entry_points.txt"):
        return True
    return path in ("metadata.json", "plugin.json")


def _decide_keep(path: str, declared: int, budget: _Budget, collected: _Collected) -> bool:
    if not _wants_text(path):
        return False
    if declared > MAX_TEXT_BYTES:
        collected.text_skipped[path] = "over_text_cap"
        return False
    if budget.retained + declared > MAX_RETAINED_BYTES:
        collected.text_skipped[path] = "over_retained_budget"
        return False
    return True


def _consume(stream: Any, name: str, declared: int, budget: _Budget, keep: bool):
    """Read ``stream`` in bounded chunks, hashing and enforcing every cap."""
    limit = min(declared, budget.max_member)
    digest = hashlib.sha256()
    count = 0
    chunks: list[bytes] | None = [] if keep else None
    while True:
        chunk = stream.read(READ_CHUNK)
        if not chunk:
            break
        count += len(chunk)
        budget.read += len(chunk)
        if count > limit:
            if count > declared:
                raise _Reject([_rej(name, "size_lie", f"read {count} bytes, header declared {declared}")])
            raise _Reject([_rej(name, "over_member_cap", f"read {count} bytes exceeds {budget.max_member}")])
        if budget.read > budget.max_total:
            raise _Reject([_rej(name, "over_total_cap", f"read {budget.read} bytes exceeds {budget.max_total}")])
        digest.update(chunk)
        if chunks is not None:
            chunks.append(chunk)
    if count != declared:
        raise _Reject([_rej(name, "size_lie", f"header declared {declared}, read {count}")])
    data = b"".join(chunks) if chunks is not None else None
    if data is not None:
        budget.retained += len(data)
    return digest.hexdigest(), count, data


def _hash_file(path: str, cap: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise _Reject([_rej(None, "archive_too_large", f"archive exceeds {cap} bytes")])
            digest.update(chunk)
    return digest.hexdigest()


def _orig(info: zipfile.ZipInfo) -> str:
    return getattr(info, "orig_filename", info.filename)


def _precheck_zip(path: str, size: int, max_members: int) -> tuple[str, str] | None:
    """Look at the end-of-central-directory record before zipfile parses it."""
    tail_len = min(size, 22 + 65535)
    with open(path, "rb") as handle:
        handle.seek(size - tail_len)
        tail = handle.read(tail_len)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        return "corrupt_archive", "end of central directory record not found"
    _, disk, cd_disk, _n_disk, n_total, size_cd, off_cd, _clen = struct.unpack(
        "<4s4H2LH", tail[index : index + 22]
    )
    if disk or cd_disk:
        return "corrupt_archive", "multi-disk archives are not supported"
    if n_total == 0xFFFF or size_cd == 0xFFFFFFFF or off_cd == 0xFFFFFFFF:
        return "zip64_not_supported", "zip64 archives exceed every cap of this inspector"
    if n_total > max_members:
        return "over_count_cap", f"{n_total} members exceeds {max_members}"
    if size_cd > MAX_CENTRAL_DIRECTORY_BYTES:
        return "central_directory_too_large", f"central directory of {size_cd} bytes"
    return None


def _validate_infos(infos: list[zipfile.ZipInfo], limits: dict[str, int]) -> list[dict[str, Any]]:
    if len(infos) > limits["max_members"]:
        return [_rej(None, "over_count_cap", f"{len(infos)} members exceeds {limits['max_members']}")]
    problems: list[dict[str, Any]] = []
    items: list[tuple[str, bool]] = []
    total = 0
    for info in infos:
        name = _orig(info)
        reason = _name_problem(name)
        if reason:
            problems.append(_rej(name, reason))
            continue
        is_dir = name.endswith("/")
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode)
        if kind == stat.S_IFLNK:
            problems.append(_rej(name, "symlink_member"))
            continue
        if info.external_attr & _REPARSE_POINT:
            problems.append(_rej(name, "reparse_point_member"))
            continue
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            problems.append(_rej(name, "special_file_member"))
            continue
        if info.flag_bits & 0x1:
            problems.append(_rej(name, "encrypted_member"))
            continue
        if not is_dir:
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                problems.append(_rej(name, "unsupported_compression", str(info.compress_type)))
                continue
            if info.file_size > limits["max_member_bytes"]:
                problems.append(
                    _rej(name, "over_member_cap", f"declares {info.file_size} bytes > {limits['max_member_bytes']}")
                )
                continue
            total += info.file_size
        items.append((name, is_dir))
    if total > limits["max_total_bytes"]:
        problems.append(_rej(None, "over_total_cap", f"declared {total} bytes > {limits['max_total_bytes']}"))
    problems.extend(_namespace_problems(items))
    return problems


def _collect_zip(target: str, size: int, limits: dict[str, int], budget: _Budget, collected: _Collected) -> str:
    if size > limits["max_archive_bytes"]:
        raise _Reject([_rej(None, "archive_too_large", f"{size} bytes > {limits['max_archive_bytes']}")])
    try:
        problem = _precheck_zip(target, size, limits["max_members"])
    except OSError as exc:
        raise _Reject([_rej(None, "corrupt_archive", f"{type(exc).__name__}: {exc}")]) from exc
    if problem:
        raise _Reject([_rej(None, problem[0], problem[1])])
    try:
        archive = zipfile.ZipFile(target)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError) as exc:
        raise _Reject([_rej(None, "corrupt_archive", f"{type(exc).__name__}: {exc}")]) from exc
    with archive:
        infos = archive.infolist()
        problems = _validate_infos(infos, limits)
        if problems:
            raise _Reject(problems)
        for info in sorted(infos, key=_orig):
            name = _orig(info)
            if name.endswith("/"):
                collected.dirs.add(name[:-1])
                continue
            declared = info.file_size
            budget.declared += declared
            keep = _decide_keep(name, declared, budget, collected)
            try:
                with archive.open(info, "r") as stream:
                    digest, count, data = _consume(stream, name, declared, budget, keep)
            except _Reject:
                raise
            except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError, ValueError) as exc:
                raise _Reject([_rej(name, "corrupt_member", f"{type(exc).__name__}: {exc}")]) from exc
            collected.files.append({"path": name, "size": count, "sha256": digest})
            if data is not None:
                collected.texts[name] = data
    return _hash_file(target, limits["max_archive_bytes"])


def _collect_directory(root: str, limits: dict[str, int], budget: _Budget, collected: _Collected) -> None:
    rejections: list[dict[str, Any]] = []
    items: list[tuple[str, bool]] = []
    files: list[tuple[str, str, int]] = []
    stack: list[tuple[str, str]] = [("", root)]
    count = 0
    stop = False
    while stack and not stop:
        rel, current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                entries = sorted(iterator, key=lambda e: e.name)
        except OSError as exc:
            rejections.append(_rej(rel or ".", "unreadable_directory", f"{type(exc).__name__}: {exc}"))
            continue
        for entry in entries:
            count += 1
            if count > limits["max_members"]:
                rejections.append(_rej(None, "over_count_cap", f"more than {limits['max_members']} members"))
                stop = True
                break
            relname = f"{rel}/{entry.name}" if rel else entry.name
            reason = _name_problem(relname)
            if reason:
                rejections.append(_rej(relname, reason))
                continue
            try:
                st = os.lstat(entry.path)
            except OSError as exc:
                rejections.append(_rej(relname, "unreadable_member", f"{type(exc).__name__}: {exc}"))
                continue
            if stat.S_ISLNK(st.st_mode):
                rejections.append(_rej(relname, "symlink_member"))
            elif _is_reparse(st):
                rejections.append(_rej(relname, "reparse_point_member"))
            elif stat.S_ISDIR(st.st_mode):
                items.append((relname, True))
                stack.append((relname, entry.path))
            elif stat.S_ISREG(st.st_mode):
                if st.st_nlink > 1:
                    rejections.append(_rej(relname, "hardlink_member", f"{st.st_nlink} links"))
                    continue
                items.append((relname, False))
                files.append((relname, entry.path, st.st_size))
            else:
                rejections.append(_rej(relname, "special_file_member"))
    total = 0
    for relname, _path, size in files:
        if size > limits["max_member_bytes"]:
            rejections.append(_rej(relname, "over_member_cap", f"{size} bytes > {limits['max_member_bytes']}"))
        total += size
    if total > limits["max_total_bytes"]:
        rejections.append(_rej(None, "over_total_cap", f"{total} bytes > {limits['max_total_bytes']}"))
    rejections.extend(_namespace_problems(items))
    if rejections:
        raise _Reject(rejections)
    for relname, is_dir in items:
        if is_dir:
            collected.dirs.add(relname)
    for relname, abspath, size in sorted(files):
        budget.declared += size
        keep = _decide_keep(relname, size, budget, collected)
        try:
            with open(abspath, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise _Reject([_rej(relname, "special_file_member")])
                digest, counted, data = _consume(handle, relname, size, budget, keep)
        except OSError as exc:
            raise _Reject([_rej(relname, "unreadable_member", f"{type(exc).__name__}: {exc}")]) from exc
        collected.files.append({"path": relname, "size": counted, "sha256": digest})
        if data is not None:
            collected.texts[relname] = data


def _collect_py_file(target: str, size: int, limits: dict[str, int], budget: _Budget, collected: _Collected) -> None:
    name = os.path.basename(target)
    reason = _name_problem(name)
    if reason:
        raise _Reject([_rej(name, reason)])
    if size > limits["max_member_bytes"]:
        raise _Reject([_rej(name, "over_member_cap", f"{size} bytes > {limits['max_member_bytes']}")])
    budget.declared += size
    keep = _decide_keep(name, size, budget, collected)
    try:
        with open(target, "rb") as handle:
            digest, count, data = _consume(handle, name, size, budget, keep)
    except OSError as exc:
        raise _Reject([_rej(name, "unreadable_member", f"{type(exc).__name__}: {exc}")]) from exc
    collected.files.append({"path": name, "size": count, "sha256": digest})
    if data is not None:
        collected.texts[name] = data


# ---------------------------------------------------------------------------
# Text/data parsing helpers
# ---------------------------------------------------------------------------


def _decode(data: bytes | None) -> str | None:
    if data is None:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _normalize_name(name: str) -> str:
    """PEP 503 normalisation."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _canon_version(version: str) -> str:
    """Compare versions without treating '1.0' and '1.0.0' as different."""
    text = version.strip().lower()
    match = re.match(r"^v?([0-9]+(?:\.[0-9]+)*)(.*)$", text)
    if not match:
        return text
    release = match.group(1).split(".")
    while len(release) > 1 and release[-1] == "0":
        release.pop()
    return ".".join(release) + match.group(2)


_CLAUSE = r"(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]*"
_SPEC_RE = re.compile(rf"^{_CLAUSE}(?:\s*,\s*{_CLAUSE})*$")
_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?P<extras>\[[^\]]*\])?\s*(?P<rest>.*)$"
)
_EXACT_VERSION_RE = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?(?:\+[A-Za-z0-9.]+)?$"
)
_EXTRA_MARKER_RE = re.compile(r"\bextra\s*(?:==|!=|in\b)")


def _parse_requirement(raw: str) -> dict[str, Any] | None:
    """Parse one requirement as text.  Returns None when it is not parseable."""
    text = raw.strip()
    marker = None
    if ";" in text:
        text, marker_text = text.split(";", 1)
        text = text.strip()
        marker = marker_text.strip() or None
    match = _REQ_RE.match(text)
    if not match:
        return None
    declared = match.group("name")
    extras_text = match.group("extras")
    rest = match.group("rest").strip()
    notes: list[str] = []
    direct = False
    spec = ""
    if rest.startswith("@"):
        direct = True
    else:
        if rest.startswith("(") and rest.endswith(")"):
            rest = rest[1:-1].strip()
        spec = rest
        if spec and not _SPEC_RE.match(spec):
            return None
    version = None
    if direct:
        notes.append("direct_reference")
    elif not spec:
        notes.append("no_pin")
    else:
        clauses = [clause.strip() for clause in spec.split(",")]
        if len(clauses) == 1 and clauses[0].startswith("==") and not clauses[0].startswith("==="):
            candidate = clauses[0][2:].strip()
            if _EXACT_VERSION_RE.match(candidate):
                version = candidate
            elif "*" in candidate:
                notes.append("wildcard")
            else:
                notes.append("not_exact_version")
        else:
            notes.append("range")
    extras: list[str] = []
    if extras_text:
        extras = [item.strip() for item in extras_text[1:-1].split(",") if item.strip()]
        notes.append("extras_present")
    if marker:
        notes.append("marker_not_evaluated")
    classification = "pinned" if version is not None and not extras else "unpinned"
    return {
        "raw": raw.strip(),
        "declared_name": declared,
        "name": _normalize_name(declared),
        "specifier": spec,
        "version": version,
        "extras": extras,
        "marker": marker,
        "classification": classification,
        "notes": notes,
    }


def _parse_requirements_text(path: str, text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    requirements: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = re.split(r"\s+#", stripped, maxsplit=1)[0].strip()
        if stripped.endswith("\\"):
            invalid.append({"file": path, "line": number, "text": stripped[:200], "reason": "line_continuation_unsupported"})
            continue
        if stripped.startswith("-"):
            invalid.append({"file": path, "line": number, "text": stripped[:200], "reason": "option_line_unsupported"})
            continue
        parsed = _parse_requirement(stripped)
        if parsed is None:
            invalid.append({"file": path, "line": number, "text": stripped[:200], "reason": "unparseable_requirement"})
            continue
        parsed["file"] = path
        parsed["line"] = number
        requirements.append(parsed)
    return requirements, invalid


def _compatibility(manifest: dict[str, Any] | None) -> dict[str, Any]:
    """Prefix heuristic, same semantics as plugin_catalog._compatibility.

    ``checked`` is False when ``versions`` is absent or not a list; otherwise
    ``compatible_with_pinned`` is True when any ``versions[].kicad_version``
    starts with "10.0".  This is a heuristic, not a compatibility verdict.
    """
    result: dict[str, Any] = {
        "checked": False,
        "compatible_with_pinned": None,
        "pinned_prefix": PINNED_KICAD_PREFIX,
        "heuristic": "string prefix on versions[].kicad_version; not a compatibility determination",
    }
    if not isinstance(manifest, dict):
        return result
    versions = manifest.get("versions")
    if not isinstance(versions, list):
        return result
    result["checked"] = True
    result["compatible_with_pinned"] = any(
        isinstance(item, dict)
        and isinstance(item.get("kicad_version"), str)
        and item["kicad_version"].startswith(PINNED_KICAD_PREFIX)
        for item in versions
    )
    return result


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _person_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _str_or_none(value.get("name"))
    return _str_or_none(value)


def _claims(kind: str, manifest: dict[str, Any] | None) -> dict[str, Any]:
    publisher = repository = identifier = None
    author = resources = None
    if isinstance(manifest, dict):
        if kind == "pcm":
            author = manifest.get("author")
            publisher = _person_name(author)
            res = manifest.get("resources")
            if isinstance(res, dict):
                resources = {k: v for k, v in res.items() if isinstance(k, str) and isinstance(v, str)}
                repository = _str_or_none(res.get("repository"))
            identifier = _str_or_none(manifest.get("identifier"))
        elif kind == "ipc":
            author = manifest.get("author")
            publisher = _person_name(manifest.get("publisher")) or _person_name(author)
            for key in ("repository", "repository_url", "url"):
                repository = _str_or_none(manifest.get(key))
                if repository:
                    break
            res = manifest.get("resources")
            if isinstance(res, dict):
                resources = {k: v for k, v in res.items() if isinstance(k, str) and isinstance(v, str)}
                repository = repository or _str_or_none(res.get("repository"))
            identifier = _str_or_none(manifest.get("identifier"))
    return {
        "provenance": "unverified",
        "claimed_publisher": publisher or "unknown",
        "claimed_repository": repository or "unknown",
        "claimed_identifier": identifier or "unknown",
        "claimed_author": author if isinstance(author, (dict, str)) else None,
        "claimed_resources": resources,
    }


def _license(manifest: dict[str, Any] | None) -> Any:
    if isinstance(manifest, dict):
        value = manifest.get("license")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (dict, list)) and value:
            return value
    return "unknown"


# ---------------------------------------------------------------------------
# Static analysis (ast.parse of text only)
# ---------------------------------------------------------------------------


def _dotted(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _outside_package(literal: str) -> bool:
    text = literal.replace("\\", "/")
    if text.startswith(("/", "~")) or _DRIVE_RE.match(text):
        return True
    return ".." in text.split("/")


def _hint(kind: str, path: str, node: ast.AST, detail: str) -> dict[str, Any]:
    return {"kind": kind, "file": path, "line": getattr(node, "lineno", 0), "detail": detail}


def _is_file_dialog_call(dotted: str) -> bool:
    """True for wx/tkinter/Qt/pcbnew file or directory dialog call names (names only, no import)."""
    parts = dotted.split(".")
    last = parts[-1]
    if last in _TK_DIALOG_FUNCTIONS:
        return True
    if len(parts) >= 2 and parts[-2] == "filedialog" and last in _TK_DIALOG_CLASSES:
        return True
    if "QFileDialog" in parts:
        return True
    if last in _WX_DIALOG_NAMES and (len(parts) == 1 or "wx" in parts[:-1] or parts[0] == "pcbnew"):
        return True
    return parts[0] == "pcbnew" and last.lower() in _PCBNEW_DIALOG_HELPERS


def _check_open(node: ast.Call, path: str, hints: list[dict[str, Any]]) -> None:
    target = node.args[0] if node.args else None
    mode = node.args[1] if len(node.args) > 1 else None
    for keyword in node.keywords:
        if keyword.arg == "file" and target is None:
            target = keyword.value
        elif keyword.arg == "mode" and mode is None:
            mode = keyword.value
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return
    if not set(mode.value) & set("wax+"):
        return
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        if _outside_package(target.value):
            hints.append(_hint("write_outside_package", path, node, f"open({target.value!r}, {mode.value!r})"))
    else:
        hints.append(_hint("write_unknown_target", path, node, f"open(<non-literal path>, {mode.value!r})"))


def _check_path_write(node: ast.Call, attr: str, path: str, hints: list[dict[str, Any]]) -> None:
    receiver = node.func.value  # type: ignore[attr-defined]
    if isinstance(receiver, ast.Call):
        name = _dotted(receiver.func)
        if name and name.rsplit(".", 1)[-1] in ("Path", "PurePath", "PosixPath", "WindowsPath"):
            first = receiver.args[0] if receiver.args else None
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if _outside_package(first.value):
                    hints.append(_hint("write_outside_package", path, node, f"Path({first.value!r}).{attr}"))
                return
    hints.append(_hint("write_unknown_target", path, node, f"<path>.{attr}"))


def _analyze_tree(path: str, tree: ast.AST) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    classes: list[dict[str, Any]] = []
    hints: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [name for name in (_dotted(b) for b in node.bases) if name]
            plugin_type = None
            for base in bases:
                last = base.rsplit(".", 1)[-1]
                if last.endswith("FootprintWizardPlugin"):
                    plugin_type = "footprint_wizard"
                    break
                if last.endswith("ActionPlugin"):
                    plugin_type = "action_plugin"
                    break
            if plugin_type:
                classes.append(
                    {"file": path, "line": node.lineno, "class": node.name, "bases": bases, "plugin_type": plugin_type}
                )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                kind = _IMPORT_HINTS.get(alias.name.split(".")[0])
                if kind:
                    hints.append(_hint(kind, path, node, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                kind = _IMPORT_HINTS.get(node.module.split(".")[0])
                if kind:
                    hints.append(_hint(kind, path, node, f"from {node.module} import ..."))
                if node.module == "os":
                    for alias in node.names:
                        if alias.name in ("system", "popen"):
                            hints.append(_hint(f"os_{alias.name}_import", path, node, f"from os import {alias.name}"))
        elif isinstance(node, ast.Call):
            func = node.func
            dotted_call = _dotted(func)
            if dotted_call and _is_file_dialog_call(dotted_call):
                hints.append(_hint("file_dialog", path, node, f"{dotted_call}(...)"))
            if isinstance(func, ast.Name):
                if func.id == "eval":
                    hints.append(_hint("eval_call", path, node, "eval(...)"))
                elif func.id == "exec":
                    hints.append(_hint("exec_call", path, node, "exec(...)"))
                elif func.id == "open":
                    _check_open(node, path, hints)
                elif func.id == "__import__":
                    hints.append(_hint("dynamic_import_call", path, node, "__import__(...)"))
            elif isinstance(func, ast.Attribute):
                base = func.value
                if isinstance(base, ast.Name) and base.id == "os" and func.attr in ("system", "popen"):
                    hints.append(_hint(f"os_{func.attr}_call", path, node, f"os.{func.attr}(...)"))
                elif func.attr in ("write_text", "write_bytes"):
                    _check_path_write(node, func.attr, path, hints)
                elif func.attr == "open" and isinstance(base, ast.Name) and base.id == "io":
                    _check_open(node, path, hints)
                elif func.attr == "import_module" and isinstance(base, ast.Name) and base.id == "importlib":
                    hints.append(_hint("dynamic_import_call", path, node, "importlib.import_module(...)"))
    return classes, hints


def _parse_entry_points_text(text: str) -> dict[str, dict[str, str]]:
    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped[0] in "#;":
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip()
            sections.setdefault(current, {})
        elif "=" in stripped and current is not None and count < MAX_ENTRY_POINTS:
            key, value = stripped.split("=", 1)
            sections[current][key.strip()] = value.strip()
            count += 1
    return sections


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _all_dirs(paths: set[str], explicit: set[str]) -> list[str]:
    result = set(explicit)
    for path in paths:
        parts = path.split("/")
        for i in range(1, len(parts)):
            result.add("/".join(parts[:i]))
    return sorted(result)


def _detect_kind(paths: set[str], input_type: str) -> tuple[str, list[str]]:
    ambiguities: list[str] = []
    if input_type == "py_file":
        return "legacy_scripting_module", ambiguities
    has_meta = "metadata.json" in paths
    has_plugin = "plugin.json" in paths
    has_init = "__init__.py" in paths
    root_py = sorted(p for p in paths if "/" not in p and p.lower().endswith((".py", ".pyw")) and p != "__init__.py")
    if has_meta and has_plugin:
        ambiguities.append("metadata.json_and_plugin.json_both_at_root")
    if has_meta:
        kind = "pcm"
    elif has_plugin:
        kind = "ipc"
    elif has_init:
        kind = "legacy_scripting_package"
    elif len(root_py) == 1:
        kind = "legacy_scripting_module"
    else:
        kind = "unknown"
        if len(root_py) > 1:
            ambiguities.append("multiple_top_level_py_modules")
        for path in sorted(paths):
            base = path.rsplit("/", 1)[-1]
            if "/" in path and base in ("metadata.json", "plugin.json", "__init__.py"):
                ambiguities.append(f"marker_not_at_root:{path}")
    if kind in ("pcm", "ipc") and has_init:
        ambiguities.append("kind_markers_conflict:__init__.py_at_root")
    return kind, ambiguities


def _load_manifest(kind: str, collected: _Collected, sha_by_path: dict[str, str]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    filename = {"pcm": "metadata.json", "ipc": "plugin.json"}.get(kind)
    if filename is None:
        return {"file": None, "status": "absent", "error": None, "sha256": None}, None
    info: dict[str, Any] = {"file": filename, "status": "ok", "error": None, "sha256": sha_by_path.get(filename)}
    data = collected.texts.get(filename)
    if data is None:
        info["status"] = "invalid_metadata"
        info["error"] = f"not read: {collected.text_skipped.get(filename, 'not_retained')}"
        return info, None
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError, MemoryError) as exc:
        info["status"] = "invalid_metadata"
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info, None
    if not isinstance(parsed, dict):
        info["status"] = "invalid_metadata"
        info["error"] = "manifest is not a JSON object"
        return info, None
    info["data"] = parsed
    return info, parsed


def _build_importables(
    file_records: list[dict[str, Any]], collected: _Collected
) -> tuple[dict[str, Any], list[str]]:
    sourceless: list[dict[str, Any]] = []
    extension_modules: list[dict[str, Any]] = []
    hooks: list[dict[str, Any]] = []
    py_paths: list[str] = []
    importable_dirs: set[str] = set()
    init_dirs: set[str] = set()
    pycache: dict[str, list[str]] = {}
    for record in sorted(file_records, key=lambda r: r["path"]):
        path = record["path"]
        low = path.lower()
        base = low.rsplit("/", 1)[-1]
        parts = path.split("/")
        in_pycache = "__pycache__" in parts[:-1]
        sha = record["sha256"]
        if in_pycache:
            top = parts[: parts.index("__pycache__") + 1]
            pycache.setdefault("/".join(top), []).append(path)
        entry = {"path": path, "sha256": sha, "status": PRESENT_NOT_EXECUTED, "in_pycache": in_pycache}
        importable = False
        if low.endswith(".pyc"):
            sourceless.append({**entry, "type": "sourceless_bytecode"})
            importable = True
        elif low.endswith(".pyd") or _SO_RE.search(low):
            extension_modules.append({**entry, "type": "extension_module"})
            importable = True
        elif low.endswith((".py", ".pyw")):
            py_paths.append(path)
            importable = True
        if low.endswith(".pth"):
            hook = {"path": path, "type": "pth_file", "status": PRESENT_NOT_EXECUTED, "sha256": sha}
            text = _decode(collected.texts.get(path))
            if text is not None:
                lines = text.splitlines()
                hook["line_count"] = len(lines)
                hook["import_lines"] = [
                    number for number, line in enumerate(lines, 1) if line.startswith(("import ", "import\t"))
                ]
            hooks.append(hook)
        elif base in _HOOK_BASENAMES:
            hook = {"path": path, "type": base, "status": PRESENT_NOT_EXECUTED, "sha256": sha}
            text = _decode(collected.texts.get(path))
            if base == "pyproject.toml" and text is not None:
                match = re.search(r"(?m)^\s*build-backend\s*=\s*[\"']([^\"']+)[\"']", text)
                hook["build_backend"] = match.group(1) if match else None
            elif base == "entry_points.txt" and text is not None:
                hook["entry_points"] = _parse_entry_points_text(text)
            hooks.append(hook)
        if importable and not in_pycache:
            directory = path.rsplit("/", 1)[0] if "/" in path else ""
            while directory:
                importable_dirs.add(directory)
                directory = directory.rsplit("/", 1)[0] if "/" in directory else ""
            if base == "__init__.py" and "/" in path:
                init_dirs.add(path.rsplit("/", 1)[0])
    namespace_dirs = [
        {
            "path": directory,
            "top_level": "/" not in directory,
            "note": "no __init__.py but contains importables; importable as a namespace package once on sys.path (A2)",
        }
        for directory in sorted(importable_dirs - init_dirs)
    ]
    pycache_dirs = [
        {"path": directory, "top_level": "/" not in directory, "files": sorted(files)}
        for directory, files in sorted(pycache.items())
    ]
    importables = {
        "python_sources": py_paths,
        "sourceless_bytecode": sourceless,
        "extension_modules": extension_modules,
        "namespace_dirs": namespace_dirs,
        "pycache_dirs": pycache_dirs,
        "build_hook_files": hooks,
        "status": PRESENT_NOT_EXECUTED,
    }
    return importables, py_paths


def _limits_block(limits: dict[str, int], budget: _Budget, member_count: int) -> dict[str, Any]:
    return {
        **limits,
        "max_text_bytes": MAX_TEXT_BYTES,
        "max_retained_bytes": MAX_RETAINED_BYTES,
        "max_central_directory_bytes": MAX_CENTRAL_DIRECTORY_BYTES,
        "read_chunk_bytes": READ_CHUNK,
        "declared_bytes": budget.declared,
        "bytes_read": budget.read,
        "retained_text_bytes": budget.retained,
        "member_count": member_count,
    }


def _build_report(
    info: dict[str, Any],
    collected: _Collected,
    limits: dict[str, int],
    budget: _Budget,
    archive_sha256: str | None,
) -> dict[str, Any]:
    file_records = sorted(collected.files, key=lambda r: r["path"])
    paths = {r["path"] for r in file_records}
    sha_by_path = {r["path"]: r["sha256"] for r in file_records}
    kind, ambiguities = _detect_kind(paths, info["type"])
    metadata, manifest = _load_manifest(kind, collected, sha_by_path)
    claims = _claims(kind, manifest)

    # Declared dependencies (text only).
    requirements: list[dict[str, Any]] = []
    invalid_lines: list[dict[str, Any]] = []
    requirement_files: list[str] = []
    for path in sorted(paths):
        if path.lower().rsplit("/", 1)[-1] != "requirements.txt":
            continue
        requirement_files.append(path)
        data = collected.texts.get(path)
        if data is None:
            invalid_lines.append(
                {"file": path, "line": 0, "text": "", "reason": f"not_read:{collected.text_skipped.get(path, 'not_retained')}"}
            )
            continue
        text = _decode(data)
        if text is None:
            invalid_lines.append({"file": path, "line": 0, "text": "", "reason": "not_utf8"})
            continue
        parsed, invalid = _parse_requirements_text(path, text)
        requirements.extend(parsed)
        invalid_lines.extend(invalid)
    runtime = manifest.get("runtime") if kind == "ipc" and isinstance(manifest, dict) else None
    unpinned = sum(1 for r in requirements if r["classification"] == "unpinned")
    # Unpinned requirements, and lines that cannot be checked as exact pins (option lines,
    # continuations, unparseable or unread files), block X1/X2 (design A3c).
    blocks_reasons = []
    if unpinned:
        blocks_reasons.append("unpinned_requirement")
    if invalid_lines:
        blocks_reasons.append("unverifiable_requirement_lines")
    dependencies = {
        "requirements_files": requirement_files,
        "requirements": requirements,
        "invalid_lines": invalid_lines,
        "pinned_count": len(requirements) - unpinned,
        "unpinned_count": unpinned,
        "has_unpinned": unpinned > 0,
        "blocks_execution": bool(blocks_reasons),
        "blocks_reasons": blocks_reasons,
        "plugin_json_runtime": runtime,
        "plugin_json_runtime_note": "recorded as data; never interpreted or launched",
    }

    importables, py_paths = _build_importables(file_records, collected)

    # Python sources: ast.parse of decoded text only.
    python_files: list[dict[str, Any]] = []
    classes: list[dict[str, Any]] = []
    hints: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    for path in py_paths:
        record: dict[str, Any] = {"path": path, "parsed": False, "parse_error": None}
        data = collected.texts.get(path)
        if data is None:
            record["parse_skipped"] = collected.text_skipped.get(path, "not_retained")
            python_files.append(record)
            continue
        text = _decode(data)
        if text is None:
            record["parse_error"] = "UnicodeDecodeError: source is not UTF-8"
            parse_errors.append({"file": path, "error": record["parse_error"]})
            python_files.append(record)
            continue
        try:
            tree = ast.parse(text, filename=path)
        except (SyntaxError, ValueError, RecursionError, MemoryError, OverflowError) as exc:
            record["parse_error"] = f"{type(exc).__name__}: {exc}"
            parse_errors.append({"file": path, "error": record["parse_error"]})
            python_files.append(record)
            continue
        record["parsed"] = True
        python_files.append(record)
        found_classes, found_hints = _analyze_tree(path, tree)
        classes.extend(found_classes)
        hints.extend(found_hints)
    hints.sort(key=lambda h: (h["file"], h["line"], h["kind"]))
    classes.sort(key=lambda c: (c["file"], c["line"]))

    ipc_actions: list[Any] = []
    ipc_actions_status = "not_applicable"
    if kind == "ipc" and isinstance(manifest, dict):
        actions = manifest.get("actions")
        if actions is None:
            ipc_actions_status = "absent"
        elif isinstance(actions, list):
            ipc_actions = actions[:MAX_IPC_ACTIONS]
            ipc_actions_status = "ok" if len(actions) <= MAX_IPC_ACTIONS else "truncated"
        else:
            ipc_actions_status = "not_a_list"

    return {
        "status": "ok",
        "executes_nothing": True,
        "input": info,
        "kind": kind,
        "ambiguities": ambiguities,
        "archive_sha256": archive_sha256,
        "files": file_records,
        "directories": _all_dirs(paths, collected.dirs),
        "metadata": metadata,
        **claims,
        "license": _license(manifest),
        "declared_dependencies": dependencies,
        "compatibility": _compatibility(manifest),
        "entry_points": {
            "classes": classes,
            "ipc_actions": ipc_actions,
            "ipc_actions_status": ipc_actions_status,
            "python_files": python_files,
            "parse_errors": parse_errors,
        },
        "importables": importables,
        "static_hints": hints,
        "static_hints_note": STATIC_HINTS_NOTE,
        "limits_applied": _limits_block(limits, budget, len(file_records)),
    }


def _check_limit(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PluginInspectError(f"{name} must be a positive integer", "limit_invalid")
    return value


def inspect_package(
    path: str,
    quarantine_root: str,
    *,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_members: int = MAX_MEMBERS,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
) -> dict[str, Any]:
    # covers: L3-2
    """Inspect a package (``.zip`` archive, directory or single ``.py``) offline.

    ``path`` is a name relative to ``quarantine_root`` (an existing directory,
    passed explicitly for tests; the MCP tools use :func:`resolve_quarantine_root`)
    and must resolve strictly inside it; otherwise :class:`PluginInspectError`
    (with a ``code``) is raised.  Member-level
    problems never raise: the whole package is rejected and a structured
    ``{"status": "rejected", "rejection": {"member", "reason", "code", ...}}`` result
    is returned, with nothing partially accepted.  Nothing is imported,
    compiled or executed.
    """
    limits = {
        "max_total_bytes": _check_limit("max_total_bytes", max_total_bytes),
        "max_member_bytes": _check_limit("max_member_bytes", max_member_bytes),
        "max_members": _check_limit("max_members", max_members),
        "max_archive_bytes": _check_limit("max_archive_bytes", max_archive_bytes),
    }
    root_abs, root_real = _prepare_root(quarantine_root)
    target = _resolve_inside(path, root_abs, root_real)
    st = os.lstat(target)
    if stat.S_ISDIR(st.st_mode):
        input_type = "directory"
    elif stat.S_ISREG(st.st_mode):
        input_type = "py_file" if target.lower().endswith((".py", ".pyw")) else "zip"
    else:
        raise PluginInspectError("path is neither a regular file nor a directory", "path_wrong_kind")
    info = {"path": target, "name": os.path.basename(target), "type": input_type}
    budget = _Budget(limits["max_total_bytes"], limits["max_member_bytes"])
    collected = _Collected()
    archive_sha256: str | None = None
    try:
        if input_type == "zip":
            archive_sha256 = _collect_zip(target, st.st_size, limits, budget, collected)
        elif input_type == "directory":
            _collect_directory(target, limits, budget, collected)
        else:
            _collect_py_file(target, st.st_size, limits, budget, collected)
    except _Reject as rejected:
        return {
            "status": "rejected",
            "accepted": False,
            "executes_nothing": True,
            "input": info,
            "rejection": rejected.rejections[0],
            "rejections": rejected.rejections,
            "limits_applied": _limits_block(limits, budget, 0),
        }
    return _build_report(info, collected, limits, budget, archive_sha256)


# ---------------------------------------------------------------------------
# D1 offline closure check (A3c)
# ---------------------------------------------------------------------------

_DISTINFO_METADATA_RE = re.compile(r"^[^/\\\x00]+\.dist-info/METADATA$")


def _read_wheel(target: str, max_wheel_bytes: int, max_wheel_members: int, max_metadata_bytes: int) -> dict[str, Any]:
    """Read one wheel's METADATA headers as data.  Raises _Reject on problems."""
    st = os.lstat(target)
    if st.st_size > max_wheel_bytes:
        raise _Reject([_rej(None, "archive_too_large", f"{st.st_size} bytes > {max_wheel_bytes}")])
    try:
        problem = _precheck_zip(target, st.st_size, max_wheel_members)
    except OSError as exc:
        raise _Reject([_rej(None, "corrupt_archive", f"{type(exc).__name__}: {exc}")]) from exc
    if problem:
        raise _Reject([_rej(None, problem[0], problem[1])])
    sha256 = _hash_file(target, max_wheel_bytes)
    try:
        archive = zipfile.ZipFile(target)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError) as exc:
        raise _Reject([_rej(None, "corrupt_archive", f"{type(exc).__name__}: {exc}")]) from exc
    with archive:
        candidates = [i for i in archive.infolist() if _DISTINFO_METADATA_RE.match(_orig(i))]
        if not candidates:
            raise _Reject([_rej(None, "no_metadata", "no *.dist-info/METADATA member")])
        if len(candidates) > 1:
            raise _Reject([_rej(None, "ambiguous_metadata", "more than one *.dist-info/METADATA member")])
        member = candidates[0]
        name = _orig(member)
        if member.flag_bits & 0x1:
            raise _Reject([_rej(name, "encrypted_member")])
        if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise _Reject([_rej(name, "unsupported_compression", str(member.compress_type))])
        if member.file_size > max_metadata_bytes:
            raise _Reject([_rej(name, "over_member_cap", f"METADATA declares {member.file_size} bytes")])
        budget = _Budget(max_metadata_bytes, max_metadata_bytes)
        try:
            with archive.open(member, "r") as stream:
                _digest, _count, data = _consume(stream, name, member.file_size, budget, True)
        except _Reject:
            raise
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, NotImplementedError, RuntimeError, ValueError) as exc:
            raise _Reject([_rej(name, "corrupt_member", f"{type(exc).__name__}: {exc}")]) from exc
    try:
        text = (data or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _Reject([_rej(name, "metadata_not_utf8", str(exc))]) from exc
    message = email.parser.Parser().parsestr(text, headersonly=True)
    dist_name = message.get("Name")
    version = message.get("Version")
    if not dist_name or not version:
        raise _Reject([_rej(name, "metadata_incomplete", "Name or Version header missing")])
    requires = [" ".join(str(item).split()) for item in (message.get_all("Requires-Dist") or [])]
    return {
        "wheel_filename": os.path.basename(target),
        "wheel_sha256": sha256,
        "name": _normalize_name(" ".join(str(dist_name).split())),
        "version": " ".join(str(version).split()),
        "requires_dist": requires,
    }


def closure_check(
    quarantine_root: str,
    wheels: list[str],
    *,
    max_wheel_bytes: int = MAX_ARCHIVE_BYTES,
    max_wheel_members: int = MAX_WHEEL_MEMBERS,
    max_metadata_bytes: int = MAX_TEXT_BYTES,
) -> dict[str, Any]:
    # covers: L3-2
    """Resolve the exact-pin dependency closure of wheels already in quarantine.

    Reads each wheel's ``*.dist-info/METADATA`` as data only (no install, no
    import, no network).  Every given wheel is a closure member; its
    ``Requires-Dist`` entries are checked against the wheels that are present.
    Requirements with an ``extra ==`` marker are skipped and reported; other
    markers are not evaluated (the requirement is still resolved and recorded
    under ``marker_not_evaluated``).  ``blocks`` is True on any conflict,
    missing closure member, unpinned requirement or unreadable wheel.
    """
    limits = {
        "max_wheel_bytes": _check_limit("max_wheel_bytes", max_wheel_bytes),
        "max_wheel_members": _check_limit("max_wheel_members", max_wheel_members),
        "max_metadata_bytes": _check_limit("max_metadata_bytes", max_metadata_bytes),
    }
    root_abs, root_real = _prepare_root(quarantine_root)
    if isinstance(wheels, (str, bytes, os.PathLike)) or not isinstance(wheels, (list, tuple)):
        raise PluginInspectError("wheels must be a list of root-relative paths", "wheels_invalid")
    targets: list[str] = []
    seen: set[str] = set()
    for item in wheels:
        target = _resolve_inside(item, root_abs, root_real, wheel=True)
        if not stat.S_ISREG(os.lstat(target).st_mode):
            raise PluginInspectError("a wheel path is not a regular file", "wheel_invalid")
        if not target.lower().endswith(".whl"):
            raise PluginInspectError("a wheel path does not end in .whl", "wheel_invalid")
        key = os.path.normcase(target)
        if key not in seen:
            seen.add(key)
            targets.append(target)

    wheel_errors: list[dict[str, Any]] = []
    infos: list[dict[str, Any]] = []
    for target in targets:
        try:
            infos.append(
                _read_wheel(target, limits["max_wheel_bytes"], limits["max_wheel_members"], limits["max_metadata_bytes"])
            )
        except _Reject as rejected:
            first = rejected.rejections[0]
            wheel_errors.append(
                {"wheel_filename": os.path.basename(target), "reason": first["reason"], "code": first["code"],
                 "detail": first["detail"]}
            )
    infos.sort(key=lambda i: (i["name"], _canon_version(i["version"]), i["wheel_filename"]))

    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in infos:
        by_name.setdefault(item["name"], []).append(item)

    conflicts: list[dict[str, Any]] = []
    for name, group in sorted(by_name.items()):
        if len({_canon_version(w["version"]) for w in group}) > 1:
            conflicts.append(
                {
                    "type": "multiple_wheel_versions",
                    "name": name,
                    "versions": sorted(w["version"] for w in group),
                    "wheels": sorted(w["wheel_filename"] for w in group),
                }
            )

    pins: dict[str, dict[str, dict[str, Any]]] = {}
    required_by: dict[str, set[str]] = {}
    missing_map: dict[str, dict[str, Any]] = {}
    unpinned: list[dict[str, Any]] = []
    skipped_extras: list[dict[str, Any]] = []
    marker_not_evaluated: list[dict[str, Any]] = []
    for wheel in infos:
        label = f"{wheel['name']}=={wheel['version']}"
        for raw in wheel["requires_dist"]:
            parsed = _parse_requirement(raw)
            if parsed is None:
                wheel_errors.append(
                    {"wheel_filename": wheel["wheel_filename"], "reason": "invalid_requirement", "detail": raw[:200]}
                )
                continue
            marker = parsed["marker"]
            if marker and _EXTRA_MARKER_RE.search(marker):
                skipped_extras.append({"required_by": label, "requirement": raw, "marker": marker})
                continue
            if marker:
                marker_not_evaluated.append(
                    {"required_by": label, "requirement": raw, "marker": marker, "status": "marker_not_evaluated"}
                )
            name = parsed["name"]
            required_by.setdefault(name, set()).add(label)
            if parsed["classification"] == "unpinned":
                unpinned.append(
                    {
                        "status": "unpinned",
                        "name": name,
                        "required_by": label,
                        "requirement": raw,
                        "specifier": parsed["specifier"],
                        "notes": parsed["notes"],
                    }
                )
            pin = parsed["version"]
            if pin:
                entry = pins.setdefault(name, {}).setdefault(
                    _canon_version(pin), {"version": pin, "required_by": []}
                )
                entry["required_by"].append(label)
            present = by_name.get(name)
            if present is None:
                missing = missing_map.setdefault(
                    name, {"name": name, "required_by": set(), "pinned_versions": set()}
                )
                missing["required_by"].add(label)
                if pin:
                    missing["pinned_versions"].add(pin)
            elif pin and _canon_version(pin) not in {_canon_version(w["version"]) for w in present}:
                conflicts.append(
                    {
                        "type": "present_wheel_version_differs_from_pin",
                        "name": name,
                        "pinned": pin,
                        "present": sorted(w["version"] for w in present),
                        "required_by": label,
                    }
                )
    for name, versions in sorted(pins.items()):
        if len(versions) > 1:
            conflicts.append(
                {
                    "type": "conflicting_pins",
                    "name": name,
                    "pins": [
                        {"version": v["version"], "required_by": sorted(v["required_by"])}
                        for _, v in sorted(versions.items())
                    ],
                }
            )

    members = [
        {
            "name": wheel["name"],
            "version": wheel["version"],
            "wheel_filename": wheel["wheel_filename"],
            "wheel_sha256": wheel["wheel_sha256"],
            "required_by": sorted(required_by.get(wheel["name"], set())),
            "root": not required_by.get(wheel["name"]),
        }
        for wheel in infos
    ]
    missing = [
        {
            "name": entry["name"],
            "required_by": sorted(entry["required_by"]),
            "pinned_versions": sorted(entry["pinned_versions"]),
        }
        for _, entry in sorted(missing_map.items())
    ]
    reasons = []
    if conflicts:
        reasons.append("conflict")
    if missing:
        reasons.append("missing")
    if unpinned:
        reasons.append("unpinned")
    if wheel_errors:
        reasons.append("wheel_error")
    return {
        "executes_nothing": True,
        "network_used": False,
        "members": members,
        "conflicts": conflicts,
        "missing": missing,
        "unpinned": unpinned,
        "skipped_extras": skipped_extras,
        "marker_not_evaluated": marker_not_evaluated,
        "wheel_errors": wheel_errors,
        "blocks": bool(reasons),
        "blocks_reasons": reasons,
        "limits_applied": limits,
    }
