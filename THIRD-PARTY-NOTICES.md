# Third-party notices

kicad-ai-tools is MIT-licensed (see `LICENSE`). It started from the open-source `kicad-mcp` project
(MIT; its copyright notice is kept in `LICENSE`).

Licenses below come from the installed packages' metadata on 2026-11-26. Regenerate this list from
the release SBOM (`python scripts/build_release.py`); it does not replace that SBOM.

## Runtime dependencies (MCP server)

| Package | Version | License |
|---|---|---|
| mcp | 1.11.0 | MIT |
| pydantic | 2.11.7 | MIT |
| defusedxml | 0.7.1 | PSF |
| cryptography (BoardTooling release export only) | 50.0.0 | Apache-2.0 OR BSD-3-Clause |
| comtypes (L4 UI helper, Windows) | 1.5.0 | MIT |
| protobuf | 5.29.6 | BSD-3-Clause |
| protoc-wheel-0 | 29.4 | BSD-3-Clause |
| pynng | 0.9.0 | MIT |
| zstandard (embedded-file tools) | 0.25.0 | BSD-3-Clause |

## KiCad plugin dependencies

These are installed by KiCad into the plugin's own venv from `plugin/requirements.txt`.

| Package | Version | License |
|---|---|---|
| kicad-python (kipy) | 0.8.0 | MIT |
| protobuf | 5.29.6 | BSD-3-Clause |
| pynng | 0.9.0 | MIT |
| cffi | 2.1.1 | MIT-0 |
| pycparser | 3.11 | BSD-3-Clause |
| sniffio | 1.3.1 | MIT OR Apache-2.0 |
| typing_extensions | 4.16.0 | PSF-2.0 |

## KiCad
KiCad (GPL-3.0-or-later) is **not** bundled. The tools use the user's own KiCad installation:
- they run `kicad-cli` and `pcbnew` as separate processes;
- they import `pcbnew` only inside KiCad's own bundled Python interpreter;
- they talk to the running PCB Editor over KiCad's IPC API.

Vendored files (`kicad_mcp/adaptive_editor/adaptive_engine/kicad_headless.py`,
`kicad_mcp/utils/winproc.py`) are original work under this package's license.
