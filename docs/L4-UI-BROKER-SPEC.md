# L4 UI broker: implementation spec

L4 is a UI fallback that drives KiCad on a private Win32 desktop from a recorded, sha256-approved registry. Its backend lives in `kicad_mcp/l4/hidden_desktop.py` and `kicad_mcp/adaptive_editor/live/hidden_kicad.py`.

## Components (kicad-mcp)
```
kicad_mcp/l4/hidden_desktop.py   # vendored copy of hidden_kicad.py (launcher, tripwire, profile seeding), generalised to any KiCad exe
kicad_mcp/l4/monitor.py          # user-desktop guard: task windows on input desktop, foreground changes, WTS console state
kicad_mcp/l4/uia_helper.py       # runs ON the hidden desktop (spawned there); UIA via comtypes; reads JSON commands on stdin, replies JSON
kicad_mcp/l4/registry.py         # closed versioned registry: schema, load, sha256 approval check, typed value validation
kicad_mcp/l4/broker.py           # ui_fallback(operation, project_clone_dir, params) orchestration
kicad_mcp/l4/registry/ui-registry-10.0.5.json
kicad_mcp/tools/l4_tools.py      # MCP tools
tests/test_l4.py
```

## Rules
- **Closed registry.** Each operation enumerates:
  - the window, dialog and control identifiers (UIA AutomationId, or Name + ControlType + ancestry path);
  - a typed parameter schema;
  - a postcondition, checked against files on disk;
  - a safe-to-cancel flag per dialog.

  Callers supply only the operation name and typed values. Anything unknown is rejected before any
  input is sent. A registry whose sha256 is not in the approved list (`kicad_mcp/l4/registry/APPROVED.sha256`, plus `KICAD_MCP_L4_APPROVED_REGISTRY_SHA256` for a registry you re-recorded) refuses all operations, as does a
  registry version that differs from the running KiCad build.
- **Identifier recording** (`record_identifiers`): open the target dialogs on the hidden desktop and dump
  their UIA trees. Record candidate identifiers only if they resolve to the same control across 3
  independent launches . Unstable ones are dropped and reported.
- **Project clone.** Operations act only on a task-owned copy of the project made by the broker. They
  never act on the original.
- **Launch.** Every launch verifies env redirection in the child environment block. The pre-launch integrity check of installed packages is trivial here because
  the profile holds no packages.
- **Monitor during a run.****
  - Zero task windows on the input desktop. Any window kills the process immediately and fails the run.
  - No foreground change attributable to the task.
  - Owner console session connect/lock state unchanged, checked by WTS before and after.
- **Unexpected dialog:** capture its UIA tree. Cancel only if the registry marks it safe-to-cancel;
  otherwise terminate the instance by exact PID.
- **Cleanup:** terminate by exact PID, close the desktop, keep the evidence directory.
- **No crash/assert forcing here** (crash-UI handling requires a configured precondition).

## Initial registry operations (record identifiers for these)
1. `pcb_open_and_save`: open the cloned board in pcbnew and save it (File > Save). Postcondition: the
   file is rewritten and still loads with kicad-cli.
2. `pcb_export_gerbers_dialog`: File > Fabrication Outputs > Gerbers. Set the output directory
   parameter (clone-relative) and press Plot. Postcondition: at least one .gbr file in the output
   directory.
3. `pcb_board_setup_read`: open Board Setup, select the Board Stackup > Physical Stackup page, read the
   copper-layer count (read-only), close with
   Cancel (safe-to-cancel). Postcondition: values returned and the file unchanged.

Each operation exists because it exercises dialogs and menus, which is the L4 purpose. The
equivalents in kicad-cli are noted in each registry entry's `api_alternative` field.

## Tests (`tests/test_l4.py`)
Unit (no GUI):
- registry schema validation;
- unknown operation, parameter or type rejected;
- unapproved sha256 refuses all operations;
- version mismatch refuses;
- clone-relative path escapes rejected;
- monitor logic on fake window lists.

Integration (real KiCad on the hidden desktop, marked `l4_live`, run in the check):
- `pcb_open_and_save` and `pcb_export_gerbers_dialog` succeed on a synthetic project clone;
- the original project is byte-identical afterwards;
- zero task windows reached the input desktop during the run;
- WTS state unchanged;
- a decoy second KiCad instance is never targeted (the binding is by PID and desktop).

`record_identifiers` must have produced a registry whose identifiers pass the 3-launch stability
check, committed as `ui-registry-10.0.5.json`. Report its sha256 for owner approval. Tests may set
the approval env var to that sha256 explicitly.
