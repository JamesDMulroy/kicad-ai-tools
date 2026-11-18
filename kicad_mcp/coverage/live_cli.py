"""Live sweep of every kicad-cli command in an inventory, against a private copy of a test project.

Each command runs with a real invocation built from the copy's own files (schematic, board, project,
the project's symbol and footprint libraries) and passes only when it exits 0 AND produces what it
declares (an output file or directory, or output text). Inputs some commands need are made by earlier
commands in the same run: Gerbers from ``pcb export gerbers`` feed the gerber commands, a jobset is
written for ``jobset run``. A command whose input cannot be produced here (an import needs a design in
another tool's format) is reported ``needs_input`` with what is missing, never passed.

    python -m kicad_mcp.coverage.live_cli --inventory inv.json --project <dir> --cli <kicad-cli> --out ev.json
"""
from __future__ import annotations
from kicad_mcp.utils.temp_cleanup import scoped_mkdtemp, cleanup_after_call, temporary_owner

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

TIMEOUT_S = 300


@temporary_owner(cleanup='close', allocating=('__init__',))
class Ctx:
    def __init__(self, project: Path, cli: str, foreign: dict[str, Path]):
        self.cli = cli
        self.root = Path(scoped_mkdtemp(prefix="kicad-ai-tools-cli-"))
        self.work = self.root / "project"
        shutil.copytree(project, self.work, ignore=shutil.ignore_patterns("~*", "*.lck"))
        self.out = self.root / "out"
        self.out.mkdir()
        self.sch = next(self.work.glob("*.kicad_sch"))
        self.pcb = next(self.work.glob("*.kicad_pcb"))
        self.pro = next(self.work.glob("*.kicad_pro"))
        self.sym = next(self.work.glob("*.kicad_sym"), None)
        self.pretty = next(self.work.glob("*.pretty"), None)
        self.foreign = foreign  # format name -> a design file in that format, if one is available

    def o(self, name: str) -> str:
        return str(self.out / name)

    def copy(self, path: Path, suffix: str = "-copy") -> Path:
        """A copy outside the project folder, so no command sees it as part of the project."""
        target = self.root / "copies" / (path.stem + suffix + path.suffix)
        target.parent.mkdir(exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
        else:
            shutil.copy2(path, target)
        return target

    def gerber(self) -> Path:
        folder = self.out / "gerbers"
        if not folder.exists() or not any(folder.iterdir()):
            run(self, ["pcb", "export", "gerbers", "-o", str(folder) + "/", str(self.pcb)])
        files = sorted(folder.glob("*.g*"), key=lambda f: ("F_Cu" not in f.name, -f.stat().st_size))
        return files[0]  # the front copper layer: never an empty layer

    def footprint_name(self) -> str:
        return next(self.pretty.glob("*.kicad_mod")).stem

    def jobset(self) -> Path:
        """A one-job jobset (board drill export) in KiCad's jobset format."""
        path = self.work / "coverage.kicad_jobset"
        path.write_text(json.dumps({
            "jobs": [{"id": "00000000-0000-0000-0000-000000000001", "type": "pcb_export_drill",
                      "description": "coverage drill", "settings": {}}],
            "outputs": [{"id": "00000000-0000-0000-0000-000000000002", "type": "folder",
                         "only": [], "description": "coverage", "settings": {"output_path": str(self.out / "jobset")}}],
            "meta": {"version": 1}}, indent=1), encoding="utf-8")
        return path


def run(ctx: Ctx, args: list[str]) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.run([ctx.cli, *args], capture_output=True, text=True, timeout=TIMEOUT_S,
                          creationflags=flags, cwd=ctx.work)


# command -> (args builder, what must exist afterwards: a path, or None to require non-empty stdout)
Spec = tuple[Callable[[Ctx], list[str]], Callable[[Ctx], Path | None]]


def _export(sub: str, ext: str, *extra: str) -> Spec:
    return (lambda c: ["pcb", "export", sub, *extra, "-o", c.o(f"board.{ext}"), str(c.pcb)],
            lambda c: c.out / f"board.{ext}")


def _export_dir(kind: str, sub: str, *extra: str) -> Spec:
    source = (lambda c: c.pcb) if kind == "pcb" else (lambda c: c.sch)
    return (lambda c: [kind, "export", sub, *extra, "-o", c.o(f"{kind}-{sub}") + "/", str(source(c))],
            lambda c: c.out / f"{kind}-{sub}")


def _sch_file(sub: str, ext: str) -> Spec:
    return (lambda c: ["sch", "export", sub, "-o", c.o(f"sch.{ext}"), str(c.sch)], lambda c: c.out / f"sch.{ext}")


SPECS: dict[str, Spec] = {
    "version": (lambda c: ["version"], lambda c: None),
    "sch erc": (lambda c: ["sch", "erc", "-o", c.o("erc.rpt"), str(c.sch)], lambda c: c.out / "erc.rpt"),
    "pcb drc": (lambda c: ["pcb", "drc", "-o", c.o("drc.rpt"), str(c.pcb)], lambda c: c.out / "drc.rpt"),
    "sch diff": (lambda c: ["sch", "diff", "-o", c.o("sch.diff"), str(c.sch), str(c.copy(c.sch))],
                 lambda c: c.out / "sch.diff"),
    "pcb diff": (lambda c: ["pcb", "diff", "-o", c.o("pcb.diff"), str(c.pcb), str(c.copy(c.pcb))],
                 lambda c: c.out / "pcb.diff"),
    "sym diff": (lambda c: ["sym", "diff", "-o", c.o("sym.diff"), str(c.sym), str(c.copy(c.sym))],
                 lambda c: c.out / "sym.diff"),
    "fp diff": (lambda c: ["fp", "diff", "-o", c.o("fp.diff"), str(c.pretty), str(c.copy(c.pretty))],
                lambda c: c.out / "fp.diff"),
    "mergetool": (lambda c: ["mergetool", "--output", c.o("merged.kicad_pcb"), str(c.pcb), str(c.copy(c.pcb, "-a")),
                             str(c.copy(c.pcb, "-b"))], lambda c: c.out / "merged.kicad_pcb"),
    "sch upgrade": (lambda c: ["sch", "upgrade", "--force", str(c.copy(c.sch, "-up"))],
                    lambda c: c.root / "copies" / (c.sch.stem + "-up.kicad_sch")),
    "pcb upgrade": (lambda c: ["pcb", "upgrade", "--force", str(c.copy(c.pcb, "-up"))],
                    lambda c: c.root / "copies" / (c.pcb.stem + "-up.kicad_pcb")),
    "sym upgrade": (lambda c: ["sym", "upgrade", "--force", "--output", c.o("sym-up.kicad_sym"), str(c.sym)],
                    lambda c: c.out / "sym-up.kicad_sym"),
    "fp upgrade": (lambda c: ["fp", "upgrade", "--force", "--output", c.o("fp-up.pretty"), str(c.pretty)],
                   lambda c: c.out / "fp-up.pretty"),
    "sch downgrade": (lambda c: ["sch", "downgrade", "--force", "--output", c.o("sch-down"), str(c.sch)],
                      lambda c: c.out / "sch-down"),
    "pcb downgrade": (lambda c: ["pcb", "downgrade", "--force", "--output", c.o("pcb-down.kicad_pcb"), str(c.pcb)],
                      lambda c: c.out / "pcb-down.kicad_pcb"),
    "project downgrade": (lambda c: ["project", "downgrade", "--force", "--output", c.o("project-down"), str(c.pro)],
                          lambda c: c.out / "project-down"),
    "sym export svg": (lambda c: ["sym", "export", "svg", "-o", c.o("sym-svg"), str(c.sym)], lambda c: c.out / "sym-svg"),
    "fp export svg": (lambda c: ["fp", "export", "svg", "-o", _mkdir(c.out / "fp-svg"), "--footprint", c.footprint_name(),
                                 str(c.pretty)], lambda c: c.out / "fp-svg"),
    "gerber info": (lambda c: ["gerber", "info", str(c.gerber())], lambda c: None),
    "gerber convert png": (lambda c: ["gerber", "convert", "png", "-o", c.o("gerber.png"), str(c.gerber())],
                           lambda c: c.out / "gerber.png"),
    "gerber diff": (lambda c: ["gerber", "diff", "-o", c.o("gerber-diff.png"), str(c.gerber()), str(c.gerber())],
                    lambda c: c.out / "gerber-diff.png"),
    "jobset run": (lambda c: ["jobset", "run", "--file", str(c.jobset()), str(c.pro)], lambda c: c.out / "jobset"),
    "pcb render": (lambda c: ["pcb", "render", "-o", c.o("render.png"), "--width", "400", "--height", "300",
                              str(c.pcb)], lambda c: c.out / "render.png"),
    "pcb export gerbers": _export_dir("pcb", "gerbers"),
    "pcb export drill": _export_dir("pcb", "drill"),
    "pcb export dxf": _export_dir("pcb", "dxf", "--layers", "F.Cu,Edge.Cuts"),
    "pcb export ps": _export_dir("pcb", "ps", "--layers", "F.Cu,Edge.Cuts"),
    "pcb export hpgl": _export_dir("pcb", "hpgl", "--layers", "F.Cu,Edge.Cuts"),
    "pcb export svg": _export("svg", "svg", "--layers", "F.Cu,Edge.Cuts"),
    "pcb export pdf": _export("pdf", "pdf", "--layers", "F.Cu,Edge.Cuts"),
    "pcb export png": _export("png", "png", "--layers", "F.Cu,Edge.Cuts"),
    "sch export svg": _export_dir("sch", "svg"),
    "sch export dxf": _export_dir("sch", "dxf"),
    "sch export ps": _export_dir("sch", "ps"),
    "sch export hpgl": _export_dir("sch", "hpgl"),
    "sch export pdf": _sch_file("pdf", "pdf"),
    "sch export png": _export_dir("sch", "png"),
    "sch export bom": _sch_file("bom", "csv"),
    "sch export netlist": _sch_file("netlist", "net"),
    "sch export python-bom": _sch_file("python-bom", "xml"),
    "api-server": (None, None),  # proven by every HeadlessSession of the API sweep (see sweep())
}
for _sub, _ext in {"3dpdf": "pdf", "brep": "brep", "glb": "glb", "ply": "ply", "step": "step", "stl": "stl",
                   "stpz": "stpz", "u3d": "u3d", "vrml": "wrl", "xao": "xao"}.items():
    SPECS[f"pcb export {_sub}"] = _export(_sub, _ext, "--force")
for _sub, _ext in {"bom": "csv", "gencad": "cad", "idf": "emn", "ipc2581": "xml", "ipcd356": "d356",
                   "odb": "zip", "pos": "pos", "stackup": "txt", "stats": "txt"}.items():
    SPECS[f"pcb export {_sub}"] = _export(_sub, _ext)

def _mkdir(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


DIFF_COMMANDS = {"sch diff", "pcb diff", "sym diff", "fp diff", "gerber diff"}  # exit 5 = "differences found"
REMOVED_MARKERS = ("no longer supported",)  # KiCad lists the command but refuses it by design

IMPORTS = {"import": "any", "pcb import": "board", "sch import": "schematic"}


def _import_spec(command: str, ctx: Ctx) -> tuple[list[str], Path] | None:
    kind = IMPORTS[command]
    source = ctx.foreign.get(kind) or (ctx.foreign.get("board") if kind == "any" else None)
    if not source:
        return None
    if command == "import":
        return ["import", "--output", ctx.o("imported.kicad_pro"), str(source)], ctx.out / "imported.kicad_pro"
    ext = ".kicad_pcb" if kind == "board" else ".kicad_sch"
    words = command.split()
    return [*words, "-o", ctx.o("imported" + ext), str(source)], ctx.out / ("imported" + ext)


def check(ctx: Ctx, command: str) -> tuple[str, str]:
    if command in IMPORTS:
        spec = _import_spec(command, ctx)
        if spec is None:
            return "needs_input", f"no {IMPORTS[command]} design in another tool's format is available"
        args, expected = spec
    elif command in SPECS and SPECS[command][0] is not None:
        build, expect = SPECS[command]
        args, expected = build(ctx), expect(ctx)
    else:
        return "no_spec", "no invocation defined for this command yet"
    try:
        result = run(ctx, args)
    except subprocess.TimeoutExpired:
        return "error", f"timed out after {TIMEOUT_S} s"
    tail = (result.stderr or result.stdout).strip().splitlines()[-1:] or [""]
    text = (result.stderr + result.stdout).lower()
    if result.returncode != 0 and any(marker in text for marker in REMOVED_MARKERS):
        return "removed_upstream", f"KiCad lists it but refuses it: {tail[0][:160]}"
    if result.returncode != 0 and not (command in DIFF_COMMANDS and result.returncode == 5):
        return "error", f"exit {result.returncode}: {tail[0][:200]}"
    if expected is None:
        return ("ok", result.stdout.strip()[:80]) if result.stdout.strip() else ("error", "no output text")
    if not expected.exists() or (expected.is_dir() and not any(expected.iterdir())) \
            or (expected.is_file() and expected.stat().st_size == 0):
        return "error", f"exit 0 but {expected.name} was not produced"
    return "ok", expected.name


@cleanup_after_call
def sweep(inventory: dict, project: Path, cli: str, foreign: dict[str, Path], api_evidence: dict | None) -> dict:
    ctx = Ctx(project, cli, foreign)
    rows = []
    try:
        for command in inventory["cli_commands"]:
            if command == "api-server":
                proven = bool(api_evidence and api_evidence.get("passed"))
                rows.append({"command": command, "outcome": "ok" if proven else "needs_input",
                             "detail": "served every API sweep session" if proven else "run the API sweep first"})
                continue
            outcome, detail = check(ctx, command)
            rows.append({"command": command, "outcome": outcome, "detail": detail})
    finally:
        ctx.close()
    passed = sorted(f"cli:{r['command']}" for r in rows if r["outcome"] == "ok")
    gaps = sorted(f"cli:{r['command']}" for r in rows if r["outcome"] == "removed_upstream")
    return {"commit": inventory["source_commit"], "kicad_version": inventory["kicad_version"],
            "when": time.strftime("%Y-%m-%dT%H:%M:%S"), "passed": passed, "upstream_gaps": gaps, "rows": rows}


@cleanup_after_call
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--cli", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--api-evidence", type=Path, help="the API sweep's evidence (proves api-server)")
    parser.add_argument("--foreign-board", type=Path, help="a board in another tool's format, for imports")
    parser.add_argument("--foreign-schematic", type=Path, help="a schematic in another tool's format")
    args = parser.parse_args(argv)
    foreign = {k: v for k, v in (("board", args.foreign_board), ("schematic", args.foreign_schematic)) if v}
    api_evidence = json.loads(args.api_evidence.read_text(encoding="utf-8")) if args.api_evidence else None
    result = sweep(json.loads(args.inventory.read_text(encoding="utf-8")), args.project, args.cli, foreign,
                   api_evidence)
    args.out.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
