"""
Utility functions for detecting and selecting available KiCad API approaches.
"""
import logging
import os
import subprocess


def cli_api_status() -> tuple[bool, str | None]:
    """``(True, None)`` when the resolved kicad-cli runs, else ``(False, reason)``. The reason is the
    resolver's own (config_invalid, configured_path_invalid, not found) so the caller can show the right fix.
    No fallback: kicad_env is the single resolution."""
    from kicad_mcp.adaptive_editor import kicad_env
    cli, error = kicad_env.resolve_kicad_cli()
    if error:
        return False, error
    try:
        done = subprocess.run([str(cli), "--version"], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL, creationflags=0x08000000 if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"kicad-cli at {cli} did not run: {exc}"
    if done.returncode != 0:
        return False, f"kicad-cli at {cli} failed --version (exit {done.returncode})"
    return True, None


def check_for_cli_api() -> bool:
    """True when the resolved kicad-cli runs; otherwise the reason is logged (see cli_api_status)."""
    ok, reason = cli_api_status()
    if not ok:
        logging.getLogger(__name__).warning("kicad-cli unavailable: %s", reason)
    return ok
