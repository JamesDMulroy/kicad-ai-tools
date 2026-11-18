# Configuration

Nothing in kicad-ai-tools assumes where things live on your machine. Every setting resolves in this
order:

1. **Environment variable.** Set it in the environment of the MCP client or your shell.
2. **Per-machine config file.** `python -m kicad_mcp.setup` writes it to
   `<user config dir>/kicad-ai-tools/config.json` (Windows: `%APPDATA%\kicad-ai-tools\config.json`).
   Set `KICAD_AI_TOOLS_CONFIG` to use another path.
3. **Discovery.**
   - Windows: KiCad's own uninstall registry entries, then `kicad-cli` on PATH.
   - macOS / Linux: `kicad-cli` on PATH only (untested backends).

`python -m kicad_mcp.setup --check` shows what each setting resolved to, without writing anything.

## Settings

| Config key | Environment variable | Meaning | When unset |
|---|---|---|---|
| `kicad_app_root` | `KICAD_APP_PATH` | KiCad install root | Discovery. If set but wrong, every KiCad tool reports it (no silent fallback) |
| `kicad_cli` | `KICAD_CLI_PATH` | Exact kicad-cli executable | `<root>/bin/kicad-cli` |
| `kicad_python` | `KICAD_AI_TOOLS_KICAD_PYTHON` | KiCad's bundled python (pcbnew) | `<root>/bin/python.exe` on Windows |
| `kicad_user_dir` | `KICAD_USER_DIR` | Your KiCad documents folder | `<OS Documents>/KiCad` (redirected folders honoured) |
| `project_search_paths` | `KICAD_SEARCH_PATHS` (comma-separated) | Extra folders to look for projects in | None; no folders are guessed |
| — | `KICAD_DOCUMENTS_HOME` | KiCad's own documents-home override (KiCad honours it too) | OS Documents folder |
| `data_dir` | `KICAD_AI_TOOLS_DATA_DIR` | Per-user state: tool manifest cache, workflow jobs | `<user data dir>/kicad-ai-tools` |
| `job_dir` | `KICAD_MCP_JOB_DIR` | Workflow job journals | `<data dir>/workflow-jobs` |
| `export_python` | `KICAD_AI_TOOLS_EXPORT_PYTHON` | Interpreter for the BoardTooling release exporter (needs `cryptography`) | The server's interpreter |
| `powershell` | `KICAD_AI_TOOLS_POWERSHELL` | PowerShell 7 for BoardTooling gates | `pwsh` on PATH |
| `server_idle_seconds` | `KICAD_AI_TOOLS_SERVER_IDLE_SECONDS` | How long an unused MCP server keeps running | 600 |
| `server_python` | `KICAD_AI_TOOLS_SERVER_PYTHON` | Interpreter that runs the server | `python.exe` beside the launcher's `pythonw.exe`, else the launcher's interpreter |
| `boardtooling_root` | `KICAD_AI_TOOLS_BOARDTOOLING_ROOT` | Optional BoardTooling checkout | BoardTooling tools report "not configured" |
| `boardtooling_trust_config` | `BOARDTOOLING_TRUST_CONFIG` | BoardTooling attestation trust configuration | Release/attestation operations refuse |
| `boardtooling_visual_review_public_key` | `BOARDTOOLING_VISUAL_REVIEW_PUBLIC_KEY` | Visual-review public key file | Release operations refuse |
| — | `KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR` | Host opt-in for `kicad_run_bom_generator` to start an external program (`1` = allowed) | Refused |
| — | `KICAD_MCP_L4_APPROVED_REGISTRY_SHA256` | Extra L4 registry approvals (comma-separated sha256) | Only `registry/APPROVED.sha256` approves |
| `quarantine_root` | `KICAD_MCP_QUARANTINE_ROOT` | Where plugin packages are inspected offline (L3) | `<LocalAppData>/kicad-mcp/quarantine` (the OS known folder; refused if unavailable) |
| — | `KICAD_CONFIG_HOME` | KiCad's own settings-folder override; the plugin inventory follows it like KiCad does | `<Roaming AppData>/kicad/<major.minor>` |
| — | `KICAD<major>_3RD_PARTY` (e.g. `KICAD10_3RD_PARTY`) | KiCad's own third-party (PCM) folder override; the inventory follows it | `<Documents>/KiCad/<major.minor>/3rdparty` |

Variables without a config key are read only from the environment: KiCad's own overrides
(`KICAD_DOCUMENTS_HOME`, `KICAD_CONFIG_HOME`, `KICAD<major>_3RD_PARTY`) are read exactly as KiCad reads
them, and extra L4 approvals are a host-security choice kept out of the config file.

On Windows, paths in JSON need doubled backslashes or forward slashes, for example
`"kicad_app_root": "<KiCad install folder, with / separators>"`.

`.env.example` lists the same variables with a one-line description each. The package does not load a
`.env` file by itself; export the variables in the environment the MCP client starts from.
