@echo off
rem PS-189 cover gate, run by the NINA ExternalScript item PhotonScript puts
rem at the start of each Piggy-600 OSC image pass, right after OpenCover
rem (NINA #2 only). Arguments: <rig> --label="<what>"
rem It asks the running PhotonScript service to hold until the Wanderer
rem Cover reads Open on NINA #2 (ninaAPI flatdevice info), at most
rem piggyback_cover_gate_timeout_s. Exit 1 when the gate says SKIP (the CLI
rem exits 3): NINA SkipInstructionSetOnError then skips that image pass, so
rem no light is ever shot with the cover closed. With the service down the
rem CLI reads NINA #2 itself and also skips unless the cover reads Open.
"C:\astro\venv\Scripts\photonscript.exe" cover-gate %* --from-nina
if %ERRORLEVEL% EQU 3 exit /b 1
exit /b 0
