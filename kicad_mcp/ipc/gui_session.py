"""A KiCad GUI editor on a private, never-shown desktop, spoken to over the same IPC API.

Some API commands exist only in the GUI (selection, focus, layer visibility, appearance, interactive
move, RunAction, opening a library item): KiCad refuses them in headless ``api-server`` mode. For those,
this session runs the real editor (eeschema or pcbnew) on a private desktop via the L4
``HiddenKiCad`` launcher, which owns the process tree in a job object and kills it if any window ever
reaches the user's desktop. Its profile is task-owned (private KICAD_CONFIG_HOME, TEMP, ...), with the
API server enabled there only, so the user's own KiCad settings are never touched and its socket
(``<profile>/temp/kicad/api.sock``) cannot collide with any other KiCad.

It serves a candidate copy, exactly like ``HeadlessSession``; ``promote()`` is the only write-back.
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, temporary_owner

from pathlib import Path
import tempfile
import time
import uuid

from kicad_mcp.ipc.client import ApiCallError, Client
from kicad_mcp.ipc.schema import load as load_schema
from kicad_mcp.ipc.session import (
    DOCUMENT_SUFFIXES,
    Candidate,
    SessionError,
    find_kicad_cli,
    installed_commit,
    kicad_app_root,
    stop_strays,
    stray_image_root,
)

READY_TIMEOUT_S = 180.0
EMPTY_TABLES = {"sym-lib-table": "(sym_lib_table\n  (version 7)\n)\n",
                "fp-lib-table": "(fp_lib_table\n  (version 7)\n)\n"}


class GuiSession:
    """The GUI editor for one document of a candidate copy, on a private desktop."""

    def __init__(self, document: str | Path, cli: str | None = None, candidate_root: Path | None = None):
        self.started_at = time.time()  # before any folder of this session exists (stray-check evidence)
        document = Path(document)
        if document.suffix not in DOCUMENT_SUFFIXES:
            raise SessionError(f"a session serves a .kicad_sch or .kicad_pcb, not {document.name}")
        self.cli = cli or find_kicad_cli()
        self.app_root = kicad_app_root(self.cli)  # resolved, so the stray check sees the real install
        self.commit = installed_commit(self.cli)
        self.schema = load_schema(self.commit)
        self.candidate = Candidate.of(document, candidate_root)
        self.document = self.candidate.work_dir / document.name
        self.profile = Path(candidate_root or tempfile.gettempdir()) / "kicad-ai-tools" / "gui" / uuid.uuid4().hex[:10]
        self.kicad = None
        self.client: Client | None = None
        self._document_spec = None
        self.uia = None
        self.helper = None

    @property
    def kind(self) -> str:
        return "schematic" if self.document.suffix == ".kicad_sch" else "pcb"

    @property
    def socket_url(self) -> str:
        return f"ipc://{self.profile / 'temp' / 'kicad' / 'api.sock'}"

    def _settings_version(self) -> str:
        from kicad_mcp.ipc.session import subprocess, sys  # same windowless runner as the CLI calls

        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        plain = subprocess.run([self.cli, "version"], capture_output=True, text=True, timeout=60,
                               creationflags=flags).stdout.strip()
        return ".".join(plain.split(".")[:2])  # 10.99.0 -> 10.99, the profile directory KiCad reads

    def start(self) -> GuiSession:
        from kicad_mcp.l4.hidden_desktop import HiddenKiCad

        version = self._settings_version()
        config = self.profile / "config" / version
        config.mkdir(parents=True, exist_ok=True)
        for name, body in EMPTY_TABLES.items():  # a lite install ships no template tables
            if not (config / name).exists():
                (config / name).write_text(body, encoding="utf-8")
        exe = "eeschema.exe" if self.kind == "schematic" else "pcbnew.exe"
        self.kicad = HiddenKiCad(self.document, self.profile, exe=exe, app_root=self.app_root,
                                 settings_version=version, tripwire_s=0)
        self.kicad.start()
        try:
            self._wait_ready()
        except BaseException:
            self.close()
            raise
        return self

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + READY_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            if not self.kicad.alive():
                raise SessionError(f"{self.kicad.exe} exited before its API server was ready")
            self.kicad._tripwire(0.25)  # keep watching the user's desktop while KiCad starts
            probe = Client(self.socket_url, self.schema, "kicad-ai-tools.probe", timeout_ms=500)
            try:
                probe.send(self.schema.new("kiapi.common.commands.Ping"))
                self.client = Client(self.socket_url, self.schema)
                if self.document_spec() is not None:  # the editor has finished loading the document
                    return
            except (ConnectionError, ApiCallError) as exc:
                last = str(exc)
            finally:
                probe.close()
        raise SessionError(f"GUI API server not ready after {READY_TIMEOUT_S:.0f} s: {last}")

    def document_spec(self):
        if self._document_spec is None and self.client is not None:
            request = self.schema.new("kiapi.common.commands.GetOpenDocuments")
            request.type = self.schema.pool.FindEnumTypeByName("kiapi.common.types.DocumentType") \
                .values_by_name["DOCTYPE_SCHEMATIC" if self.kind == "schematic" else "DOCTYPE_PCB"].number
            try:
                documents = self.client.send(request).documents
            except ApiCallError:
                return None
            self._document_spec = documents[0] if documents else None
        return self._document_spec

    def healthy(self) -> bool:
        if self.kicad is None or not self.kicad.alive():
            return False
        self._document_spec = None
        try:
            return self.document_spec() is not None
        except (ConnectionError, ApiCallError):
            return False

    @property
    def pid(self) -> int | None:
        return self.kicad.pid if self.kicad else None

    @property
    def process(self):
        return self.kicad.proc if self.kicad else None

    def attach_uia(self, channel: Path | None = None):
        """The L4 UI Automation helper on this session's private desktop (reads trees, dialogs, status bars;
        invokes controls). Started once; stopped with the session."""
        if self.uia is not None:
            return self.uia
        import json as _json

        from kicad_mcp.l4 import hidden_desktop as hd
        from kicad_mcp.l4.broker import UiaClient

        channel = channel or self.profile / "uia"
        channel.mkdir(parents=True, exist_ok=True)
        helper = Path(hd.__file__).with_name("uia_helper.py")
        sites = " ".join(f'--site "{s}"' for s in hd.site_packages_paths())
        arguments = (f'-I "{helper}" --channel "{channel}" --pid {self.kicad.pid} '
                     f'--desktop {self.kicad.desktop_name} {sites}')
        self.helper = hd.spawn_windowless_on_desktop(arguments, self.kicad.desktop_name, str(channel), "uia-helper")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = channel / "ready.json"
            if ready.exists():
                info = _json.loads(ready.read_text(encoding="utf-8"))
                if info.get("target_pid") != self.kicad.pid or info.get("thread_desktop") != self.kicad.desktop_name:
                    raise SessionError("UIA helper bound to the wrong desktop or process")
                self.uia = UiaClient(channel, self.helper)
                return self.uia
            if (channel / "fatal.json").exists() or not self.helper.alive():
                raise SessionError("UIA helper could not start")
            time.sleep(0.2)
        raise SessionError("UIA helper not ready in 60 s")

    def _stop_uia(self) -> None:
        if self.helper is not None:
            self.helper.kill()
            self.helper.close()
            self.helper = None
        self.uia = None

    def close(self, discard: bool = False) -> None:
        self._stop_uia()
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.kicad is not None:
            self.kicad.close()
            self.kicad = None
        self.strays = stop_strays(self.candidate.work_dir, self.profile,  # anything that escaped the job
                                  image_root=stray_image_root(self.app_root), since=self.started_at)
        if discard:
            self.candidate.discard()

    def restart(self) -> GuiSession:
        """A fresh editor on the same candidate copy and profile (e.g. after the profile's hotkeys change)."""
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.kicad is not None:
            self.kicad.close()
            self.kicad = None
        self._document_spec = None
        return self.start()

    def __enter__(self) -> GuiSession:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.close()


class FrameSession(GuiSession):
    """A KiCad program with no API server of its own (Gerber viewer, drawing-sheet editor, project
    manager) on a private desktop, ready when its main window shows. It works on a private copy of the
    input file; editor actions reach it through hotkeys (see ipc.hotkeys)."""

    TITLES = {"gerbview.exe": "Gerber Viewer", "pl_editor.exe": "Drawing Sheet Editor", "kicad.exe": "KiCad"}

    def __init__(self, input_file: str | Path, exe: str, cli: str | None = None, root: Path | None = None):
        import shutil

        self.started_at = time.time()  # before any folder of this session exists (stray-check evidence)
        self.cli = cli or find_kicad_cli()
        self.app_root = kicad_app_root(self.cli)  # resolved, so the stray check sees the real install
        self.commit = installed_commit(self.cli)
        self.schema = load_schema(self.commit)
        self.exe = exe
        base = Path(root or tempfile.gettempdir()) / "kicad-ai-tools" / "frames" / uuid.uuid4().hex[:10]
        self.profile = base / "profile"
        work = base / "work"
        work.mkdir(parents=True)
        source = Path(input_file)
        if source.suffix == ".kicad_pro":  # the project manager opens the whole project folder
            for item in source.parent.iterdir():
                if item.name.startswith("~") or item.name.endswith("-backups"):
                    continue
                if item.is_file():
                    shutil.copy2(item, work / item.name)
                elif item.is_dir():  # libraries, local history, sub-sheets' folders
                    shutil.copytree(item, work / item.name)
        elif source.suffix == ".gbrjob":  # a Gerber job file names its layers' files beside it
            for item in source.parent.iterdir():
                if item.is_file():
                    shutil.copy2(item, work / item.name)
        else:
            shutil.copy2(source, work / source.name)
        self.document = work / source.name
        self.work_dir = work
        self.candidate = None
        self.kicad = None
        self.client = None
        self._document_spec = None
        self.uia = None
        self.helper = None

    @property
    def kind(self) -> str:
        return self.exe.removesuffix(".exe")

    def start(self) -> FrameSession:
        from kicad_mcp.l4.hidden_desktop import HiddenKiCad

        version = self._settings_version()
        config = self.profile / "config" / version
        config.mkdir(parents=True, exist_ok=True)
        for name, body in EMPTY_TABLES.items():
            if not (config / name).exists():
                (config / name).write_text(body, encoding="utf-8")
        self.kicad = HiddenKiCad(self.document, self.profile, exe=self.exe, app_root=self.app_root,
                                 settings_version=version, tripwire_s=0)
        self.kicad.start()
        deadline = time.monotonic() + READY_TIMEOUT_S
        title = self.TITLES.get(self.exe, "")
        while time.monotonic() < deadline:
            if not self.kicad.alive():
                self.close()
                raise SessionError(f"{self.exe} exited before its window showed")
            self.kicad._tripwire(0.25)
            if any(title in w["title"] for w in self.kicad.desktop.windows()
                   if w["pid"] == self.kicad.pid and w["visible"]):
                time.sleep(1.0)  # let it finish loading the file
                if self.exe == "kicad.exe":
                    self._skip_model_preview_prompt()
                return self
        self.close()
        raise SessionError(f"{self.exe} showed no '{title}' window in {READY_TIMEOUT_S:.0f} s")

    def _skip_model_preview_prompt(self, wait_s: float = 8.0) -> None:
        """The project manager asks once per KiCad version whether to set up Explorer's 3D model previews
        (MaybeShowModelPreviewSetupPrompt, common/model_preview_manager.cpp:85, posted from kicad/kicad.cpp:474). Its
        native task dialog holds the manager's main thread in a modal loop that never sends wx idle events, so
        everything KiCad defers to idle (opening a project-tree file) never runs. The session answers it "Skip This
        Version", which records the decline in the session's own profile only."""
        import ctypes

        user32 = ctypes.windll.user32
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            prompts = [w["hwnd"] for w in self.kicad.desktop.windows()
                       if w["pid"] == self.kicad.pid and w["visible"] and w["title"] == "3D Model Previews"]
            if prompts:
                for hwnd in prompts:
                    user32.PostMessageW(hwnd, 0x0400 + 102, 7, 0)  # TDM_CLICK_BUTTON, IDNO: "Skip This Version"
                time.sleep(1.0)
                still = [w for w in self.kicad.desktop.windows() if w["hwnd"] in prompts and w["visible"]]
                for w in still:  # not a task dialog: the classic message box takes IDNO as a command
                    user32.PostMessageW(w["hwnd"], 0x0111, 7, 0)
                self.model_preview_prompt = "skipped"
                return
            time.sleep(0.4)
        self.model_preview_prompt = "none"

    def document_spec(self):
        return None  # no API document

    def healthy(self) -> bool:
        return self.kicad is not None and self.kicad.alive()

    def close(self, discard: bool = False) -> None:
        self._stop_uia()
        if self.kicad is not None:
            self.kicad.close()
            self.kicad = None
        self.strays = stop_strays(self.work_dir, self.profile, image_root=stray_image_root(self.app_root),
                                  since=self.started_at)
        if discard:
            import shutil

            shutil.rmtree(self.work_dir.parent, ignore_errors=True)


@temporary_owner(cleanup='close', allocating=('__init__',))
class ProgramSession(FrameSession):
    """A KiCad program with neither actions nor an API (PCB calculator, image converter) on a private
    desktop, driven through UI Automation by the L4 helper running on that same desktop."""

    TITLES = {**FrameSession.TITLES, "pcb_calculator.exe": "Calculator Tools", "bitmap2component.exe": "Image Converter"}

    def __init__(self, exe: str, input_file: str | Path | None = None, cli: str | None = None):
        placeholder = None
        if input_file is None:  # the calculator opens no file; it still gets a harmless argument
            placeholder = Path(scoped_mkdtemp(prefix="kicad-ai-tools-prog-")) / "none.txt"
            placeholder.write_text("", encoding="utf-8")
        super().__init__(input_file or placeholder, exe, cli)

    def start(self) -> ProgramSession:
        super().start()
        try:
            self._attach_uia()
        except BaseException:
            self.close()
            raise
        return self

    def _attach_uia(self) -> None:
        self.attach_uia(self.work_dir.parent / "uia")

    def close(self, discard: bool = False) -> None:
        super().close(discard)
