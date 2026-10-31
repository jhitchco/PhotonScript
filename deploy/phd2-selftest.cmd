@echo off
rem PS-92 pulse-path self-test, run by NINA's ExternalScript items that
rem PhotonScript inserts (after the twilight autofocus, and before each
rem guided target's StartGuiding). Argument: twilight | target.
rem It asks the running PhotonScript service to test the guide-pulse path
rem and ALWAYS exits 0, so a failed or skipped test never stalls NINA.
"C:\astro\venv\Scripts\photonscript.exe" phd2-selftest %1 --from-nina
exit /b 0
