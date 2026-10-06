@echo off
rem PS-27 settle gate, run by the NINA ExternalScript item PhotonScript puts
rem before each Piggy-600 OSC light (NINA #2 only; NINA #1 is never touched).
rem Arguments: --label="<what>"
rem It asks the running PhotonScript service to hold until the RC16 mount is
rem not slewing, has been still for piggyback_settle_still_s and PHD2 is not
rem settling, at most piggyback_settle_timeout_s. It ALWAYS exits 0: the gate
rem can only delay a sub, never skip one, and a broken gate (service down,
rem CLI missing) never stops imaging.
"C:\astro\venv\Scripts\photonscript.exe" settle-gate %* --from-nina
exit /b 0
