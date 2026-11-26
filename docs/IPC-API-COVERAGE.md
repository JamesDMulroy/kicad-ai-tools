# KiCad IPC API layer and the coverage gate

This package reaches every function a KiCad build exposes through KiCad's own, official interfaces —
no fork, no patched KiCad, nothing that has to be maintained against KiCad's source. It targets KiCad 11
(the nightly today), whose IPC API covers the schematic as well as the board, and whose `kicad-cli
api-server` serves that API with no window at all.

## Routes

| Route | Reaches | How |
|---|---|---|
| Headless API | most API commands | `kicad-cli api-server` on a candidate copy, no window |
| GUI API | GUI-only commands (selection, focus, layers, appearance, interactive move, RunAction, OpenLibraryItem) | the real editor on a private, never-shown desktop (L4 `HiddenKiCad`), task-owned profile |
| RunAction | board-editor and common actions | API `RunAction` in a GUI board session; selection retries with a footprint, track and zone |
| Menus | every frame's menu items | the frame's native menu bar read (labels, ids, check marks) and clicked with `WM_COMMAND` |
| Hotkeys | actions of frames without RunAction (schematic, symbol and footprint editors, simulator, Gerber viewer, 3D viewer, drawing-sheet editor, project manager, footprint assigner) | the session profile's `user.hotkeys` gives the action a key; the key is posted to the canvas or focused control |
| Typed equivalents | interactive tools (draw, place, route, constrain, tune, pad and pin edits) | the API command that makes the same result, read back by type, geometry or field; library tools inside a symbol or footprint opened headless by lib id |
| Tool driver | interactive tools with no API equivalent | activate, post calibrated clicks on the canvas, Escape |
| UI Automation | programs with neither actions nor API (PCB calculator, image converter) | the L4 UIA helper on the same private desktop |
| UIA readback | frames whose state is shown in controls: footprint assigner lists, library trees, Gerber viewer layer list and highlight boxes | the action's key is pressed; the controls' rows, values and selection are read back through UI Automation (`live_cvpcb`, `live_libtree`, `live_gerbview`) |
| Launch hook | actions that hand off to a browser, Explorer, a text editor or another KiCad program | a Frida hook in the session's KiCad process records the `ShellExecute*`/`CreateProcessW` request and points it at a path that does not exist, so nothing starts (`launch_hook`, `live_external`; needs Frida 16, the `coverage` extra) |
| Painted text | state shown only in painted panels (the board editor's message panel: router mode, corner style) | a Frida hook on `ExtTextOutW` in the session's process records the strings a repaint draws (`text_hook`) |
| Zone fixtures | zone order and merge | overlapping zones made with CreateItems, selected, RunAction, priorities read back (`live_zones`) |
| kicad-cli | every CLI command | typed invocation, output checked |

## What counts as proof

A route only counts when a live run on the installed build's exact commit shows the function did its job:

- an API command: KiCad's reply decoded with its own type (and for edits, the item read back);
- a CLI command: exit 0 and the declared output produced;
- an action: an observed effect — a document change (API item hash), an editor-state change (active and
  visible layers, appearance, selection), a menu check mark flipping, a window it opened (closed with
  Cancel), or a change in the editor window. **Not** proof: RunAction's RAS_OK alone (it only says KiCad
  found and dispatched the action), or a window change that is only an info-bar message ("cannot do
  that"), which is recorded with its text instead;
- a typed equivalent: the created or changed item read back with the expected type, geometry or fields;
- a calculator panel: a result field or the panel's rendering changing after a changed input, or, for a
  reference table, the populated table.

The schema for every request and reply comes from the **installed build itself**: the package fetches
`api/proto` at the installed KiCad's commit (`kicad-cli version --format commit`) from KiCad's GitLab,
compiles it with `protoc` into a private descriptor pool, and speaks NNG with KiCad's own envelope. A
command KiCad adds tomorrow is reachable the day it ships, with no client library update.

## Safety

- Every session serves a **candidate copy** of the project (project-local libraries included). Nothing
  reaches the user's files except `kicad_session_promote`, which refuses if any original changed since
  the copy was made and backs up every file it replaces. Lock files and libraries are never written back.
- KiCad runs windowless (headless) or on a private desktop with a tripwire that kills the session if any
  window reaches the user's desktop. Actions that would hand off to another program (browser, Explorer,
  external editor, print) are never run by the sweeps.
- Each session owns its process tree in a job object. On close a session also stops a process that escaped
  the job only when all three hold, checked through the handle that stops it: its executable is under the
  session's KiCad installation, it was created after the session began, and its command line names one of
  the session's own uniquely named folders. Sweeps prove every editor they launched has ended.

## The coverage gate

`python -m kicad_mcp.coverage.inventory` lists every function of the installed build from that build's
own sources (each `registerHandler`, each `TOOL_ACTION`, each `kicad-cli` command) and refuses a source
checkout that is not the installed binary's commit. `python -m kicad_mcp.coverage.gate` gives each item
one state:

- **verified** — a route reaches it and a live run on this exact commit proved it (a reply with its
  type, an action KiCad accepted or whose effect was observed, a CLI output that exists);
- **unverified** — a route exists, but no passing live evidence on this commit;
- **missing** — no route yet;
- **upstream_gap** — KiCad itself declares it but does not serve it, or lists it and refuses it.

The gate passes only when every item is verified, so a new KiCad release fails until each function it
added is covered. Evidence from a different commit is refused.

Sweeps (each writes evidence for one commit; the gate merges them):

```
python -m kicad_mcp.coverage.live_api      --inventory inv.json --project <dir> --out api.json
python -m kicad_mcp.coverage.live_actions  --inventory inv.json --project <dir> --out actions.json [--start N --count M]
python -m kicad_mcp.coverage.live_hotkeys  --inventory inv.json --project <dir> --editor eeschema --out hk.json
python -m kicad_mcp.coverage.live_typed    --inventory inv.json --project <dir> --out typed.json
python -m kicad_mcp.coverage.live_cli      --inventory inv.json --project <dir> --cli <kicad-cli> --out cli.json
python -m kicad_mcp.coverage.gate --inventory inv.json --evidence api.json actions.json ... --report report.md
```

## Upstream gaps found so far (KiCad 10.99.0-5265-g0616634034)

Each is a candidate for a small, well-scoped contribution to KiCad (see KiCad's contribution policy:
an issue assigned by KiCad developers first; human-written commit messages and descriptions).

0. **`RunAction` replaces every action's own parameter with `true`** (a bug, with a one-line fix).
   `API_HANDLER_BOARD::handleRunAction` calls `toolManager()->RunAction( aCtx.Request.action(), true )`;
   the `true` meant "run now" binds to the template `RunAction( const std::string&, T aParam )`, so the
   event's parameter becomes `bool(true)` and an action that carries its own value (a layer, a grid
   index, a zoom preset...) runs with the wrong one. Proven live on 10.99.0-5265: `layerInner1`,
   `layerInner2`, `layerInner5` and `layerTop`, each run from B.Cu, all switch to F.Cu (layer 0). Fix:
   `toolManager()->RunAction( aCtx.Request.action() )` (the overload that runs now with no override).
   Until then the coverage sweeps verify parameterized actions through hotkeys or menus, which keep the
   action's own value.
1. **The schematic editor registers no `RunAction`** (only the board editor does). Mirroring
   `API_HANDLER_BOARD::handleRunAction` into `API_HANDLER_SCH` would make every schematic action
   reachable with an acknowledgement, instead of through hotkeys with observed effects.
2. **Eight declared but unimplemented commands:** library-table add/update/delete/get, library search,
   `FocusOnItem`, `RefreshEditor`, and `UpdateBoardStackup` (the board stackup cannot be written).
3. **No `OpenDocument` handler in GUI mode**, so no API command opens a library editor in the GUI and
   `OpenLibraryItem` is unreachable unless that editor is already open.
4. **`RunAction` takes no parameter**, so actions whose built-in parameter is a placeholder for caller
   data (27 of them) do nothing useful when run by name.
5. **`kicad-cli pcb/sch export hpgl`** are still listed but refuse to run ("no longer supported").
7. **Symbol editor crashes on "next/previous symbol".** `SYMBOL_EDITOR_CONTROL::NextSymbol`/`PreviousSymbol`
   (`symbol_editor_control.cpp`) `static_cast` the frame to `SYMBOL_VIEWER_FRAME*`; pressing the key in the
   symbol editor (where the action is also registered) dereferences the wrong frame type and KiCad exits.
   Fix: `dynamic_cast` and return when it is not the viewer.
8. **Footprint auto-association never matches values containing spaces.** cvpcb's `auto_associate.cpp`
   replaces spaces with `_` in the equivalence file's value, then compares it with the symbol's unmodified
   value.
9. **Inventory friendly names can be another platform's.** Some actions' friendly names are chosen by
   `#ifdef` ("Reveal Project in Finder" on macOS, "Browse Project Files" on Windows), so the source text
   is not always the Windows menu label; the external sweep carries the Windows label per case.
10. **Dead actions:** `eeschema.EditorControl.generateBOMExternal`, `common.Embed.extractFile` and
    `eeschema.InteractiveSelection.SyncSelection` are declared (name, text, tooltip) but nothing in the source
    binds a handler to them or offers them in a menu or toolbar, so they cannot do anything. The board
    editor's own `rescueAutosave` handler is declared but never defined, and `SelectOnSheet` crashes when run
    by name (found by the board-editor sweep). Candidates for removal or wiring upstream.
6. **No API for design blocks, graphics import or the drawing-sheet editor** (and none in headless mode
   for LoadAllLibraries' GUI counterpart): those tools are reachable only through the UI.

### MCP fallbacks for API commands KiCad declares but does not implement

Item 2 above stays an upstream gap (the commands have no handler), but each function now has an MCP tool
that does what the proto describes by other means (`kicad_mcp/tools/fallback_tools.py`, table parser in
`kicad_mcp/ipc/libtables.py`). They work on a session's candidate copy, never on the user's real files or
global configuration (the "global" table is the session profile's private one). The gate routes them as
`fallback:<tool>` and counts each verified only with the live evidence of `coverage/live_fallbacks.py`
(`.tmp/coverage/evidence-fallbacks.json`), which pairs a KiCad-side readback with a control.

| Command | Tool | Mechanism |
|---|---|---|
| `GetLibraryTable` | `kicad_library_table_get` | reads `sym-lib-table`/`fp-lib-table`/`design-block-lib-table` (project, global, or both; `substituted` expands `${VARS}`) |
| `AddLibraryTableEntry` | `kicad_library_table_add` | writes the row in KiCad's own table format (`LCS_ALREADY_EXISTS`, `LCS_READ_ONLY`, `LCS_ERROR` as the proto lists) |
| `UpdateLibraryTableEntry` | `kicad_library_table_update` | replaces the row with that nickname, keeping fields the entry omits (`LCS_NOT_FOUND`) |
| `DeleteLibraryTableEntry` | `kicad_library_table_delete` | removes the row; the library files stay |
| `SearchLibraries` | `kicad_search_libraries` | enumerates the enabled libraries' items from their files; chooser-style match (terms ANDed, substring or `*`/`?` glob); optional description/keyword match |
| `FocusOnItem` | `kicad_focus_on_item` | resolves footprint/pad/symbol/sheet to item ids, runs the handled `FocusOnItems` (zoom to fit) and makes the item the selection (`highlight`) |
| `RefreshEditor` | `kicad_refresh_editor` | the editor's Redraw action (`common.Control.zoomRedraw`); a frame the session does not have open is reported, not refreshed |
| `UpdateBoardStackup` | `kicad_update_board_stackup` | saves the document, rewrites the board file's stackup block, reloads it with `RevertDocument`, returns `GetBoardStackup`'s normalized readback |

Differences from a native handler, stated plainly: KiCad has no command to re-read library tables, so
`kicad_library_table_reload` saves the document and restarts the session's editor on the same candidate
before `GetLibraryStatuses`/`GetLibraryItems` show a table change; `UpdateBoardStackup` keeps the copper
layers the board already enables (use `SetBoardEnabledLayers` first) and, because the document is reloaded,
clears the undo history; `FocusOnItem` selects the item where KiCad's own focus brightens it, because
brightening is not reachable from the API.

### MCP tools for the functions of dead actions

Seven `upstream_gap` actions do nothing inside KiCad (never bound, never posted, never defined, or bound
but unusable). The action stays `upstream_gap`; the function it names exists elsewhere in KiCad, and the MCP
does it (`kicad_mcp/tools/function_fallbacks.py`). The gate prints "function covered" next to the gap when
an evidence file lists it under `function_covered`; that note never moves the item to `verified`. Evidence:
`coverage/live_function_fallbacks.py` -> `.tmp/coverage/evidence-function-fallbacks.json` (`passed` is empty
on purpose).

| Dead action | Tool | What it does instead |
|---|---|---|
| `3DViewer.Control.showBoundingBoxes` | `kicad_3d_model_bounding_boxes` | clicks the Appearance panel's "Model Bounding Boxes" toggle; the state is read from the viewer's Preferences page and from `3d_viewer.json` |
| `common.Embed.extractFile` | `kicad_embedded_files`, `kicad_extract_embedded_file` | board: `GetEmbeddedFiles`; schematic: the saved file; decoded as KiCad does (base64, zstd) |
| `common.Embed.removeFile` | `kicad_remove_embedded_file` | board: `GetEmbeddedFiles` + `SetEmbeddedFiles`; schematic: Schematic Setup > Embedded Files > Remove through UI Automation then OK (the result is checked on a scratch copy; the file edit + reload is only a fallback) |
| `eeschema.EditorControl.generateBOMExternal` | `kicad_run_bom_generator` | the BOM generator dialog's flow: `kicad-cli sch export python-bom` writes the intermediate netlist (%I; the same exporter and options as the dialog's `GNL_OPT_BOM\|GNL_ALL`), the command line (%O, %B, %P) runs contained |
| `eeschema.InteractiveSelection.SyncSelection` | `kicad_sync_selection` | the API's `SyncSelection` with symbol/pad items (the schematic's cross-probe handler) plus the selection read back |
| `pcbnew.EditorControl.rescueAutosave` | `kicad_recover_autosave` | KiCad's Auto-Save Recovery Restore / Keep Both for `_autosave-<name>` (project folder or the profile's autosave folder; Keep Both names the copy `<name>.recovered.<stamp>` as KiCad does), then `RevertDocument`; a `.before-recovery` backup on Restore is opt-in (`backup=True`), KiCad keeps none |
| `pcbnew.InteractiveSelection.SelectOnSheet` | `kicad_board_sheets`, `kicad_select_items_on_sheet` | footprints of a sheet (by the symbol path the API reports) selected with `SyncSelection`; connections are selected by default, as `selectAllItemsOnSheet` does (`include_connections=False` for footprints only) |

Stated plainly: the schematic embedded-file tools work on the saved file (the schematic API has no embedded
file commands); footprint-level embedded files are not touched; `kicad_run_bom_generator` starts a program
the caller names, so it needs `allow_execute`; local-history (git) snapshots, KiCad's default autosave
format, are a separate feature, only the autosave-file format is recovered; KiCad's autosave timer needs
an active frame: with the interval at its minimum (1 s), file-format autosave and a board edit through the API,
no `_autosave-*` appeared in 120 s on a private desktop, so the proof writes the autosave with KiCad's own
serializer (`SaveCopyOfDocument`) and checks the tool against KiCad's real Auto-Save Recovery dialog (Keep Both and
Restore answered in the hidden session; an older autosave raises no prompt). The two HPGL CLI commands are removed from KiCad and stay gaps; their
the BOM generator dialog itself was driven in a hidden session (a generator that copies `%I`) and its intermediate netlist compared with the tool's (no differing lines outside the date/tool header); the HPGL refusal text is in the same evidence file, with `export svg` of the same files as a control and a check that the HPGL call wrote no file.

## Current coverage

The current inventory has **1208 of 1217 functions verified live**. Nine gaps remain inside KiCad: seven dead actions—`3DViewer.Control.showBoundingBoxes`, `common.Embed.extractFile`, `common.Embed.removeFile`, `eeschema.EditorControl.generateBOMExternal`, `eeschema.InteractiveSelection.SyncSelection`, `pcbnew.EditorControl.rescueAutosave`, and `pcbnew.InteractiveSelection.SelectOnSheet`—and two removed exports, `kicad-cli pcb export hpgl` and `kicad-cli sch export hpgl`. The corresponding functions of the seven dead actions have MCP tools; the API-declared fallback commands described above are verified through MCP tools.

To reproduce the inventory, run `python -m kicad_mcp.coverage.inventory`; then run the live sweeps listed above and merge their evidence with `python -m kicad_mcp.coverage.gate --inventory inv.json --evidence api.json actions.json ... --report report.md`. The gate requires every inventoried function to be verified.

### Known upstream gaps

**RunAction parameter handling.** The board handler passes `true` where the template overload treats it as an action parameter, so parameterized actions receive the wrong value. The schematic editor also has no `RunAction` handler, and the existing board `RunAction` cannot pass caller data to actions that require it.

**Missing API handlers.** KiCad declares library-table add/update/delete/get, library search, `FocusOnItem`, `RefreshEditor`, and `UpdateBoardStackup` without implementing handlers. MCP fallback tools provide these functions through other routes.

**GUI document opening.** GUI mode has no `OpenDocument` handler, leaving `OpenLibraryItem` unreachable unless the library editor is already open.

**Removed HPGL exports.** `kicad-cli pcb export hpgl` and `kicad-cli sch export hpgl` remain listed but refuse to run because HPGL plotting is no longer supported.

**Symbol editor navigation crash.** The next/previous symbol actions cast the symbol editor frame to the viewer frame type and can crash; the cast needs to be checked before use.

**Footprint auto-association.** cvpcb replaces spaces with underscores in the equivalence-file value but compares it with the unchanged symbol value, so values containing spaces do not match.

**Dead or unusable actions.** Seven declared actions are unbound, never posted, undefined, or unusable: the seven listed above. `SelectOnSheet` crashes when run by name. Their named functions have MCP fallbacks, but the action gaps remain.

**UI-only tools.** Design blocks, graphics import, the drawing-sheet editor, and the GUI counterpart of `LoadAllLibraries` have no API route and are reachable only through the UI.

**Terminal-pad loading.** Loading a board file drops terminal-pad pointers; a later save can lose them, and `setTerminalPad` has nothing to replace until a netlist update sets them again.
