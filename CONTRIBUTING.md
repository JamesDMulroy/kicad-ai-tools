# Contributing

Thank you for helping improve kicad-ai-tools.

## Ground rules
- **Nothing visible.** No change may show a window, steal focus or open a browser on the user's desktop.
  GUI work goes through the private-desktop runtime (`kicad_mcp/l4/`, `adaptive_editor/live/`).
- **No free-form execution.** MCP tools take typed, validated arguments. Do not add tools that take a
  command line, a script or an executable path from the client.
- **Mutations are guarded.** Anything that writes a user's project needs an explicit flag or a sha256
  guard, and must make a backup first.
- **No machine paths or names.** `tests/test_portability.py` fails on drive-letter paths, home folders
  and personal names. Locations come from the environment, the per-machine config or discovery.

## Development setup
```
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[l4]" pytest pytest-asyncio ruff pre-commit
.venv\Scripts\pre-commit install
```

## Before you open a pull request
1. Run the Windows and KiCad tests on a maintainer's Windows machine with KiCad installed:
   `python -m pytest` (with an unlocked desktop).
2. CI runs lint and the Linux-safe tests.
3. Run `ruff check kicad_mcp tests`.
4. If you changed the tool surface, run `python scripts/gen_operation_matrix.py` and commit
   `docs/OPERATION-MATRIX.md`.
5. Describe what you verified, and how, in the pull request.

## Reporting bugs and security issues
- Bugs and feature requests: open an issue using the templates in `.forgejo/ISSUE_TEMPLATE/`.
- Security vulnerabilities: do **not** open an issue. Follow `SECURITY.md`.

By contributing you agree that your contributions are licensed under the MIT License (`LICENSE`).
