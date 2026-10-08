@echo off
rem PS-171 TPoint sample, run by the NINA ExternalScript item PhotonScript
rem puts after each frame of the TPoint mapping run (recipe
rem tpoint_mapping_then_tonight). Arguments: --rig rc16 --point <i> --of <n>
rem --alt <deg> --az <deg> --side east^|west
rem It Image Links the newest saved frame through TheSky (TCP 3040), adds
rem the point to TPoint only when PS_TPOINT_SAMPLE_ADD=auto and the probe
rem found an add method, and always writes a row to runs\<night>_tpoint.csv.
rem It ALWAYS exits 0: a failed solve (or a missing CLI) never stops the
rem mapping loop in NINA.
"C:\astro\venv\Scripts\photonscript.exe" tpoint-sample %* --from-nina
exit /b 0
