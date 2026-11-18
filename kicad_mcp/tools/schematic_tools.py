"""Small, actionable schematic diagnostics and durable typed CLI jobs."""
from __future__ import annotations

from pathlib import Path
import re
import uuid

from kicad_mcp.tools.cli_tools import run_cli
from kicad_mcp.utils import schematic_overlap as overlap


def summarize_erc(report: str) -> dict:
    """Deduplicate KiCad report blocks without discarding affected items."""
    groups = {}
    blocks = []
    lines = []
    sheet = '/'
    for line in report.splitlines():
        heading = re.match(r'\*+ Sheet (.*)',line)
        if heading or re.match(r'^\[[^\]]+\]:',line):
            if lines:
                blocks.append((sheet,'\n'.join(lines)))
                lines = []
            if heading:
                sheet = heading[1].strip()
        if re.match(r'^\[[^\]]+\]:',line) or lines:
            lines.append(line)
    if lines:
        blocks.append((sheet,'\n'.join(lines)))
    for sheet,block in blocks:
        match = re.match(r'\[([^\]]+)\]:\s*(.*)', block)
        if not match:
            continue
        rule, message = match.groups()
        key = rule, message
        entry = groups.setdefault(key, dict(rule=rule, message=message, count=0,
                                            locations=[], refs=[], nets=[], sheets=[], severities=[], hint='Inspect the listed pins and their named nets.'))
        entry['count'] += 1
        entry['locations'].extend(re.findall(r'@\([^\n]+', block))
        entry['refs'] = sorted(set(entry['refs']) | set(re.findall(r'(?<!\w)#?[A-Z]+\d+\b', block)))
        entry['nets'] = sorted(set(entry['nets']) | set(re.findall(r"(?:Label|net) '([^']+)'",block)))
        entry['sheets'] = sorted(set(entry['sheets']) | {sheet} | set(re.findall(r'Sheet:\s*(\S+)', block)))
        entry['severities'] = sorted(set(entry['severities']) | set(re.findall(r'(?:Severity:\s*|;\s*)(error|warning)', block, flags=re.I)))
        if rule == 'power_pin_not_driven':
            entry['hint'] = 'Trace the supply source; add a flag only when an external supply actually drives it.'
        elif rule == 'pin_to_pin':
            entry['hint'] = 'Trace the connected pin types and fix the unintended connection or symbol declaration.'
        elif rule == 'hier_label_mismatch':
            entry['hint'] = 'Match the child hierarchical label with its parent sheet pin.'
    counts = re.search(r'Errors\s+(\d+).*Warnings\s+(\d+)', report, re.S)
    return dict(errors=int(counts[1]) if counts else None,
                warnings=int(counts[2]) if counts else None,
                violations=list(groups.values()))


def check_overlap(schematic_path: str, output_root: str) -> dict:
    sch = Path(schematic_path).resolve(strict=True)
    out = Path(output_root).resolve() / ('overlap-' + uuid.uuid4().hex)
    result = run_cli('sch export svg', str(sch), {}, str(out), timeout_seconds=None)
    if not result['ok']:
        return result
    svg = out / (sch.stem + '.svg')
    hits = overlap.check(sch.stem, overlap.svg_texts(svg), *overlap.geometry(sch), overlap.page_size(svg))
    return dict(ok=not hits, sheet=str(sch), clear_mm=overlap.CLEAR, tolerance_mm=overlap.TOL,
                violations=[dict(gate=kind, detail=detail, coordinates=list(box),
                                 hint='Move the affected ink or use a packed schematic group.')
                            for kind, detail, box in hits], svg_path=str(svg))


def register_schematic_tools(mcp, jobs):
    @mcp.tool()
    def svg_normalize_whitespace(svg_dir: str, allow_mutation: bool = False) -> dict:
        """Format native SVG export whitespace with exact backups and XML drawing/text checks.

        Does not edit the schematic, board or gates. Raster byte equivalence must
        still be verified before reusing an existing visual approval.
        """
        from kicad_mcp.utils.svg_exports import normalize
        return normalize(svg_dir,str(jobs.root),allow_mutation)

    @mcp.tool()
    def schematic_pcb_sync(schematic_path: str, board_path: str,
                           allow_mutation: bool = False, replace_design: bool = False) -> dict:
        """Preview or import a fresh schematic population and exact pad nets into an existing board.

        Preview reports library/pad mismatches. Explicit replace_design plus
        allow_mutation replaces obsolete footprints/copper, retaining mechanics
        and settings, with a verified backup and candidate connectivity readback.
        New parts are unplaced outside the outline; placement/DRC remain required.
        """
        from kicad_mcp.utils.pcb_schematic_sync import sync
        return sync(schematic_path,board_path,str(jobs.root),allow_mutation,replace_design,run_cli)

    @mcp.tool()
    def schematic_write_generated_bundle(spec_path: str, output_dir: str,
                                          allow_mutation: bool = False,
                                          remove_obsolete_sheets: bool = False) -> dict:
        """Author a generated flat schematic hierarchy from a JSON root/files bundle.

        Parses the candidate using KiCad netlist export before promotion, backs
        up replaced files and records hashes. No executable code is accepted.
        Does not waive ERC, overlap checks, visual review or PCB import checks.
        """
        from kicad_mcp.utils.generated_schematic import write_bundle
        return write_bundle(spec_path, output_dir, str(jobs.root), allow_mutation, run_cli,remove_obsolete_sheets)

    @mcp.tool()
    def schematic_readability_check(schematic_path: str) -> dict:
        """Audit every hierarchy sheet: A3 landscape, 1.27mm text, local labels, content extent and strict overlaps. Does not grant visual signoff."""
        from kicad_mcp.utils.schematic_readability import hierarchy, measure_sheet
        sch = Path(schematic_path).resolve(strict=True)
        sheets = list(hierarchy(sch))
        # Export each distinct source standalone, avoiding filename assumptions
        # about nested paths and preserving independently instantiated sheets.
        results = {}
        for path, tree in sheets:
            if str(path) in results:
                continue
            out = jobs.root / ('readability-' + uuid.uuid4().hex)
            result = run_cli('sch export svg', str(path),
                             {'--exclude-drawing-sheet': True}, str(out), timeout_seconds=None)
            if not result['ok']:
                return dict(ok=False, export_failure=result, sheets=list(results.values()))
            svg = out / (path.stem + '.svg')
            if not svg.is_file():
                return dict(ok=False, error=f'Expected source sheet export missing: {svg}', sheets=list(results.values()))
            results[str(path)] = measure_sheet(path, tree, svg)
        return dict(ok=all(r['structural_requirements_pass'] for r in results.values()),
                    sheet_instances=len(sheets), distinct_sheets=len(results),
                    sheets=list(results.values()), visual_signoff=False)

    @mcp.tool()
    def schematic_overlap_check(schematic_path: str) -> dict:
        """Check one sheet's real SVG ink; return strict violations with millimetre coordinates."""
        return check_overlap(schematic_path, str(jobs.root))

    @mcp.tool()
    def erc_summary(schematic_path: str) -> dict:
        """Run severity-all ERC and return deduplicated issues, affected refs, locations and hints."""
        report = jobs.root / ('erc-' + uuid.uuid4().hex + '.rpt')
        result = run_cli('sch erc', schematic_path, {'--severity-all': True}, str(report), timeout_seconds=None)
        if not result['ok']:
            return result
        summary = summarize_erc(report.read_text(encoding='utf-8'))
        return dict(ok=summary['errors'] == 0, report_path=str(report), **summary)

    @mcp.tool()
    def job_start(command: str, input_path: str | None = None,
                  options: dict | None = None, allow_mutation: bool = False) -> dict:
        """Queue a registry-validated kicad-cli command; no tool-call lifetime limit or free argv.

        Output and full receipts belong to the job directory. Mutations retain
        the CLI runner's explicit permission and verified-backup requirements.
        """
        return jobs.start('kicad_cli', dict(command=command, input_path=input_path,
                                          options=options or {}, allow_mutation=allow_mutation))

    @mcp.tool()
    def job_status(job_id: str) -> dict:
        """Read a durable CLI job receipt, including failure details and result paths."""
        return jobs.status(str(uuid.UUID(job_id)))
