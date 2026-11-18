"""Headless KiCad sessions over the official IPC API (``kicad-cli api-server``), on a candidate copy.

A session never opens the caller's files. It copies the project's KiCad files into a private candidate
folder, records each original's sha256, and serves the API from the copy. Changes reach the real project
only through ``promote()``, which refuses if any original changed since the copy was taken and keeps a
byte-exact backup of every file it replaces.

One server serves one document type: a schematic session answers schematic requests only and a board
session board requests only (KiCad 11 headless behaviour), so a session is opened per document.

The server runs with no window, in a job object that owns its whole process tree: closing the session,
or this process exiting, ends it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from kicad_mcp.ipc.client import ApiCallError, Client
from kicad_mcp.ipc.schema import load as load_schema

# Files that make up a KiCad project; copied into the candidate so the server sees the full project.
PROJECT_PATTERNS = ("*.kicad_pro", "*.kicad_prl", "*.kicad_sch", "*.kicad_pcb", "*.kicad_dru",
                    "*.kicad_wks", "sym-lib-table", "fp-lib-table", "design-block-lib-table")
PROJECT_LIBRARY_PATTERNS = ("*.kicad_sym", "*.pretty", "*.kicad_blocks",
                            # what the project refers to by ${KIPRJMOD}: SPICE models and the simulator's
                            # workbook, IBIS models, local 3D models (a simulation demo names its diode.mod)
                            "*.mod", "*.lib", "*.cir", "*.sub", "*.sp", "*.spice", "*.inc", "*.ibs", "*.wbk",
                            "3dmodels", "*.3dshapes")
DOCUMENT_SUFFIXES = (".kicad_sch", ".kicad_pcb")
READY_TIMEOUT_S = 90.0


class SessionError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_kicad_cli() -> str:
    """The kicad-cli to serve the API: $KICAD_CLI, else the newest install that has ``api-server``."""
    explicit = os.environ.get("KICAD_CLI")
    if explicit:
        return explicit
    candidates: list[Path] = []
    if sys.platform == "win32":
        for root in (Path.home(), Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))):  # portability-ok: platform default when the variable is unset
            candidates += root.glob("KiCad*/bin/kicad-cli.exe")
            candidates += root.glob("KiCad/*/bin/kicad-cli.exe")
    else:
        candidates += [Path(p) for p in ("/usr/bin/kicad-cli", "/usr/local/bin/kicad-cli",
                                         "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")]
    for cli in sorted(candidates, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if cli.exists() and _has_api_server(str(cli)):
            return str(cli)
    raise SessionError("no kicad-cli with 'api-server' found (KiCad 11 or a nightly); set KICAD_CLI")


def _has_api_server(cli: str) -> bool:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        out = subprocess.run([cli, "--help"], capture_output=True, text=True, timeout=30, creationflags=flags)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "api-server" in out.stdout


@dataclass
class Candidate:
    """A private copy of a project's KiCad files, with the originals' hashes at copy time."""

    source_dir: Path
    work_dir: Path
    originals: dict[str, str] = field(default_factory=dict)  # file name -> sha256 at copy time
    libraries: set[str] = field(default_factory=set)  # project-local libraries copied for resolution

    @classmethod
    def of(cls, document: Path, root: Path | None = None) -> Candidate:
        source_dir = document.resolve().parent
        base = Path(root or tempfile.gettempdir()) / "kicad-ai-tools" / "candidates"
        work_dir = base / f"{document.stem}-{uuid.uuid4().hex[:10]}"
        work_dir.mkdir(parents=True)
        candidate = cls(source_dir, work_dir)
        for pattern in PROJECT_PATTERNS:
            for path in source_dir.glob(pattern):
                if path.is_file():
                    shutil.copy2(path, work_dir / path.name)
                    candidate.originals[path.name] = sha256(path)
        # Project-local libraries (${KIPRJMOD}/...) go along read-only-in-spirit: the server must resolve
        # the project's own symbols and footprints, but promote() never writes them back.
        for pattern in PROJECT_LIBRARY_PATTERNS:
            for path in source_dir.glob(pattern):
                if path.is_dir():
                    shutil.copytree(path, work_dir / path.name)
                elif path.is_file():
                    shutil.copy2(path, work_dir / path.name)
                candidate.libraries.add(path.name)
        if document.name not in candidate.originals:
            raise SessionError(f"{document} is not a KiCad document in its project folder")
        return candidate

    def changed_files(self) -> list[str]:
        """Candidate files whose bytes differ from the originals as copied (new files included)."""
        changed = []
        for path in sorted(self.work_dir.iterdir()):
            if path.name.startswith("~") or path.suffix in (".lck", ".kicad_prl") or path.name in self.libraries:
                continue  # KiCad's lock and per-user view state: never promoted into the project
            if path.is_file() and sha256(path) != self.originals.get(path.name):
                changed.append(path.name)
        return changed

    def promote(self, backup_root: Path | None = None) -> dict:
        """Copy changed files back, refusing if any original changed since the copy; back up first."""
        changed = self.changed_files()
        drifted = [name for name in changed if (self.source_dir / name).exists()
                   and sha256(self.source_dir / name) != self.originals.get(name)]
        if drifted:
            raise SessionError(f"originals changed since the candidate was made: {drifted}; nothing written")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = Path(backup_root or self.source_dir / ".kicad-ai-tools-backups") / stamp
        written = []
        for name in changed:
            target = self.source_dir / name
            if target.exists():
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup_dir / name)
            shutil.copy2(self.work_dir / name, target)
            self.originals[name] = sha256(target)  # promoted bytes are the new baseline
            written.append({"file": name, "sha256": self.originals[name]})
        return {"written": written, "backup_dir": str(backup_dir) if written else None}

    def discard(self) -> None:
        shutil.rmtree(self.work_dir, ignore_errors=True)


class HeadlessSession:
    """A windowless ``kicad-cli api-server`` serving one document of a candidate copy."""

    def __init__(self, document: str | Path, cli: str | None = None, candidate_root: Path | None = None):
        self.started_at = time.time()  # before any folder of this session exists (stray-check evidence)
        document = Path(document)
        if document.suffix not in DOCUMENT_SUFFIXES:
            raise SessionError(f"a session serves a .kicad_sch or .kicad_pcb, not {document.name}")
        self.cli = cli or find_kicad_cli()
        self.app_root = kicad_app_root(self.cli)
        self.commit = installed_commit(self.cli)
        self.schema = load_schema(self.commit)
        self.candidate = Candidate.of(document, candidate_root)
        self.document = self.candidate.work_dir / document.name
        self.socket_path = str(Path(tempfile.gettempdir()) / "kicad-ai-tools" / f"api-{uuid.uuid4().hex[:12]}.sock")
        self._tree = None
        self._document_spec = None
        self.process: subprocess.Popen | None = None
        self.client: Client | None = None

    @property
    def kind(self) -> str:
        return "schematic" if self.document.suffix == ".kicad_sch" else "pcb"

    def start(self) -> HeadlessSession:
        Path(self.socket_path).parent.mkdir(parents=True, exist_ok=True)
        argv = [self.cli, "api-server", "--socket", self.socket_path, str(self.document)]
        if sys.platform == "win32":
            from kicad_mcp.utils.winproc import Tree

            self._tree = Tree()
            self.process = self._tree.spawn(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True)
        try:
            self._wait_ready()
        except BaseException:
            self.close()
            raise
        return self

    def _wait_ready(self) -> None:
        url = f"ipc://{self.socket_path}"
        deadline = time.monotonic() + READY_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise SessionError(f"kicad-cli api-server exited with {self.process.returncode} before ready")
            probe = Client(url, self.schema, "kicad-ai-tools.probe", timeout_ms=250)
            try:
                probe.send(self.schema.new("kiapi.common.commands.Ping"))
                self.client = Client(url, self.schema)
                return
            except (ConnectionError, ApiCallError) as exc:
                last = str(exc)
            finally:
                probe.close()
            time.sleep(0.2)
        raise SessionError(f"kicad-cli api-server not ready after {READY_TIMEOUT_S:.0f} s: {last}")

    def document_spec(self):
        """The DocumentSpecifier of the document this server has open (None if it has none)."""
        if self._document_spec is None:
            request = self.schema.new("kiapi.common.commands.GetOpenDocuments")
            request.type = self.schema.pool.FindEnumTypeByName("kiapi.common.types.DocumentType")                 .values_by_name["DOCTYPE_SCHEMATIC" if self.kind == "schematic" else "DOCTYPE_PCB"].number
            try:
                documents = self.client.send(request).documents
            except ApiCallError:
                return None  # no document open (closed by a command): nothing to fill in
            self._document_spec = documents[0] if documents else None
        return self._document_spec

    def healthy(self) -> bool:
        """The server runs and still has its document open (a command may close it)."""
        if self.process is None or self.process.poll() is not None:
            return False
        self._document_spec = None
        try:
            return self.document_spec() is not None
        except (ConnectionError, ApiCallError):
            return False

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    def close(self, discard: bool = False) -> None:
        """End the server and its whole process tree; optionally delete the candidate copy."""
        if self.client is not None:
            self.client.close()
            self.client = None
        if self._tree is not None:
            self._tree.close()
            self._tree = None
        elif self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.strays = stop_strays(self.candidate.work_dir, image_root=self.app_root,  # anything that escaped the job
                                  since=self.started_at)
        if discard:
            self.candidate.discard()

    def __enter__(self) -> HeadlessSession:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


def installed_commit(cli: str) -> str:
    """The full commit hash the installed kicad-cli was built from (as it reports it)."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    out = subprocess.run([cli, "version", "--format", "commit"], capture_output=True, text=True,
                         timeout=60, creationflags=flags).stdout.strip()
    if len(out) != 40:
        raise SessionError(f"{cli} did not report a commit hash (got {out!r}); KiCad 11 or a nightly is needed")
    return out


def kicad_app_root(cli: str) -> Path:
    """The KiCad installation a ``kicad-cli`` belongs to (``<root>/bin/kicad-cli``)."""
    return Path(shutil.which(cli) or cli).resolve().parent.parent


def stop_strays(*owned_paths: Path, image_root: str | Path | None = None, since: float | None = None) -> list[int]:
    """Stop KiCad processes that escaped a session's job object (relaunched through a system hand-off).

    Never by name or pattern alone. A process is stopped only when all three hold, each read through the one
    handle that then terminates it (so a reused PID can never be hit):
    - its executable lies under ``image_root``, the KiCad installation the session started;
    - it was created no earlier than ``since`` (epoch seconds, taken before the session's folders existed);
    - its command line names one of this session's own uniquely named folders (candidate copy or GUI profile).
    Only the process itself is stopped, never a tree, and only if this user may terminate it. Without an
    ``image_root`` and ``since`` nothing is stopped. Returns the PIDs stopped.
    """
    if sys.platform != "win32" or not owned_paths or image_root is None or since is None:
        return []
    root = Path(os.path.abspath(image_root))
    if root.parent == root:  # a drive root would match every program on it
        return []
    from kicad_mcp.l4 import hidden_desktop as hd

    needles = [os.path.normcase(str(p)) for p in owned_paths if p]
    prefix = os.path.normcase(str(root)).rstrip("\\") + "\\"
    since_filetime = int((since - 1.0) * 10_000_000) + 116_444_736_000_000_000  # 1 s clock tolerance
    access = hd.PROCESS_TERMINATE | hd.PROCESS_QUERY_LIMITED_INFORMATION | hd.SYNCHRONIZE
    k32 = _win_process_api()
    stopped = []
    for pid, _parent in hd.process_table():
        if pid in (0, 4, os.getpid()):
            continue
        handle = k32.OpenProcess(access, False, pid)
        if not handle:
            continue  # gone, or not ours to stop
        try:
            created = hd.process_creation_time(handle)
            if created is None or created < since_filetime:
                continue
            image = _process_image(k32, handle)
            if not image or not os.path.normcase(image).startswith(prefix):
                continue
            command = os.path.normcase(_process_command_line(handle) or "")
            if not any(needle in command for needle in needles):
                continue
            if k32.TerminateProcess(handle, 1):
                k32.WaitForSingleObject(handle, 15000)
                stopped.append(pid)
        finally:
            k32.CloseHandle(handle)
    return stopped


def _win_process_api():
    """A private kernel32 binding (its own argtypes) for the stray check."""
    import ctypes
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.OpenProcess.restype = wt.HANDLE
    k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    return k32


def _process_image(k32, handle) -> str | None:
    import ctypes
    from ctypes import wintypes as wt

    size = wt.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(size.value)
    if not k32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
        return None
    return buffer.value


def _process_command_line(handle) -> str | None:
    """The process's command line (ProcessCommandLineInformation; needs only limited query access)."""
    import ctypes
    from ctypes import wintypes as wt

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]

    nt = ctypes.WinDLL("ntdll")
    nt.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                             ctypes.POINTER(wt.ULONG)]
    nt.NtQueryInformationProcess.restype = ctypes.c_long
    needed = wt.ULONG(0)
    nt.NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(needed))
    if not needed.value or needed.value > 1 << 20:
        return None
    buffer = ctypes.create_string_buffer(needed.value)
    if nt.NtQueryInformationProcess(handle, 60, buffer, needed, ctypes.byref(needed)) != 0:
        return None
    text = UnicodeString.from_buffer(buffer)
    if not text.Buffer or not text.Length:
        return ""
    return ctypes.wstring_at(text.Buffer, text.Length // 2)
