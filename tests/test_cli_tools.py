"""Parsing and validation tests for the kicad-cli 10.0.5 command registry.

Pure unit tests: they read the captured help tree and never run kicad-cli.
"""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from kicad_mcp.utils import cli_registry
from kicad_mcp.utils.cli_registry import (
    CliRegistryError,
    Command,
    get_command,
    list_commands,
    validate_options,
)

# The captured help gives pcb 27 leaves: drc, 23 exports, import, render, upgrade.
EXPECTED_GROUP_COUNTS = {"fp": 2, "jobset": 1, "pcb": 27, "sch": 10, "sym": 2, "version": 1}
MUTATING = {"fp upgrade", "sym upgrade", "sch upgrade", "pcb upgrade", "pcb import"}


def opt(command: str, long: str):
    for option in get_command(command).options:
        if option.long == long:
            return option
    raise AssertionError(f"{command} has no option {long}")


def code_of(command, options) -> str:
    with pytest.raises(CliRegistryError) as caught:
        validate_options(command, options)
    return caught.value.code


# --- parsing ----------------------------------------------------------------------------------


def test_exactly_43_leaf_commands():
    commands = list_commands()
    assert len(commands) == cli_registry.EXPECTED_LEAF_COUNT == 43
    assert len({c.name for c in commands}) == 43
    assert all(isinstance(c, Command) for c in commands)


def test_leaves_match_the_captured_tree():
    tree = json.loads(cli_registry.TREE_PATH.read_text(encoding="utf-8-sig"))
    leaves = {k for k, v in tree.items() if k != "(root)" and not v["subcommands"]}
    assert leaves == {c.name for c in list_commands()}


def test_group_counts():
    counts = Counter(c.group for c in list_commands())
    assert dict(counts) == EXPECTED_GROUP_COUNTS
    assert sum(1 for c in list_commands() if c.name.startswith("pcb export ")) == 23
    assert sum(1 for c in list_commands() if c.name.startswith("sch export ")) == 8


def test_named_leaves_exist():
    names = {c.name for c in list_commands()}
    for expected in ("fp export svg", "fp upgrade", "jobset run", "pcb drc", "pcb import",
                     "pcb render", "pcb upgrade", "sch erc", "sch upgrade", "sym export svg",
                     "sym upgrade", "version", "pcb export hpgl", "sch export python-bom",
                     "pcb export 3dpdf"):
        assert expected in names


def test_wrong_leaf_count_fails_loudly():
    tree = json.loads(cli_registry.TREE_PATH.read_text(encoding="utf-8-sig"))
    broken = copy.deepcopy(tree)
    del broken["version"]
    broken["(root)"]["subcommands"].remove("version")
    with pytest.raises(CliRegistryError, match="43"):
        cli_registry._parse_tree(broken)


def test_unparseable_help_fails_loudly():
    with pytest.raises(CliRegistryError):
        cli_registry._parse_command("x y", "not a help text")


def test_command_shape():
    command = get_command("pcb export pos")
    assert command.name == "pcb export pos"
    assert command.argv_prefix == ["pcb", "export", "pos"]
    assert command.summary == "Generate Position File"
    assert command.positional.name == "INPUT_FILE"
    assert command.positional.kind == "file"
    assert command.classification == "read_only"


def test_get_command_normalizes_names():
    assert get_command("PCB  export   pos") is get_command("pcb export pos")
    assert get_command(["pcb", "export", "pos"]) is get_command("pcb export pos")
    assert get_command("sch export python_bom") is get_command("sch export python-bom")


@pytest.mark.parametrize("name", ["", "pcb", "pcb export", "pcb export nope", "kicad-cli", "(root)"])
def test_get_command_rejects_non_leaves(name):
    with pytest.raises(CliRegistryError) as caught:
        get_command(name)
    assert caught.value.code == "unknown_command"


def test_positional_kinds():
    assert get_command("version").positional.name is None
    assert get_command("version").positional.kind == "none"
    for name in ("fp upgrade", "sym upgrade", "fp export svg"):
        assert get_command(name).positional.name == "INPUT_FILE_OR_DIR"
        assert get_command(name).positional.kind == "file_or_dir"
    assert get_command("pcb drc").positional.kind == "file"
    assert get_command("jobset run").positional.name == "INPUT_FILE"


def test_output_kinds():
    assert get_command("pcb export step").output_kind == "file"
    assert get_command("pcb drc").output_kind == "file"
    assert get_command("pcb import").output_kind == "file"
    assert get_command("pcb export gerbers").output_kind == "dir"
    assert get_command("pcb export drill").output_kind == "dir"
    assert get_command("fp upgrade").output_kind == "dir"
    assert get_command("sch export svg").output_kind == "dir"
    assert get_command("sch export pdf").output_kind == "file"
    assert get_command("sym upgrade").output_kind == "file_or_dir"
    assert get_command("version").output_kind is None
    assert get_command("pcb upgrade").output_kind is None
    assert get_command("jobset run").output_kind is None  # --output names a job output, not a path


def test_help_is_never_an_option_and_dash_h_is_not_a_short_flag():
    for command in list_commands():
        for option in command.options:
            assert option.long != "--help"
            assert "--help" not in option.aliases
            assert option.short != "-h"
            assert option.metavar is not None or not option.takes_value


def test_render_height_keeps_no_short_flag():
    height = opt("pcb render", "--height")
    assert height.short is None
    assert height.value_type == "int"
    assert height.default == 900
    width = opt("pcb render", "--width")
    assert width.short == "-w"
    assert width.default == 1600


def test_gencad_leading_space_options_are_parsed():
    longs = [o.long for o in get_command("pcb export gencad").options]
    assert longs == ["--output", "--define-var", "--flip-bottom-pads", "--unique-pins",
                     "--unique-footprints", "--use-drill-origin", "--store-origin-coord"]
    assert opt("pcb export gencad", "--flip-bottom-pads").short == "-f"
    unique = opt("pcb export gencad", "--unique-pins")
    assert unique.takes_value is False
    assert unique.help == "Generate unique pin names"


def test_every_usage_flag_is_a_parsed_option():
    for command in list_commands():
        usage_longs = {f.group(1) for f in cli_registry._USAGE_OPTION_RE.finditer(command.usage)}
        usage_longs.discard("--help")
        assert usage_longs == {o.long for o in command.options}, command.name


def test_option_order_of_a_small_command():
    assert [o.long for o in get_command("pcb export pos").options] == [
        "--output", "--side", "--format", "--units", "--bottom-negate-x",
        "--use-drill-file-origin", "--smd-only", "--exclude-fp-th", "--exclude-dnp",
        "--gerber-board-edge", "--variant"]
    assert [o.long for o in get_command("version").options] == ["--format"]
    assert [o.long for o in get_command("pcb upgrade").options] == ["--force"]
    assert [o.long for o in get_command("fp upgrade").options] == ["--output", "--force"]


def test_aliases_and_short_flags():
    footprint = opt("fp export svg", "--footprint")
    assert footprint.aliases == ("--fp",)
    assert footprint.short is None
    assert opt("fp export svg", "--sketch-pads-on-fab-layers").aliases == ("--sp",)
    assert opt("fp export svg", "--hide-DNP-footprints-on-fab-layers").aliases == ("--hdnp",)
    assert opt("pcb export dxf", "--common-layers").aliases == ("--cl",)
    assert opt("pcb export dxf", "--output-units").aliases == ("--ou",)
    assert opt("fp export svg", "--layers").short == "-l"
    assert opt("fp export svg", "--output").short == "-o"
    assert opt("pcb export ps", "--track-width-correction").short == "-C"


def test_repeatable_options_and_continuation_lines():
    define = opt("pcb drc", "--define-var")
    assert define.short == "-D"
    assert define.repeatable and define.takes_value
    assert define.default == []
    assert "'--define-var key=value'" in define.help
    variant = opt("pcb export pos", "--variant")
    assert variant.repeatable and variant.takes_value
    assert "${VARIANT}" in variant.help
    assert "default variant is output" in variant.help
    assert "[nargs" not in variant.help and "[may be repeated]" not in variant.help
    assert not opt("pcb drc", "--output").repeatable


def test_value_options_versus_flags():
    assert opt("pcb drc", "--save-board").takes_value is False
    assert opt("pcb drc", "--save-board").value_type == "bool"
    assert opt("pcb drc", "--save-board").default is None
    assert opt("pcb drc", "--output").takes_value is True
    assert opt("pcb drc", "--output").default == ""
    assert opt("pcb drc", "--format").default == "report"


def test_value_types_and_defaults():
    assert opt("pcb export gerbers", "--precision").value_type == "int"
    assert opt("pcb export gerbers", "--precision").default == 6
    assert opt("pcb export svg", "--drill-shape-opt").value_type == "int"
    assert opt("pcb export svg", "--scale").value_type == "float"
    assert opt("pcb export svg", "--scale").default == 1.0
    assert opt("pcb render", "--zoom").value_type == "float"
    assert opt("pcb render", "--light-side-elevation").default == 60.0
    assert opt("sch export hpgl", "--pen-size").default == 0.5
    assert opt("sch export bom", "--sort-asc").value_type == "bool"
    assert opt("sch export bom", "--sort-asc").takes_value is True
    assert opt("sch export bom", "--sort-asc").default is True
    assert opt("sch export bom", "--string-delimiter").default == '"'
    assert opt("pcb export step", "--min-distance").default == "0.01mm"
    assert opt("pcb export step", "--output").value_type == "string"


def test_deprecation_flags():
    assert get_command("pcb export hpgl").deprecated is True
    assert get_command("sch export hpgl").deprecated is False
    assert [c.name for c in list_commands() if c.deprecated] == ["pcb export hpgl"]
    assert opt("sch export hpgl", "--pen-size").deprecated is True
    assert opt("sch export bom", "--include-excluded-from-bom").deprecated is True
    assert opt("sch export bom", "--exclude-dnp").deprecated is False


def test_choices_where_listed():
    assert opt("pcb export pos", "--format").choices == ("ascii", "csv", "gerber")
    assert opt("pcb export pos", "--side").choices == ("front", "back", "both")
    assert opt("pcb export pos", "--units").choices == ("in", "mm")
    assert opt("version", "--format").choices == ("plain", "commit", "about")
    assert opt("pcb drc", "--units").choices == ("in", "mm", "mils")
    assert opt("pcb drc", "--format").choices == ("json", "report")
    assert opt("sch erc", "--units").choices == ("in", "mm", "mils")
    netlist = opt("sch export netlist", "--format").choices
    assert netlist == ("kicadsexpr", "kicadxml", "cadstar", "orcadpcb2", "spice", "spicemodel",
                       "pads", "allegro")
    assert len(netlist) == 8
    assert opt("pcb import", "--format").choices == (
        "auto", "pads", "altium", "eagle", "cadstar", "fabmaster", "pcad", "solidworks")
    assert opt("pcb import", "--report-format").choices == ("none", "json", "text")
    assert opt("pcb export drill", "--format").choices == ("excellon", "gerber")
    assert opt("pcb export drill", "--drill-origin").choices == ("absolute", "plot")
    assert opt("pcb export drill", "--excellon-zeros-format").choices == (
        "decimal", "suppressleading", "suppresstrailing", "keep")
    assert opt("pcb export drill", "--excellon-oval-format").choices == ("route", "alternate")
    assert opt("pcb export drill", "--map-format").choices == ("pdf", "gerberx2", "ps", "dxf", "svg")
    assert opt("pcb export vrml", "--units").choices == ("mm", "m", "in", "tenths")
    assert opt("pcb export dxf", "--output-units").choices == ("mm", "in")
    assert opt("pcb export gerbers", "--precision").choices == ("5", "6")
    assert opt("pcb render", "--side").choices == ("top", "bottom", "left", "right", "front", "back")
    assert opt("pcb render", "--quality").choices == ("basic", "high", "user", "job_settings")
    assert opt("pcb render", "--background").choices == ("default", "transparent", "opaque")


def test_no_choices_invented():
    assert opt("pcb render", "--preset").choices is None  # also accepts user-defined names
    assert opt("pcb export ipc2581", "--version").choices is None
    assert opt("pcb export ipc2581", "--units").choices is None
    assert opt("pcb export odb", "--compression").choices is None
    assert opt("pcb export pdf", "--bg-color").choices is None
    assert opt("pcb export drill", "--gerber-precision").choices is None
    assert opt("sch export bom", "--sort-asc").choices is None
    assert opt("pcb render", "--pan").choices is None
    assert opt("pcb render", "--light-top").choices is None
    for command in list_commands():
        for option in command.options:
            if not option.takes_value:
                assert option.choices is None


def test_mutating_classification():
    assert {c.name for c in list_commands() if c.mutating} == MUTATING
    for command in list_commands():
        expected = "mutating" if command.name in MUTATING else "read_only"
        assert command.classification == expected, command.name


def test_mutating_options_flag_writes_to_input():
    assert get_command("pcb drc").mutating_options == ("--save-board",)
    assert opt("pcb drc", "--save-board").writes_input is True
    assert get_command("pcb drc").may_write_input is True
    assert get_command("pcb drc").mutating is False
    for command in list_commands():
        if command.name != "pcb drc":
            assert command.mutating_options == (), command.name
        flagged = [o.long for o in command.options if o.writes_input]
        assert tuple(flagged) == command.mutating_options
    assert get_command("pcb upgrade").may_write_input is True
    assert get_command("pcb export step").may_write_input is False


def test_to_dict_is_json_serializable():
    payload = json.dumps([c.to_dict() for c in list_commands()])
    assert "pcb export pos" in payload


# --- validation: accepted input ---------------------------------------------------------------


def test_validate_normalizes_to_declaration_order():
    tokens = validate_options("pcb export pos", {
        "variant": ["a", "b"], "smd-only": True, "units": "mm", "format": "csv"})
    assert tokens == ["--format", "csv", "--units", "mm", "--smd-only",
                      "--variant", "a", "--variant", "b"]


def test_validate_accepts_command_objects_and_empty_options():
    command = get_command("pcb upgrade")
    assert validate_options(command, {}) == []
    assert validate_options(command, None) == []
    assert validate_options(command, {"force": True}) == ["--force"]
    assert validate_options(command, {"force": False}) == []
    assert validate_options(command, {"force": None}) == []


@pytest.mark.parametrize("key", ["footprint", "--footprint", "fp", "--fp", "-fp"])
def test_validate_accepts_long_and_alias_names(key):
    assert validate_options("fp export svg", {key: "R_0603"}) == ["--footprint", "R_0603"]


@pytest.mark.parametrize("key", ["o", "-o", "output", "--output"])
def test_validate_accepts_short_and_long_with_or_without_dashes(key):
    assert validate_options("fp export svg", {key: "out"}) == ["--output", "out"]


def test_validate_accepts_underscores_for_hyphens():
    assert validate_options("fp export svg", {"sketch_pads_on_fab_layers": True}) == [
        "--sketch-pads-on-fab-layers"]
    assert validate_options("fp export svg", {"hide_DNP_footprints_on_fab_layers": True}) == [
        "--hide-DNP-footprints-on-fab-layers"]


def test_validate_every_spelling_of_every_option():
    for command in list_commands():
        for option in command.options:
            if not option.takes_value:
                sample, expected = True, [option.long]
            elif option.choices:
                sample = option.choices[0]
                expected = [option.long, option.choices[0]]
            elif option.long == "--define-var":
                sample, expected = "A=1", [option.long, "A=1"]
            elif option.value_type == "int":
                sample, expected = 3, [option.long, "3"]
            elif option.value_type == "float":
                sample, expected = 1.5, [option.long, "1.5"]
            elif option.value_type == "bool":
                sample, expected = True, [option.long, "true"]
            else:
                sample, expected = "x", [option.long, "x"]
            for spelling in option.spellings:
                for key in (spelling, spelling.lstrip("-")):
                    assert validate_options(command, {key: sample}) == expected, (
                        command.name, key)


def test_validate_value_normalization():
    assert validate_options("pcb export gerbers", {"precision": 5}) == ["--precision", "5"]
    assert validate_options("pcb export gerbers", {"precision": "5"}) == ["--precision", "5"]
    assert validate_options("pcb render", {"width": 1200, "height": "900"}) == [
        "--width", "1200", "--height", "900"]
    assert validate_options("pcb render", {"zoom": 2}) == ["--zoom", "2"]
    assert validate_options("pcb render", {"zoom": 0.5}) == ["--zoom", "0.5"]
    assert validate_options("pcb render", {"zoom": "2"}) == ["--zoom", "2.0"]
    assert validate_options("sch export bom", {"sort-asc": False}) == ["--sort-asc", "false"]
    assert validate_options("sch export bom", {"sort-asc": "TRUE"}) == ["--sort-asc", "true"]
    assert validate_options("pcb drc", {"define-var": "A=1"}) == ["--define-var", "A=1"]
    assert validate_options("pcb drc", {"define-var": ["A=1", "B=-2"]}) == [
        "--define-var", "A=1", "--define-var", "B=-2"]
    assert validate_options("pcb drc", {"define-var": []}) == []
    assert validate_options("pcb drc", {"units": "mils", "format": "json"}) == [
        "--format", "json", "--units", "mils"]


# --- validation: rejected input ---------------------------------------------------------------


@pytest.mark.parametrize("key", ["nope", "--nope", "help", "--help", "h", "-h", "force-everything",
                                 "output=x", "---output", "", "--", "-"])
def test_unknown_options_are_rejected(key):
    assert code_of("pcb drc", {key: True}) == "unknown_option"


def test_non_string_keys_and_non_mappings_are_rejected():
    assert code_of("pcb drc", {1: True}) == "unknown_option"
    assert code_of("pcb drc", ["--save-board"]) == "wrong_type"


def test_dash_h_is_not_height_on_render():
    assert code_of("pcb render", {"-h": 5}) == "unknown_option"
    assert code_of("pcb render", {"h": 5}) == "unknown_option"
    assert validate_options("pcb render", {"height": 5}) == ["--height", "5"]


def test_options_of_another_command_are_unknown():
    assert code_of("pcb drc", {"smd-only": True}) == "unknown_option"
    assert code_of("version", {"output": "x"}) == "unknown_option"
    assert code_of("pcb upgrade", {"output": "x"}) == "unknown_option"


def test_unknown_command_is_rejected():
    assert code_of("pcb export nope", {}) == "unknown_command"
    assert code_of("pcb", {}) == "unknown_command"


def test_duplicate_spellings_are_rejected():
    assert code_of("fp export svg", {"fp": "a", "footprint": "b"}) == "duplicate_option"
    assert code_of("fp export svg", {"o": "a", "--output": "b"}) == "duplicate_option"


@pytest.mark.parametrize("command,options", [
    ("pcb drc", {"units": "cm"}),
    ("pcb drc", {"format": "xml"}),
    ("pcb drc", {"units": "MM"}),
    ("pcb export pos", {"format": "pdf"}),
    ("pcb export pos", {"side": "top"}),
    ("sch export netlist", {"format": "kicad"}),
    ("version", {"format": "json"}),
    ("pcb render", {"side": "up"}),
    ("pcb export gerbers", {"precision": 7}),
])
def test_bad_choices_are_rejected(command, options):
    assert code_of(command, options) == "invalid_choice"


@pytest.mark.parametrize("command,options", [
    ("pcb render", {"width": "abc"}),
    ("pcb render", {"width": 1.5}),
    ("pcb render", {"width": True}),
    ("pcb render", {"width": "1.5"}),
    ("pcb export gerbers", {"precision": "five"}),
    ("pcb render", {"zoom": "fast"}),
    ("pcb render", {"zoom": True}),
    ("pcb render", {"zoom": float("nan")}),
    ("pcb render", {"zoom": float("inf")}),
    ("pcb drc", {"output": 5}),
    ("pcb drc", {"output": True}),
    ("pcb drc", {"output": b"x"}),
    ("sch export bom", {"sort-asc": "maybe"}),
    ("sch export bom", {"sort-asc": 1}),
    ("pcb drc", {"define-var": {"A": "1"}}),
    ("pcb drc", {"define-var": ["A=1", None]}),
])
def test_wrong_types_are_rejected(command, options):
    assert code_of(command, options) == "wrong_type"


@pytest.mark.parametrize("value", [["a.kicad_pcb"], ("a",), {"a"}, {"k": "v"}])
def test_non_repeatable_options_reject_lists(value):
    assert code_of("pcb drc", {"output": value}) == "not_repeatable"


def test_non_repeatable_layers_list_is_rejected():
    assert code_of("pcb export svg", {"layers": ["F.Cu", "B.Cu"]}) == "not_repeatable"


@pytest.mark.parametrize("value", ["yes", "true", 1, 0, "", "--save-board", ["x"]])
def test_booleans_given_a_value_are_rejected(value):
    assert code_of("pcb drc", {"save-board": value}) == "bool_with_value"


@pytest.mark.parametrize("command,options", [
    ("pcb drc", {"output": "-evil"}),
    ("pcb drc", {"output": "--force"}),
    ("pcb drc", {"output": " -x"}),
    ("pcb drc", {"output": "-"}),
    ("pcb render", {"rotate": "-45,0,45"}),
    ("pcb render", {"pan": "-1,0,0"}),
    ("pcb render", {"width": -5}),
    ("pcb render", {"width": "-5"}),
    ("pcb render", {"zoom": -0.5}),
    ("pcb export ps", {"track-width-correction": -0.1}),
    ("pcb export pos", {"variant": ["ok", "-bad"]}),
    ("pcb export svg", {"layers": "-F.Cu"}),
])
def test_values_starting_with_dash_are_rejected(command, options):
    assert code_of(command, options) == "leading_dash"


@pytest.mark.parametrize("value", ["a\x00b", "a\nb", "a\rb", "x\n--force"])
def test_control_characters_are_rejected(value):
    assert code_of("pcb drc", {"output": value}) == "invalid_value"


def test_oversized_values_are_rejected():
    assert code_of("pcb drc", {"output": "x" * (cli_registry.MAX_VALUE_CHARS + 1)}) == "invalid_value"


@pytest.mark.parametrize("value", ["novalue", "=1", "1A=1", "A B=1", "A=1\n"])
def test_define_var_requires_key_equals_value(value):
    assert code_of("pcb drc", {"define-var": value}) == "invalid_value"


def test_error_carries_option_and_command():
    with pytest.raises(CliRegistryError) as caught:
        validate_options("pcb drc", {"units": "cm"})
    assert caught.value.option == "--units"
    assert caught.value.command == "pcb drc"
    assert "--units" in str(caught.value)


# --- extension point --------------------------------------------------------------------------
# Real kicad-cli integration tests do not live in this file. Add new registry unit tests above
# this marker; integration cases belong with the tool tests that own them.
