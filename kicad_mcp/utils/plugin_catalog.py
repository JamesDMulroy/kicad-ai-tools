"""Read-only, host-configured KiCad plugin/addon metadata inventory.

Distinguishes bundled legacy scripting-API files, user scripting plugins, PCM
``metadata.json`` packages, and IPC ``plugin.json`` addons. Never imports,
compiles, parses as code, or executes any plugin file; never accepts a
caller-supplied scan root.

Roots mirror KiCad 10.0.5's ``PATHS``. The scanner checks the bundled, settings, documents,`r`nand environment-configured plugin roots described below; live KiCad behaviour for these`r`nroots is not yet verified. The user
documents base is ``KICAD_DOCUMENTS_HOME`` if set, else the OS Documents known
folder, with ``KiCad/10.0`` appended. A set-but-empty ``KICAD_DOCUMENTS_HOME`` is
treated as set (KiCad tests only ``wxGetEnv``): every documents-derived root is then
reported with status ``kicad_documents_home_empty``, a null path and
``resolved_from: KICAD_DOCUMENTS_HOME``, is not scanned and has no fallback.
3rd-party resolves like KiCad's settings-managed variable: a non-empty process
``KICAD10_3RD_PARTY`` wins (``resolved_from: env``); else the value saved at
``environment.vars.KICAD10_3RD_PARTY`` in ``<settings root>/kicad_common.json``
(``kicad_common_saved``; that saved values rank below the process environment is
inferred from ``common_settings.cpp`` ``addVar``, not verified live); else
``<docs>/3rdparty`` (``default``). IPC user plugins are ``<docs>/plugins``, user
scripting plugins ``<docs>/scripting/plugins``. The user settings root is
``KICAD_CONFIG_HOME`` (versioned ``10.0`` subdirectory appended, mirroring the
APPDATA branch; the excerpt that would confirm the subdirectory is not in this
repo, so that detail is unverified), else ``%APPDATA%/kicad/10.0``. Every root
reports ``path``, ``status`` and ``resolved_from`` so an absent root is never
ambiguous.

Links that are not followed (resolving outside the scanned root, to a
non-immediate child, or dangling) are reported as ``link_entry_not_followed``
records instead of being dropped. File hashing is streamed and capped
(``MAX_HASH_BYTES``); an oversize file is reported as ``oversize`` and not read.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

KICAD_VERSION = "10.0"
PINNED_RELEASE = "10.0.5"

LINK_ENTRY_NOT_FOLLOWED = "link_entry_not_followed"
DOCUMENTS_HOME_EMPTY = "kicad_documents_home_empty"

# Hash and traversal caps. Module-level so tests can monkeypatch them.
MAX_HASH_BYTES = 64 * 1024 * 1024
HASH_BLOCK_BYTES = 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_COMMON_JSON_BYTES = 1024 * 1024
MAX_PACKAGE_FILES = 2000
MAX_PACKAGE_DEPTH = 8
MAX_TREE_BYTES = 512 * 1024 * 1024
MAX_DISCOVERY_DEPTH = 8
MAX_DISCOVERY_ENTRIES = 5000
MAX_LISTING_ENTRIES = 1000

# '.pyw' is Python source (importable as a module on Windows), so it is not listed here. Source
# suffixes are case-sensitive, like the loader's '.py' match; the non-source ones are matched
# case-insensitively.
_SOURCE_SUFFIXES = (".py", ".pyw")
_NON_PY_IMPORTABLE_SUFFIXES = (".pyc", ".pyd", ".so")

_NOT_IMPORTED_NOTE = "File name and hash only; not imported, parsed, or executed."


def _env_nonempty(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _known_documents_folder() -> Path | None:
    """Read-only lookup of the OS Documents known folder; None when unavailable.

    On Windows this calls SHGetKnownFolderPath(FOLDERID_Documents) and frees the
    returned buffer with CoTaskMemFree. Tests monkeypatch this function.
    """
    # FOLDERID_Documents {FDD39AD0-238F-46AF-ADB4-6C85480369C7}
    return _known_folder(0xFDD39AD0, 0x238F, 0x46AF, (0xAD, 0xB4, 0x6C, 0x85, 0x48, 0x03, 0x69, 0xC7))


def _known_roaming_folder() -> Path | None:
    """FOLDERID_RoamingAppData, the folder wx (and so KiCad) uses for user settings. Tests monkeypatch it."""
    # FOLDERID_RoamingAppData {3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}
    return _known_folder(0x3EB685DB, 0x65F9, 0x4CF6, (0xA0, 0x3A, 0xE3, 0xEF, 0x65, 0x72, 0x9F, 0x3D))


def _known_folder(d1: int, d2: int, d3: int, d4: tuple) -> Path | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    folder_id = _GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*d4))
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(_GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole32.CoTaskMemFree.restype = None
    buffer = ctypes.c_void_p()
    try:
        shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(buffer))
        text = ctypes.wstring_at(buffer.value) if buffer.value else ""
    finally:
        if buffer.value:
            ole32.CoTaskMemFree(buffer)
    return Path(text) if text else None


def _documents_base() -> tuple[Path | None, str]:
    """Documents base; ``(None, "KICAD_DOCUMENTS_HOME")`` when the variable is set but empty."""
    configured = os.environ.get("KICAD_DOCUMENTS_HOME")
    if configured is not None:
        if configured == "":
            return None, "KICAD_DOCUMENTS_HOME"
        return Path(configured), "KICAD_DOCUMENTS_HOME"
    try:
        known = _known_documents_folder()
    except Exception:
        known = None
    if known:
        return Path(known), "known_folder"
    if sys.platform == "win32":
        return None, "documents_unresolved"  # never a guessed folder: reported with the fix
    return Path.home() / "Documents", "os_standard_documents"  # portability-ok: macOS-defined / XDG default


def _kicad_docs() -> tuple[Path | None, str]:
    base, source = _documents_base()
    if base is None:
        return None, source
    return base / "KiCad" / KICAD_VERSION, source


def _docs_child(*parts: str) -> tuple[Path | None, str]:
    """A path under ``<docs>``; None (never a fallback) when KICAD_DOCUMENTS_HOME is set but empty."""
    docs, source = _kicad_docs()
    if docs is None:
        return None, source
    return docs.joinpath(*parts), source


def _documents_home_empty_section(**extra: Any) -> dict[str, Any]:
    _base, source = _documents_base()
    if source == "documents_unresolved":
        return {"status": "documents_unresolved", "path": None, "resolved_from": source,
                "reason": "Windows did not report a Documents folder; set KICAD_DOCUMENTS_HOME so this "
                          "documents-derived root can be derived and scanned", **extra}
    return {"status": DOCUMENTS_HOME_EMPTY, "path": None, "resolved_from": "KICAD_DOCUMENTS_HOME",
            "reason": "KICAD_DOCUMENTS_HOME is set but empty; KiCad does not fall back, so this "
                      "documents-derived root is neither derived nor scanned",
            **extra}


def _kicad_app_path() -> tuple[Path, str]:
    """KiCad's root for this read-only inventory, as KiCad itself would read its share/ folder: an
    explicit KICAD_APP_PATH or per-machine ``kicad_app_root`` is used as given (no kicad-cli needed to list
    plugin folders); otherwise kicad_env discovery. Failures are labelled in ``resolved_from``
    (``config_invalid`` / ``not_found``) with the message in the placeholder path."""
    from kicad_mcp.adaptive_editor import kicad_env
    if _env_nonempty(kicad_env.KICAD_ROOT_ENV):
        return Path(os.environ[kicad_env.KICAD_ROOT_ENV]), "KICAD_APP_PATH"
    try:
        stored = kicad_env.setting("kicad_app_root", kicad_env.KICAD_ROOT_ENV)
    except kicad_env.ConfigError as exc:
        return Path(f"<{exc}>"), "config_invalid"
    if stored:
        return Path(stored), "per_machine_config"
    try:
        return kicad_env.kicad_app_root(), "kicad_env_discovery"
    except kicad_env.KiCadNotFound as exc:
        return Path(f"<{exc}>"), "not_found"


def _kicad_root_section() -> dict[str, Any]:
    """The KiCad root this inventory used, with an explicit status, so a wrong or missing KiCad is never
    a silently empty inventory."""
    root, source = _kicad_app_path()
    present = root.is_dir() and (root / "share" / "kicad").is_dir()
    section = {"path": str(root), "resolved_from": source, "status": "present" if present else "absent"}
    if not present:
        section["error"] = (str(root)[1:-1] if str(root).startswith("<") else
                            f"{root} is not a KiCad installation (no share/kicad); fix KICAD_APP_PATH / the "
                            "per-machine kicad_app_root, or run python -m kicad_mcp.setup")
    return section


def _legacy_scripting_base() -> tuple[Path, str]:
    app, source = _kicad_app_path()
    return app / "share" / "kicad" / "scripting", source


def _legacy_scripting_root() -> tuple[Path, str]:
    base, source = _legacy_scripting_base()
    return base / "plugins", source


def _stock_plugins_root() -> tuple[Path, str]:
    app, source = _kicad_app_path()
    return app / "share" / "kicad" / "plugins", source


def _ipc_default_plugins_root() -> tuple[Path | None, str]:
    return _docs_child("plugins")


def _user_scripting_base() -> tuple[Path | None, str]:
    return _docs_child("scripting")


def _user_scripting_root() -> tuple[Path | None, str]:
    return _docs_child("scripting", "plugins")


def _settings_root() -> tuple[Path, str]:
    config_home = _env_nonempty("KICAD_CONFIG_HOME")
    if config_home:
        return Path(config_home) / KICAD_VERSION, "KICAD_CONFIG_HOME"
    configured = _env_nonempty("APPDATA")
    if configured:
        return Path(configured) / "kicad" / KICAD_VERSION, "APPDATA"
    try:
        roaming = _known_roaming_folder()
    except Exception:  # the OS lookup failed; reported below as an unresolved root
        roaming = None
    if roaming:
        return Path(roaming) / "kicad" / KICAD_VERSION, "known_folder_RoamingAppData"
    return Path("<KiCad settings root unresolved: set KICAD_CONFIG_HOME>"), "unresolved"


def _settings_scripting_base() -> tuple[Path, str]:
    root, source = _settings_root()
    return root / "scripting", source


def _settings_scripting_root() -> tuple[Path, str]:
    base, source = _settings_scripting_base()
    return base / "plugins", source


def _saved_third_party_value() -> str | None:
    """KICAD10_3RD_PARTY saved in ``<settings root>/kicad_common.json`` (environment.vars).

    Read as size-capped JSON data only. Absent, linked-outside, oversize, invalid, non-object or
    non-string/empty values all yield None.
    """
    settings_root, _ = _settings_root()
    name = "kicad_common.json"
    if not os.path.lexists(settings_root / name):
        return None
    resolved, _reason = _classify_child(settings_root, name)
    if resolved is None:
        return None
    try:
        if not resolved.is_file():
            return None
        with resolved.open("rb") as stream:
            raw = stream.read(MAX_COMMON_JSON_BYTES + 1)
        if len(raw) > MAX_COMMON_JSON_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    environment = data.get("environment") if isinstance(data, dict) else None
    variables = environment.get("vars") if isinstance(environment, dict) else None
    value = variables.get("KICAD10_3RD_PARTY") if isinstance(variables, dict) else None
    if isinstance(value, str) and value.strip() and "\x00" not in value:
        return value
    return None


def _third_party_root() -> tuple[Path | None, str]:
    """3rd-party root with KiCad-style precedence: process env, saved value, ``<docs>/3rdparty``.

    The saved-value rank is inferred from ``common_settings.cpp`` ``addVar`` (not verified live).
    Returns ``(None, "KICAD_DOCUMENTS_HOME")`` when only the default applies and the documents
    home is set but empty.
    """
    configured = _env_nonempty("KICAD10_3RD_PARTY")
    if configured:
        return Path(configured), "env"
    saved = _saved_third_party_value()
    if saved:
        return Path(saved), "kicad_common_saved"
    root, source = _docs_child("3rdparty")
    if root is None:
        return None, source
    return root, "default"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _classify_child(root: Path, name: str) -> tuple[Path | None, str | None]:
    """Resolve an entry of root. Returns (resolved, None) for an immediate, non-escaping
    child, else (None, reason) so the caller can report the entry instead of dropping it."""
    candidate = root / name
    try:
        resolved = candidate.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return None, f"dangling_or_unresolvable_link: {type(exc).__name__}"
    if resolved.parent == resolved_root:
        return resolved, None
    if _is_within(resolved, resolved_root):
        return None, "resolves_to_non_immediate_child_of_root"
    return None, "resolves_outside_root"


def _safe_child(root: Path, name: str) -> Path | None:
    """Accept only entries that resolve to an immediate, non-escaping child of root."""
    return _classify_child(root, name)[0]


def _hash_record(path: Path) -> dict[str, Any]:
    """Stream a sha256 in HASH_BLOCK_BYTES blocks, never reading more than MAX_HASH_BYTES.

    Normal file: ``{"sha256": hex}``. Over the cap: ``{"sha256": None, "size": n,
    "status": "oversize"}`` (a file larger than the cap is not opened at all).
    """
    cap = MAX_HASH_BYTES
    try:
        size = path.stat().st_size
        if size > cap:
            return {"sha256": None, "size": size, "status": "oversize"}
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as stream:
            while True:
                block = stream.read(HASH_BLOCK_BYTES)
                if not block:
                    break
                total += len(block)
                if total > cap:  # the file grew while it was being read
                    return {"sha256": None, "size": total, "status": "oversize"}
                digest.update(block)
    except OSError:
        return {"sha256": None, "status": "unreadable"}
    return {"sha256": digest.hexdigest()}


def _hash_file(path: Path) -> str | None:
    return _hash_record(path)["sha256"]


def _merge_hash(item: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """Add a hash record to an entry; a hash status never overwrites the entry's own status."""
    item["sha256"] = record["sha256"]
    if "size" in record:
        item["size"] = record["size"]
    if record.get("status"):
        item["hash_status" if "status" in item else "status"] = record["status"]
    return item


def _is_non_py_importable(name: str) -> bool:
    return name.lower().endswith(_NON_PY_IMPORTABLE_SUFFIXES)


def _is_importable_file_name(name: str) -> bool:
    return name.endswith(_SOURCE_SUFFIXES) or _is_non_py_importable(name)


def _absent(root: Path, resolved_from: str) -> dict[str, Any]:
    return {"status": "absent", "path": str(root), "resolved_from": resolved_from}


def _file_record(path: Path, filename: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"filename": filename}
    if state is not None:
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        if size is not None and state["bytes"] + size > MAX_TREE_BYTES:
            record.update(sha256=None, size=size, status="size_budget_exceeded")
            return record
        if size is not None:
            state["bytes"] += size
    record.update(_hash_record(path))
    return record


def _flat_listing(directory: Path, rel: str) -> list[dict[str, Any]]:
    """Hash the files directly inside a __pycache__ directory (names only; nothing is loaded)."""
    records: list[dict[str, Any]] = []
    state = {"bytes": 0}
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        return [{"filename": rel, "status": "unreadable_directory", "reason": type(exc).__name__}]
    for name in names:
        if len(records) >= MAX_PACKAGE_FILES:
            break
        filename = f"{rel}/{name}" if rel else name
        resolved, reason = _classify_child(directory, name)
        if resolved is None:
            records.append({"filename": filename, "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason})
        elif resolved.is_dir():
            records.append({"filename": filename, "status": "subdirectory_not_descended"})
        elif resolved.is_file():
            records.append(_file_record(resolved, filename, state))
    return records


def _walk_tree(base: Path) -> dict[str, Any]:
    """Recursively hash every regular file under base (relative POSIX names), without loading anything.

    Links that do not resolve to an immediate child of their directory are reported as
    ``link_entry_not_followed`` and never followed or hashed. ``__pycache__`` directories are
    listed separately. Bounded by MAX_PACKAGE_FILES, MAX_PACKAGE_DEPTH and MAX_TREE_BYTES.
    """
    files: list[dict[str, Any]] = []
    pycache_dirs: list[dict[str, Any]] = []
    subdirectories: list[str] = []
    not_descended: list[str] = []
    non_py: list[str] = []
    state: dict[str, Any] = {"bytes": 0, "truncated": False}

    def visit(directory: Path, rel: str, depth: int) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            files.append({"filename": rel or ".", "status": "unreadable_directory",
                          "reason": type(exc).__name__})
            return
        for name in names:
            if len(files) >= MAX_PACKAGE_FILES:
                state["truncated"] = True
                return
            rel_name = f"{rel}/{name}" if rel else name
            resolved, reason = _classify_child(directory, name)
            if resolved is None:
                files.append({"filename": rel_name, "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason})
                continue
            if resolved.is_dir():
                if name == "__pycache__":
                    pycache_dirs.append({"directory": rel_name, "files": _flat_listing(resolved, rel_name)})
                    continue
                subdirectories.append(rel_name)
                if depth >= MAX_PACKAGE_DEPTH:
                    not_descended.append(rel_name)
                    continue
                visit(resolved, rel_name, depth + 1)
                continue
            if not resolved.is_file():
                continue
            files.append(_file_record(resolved, rel_name, state))
            if _is_non_py_importable(name):
                non_py.append(rel_name)

    visit(base, "", 0)
    return {"files": files, "pycache_dirs": pycache_dirs, "non_py_importables": non_py,
            "subdirectories": subdirectories, "subdirectories_not_descended": not_descended,
            "truncated": state["truncated"]}


def _link_entry(name: str, reason: str | None, file_type: str, package_type: str) -> dict[str, Any]:
    if _is_importable_file_name(name):
        return {"type": file_type, "filename": name, "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason}
    return {"type": package_type, "package_dir": name, "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason}


def _scan_scripting_dir(root: Path, resolved_from: str, file_type: str, package_type: str,
                        plugins_inventory_key: str | None = None) -> dict[str, Any]:
    """Shared one-level scan mirroring KiCad's LoadPlugins (kicadplugins.i:259-274).

    For a scripting ROOT scan, pass ``plugins_inventory_key``: the ``plugins`` subdirectory is
    then not listed as a package but noted as scanned separately under that inventory key.
    """
    if not root.is_dir():
        return _absent(root, resolved_from)
    entries: list[dict[str, Any]] = []
    plugins_note: dict[str, Any] | None = None
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        return {"status": "present", "path": str(root), "resolved_from": resolved_from, "entries": [],
                "error": f"{type(exc).__name__}: {exc}"}
    for name in names:
        resolved, reason = _classify_child(root, name)
        if plugins_inventory_key and name == "plugins" and (resolved is None or resolved.is_dir()):
            if resolved is None:
                # a link that does not resolve to an immediate child is reported, not followed,
                # and the separate *_scripting_plugins scan refuses it too (_scan_plugins_subdir)
                entries.append(_link_entry(name, reason, file_type, package_type))
            else:
                plugins_note = {"status": "scanned_separately", "inventory_key": plugins_inventory_key}
            continue
        if resolved is None:
            entries.append(_link_entry(name, reason, file_type, package_type))
            continue
        if resolved.is_dir():
            if name == "__pycache__":
                tree = _walk_tree(resolved)
                entries.append({"type": package_type, "package_dir": name, "status": "pycache_present",
                                "files": tree["files"], "truncated": tree["truncated"],
                                "note": _NOT_IMPORTED_NOTE})
            elif (resolved / "__init__.py").is_file():
                entries.append({"type": package_type, "package_dir": name, **_walk_tree(resolved),
                                "note": _NOT_IMPORTED_NOTE})
            else:
                # No __init__.py: KiCad's loader skips it, but once the directory is on sys.path a
                # namespace package inside it is importable (design A2), so list and hash its contents.
                entries.append({"type": package_type, "package_dir": name,
                                "status": "not_loaded_by_kicad", "importable_namespace_dir": True,
                                **_walk_tree(resolved), "note": _NOT_IMPORTED_NOTE})
            continue
        if name == "__init__.py" or not resolved.is_file():
            continue
        if name.endswith(_SOURCE_SUFFIXES):
            entries.append(_merge_hash({"type": file_type, "filename": name, "note": _NOT_IMPORTED_NOTE},
                                       _hash_record(resolved)))
        elif _is_non_py_importable(name):
            entries.append(_merge_hash({"type": file_type, "filename": name, "status": "non_py_importable",
                                        "note": _NOT_IMPORTED_NOTE}, _hash_record(resolved)))
    result: dict[str, Any] = {"status": "present", "path": str(root), "resolved_from": resolved_from,
                              "entries": entries}
    if plugins_note:
        result["plugins_subdirectory"] = plugins_note
    return result


def _scan_plugins_subdir(base: Path, root: Path, resolved_from: str, file_type: str,
                         package_type: str) -> dict[str, Any]:
    """Scan ``<scripting root>/plugins`` unless it is a link that does not stay inside ``base``.

    ``root`` must be ``base / "plugins"``. A symlink/junction resolving outside (or to a
    non-immediate child of) the parent scripting root is reported, never followed.
    """
    if not os.path.lexists(root):
        return _absent(root, resolved_from)
    resolved, reason = _classify_child(base, root.name)
    if resolved is None:
        return {"status": LINK_ENTRY_NOT_FOLLOWED, "path": str(root), "resolved_from": resolved_from,
                "reason": reason, "entries": []}
    return _scan_scripting_dir(root, resolved_from, file_type, package_type)


def _read_json_metadata(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return {"valid": False, "error": f"metadata file larger than {MAX_JSON_BYTES} bytes; not read"}
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return {"valid": False, "error": f"Invalid JSON: {exc}"}
    if not isinstance(data, dict):
        return {"valid": False, "error": "metadata root is not a JSON object"}
    return {"valid": True, "data": data}


def _compatibility(metadata: dict[str, Any]) -> dict[str, Any]:
    versions = metadata.get("versions")
    if not isinstance(versions, list):
        return {"checked": False, "reason": "metadata has no 'versions' compatibility list",
                 "pinned_release": PINNED_RELEASE}
    compatible = [
        entry for entry in versions
        if isinstance(entry, dict) and str(entry.get("kicad_version", "")).startswith(KICAD_VERSION)
    ]
    return {"checked": True, "declared_versions": versions, "compatible_with_pinned": bool(compatible),
            "pinned_release": PINNED_RELEASE}


def _scan_addon_dir(root: Path, resolved_from: str, manifest_name: str, manifest_type: str) -> dict[str, Any]:
    if not root.is_dir():
        return _absent(root, resolved_from)
    entries = []
    try:
        names = sorted(os.listdir(root))
    except OSError as exc:
        return {"status": "present", "path": str(root), "resolved_from": resolved_from, "entries": [],
                "error": f"{type(exc).__name__}: {exc}"}
    for name in names:
        resolved, reason = _classify_child(root, name)
        if resolved is None:
            entries.append({"type": manifest_type, "package_dir": name,
                            "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason})
            continue
        if not resolved.is_dir():
            continue
        manifest_path = resolved / manifest_name
        if not manifest_path.is_file():
            entries.append({"type": manifest_type, "package_dir": name, "status": "no_manifest_found"})
            continue
        manifest_resolved = _safe_child(resolved, manifest_name)
        if manifest_resolved is None:
            entries.append({"type": manifest_type, "package_dir": name, "status": "manifest_path_rejected"})
            continue
        parsed = _read_json_metadata(manifest_resolved)
        item: dict[str, Any] = {"type": manifest_type, "package_dir": name,
                                 "manifest_sha256": _hash_file(manifest_resolved)}
        if not parsed["valid"]:
            item.update(status="invalid_metadata", error=parsed["error"])
        else:
            item.update(status="ok", metadata=parsed["data"], compatibility=_compatibility(parsed["data"]))
        entries.append(item)
    return {"status": "present", "path": str(root), "resolved_from": resolved_from, "entries": entries}


def _read_installed_packages_index(settings_root: Path, resolved_from: str) -> dict[str, Any]:
    """Read <settings>/installed_packages.json as data only: size capped, parsed with json."""
    name = "installed_packages.json"
    path = settings_root / name
    base = {"path": str(path), "resolved_from": resolved_from}
    if not os.path.lexists(path):
        return {**base, "status": "absent"}
    resolved, reason = _classify_child(settings_root, name)
    if resolved is None:
        return {**base, "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason}
    if not resolved.is_file():
        return {**base, "status": "invalid", "sha256": None, "error": "installed_packages.json is not a regular file"}
    try:
        size = resolved.stat().st_size
        if size > MAX_INDEX_BYTES:
            return {**base, "status": "invalid", "sha256": None, "size": size,
                    "error": f"file larger than {MAX_INDEX_BYTES} bytes; not read"}
        raw = resolved.read_bytes()
    except OSError as exc:
        return {**base, "status": "invalid", "sha256": None, "error": f"{type(exc).__name__}: {exc}"}
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return {**base, "status": "invalid", "sha256": digest, "size": len(raw),
                "error": f"{type(exc).__name__}: {exc}"}
    except ValueError as exc:
        return {**base, "status": "invalid", "sha256": digest, "size": len(raw), "error": f"Invalid JSON: {exc}"}
    return {**base, "status": "ok", "sha256": digest, "size": len(raw), "data": data}


def _scan_pcm_installed_code(root: Path, resolved_from: str) -> dict[str, Any]:
    """Enumerate PCM-installed code under <3rdparty>/<category>/<pkgid>/..., hashes only."""
    if not root.is_dir():
        return _absent(root, resolved_from)
    categories: list[dict[str, Any]] = []
    truncated = False
    try:
        category_names = sorted(os.listdir(root))
    except OSError as exc:
        return {"status": "present", "path": str(root), "resolved_from": resolved_from, "categories": [],
                "truncated": False, "error": f"{type(exc).__name__}: {exc}", "note": _NOT_IMPORTED_NOTE}
    if len(category_names) > MAX_LISTING_ENTRIES:
        category_names, truncated = category_names[:MAX_LISTING_ENTRIES], True
    for category in category_names:
        resolved, reason = _classify_child(root, category)
        if resolved is None:
            categories.append({"category": category, "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason})
            continue
        if not resolved.is_dir():
            continue
        item: dict[str, Any] = {"category": category, "status": "present", "packages": [],
                                "loose_files": [], "truncated": False}
        try:
            package_names = sorted(os.listdir(resolved))
        except OSError as exc:
            item.update(status="unreadable", error=f"{type(exc).__name__}: {exc}")
            categories.append(item)
            continue
        if len(package_names) > MAX_LISTING_ENTRIES:
            package_names, item["truncated"] = package_names[:MAX_LISTING_ENTRIES], True
        for package in package_names:
            child, child_reason = _classify_child(resolved, package)
            if child is None:
                item["packages"].append({"package_id": package, "status": LINK_ENTRY_NOT_FOLLOWED,
                                         "reason": child_reason})
            elif child.is_dir():
                item["packages"].append({"package_id": package, **_walk_tree(child)})
            elif child.is_file() and _is_importable_file_name(package):
                loose = _merge_hash({"filename": package}, _hash_record(child))
                if _is_non_py_importable(package):
                    loose["non_py_importable"] = True
                item["loose_files"].append(loose)
        categories.append(item)
    return {"status": "present", "path": str(root), "resolved_from": resolved_from,
            "categories": categories, "truncated": truncated, "note": _NOT_IMPORTED_NOTE}


def _discover_plugin_json(root: Path, resolved_from: str) -> dict[str, Any]:
    """Recursive plugin.json discovery (bounded depth/entries), mirroring wxDir::Traverse.

    Links resolving outside the tree, dangling links and already-visited directories are reported
    as ``link_entry_not_followed``; nothing outside the tree is read.
    """
    if not root.is_dir():
        return {**_absent(root, resolved_from), "entries": []}
    try:
        tree_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return {**_absent(root, resolved_from), "entries": [], "error": f"{type(exc).__name__}: {exc}"}
    entries: list[dict[str, Any]] = []
    not_descended: list[str] = []
    seen = {tree_root}
    state = {"visited": 0, "truncated": False}

    def link(rel_name: str, reason: str) -> None:
        entries.append({"type": "ipc_plugin_json", "relative_path": rel_name,
                        "status": LINK_ENTRY_NOT_FOLLOWED, "reason": reason})

    def visit(directory: Path, rel: str, depth: int) -> None:
        try:
            names = sorted(os.listdir(directory))
        except OSError as exc:
            link(rel or ".", f"unreadable_directory: {type(exc).__name__}")
            return
        for name in names:
            if state["visited"] >= MAX_DISCOVERY_ENTRIES:
                state["truncated"] = True
                return
            state["visited"] += 1
            rel_name = f"{rel}/{name}" if rel else name
            try:
                resolved = (directory / name).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                link(rel_name, f"dangling_or_unresolvable_link: {type(exc).__name__}")
                continue
            if not _is_within(resolved, tree_root):
                link(rel_name, "resolves_outside_tree")
                continue
            if resolved.is_dir():
                if resolved in seen:
                    link(rel_name, "directory_already_visited")
                    continue
                seen.add(resolved)
                if depth >= MAX_DISCOVERY_DEPTH:
                    if len(not_descended) < MAX_LISTING_ENTRIES:
                        not_descended.append(rel_name)
                    continue
                visit(resolved, rel_name, depth + 1)
                continue
            if name == "plugin.json" and resolved.is_file():
                parsed = _read_json_metadata(resolved)
                item: dict[str, Any] = {"type": "ipc_plugin_json", "relative_path": rel_name}
                record = _hash_record(resolved)
                item["manifest_sha256"] = record["sha256"]
                if record.get("status"):
                    item["hash_status"] = record["status"]
                if not parsed["valid"]:
                    item.update(status="invalid_metadata", error=parsed["error"])
                else:
                    item.update(status="ok", metadata=parsed["data"],
                                compatibility=_compatibility(parsed["data"]))
                entries.append(item)

    visit(root, "", 0)
    return {"status": "present", "path": str(root), "resolved_from": resolved_from, "entries": entries,
            "entries_visited": state["visited"], "truncated": state["truncated"],
            "directories_not_descended": not_descended}


def inventory_plugins() -> dict[str, Any]:
    """Read-only, host-configured metadata inventory. Never follows a caller-supplied root."""
    legacy_root, legacy_src = _legacy_scripting_root()
    legacy_base, _ = _legacy_scripting_base()
    third_root, third_src = _third_party_root()
    ipc_root, ipc_src = _ipc_default_plugins_root()
    user_root, user_src = _user_scripting_root()
    user_base, _ = _user_scripting_base()
    settings_root, settings_src = _settings_root()
    settings_base, _ = _settings_scripting_base()
    settings_plugins, _ = _settings_scripting_root()
    stock_ipc_root, stock_ipc_src = _stock_plugins_root()
    # Documents-derived roots are None (reported, not scanned, no fallback) when
    # KICAD_DOCUMENTS_HOME is set but empty.
    if user_root is None or user_base is None:
        user_plugins_section = _documents_home_empty_section(entries=[])
        user_root_section = _documents_home_empty_section(entries=[])
    else:
        user_plugins_section = _scan_plugins_subdir(
            user_base, user_root, user_src, "user_scripting_plugin_file", "user_scripting_plugin_package")
        user_root_section = _scan_scripting_dir(
            user_base, user_src, "user_scripting_root_module_file", "user_scripting_root_module_package",
            plugins_inventory_key="user_scripting_plugins")
    if third_root is None:
        pcm_section = _documents_home_empty_section(entries=[])
        pcm_code_section = _documents_home_empty_section(categories=[])
        third_discovery = _documents_home_empty_section(entries=[])
    else:
        pcm_section = _scan_addon_dir(third_root, third_src, "metadata.json", "pcm_package")
        pcm_code_section = _scan_pcm_installed_code(third_root, third_src)
        third_discovery = _discover_plugin_json(third_root, third_src)
    if ipc_root is None:
        ipc_section = _documents_home_empty_section(entries=[])
        ipc_discovery = _documents_home_empty_section(entries=[])
    else:
        ipc_section = _scan_addon_dir(ipc_root, ipc_src, "plugin.json", "ipc_addon")
        ipc_discovery = _discover_plugin_json(ipc_root, ipc_src)
    return {
        "kicad_pinned_release": PINNED_RELEASE,
        "kicad_root": _kicad_root_section(),
        "legacy_scripting_plugins": _scan_plugins_subdir(
            legacy_base, legacy_root, legacy_src, "legacy_scripting_plugin_file",
            "legacy_scripting_plugin_package"),
        "legacy_scripting_root_modules": _scan_scripting_dir(
            legacy_base, legacy_src, "legacy_scripting_root_module_file",
            "legacy_scripting_root_module_package", plugins_inventory_key="legacy_scripting_plugins"),
        "user_scripting_plugins": user_plugins_section,
        "user_scripting_root_modules": user_root_section,
        "settings_scripting_root_modules": _scan_scripting_dir(
            settings_base, settings_src, "settings_scripting_root_module_file",
            "settings_scripting_root_module_package", plugins_inventory_key="settings_scripting_plugins"),
        "settings_scripting_plugins": _scan_plugins_subdir(
            settings_base, settings_plugins, settings_src, "settings_scripting_plugin_file",
            "settings_scripting_plugin_package"),
        "pcm_third_party_packages": pcm_section,
        "pcm_installed_package_code": pcm_code_section,
        "installed_packages_index": _read_installed_packages_index(settings_root, settings_src),
        "ipc_addon_plugins": ipc_section,
        "ipc_plugin_json_discovery": {
            "stock_plugins": _discover_plugin_json(stock_ipc_root, stock_ipc_src),
            "third_party": third_discovery,
            "user_plugins": ipc_discovery,
        },
        "settings_root": {"path": str(settings_root),
                          "status": "present" if settings_root.is_dir() else "absent",
                          "resolved_from": settings_src},
    }
