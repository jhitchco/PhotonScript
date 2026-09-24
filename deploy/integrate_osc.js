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
//   - Soft/eccentric subs are KEPT and down-weighted (SubframeSelector SSWEIGHT),
//     not hard-rejected: ImageIntegration weights by that keyword so a slightly
//     blurred sub still contributes signal instead of being thrown away.
//   - StarAlignment.distortionCorrection = true: the 600mm rig's field drifts and
//     rotates ~0.16 deg/night, which a rigid transform cannot remove. The local
//     distortion model (thin-plate splines) fixes the off-center star trailing.

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

// Linked STF-style autostretch baked into an RGB view (one MTF from luminance).
function autoStretchRGB(view) {
   var img = view.image;
   img.colorSpace = ColorSpace_CIEXYZ;   // measure on luminance
   var med = img.median(); var mad = img.MAD() * 1.4826;
   img.colorSpace = ColorSpace_RGB;
   var c0 = Math.max(0, Math.min(1, med - 2.8 * mad));
   var m = mtfv(0.15, Math.max(1.0e-6, med - c0));
   var HT = new HistogramTransformation;
   HT.H = [[c0, m, 1, 0, 1], [c0, m, 1, 0, 1], [c0, m, 1, 0, 1],
           [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1]];
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
      logDrops("calibration", lights, work);
   } else {
      log("no masters staged -> UNCALIBRATED run (debayer + register + integrate only)");
   }

   // 4) cosmetic correction on the CFA frames (hot/cold pixel cleanup)
   var CC = new CosmeticCorrection;
   CC.targetFrames = work.map(function (f) { return [true, f]; });
   CC.cfa = true;   // operate on the CFA (do not treat as debayered)
   CC.useAutoDetect = true; CC.hotAutoCheck = true; CC.hotAutoValue = 3.0;
   CC.coldAutoCheck = true; CC.coldAutoValue = 3.0;
   CC.outputDir = OUT + "/cc"; ensureDir(CC.outputDir); CC.overwrite = true;
   if (!CC.executeGlobal()) throw new Error("cosmetic correction failed");
   var ccFiles = listFits(CC.outputDir);
   logDrops("cosmetic", work, ccFiles);

   // 5) debayer RGGB -> RGB
   var DB = new Debayer;
   DB.cfaPattern = Debayer.prototype.RGGB;
   DB.debayerMethod = Debayer.prototype.VNG;
   DB.targetItems = ccFiles.map(function (f) { return [true, f]; });
   DB.outputDirectory = OUT + "/debayer"; ensureDir(DB.outputDirectory);
   DB.outputExtension = ".xisf"; DB.overwriteExistingFiles = true;
   if (!DB.executeGlobal()) throw new Error("debayer failed");
   var rgbFiles = listFits(DB.outputDirectory);
   log("debayered " + rgbFiles.length + " frames");

   // 6) SubframeSelector: measure + write SSWEIGHT (keep ALL, weight by quality).
   //    Soft/eccentric subs get a low weight but still contribute; only the
   //    integration's sigma rejection removes true outlier pixels.
   var SS = new SubframeSelector;
   SS.routine = SubframeSelector.prototype.MeasureSubframes;
   SS.subframes = rgbFiles.map(function (f) { return [true, f]; });
   SS.fileCache = true;
   SS.subframeScale = 1.293;               // arcsec/px (600mm, 3.76um)
   SS.scaleUnit = SubframeSelector.prototype.ArcSeconds;
   SS.cameraGain = 1.0;
   SS.cameraResolution = SubframeSelector.prototype.Bits16;
   SS.dataUnit = SubframeSelector.prototype.Electron;
   SS.pedestal = 0;
   SS.approvalExpression = "";             // keep every sub
   // 15 floor + reward low FWHM, low eccentricity, high SNR (normalized 0..1)
   SS.weightingExpression =
      "15" +
      " + 25*(1 - (FWHM - FWHMMin)/max(FWHMMax - FWHMMin, 1e-6))" +
      " + 25*(1 - (Eccentricity - EccentricityMin)/max(EccentricityMax - EccentricityMin, 1e-6))" +
      " + 35*((SNRWeight - SNRWeightMin)/max(SNRWeightMax - SNRWeightMin, 1e-6))";
   if (!SS.executeGlobal()) throw new Error("SubframeSelector measure failed");
   // write the weights into copies (SSWEIGHT keyword) for ImageIntegration
   SS.routine = SubframeSelector.prototype.OutputSubframes;
   SS.outputDirectory = OUT + "/weighted"; ensureDir(SS.outputDirectory);
   SS.outputExtension = ".xisf"; SS.overwriteExistingFiles = true;
   SS.outputKeyword = "SSWEIGHT";
   SS.outputPrefix = ""; SS.outputPostfix = "_w";
   if (!SS.executeGlobal()) throw new Error("SubframeSelector output failed");
   var wFiles = listFits(OUT + "/weighted");
   log("weighted " + wFiles.length + " frames (SSWEIGHT written)");
   if (!wFiles.length) wFiles = rgbFiles;  // fallback: unweighted

   // 7) register with distortion correction to a good reference
   var refImage = wFiles[Math.floor(wFiles.length / 2)];
   var SA = new StarAlignment;
   SA.referenceImage = refImage; SA.referenceIsFile = true;
   SA.targets = wFiles.map(function (f) { return [true, true, f]; });
   SA.outputDirectory = OUT + "/reg"; ensureDir(SA.outputDirectory);
   SA.outputExtension = ".xisf"; SA.overwriteExistingFiles = true;
   SA.distortionCorrection = true;         // key fix for the wandering/rotating field
   SA.structureLayers = 5;
   SA.sensitivity = 0.60;
   SA.peakResponse = 0.50;
   SA.useTriangleSimilarity = true;
   if (!SA.executeGlobal()) throw new Error("registration failed");
   var regFiles = listFits(OUT + "/reg");
   logDrops("registration", wFiles, regFiles);
   log("funnel: " + lights.length + " staged -> " + rgbFiles.length +
       " debayered -> " + regFiles.length + " registered");
   if (regFiles.length < 3) throw new Error("too few registered frames");

   // 8) integrate, weighting by SSWEIGHT (soft subs contribute less, not zero)
   var II = new ImageIntegration;
   II.images = regFiles.map(function (f) { return [true, f, "", ""]; });
   II.combination = ImageIntegration.prototype.Average;
   II.weightMode = ImageIntegration.prototype.KeywordWeight;
   II.weightKeyword = "SSWEIGHT";
   II.minWeight = 0.0;
   II.generateIntegratedImage = true; II.generateRejectionMaps = false;
   II.rejection = regFiles.length >= 15
      ? ImageIntegration.prototype.WinsorizedSigmaClip
      : ImageIntegration.prototype.SigmaClip;
   II.normalization = ImageIntegration.prototype.AdditiveWithScaling;
   II.rejectionNormalization = ImageIntegration.prototype.Scale;
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
