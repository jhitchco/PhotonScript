// PhotonScript OSC (one-shot-color) integration pipeline (PJSR)
// Launched by run-integration-osc.ps1. Companion to integrate_sho.js (mono).
//
// Layout expected (from prepare-integration-osc.ps1):
//   STAGING: LIGHTS.OSC (CFA .fits), DARKS, BIAS, FLATS.OSC   (all optional but LIGHTS)
// Output: out.master.masterOSC.xisf  (+ masterOSC_review.jpg) and intermediates.
//
// Design notes (see docs/HANDBOOK.md sec 6 for PJSR lessons):
//   - Pure ASCII; never put a slash-star sequence inside a line comment (the PI
//     preprocessor would open a block comment). Inside string globs it is fine.
//   - Soft/eccentric subs are KEPT and down-weighted via ImageIntegration PSF
//     Signal Weight (no SubframeSelector: its scripted -pxm.dll access-violates on
//     this PI build). A slightly blurred sub contributes less, none are dropped.
//   - StarAlignment.distortionCorrection = true: the 600mm rig's field drifts and
//     rotates ~0.16 deg/night, which a rigid transform cannot remove. The local
//     distortion model (thin-plate splines) fixes the off-center star trailing.
//   - Every run starts by CLEARING its own intermediate folders (cal, cc,
//     debayer, reg, ln). 2026-09-25 M31_OSC2: stale _cc_d files from an earlier
//     run were re-listed, so 63 subs went in as 125 (each twice, one copy with a
//     CosmeticCorrection hole in the M31 core). A funnel check now throws if any
//     stage outputs more frames than it was given.
//   - Split-pointing subs (mount moved mid-exposure) are culled BEFORE this
//     script by photonscript/image_processor/osc_cull.py, which also writes
//     reference.txt (sharpest kept sub) used as the StarAlignment reference.

#include <pjsr/DataType.jsh>

var STAGING = "__STAGING__";
var OUT = STAGING + "/out";
var CFA = "RGGB";

function listFits(dir) {
   var f = searchDirectory(dir + "/*.fits", false)
           .concat(searchDirectory(dir + "/*.xisf", false));
   f.sort();
   return f;
}
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
var LOGLINES = [];
function writeLog() {
   try {
      ensureDir(OUT);
      var f = new File;
      f.createForWriting(OUT + "/pipeline.log");
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("log write failed: " + e); }
}
function log(s) {
   console.noteln("<b>[OSC]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   writeLog();
}

// Average-integrate calibration frames (bias/dark). No stars -> equal weights,
// no PSF/SNR re-judging (would throw on star-poor frames).
function integrateCal(files, id) {
   log("integrate " + id + ": " + files.length + " frames");
   var II = new ImageIntegration;
   II.images = files.map(function (f) { return [true, f, "", ""]; });
   II.combination = ImageIntegration.prototype.Average;
   II.weightMode = ImageIntegration.prototype.DontCare;
   II.minWeight = 0.0;
   II.generateIntegratedImage = true;
   II.generateRejectionMaps = false;
   II.rejection = files.length >= 15
      ? ImageIntegration.prototype.WinsorizedSigmaClip
      : (files.length >= 8 ? ImageIntegration.prototype.SigmaClip
                           : ImageIntegration.prototype.PercentileClip);
   II.normalization = ImageIntegration.prototype.NoNormalization;
   II.rejectionNormalization = ImageIntegration.prototype.NoRejectionNormalization;
   II.evaluateSNR = false;
   if (!II.executeGlobal())
      throw new Error("ImageIntegration failed for " + id);
   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var path = OUT + "/master/" + id + ".xisf";
   w.saveAs(path, false, false, false, false);
   w.forceClose();
   log("master saved: " + path);
   return path;
}

// Master flat from CFA flats (bias-calibrated), multiplicative/equalize-fluxes.
function integrateFlats(files, masterBias) {
   var cal = files;
   if (masterBias) {
      var IC = new ImageCalibration;
      IC.targetFrames = files.map(function (f) { return [true, f]; });
      IC.masterBiasEnabled = true; IC.masterBiasPath = masterBias;
      IC.masterDarkEnabled = false; IC.masterFlatEnabled = false;
      IC.outputDirectory = OUT + "/flatcal"; ensureDir(IC.outputDirectory);
      IC.outputExtension = ".xisf"; IC.overwriteExistingFiles = true;
      if (!IC.executeGlobal()) throw new Error("flat calibration failed");
      cal = listFits(IC.outputDirectory);
   }
   var II = new ImageIntegration;
   II.images = cal.map(function (f) { return [true, f, "", ""]; });
   II.combination = ImageIntegration.prototype.Average;
   II.weightMode = ImageIntegration.prototype.DontCare;
   II.minWeight = 0.0; II.evaluateSNR = false;
   II.rejection = ImageIntegration.prototype.PercentileClip;
   II.normalization = ImageIntegration.prototype.Multiplicative;
   II.rejectionNormalization = ImageIntegration.prototype.EqualizeFluxes;
   if (!II.executeGlobal()) throw new Error("flat integration failed");
   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var path = OUT + "/master/masterFlat_OSC.xisf";
   w.saveAs(path, false, false, false, false); w.forceClose();
   log("master flat saved: " + path);
   return path;
}

// Remove this pipeline's own intermediates so nothing stale is re-listed.
function clearDir(d) {
   if (!File.directoryExists(d)) return 0;
   var n = 0;
   var globs = ["*.xisf", "*.xdrz", "*.xnml", "*.fits", "*.fit"];
   for (var g = 0; g < globs.length; ++g) {
      var fs = searchDirectory(d + "/" + globs[g], false);
      for (var i = 0; i < fs.length; ++i) { File.remove(fs[i]); ++n; }
   }
   return n;
}

// Funnel guard: a stage may drop frames but must never ADD them.
function assertFunnel(stage, nIn, nOut) {
   if (nOut > nIn)
      throw new Error(stage + " produced " + nOut + " frames from " + nIn +
                      " inputs - stale files in the output folder?");
}

// Strip pipeline suffixes: <base>[_c][_cc]_d[_r] -> <base>
function baseOf(path) {
   var n = File.extractName(path);
   var sfx = ["_r", "_d", "_cc", "_c"];
   for (var i = 0; i < sfx.length; ++i)
      if (n.length > sfx[i].length && n.substr(n.length - sfx[i].length) === sfx[i])
         n = n.substr(0, n.length - sfx[i].length);
   return n;
}

function findByBase(files, base) {
   for (var i = 0; i < files.length; ++i) if (baseOf(files[i]) === base) return files[i];
   return null;
}

// Registration reference: reference.txt (written by osc_cull.py = sharpest kept
// sub) if present and matched, else the mid-stack frame.
function pickReference(rgbFiles) {
   var rf = STAGING + "/reference.txt";
   if (File.exists(rf)) {
      try {
         var lines = File.readLines(rf);
         var want = File.extractName(String(lines[0]).trim());
         var hit = findByBase(rgbFiles, want);
         if (hit) { log("reference (osc_cull sharpest): " + File.extractName(hit)); return hit; }
         log("reference.txt names " + want + " but no debayered match; using mid-stack");
      } catch (e) { log("reference.txt unreadable (" + e + "); using mid-stack"); }
   }
   return rgbFiles[Math.floor(rgbFiles.length / 2)];
}

function logDrops(stage, inputs, outputs) {
   var out = [];
   for (var j = 0; j < outputs.length; ++j) out.push(File.extractName(outputs[j]));
   var lost = 0;
   for (var i = 0; i < inputs.length; ++i) {
      var n = File.extractName(inputs[i]); var ok = false;
      for (var j = 0; j < out.length; ++j) if (out[j].indexOf(n) === 0) { ok = true; break; }
      if (!ok) { log("  DROPPED at " + stage + ": " + n); lost++; }
   }
   return lost;
}

function mtfv(m, x) { if (x <= 0) return 0; if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m); }

// UNLINKED STF-style autostretch (one MTF per channel). The uncalibrated OSC
// master carries a strong color cast; a linked stretch paints the whole review
// olive and hides real color problems. Per-channel neutralizes the background
// so the review shows structure and gradients honestly. Falls back to linked.
function autoStretchRGB(view) {
   var img = view.image;
   var rows = [];
   try {
      for (var c = 0; c < 3; ++c) {
         img.selectedChannel = c;
         var med = img.median();
         var mad = img.MAD() * 1.4826;
         var c0 = Math.max(0, Math.min(1, med - 2.8 * mad));
         rows.push([c0, mtfv(0.15, Math.max(1.0e-6, med - c0)), 1, 0, 1]);
      }
      img.resetSelections();
   } catch (e) {
      img.resetSelections();
      var lmed = img.median(), lmad = img.MAD() * 1.4826;
      var l0 = Math.max(0, Math.min(1, lmed - 2.8 * lmad));
      var lm = mtfv(0.15, Math.max(1.0e-6, lmed - l0));
      rows = [[l0, lm, 1, 0, 1], [l0, lm, 1, 0, 1], [l0, lm, 1, 0, 1]];
   }
   var HT = new HistogramTransformation;
   HT.H = [rows[0], rows[1], rows[2], [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1]];
   HT.executeOn(view, false);
}

function cropBorders(view, frac) {
   var img = view.image;
   var dx = Math.round(img.width * frac), dy = Math.round(img.height * frac);
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -dx; CR.rightMargin = -dx; CR.topMargin = -dy; CR.bottomMargin = -dy;
   CR.executeOn(view, false);
}

function main() {
   console.show();
   log("staging: " + STAGING + "  CFA=" + CFA);
   ensureDir(OUT);
   var stale = 0;
   ["flatcal", "cal", "cc", "debayer", "reg", "ln"].forEach(function (d) {
      stale += clearDir(OUT + "/" + d); });
   if (stale) log("cleared " + stale + " stale intermediate files from previous runs");

   // 1) calibration masters (all optional)
   var biasFiles = listFits(STAGING + "/BIAS");
   var darkFiles = listFits(STAGING + "/DARKS");
   var masterBias = biasFiles.length ? integrateCal(biasFiles, "masterBias") : null;
   var masterDark = darkFiles.length ? integrateCal(darkFiles, "masterDark") : null;
   var flatDir = STAGING + "/FLATS/OSC";
   var masterFlat = (File.directoryExists(flatDir) && listFits(flatDir).length)
      ? integrateFlats(listFits(flatDir), masterBias) : null;
   log("bias=" + biasFiles.length + " dark=" + darkFiles.length +
       " flat=" + (masterFlat ? "yes" : "no"));

   // 2) lights
   var lightDir = STAGING + "/LIGHTS/OSC";
   if (!File.directoryExists(lightDir) || !listFits(lightDir).length)
      lightDir = STAGING + "/LIGHTS";
   if (!File.directoryExists(lightDir) || !listFits(lightDir).length)
      lightDir = STAGING;   // loose culled fits dropped straight in the folder
   var lights = listFits(lightDir);
   if (!lights.length) throw new Error("no OSC lights under " + STAGING + "/LIGHTS");
   log("=== OSC: " + lights.length + " lights ===");

   // 3) calibrate CFA lights (pedestal guards a higher-offset dark, see HANDBOOK)
   var work = lights;
   if (masterBias || masterDark || masterFlat) {
      var IC = new ImageCalibration;
      IC.targetFrames = lights.map(function (f) { return [true, f]; });
      IC.masterBiasEnabled = !!masterBias; if (masterBias) IC.masterBiasPath = masterBias;
      IC.masterDarkEnabled = !!masterDark;
      if (masterDark) { IC.masterDarkPath = masterDark; IC.optimizeDarks = true; }
      IC.masterFlatEnabled = !!masterFlat; if (masterFlat) IC.masterFlatPath = masterFlat;
      IC.outputPedestal = 1000;
      if (ImageCalibration.prototype.OutputPedestal_Literal !== undefined)
         IC.outputPedestalMode = ImageCalibration.prototype.OutputPedestal_Literal;
      IC.outputDirectory = OUT + "/cal"; ensureDir(IC.outputDirectory);
      IC.outputExtension = ".xisf"; IC.overwriteExistingFiles = true;
      if (!IC.executeGlobal()) throw new Error("calibration failed");
      work = listFits(IC.outputDirectory);
      assertFunnel("calibration", lights.length, work.length);
      logDrops("calibration", lights, work);
   } else {
      log("no masters staged -> UNCALIBRATED run (debayer + register + integrate only)");
   }

   // 4) cosmetic correction - only with a master dark. Uncalibrated, auto hot-
   //    pixel detection mistakes bright COMPACT sources (galaxy nuclei, M32,
   //    saturated star cores) for hot-pixel clusters and punches dark holes with
   //    color fringing. Without a dark we skip it and let ImageIntegration's
   //    sigma rejection clean hot pixels across the stack instead. With a dark,
   //    most hot pixels are already gone, so CC runs gentle (hot only, sigma 5).
   var ccFiles;
   if (masterDark) {
      var CC = new CosmeticCorrection;
      CC.targetFrames = work.map(function (f) { return [true, f]; });
      CC.cfa = true; CC.useAutoDetect = true;
      CC.hotAutoCheck = true; CC.hotAutoValue = 5.0;
      CC.coldAutoCheck = false;
      CC.outputDir = OUT + "/cc"; ensureDir(CC.outputDir); CC.overwrite = true;
      if (!CC.executeGlobal()) throw new Error("cosmetic correction failed");
      ccFiles = listFits(CC.outputDir);
      assertFunnel("cosmetic", work.length, ccFiles.length);
      logDrops("cosmetic", work, ccFiles);
   } else {
      log("no master dark -> skipping CosmeticCorrection (sigma-clip integration "
          + "cleans hot pixels; avoids punching holes in bright compact sources)");
      ccFiles = work;
   }

   // 5) debayer RGGB -> RGB
   var DB = new Debayer;
   DB.cfaPattern = Debayer.prototype.RGGB;
   DB.debayerMethod = Debayer.prototype.VNG;
   DB.targetItems = ccFiles.map(function (f) { return [true, f]; });
   DB.outputDirectory = OUT + "/debayer"; ensureDir(DB.outputDirectory);
   DB.outputExtension = ".xisf"; DB.overwriteExistingFiles = true;
   if (!DB.executeGlobal()) throw new Error("debayer failed");
   var rgbFiles = listFits(DB.outputDirectory);
   assertFunnel("debayer", ccFiles.length, rgbFiles.length);
   log("debayered " + rgbFiles.length + " frames");

   // 6) (No SubframeSelector.) The scripted SubframeSelector-pxm.dll throws a
   //    native access violation on this PixInsight build (crashes right after
   //    'measure'), so instead of writing SSWEIGHT we let ImageIntegration
   //    weight by PSF Signal Weight below: it keeps every sub and down-weights
   //    the soft / low-SNR ones (blurred subs contribute less, none dropped).

   // 7) register with distortion correction to a good reference
   var refImage = pickReference(rgbFiles);
   var SA = new StarAlignment;
   SA.referenceImage = refImage; SA.referenceIsFile = true;
   SA.targets = rgbFiles.map(function (f) { return [true, true, f]; });
   SA.outputDirectory = OUT + "/reg"; ensureDir(SA.outputDirectory);
   SA.outputExtension = ".xisf"; SA.overwriteExistingFiles = true;
   SA.distortionCorrection = true;         // key fix for the wandering/rotating field
   SA.structureLayers = 5;
   SA.sensitivity = 0.60;
   SA.peakResponse = 0.50;
   SA.useTriangleSimilarity = true;
   if (!SA.executeGlobal()) throw new Error("registration failed");
   var regFiles = listFits(OUT + "/reg");
   assertFunnel("registration", rgbFiles.length, regFiles.length);
   logDrops("registration", rgbFiles, regFiles);
   log("funnel: " + lights.length + " staged -> " + rgbFiles.length +
       " debayered -> " + regFiles.length + " registered");
   if (regFiles.length < 3) throw new Error("too few registered frames");

   // 7b) LocalNormalization: evens out the gradient that changes through the
   //     night (M31 rising, sky brightening) before rejection. Uses the
   //     registered reference. Any failure falls back to global scaling.
   var lnData = null;
   try {
      var regRef = findByBase(regFiles, baseOf(refImage)) || regFiles[0];
      var LN = new LocalNormalization;
      LN.referencePathOrViewId = regRef;
      LN.referenceIsView = false;
      LN.targetItems = regFiles.map(function (f) { return [true, f]; });
      LN.outputDirectory = OUT + "/ln"; ensureDir(LN.outputDirectory);
      LN.overwriteExistingFiles = true;
      LN.generateNormalizationData = true;
      if (!LN.executeGlobal()) throw new Error("LocalNormalization returned false");
      var maps = [];
      for (var i = 0; i < regFiles.length; ++i) {
         var x = OUT + "/ln/" + File.extractName(regFiles[i]) + ".xnml";
         if (!File.exists(x)) throw new Error("missing " + x);
         maps.push(x);
      }
      lnData = maps;
      log("local normalization: " + maps.length + " maps (ref " + File.extractName(regRef) + ")");
   } catch (e) {
      log("local normalization skipped (" + e + "); using AdditiveWithScaling");
      lnData = null;
   }

   // 8) integrate, weighting by PSF Signal Weight (soft subs contribute less, not zero)
   var II = new ImageIntegration;
   II.images = regFiles.map(function (f, i) {
      return [true, f, "", lnData ? lnData[i] : ""]; });
   II.combination = ImageIntegration.prototype.Average;
   II.weightMode = ImageIntegration.prototype.PSFSignalWeight;
   II.minWeight = 0.0;
   II.generateIntegratedImage = true; II.generateRejectionMaps = false;
   II.rejection = regFiles.length >= 15
      ? ImageIntegration.prototype.WinsorizedSigmaClip
      : ImageIntegration.prototype.SigmaClip;
   if (lnData) {
      II.normalization = ImageIntegration.prototype.LocalNormalization;
      II.rejectionNormalization = ImageIntegration.prototype.LocalRejectionNormalization;
   } else {
      II.normalization = ImageIntegration.prototype.AdditiveWithScaling;
      II.rejectionNormalization = ImageIntegration.prototype.Scale;
   }
   II.evaluateSNR = true;
   if (!II.executeGlobal()) throw new Error("integration failed");
   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var masterPath = OUT + "/master/masterOSC.xisf";
   w.saveAs(masterPath, false, false, false, false);
   ["rejection_low", "rejection_high", "slope"].forEach(function (rid) {
      var rw = ImageWindow.windowById(rid); if (!rw.isNull) rw.forceClose(); });
   log("master saved: " + masterPath + " (" + regFiles.length + " frames)");

   // 9) stretched color review jpg
   cropBorders(w.mainView, 0.015);
   autoStretchRGB(w.mainView);
   w.saveAs(OUT + "/master/masterOSC_review.jpg", false, false, false, false);
   w.forceClose();
   log("review saved: " + OUT + "/master/masterOSC_review.jpg");
   log("DONE - master in " + OUT + "/master");
}

try { main(); log("EXIT OK"); }
catch (e) { log("ERROR: " + e.toString()); console.criticalln("[OSC] FAILED: " + e.toString()); }
writeLog();
