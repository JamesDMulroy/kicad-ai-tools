"""Fixed-operation child for the asynchronous workflow API; no free-form code."""
import json
import os
from pathlib import Path
import sys
import traceback

from kicad_mcp.utils import boardtooling
from kicad_mcp.utils.workflow_jobs import OPERATIONS, PARAMETERS, _write


def dispatch(operation, args, folder):
    required, optional = PARAMETERS[operation]
    if not isinstance(args, dict) or not required.issubset(args) or set(args) - required - optional:
        raise ValueError("Workflow request arguments do not match the fixed operation")
    if operation == 'kicad_cli':
        from kicad_mcp.tools.cli_tools import run_cli
        from kicad_mcp.utils.cli_registry import get_command
        kind = get_command(args['command']).output_kind
        output = str(folder / ('output' if kind == 'dir' else 'output.report')) if kind else None
        return run_cli(**args, output_path=output, evidence_base=folder / 'native-commands', timeout_seconds=None)
    if operation == "inspect_bundle":
        return boardtooling.inspect_bundle(**args)
    if operation == "create_contract_draft":
        return boardtooling.create_contract_draft(**args)
    if operation == "evaluate_contract":
        return boardtooling.evaluate_contract(**args)
    if operation == "plan_placement":
        return boardtooling.plan_placement(**args)
    if operation == "evaluate_gates":
        return boardtooling.evaluate_gates(**args, output_dir=str(folder / "gate-evidence"))
    if operation == "query_scene":
        return boardtooling.query_scene(**args)
    if operation == "release_preview":
        return boardtooling.release(**args, output_dir=str(folder / "release-preview"))
    if operation == "release_finalize":
        preview = args["preview_dir"]
        remaining = {name: value for name, value in args.items() if name != "preview_dir"}
        return boardtooling.release(**remaining, output_dir=str(folder / "release-finalize"),
                                    finalize_preview=preview)
    raise ValueError("Unsupported fixed workflow operation")


def main():
    if len(sys.argv) != 3:
        raise ValueError("Workflow worker requires exact request and result paths")
    request_path, result_path = map(Path, sys.argv[1:])
    os.environ["KICAD_MCP_ACTIVE_JOB_DIR"] = str(result_path.parent.resolve())
    request = json.loads(request_path.read_text(encoding="utf-8"))
    operation = request["operation"]
    if operation not in OPERATIONS:
        raise ValueError("Unsupported workflow operation")
    try:
        result = dispatch(operation, request["arguments"], result_path.parent)
        _write(result_path, {"worker_completed": True, "result": result})
    except BaseException as exc:
        _write(result_path, {"worker_completed": False,
                            "error": f"{type(exc).__name__}: {exc}"})
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
