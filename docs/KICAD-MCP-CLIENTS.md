# Connect kicad-ai-tools to an MCP client

This is a local **stdio MCP server**. An MCP client launches the command, sends JSON-RPC over its pipes, and discovers the typed tools. Run `python -m kicad_mcp.setup` with the Python that will serve the tools: it discovers KiCad, writes the per-machine config and prints the exact client entry for this machine (on Windows `pythonw.exe <checkout>/silent_stdio.pyw`, or `pythonw -m kicad_mcp.launcher` for an installed package). The launcher answers the handshake from a cached tool manifest and starts the real server only while a session is using it. The server has no network listener.

For Claude Code, pass setup's printed `claude_code_add_json` value: `claude mcp add-json <name> '<claude_code_add_json>' --scope user` (other clients merge `mcp_server_entry` into their `mcpServers` map) (see Claude Code's [MCP documentation](https://code.claude.com/docs/en/mcp)); new sessions see it. Do not paste trust keys into the client entry.

## Use the tools

1. Call `boardtooling_capabilities` and `board_design_profiles` to see supported operations, standard sources, and evidence requirements. `inspect_board_bundle` or `start_board_bundle_job` returns the exact source digest. Poll a started job with `board_workflow_job_status`; `cancel_board_workflow_job` stops an exact owned job.
2. Use `create_board_contract_draft` or `start_board_contract_draft_job` with a project, expected bundle digest, profile, and variant. An expert supplies and reviews the schematic, electrical, mechanical, movement-permission, and source artifacts. Draft creation is not expert approval.
3. Run headless gates with `start_board_gate_job` where applicable. Use `evaluate_board_contract` or `start_board_contract_evaluation_job` with the exact gate report and signed `attestation_paths`. The host alone configures `BOARDTOOLING_TRUST_CONFIG`; a caller cannot select the trust authority. Real acceptance requires independently issued, applicable evidence and complete measured inputs. The server rejects missing, forged, stale, changed, or revoked evidence.
4. `plan_board_placement` and `start_board_placement_plan_job` return source-bound permission-aware plans. They do not move live KiCad objects. When authority, gate, and visual-review requirements are satisfied, `start_board_release_preview_job` and `start_board_release_finalize_job` run the maintained release workflow with exact evidence pins. Poll each job and inspect its result: worker completion alone is not design acceptance or a verified release. No production signing keys are installed by this tooling.

Long operations use typed durable `start_*_job` / `board_workflow_job_status` / `cancel_board_workflow_job` calls. Avoid retrying a timed-out start blindly; inspect the durable job journal and result first. The server does not provide arbitrary script execution, executable selection, or caller-controlled trust configuration.

`native_board_interaction_capability` reports the routes for footprint drag and board-edge moves: the `adaptive_*` tools (file level) and the Adaptive Editor KiCad plugin (in the PCB Editor, one undo step). Cursor-following on the KiCad canvas itself is reported unsupported (KiCad 10.0.5's IPC API does not render uncommitted changes).

## Clients without local stdio MCP

The optional BoardTooling checkout's `scripts/design_contract.py` exposes a limited JSON-output CLI: `draft <project> --profile <profile> --variant <variant>`, `evaluate <project> [--gate-report <path>] [--attestation <path> ...]`, and `plan-placement <project> <request_json>`. Launch it with an explicit installed Python executable and a hidden, redirected child process on Windows. Its exit code is `3` for an unaccepted result, including a blocked placement plan; parse the JSON result to distinguish policy rejection from a process failure. Host trust checks still apply. This CLI is a contract/evaluation subset, not the MCP durable job or release API. A client with no MCP support cannot use the full MCP workflow through this CLI alone.

The entry printed by setup is a connection template, not a promise that every AI application supports MCP or this configuration shape. Consult that client's MCP documentation and verify `initialize`, `tools/list`, and a read-only tool call before use.
