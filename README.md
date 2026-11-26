# kicad-ai-tools (KiCad MCP)

An MCP server that gives AI agents real access to KiCad: schematics, PCB layout, design rules, 3D, and
fabrication outputs. It drives KiCad only through KiCad's own official interfaces (the IPC API,
`kicad-cli` and the editors themselves), with no fork and no patched KiCad. In the last full live run,
1,208 of the 1,217 functions the KiCad build exposes were verified end to end; the other 9 are gaps inside
KiCad itself. See `docs/IPC-API-COVERAGE.md`.

## Why it exists

Most KiCad integrations for AI assistants read files or wrap a handful of `kicad-cli` commands. That is
fine for a bill of materials, but not for real design work. This project aims to let an assistant do what
a person does in KiCad:
- place and edit;
- run ERC and DRC;
- change rules;
- export fabrication files.

It does this safely. Work happens on a candidate copy of your project, nothing is written back until you
promote it, and no KiCad window ever appears on your screen.

## Who it is for

- Hardware engineers who use Claude Code, Claude Desktop or another MCP client and want it to work
  inside their KiCad projects.
- Tool builders who need a typed, validated KiCad automation layer.
- KiCad users who want the included **Adaptive Drag** plugin: Fusion-style footprint and board-edge
  moves that shove neighbours within the board's rules.

## What it provides

One Python package (`kicad_mcp`) provides:

- **All of KiCad through its official IPC API (KiCad 10.99 nightly, the future KiCad 11).** Schematic and board, any
  API command the installed build defines, any editor action, all of `kicad-cli`. It works on a
  candidate copy of your project and writes back only when you promote it.
- **An on-demand MCP server.** The launcher answers the client handshake from a cached tool manifest,
  starts the server only while a tool is in use, and stops it when idle.
- **Adaptive board editing.** Drag footprints or a board edge. Neighbours shove within the board's own
  rules and copper legs re-aim. You preview the result and apply it as one undo step. This is available
  as MCP tools (`adaptive_plan/preview/apply/undo`) and as a KiCad plugin.
- **L1, typed `kicad-cli` coverage.** All commands are validated against a registry, and a backup is
  made before any mutation. Free-form argv is never accepted.
- **L2, pinned KiCad source.** Tree, read and path-scoped search of KiCad's public source.
- **L3, plugin inventory and offline inspection.** Nothing is executed.
- **L4, UI fallback on a private desktop.** It runs from a recorded, sha256-approved UI registry and
  never shows anything on your screen.
- **Optional BoardTooling workflow tools** (contracts, gates, release). They need a separately installed
  BoardTooling checkout, which is not part of this repository. Without one, those tools report "not
  configured".

`docs/OPERATION-MATRIX.md` lists every operation with its route, status and evidence.

## Requirements

- **Windows 10 or 11.** The job runtime and the private-desktop (L4) layer use Win32 APIs. macOS and
  Linux discovery exists but is untested.
- **Python 3.11 or newer.**
- **KiCad, installed separately:** [download KiCad](https://www.kicad.org/download/).
- **KiCad:**
  - KiCad 10.0.x (tested with 10.0.5) for the `kicad-cli`, adaptive and L4 tools;
  - KiCad 10.99 nightly (the future KiCad 11) for the IPC API session tools.

  Nothing else is needed: the package compiles the installed build's API schema locally with the
  bundled `protoc`.
- **Internet access once,** for `pip install`. KiCad also builds the plugin's environment from PyPI the
  first time it loads the plugin.

## Install

```
git clone https://git.bastionary.com/james/kicad-ai-tools.git
git clone https://github.com/JamesDMulroy/kicad-ai-tools.git
cd kicad-ai-tools
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[l4]"
.venv\Scripts\python -m kicad_mcp.setup --install-plugin
```

Either repository works.

`setup` does four things:
1. Finds KiCad: from `KICAD_APP_PATH`, else the per-machine config, else the Windows registry, else
   `PATH`. A wrong explicit value is reported, never silently replaced.
2. Writes the per-machine config (`%APPDATA%\kicad-ai-tools\config.json`).
3. Installs the KiCad plugin for your user (`--install-plugin`, optional).
4. Prints the MCP client entry for this machine, as JSON, under `mcp_server_entry` and
   `claude_code_add_json`.

Useful options:
- `--check`: report only, write nothing.
- `--idle-seconds N`: how long an unused server stays up (default 600).
- `--server-name NAME`: the MCP server name (default `kicad_ai_tools`).

Every setting can also come from the environment. See `.env.example` and `docs/configuration.md`.

## Add it to your MCP client

The entry `setup` prints looks like this (your paths will differ):

```json
{
  "type": "stdio",
  "command": "C:\\path\\to\\kicad-ai-tools\\.venv\\Scripts\\pythonw.exe",
  "args": ["C:\\path\\to\\kicad-ai-tools\\silent_stdio.pyw"],
  "env": {}
}
```

### Claude Code

Pass the printed `claude_code_add_json` value:

```
claude mcp add-json kicad_ai_tools '<claude_code_add_json>' --scope user
```

In Windows PowerShell 5.1 the inner quotes are stripped; use PowerShell 7.3 or later, or paste the JSON from a file.

Then start a new session and run `/mcp` to check that `kicad_ai_tools` is connected.

### Claude Desktop

1. Open Settings > Developer > Edit Config. This opens `claude_desktop_config.json`.
2. Merge the printed `mcp_server_entry` into the `mcpServers` map:

   ```json
   {
     "mcpServers": {
       "kicad_ai_tools": {
         "command": "C:\\path\\to\\kicad-ai-tools\\.venv\\Scripts\\pythonw.exe",
         "args": ["C:\\path\\to\\kicad-ai-tools\\silent_stdio.pyw"]
       }
     }
   }
   ```
3. Restart Claude Desktop.

### Other MCP clients

Cursor, VS Code, Windsurf and others use the same shape. Add the printed `mcp_server_entry` to the
client's `mcpServers` (or `servers`) map. The server speaks MCP over stdio and opens no network port.

## Quick start

Ask your assistant, for example:

- "List the kicad-cli commands you can run, then run ERC on `C:\path\to\my\board.kicad_sch`."
- "Export Gerbers and drill files for `board.kicad_pcb` into a new folder `fab-out`."
- "Open `board.kicad_pcb` in a headless session, list the footprints, move R12 by 2 mm, show me the
  changes, and promote them if DRC is clean."

The tools it will use:
- `kicad_cli_commands`, `kicad_cli_describe` and `kicad_cli_run`: typed `kicad-cli`.
- The IPC session tools:
  1. `kicad_session_open(document, mode)` on a `.kicad_sch` or `.kicad_pcb`. `headless` uses
     `kicad-cli api-server` with no window. `gui` runs the real editor on a private desktop that never
     shows.
  2. `kicad_api_commands` and `kicad_api_describe`: what the installed KiCad defines.
  3. `kicad_api_call(session, command, payload)` and `kicad_run_action(session, action)`.
  4. `kicad_session_changes`: KiCad's own diff of what would be written.
  5. `kicad_session_promote`: write back. This is refused if the original changed meanwhile, and every
     replaced file is backed up first. `kicad_session_close` ends the session.
- `adaptive_capability`, then `adaptive_plan`, `adaptive_preview`, `adaptive_apply` (with the previewed
  `expected_sha256`) and `adaptive_undo`.

In KiCad itself: select footprints, or one straight Edge.Cuts segment, and press the **Adaptive Drag**
toolbar button. Drag on the panel preview or use the sliders, then Apply (one Ctrl+Z reverts it) or
Cancel.

## Safety model

In short (full details in `SECURITY.md`):
- The server is local stdio only, with no network listener.
- It runs no shell, and no tool accepts free-form command lines.
- Mutating tools need an explicit flag or a sha256 guard, and make a backup first.
- GUI work runs on a private Win32 desktop with a tripwire.

The single tool that can start an external program, `kicad_run_bom_generator`, is disabled unless the
person running the server sets `KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR=1`.

## Test

```
.venv\Scripts\python -m pip install -e ".[l4]" pytest pytest-asyncio
.venv\Scripts\python -m pytest tests
<KiCad python> -m unittest discover -s kicad_mcp/adaptive_editor/tests -t kicad_mcp/adaptive_editor
```

`<KiCad python>` is KiCad's bundled interpreter, for example `<KiCad root>\bin\python.exe`.

## Documentation

- [Configuration](docs/configuration.md)
- [KiCad MCP clients](docs/KICAD-MCP-CLIENTS.md)
- [Operation matrix](docs/OPERATION-MATRIX.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Temporary storage](docs/TEMP-STORAGE.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

Tests that need KiCad or a live session skip when KiCad is not found. Live tests never show a window:
- set `KICAD_MCP_L4_LIVE=0` / `KICAD_MCP_ADAPTIVE_LIVE=0` to skip them;
- they write evidence under `.tmp/evidence/`, or `KICAD_AI_TOOLS_EVIDENCE_DIR` if set.

The live coverage drivers in `kicad_mcp/coverage/` also need prepared fixture projects. These are copies
of KiCad's demos with extra content, in `$KICAD_AI_FIXTURES`, and are not part of this repository.
The `kicad_mcp/coverage/live_*` drivers are maintainer-only; they need private KiCad fixtures that are not included.

## Layout
- **`kicad_mcp/launcher.py`, `workflow_server.py`, `setup.py`:** the launcher, the server and per-machine
  setup.
- **`kicad_mcp/ipc/`:** the IPC API client, sessions and schema compiler.
- **`kicad_mcp/adaptive_editor/`:** the adaptive engine, the live IPC session and the KiCad plugin.
- **`kicad_mcp/l4/`:** the hidden-desktop UI broker and the recorded registry
  (`registry/APPROVED.sha256`).
- **`kicad_mcp/tools/`:** the MCP tool layers.
- **`kicad_mcp/coverage/`:** the live coverage drivers and gate.

## Building a release

`python scripts/build_release.py --env-python <the server's python>` (needs `hatchling`) writes the
following to `dist/`:
- the wheel and the sdist;
- `SHA256SUMS`;
- a CycloneDX SBOM.

## Uninstall

1. Run `python kicad_mcp/adaptive_editor/plugin/install.py uninstall`. It removes only the plugin files
   it installed.
2. Remove the MCP entry from your client.
3. Delete `%APPDATA%\kicad-ai-tools\`.

## License and credit

MIT (see `LICENSE`). This project started from the open-source
[`kicad-mcp`](https://github.com/lamaalrajih/kicad-mcp) server by Lama Al Rajih. That server's original tools are no
longer part of this project: they worked outside the safety model in `SECURITY.md`. Use the upstream project
if you need them.
Third-party notices are in `THIRD-PARTY-NOTICES.md`.
Contributions are welcome: see `CONTRIBUTING.md`.
