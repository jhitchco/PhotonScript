@echo off
rem Re-entry gate (2026-10-15), run by the NINA ExternalScript items that
rem PhotonScript puts first in a target's "start AF", "slew" and "center"
rem re-entry containers. Arguments: af|slew|center --label="<target>"
rem [--filter=L] [--ra=<h> --dec=<deg>]. It asks the running PhotonScript
rem service whether that step can be skipped (POST /api/reentry/gate; for
rem the AF the service first moves the focuser to the last AF's focus).
rem Exit 1 ONLY when the gate says SKIP (the CLI exits 3): NINA's
rem SkipInstructionSetOnError then skips the rest of that small container.
rem Every other outcome (RUN, service down, CLI missing, a timeout) exits 0,
rem so the AF / slew / center run as before.
"C:\astro\venv\Scripts\photonscript.exe" reentry-gate %* --from-nina
if %ERRORLEVEL% EQU 3 exit /b 1
exit /b 0
