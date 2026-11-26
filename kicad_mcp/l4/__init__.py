"""L4 isolated UI fallback broker (docs/L4-UI-BROKER-SPEC.md).

Drives stock KiCad 10.0.5 dialogs on a private Win32 desktop through UI Automation, only for
operations declared in a closed, versioned, sha256-approved registry. Submodules:

- ``registry``       closed registry schema, approval gate and typed parameter validation (pure)
- ``monitor``        user-desktop guard logic (pure) plus the Win32 samplers and watchdog
- ``hidden_desktop`` CreateProcessW + STARTUPINFOW.lpDesktop launcher (Windows only)
- ``uia_helper``     UIA command executor that runs ON the hidden desktop
- ``broker``         ``ui_fallback`` and ``record_identifiers`` orchestration

Only ``registry`` and the pure half of ``monitor`` import nothing Windows-specific.
"""
