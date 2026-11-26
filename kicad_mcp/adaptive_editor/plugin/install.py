"""Install / uninstall the Adaptive Editor IPC plugin with an ownership record.

    python install.py install   [--documents DIR | --target DIR] [--dry-run]
    python install.py uninstall [--documents DIR | --target DIR] [--dry-run]
    python install.py status    [--documents DIR | --target DIR]

The plugin goes to <documents>/KiCad/<major.minor>/plugins/local.kicad_ai_tools.adaptive_editor, which KiCad scans
(PATHS::GetUserPluginsPath; api_plugin_manager.cpp ReloadPlugins). <documents> defaults to
KICAD_DOCUMENTS_HOME, else the OS Documents folder (kicad_env). The install writes `.install-record.json`
listing every file with its sha256 and every directory it created. Uninstall removes only recorded
files whose bytes still match (a file the user changed is kept and reported), the bytecode Python wrote
for those modules, and then the directories the installer created, if they are empty. An existing
target directory without a record from this installer is never overwritten; an upgrade refuses to
overwrite a locally changed or unowned file; an interrupted install stays recoverable (pending hashes).

KiCad creates the plugin's Python venv itself on next start (or Preferences > Plugins > Reload)
and pip-installs requirements.txt (pinned) into it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

PLUGIN_ID = "local.kicad_ai_tools.adaptive_editor"
# KiCad 10.0.5 API_PLUGIN::IsValidIdentifier (common/api/api_plugin.cpp): reverse-DNS, word characters only.
# It is stricter than api.v1.schema.json; a plugin failing it is dropped silently. ".local" claims no domain.
KICAD_IDENTIFIER_RULE = r"[\w\d]{2,}\.[\w\d]+\.[\w\d]+"
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # adaptive_editor/ (kicad_env)
import kicad_env  # noqa: E402
ENGINE_SRC = HERE.parent  # kicad_mcp/adaptive_editor (engine, live, kicad_env)
RECORD = ".install-record.json"
PART_SUFFIX = ".installing"  # + "-<install id>": a copy in progress, replaced into place when complete
PLUGIN_FILES = ("plugin.json", "requirements.txt", "adaptive_drag.py", "adaptive_edge.py", "panel.py")
LIVE_FILES = ("live_session.py", "engine_worker.py", "preview_render.py")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_rel(rel) -> bool:
    """Whether a record path is a safe, normalized plugin-relative POSIX path."""
    if not isinstance(rel, str) or not rel or "\\" in rel or ":" in rel or rel.startswith("/"):
        return False
    return all(part not in ("..", ".", "") for part in PurePosixPath(rel).parts)


def default_target(documents: Path | None = None) -> Path:
    """<documents>/KiCad/<major.minor of the installed KiCad>/plugins/<plugin id> (kicad_env discovery)."""
    if documents is None:
        return kicad_env.user_plugins_dir() / PLUGIN_ID
    return documents / "KiCad" / kicad_env.settings_version() / "plugins" / PLUGIN_ID


def sources() -> list[tuple[Path, str]]:
    """(source file, path relative to the plugin dir) for everything that is installed."""
    out = [(HERE / f, f) for f in PLUGIN_FILES]
    out += [(p, f"icons/{p.name}") for p in sorted((HERE / "icons").glob("*.png"))]
    out += [(ENGINE_SRC / "live" / f, f"engine/live/{f}") for f in LIVE_FILES]
    out += [(ENGINE_SRC / "kicad_env.py", "engine/kicad_env.py")]
    pkg = ENGINE_SRC / "adaptive_engine"
    out += [(p, f"engine/adaptive_engine/{p.relative_to(pkg).as_posix()}") for p in sorted(pkg.rglob("*.py"))]
    missing = [str(s) for s, _ in out if not s.is_file()]
    if missing:
        raise SystemExit(f"missing source files: {missing}")
    return out


def _parse_record(path: Path) -> dict | None:
    """A complete ownership record for this plugin, or None (missing, truncated, or someone else's)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("plugin") != PLUGIN_ID or not isinstance(data.get("files"), dict):
        return None
    pending = data.get("pending", {})
    dirs = data.get("dirs", [])
    if (not isinstance(pending, dict) or not isinstance(dirs, list)
            or any(not _safe_rel(rel) for rel in data["files"])
            or any(not _safe_rel(rel) for rel in pending)
            or any(not _safe_rel(rel) for rel in dirs)):
        return None
    return data


def _read_record(target: Path) -> dict | None:
    """The ownership record. Records are written to a unique, fsynced temporary file and renamed into place,
    so after any interruption either RECORD is complete or a complete temporary record is (the newest one
    wins); a truncated temporary file is never trusted."""
    rec = target / RECORD
    if rec.is_file():
        data = _parse_record(rec)
        if data is None:
            raise SystemExit(f"{rec} is not a readable ownership record for {PLUGIN_ID}; refusing")
        return data
    pending = sorted((p for p in target.glob(RECORD + ".tmp*") if p.is_file()),
                     key=lambda p: p.stat().st_mtime, reverse=True) if target.is_dir() else []
    for path in pending:  # interrupted between writing the record and renaming it: the written record is ours
        data = _parse_record(path)
        if data is not None:
            return data
    return None


def _write_record(target: Path, record: dict) -> None:
    tmp = target / f"{RECORD}.tmp-{uuid.uuid4().hex[:12]}"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(record, indent=1))
        fh.flush()
        os.fsync(fh.fileno())  # durable before the rename makes it the record
    os.replace(tmp, target / RECORD)
    for leftover in target.glob(RECORD + ".tmp*"):  # earlier interrupted writes (this installer's names only)
        try:
            leftover.unlink()
        except OSError:
            pass


def _create_target(target: Path, record: dict) -> None:
    """Create the plugin folder with its ownership record already inside, atomically: the folder is built
    under a unique sibling name and renamed into place, so the target never exists without a record
    (created_target is therefore always known). An interrupted attempt leaves only a sibling folder holding
    nothing but this plugin's record, which the next install removes."""
    staging = target.parent / f".{target.name}.creating-{uuid.uuid4().hex[:12]}"
    staging.mkdir(parents=True)
    _write_record(staging, record)
    os.rename(staging, target)


def _remove_abandoned_staging(target: Path) -> None:
    """Sibling folders an interrupted first install left: removed only if they hold nothing but a complete
    ownership record of this plugin (proven ours)."""
    if not target.parent.is_dir():
        return
    for staging in target.parent.glob(f".{target.name}.creating-*"):
        if not staging.is_dir():
            continue
        contents = list(staging.iterdir())
        # only this installer's record names (complete, pending or truncated) may be inside: the folder name
        # carries this plugin's id and a creating-<id> suffix only this installer writes
        if all(p.is_file() and (p.name == RECORD or p.name.startswith(RECORD + ".tmp")) for p in contents):
            for p in contents:
                p.unlink()
            staging.rmdir()


def _remove_own_parts(target: Path, record: dict) -> None:
    """Delete partial copies an interrupted install left: only names carrying the record's own part suffix."""
    suffix = record.get("part_suffix")
    if not suffix:
        return
    for rel in set(record["files"]) | set(record.get("pending", {})):
        part = target / (rel + suffix)
        if part.is_file():
            part.unlink()


def _owned_hashes(record: dict, rel: str) -> set[str]:
    """Hashes a file may have and still be ours: the installed one, or the one an interrupted install
    was writing (``pending``)."""
    return {h for h in (record["files"].get(rel), record.get("pending", {}).get(rel)) if h}


def check_manifest() -> dict:
    """The manifest must pass KiCad's own identifier rule and name this installer's PLUGIN_ID."""
    import re
    manifest = json.loads((HERE / "plugin.json").read_text(encoding="utf-8"))
    ident = manifest.get("identifier", "")
    if ident != PLUGIN_ID or not re.search(KICAD_IDENTIFIER_RULE, ident):
        raise SystemExit(f"plugin.json identifier {ident!r} is not {PLUGIN_ID!r} or fails KiCad's identifier rule")
    return manifest


def install(target: Path, dry_run: bool) -> dict:
    check_manifest()
    record = _read_record(target)
    if target.exists() and any(target.iterdir()) and record is None:
        raise SystemExit(f"{target} exists and was not installed by this installer; refusing to overwrite")
    plan = sources()
    files = {rel: _sha(src) for src, rel in plan}
    if record:
        # Upgrade: every file we would overwrite must be ours and unchanged (or from an interrupted install).
        changed = [rel for rel in set(record["files"]) | set(record.get("pending", {}))
                   if (target / rel).is_file() and _sha(target / rel) not in _owned_hashes(record, rel)]
        if changed:
            raise SystemExit(f"installed files were modified locally, refusing to overwrite: {sorted(changed)}")
    owned = set(record["files"]) | set(record.get("pending", {})) if record else set()
    unowned = sorted(rel for rel in files if rel not in owned and (target / rel).exists())
    if unowned:
        raise SystemExit(f"files not installed by this installer are in the way, refusing: {unowned}")
    created_target = record.get("created_target", False) if record else not target.exists()
    result = {"action": "install", "target": str(target), "files": len(files), "dry_run": dry_run}
    if dry_run:
        return result
    _remove_abandoned_staging(target)
    dirs = set(record.get("dirs", [])) if record else set()
    for _src, rel in plan:  # directories we create are recorded so uninstall removes only those
        parent = Path(rel).parent
        while str(parent) not in ("", "."):
            if not (target / parent).exists():
                dirs.add(parent.as_posix())
            parent = parent.parent
    if record:
        _remove_own_parts(target, record)  # an earlier interrupted install's partial copies
    base = {"plugin": PLUGIN_ID, "source": str(HERE), "created_target": created_target,
            "dirs": sorted(dirs), "files": record["files"] if record else {}}
    if not target.exists():
        _create_target(target, base)  # the folder appears together with its record
    elif target.is_dir() and record is None:
        target.mkdir(parents=True, exist_ok=True)  # an existing empty folder (not created by us)
    part_suffix = f"{PART_SUFFIX}-{uuid.uuid4().hex[:12]}"  # unique to this install: never someone else's file
    # an interruption from here on stays recoverable: the record names the pending files and their parts
    _write_record(target, {**base, "pending": files, "part_suffix": part_suffix})
    for src, rel in plan:
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        part = dst.with_name(dst.name + part_suffix)
        if part.exists():
            raise SystemExit(f"{part} already exists and is not this install's; refusing")
        shutil.copyfile(src, part)  # an interrupted copy never leaves a truncated installed file
        with open(part, "rb+") as fh:
            os.fsync(fh.fileno())  # durable before the rename puts it in place (power loss included)
        os.replace(part, dst)
    # stale: files of the previous install AND files an interrupted install had already copied (pending)
    stale = sorted((set(record["files"]) | set(record.get("pending", {}))) - set(files)) if record else []
    for rel in stale:
        p = target / rel
        if p.is_file() and _sha(p) in _owned_hashes(record, rel):
            p.unlink()
    _write_record(target, {**base, "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files})
    result["removed_stale"] = stale
    return result


def _remove_own_bytecode(directory: Path, recorded_py: set[str]) -> None:
    """Delete only bytecode Python wrote for our recorded modules (<stem>.<tag>.pyc in __pycache__)."""
    cache = directory / "__pycache__"
    if not cache.is_dir() or cache.is_symlink():
        return
    for pyc in cache.iterdir():
        stem = pyc.name.split(".", 1)[0]
        if pyc.is_file() and pyc.suffix == ".pyc" and stem in recorded_py:
            pyc.unlink()
    if not any(cache.iterdir()):
        cache.rmdir()


def uninstall(target: Path, dry_run: bool) -> dict:
    record = _read_record(target)
    if record is None:
        return {"action": "uninstall", "target": str(target), "status": "not_installed"}
    removed, kept = [], []
    if not dry_run:
        _remove_own_parts(target, record)  # partial copies named with this install's own id only
    for rel in sorted(set(record["files"]) | set(record.get("pending", {}))):
        p = target / rel
        if not p.is_file():
            continue
        if _sha(p) in _owned_hashes(record, rel):
            removed.append(rel)
            if not dry_run:
                p.unlink()
        else:
            kept.append(rel)
    if not dry_run:
        by_dir: dict[str, set[str]] = {}
        for rel in removed:
            if rel.endswith(".py"):
                by_dir.setdefault(Path(rel).parent.as_posix(), set()).add(Path(rel).stem)
        for d, stems in by_dir.items():
            _remove_own_bytecode(target / d, stems)
        for d in sorted(record.get("dirs", []), key=lambda s: -len(Path(s).parts)):
            p = target / d
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        if not kept:
            (target / RECORD).unlink(missing_ok=True)
            for leftover in target.glob(RECORD + ".tmp*"):  # interrupted record writes (this installer's names)
                leftover.unlink()
            if record.get("created_target") and target.is_dir() and not any(target.iterdir()):
                target.rmdir()
    return {"action": "uninstall", "target": str(target), "removed": len(removed), "kept_modified": kept,
            "left_in_place": sorted(str(x.relative_to(target)) for x in target.rglob("*"))[:50]
            if target.exists() else [], "dry_run": dry_run}


def status(target: Path) -> dict:
    record = _read_record(target)
    if record is None:
        return {"target": str(target), "installed": False}
    drift = [rel for rel, h in record["files"].items() if not (target / rel).is_file() or _sha(target / rel) != h]
    src = {rel: _sha(s) for s, rel in sources()}
    outdated = sorted(rel for rel in src if record["files"].get(rel) != src[rel])
    return {"target": str(target), "installed": True, "installed_at": record.get("installed_at"),
            "files": len(record["files"]), "modified_or_missing": drift, "outdated_vs_source": outdated}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("action", choices=("install", "uninstall", "status"))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--documents", help="KiCad documents home (contains KiCad/<major.minor>/plugins)")
    g.add_argument("--target", help="exact plugin directory")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    target = Path(a.target) if a.target else default_target(Path(a.documents) if a.documents else None)
    if a.action == "install":
        out = install(target, a.dry_run)
    elif a.action == "uninstall":
        out = uninstall(target, a.dry_run)
    else:
        out = status(target)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
