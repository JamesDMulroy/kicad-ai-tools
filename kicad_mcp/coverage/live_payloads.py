"""Request payloads for live coverage of API commands that need arguments.

Each payload is built per session from live data (the project specifier, real item IDs fetched from the
open document, the project's own library entries), never from constants that only fit one machine.
A payload returns None when the command does not apply to that session's document type.

LAST lists commands that change the session itself (variants, deletes, save-as, close, open); the sweep
runs them after everything else, in this order, so they cannot mask other commands.
"""
from __future__ import annotations

from collections.abc import Callable
import contextlib
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from kicad_mcp.ipc import api

LAST = ["BeginCommit", "EndCommit", "RevertDocument", "AddVariant", "CopyVariant", "RenameVariant", "SetVariantDescription", "DeleteVariant",
        "DeleteItems", "SaveCopyOfDocument", "OpenDocument", "SaveDocumentAs", "CreateDocument",
        "CloseDocument", "CloseAllDocuments"]


class Context:
    """Live facts about one session, fetched lazily and cached."""

    def __init__(self, session):
        self.session = session
        self.kind = session.kind  # "schematic" | "pcb"
        self.out_dir = session.candidate.work_dir.parent / f"{session.candidate.work_dir.name}-out"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, Any] = {}
        self.responses: dict[str, dict] = {}  # last OK response per command (BeginCommit -> EndCommit)

    @property
    def project(self) -> dict:
        spec = api.json_format.MessageToDict(self.session.document_spec(), preserving_proto_field_name=True,
                                             descriptor_pool=self.session.schema.pool)
        return spec.get("project", {})

    def out(self, name: str) -> str:
        return str(self.out_dir / name)

    def first_id(self, *types: str) -> str | None:
        key = "id:" + ",".join(types)
        if key not in self._cache:
            items = api.call(self.session, "GetItems", {"types": list(types)})["response"].get("items", [])
            self._cache[key] = next((i["id"]["value"] for i in items if i.get("id", {}).get("value")), None)
        return self._cache[key]

    def library(self, kind: str) -> tuple[str, str] | None:
        """(nickname, entry) of the first entry in the project's own symbol or footprint library."""
        work = self.session.candidate.work_dir
        if kind == "symbol":
            for lib in sorted(work.glob("*.kicad_sym")):
                match = re.search(r'^\s*\(symbol "([^"]+)"', lib.read_text(encoding="utf-8"), re.MULTILINE)
                if match:
                    return _nickname(work / "sym-lib-table", lib.name, lib.stem), match.group(1)
        else:
            for lib in sorted(work.glob("*.pretty")):
                for mod in sorted(lib.glob("*.kicad_mod")):
                    return _nickname(work / "fp-lib-table", lib.name, lib.stem), mod.stem
        return None

    def netlist(self) -> str:
        """A KiCad netlist of the candidate's schematic, made by the same kicad-cli."""
        if "netlist" not in self._cache:
            sch = next(self.session.candidate.work_dir.glob("*.kicad_sch"))
            out = self.out("coverage.net")
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            subprocess.run([self.session.cli, "sch", "export", "netlist", "-o", out, str(sch)],
                           capture_output=True, timeout=120, creationflags=flags, check=True)
            self._cache["netlist"] = out
        return self._cache["netlist"]

    def label_id(self) -> str | None:
        if self.kind == "schematic":
            return (self.first_id("KOT_SCH_LABEL") or self.first_id("KOT_SCH_GLOBAL_LABEL")
                    or self.first_id("KOT_SCH_HIER_LABEL"))
        return self.first_id("KOT_PCB_TEXT", "KOT_PCB_SHAPE")

    def any_id(self) -> str | None:
        return self.first_id("KOT_SCH_SYMBOL") if self.kind == "schematic" else self.first_id("KOT_PCB_FOOTPRINT")


class MissingFixture(RuntimeError):
    """The test document lacks what a payload needs: an error, never a vacuous pass."""


def _ids(value: str | None) -> list[dict]:
    if not value:
        raise MissingFixture("the test document has no item of the kind this command needs")
    return [{"value": value}]


def _pcb(build: Callable[[Context], dict]) -> Callable[[Context], dict | None]:
    return lambda ctx: build(ctx) if ctx.kind == "pcb" else None


def _sch(build: Callable[[Context], dict]) -> Callable[[Context], dict | None]:
    return lambda ctx: build(ctx) if ctx.kind == "schematic" else None


def _nickname(table: Path, file_name: str, default: str) -> str:
    """The name the project's library table gives the library file (it need not be the file's stem)."""
    if table.exists():
        for name, uri in re.findall(r'\(lib \(name "([^"]+)"\).*?\(uri "([^"]+)"\)', table.read_text(encoding="utf-8")):
            if uri.replace("\\", "/").rsplit("/", 1)[-1] == file_name:
                return name
    return default


def _lib_id(ctx: Context, kind: str) -> dict:
    found = ctx.library(kind)
    if not found:
        raise MissingFixture(f"the test project has no {kind} library of its own")
    return {"library_nickname": found[0], "entry_name": found[1]}


def _project_document(ctx: Context) -> dict:
    return {"type": "DOCTYPE_PROJECT", "project": ctx.project}


def _doctype(ctx: Context) -> str:
    return "DOCTYPE_SCHEMATIC" if ctx.kind == "schematic" else "DOCTYPE_PCB"


def _reopen(ctx: Context) -> dict:
    """OpenDocument needs no document of its type open: close the session's document, then reopen it."""
    api.call(ctx.session, "CloseDocument", {})
    ctx.session._document_spec = None
    return {"type": _doctype(ctx), "path": str(ctx.session.document)}


def _job(ext: str) -> Callable[[Context], dict]:
    return lambda ctx: {"job_settings": {"output_path": ctx.out(f"job.{ext}")}}


PAYLOADS: dict[str, Callable[[Context], dict | None]] = {
    "AddVariant": lambda ctx: {"name": "ci-a", "description": "coverage test"},
    "CopyVariant": lambda ctx: {"old_name": "ci-a", "new_name": "ci-b", "new_description": "copy"},
    "RenameVariant": lambda ctx: {"old_name": "ci-b", "new_name": "ci-c"},
    "SetVariantDescription": lambda ctx: {"name": "ci-c", "description": "renamed"},
    "DeleteVariant": lambda ctx: {"name": "ci-c"},
    "DeleteItems": lambda ctx: {"item_ids": _ids(ctx.label_id())},
    "GetItemsById": lambda ctx: {"items": _ids(ctx.any_id())},
    "HitTest": lambda ctx: {"id": _ids(ctx.any_id())[0], "position": {"x_nm": 0, "y_nm": 0},
                            "tolerance": 1000000},
    "GetConnectedItems": _pcb(lambda ctx: {"items": _ids(ctx.first_id("KOT_PCB_PAD")),
                                           "types": ["KOT_PCB_TRACE", "KOT_PCB_VIA", "KOT_PCB_PAD"]}),
    "GetBoardBoundingBox": _pcb(lambda ctx: {"mode": "BBBM_ALL_ITEMS"}),
    "GetBoardOrigin": _pcb(lambda ctx: {"type": "BOT_GRID"}),
    "SetBoardOrigin": _pcb(lambda ctx: {"type": "BOT_GRID", "origin": {"x_nm": 0, "y_nm": 0}}),
    "SetBoardEnabledLayers": _pcb(lambda ctx: {
        k: v for k, v in api.call(ctx.session, "GetBoardEnabledLayers")["response"].items() if k != "board"}),
    "ImportNetlist": _pcb(lambda ctx: {"netlist_path": ctx.netlist(), "dry_run": True,
                                       "match_mode": "NMM_REFERENCE"}),
    "PlaceFootprintFromLibrary": _pcb(lambda ctx: {"lib_id": _lib_id(ctx, "footprint"),
                                                   "position": {"x_nm": 0, "y_nm": 0}, "layer": "BL_F_Cu"}),
    "PlaceSymbolFromLibrary": _sch(lambda ctx: {"lib_id": _lib_id(ctx, "symbol"),
                                                "position": {"x_nm": 25400000, "y_nm": 25400000},
                                                "orientation": "SSO_0", "unit": {"unit": 1}}),
    "GetLibraryStatuses": lambda ctx: {"types": ["LT_SYMBOL", "LT_FOOTPRINT"], "scope": "LTS_BOTH"},
    "ReloadLibrary": lambda ctx: {"type": "LT_SYMBOL" if ctx.kind == "schematic" else "LT_FOOTPRINT",
                                  "scope": "LTS_PROJECT",
                                  "nickname": [_lib_id(ctx, "symbol" if ctx.kind == "schematic" else "footprint")["library_nickname"]]},
    "GetLibraryItems": lambda ctx: {"type": "LT_SYMBOL" if ctx.kind == "schematic" else "LT_FOOTPRINT",
                                    "nickname": [_lib_id(ctx, "symbol" if ctx.kind == "schematic" else "footprint")["library_nickname"]]},
    "GetItemsFromLibrary": lambda ctx: {"type": "LT_SYMBOL" if ctx.kind == "schematic" else "LT_FOOTPRINT",
                                        "item_ids": [_lib_id(ctx, "symbol" if ctx.kind == "schematic"
                                                             else "footprint")]},
    "OpenLibraryItem": lambda ctx: {"identifier": _lib_id(ctx, "symbol" if ctx.kind == "schematic"
                                                          else "footprint")},
    "GetNetClasses": lambda ctx: {"project": ctx.project},
    "GetNetClassAssignments": lambda ctx: {"project": ctx.project},
    "SetNetClasses": lambda ctx: {"project": ctx.project, "merge_mode": "MMM_MERGE",
                                  "net_classes": [{"name": "CI_TEST", "priority": 5, "type": "NCT_EXPLICIT"}]},
    "SetNetClassAssignments": lambda ctx: {"project": ctx.project, "merge_mode": "MMM_MERGE",
                                           "pattern_assignments": [{"pattern": "CI_*", "netclass": "Default"}]},
    "GetPluginSettingsPath": lambda ctx: {"identifier": "local.kicad_ai_tools.coverage"},
    "GetOpenDocuments": lambda ctx: {"type": "DOCTYPE_SCHEMATIC" if ctx.kind == "schematic" else "DOCTYPE_PCB"},
    "GetTextVariables": lambda ctx: {"document": _project_document(ctx)},
    "SetTextVariables": lambda ctx: {"document": _project_document(ctx), "merge_mode": "MMM_MERGE",
                                     "variables": {"variables": {"CI_VAR": "1"}}},  # a proto map
    "EndCommit": lambda ctx: {"id": ctx.responses.get("BeginCommit", {}).get("id", {}), "action": "CMA_DROP"},
    "SetPageSettings": lambda ctx: {"page_settings": {"page_size": "PS_A3", "orientation": "PO_LANDSCAPE"}},
    "SetTitleBlockInfo": lambda ctx: {"title_block": {"title": "coverage test", "revision": "T"}},
    "SaveCopyOfDocument": lambda ctx: {"path": ctx.out("copy" + Path(ctx.session.document).suffix),
                                       "options": {"overwrite": True}},
    "SaveDocumentAs": lambda ctx: {"path": ctx.out("saved-as" + Path(ctx.session.document).suffix)},
    "CreateDocument": lambda ctx: {"type": _doctype(ctx), "path": ctx.out("created" + Path(ctx.session.document).suffix)},
    "OpenDocument": lambda ctx: _reopen(ctx),
}

# GUI-only commands (KiCad refuses them headless); the sweep sends them to a GuiSession.
GUI_ORDER = ["AddToSelection", "GetSelection", "SaveSelectionToString", "SyncSelection", "RemoveFromSelection",
             "ClearSelection"]
# Commands after which the session is always restarted (an interactive tool keeps the editor busy).
RESTART_AFTER = {"InteractiveMoveItems"}
GUI_LAST = ["InteractiveMoveItems"]


def _open_library_item(ctx: Context) -> dict:
    """OpenLibraryItem is served by the library editor's own window: open that editor first (board side;
    the schematic editor has no RunAction to open its symbol editor with)."""
    if ctx.kind == "pcb":
        with contextlib.suppress(api.ApiCallError):
            api.call(ctx.session, "RunAction", {"action": "common.Control.showFootprintEditor"})
        import time

        time.sleep(3)  # the footprint editor frame registers its API handler as it opens
    return {"type": "DOCTYPE_SYMBOL" if ctx.kind == "schematic" else "DOCTYPE_FOOTPRINT",
            "identifier": _lib_id(ctx, "symbol" if ctx.kind == "schematic" else "footprint")}


# Calls made right after a command to leave the editor idle again (an interactive tool waits for input).
AFTER: dict[str, list[tuple[str, dict]]] = {
    "InteractiveMoveItems": [("RunAction", {"action": "common.Interactive.cancel"})],
}


def _first_net(ctx: Context) -> list[str]:
    if ctx.kind == "pcb":
        nets = api.call(ctx.session, "GetNets")["response"].get("nets", [])
    else:
        nets = api.call(ctx.session, "GetSchematicNetlist")["response"].get("nets", [])
    return [next((n["name"] for n in nets if n.get("name")), "GND")]


PAYLOADS.update({
    "AddToSelection": lambda ctx: {"items": _ids(ctx.any_id())},
    "RemoveFromSelection": lambda ctx: {"items": _ids(ctx.any_id())},
    "GetSelection": lambda ctx: {},
    "FocusOnItems": lambda ctx: {"items": _ids(ctx.any_id())},
    "HighlightNets": lambda ctx: {"net_name": _first_net(ctx)},
    "InteractiveMoveItems": _pcb(lambda ctx: {"items": _ids(ctx.first_id("KOT_PCB_FOOTPRINT"))}),
    "OpenLibraryItem": lambda ctx: _open_library_item(ctx),
    "SyncSelection": lambda ctx: {"context": "SSC_EXPLICIT", "mode": "SSM_ITEMS_ONLY"},
    "SetActiveLayer": _pcb(lambda ctx: {"layer": "BL_F_Cu"}),
    "SetVisibleLayers": _pcb(lambda ctx: {
        "layers": api.call(ctx.session, "GetVisibleLayers")["response"].get("layers", [])}),
    "SetBoardEditorAppearanceSettings": _pcb(lambda ctx: {
        "settings": api.call(ctx.session, "GetBoardEditorAppearanceSettings")["response"].get("settings", {})}),
    "GetActiveLayer": _pcb(lambda ctx: {}),
    "GetVisibleLayers": _pcb(lambda ctx: {}),
    "GetBoardEditorAppearanceSettings": _pcb(lambda ctx: {}),
    "RunAction": lambda ctx: {"action": "common.Control.zoomFitScreen"},
})

# Every API export job: write into the session's output folder (the job's own defaults otherwise).
for _name, _ext in {
    "RunBoardJobExport3D": "step", "RunBoardJobExportDrill": "drl", "RunBoardJobExportDxf": "dxf",
    "RunBoardJobExportGencad": "cad", "RunBoardJobExportGerbers": "gbr", "RunBoardJobExportIdf": "emn",
    "RunBoardJobExportIpc2581": "xml", "RunBoardJobExportIpcD356": "d356", "RunBoardJobExportODB": "zip",
    "RunBoardJobExportPdf": "pdf", "RunBoardJobExportPng": "png", "RunBoardJobExportPosition": "pos",
    "RunBoardJobExportPs": "ps", "RunBoardJobExportRender": "png", "RunBoardJobExportStats": "txt",
    "RunBoardJobExportSvg": "svg",
}.items():
    PAYLOADS[_name] = _pcb(_job(_ext))
for _name, _ext in {
    "RunSchematicJobExportBOM": "csv", "RunSchematicJobExportDxf": "dxf", "RunSchematicJobExportNetlist": "net",
    "RunSchematicJobExportPdf": "pdf", "RunSchematicJobExportPng": "png", "RunSchematicJobExportPs": "ps",
    "RunSchematicJobExportSvg": "svg",
}.items():
    PAYLOADS[_name] = _sch(_job(_ext))
PAYLOADS["RunSchematicJobExportBOM"] = _sch(lambda ctx: {"job_settings": {"output_path": ctx.out("job.csv")},
                                                         "fields": {"preset_name": "Grouped By Value"}})
PAYLOADS["RunSchematicJobExportNetlist"] = _sch(lambda ctx: {"job_settings": {"output_path": ctx.out("job.net")},
                                                             "format": "SNF_KICAD_SEXPR"})
