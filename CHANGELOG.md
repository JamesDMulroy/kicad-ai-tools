# Changelog

## [Unreleased]

### Security
- Escaped-process cleanup no longer matches command lines alone: a process is stopped only if its
  executable is under the session's KiCad installation, it started after the session, and its command
  line names the session's own folder, all verified through the handle that terminates it (no PowerShell,
  no `taskkill /T`).
- The KiCad API `.proto` archive is fetched without redirects, and every member must resolve inside the
  destination (absolute, drive-qualified and UNC names are refused).
- `kicad_run_bom_generator` splits the command template before filling placeholders, and refuses to fall
  back to the server's own interpreter.
- The wheel no longer ships the live coverage drivers that use Frida or cross-process memory writes.
- The coverage sweep's survivor clean-up uses the same handle-based checks (no `taskkill`), limited to the
  editors it launched. The stray check's image root is the resolved `<install>/bin` of a real KiCad
  installation; anything else stops nothing.
- The `.proto` archive's decompressed size is capped, and download errors surface as the tool's own error.
- BOM generator placeholders are filled in one pass, and a quoted part inside an argument (`--opt="a b"`)
  stays one argument without its quotes.

### Added
- **On-demand MCP launcher** (`kicad_mcp/launcher.py`, `pythonw -m kicad_mcp.launcher`). The server
  runs only while a session uses it and stops after `server_idle_seconds` (default 600), never while
  one of its jobs is queued or running.
- **Per-machine setup** (`python -m kicad_mcp.setup`): KiCad discovery, a config file, the MCP client
  entry and an optional plugin install.
- **`kicad_env`**: one dependency-free discovery and settings module (environment → config → platform
  backend). The Windows backend reads KiCad's uninstall registry entries; macOS and Linux are stubs.
- **The adaptive editor is merged into the package** (`kicad_mcp/adaptive_editor/`): engine, live IPC
  session and KiCad plugin.
- **MCP adaptive tools**: `adaptive_capability`, `adaptive_plan`, `adaptive_preview`, `adaptive_apply`,
  `adaptive_undo`.
- **L4 shipped approval file** `kicad_mcp/l4/registry/APPROVED.sha256`, with the recorded registry
  for KiCad 10.0.5.
- **Generated operation matrix** `docs/OPERATION-MATRIX.md` (F01), with a drift test.
- **Guards**: a portability test, launcher tests, installer ownership tests and a hidden-desktop test
  harness that writes evidence.

- **Process ownership by Job Object** (`adaptive_engine/job.py`): engine, live-harness and L4 launches own
  every descendant, even after the parent exits; tree walks follow a parent link only to a younger process.
- **Fail-closed owner-desktop tripwires** for the broker, the live harness, the pytest session
  (`tests/conftest.py`) and the KiCad-python unittest tier: a visible window stops the task, and so does a
  sample where the desktop cannot be observed.
- **Release bill of materials** (`scripts/build_release.py`): wheel, sdist, `SHA256SUMS` and a CycloneDX 1.5
  SBOM of the server's resolved dependencies, the plugin's pins and KiCad as an external component.
- **L4 `pcb_board_setup_read`**: selects the Board Setup dialog's Physical Stackup page before reading the
  copper-layer count; the registry was re-recorded with all three operations admitted.
- **`scripts/check_package.py`** (wheel/sdist contents and pyflakes) and **`scripts/source_digest.py`** (the
  package hash every evidence record carries).

### Changed
- **Live-session stale check** hashes every board item's whole IPC state (zones, text, dimensions, groups,
  arc midpoints included), and fails closed when it cannot see a collection.
- **Plugin installer** copies through fsynced temporary files, writes its ownership record atomically and
  creates the plugin folder together with that record, so any interruption is recoverable.
- **Plugin id** is now `local.kicad_ai_tools.adaptive_editor`, which passes KiCad's own identifier rule.
- **Live tests** run every subprocess on a private desktop, with a tripwire over the whole process tree.
- **Live-session apply** checks the whole board (geometry, orientation, layer, added or deleted items)
  and keeps `push_commit` inside the guarded block.
- **Workflow jobs** default to the per-user data directory instead of a folder inside the checkout.
- **L4 menus** are driven natively inside a synthetic menu loop; owned dialogs are found under their
  owner; wx stock ids are accepted as stable automation ids, under a strict grammar enforced at
  validation.

### Fixed
- **No more hardcoded locations.** Removed every machine path, home-folder guess and company name from
  the package: the KiCad root, kicad-cli, the BoardTooling root, the export interpreter, PowerShell,
  the owned-process runtime and the documents folder (OneDrive-redirected Documents is now honoured).
- **Plugin installer ownership.** Uninstall removes only recorded, unchanged files and their bytecode.
  Upgrades refuse to overwrite unowned or changed files, and an interrupted install is recoverable.
