# Adaptive Editor: live KiCad 10 IPC plugin (Phase B), as built

Deliverable form R25: a KiCad plugin. It runs the Phase A engine (`kicad_mcp/adaptive_editor/adaptive_engine/`) against
the running PCB Editor through KiCad 10.0.5's IPC API (`kicad-python`/`kipy` 0.8.0).

## Compatibility
- The plugin targets KiCad 10.0.5 and uses the KiCad IPC plugin runtime with `kicad-python`/`kipy` 0.8.0.
- The IPC API supports board snapshots and committed edits. It cannot show uncommitted changes on the KiCad canvas, so previews appear in the plugin panel rather than following the cursor on the canvas.

## 1. Components
```
kicad_mcp/adaptive_editor/live/live_session.py    LiveSession: snapshot -> engine worker; preview-only update; one-commit apply
kicad_mcp/adaptive_editor/live/engine_worker.py   persistent worker under KiCad's python.exe (pcbnew): load / solve / edges
kicad_mcp/adaptive_editor/live/preview_render.py  ChangeSet -> drawing primitives (mm) -> SVG or Tk canvas
kicad_mcp/adaptive_editor/live/hidden_kicad.py    private-desktop pcbnew + run_hidden() for GUI self-tests
plugin/plugin.json             IPC plugin manifest (validated against share/kicad/schemas/api.v1.schema.json)
plugin/adaptive_drag.py        action entrypoint "Adaptive Drag"
plugin/adaptive_edge.py        action entrypoint "Adaptive Edge Move"
plugin/panel.py                Controller (UI-free logic) + Tk Panel (preview canvas and controls)
plugin/requirements.txt        exact pins: kicad-python 0.8.0 and its dependency closure
kicad_mcp/adaptive_editor/plugin/install.py              install / uninstall / status with an ownership record (sha256 per file)
plugin/make_icons.py, icons/   toolbar icons (light/dark, 24/48 px)
kicad_mcp/adaptive_editor/tests_live/run_live_session.py, run_plugin_live.py   live tests (hidden desktop only)
```

## 2. LiveSession semantics (one undo step per operation, F06)
- `start()`:
  - Take a snapshot of the live board text (`get_as_string`, UUIDs preserved). Copy the project's
    `.kicad_pro`/`.kicad_dru` next to the snapshot, because the engine never invents rule values.
  - Load the snapshot into the engine worker as the baseline.
  - Record two things, taken while the board is unchanged:
    - the geometry (position, or end points) of every item a plan can move: footprints, vias, tracks
      and arcs, and segment-like shapes;
    - the whole state of every board item (footprints, tracks and arcs, vias, shapes, zones and
      keepouts, text, dimensions, groups): a sha256 of each item's serialized IPC protobuf.
- `update(request, render)`:
  - Solve from the baseline. The result depends only on the current request, so A→B→A gives A.
  - Return the status, report, change set, and an SVG or drawing primitives.
  - The live board is **not** touched.
- `apply(message, refill=False)`:
  - Stale check over the **whole board**: if any item's state changed (a move, rotation, flip, layer,
    arc midpoint, zone outline or text edit), or an item was added or deleted, apply refuses with
    `concurrent_edit`. If the check cannot see a collection or an item's state, it refuses with
    `stale_check_unavailable` (fail closed).
  - Then one `begin_commit`, one `update_items` with absolute targets, `create_items` for bend
    segments, and `push_commit`. That is **one undo step** in KiCad.
  - Zone refill is optional and a separate KiCad operation.
- `cancel()`: nothing was written, so nothing is undone.

## 3. Interaction (R1–R8, R21–R23)
- **Adaptive Drag:** select footprints and press the toolbar button. The panel shows the preview
  canvas, which redraws instantly on every change. To set the displacement, either drag on the canvas
  (Fusion-style) or use the dx/dy sliders and fields. Other controls:
  - mode: cascade or explicit;
  - copper: adapt or fixed;
  - stop-at-limit (partial);
  - frozen, priority and movers references;
  - an eligibility radius;
  - the status/report text: achieved vs requested, who moved, legs re-aimed, blockers and warnings.

  Apply writes everything as one undo step; Cancel writes nothing.
- **Adaptive Edge Move:** select one straight Edge.Cuts segment. The panel works out which side of the
  board it is. Positive outward distance grows the board; negative shrinks it and pushes parts inward.
- **Not provided:** continuous adaptation while dragging on the KiCad canvas itself. The 10.0.5 IPC
  API cannot render uncommitted changes. `native_board_interaction_capability("canvas_mouse_tracking")`
  reports this.

## 4. Packaging and install
- `python kicad_mcp/adaptive_editor/plugin/install.py install [--documents DIR | --target DIR] [--dry-run]` copies the plugin
  and the engine (`engine/live`, `engine/adaptive_engine`) into
  `<documents>/KiCad/<major.minor>/plugins/local.kicad_ai_tools.adaptive_editor` and writes `.install-record.json`.
- An existing directory without our record is never overwritten.
- Upgrades refuse if an installed file was modified locally.
- `uninstall` removes only recorded files whose bytes still match, then empty directories.
- `status` reports drift and files that are outdated vs the source.
- KiCad builds the plugin's venv on next start (or Preferences > Plugins > Reload). It needs PyPI
  access for the pinned wheels.

## 5. Verification
- Live tests run on a private Win32 desktop.
- Not exercised: a human pressing the toolbar button. Plugin actions are invoked from toolbar
  buttons, not TOOL_ACTIONs, so `run_action` cannot trigger them. The button runs `adaptive_*.py` →
  `panel.main()`, which uses the same Controller that was driven above.
