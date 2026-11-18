"""docs/OPERATION-MATRIX.md (F01) must match what the registries and routes say right now."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _gen():
    spec = importlib.util.spec_from_file_location("gen_operation_matrix", ROOT / "scripts" / "gen_operation_matrix.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_operation_matrix_is_current():
    gen = _gen()
    assert (ROOT / "docs" / "OPERATION-MATRIX.md").read_text(encoding="utf-8") == gen.render(), \
        "run scripts/gen_operation_matrix.py"


def test_every_cli_command_and_domain_is_listed():
    gen = _gen()
    rows = gen.rows()
    from kicad_mcp.utils import cli_registry
    names = {c.name for c in cli_registry.list_commands()}
    listed = {r[1].split("`")[1].removeprefix("kicad-cli ") for r in rows if r[1].startswith("`kicad-cli ")}
    assert listed == names
    assert {r[0] for r in rows} == set(gen.DOMAINS)
    assert all(r[5] in ("supported", "callable_unverified", "blocked", "unsupported", "unavailable", "deprecated")
               for r in rows)
