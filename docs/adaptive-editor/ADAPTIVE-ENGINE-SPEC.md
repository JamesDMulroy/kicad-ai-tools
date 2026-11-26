# Adaptive engine: implementation spec (Phase A, engine + file adapter)

This phase builds the shared engine and the headless file adapter. Phase B adds the live KiCad 10 IPC plugin on the same engine, using one open commit (live in the editor, pushed as **one undo step**, or dropped on cancel).

An earlier standalone adaptive-move script is **not** reused as the feature. New code lives in `kicad_mcp/adaptive_editor/adaptive_engine/`.

## 1. Package layout (all new)
```
kicad_mcp/adaptive_editor/adaptive_engine/
 __init__.py
 model.py # neutral board model (pure Python, no pcbnew import)
 geometry.py # polygon/segment math in integer nanometres (pure Python)
 solver.py # cascade / explicit / freeze / priority / eligibility / stop-at-limit; edge shrink
 copper.py # track/via adaptation: re-aim legs to moved terminals (no rerouting)
 pcbnew_adapter.py # load a .kicad_pcb with KiCad's pcbnew -> model; apply a ChangeSet back
 session.py # preview / apply / undo for files; DRC comparison; refill
 cli.py # JSON-in/JSON-out entry point run with KiCad's python.exe
kicad_mcp/adaptive_editor/tests/
 fixtures.py # builds synthetic boards with pcbnew (outline, footprints with courtyards, tracks, vias, keepout)
 test_geometry.py, test_solver.py, test_copper.py, test_adapter_session.py
```
`model.py`, `geometry.py`, `solver.py` and `copper.py` import nothing from pcbnew. They are unit-testable
with any Python 3.11. Units are **integer nanometres** throughout (KiCad's internal unit).

## 2. Neutral model (`model.py`)
- `Part`: uuid, ref, position (x,y), rotation, layer side, `courtyard` (list of polygons, each a list of
 points; fall back to the pad bounding box plus `fallback_margin_nm` **only if** no courtyard exists,
 and report `missing_courtyard`), `locked` (native lock), pads (uuid, net, centre, polygon).
- `Track`: uuid, net, layer, start, end, width, optional arc mid, locked. `Via`: uuid, net, pos,
 diameter, drill, layers, locked.
- `Outline`: polygons from `GetBoardPolygonOutlines`, plus the underlying edge segments (uuid, start,
 end) so a single edge can be dragged.
- `Keepout`: rule-area polygons with their flags (no tracks / no vias / no footprints / no copper pour).
- `Zone`: uuid, net, layers, outline polygon (zones are refilled after apply, never hand-edited).
- `Rules`: clearance (board default and netclass/DRU-resolved per pair when available via the adapter),
 copper-to-edge clearance, courtyard clearance (default 0). Rules come from the board ;
 the engine never invents a value. Missing rule data is reported, not guessed.
- `Contract` (optional): per-part movement permissions `{uuid: "movable"|"fixed"}` from an expert
 contract. The effective permission is the **intersection** of contract, request and native lock
 . `ALL_UNLOCKED` never widens the contract.

## 3. Operations (`solver.py`)
Request (JSON):
```
{ "driver": {"kind":"part","uuids":[...]} | {"kind":"edge","edge_uuid":..., "side": "left|right|top|bottom"},
 "displacement_nm": [dx, dy], # for part drivers; for edge drivers the normal displacement
 "mode": "cascade" | "explicit", # / / 
 "movers": [uuid...], # explicit mode: authoritative set 
 "frozen": [uuid...], # : obstacles, never moved
 "priority": [uuid...], # : these move first; others only after they are exhausted
 "eligibility_nm": int | null, # : only parts within this distance of the driver/edge may join
 "allow_partial": true, # : if blocked, achieve the largest legal displacement
 "copper": "adapt" | "fixed" } # /: re-aim attached legs, or treat copper as obstacles only
```
Semantics:
- **Cascade :** the driver translates by the displacement. Any permitted part whose courtyard
 (inflated by courtyard clearance) is hit by a moving part's swept courtyard is pushed in the same
 direction, by the minimum amount that clears it. This propagates as a wave in order of first contact
 (a chain, not a broadcast). Priority parts are pushed before non-priority parts at equal contact.
- **Explicit :** only `movers` (plus the driver) may move, and they are pushed as in cascade. Any
 other part they hit is an obstacle.
- **Obstacles:** frozen, locked, contract-fixed or ineligible parts; keepouts with "no footprints";
 board edge (a part must stay inside the outline minus copper-to-edge clearance for its pads); and,
 when `copper: fixed`, all tracks/vias.
- **Stop-at-limit :** if the full displacement is illegal, binary-search is **not** assumed to be
 valid. Step from 0 to the full displacement in increments of `max(10 µm, |d|/200)`, then refine the
 last legal step by bisection **within that interval only**. Report requested vs achieved and the
 typed blockers: `{uuid, kind: frozen|locked|contract|keepout|edge|ineligible|copper, at_nm}`.
 With `allow_partial: false`, a blocked request returns no change set.
- **Edge drag :** an outline edge segment (and its two adjacent segments' shared endpoints)
 moves along its normal. Parts that the new outline would exclude (pads outside the outline minus edge
 clearance) are pushed inward by the cascade rules. Supported topology: a simple polygon whose dragged
 edge is axis-aligned. Anything else (arcs on the edge, cutouts touching it, self-intersection
 afterwards, collapse to zero area) is rejected with a typed reason, leaving everything unchanged.
- **Determinism:** the same input always gives the same ChangeSet.

## 4. Copper adaptation (`copper.py`, –, /)
- A **leg** is the maximal chain of tracks from a pad of a moved part to the first via, pad or
 junction belonging to a non-moved item. If every terminal of a chain moves by the same vector
 (both ends belong to moved parts, or a via is enclosed by a moved courtyard), the whole chain and its
 vias translate rigidly ().
- Otherwise the leg **re-aims**. Only the segment touching the moved pad changes. Its endpoint follows
 the pad. If the segment was axis-aligned or 45°, insert at most one bend point that restores
 45°/90° geometry using the segment's existing direction. Track widths, layers and nets are never
 changed.
- After adaptation, each changed segment is checked against the rules: clearance to other-net copper,
 pads, vias, keepouts ("no tracks"/"no vias") and edge clearance. A leg that cannot stay legal is
 **reported** (`leg_blocked`, with uuids). The engine does not reroute it . With `allow_partial`, the
 whole operation backs off to the largest displacement where every leg is legal.
- **Tuned geometry :** a track inside a tuning-pattern group, or a chain with ≥ 4 alternating
 bends of equal pitch (serpentine heuristic), is **protected**. It may translate rigidly with its parts
 but must not be stretched. If the move would stretch it, the operation blocks (`tuned_leg`). Total
 length before/after is reported for every leg touched.
- Net connectivity is preserved by construction: no segment is deleted, no endpoint is detached.

## 5. File adapter and session (`pcbnew_adapter.py`, `session.py`)
- Run with KiCad's `python.exe` and `scripts/kicad_headless.py`'s non-interactive policy.
- `preview(board, request) -> {changeset, report, candidate_path, png_path, drc}`:
 1. Load the board.
 2. Compute the ChangeSet.
 3. Apply it to an in-memory copy.
 4. **Refill zones** with `ZONE_FILLER` (teardrops regenerate on refill, /).
 5. Save `candidate.kicad_pcb` in a task-owned session directory, never over the original.
 6. Render a PNG with `kicad-cli pcb export svg` (then convert, or keep the SVG if no converter
 exists).
 7. Run DRC on the original and the candidate (`kicad-cli pcb drc --format json`). Compare the
 violation **identities** (type + item uuids), not counts (). New violations are listed, and the
 preview is marked `drc_regressions: true` if any appear.
- `apply(session_id, expected_board_sha256)`: refuses if the original board's sha256 changed since the
 preview (concurrent edit). Otherwise it keeps a byte-exact backup in the session, then atomically
 replaces the board with the candidate (write temp, then `os.replace`). Apply also refuses if
 `drc_regressions` and `force` is not given; `force` is never set by the tool on its own.
- `undo(session_id)`: restores the backup byte-for-byte, only if the current board equals the applied
 candidate's sha256 (expected-content check). Otherwise it refuses. This gives **one-step undo** per
 operation .
- Sessions are JSON journals under the board's `.adaptive/` directory: request, sha256 of original,
 candidate and backup, report.

## 6. CLI and MCP surface
- `cli.py`: `python.exe cli.py {plan|preview|apply|undo|capability} --json <request.json>`. It prints
 one JSON object and exits 0 on success, 2 on refused, 1 on error.
- `kicad-ai-tools-server`: its `adaptive_plan`, `adaptive_preview`, `adaptive_apply` and `adaptive_undo`
 tools call `cli.py` through the existing hidden-subprocess runner (no window). Phase B adds the live plugin.

## 7. Acceptance tests (offline, synthetic boards built by `fixtures.py`)
Geometry:
- polygon collide/clearance;
- swept-courtyard collision (thin obstacle between start and end is detected).

Solver:
- both axes and directions;
- cascade chain of 3 with unequal gaps (wave order);
- explicit set honoured;
- frozen part blocks;
- native-locked part blocks;
- contract-fixed but unlocked part blocks;
- priority ordering;
- eligibility radius excludes far parts;
- stop-at-limit reports the achieved displacement and the blockers;
- `allow_partial: false` gives no change;
- keepout blocks;
- edge clearance blocks;
- missing courtyard reported;
- duplicate or unknown uuid rejected;
- deterministic output.

Edge:
- each of 4 sides shrinks and pushes parts;
- edge collapse rejected;
- arc edge rejected;
- part that cannot move (frozen) blocks the shrink at its limit.

Copper:
- rigid translation of a fully enclosed leg and via;
- re-aim with 45° restoration;
- leg blocked by other-net track (reported, nothing rerouted);
- tuned serpentine protected (`tuned_leg`);
- width/layer/net unchanged;
- connectivity unchanged (BuildConnectivity: same unconnected count **and** same net per pad).

Session:
- preview never touches the original;
- DRC identity comparison flags an introduced violation even when the total count is unchanged;
- apply refuses on a changed original;
- apply then undo restores byte-identical;
- undo refuses after an external edit;
- refill runs (zone filled area changes when a part moves into the pour).

The check command runs `unittest` with KiCad's python, headless.
