# NINA x2 + PHD2 UI survey (2026-11-02)

Read-only computer-use walk on the scope PC, armer DISARMED, no sequence
running, mount parked. Equipment pages and Options tabs were viewed, and the
PHD2 Advanced Settings (Brain) dialog was opened and closed with **Cancel**.
Nothing was connected, applied or saved. Each screen was compared with the
API at the same moment:
- ninaAPI `GET /v2/api/profile/show?active=true` and `equipment/*/info` on
  :1888 / :1889
- PHD2 `get_*` JSON-RPC on :4400

Companion: `docs/UI_SURVEY_THESKY.md`.

**Bottom line:**
- **NINA:** every Options value and camera-driver toggle on screen matched
  the API profile. The camera toggles are `CameraSettings.TouptekAlike*`,
  `BinAverageEnabled` and `GenericCameraFanSpeed` in `profile/show`, not in
  `equipment/camera/info`.
- **PHD2:** the JSON-RPC calls cover about a third of the Brain dialog. The
  rest is reachable only through the registry, which `phd2_profile_store`
  already reads.

## 1. NINA #1 (RC16, :1888, profile "RC16", 3864b0f8-...)

**Equipment** (all connected):
- **Camera:** AP26MC mono 6224x4168, 3.76 µm, OGMA SDK 59.28926. The
  driver panel shows: default gain 200, offset 256, USB limit 2, readout
  **High Conversion Gain** for both snapshots and sequences, **Ultra mode
  ON**, **High Fullwell Mode ON**, **Binning averages pixels ON**, dew
  heater strength 4, fan speed 0, LED off. Sensor 35 °C, cooler off (day).
- **Filter wheel:** OGMAVision, 7 positions R G B S H O L.
- **Focuser:** EAF "MainRC16", position 5737, 18.3 °C, max step 60000,
  reverse off.
- **Mount:** "Driver for telescope connected through TheSky" (ASCOM
  SoftwareBisque 6.2, TheSky64 10.5.0 build 14139). Parked, tracking
  stopped, side of pier West.
- **Guider:** PHD2, connected, Stopped. Guider scale 0.37"/px, camera scale
  0.24"/px. Dither 20 px (= 31.17 camera px). The panel shows: settle pixel
  tolerance **5 px**, settle timeout 60 s, guiding start timeout 300 s,
  minimum settle time 5 s, guiding start retry ON, ROI 100 %, dither RA only
  off, PHD2 profile "Primary RC Profile (Guider)".
- **Weather:** "AARO Conditions" (11.2 °C, 92 % RH, 879.9 hPa, sky 12.06
  mag).
- **Safety monitor:** "AARO Safety Obs 2" (Is Safe: no; daytime).
- **Flat panel:** none.

**Options:**
- **General:** site 31.906944 / -109.021111 / 1300 m.
- **Equipment:**
  - Camera: 3.76 µm, 16 bit, Bayer Auto.
  - Telescope: "RC16" 3248 mm f/8.
  - Mount: "Paramount", settle after slew 5 s, **Coordinates sync ON**,
    Time sync ON, Location sync Prompt.
  - Disable guiding during filter change: off.
- **Autofocus:**
  - Filter offsets off, initial offset steps 4, step 110, exposure 6 s,
    Star HFR, Hyperbolic, 2 attempts, 2 exposures per point.
  - Binning 4, R² 0.6, settle 1 s, inner crop 0.8, backlash Overshoot
    IN 50 / OUT 0, disable guiding during AF ON.
  - Per filter: L/R/G/B 6 s at 1x1; S 20 s, H 30 s, O 20 s at 3x3. All
    gain 200, offset 256, focus offset 0.
- **Imaging:**
  - Files: FITS to `C:\Users\jeremy\Documents\N.I.N.A`, pattern
    `$$DATEMINUS12$$\$$IMAGETYPE$$\$$DATETIME$$_$$TARGETNAME$$_$$FILTER$$_$$EXPOSURETIME$$s_$$FRAMENR$$`.
  - Meridian flip: 5 / 10 min after, use side of pier ON, recenter ON,
    settle 30 s, pause 0, AF after flip off.
  - Image options: annotate ON, Hocus Focus detector.
- **Plate solving:** ASTAP for both solver and blind solver, 20 s, filter
  L, bin 1, **gain 300**, 1' tolerance, 5 attempts, 0.5 min delay, search
  radius 30, downsample 0, max objects 500.

**API vs UI:** every Options value above matched `profile/show`. One naming
difference: the API's `GuiderName` is "PHD2_Single" while the UI shows
"PHD2".

**Camera driver toggles:** these are in the API profile, under
`CameraSettings`:
- `TouptekAlikeUltraMode`
- `TouptekAlikeHighFullwell`
- `BinAverageEnabled`
- `TouptekAlikeLEDLights`
- `TouptekAlikeDewHeaterStrength`
- `GenericCameraFanSpeed` (70 in the profile, against "0" on the panel)

The ledger has stored them in every snapshot since 2026-10-13.

## 2. NINA #2 (Piggy-600, :1889, profile "piggyback600", bca6fe74-...)

**Equipment:**
- **Camera:** AP26CC (RGGB). Gain 100, offset 256, USB limit 9, readout
  **Low Conversion Gain** for both, Ultra mode ON, **High Fullwell OFF**,
  binning averages OFF, dew strength 4, fan 0, LED ON.
- **Filter wheel:** none.
- **Focuser:** ZWO Focuser (ASCOM 6.0), position 11045, 19.3 °C.
- **Mount:** TheSky driver selected, **not connected** (by design).
- **Guider:** PHD2 (OFFLINE), dither 5 px. Camera scale 1.29"/px.
- **Flat panel:** WandererEmpire Cover 1, cover Closed, light off. Trained
  flat at 1x1, gain 100, offset 256, brightness 50, 0.14 s.
- **Safety monitor:** "OBS2 Safety 2026", **not connected**.

**Options:**
- **General:** same site values as NINA #1.
- **Equipment:** telescope 600 mm f/5.6; mount sync and time sync ON,
  location sync Prompt.
- **Autofocus:** step 50, 4 s, Star HFR, Hyperbolic, 1 attempt, 1 per
  point, bin 1, R² 0.7, no backlash.
- **Imaging:** FITS to `C:\Users\jeremy\Documents\NINA-Piggyback\`;
  meridian flip same as NINA #1; annotate off.
- **Plate solving:** ASTAP, 2 s, bin 1, gain default, 10 attempts, 2 min
  delay, radius 30.

**API vs UI:** matched.

## 3. PHD2 2.6.14, profile 2 "Primary RC Profile (Guider)"

Equipment is ZWO ASI174MM Mini plus the TheSky ASCOM mount, both connected,
Stopped, calibrated. Calibration: xAngle 58.3°, xRate 24.459, yAngle
-133.7°, yRate 30.6, at Dec 61.6°.

| Brain tab | Setting | UI | JSON-RPC | Desired (PS-89) |
|---|---|---|---|---|
| Global | Dither mode / scale / RA only | Random / 1.0 / off | not exposed | none |
| | Diagnostic image logging; save lost-star frames; relative / absolute error triggers | on; on; 4 / 4 px | not exposed | none |
| | Log folder | `C:\Users\jeremy\OneDrive\Documents\PHD2` | not exposed | none |
| Camera | Noise reduction | 3x3 median | not exposed | none |
| | Auto exposure min / max / target SNR | 2 s / 6 s / 50 | not exposed | none |
| | Exposure (main window) | 2.0 s | get_exposure 2000 | ASI 1500-4000 ✓ |
| | Saturation by max-ADU | 65535 | registry | ✓ |
| | Pixel size / gain / binning | 5.86 / 40 / 1 | binning: get_camera_binning 1; rest registry | ✓ / band 33-50 ✓ / 1 ✓ |
| | Variable exposure delay | off (1 / 10 s) | get_variable_delay_settings | none |
| | Disconnect unresponsive camera after | 15 s | not exposed | none |
| Guiding | Search region | 20 px | get_search_region 20 | ASI 20-35, target 25 (low edge) |
| | Star mass detection | on, 80 % | registry | ✓ |
| | Min / max star HFD | 1.6 / 10.0 | registry (min) | 1.5-2.5, target 2.0 ✓ |
| | Min SNR for AutoFind | 12 | registry | 8-20 ✓ |
| | Multiple stars | on | registry | ✓ |
| | Focal length | 3248 | registry | ✓ |
| | Calibration step | 180 ms | registry | computed |
| | Auto restore calibration / Dec compensation | on / on | registry | ✓ / ✓ |
| | Assume Dec orthogonal | off | registry | none |
| | Reverse Dec after meridian flip | **off** | registry | computed "flip" (verify) |
| | Stop guiding when mount slews / enable guide output / fast recenter / always scale | on / on / on / on | guide output: get_guide_output_enabled true | ✓ |
| Algorithms | RA | Lowpass2, aggressiveness 55, min move 1.50 px | get_algo_param ✓ | Lowpass2 OK with ProTrack; MinMove from GA |
| | Dec | ResistSwitch, aggressiveness 100, min move 1.50, fast switch on | get_algo_param ✓ | ResistSwitch ✓; ≤ 5 px ✓ |
| | Dec backlash compensation | off (20 / 20 / 20) | registry | off ✓ |
| | Max RA / Dec duration; Dec guide mode | 2500 / 2500 ms; Auto | Dec mode: get_dec_guide_mode Auto | Auto ✓ |

## 4. Findings

1. **NINA #1 settle tolerance: fixed 2026-11-02.** It was 5 px (1.9"). It
   is now **2.6 px** (0.97", inside the 0.5-1.0" range), set through
   ninaAPI. 2.7 px would be 1.004", just over the limit, and the rule's
   old fix text said 2.7. Rule `nina1_settle_arcsec` passes.
2. **High Fullwell: kept as is and now pinned.** RC16 has it ON and the
   Piggy-600 OFF, unchanged in every ledger snapshot since 2026-10-13.
   - Every library dark and flat was shot that way, and the mode is in no
     FITS header. Flipping it would silently orphan the calibration
     library, so it stays.
   - New ledger rules: `nina1_high_fullwell` (FAIL, pages),
     `nina2_high_fullwell` (FAIL), and `nina1_ultra_mode` /
     `nina2_ultra_mode` (WARN).
   - Change the mode only together with a new dark and flat library.
3. **NINA #1 coordinates sync: turned off 2026-11-02.** NoSync is now true,
   so NINA centers with offset slews and never syncs TheSky's TPoint model,
   which TheSky also confirms on every sync (`m_bConfirmSyncs=1`). New rule
   `nina1_no_sync` (WARN). NINA #2 does not connect the mount, so it was
   left alone.
4. **NINA #2's safety monitor is not connected in the daytime, and that is
   by design.**
   - Its profile selects `AlpacaDynamic4` ("OBS2 Safety 2026"); NINA #1
     uses `AlpacaDynamic3`. In the ASCOM registry, both entries (and
     `AlpacaDynamic1`) point at the same device: 192.168.1.8:12345, device
     2.
   - The armer connects it when the piggyback companion dispatches, with
     one retry (`armer._dispatch_piggyback_companion`), and allows OSC
     lights only if that connect succeeds.
   - The ledger rule `nina2_safety_connected` works. Snapshots taken while
     a night runs show NINA #2's monitor Connected=true on 10-24 through
     10-27. The "na" in the night summary comes from the final snapshot,
     taken after the night is COMPLETE, when "while a night runs" rules do
     not apply.
5. **NINA #1 plate-solve gain is 300**, against camera gain 200. This is
   harmless for solving, but noted because the ledger checks only the
   camera gain.
6. **PHD2 search region is 20**, the bottom of the ASI band (target 25),
   and equal to NINA's 20 px dither. It is within the rule, with no margin.
7. **PHD2 "Reverse Dec output after meridian flip" is off.** PS-89 computes
   the right value from `CalFlipRequiresDecFlip`. Confirm the audit row
   reads PASS.
8. **PHD2 Brain settings outside JSON-RPC are now tracked (2026-11-02).**
   `phd2_profile_store.KEYS` gained 20 registry keys, checked against
   profile 2:
   - noise reduction
   - auto exposure min / max / SNR
   - camera timeout and subframes
   - dither mode / scale / RA only
   - max star HFD, tolerate jumps, fast recenter, scale image
   - max RA / Dec pulse
   - HiResEncoders
   - the image logger

   The ledger snapshots them, and report rows (`phd2_noise_reduction`,
   `phd2_auto_exposure_range`, `phd2_dither_mode`, `phd2_dither_scale`,
   `phd2_max_pulse`, `phd2_image_logging`) show them, so a change appears
   in the System page diff.

## 5. Device drivers (read 2026-11-02, registry only)

The ASCOM drivers' settings were read from their ASCOM profiles. Driver
setup dialogs were not opened (many apply on OK or need the device
disconnected), and ASCOM Diagnostics' device tests were not run (they
connect to devices NINA holds). Location:
`HKLM\SOFTWARE\WOW6432Node\ASCOM\<Type> Drivers\<ProgID>`. ASCOM Platform
7.1.3 (current).

| Device (who uses it) | Driver | Settings |
|---|---|---|
| Mount (NINA #1, PHD2) | `ASCOM.SoftwareBisque.Telescope` 6.2 (TheSky64) | **UseDirectGuide False**, **InhibitSync True**, PulseGuide True, CanGetPierSide True, **CanSetTracking False**, CanPark True, UnparkOnConnect True, TrackOffsets True, SlewDelay True, AlignmentMode German polar, RA/Dec guide rate 7.5"/s (= 0.5x, matching the TCS 0.499995), trace off |
| Filter wheel (NINA #1) | `ASCOM.OGMAVision.FilterWheel` 6.5 | 7 slots R G B S H O L, offsets 0, Bidir false |
| RC16 focuser (NINA #1) | NINA native ZWO EAF "MainRC16" | in the NINA profile (max step 60000, reverse off) |
| Piggy focuser (NINA #2) | `ASCOM.EAF.Focuser` 6.0 | EAF "PigAstro", USB (not BLE), step increment 10, reconnect True |
| Cameras (NINA #1 / #2) | NINA native OGMA SDK | in the NINA profile (section 1) |
| RC16 camera **for TheSky** (TPoint, Image Link) | `ASCOM.OGMAVision.Camera` | selected camera `Camid` = pid_9428 (**AP26MC**). Per-camera keys: AP26MC gain 85, black level 20, HCG (Cvgain 1), Low Noise on, **High Fullwell off**, 16 bit raw, speed 9, bin 1. AP26CC gain 50, black level 0. These never affect NINA frames. Careful: the driver's Setup dialog opens on AP26CC (the first camera in its list), so pressing OK there can switch TheSky to the Piggy-600 camera. Close it with Cancel. |
| Guide camera (PHD2) | native ZWO | in the PHD2 profile (section 3) |
| Flat cover (NINA #2) | `ASCOM.WandererCover1.CoverCalibrator` 2.0 | COM1, trace off |
| Safety monitor (both) | Alpaca Dynamic 1/3/4 | all 192.168.1.8:12345 device 2; timeouts 10 / 30 / 120 s |
| Weather (NINA #1) | Alpaca Dynamic 1 ObservingConditions "AARO Weather" | 192.168.1.8:12345 device 0; timeouts **2** / 10 / 120 s |

The Alpaca, cover and Diagnostics logs (`Documents\ASCOM\Logs <date>`)
show no timeouts or exceptions from 10-27 to 11-02.

Findings:
1. **DirectGuide is off.** The PS-89 row `ascom_direct_guide` wants it on
   (FAIL): without it TheSky can drop ASCOM pulse guides, the PS-167
   pattern. The audit could not read this flag before.
   `phd2_profile_store.ASCOM_KEYS` now reads `UseDirectGuide` and
   `CanGetPierSide`, so the next arm audit reports it as a FAIL and pages.
   To turn it on:
   - disconnect the mount in PHD2 and NINA #1;
   - open the Bisque driver setup (NINA #1 > Equipment > Mount > gear icon,
     or ASCOM Profile Explorer);
   - tick "use DirectGuide", then reconnect.

   Changed 2026-11-03 06:5x local with Jeremy's go, while no app held the
   mount (NINA #1 and PHD2 closed, NINA #2 mount disconnected): `UseDirectGuide`
   False -> True under `HKLM\SOFTWARE\WOW6432Node\ASCOM\Telescope Drivers\
   ASCOM.SoftwareBisque.Telescope` (the ASCOM profile; writable as the user).
   It takes effect at the next mount connect.
2. **InhibitSync True:** the driver already ignored syncs, so NINA #1's
   centering "syncs" never reached TheSky. A sync that is silently ignored
   leaves NINA re-slewing to the same wrong coordinates. That may explain
   slow centers, such as the 13.7 min center on 2026-10-10 (unverified).
   NINA #1 now has NoSync on, so it centers with offset slews, which should
   converge in fewer passes. Watch the first centered target.
3. **CanSetTracking False:** recent NINA logs show no refused SetTracking,
   and tracking works through TheSky, so this has no effect seen so far.
4. **UnparkOnConnect True** in the driver too (as in TheSky's own setting):
   connecting the mount from NINA in daylight unparks it.

## 6. How to check and change each setting

| Program | Check | Change by hand | Change by script |
|---|---|---|---|
| NINA #1 / #2 profile (Options, camera toggles, guider settle/dither) | `GET :1888/v2/api/profile/show?active=true` (`:1889` for #2); ledger snapshot | the UI path in the rule's `fix` | `GET :1888/v2/api/profile/change-value?settingpath=<Section>-<Key>&newValue=<v>`. NINA saves the `.profile` file at once (`%LOCALAPPDATA%\NINA\Profiles\<id>.profile`; check it with grep). Only while no sequence runs |
| PHD2 JSON-RPC settings (exposure, algorithm params, Dec mode) | `get_*` on :4400 | Brain dialog | `set_exposure`, `set_algo_param`, `set_dec_guide_mode` (PS-89 apply) |
| PHD2 registry-only settings (section 3 table) | `phd2_profile_store.read(<id>)`, ledger | Brain dialog (PHD2 saves on OK) | With PHD2 **closed**: `phd2_profile_store.backup()`, then edit `HKCU\Software\StarkLabs\PHDGuidingV2\profile\<id>\<key>`. PHD2 rewrites the registry on exit, so an edit made while it runs is lost. The `write()` gate stays closed (`WRITABLE` empty) until a write is verified |

Changes made through ninaAPI on 2026-11-02, while DISARMED with backups of
both `.profile` files kept:
- NINA #1 `GuiderSettings-SettlePixels` 5 to 2.6
- NINA #1 `TelescopeSettings-NoSync` false to true
