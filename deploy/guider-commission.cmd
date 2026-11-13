@echo off
rem 2026-10-28 guider commissioning night (arm recipe guider_commission), run
rem by the NINA ExternalScript items PhotonScript puts in the recipe
rem container and in tonight's PHD2_CALIBRATION slot.
rem Argument: gate | star | selftest | ga | ga-apply | nb-report
rem It asks the running PhotonScript service to do that step. Exit 1 ONLY
rem when the service says STOP (the CLI exits 3): NINA's
rem SkipInstructionSetOnError then skips the rest of that container (gate:
rem the guider steps were stopped tonight; star: no guide star on the OAG).
rem Every other outcome (done, failed, skipped, service down, CLI missing)
rem exits 0, so a failed step never stops the night.
"C:\astro\venv\Scripts\photonscript.exe" guider-commission-step %1 --from-nina
if %ERRORLEVEL% EQU 3 exit /b 1
exit /b 0
