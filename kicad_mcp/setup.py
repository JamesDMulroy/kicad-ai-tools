"""One-command per-machine setup for kicad-ai-tools.

    python -m kicad_mcp.setup                    # discover, write the per-machine config, print the MCP entry
    python -m kicad_mcp.setup --install-plugin   # ... and install the KiCad plugin for this user
    python -m kicad_mcp.setup --boardtooling DIR # ... and enable the optional BoardTooling workflow
    python -m kicad_mcp.setup --check            # report only; write nothing
    python -m kicad_mcp.setup --forget kicad_app_root   # drop a stale stored setting

Writes only the per-machine config file (``kicad_env.config_path()``), and the plugin directory when
asked. The MCP client entry it prints is built from this machine's interpreter and this checkout's
location; nothing in the package names a machine path.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from kicad_mcp.adaptive_editor import kicad_env

ROOT = Path(__file__).resolve().parents[1]
SERVER_NAME = "kicad_ai_tools"  # default MCP server name; --server-name keeps an existing registration


# Every location/behaviour variable set while setup ran is carried into the client entry, so the server
# resolves exactly what setup validated (KiCad's own redirect variables included).
CARRIED_ENV = tuple(dict.fromkeys(
    [kicad_env.CONFIG_ENV, kicad_env.DATA_DIR_ENV, kicad_env.DOCUMENTS_ENV, "KICAD_CONFIG_HOME",
     "KICAD_MCP_QUARANTINE_ROOT", "KICAD_MCP_L4_APPROVED_REGISTRY_SHA256"]
    + [env for _key, env, _meaning in kicad_env.SETTINGS]))


def _carried_env() -> dict:
    carried = {k: os.environ[k] for k in CARRIED_ENV if os.environ.get(k)}
    for k, v in os.environ.items():  # KiCad's own versioned overrides, e.g. KICAD10_3RD_PARTY
        if v and re.fullmatch(r"KICAD\d+_3RD_PARTY", k.upper()):
            carried[k] = v
    return carried


class EntryError(RuntimeError):
    """No client entry can be produced with this interpreter."""


def client_entry() -> dict:
    """The stdio server entry for an MCP client (Claude Code, Claude Desktop, others)."""
    exe = Path(sys.executable)
    if os.name == "nt":
        pythonw = exe.with_name("pythonw.exe")
        if not pythonw.is_file():
            raise EntryError(f"pythonw.exe not found beside {exe}; run setup with the Python that will serve the tools")
        shim = ROOT / "silent_stdio.pyw"  # present in a source checkout; an installed package uses -m
        args = [str(shim)] if shim.is_file() else ["-m", "kicad_mcp.launcher"]
        return {"type": "stdio", "command": str(pythonw), "args": args, "env": _carried_env()}
    return {"type": "stdio", "command": str(exe), "args": ["-m", "kicad_mcp.launcher"], "env": _carried_env()}


def read_config_for_update() -> tuple[dict, str | None]:
    """The current per-machine config, or {} after moving a malformed file aside (so setup can repair it)."""
    path = kicad_env.config_path()
    if not path.is_file():
        return {}, None
    try:
        return kicad_env.load_config(), None
    except kicad_env.ConfigError:
        backup = path.with_name(path.name + f".broken-{int(time.time())}")
        os.replace(path, backup)
        return {}, str(backup)


def write_config(values: dict, forget=()) -> Path:
    """Merge explicit values (None = leave unchanged) and drop the keys in ``forget``. Only what the user
    passed is stored; discovered values are never pinned, so a moved or upgraded KiCad is found again."""
    path = kicad_env.config_path()
    current, _ = read_config_for_update()
    current.update({k: v for k, v in values.items() if v is not None})
    for key in forget:
        current.pop(key, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(current, indent=1), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _clear_caches() -> None:
    kicad_env.kicad_app_root.cache_clear()
    kicad_env.kicad_version.cache_clear()


def main(argv=None) -> int:
    keys = [key for key, _env, _meaning in kicad_env.SETTINGS]
    ap = argparse.ArgumentParser(description="kicad-ai-tools per-machine setup")
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument("--kicad-root", help="pin this KiCad install root (otherwise it is discovered each time)")
    ap.add_argument("--boardtooling", help="enable the optional BoardTooling workflow at this directory")
    ap.add_argument("--idle-seconds", type=float, help="stop the MCP server after this long unused (default 600)")
    ap.add_argument("--forget", action="append", choices=keys, default=[],
                    help="remove a stored setting (e.g. a stale kicad_app_root); repeatable")
    ap.add_argument("--install-plugin", action="store_true", help="install the KiCad Adaptive Editor plugin")
    ap.add_argument("--server-name", default=SERVER_NAME, help="MCP server entry name (default %(default)s)")
    a = ap.parse_args(argv)

    report: dict = {}
    try:
        kicad_env.config_path()
    except kicad_env.ConfigError as exc:  # e.g. Windows reports no AppData folder
        report["kicad"] = kicad_env.describe()
        report["fix"] = f"{exc}"
        print(json.dumps(report, indent=1))
        return 2
    if a.boardtooling and not (Path(a.boardtooling) / "scripts").is_dir():
        raise SystemExit(f"{a.boardtooling} does not look like a BoardTooling checkout (no scripts/)")
    ambient_root = os.environ.get(kicad_env.KICAD_ROOT_ENV)
    if a.check and a.kicad_root:
        os.environ[kicad_env.KICAD_ROOT_ENV] = a.kicad_root  # report on the root asked about (restored below)
        _clear_caches()
    if not a.check:
        if kicad_env.config_path().is_file():
            _, moved = read_config_for_update()
            if moved:
                report["malformed_config_moved_to"] = moved
        if a.kicad_root:
            os.environ[kicad_env.KICAD_ROOT_ENV] = a.kicad_root  # validated by describe() below
        _clear_caches()
        probe = kicad_env.describe()
        if a.kicad_root and "error" in probe:
            report["kicad"] = probe
            print(json.dumps(report, indent=1))
            return 2  # nothing written: the explicit root is wrong
        report["config_written"] = str(write_config({
            "kicad_app_root": str(Path(a.kicad_root).resolve()) if a.kicad_root else None,
            "boardtooling_root": str(Path(a.boardtooling).resolve()) if a.boardtooling else None,
            "server_idle_seconds": a.idle_seconds}, forget=a.forget))
        if a.kicad_root:
            os.environ.pop(kicad_env.KICAD_ROOT_ENV, None)
        _clear_caches()
    report["kicad"] = kicad_env.describe()
    report["settings"] = kicad_env.explain_settings()
    if a.check and a.kicad_root:  # the probe must not leak into the client entry (it carries KICAD_APP_PATH)
        if ambient_root is None:
            os.environ.pop(kicad_env.KICAD_ROOT_ENV, None)
        else:
            os.environ[kicad_env.KICAD_ROOT_ENV] = ambient_root
        _clear_caches()
    try:
        entry = client_entry()
        report["mcp_server_entry"] = {a.server_name: entry}  # for an mcpServers map in a client's JSON config
        report["claude_code_add_json"] = json.dumps(entry)  # the '<entry>' argument of `claude mcp add-json`
    except EntryError as exc:
        report["mcp_server_entry"] = {"error": str(exc)}
    if "error" in report["kicad"] or "error" in report["mcp_server_entry"]:
        if str(report["kicad"].get("error", "")).startswith(kicad_env.KiCadCliFailed.code):
            report["fix"] = ("kicad-cli was found but does not run: repair or reinstall that KiCad, or point "
                             "KICAD_CLI_PATH at a working kicad-cli.")
        else:
            report["fix"] = ("KiCad is not usable with the current settings. If a stored kicad_app_root or kicad_cli "
                             "is stale, run: python -m kicad_mcp.setup --forget kicad_app_root --forget kicad_cli ; "
                             "or pass --kicad-root <KiCad install folder>.")
        print(json.dumps(report, indent=1))
        return 2
    if a.install_plugin and not a.check:
        installer = ROOT / "kicad_mcp" / "adaptive_editor" / "plugin" / "install.py"
        try:
            done = subprocess.run([sys.executable, "-B", str(installer), "install"], capture_output=True, text=True,
                                  creationflags=0x08000000 if os.name == "nt" else 0, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as exc:
            done = None
            report["plugin_install"] = {"error": f"installer did not complete: {exc}"}
        if done is not None:
            try:
                report["plugin_install"] = (json.loads(done.stdout) if done.returncode == 0
                                            else {"error": done.stderr[-1500:]})
            except ValueError:
                report["plugin_install"] = {"error": "installer output was not JSON", "stdout": done.stdout[-1500:],
                                            "stderr": done.stderr[-1500:]}
    report["next"] = (f"Claude Code: claude mcp add-json {a.server_name} '<claude_code_add_json>' --scope user . "
                      "Other clients: merge mcp_server_entry into their 'mcpServers' map. The server starts only "
                      "when a tool is used.")
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
