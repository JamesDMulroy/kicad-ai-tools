# Security

## Scope
kicad-ai-tools is a local developer tool. It runs as the user, on the user's machine, against the
user's KiCad projects. It opens no network listener. The only network access is:
- reading public KiCad source from GitLab (L2);
- KiCad building the plugin's Python environment from PyPI.

## Safety properties

**Silent operation.** Nothing appears on the user's screen.
- Child processes are windowless.
- GUI work (L4, live tests) runs on a private Win32 desktop, with a tripwire that kills the process
  tree if any window reaches the user's desktop.

**Processes are stopped by exact handle, never by name or pattern.** Each session owns its process tree
in a job object. On close it stops a process that escaped the job only if all three hold, each read
through the handle that then terminates it: the executable lies under the session's KiCad installation,
the process was created after the session began, and its command line names one of the session's own
uniquely named folders. Only that process is stopped, not a tree.

**AI-callable tools are typed and validated before anything runs.**
- No tool runs a shell or arbitrary code, with one opt-in exception (`kicad_run_bom_generator`, below).
- Mutating tools need an explicit flag or a sha256 guard, and keep a backup.
- L4 acts only on a clone of a project, only through operations in a recorded registry whose bytes are
  approved by sha256.

**Tool output is data.** Source text, plugin files and board contents are returned as data, never as
instructions.

**No credentials.** The package stores or reads no secrets.

**Opt-in for external programs.** `kicad_run_bom_generator` runs the external BOM generator program a
client names, like KiCad's own BOM dialog. Because the client (an AI) chooses its `allow_execute` argument,
that flag alone is not consent: the tool also refuses unless the person running the server sets
`KICAD_AI_TOOLS_ALLOW_BOM_GENERATOR=1`. Leave it unset unless you need it.

**Configured code is trusted.** The optional BoardTooling adapter imports and runs Python modules from
the BoardTooling root the operator configures. Point it only at code you trust.

**Developer-only drivers.** The live coverage drivers under `kicad_mcp/coverage` (Frida hooks into the
test's own KiCad process, cross-process memory writes) ship in the source distribution only. The wheel
keeps just the coverage modules the server imports. The drivers act only on KiCad processes the test
itself launched.

**Paths are the caller's.** Tools read and write the paths a client passes, with the permissions of the
user who runs the server. Treat the MCP client as able to read your KiCad projects. Run the server under
an account whose files you are willing to expose to the assistant you connect.

## Supported versions
Security fixes go to the latest release on the default branch.

## Reporting a vulnerability
Please report vulnerabilities privately by email to **hello@jamesmulroy.com**. Do not open a public
issue. Include:
- the tool name and the inputs;
- what happened and what you expected;
- any evidence or log file, with personal paths removed.

You should get an acknowledgement within 7 days. A fix or mitigation plan follows once the report is
confirmed. Coordinated disclosure is appreciated; credit is given unless you prefer otherwise.
