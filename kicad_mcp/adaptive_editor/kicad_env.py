"""Where KiCad and this tool's per-machine settings are, resolved without hardcoded paths.

Dependency-free (stdlib only): imported by the MCP server, by the adaptive engine under KiCad's own
python.exe, and by the KiCad plugin. Every value resolves in this order:

1. an environment variable (names below);
2. the per-machine config file written by ``python -m kicad_mcp.setup``: ``KICAD_AI_TOOLS_CONFIG``
   or ``<user config dir>/kicad-ai-tools/config.json``; never shipped in the package;
3. discovery through the platform backend (Windows: the KiCad uninstall registry entries, then
   ``kicad-cli`` on PATH; macOS / Linux: ``kicad-cli`` on PATH only, reported as untested).

Nothing here names a user, a workstation directory or a company.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

APP_NAME = "kicad-ai-tools"
CONFIG_ENV = "KICAD_AI_TOOLS_CONFIG"
KICAD_ROOT_ENV = "KICAD_APP_PATH"          # KiCad installation root (contains bin/ on Windows)
DOCUMENTS_ENV = "KICAD_DOCUMENTS_HOME"     # honoured by KiCad itself


class KiCadNotFound(RuntimeError):
    code = "kicad_not_found"


class ConfiguredPathError(KiCadNotFound):
    """An explicitly configured location is wrong. Callers must report it, never fall back to discovery."""
    code = "configured_path_invalid"


class KiCadCliFailed(KiCadNotFound):
    """kicad-cli exists but does not run (a broken or partial install). Not a missing install."""
    code = "kicad_cli_failed"


class ConfigError(RuntimeError):
    code = "config_invalid"


# ---------------------------------------------------------------- per-machine config

FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"
FOLDERID_ROAMING_APPDATA = "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}"
FOLDERID_LOCAL_APPDATA = "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}"


def windows_known_folder(folder_id: str) -> Path | None:
    """SHGetKnownFolderPath for a FOLDERID GUID string; None off Windows or on failure."""
    if os.name != "nt":
        return None
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    guid = GUID.from_buffer_copy(uuid.UUID(folder_id).bytes_le)
    out = ctypes.c_wchar_p()
    shell32, ole32 = ctypes.WinDLL("shell32"), ctypes.WinDLL("ole32")
    shell32.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
                                             ctypes.POINTER(ctypes.c_wchar_p)]
    if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return Path(out.value) if out.value else None
    finally:
        ole32.CoTaskMemFree(out)


def user_config_dir() -> Path:
    """The OS per-user config folder. Windows: APPDATA or the RoamingAppData known folder, else an error
    naming the variable to set (never a guess). macOS/Linux: the platform-defined locations."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or windows_known_folder(FOLDERID_ROAMING_APPDATA)
        if base:
            return Path(base)
        raise ConfigError(f"Windows reported no AppData folder; set {CONFIG_ENV} to the config file path")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"  # portability-ok: macOS-defined location
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"  # portability-ok: XDG base-directory default


DATA_DIR_ENV = "KICAD_AI_TOOLS_DATA_DIR"


def user_data_dir() -> Path:
    """Per-user state (manifest cache, workflow jobs): KICAD_AI_TOOLS_DATA_DIR, else the per-machine config's
    ``data_dir``, else the OS data folder (Windows: LOCALAPPDATA or the LocalAppData known folder, else an
    error naming the variable to set; macOS/Linux: the platform-defined locations)."""
    if os.environ.get(DATA_DIR_ENV):
        return Path(os.environ[DATA_DIR_ENV])
    try:
        configured = load_config().get("data_dir")
    except ConfigError:
        configured = None  # a malformed config is reported by whoever reads settings; state still works
    if configured:
        return Path(configured)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or windows_known_folder(FOLDERID_LOCAL_APPDATA)
        if base:
            return Path(base) / APP_NAME
        raise ConfigError(f"Windows reported no LocalAppData folder; set {DATA_DIR_ENV}")
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME  # portability-ok: macOS-defined
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / APP_NAME  # portability-ok: XDG default


def config_path() -> Path:
    env = os.environ.get(CONFIG_ENV)
    return Path(env) if env else user_config_dir() / APP_NAME / "config.json"


def load_config() -> dict:
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, RecursionError) as exc:
        raise ConfigError(f"cannot read the per-machine config {path}: {exc}. Fix or delete it, then run "
                          "python -m kicad_mcp.setup") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"the per-machine config {path} must hold a JSON object; run python -m kicad_mcp.setup")
    return data


def setting(key: str, env: str | None = None, default=None):
    """Environment variable, else the per-machine config file, else ``default``."""
    if env and os.environ.get(env):
        return os.environ[env]
    value = load_config().get(key)
    return default if value in (None, "") else value


# ---------------------------------------------------------------- platform backends

class _Backend:
    name = "generic"
    tested = False
    cli_name = "kicad-cli"

    def discover_roots(self) -> list[Path]:
        cli = shutil.which(self.cli_name)
        return [self.root_from_cli(Path(cli))] if cli else []

    def root_from_cli(self, cli: Path) -> Path:
        return cli.resolve().parent.parent

    def cli(self, root: Path) -> Path:
        return root / "bin" / self.cli_name

    def python(self, root: Path) -> Path | None:
        return None

    def pcbnew(self, root: Path) -> Path | None:
        return None

    def site_packages(self, python: Path) -> Path | None:
        """site-packages of KiCad's bundled interpreter (platform layout knowledge lives here only)."""
        return None


class _WindowsBackend(_Backend):
    name = "windows"
    tested = True
    cli_name = "kicad-cli.exe"

    def discover_roots(self) -> list[Path]:
        roots: list[tuple[tuple[int, ...], Path]] = []
        try:
            import winreg
        except ImportError:  # pragma: no cover - not Windows
            return super().discover_roots()
        hives = ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                 (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY))
        for hive, view in hives:
            try:
                base = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall", 0,
                                      winreg.KEY_READ | view)
            except OSError:
                continue
            with base:
                for i in range(winreg.QueryInfoKey(base)[0]):
                    try:
                        with winreg.OpenKey(base, winreg.EnumKey(base, i)) as key:
                            name = str(winreg.QueryValueEx(key, "DisplayName")[0])
                            location = str(winreg.QueryValueEx(key, "InstallLocation")[0])
                    except OSError:
                        continue
                    if not name.startswith("KiCad") or not location:
                        continue
                    root = Path(location)
                    if self.cli(root).is_file():
                        version = tuple(int(x) for x in re.findall(r"\d+", name)[:3])
                        roots.append((version, root))
        roots.sort(key=lambda item: item[0], reverse=True)
        found = [root for _v, root in roots]
        return found + [r for r in super().discover_roots() if r not in found]

    def python(self, root: Path) -> Path | None:
        p = root / "bin" / "python.exe"
        return p if p.is_file() else None

    def pcbnew(self, root: Path) -> Path | None:
        p = root / "bin" / "pcbnew.exe"
        return p if p.is_file() else None

    def site_packages(self, python: Path) -> Path | None:
        return python.parent / "Lib" / "site-packages"  # KiCad's Windows distribution layout


class _MacBackend(_Backend):
    name = "macos"

    def cli(self, root: Path) -> Path:
        return root / "Contents" / "MacOS" / "kicad-cli"

    def root_from_cli(self, cli: Path) -> Path:
        resolved = cli.resolve()
        return resolved.parents[2] if resolved.parent.name == "MacOS" else resolved.parent.parent


class _LinuxBackend(_Backend):
    name = "linux"


def backend() -> _Backend:
    if os.name == "nt":
        return _WindowsBackend()
    if sys.platform == "darwin":
        return _MacBackend()
    return _LinuxBackend()


# ---------------------------------------------------------------- KiCad locations

def _inside_kicad_python() -> Path | None:
    """When running under KiCad's bundled interpreter (or a venv made from it), its root."""
    base = Path(sys.base_prefix)
    if os.name == "nt" and (base / "Lib" / "site-packages" / "pcbnew.py").is_file() and base.name.lower() == "bin":
        return base.parent
    return None


@lru_cache(maxsize=1)
def kicad_app_root() -> Path:
    """The KiCad installation root. Raises KiCadNotFound with what was tried."""
    tried = []
    configured = setting("kicad_app_root", KICAD_ROOT_ENV)
    if configured:
        root = Path(configured)
        if backend().cli(root).is_file():
            return root
        # An explicit setting that is wrong must be fixed, not silently replaced by discovery.
        raise ConfiguredPathError(f"the configured KiCad root {root} (from {KICAD_ROOT_ENV} or the per-machine config) "
                            f"has no {backend().cli_name}; correct it or run python -m kicad_mcp.setup")
    inside = _inside_kicad_python()
    if inside and backend().cli(inside).is_file():
        return inside
    for root in backend().discover_roots():
        if backend().cli(root).is_file():
            return root
        tried.append(f"discovered {root} (no kicad-cli)")
    raise KiCadNotFound("KiCad was not found. Install KiCad 10, or run `python -m kicad_mcp.setup`, or set "
                        f"{KICAD_ROOT_ENV}. Tried: {tried or ['registry/PATH discovery: nothing']}")


def kicad_cli() -> Path:
    """KICAD_CLI_PATH / config "kicad_cli" when set (must exist), else <KiCad root>/bin/kicad-cli."""
    explicit = setting("kicad_cli", "KICAD_CLI_PATH")
    if explicit:
        if not Path(explicit).is_file():
            raise ConfiguredPathError(f"KICAD_CLI_PATH / config kicad_cli points at {explicit}, which does not exist")
        return Path(explicit)
    return backend().cli(kicad_app_root())


def kicad_python() -> Path | None:
    """KiCad's bundled interpreter that can import pcbnew; None where the platform has none."""
    configured = setting("kicad_python", "KICAD_AI_TOOLS_KICAD_PYTHON")
    if configured:
        if not Path(configured).is_file():
            raise ConfiguredPathError(f"KICAD_AI_TOOLS_KICAD_PYTHON / config kicad_python points at {configured}, "
                                      "which does not exist")
        return Path(configured)
    return backend().python(kicad_app_root())


def kicad_site_packages() -> Path | None:
    """site-packages of KiCad's bundled python, from the platform backend; None where unknown."""
    py = kicad_python()
    return backend().site_packages(py) if py else None


def pcbnew_exe() -> Path | None:
    return backend().pcbnew(kicad_app_root())


@lru_cache(maxsize=1)
def kicad_version() -> str:
    """Full KiCad version reported by kicad-cli, e.g. ``10.0.5``."""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    cli = kicad_cli()
    try:
        done = subprocess.run([str(cli), "version"], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KiCadCliFailed(f"kicad-cli at {cli} did not run: {exc}") from exc
    match = re.search(r"\d+\.\d+(?:\.\d+)?", done.stdout or "")
    if done.returncode != 0 or not match:
        raise KiCadCliFailed(f"kicad-cli at {cli} was found but 'version' failed: "
                             f"{(done.stderr or done.stdout).strip()[:200]}")
    return match.group(0)


def settings_version() -> str:
    """The ``major.minor`` directory KiCad uses for settings and user plugins (``10.0``)."""
    return ".".join(kicad_version().split(".")[:2])


# ---------------------------------------------------------------- documents folder

def _windows_documents_folder() -> Path | None:
    return windows_known_folder(FOLDERID_DOCUMENTS)


def documents_dir() -> Path:
    """KICAD_DOCUMENTS_HOME, else the OS-defined Documents folder: the Windows known folder (redirected
    folders honoured), XDG_DOCUMENTS_DIR on Linux, the macOS standard folder. Never a guess on Windows:
    if the OS cannot say, this raises with the variable to set."""
    if os.environ.get(DOCUMENTS_ENV):
        return Path(os.environ[DOCUMENTS_ENV])
    if os.name == "nt":
        known = _windows_documents_folder()
        if known:
            return known
        raise KiCadNotFound(f"Windows did not report a Documents folder; set {DOCUMENTS_ENV}")
    if sys.platform != "darwin" and os.environ.get("XDG_DOCUMENTS_DIR"):
        return Path(os.environ["XDG_DOCUMENTS_DIR"])
    return Path.home() / "Documents"  # portability-ok: macOS-defined location / XDG default on Linux


def user_plugins_dir() -> Path:
    """KiCad's per-user IPC plugin directory: <documents>/KiCad/<major.minor>/plugins."""
    return documents_dir() / "KiCad" / settings_version() / "plugins"


def resolve_kicad_cli() -> tuple[Path | None, str | None]:
    """The one kicad-cli resolution every caller uses: ``(path, None)`` or ``(None, error)``.

    Explicit settings (KICAD_CLI_PATH, KICAD_APP_PATH, the per-machine config) and a malformed config are
    errors to report, never a reason to try somewhere else. Discovery already includes PATH."""
    try:
        cli = kicad_cli()
    except (KiCadNotFound, ConfigError) as exc:
        return None, f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
    if not cli.is_file():
        return None, f"kicad_not_found: {cli} does not exist"
    return cli, None


SETTINGS = (  # (config key, environment variable, meaning) for setup --check and the docs
    ("kicad_app_root", KICAD_ROOT_ENV, "KiCad install root"),
    ("kicad_cli", "KICAD_CLI_PATH", "kicad-cli executable"),
    ("kicad_python", "KICAD_AI_TOOLS_KICAD_PYTHON", "KiCad's bundled python"),
    ("kicad_user_dir", "KICAD_USER_DIR", "KiCad documents folder"),
    ("job_dir", "KICAD_MCP_JOB_DIR", "workflow job journals"),
    ("data_dir", DATA_DIR_ENV, "per-user state (manifest cache, default job folder)"),
    ("quarantine_root", "KICAD_MCP_QUARANTINE_ROOT", "offline plugin-inspection quarantine (L3)"),
    ("server_idle_seconds", "KICAD_AI_TOOLS_SERVER_IDLE_SECONDS", "idle seconds before the server stops"),
    ("server_python", "KICAD_AI_TOOLS_SERVER_PYTHON", "server interpreter"),
    ("boardtooling_root", "KICAD_AI_TOOLS_BOARDTOOLING_ROOT", "BoardTooling checkout"),
    ("export_python", "KICAD_AI_TOOLS_EXPORT_PYTHON", "BoardTooling exporter interpreter"),
    ("powershell", "KICAD_AI_TOOLS_POWERSHELL", "PowerShell 7"),
    ("boardtooling_trust_config", "BOARDTOOLING_TRUST_CONFIG", "BoardTooling trust configuration"),
    ("boardtooling_visual_review_public_key", "BOARDTOOLING_VISUAL_REVIEW_PUBLIC_KEY", "visual-review public key"),
)


def explain_settings() -> list[dict]:
    """Where every setting currently comes from: environment, per-machine config, or unset (default)."""
    try:
        config = load_config()
        config_error = None
    except ConfigError as exc:
        config, config_error = {}, str(exc)
    out = []
    for key, env, meaning in SETTINGS:
        if os.environ.get(env):
            source, value = f"environment {env}", os.environ[env]
        elif config.get(key) not in (None, ""):
            source, value = "per-machine config", config[key]
        else:
            source, value = "default / discovery", None
        out.append({"key": key, "env": env, "meaning": meaning, "source": source, "value": value})
    if config_error:
        out.append({"key": "<config file>", "env": CONFIG_ENV, "meaning": "per-machine config", "source": "error",
                    "value": config_error})
    return out


def describe() -> dict:
    """Everything resolved, for setup and capability reports (no exceptions)."""
    out: dict = {"platform_backend": backend().name, "backend_tested": backend().tested}
    try:
        out.update(config_path=str(config_path()), config_present=config_path().is_file())
    except ConfigError as exc:
        out.update(config_path=None, config_present=False, error=f"config_invalid: {exc}")
        return out
    try:
        root = kicad_app_root()
        out.update(kicad_app_root=str(root), kicad_cli=str(kicad_cli()),
                   kicad_python=str(kicad_python()) if kicad_python() else None,
                   pcbnew=str(pcbnew_exe()) if pcbnew_exe() else None)
        out["kicad_version"] = kicad_version()
        out["user_plugins_dir"] = str(user_plugins_dir())
    except Exception as exc:  # report, never raise
        out["error"] = f"{getattr(exc, 'code', type(exc).__name__)}: {exc}"
    return out
