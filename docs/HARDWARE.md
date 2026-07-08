# AARO Rig - Hardware & Site Reference

Known facts first, open questions at the bottom. Fill the TODOs in as they
get answered; the planner and QA thresholds should eventually read from here.

## Site
- Astronomy Acres Remote Observatories (AARO), Pier 3, Rodeo NM
- Sky: SQM ~23.9 (from first-night narrowband analysis)
- All-sky cam: https://allsky.astronomyacres.com · status: https://status.astronomyacres.com
- Scope PC on Tailscale: 100.94.189.77 (dashboard :8100)

## Optical train
- OTA: RC16 (406 mm) at 3248 mm f/8 (native, no reducer in use)
- Camera: OGMA AP26MC - IMX571 mono APS-C, 6224 x 4168, 3.76 um
- Image scale: 0.239"/px · FOV 0.414 x 0.277 deg
- Oversampled vs 2-3" seeing -> capture 1x1, software-bin 2x in integration
- Filters (NINA single-letter names -> canonical): L R G B + 3nm S(II) H(a) O(III)
- Camera setpoint 0 C, tolerance 1 C

## Mount & guiding
- iOptron CEM70G, historically unguided (encoders)
- Unguided reality at 3248 mm: 300s subs lose 30-60% of frames to trailing
  (SII 9/22 through registration, 2026-07-03)
- PHD2 installed on scope PC; PS_GUIDED_DEFAULT=true as of 2026-07-07
  (StartGuiding after center, dither every 5, StopGuiding at unsafe/end)

## Exposure & calibration standards
- NB 600s, BB 180s, gain 200, offset 256, 0 C ("NEW epoch", 2026-07-05+)
- OLD epoch (pre-2026-07-05 lights, e.g. Crescent 07-03): 300s, gain 200, offset 50
- Epoch = EXPTIME+GAIN+OFFSET+SET-TEMP; darks must match temperature,
  offset drift survivable via the 1000 DN calibration pedestal
- Dark quota: PS_DARK_EXPOSURES x PS_DARK_TARGET_COUNT at the setpoint
- Flats: dusk NB-first -> L-last; dawn BB-first -> NB-last; target 50% histogram

## Desktop processing
- PixInsight: C:\Program Files\PixInsight\bin\PixInsight.exe
- Staging: C:\Users\sleep\Astrophotography\Staging\<Target>
- Library mirror (receive-only): C:\Users\sleep\ninashare\Library

## TODO - unknowns to fill in (answers unblock real decisions)

### 1. Guide optics  [blocks guiding confidence]
- OAG or guide scope? Model? Guide camera model?
- First guided-night calibration result / typical RMS:

### 2. Safety monitor  [ANSWERED 2026-07-08 from NINA log]
- ASCOM Alpaca: "AARO Safety Obs 2" (ASCOM.AlpacaDynamic1.SafetyMonitor v1.0)
- Latency between "clouds/rain" and roof close: TODO

### 3. NINA install  [ANSWERED 2026-07-08 from NINA log]
- NINA 3.2.0.9001 · profile "RC16"
- Plugins seen: Ground Station (Pushover), Hocus Focus (auto-updates)
- Mount driver: ASCOM.SoftwareBisque (through TheSky) · FW: ASCOM.OGMAVision
- Guider: "PHD2_Single" at 127.0.0.1:4400 - connects cleanly
- Plate solve: FL 3248, PixelSize 7.52 (= bin 2x2 solve frames), search 30deg,
  blind failover on. NOTE 2026-07-08: flip-recenter solves through the 3nm H
  filter failed repeatedly - set a plate-solve FILTER (L) in NINA options.

### 4. PixInsight add-ons  [processing pipeline scope]
- StarXTerminator: yes/no · NoiseXTerminator: yes/no · BlurXTerminator: yes/no
- Other licensed tools:

### 5. Horizon & slew limits  [planner usable-hours accuracy]
- Obstructions by azimuth (deg alt at N/NE/E/SE/S/SW/W/NW):
- Mount altitude/meridian limits configured in NINA:

### 6. Failure recovery  [2 AM runbook]
- Remote power cycling (smart PDU? which outlets?):
- If NINA hangs / Tailscale drops:
- AARO support contact + hours:

### 7. Optical quirks  [QA threshold tuning]
- Collimation history, known tilt/corner behavior:
- Focuser model, backlash, per-filter focus offsets:
