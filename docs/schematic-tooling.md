# Schematic diagnostics and CLI jobs

`schematic_overlap_check(schematic_path)` exports a sheet to SVG and checks its
real text ink against wires, pins, bodies, group boxes and page edges. Violations
include millimetre coordinates and a fix hint. It preserves the strict reference
policy: 0.3 mm text clearance and 0.12 mm penetration tolerance.

`erc_summary(schematic_path)` runs severity-all ERC and deduplicates identical
rules/messages while retaining affected refs, nets, sheet paths, severities and
locations. Power and hierarchy issues have specific repair hints. Export and
execution failures remain failures.

For work longer than a tool call, use `job_start(command, input_path, options)`
and poll `job_status(job_id)`. For example, start `sch export netlist` with
`{"--format":"kicadsexpr"}`. The managed receipt names the output and complete
logs. Commands and options use the existing typed CLI registry; mutation approval
and verified backups remain required. CLI workers have no tool-call deadline.
Server shutdown retains the existing owned-process cleanup behavior.

Fixtures under `tests/fixtures` cover SVG ink, native ERC report syntax,
deduplication and failure receipts.
