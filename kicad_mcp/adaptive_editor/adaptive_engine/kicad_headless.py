#!/usr/bin/env python3
"""Fail-fast, non-interactive wx assertion policy for KiCad automation.

This module is intentionally process-scoped. It never changes the user or
machine environment and it does nothing in KiCad's interactive executables.
"""

from __future__ import annotations

import ctypes
import atexit
import os
import sys
from pathlib import Path


_APP = None
_LOG_TARGET = None


def is_standalone_kicad_python() -> bool:
    """Return true only for the bundled console Python that carries pcbnew."""
    executable = Path(sys.executable).resolve()
    pcbnew_module = executable.parent / "Lib" / "site-packages" / "pcbnew.py"
    return executable.name.lower() == "python.exe" and pcbnew_module.is_file()


def _disable_windows_crash_ui() -> None:
    if os.name != "nt":
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetErrorMode.restype = ctypes.c_uint
    kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
    kernel32.SetErrorMode.restype = ctypes.c_uint
    current = kernel32.GetErrorMode()
    # Preserve existing flags and add critical-error, GP-fault/WER, open-file UI off.
    kernel32.SetErrorMode(current | 0x0001 | 0x0002 | 0x8000)

    # abort() must not open a CRT/Watson reporter. The wrapper owns the log.
    ucrt = ctypes.CDLL("ucrtbase")
    ucrt._set_abort_behavior.argtypes = [ctypes.c_uint, ctypes.c_uint]
    ucrt._set_abort_behavior.restype = ctypes.c_uint
    ucrt._set_abort_behavior(0, 0x1 | 0x2)


def install_headless_assert_policy(*, explicit: bool = False) -> bool:
    """Install the policy, returning false when called from an interactive app."""
    if not explicit and not is_standalone_kicad_python():
        return False

    global _APP, _LOG_TARGET

    # wxSystemOptions maps this environment spelling to "exit-on-assert". Keep
    # the abort path active while wxPython constructs its zero-window app.
    os.environ["WX_EXIT_ON_ASSERT"] = "1"
    _disable_windows_crash_ui()

    import wx

    wx.SystemOptions.SetOption("exit-on-assert", 1)
    if wx.SystemOptions.GetOptionInt("exit-on-assert") != 1:
        raise RuntimeError("wx exit-on-assert policy did not activate")
    if wx.GetApp() is None and wx.App.IsDisplayAvailable():
        _APP = wx.App(False)
    else:
        _APP = wx.GetApp()

    if _APP is not None:
        _APP.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
        _LOG_TARGET = wx.LogStderr()
        wx.Log.SetActiveTarget(_LOG_TARGET)
        # With wxTheApp present, this routes asserts to wxPython's exception
        # handler. Restore abort-on-assert at shutdown before wxTheApp is lost.
        wx.SystemOptions.SetOption("exit-on-assert", 0)
        atexit.register(wx.SystemOptions.SetOption, "exit-on-assert", 1)
    return True


if is_standalone_kicad_python():
    install_headless_assert_policy()
