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
an issue assigned by the lead developers first; human-written commit messages and descriptions).

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
`kicad_mcp/ipc/libtables.py`). They work on a session's candidate copy, never on the owner's real files or
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

## State on 2026-10-15, overnight (KiCad 10.99.0-5265-g0616634034)

**Final, 2026-10-15: 1208 of 1217 verified live (every pass with a KiCad-side readback and a control, checked by cheap validators; questioned rows re-run with the missing controls), 9 upstream gaps, 0 unverified, 0 missing.** The 9 are KiCad-side: 7 actions dead inside KiCad (never bound or never posted: showBoundingBoxes, extractFile, removeFile, generateBOMExternal, eeschema SyncSelection, rescueAutosave, SelectOnSheet), each with its FUNCTION provided by a proven MCP tool ("function covered" in the gate report), and the 2 HPGL exports KiCad 10.0 removed ("Plotting to HPGL is no longer supported"). The 8 API commands KiCad declares but never implements (library tables, library search, focus, refresh, stackup update) are verified through MCP fallback tools. Tests: 932 passed, 27 skipped. New upstream bug: terminal pads read from a board file are dropped at load (files.cpp:851 `BuildListOfNets` clears the pad pointers and only the parser re-resolves them, netinfo_item.cpp:95), so the next save loses them and `setTerminalPad` has nothing to replace until a netlist update sets them again (proven: the netlist import route).

New routes and what they proved (every pass has a readback and a control):

- **Router in-route actions** (`coverage/live_router.py`, KiCad's 6-layer RoyalBlue54L demo): the route key primes
  the router at the pointer (a hotkey carries its position), so the key is the first point; a double click commits
  (Escape aborts the whole route, `AbortPlacement`); each route is laid in its own place, since a route over an
  earlier one's tracks is merged into them. Tracks and vias are read back through the API. Proven: undo last
  segment, switch posture, cancel current item (the router stays active: status bar), attempt finish and continue
  from end (a two-pad net, other copper cleared, walk-around mode), place blind / micro via and via stack (layer
  pair preset F.Cu/In1.Cu and a microvia stack preset in the project file; via types read back), the three
  select-layer-and-place-via keys (the "Select Layer" list is clicked on its grid rows; the via ends on the picked
  In2.Cu), track-width change mid-route (`trackViaSizeChanged`: the committed head takes the new width), and
  switch arc posture (the arc's midpoint flips side). Router modes: the saved `tools.pns.mode` after a clean close.
- **Drag and drop** (`coverage/drop_hook.py`): the `WM_DROPFILES` block must belong to the receiving process, so a
  Frida hook allocates it inside the session's KiCad and posts the drop to the frame. Proven: append board (layer
  mapping answered, items placed), import footprint (painted message panel names it), the project manager's
  Gerber drop (GerbView asked to start with the files, through the launch hook). A dropped `.txt` is the control.
- **Project manager idle, root cause found and fixed**: the hidden manager's main thread sat in the "3D Model
  Previews" native task dialog (`MaybeShowModelPreviewSetupPrompt`, common/model_preview_manager.cpp:85, posted
  from kicad/kicad.cpp:474), whose modal loop never sends wx idle events, so every idle-deferred project-tree open
  never ran. `FrameSession` now answers it "Skip This Version" in the session's own profile. `editOtherSch` and
  `editOtherPCB` then pass (project-tree activation, launch hook records the other editor's start).
- **Pickers**: the generic picker through Set Grid Origin (grid origin read back through the API; a plain click is
  the control); Position Relative's "Select Point..." (`selectPointInteractively`, the dialog's reference label
  names the clicked point); the picker sub-tool through Move With Reference (the footprint moves by target minus
  the picked point, not by its own anchor).
- **Measure tool**: the status bar names "Measure Tool" only while it runs, and the ruler pixels lie along the
  clicked segment against two no-tool runs.
- **In-process call readbacks on exported KiCad functions**: `angleSnapModeChanged` re-selects the line-mode
  toolbar button; the toolbar asks `KiDisabledBitmapBundleDef` (exported by kicommon.dll) for that action's icon.
  Two sessions preset to different angle-snap modes ask for different icons; an unrelated action asks for none.
- **Menus**: `updateMenu` (the library tree's context menu is shown dirty and evaluated at open: different entries
  over empty space and over a row); `selectionMenu` (the schematic guess keeps a text lying in a sheet's inner
  half, so a click there opens "Clarify Selection" with both; a click on the sheet alone opens none).
- **Push Pad Settings**: one pad enlarged through its footprint (a pad sent alone to UpdateItems is ignored);
  siblings take its size after the dialog's "Change Pads on Current Footprint".
- **New upstream gap**: `common.Embed.removeFile` is bound (embed_tool.cpp:92) but nothing runs it with its file-name
  parameter; RunAction's `true` fails the parameter cast (tool_event.h:491) and removes nothing. The embedded-files
  panels call `EMBEDDED_FILES::RemoveFile` directly.
- **Library-tree and control actions** (worker, `coverage/live_libctl.py`): tree search focus (SetFocus hook), collapse
  all, unpin, select columns, save all, drop-add library, rename, import, copy footprint (copy then paste against a
  paste with nothing copied), switch to project manager (z-order), refresh (tree rebuilt), cursor click, refresh
  preview / find / preferences (in-process call counts against unbound keys).

## State on 2026-10-14, evening (KiCad 10.99.0-5265-g0616634034)

**1129 of 1217 verified, 14 upstream gaps, 66 unverified, 8 missing; the gate does not pass yet** (the inventory lost one false entry, `common.Control.Fake`, a local the hotkey store builds to look up a section name). The day went 953 -> 1129. Remaining, by tool: pcbnew.InteractiveRouter (14): AttemptFinish, CancelCurrentItem, ContinueFromEnd, HighlightMode, PlaceBlindVia, PlaceMicroVia, PlaceViaStack, SelLayerAndPlaceBlindVia, SelLayerAndPlaceMicroVia, SelLayerAndPlaceVia, ShoveMode, SwitchPosture, UndoLastSegment, WalkaroundMode; common.Control (12): collapseAll, cursorClick, ddaddLibrary, libraryTreeSearch, refreshPreview, saveAll, selectColumns, showProjectManager, unpinLibrary, updateFind, updatePreferences, zoomRedraw; eeschema.EditorControl (7): createNetChainBetweenPins, ddAddImage, highlightNetChain, nameNetChain, nextNetItem, previousNetItem, selectOnPCB; pcbnew.Control (4): DdAppendBoard, ddImportFootprint, rehatchShapes, updateLocalRatsnest; 3DViewer.Control (3): pivotCenter, reloadBoard, showBoundingBoxes; eeschema.SymbolLibraryControl (3): importSymbol, renameFootprint, toggleSyncedPinsMode; kicad.Control (3): editOtherPCB, editOtherSch, viewDroppedGerbers; pcbnew.EditorControl (3): angleSnapModeChanged, setTerminalPad, trackViaSizeChanged; pcbnew.InteractiveDrawing (3): applyDesignBlockLayout, placeLinkedDesignBlock, saveToLinkedDesignBlock; common.Interactive (2): measureTool, updateMenu; common.InteractivePicker (2): pickerSubTool, pickerTool; eeschema.InteractiveDrawingLineWireBus (2): switchPosture, undoLastSegment; eeschema.Simulation (2): probe, stopSimulation; pcbnew.InteractiveSelection (2): SelectOnSchematic, SelectOnSheet; common.Embed (1): removeFile; common.InteractiveDrawing (1): arcPosture; common.InteractiveSelection (1): selectionMenu; eeschema.InteractiveEdit (1): swapPins; eeschema.NavigateTool (1): changeSheet; eeschema.PointEditor (1): removeCorner; pcbnew.ModuleEditor (1): copyFootprint; pcbnew.PadTool (1): PushPadSettings; pcbnew.Picker (1): selectPointInteractively.

**Cheap validator (Haiku), 2026-10-14 evening:** 142 of this session's passes checked: 111 accepted, 21
questioned, 10 rejected. Acted on: `angleSnapModeChanged`, `highlightNetChain`, `copyFootprint`,
`toggleSyncedPinsMode`, `renameFootprint` and `importSymbol` were withdrawn until each has its own readback; the
Gerber reload was re-proven (an edited layer function read back after the reload); `SelectOnSheet` is not an
upstream gap (it is bound, only never posted) and is unverified again. Kept: the 3D material modes (the sweep
only accepts a pixel-only pass after `window_is_quiet`, which the evidence rows did not spell out). To firm up
(questioned, still counted): board `highlightNet` (panel readout may come from hover), schematic
`highlightNet`/`clearHighlight` (pixel with quiet control, not recorded in the row), `lineMode45`,
`showConstraints`, `arcKeepRadius` (start values not read before), `editVertices` (no vertex readback),
`recombinePad` (needs a pad-count readback), `placeText` (two texts from one click), `repeatDrawItem` (copy
not compared with the drawing), router `InlineDrag`/`PlaceVia` (panel text only), `SelectNode` (state change
not described), design-block updates (no content hash), and dialog passes that do not record the close.

What made the difference since the morning:

- **In-process hooks (Frida 16, the `coverage` extra), each confined to the session's own KiCad process:**
  `launch_hook` (outside programs are recorded and never start), `text_hook` (the strings wx paints, e.g. the
  message panel), `cursor_hook` (where KiCad believes the mouse is, and whether its left button is held, so
  cursor-bound keys and drags work on the private desktop), `focus_hook` (which control has keyboard focus,
  and where KiCad moves it).
- **Readbacks a person would use:** UI Automation on lists, trees, toolbar boxes and the status bar (zoom,
  cursor and local coordinates), the API, saved files, the clipboard guard, and window pixels only for view
  actions against a quiet control key.
- **Drivers:** `live_libtree`, `live_designblocks` (workers), `live_gerbview`, `live_pleditor` (canvas tools,
  read back from the saved sheet), `live_zones`, `live_justify`, `live_sim` (plot exports, zooms, undo zoom,
  plot to schematic), `live_statusbar` (local coordinates, zoom tool), `live_units`, `live_pinpush` (clicks
  calibrated from the status bar), `live_wiretool`, `live_cvpcb_focus`, `live_board_misc` (worker).
- **Fixtures:** KiCad's own demos where the test design has nothing to act on yet: `stickhub` (a routed
  board; a Qwiic/I2C hub, not a USB one), `simulation/rectifier` (SPICE model and workbook), `ecc83` (a
  multi-unit symbol).
- **Safety:** the owner-desktop tripwire now runs for a session's whole life (it used to stop after start-up);
  leaks are logged to `%TEMP%\kicad-ai-tools\leaks.log`. The one entry so far is a crash reporter
  (WerFault) after a worker's session crashed, ended while still invisible.

## State on 2026-10-14 (KiCad 10.99.0-5265-g0616634034)

954 of 1218 verified, 10 upstream gaps, 227 unverified, 27 missing; the gate does not pass yet. This session added
the clipboard guard (`coverage/clipboard.py`), the in-tool driver (`coverage/live_midtool.py`: length tuner and
router keys mid-route), context fixtures in `live_hotkeys` (`--names`, `--enter-sheet`, `--tree`, `--cursor`),
a hierarchy fixture for sheet navigation, and removed the two hidden tool registrations (`3DViewer.Control`,
`cvpcb.Control`) from the action inventory. Remaining unverified groups (action count): common.Control 30,
eeschema.SymbolLibraryControl 18, pcbnew.InteractiveRouter 16, pcbnew.ModuleEditor 15, eeschema.EditorControl 13,
pcbnew.EditorControl 13, gerbview.Control 11, kicad.Control 11, cvpcb.Control 10, pcbnew.Control 10,
eeschema.InteractiveDrawing 9, 3DViewer.Control 6, common.SuiteControl 5, design blocks 10, pcbnew.PointEditor 5,
cvpcb.Association 4, eeschema.Simulator 4, pcbnew.PadTool 4.

## Not yet proven, and why (2026-10-14 sweep)

Nothing below is in the evidence. Each line is an attempt that produced no observed effect, or a route that
the standing rules forbid. They are listed so the next session starts from the attempt, not from zero.

- **Interactive tools without a typed API route (27, state `missing`).** The gate counts an interactive tool
  (AF_ACTIVATE) only when an API command makes its result (the typed tools). Pressing its key activates the
  tool but does not place, draw or import anything, so the hotkey route is not counted (tests encode this).
  They are mostly design blocks, sheet-from-file, graphics import and the drawing-sheet editor tools, which
  upstream gap 6 says have no API.
- **Actions that act on the item under the cursor.** `live_hotkeys --cursor` selects an item through the API,
  zooms to it (`zoomFitSelection`: a reserved hotkey in the schematic editor, which has no RunAction) and
  clicks the canvas middle. On the board that works: a posted click at the middle selects exactly the fitted
  item, and `pcbnew.EditorControl.highlightNet` then shows an effect. Still no effect on the board for
  `hideNet`, `showNet`, `toggleNetHighlight`, `setTerminalPad`, `drillSetOrigin`, `zoneMerge`,
  `moveMidpoint`, `editVertices`, local ratsnest; and 0 of 11 in the schematic editor (net highlight, next
  and previous net item, unit switching, swap pins, push pin sizes), where a posted mouse move leaves
  KiCad's cursor elsewhere (a calibration run saw it ignored) and a click did not help. Next step: read the
  schematic cursor position back (status bar X/Y through UI Automation) to calibrate before pressing.
- **In-tool router and length tuner actions** (`live_midtool.py`): proven for the length tuner
  (amplitude and spacing keys, settings) and for the router's corner modes (45, 90, arc 45, arc 90, switch,
  cycle mode) and three router dialogs. Open: shove, walkaround and highlight modes, switch posture,
  undo last segment, attempt finish, cancel, continue from end, break track and the via placement family
  (no pixel change in the router window; `PlaceVia` showed only a state change and `Drag45Degree`,
  `DragFreeAngle`, `InlineDrag` only a pixel change, so they are held back). Next step: read the router
  settings back (the Interactive Router Settings dialog) after each mode key. `pcbnew.PointEditor.*`
  (5) were not attempted: they need a corner under the cursor.
- **Library tree actions** (`SymbolLibraryControl.*`, `ModuleEditor.*`, tree-bound `common.Control.*`):
  the tree row is selected with Home/Right/Down on the tree window, but the actions then show only a
  pixel change, an Error dialog or an editor restart; those are not accepted as proof.
- **Design blocks** (`*DesignBlockControl.*`): a project design-block library (`test.kicad_blocks`) was
  registered in `design-block-lib-table`, but the five runnable actions did nothing; the design-block pane
  is not shown in the session.
- **Footprint assigner (cvpcb):** hotkeys reach its lists unreliably; 28 of its targets showed no effect.
- **Simulator** (`Simulator.*`, `Simulation.*`): the simulator frame did not open on the hub fixture (no
  SPICE models); not attempted on a `demos/simulation` project.
- **Actions that leave KiCad** are now proven through the launch hook (the request is recorded, nothing
  starts). Open: `kicad.Control.editOtherSch`/`editOtherPCB`: the project tree is multi-selection
  (`wxTR_MULTIPLE`), where wx vetoes selection changes it did not make (`TVN_ITEMCHANGING`), so rows are
  reached with posted Home/Down keys (proven: wx's own selection key handler runs and `GetSelections`
  returns the row) and a posted Enter raises "item activated", but KiCad's idle-time `Activate` never
  launched the other editor on the private desktop (no `wxExecute`); `viewDroppedGerbers` needs a drop.
  Traced with Frida in the session: wx's `ProcessIdle` (wxAppBase, wxEventLoopBase) is never called in the
  hidden project-manager session, before or after the activation, and activating a folder row does not
  expand it either. `PROJECT_TREE_PANE::onSelect` only bookmarks the row and `onIdle` opens it, so every
  project-tree open waits on an idle step that never comes. Hiding the "3D Model Previews" window and sending
  activation messages did not start idle processing. Next step: find what keeps the manager's loop from idling
  (its thread retrieves almost no messages through user32's Peek/GetMessage, so it may run a nested loop).
- **Pixel-only passes held back.** A hotkey or menu action whose only effect is "the window changed", and that
  is not a plain view action, was not accepted when the detail named an editor restart, a leaked window from
  the previous item or no state readback. They are kept in the sweep files, not in the evidence list.

Clipboard actions (`3DViewer.Control.copyToClipboard`, the image converter's symbol export to the clipboard)
are proven: the guard in `kicad_mcp/coverage/clipboard.py` saves the owner's text, requires a sequence-number
change plus the expected format, and a pass needs a verified restore of the text.
