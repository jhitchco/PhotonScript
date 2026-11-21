# TheSky64 UI survey: Bisque TCS + TPoint (2026-11-02)

Read-only computer-use walk through TheSky64 (build 14139, Paramount MX
Series 6, MKS 6000, COM3) on the scope PC, mount parked, armer DISARMED.
Nothing was applied or saved: dialogs were opened, read and closed. Compared
against what PhotonScript reads through the TheSky scripting API (:3040),
TPoint files and the registry (see `thesky_client.py` READ_PAIRS,
`thesky_audit.py`, `tpoint_files.py`, `config/thesky/desired_rc16.toml`).

Where things live in the UI:
- Telescope tab (left dock) > **Shutdown** menu: Set Park Position, Clear
  Park Position, Park, Unpark, Disconnect Telescope.
- Telescope tab > **Tools** menu: Turn Sidereal Tracking On, Turn Tracking
  Off, Slew To Coordinates, Slew Prior, Star Search, **Bisque TCS...**,
  Virtual Mount.
- Menu **Telescope** > Telescope Setup, Connect, **TPoint Module...**,
  Telescope Limits, Digital Setting Circles.

## 1. Bisque TCS (Tools > Bisque TCS)

Firmware 6.0.174, I/O 6.0.506. Tabs: Parameters, Show Status, Periodic Error
Correction (greyed), Utilities, Logging, On-Axis Encoders.

### Parameters (values held in the MKS 6000; HA / Dec)

| Group | Parameter | HA | Dec |
|---|---|---|---|
| Common | Acceleration | 250 | 250 |
| | Non-sidereal rate | 0 | 0 |
| | Guide rate (% sidereal) | 0.499995 | 0.499995 |
| | Max slew speed (%) | 50 | 50 |
| | Flip hour angle | -0.30 h (= -18 min) | |
| | Focus pulse slow / fast | 60 / 200 | |
| User-Defined | Track rate (% sidereal) | 100 | 100 |
| | Tics/revolution | 6,000,000 | 6,000,000 |
| | Relative home sensor position | 0 | 0 |
| Axis | Unit identifier | 100 | 100 |
| | Guide rate (tics) | -350,479 | 350,479 |
| | Maximum speed | 900,000,000 | 900,000,000 |
| | Track rate | -22,430,867 | 0 |
| | Min / max position limit | -3,375,000 / 375,000 | -4,250,000 / 1,250,000 |
| | Joystick center | 1,859 | 1,860 |
| System | Mount type 0, sync version 10,000, focus axis 1, mount id "Paramount MX Series 6", debug 0, startup joystick tests 0, guider sound 0 | | |
| | Hand controller rates 1-4 | 277,581 / 2,523,471 / 66,123,104 / 661,231,040 | |
| | General purpose I/O 1-4 | 0 | |
| Homing | Home required 0, direction 1, sense 1, mode 2, in-out-in 1, from joystick 1 | same | same |
| | Home velocity high / medium / low | 700M / 80M / 4M | same |
| | Home index offset | 14 | **48** |
| Initialization | Focus multiplier 1, flip HA -0.30, temperature slew rate 60 % below 5 °C, kernel state 1, call/packet logging 0, home after connect 0, show limits 0, log slews 0, **prohibit tracking below horizon 1** | | |
| Motor | Index angle | 188 | 213 |
| | EMF constant 53, max voltage 15, max gain 120, max position error 2,000, field voltage 8, poles 8, encoder count 4,000, CoefD 8, max integral 1,280, small step 5, Coeff I/P 256/256, deriv max 256, torque advance 200, max current 950 mA, index polarity 1 | same | same |
| PEC | Motor-to-worm gear 4, cutoff speed 44,861,734, harmonics 2, ratios 1/4/1, scale 1, teeth 375, worm periods 1 | HA | Dec: harmonics 0, ratios 0, teeth 0 (no PEC on Dec, normal) |
| On-axis encoders | A2 / encoder count 67,108,864; M2 6,000,000; LP coeff 128; max pos err 10,000 | same | same |
| | PEC step small / big / above-cutoff / small-step… | 1 / 300 / 120 / 4 / 300 | 1 / 221 / 250 / 20 / 50 |
| | Trans-speed | 1,093,504 | 2,523,470 |
| | Ecc angle / Ecc dist | 0 / 0 | 0 / 0, **message "Out of range"** |

### Other tabs
- **Show Status** (parked): position HA -1,082,175 / Dec -1,518,219; servo
  state 18/18; control state 4/4; on-axis encoder HA -1,433,803 / Dec
  -1,518,218.
- **On-Axis Encoders**: both axes Ready, version 12, on-axis delta 0.1
  arcsec. Buttons Enable/Disable On-Axis Encoders (not touched).
- **Utilities** pages (viewed only): Hemisphere Setup (Northern; home sensor
  rel/abs 0), Slew Rate Adjustment (below **5 °C apply 60 %**; current
  100 %, ambient 26.2 °C), Set Motor Index Angles (Start slews each axis
  20 degrees: never click), **Mount Slew Limits: OTA east of pier -1.50 h
  past the meridian** (west not viewed: the radio button selects which
  side gets set), Hand Controller (rates greyed until "Get Current Rates").
  Not opened: Reboot/Update Firmware, Calibrate Home Index Offset, Exercise
  Mount, WiSky, Ethernet.
- **Logging**: empty, Log slews off.

## 2. TPoint Module (Telescope > TPoint Module...)

Active model **"TPoint Settings-2026oct-run3"**, Sky RMS **22.77"**, PSD
**25.13"**.

- **Setup**: **Apply pointing corrections ✓**. The "TPoint Module
  Settings" dropdown is a file menu (New, Open, Save, Save As,
  Import/Export pointing data, Restore defaults, Serial number).
- **Calibration Run**: status "Calibration complete", run date
  **2026-10-23**. Samples #1-#10 are unticked (excluded), #11 onward are used.
  - **Automated Calibration > Setup (AILS)**: exposure 8 s, **image scale
    0.472"/px**, binning greyed (blank), filter L (greyed), search area 8
    FOV, retries 2, All Sky Image Link off.
  - **Create Pointing Targets**: 123 targets, azimuth 354.5° to 352.0°,
    altitude 15° to 90°, randomize ✓, clip at horizon ✓, sort for dome off.
- **Model** terms ("), value / sigma: IH -3804.06/10.6, ID -2493.21/7.5,
  NP 159.37/16.9, CH -137.55/9.0, ME -467.44/25.2, MA -117.58/7.0,
  TF 249.25/14.8, HHSH 341.29/15.3, HDSD 279.51/10.4, HDCD 315.26/19.2,
  HDCH 252.79/23.3, HDSD4 -28.37/5.2. TX, FO, HCES/HCEC, DCES/DCEC, DAF and
  DNP are off. "Use all cores" ✓.
- **Polar Alignment**: MA -117.6" (2.0'), adjust (confidence High;
  Paramount: loosen left knob, tighten right 2.2 tics). ME -467.4"
  (7.8'); recommended ME -77" (refracted pole), so lower the polar axis
  390" (6.5', about 5 tics). The report itself says on a Paramount a
  modest error can be left to TPoint + ProTrack.
- **ProTrack**: **Activate ProTrack ✓**, **Enable tracking adjustments ✓**
  (both greyed while parked).

## 3. On disk: `ImagingSystem.ini`

`%USERPROFILE%\OneDrive\Documents\Software Bisque\TheSky Professional
Edition 64\Imaging System Profiles\ImagingSystem.ini` holds most of the
non-firmware setup in plain text. This answers several rows the API can only
mark "verify by eye":

| Section / key | Value | UI equivalent |
|---|---|---|
| [MKSDRIVER0] ProTrackOn | 1 | TPoint > ProTrack: Activate |
| PPECOn, PPECHarmonics/Ratio1-3/Teeth/Scale | 1, 2, 1/4/1, 375, 1 | TCS PEC group |
| FlipHourAngleMinutes | -18 | TCS flip HA -0.30 h |
| **UnparkOnConnect** | **1** | connecting TheSky to the mount unparks it |
| SiderealTracking | 0 | |
| Parked, ParkPositionHA2 / Dec2 | 1, -1438563 / -1518219 | park position (encoder tics) |
| TemperatureSpeedThreshold / Perc | 5 / 60 | Utilities > Slew Rate Adjustment |
| EnableSystematicSlewApproach | 0 (3600" size) | |
| PortName | COM3 | |
| [Mount0] m_bEnableModeling | 1 | Apply pointing corrections |
| m_bImposeDestinationSlewLimits / AbortSlewsEnteringLimits | 1 / 0 | Telescope Limits |
| m_dSoftwareParkAz / Alt | 0.992° / 31.784° | |
| m_bConfirmSlews / Syncs / PointingSamples | 1 / 1 / 1 | confirmation dialogs (a script or click can get stuck on them) |
| [LTI] ParkOnShutdown | false | |
| [AUTOMAP0] Scale / Binning / Exposure / SearchArea / Retries / Filter | 0.472 / **1x1** / 8 / 8 / 2 / L | AILS setup |
| AltStart, AzimStart / AzimStop, Spacing, Randomize, ClipHz | 15, 354.49 / 352.0, 11, 1, 1 | Create Pointing Targets |
| PositionAngle | 85.18 | |

## 4. Findings

1. **AILS scale vs binning:** TheSky shows 0.472"/px and `[AUTOMAP0]` says
   Binning=1x1. `docs/TPOINT_AUTOCAL.md` records that TheSky's camera
   ignores 2x2 and shoots 1x1. If so, the next TheSky-driven calibration run
   would solve at the wrong scale. 0.236 is correct for 1x1. Check the run
   binning before the next mapping run. PS-104 `ails_image_scale` judges this
   from the manual record.
2. **`ImagingSystem.ini` has 14 corrupt `FlipHourAngleMinutes<garbage>`
   keys** in `[MKSDRIVER0]`. TheSky still reads the real key (-18), but keep
   a backup of this file before reinstalls or edits.
3. **UnparkOnConnect=1:** a TheSky (re)connect unparks the mount. The
   lifecycle never restarts TheSky, but a hand restart in daylight will
   leave the mount unparked. Add Park to any manual reconnect.
4. **Polar alignment** is 7.8' in elevation (ME). It is not urgent with
   ProTrack, but it drives field rotation on long unguided subs. Worth a
   tic-adjust plus recal on a site visit.
5. The TCS Dec on-axis encoder **Ecc Angle/Dist read "Out of range"**
   (0/0). Likely benign (eccentricity not calibrated); note it for Bisque
   support if encoders misbehave.
6. Samples #1-#10 of the 10-23 run are excluded from the model by hand.

## 5. API vs UI

| Setting | API (:3040) | UI | File |
|---|---|---|---|
| Apply pointing corrections | candidate only; no TPoint object on build 14139 | ✓ | [Mount0] m_bEnableModeling=1 |
| ProTrack active / tracking adjustments | candidate only | ✓ / ✓ | [MKSDRIVER0] ProTrackOn=1 (no key seen for adjustments) |
| Model terms, RMS, points | candidate only | Model tab | `TPoint\*outmod.dat` (already parsed) |
| AILS scale, exposure, search, retries | **readable** (AutomatedImageLinkSettings) | Setup | [AUTOMAP0] |
| AILS binning | not in API | greyed | [AUTOMAP0] Binning |
| Target grid (count, az/alt range) | not in API | Create Pointing Targets | [AUTOMAP0] |
| Polar error MA/ME | not in API | Polar Alignment | outmod.dat (MA/ME terms) |
| Flip HA, PEC, slew-rate-by-temperature, UnparkOnConnect | not in API | TCS | [MKSDRIVER0] |
| TCS firmware parameters (rates, limits, motor, homing, encoders) | not in API | TCS only | none (in the MKS 6000) |
| Mount slew limit (-1.50 h east) | not in API | TCS Utilities | none seen |
| Park / tracking state | readable | Mount Status | [MKSDRIVER0] Parked |

**Suggestion:** a read-only `ImagingSystem.ini` parser could turn the
PS-104 "verify by eye" rows (Apply corrections, ProTrack) into real reads,
and add AILS binning, UnparkOnConnect and flip HA to the settings ledger.
Caveat: the file holds TheSky's last *saved* state, not necessarily the live
one.

## 6. NINA and PHD2

See `docs/UI_SURVEY_NINA_PHD2.md`, also from 2026-11-02.
