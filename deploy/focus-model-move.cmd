@echo off
rem PS-76 part 2 focus-model move, run by the NINA ExternalScript items that
rem PhotonScript inserts ONLY when focus_model_drive is on and the RC16 focus
rem model is trusted (each driven filter block start and its temperature
rem trigger). Argument: the filter class the block images in (L, Ha, ...).
rem It asks the running PhotonScript service to move the focuser to the
rem lookup-table position for the focuser's current temperature and ALWAYS
rem exits 0, so a failed or refused move never stalls NINA (the HFR trigger
rem and the periodic verify AF still refocus).
"C:\astro\venv\Scripts\photonscript.exe" focus-move %1 --from-nina
exit /b 0
