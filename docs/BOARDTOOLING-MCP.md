# BoardTooling workflow tools (optional)

The workflow tools are registered by `kicad_mcp/workflow_server.py` with the rest of kicad-ai-tools. They
need a separately installed BoardTooling checkout, enabled per machine with
`python -m kicad_mcp.setup --boardtooling <dir>` (or `KICAD_AI_TOOLS_BOARDTOOLING_ROOT`). Without it,
they report "not configured".

## Runtime

The MCP client starts the launcher (see `docs/KICAD-MCP-CLIENTS.md`). The launcher starts the server on
demand inside an owned Windows job object, through the vendored `kicad_mcp/utils/winproc.py`. The job
starts the child suspended and assigns it before it executes.

Gate execution also needs PowerShell 7: it is found on PATH, or set `KICAD_AI_TOOLS_POWERSHELL`.

The release exporter runs under the server's interpreter (or `KICAD_AI_TOOLS_EXPORT_PYTHON`), which
needs `cryptography`. KiCad's bundled Python stays the native read-only geometry parser.

Release operations need `BOARDTOOLING_VISUAL_REVIEW_PUBLIC_KEY`, an independently managed Ed25519 PEM
public key file. The request cannot select this key. Shared contract-attestation trust configuration
also belongs to the protected server installation; client arguments cannot establish authority, and
this package installs no trust material.

## Registered tools

| Tool | Purpose |
| --- | --- |
| `boardtooling_capabilities` | Actual installed tool identities and explicit capability failures |
| `inspect_board_bundle` | Complete shared project snapshot and digest |
| `board_design_profiles` | Read the maintained profile and rule catalog |
| `create_board_contract_draft` | Author a draft on an exact bundle and selected native variant |
| `evaluate_board_contract` | Evaluate an exact revision with optional gate report and typed signed-attestation file paths |
| `plan_board_placement` | Submit exact footprint UUIDs and finite mm deltas to the shared planning API; it currently blocks native execution |
| `evaluate_board_gates` | Shared full gate sweep into a new evidence directory |
| `query_board_scene` | Read-only native board scene for an exact project revision |
| `board_release_preview` | Maintained exporter preview with explicit gate report and signed-attestation file paths |
| `board_release_finalize` | Finalize a matching preview with a visual receipt and a new attempt evidence directory |
| `native_board_interaction_capability` | Reports interactive footprint drag and edge shove as unsupported |

Durable equivalents are `start_board_bundle_job`,
`start_board_contract_draft_job`, `start_board_contract_evaluation_job`,
`start_board_placement_plan_job`, `start_board_gate_job`,
`start_board_scene_job`, `start_board_release_preview_job`, and
`start_board_release_finalize_job`. Each has typed operation arguments and
returns a job UUID promptly. `board_workflow_job_status` reads status and
results, including from a previous server session. `cancel_board_workflow_job`
can stop only work owned by the current server session. No generic command,
script path, or caller-selected trust key is exposed.
Clients should use these start tools for native gate, scene, and release work,
then poll status by job UUID. The synchronous tools remain for compatibility
and can outlive a client's default tool-call wait.

Project paths are explicit tool arguments. Gate and release calls require the
expected bundle digest; the adapter rechecks source identity and rejects
missing or mismatched evidence.
Evaluation accepts `attestation_paths` as a list of direct external evidence
files and `gate_report_path` for the exact native gate receipt. Preview requires
both, pins their hashes with the current host-configured
`BOARDTOOLING_TRUST_CONFIG`, and passes them to the shared exporter using
`--design-attestation`, `--design-gate-report`, and the host trust path.
Clients cannot select trust configuration or signing authority. Finalization
derives these paths from the preview binding and rechecks their hashes, host
trust and project revision before invoking the exporter. The exporter remains
the authority for its release binding and verified receipt.
Workflow release requires a selected design contract accepted by the shared
evaluator. Until independently configured attestation trust and the shared
evaluator actually establish that acceptance, release remains blocked.
The older exporter CLI can report a legacy path, but that is outside this
strict MCP release operation. A successful gate sweep is measurement evidence,
not whole-board design acceptance.

## Lifecycle and evidence

The client owns the GUI launcher. Closing its stdin ends the MCP stdio
session. The launcher waits for the workflow server and closes its exact
owned job; the workflow server also closes its own contained child jobs on
normal exit and shutdown. No process-name kill or age-based reaping is used.
Each asynchronous request has a unique journal under `KICAD_MCP_JOB_DIR`
(default: `<data dir>/workflow-jobs`; see [configuration](configuration.md)). It retains `request.json`,
the fsynced append-only `state-history.jsonl`, an atomic `state.json` mirror,
`result.json`, and full `stdout.txt`/`stderr.txt`. Status reads use the journal;
a Windows reader of the mirror cannot block or discard a committed state.
The mirror can lag while a reader prevents replacement; its publication error
is recorded in the journal. Journal reads decode each complete byte record
independently, so a cut inside a UTF-8 character leaves the prior state
readable; the next transition is separated from the damaged bytes. Gate and
release attempts use new child evidence paths inside that journal. A preview
returns its actual `preview_dir`, which is created by the maintained exporter.
Finalization, preview, and gate failures retain `adapter-status.json`,
`adapter-run.json` (including any `launch_error`), and complete adapter logs.
Before a native command starts, its attempt also records a pending adapter
receipt and exact request, bundle, variant, and selection identity. A contained
command writes `native-command/command.json`, `events.jsonl`, `stdout.bin`, and
`stderr.bin` as it runs; read-only scene jobs use a unique `native-commands/`
child instead. The files are opened before the suspended native child resumes,
and the child writes to them directly. This retains output already produced when cancellation or EOF
stops the worker before its adapter can write final logs. The parent adds its
own cleanup receipt with the worker identity, exit status, and retained byte
counts after stopping the contained tree. The event stream records launch,
root exit when observed, and owned-tree cleanup. If cleanup cannot
be verified, the job reports `cleanup_unverified` and does not claim a stopped
tree or successful operation.
States distinguish queued/running/cancelled/failed/completed worker execution;
`operation_ok` and `design_accepted` are separate. A completed gate measurement
or blocked placement plan is not board acceptance. When the client closes
the session, the launcher first waits for jobs started through that session (and for `start_*` calls
still in flight) to finish; only then does it stop the server. A server stopped any other way (for
example when it is run directly) cancels and reaps its queued/running jobs, preserving their journals. An
interrupted previous-session journal remains readable and cannot be adopted
for cancellation by another server session.
Failed evidence is not silently replaced or pruned.

For a read-only deployment check, run [check_workflow_transport.py](../tests/check_workflow_transport.py)
through a verified silent outer runner. It checks the outer PE GUI subsystem,
pipe probe, MCP initialize/list/call, typed asynchronous start/status/result,
missing-argument rejection, failure exit, typed cancellation and EOF while a
native scene child is running, and exact launcher and child termination. It writes a unique attempt under
`.tmp/mcp-transport-*`; copy that attempt to durable review evidence before
cleaning the task-owned scratch. The capability call does not touch a board.

## Current limits

This is a cooperative same-user API, not an OS authority boundary. The typed
contract tools call the shared `design_contract.py` implementation. Draft
authoring and evaluation do not claim independent design authority. Native
interactive footprint drag and edge shove are unavailable. The shared exporter
accepts an `edge_plating` skip only when native measurement proves zero
segments and no managed group, bound to the current bundle. The adapter also
requires the matching measurement evidence file. A configuration-only skip
is rejected. Numerical gates do not replace visual review or independent
design signoff.
