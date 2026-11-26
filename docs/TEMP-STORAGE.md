# Temporary storage and retained evidence

Scratch allocations belong to a call or an editor session. `cleanup_after_call`
releases its own directories on return and on exceptions; `temporary_owner`
keeps session scratch until the existing close/exit method. Nested calls and
concurrent calls have separate allocation stacks. Each directory has a live
Windows directory-handle lease so another server can recognize a long-running
owner, without adding marker files to exports or changing the empty-directory contract.

Coverage fixtures that return paths allocate inside their caller's scope.
Coverage entrypoints and session constructors provide those scopes. Explicit
caller-supplied working directories remain caller-owned.

The Windows owned-process runtime also keeps descendants in their parent's Job
Object. The old `SILENT_BREAKAWAY_OK` flag let descendants escape cancellation;
the existing workflow cancellation/shutdown tests caught two surviving test
children. Job inheritance now remains enabled, including through venv launchers,
so the recorded owner can stop its entire tree before scratch is released.

`kicad_cli_run` keeps failed or truncated-output diagnostics in the application's
`cli-evidence` data directory, or the caller's explicit evidence base. These are
durable receipts, not abandoned system-temp directories. Complete native streams
remain available through the returned `evidence_dir`. Successful calls remove
their evidence directory in `finally`; no CLI call allocates `kicad_cli_run_*`
under the system temp folder. Unexpected failures retain their diagnostic folder.

At server startup, scratch directories named `kicad-ai-tools-*` or
`kicad_cli_run_*` are eligible for removal only after 24 hours without filesystem
activity. Workflow journals are eligible after seven days only if their last
complete state is terminal and recorded owner/worker identities are no longer
live. These lifetimes are project requirements. Recent, running, locked,
unreadable and reparse-point trees are preserved. Windows Restart Manager checks
file holders; exclusive directory probes and normal deletion errors provide
additional protection. Cleanup never stops another process. Platforms without
an open-handle authority conservatively skip automatic stale deletion.

The desktop tripwire is a test-session monitor that fails the run if any window of the test's own processes reaches the user's desktop.

Tests in `tests/unit/utils/test_temp_cleanup.py` cover call failures, owner close,
CLI calls with success/failure, retained complete errors, locked files and
workflow retention. The ordinary configured pytest gate and desktop tripwire
remain unchanged. Live editor coverage is not necessary for these unit tests.
