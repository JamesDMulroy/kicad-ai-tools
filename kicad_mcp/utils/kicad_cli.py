"""
Centralized KiCad CLI detection and management.

Provides a single source of truth for locating KiCad CLI across platforms
with caching and configuration support.
"""

import logging
import subprocess
from .secure_subprocess import run_contained

from ..config import TIMEOUT_CONSTANTS

logger = logging.getLogger(__name__)


class KiCadCLIError(Exception):
    """Raised when KiCad CLI operations fail."""

    pass


class KiCadCLIManager:
    """
    Manages KiCad CLI detection and validation across platforms.

    Provides caching and fallback mechanisms for reliable CLI access.
    """

    def __init__(self):
        """Initialize the CLI manager."""
        self._cached_cli_path: str | None = None
        self._cache_validated = False

    def find_kicad_cli(self, force_refresh: bool = False) -> str | None:
        """
        Find the KiCad CLI executable path.

        Args:
            force_refresh: Force re-detection even if cached

        Returns:
            Path to kicad-cli executable or None if not found
        """
        # Return cached path if available and valid
        if self._cached_cli_path and not force_refresh and self._cache_validated:
            return self._cached_cli_path

        # Try to find CLI
        cli_path = self._detect_cli_path()

        if cli_path:
            # Validate the found CLI
            if self._validate_cli_path(cli_path):
                self._cached_cli_path = cli_path
                self._cache_validated = True
                self.last_error = None
                logger.info(f"Found KiCad CLI at: {cli_path}")
                return cli_path
            self.last_error = (f"kicad-cli found at {cli_path} but it failed to run '--version'; check that "
                               "installation (or point KICAD_CLI_PATH at a working kicad-cli)")
            logger.warning(self.last_error)

        # Clear cache if detection failed
        self._cached_cli_path = None
        self._cache_validated = False
        logger.warning("KiCad CLI not found on this system")
        return None

    def get_cli_path(self, required: bool = True) -> str:
        """
        Get KiCad CLI path, raising exception if not found and required.

        Args:
            required: Whether to raise exception if CLI not found

        Returns:
            Path to kicad-cli executable

        Raises:
            KiCadCLIError: If CLI not found and required=True
        """
        cli_path = self.find_kicad_cli()

        if cli_path is None and required:
            raise KiCadCLIError(
                getattr(self, "last_error", None)
                or "KiCad CLI not found. Install KiCad 10, set KICAD_APP_PATH / KICAD_CLI_PATH, or run "
                   "python -m kicad_mcp.setup."
            )

        return cli_path

    def is_available(self) -> bool:
        """Check if KiCad CLI is available."""
        return self.find_kicad_cli() is not None

    def get_version(self) -> str | None:
        """
        Get KiCad CLI version string.

        Returns:
            Version string or None if CLI not available
        """
        cli_path = self.find_kicad_cli()
        if not cli_path:
            return None

        try:
            result = run_contained(  # nosec B603 - CLI path is validated
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_CONSTANTS["kicad_cli_version_check"],
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (subprocess.SubprocessError, OSError) as e:
            logger.warning(f"Failed to get KiCad CLI version: {e}")

        return None

    def _detect_cli_path(self) -> str | None:
        """kicad-cli from kicad_env.resolve_kicad_cli, the single resolution (KICAD_CLI_PATH / config
        kicad_cli, KICAD_APP_PATH / config kicad_app_root, then discovery incl. PATH). Explicit settings
        and config errors are reported, never replaced by another location."""
        from kicad_mcp.adaptive_editor import kicad_env
        cli, error = kicad_env.resolve_kicad_cli()
        self.last_error = error
        if error:
            logger.warning(f"kicad-cli unavailable: {error}")
            return None
        logger.info(f"Using KiCad CLI: {cli}")
        return str(cli)

    def _validate_cli_path(self, cli_path: str) -> bool:
        """
        Validate that a CLI path is working.

        Args:
            cli_path: Path to validate

        Returns:
            True if CLI is working
        """
        try:
            result = run_contained(  # nosec B603 - CLI path is validated
                [cli_path, "--version"],
                capture_output=True,
                text=True,
                timeout=TIMEOUT_CONSTANTS["kicad_cli_version_check"],
            )
            return result.returncode == 0
        except (subprocess.SubprocessError, OSError, FileNotFoundError):
            return False


# Global CLI manager instance
_cli_manager = None


def get_cli_manager() -> KiCadCLIManager:
    """Get the global KiCad CLI manager instance."""
    global _cli_manager
    if _cli_manager is None:
        _cli_manager = KiCadCLIManager()
    return _cli_manager


def find_kicad_cli(force_refresh: bool = False) -> str | None:
    """Convenience function to find KiCad CLI path."""
    return get_cli_manager().find_kicad_cli(force_refresh)


def get_kicad_cli_path(required: bool = True) -> str:
    """Convenience function to get KiCad CLI path."""
    return get_cli_manager().get_cli_path(required)


def is_kicad_cli_available() -> bool:
    """Convenience function to check if KiCad CLI is available."""
    return get_cli_manager().is_available()


def get_kicad_version() -> str | None:
    """Convenience function to get KiCad CLI version."""
    return get_cli_manager().get_version()
