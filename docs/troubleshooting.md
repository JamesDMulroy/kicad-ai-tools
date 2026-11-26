# Troubleshooting

This guide covers the current `kicad-ai-tools` launcher server. Run setup with the same Python environment used to install the package.

## KiCad not found

Run the read-only diagnostic:

```powershell
python -m kicad_mcp.setup --check
```

Review the reported KiCad root, `kicad-cli` path, and any error. If KiCad is installed in a location discovery does not find, set `KICAD_APP_PATH` to its install root. To use a particular command-line executable, set `KICAD_CLI_PATH` to its full path. An explicit but invalid path is reported as an error; it does not fall back to discovery. Run `--check` again after changing the environment.

## The MCP client shows the server as disconnected

Run `python -m kicad_mcp.setup` again, then use the printed `mcp_server_entry` (or `claude_code_add_json`) for your client. Check that the entry's `command` points to an existing `pythonw.exe` beside the Python interpreter used to run setup, and that its arguments point to this checkout's `silent_stdio.pyw` when present. Setup reports an error if the Windows `pythonw.exe` is missing; rerun it with the Python environment that will serve the tools. Apply the updated entry using your client's MCP server configuration and restart or start a new client session as appropriate.

## IPC session tools are refused

The IPC API session tools require KiCad 10.99 nightly or KiCad 11. KiCad 10.0 supports the `kicad-cli`, adaptive, and L4 tools, but not those IPC session tools. Check the installed version in the `--check` output and use a compatible KiCad build for IPC sessions.

## L4 registry is refused

L4 runs only from a registry whose SHA-256 is approved. The shipped registry is approved through `kicad_mcp/l4/registry/APPROVED.sha256`. If you intentionally use a registry you recorded yourself, configure its digest in `KICAD_MCP_L4_APPROVED_REGISTRY_SHA256` as a comma-separated list of SHA-256 values. Treat this as a host security setting; only approve a registry you trust.

## Tests fail on a locked or remote desktop

The L4 UI tests use a private desktop and a tripwire. A locked desktop or remote desktop session can prevent the required desktop checks from succeeding. Run those tests with the Windows desktop unlocked, then rerun the test command. A test session that launches no KiCad process passes even when the desktop cannot be observed; it reports "desktop not observable, no launches".

## BOM generator is disabled

`kicad_run_bom_generator` is refused by default because it can start an external program. To enable it for the server, set `KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR=1` in the environment from which the MCP client starts the server. The setting is an explicit host opt-in.

## Logs and per-user data

Run `python -m kicad_mcp.setup --check` to inspect resolved settings and the per-machine config path. The config defaults to `%APPDATA%\kicad-ai-tools\config.json`; `KICAD_AI_TOOLS_CONFIG` selects a different path.

Per-user state, including the tool manifest cache and workflow jobs, lives by default under the operating system's user data directory in `kicad-ai-tools`. `KICAD_AI_TOOLS_DATA_DIR` overrides that location. Workflow job journals default to `workflow-jobs` beneath the data directory and can be moved with `KICAD_MCP_JOB_DIR`.

The server uses the MCP client's logging facilities for its process output; this project does not define a separate fixed server log directory. Consult the logs for your MCP client when investigating a launch or protocol failure. Avoid including secrets or sensitive project data in reports.

## Reporting a problem

For a reproducible bug, open an issue using the repository's issue templates. Include the relevant error text, steps to reproduce, and the Python and KiCad versions; remove secrets and private design data from logs first. For a security issue, follow the instructions in `SECURITY.md` instead of posting details publicly.
