@echo off
rem PS-61 cooler gate, run by the NINA ExternalScript item PhotonScript puts
rem before each RC16 light block and each Piggy-600 OSC image pass.
rem Arguments: <rig> --setpoint=<C> --label="<target filter>"
rem It asks the running PhotonScript service to hold until the sensor is
rem within cooler_gate_tolerance_c of the setpoint (at most
rem cooler_gate_timeout_min). Exit 1 ONLY when the gate says SKIP (the CLI
rem exits 3): NINA's SkipInstructionSetOnError then skips that block. Every
rem other outcome (pass, warn mode, service down, CLI missing) exits 0, so a
rem broken gate never stops imaging.
"C:\astro\venv\Scripts\photonscript.exe" cooler-gate %* --from-nina
if %ERRORLEVEL% EQU 3 exit /b 1
exit /b 0
