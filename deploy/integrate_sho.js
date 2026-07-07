// PhotonScript SHO integration pipeline (PJSR) - launched by run-integration.ps1
// Layout expected (from prepare-integration.ps1):
//   STAGING: LIGHTS.<Filter>, DARKS, BIAS, FLATS.<Filter>  (fits files)
// Output: out.master.master_<Filter>.xisf  (+ intermediate cal, cc, reg)

#include <pjsr/DataType.jsh>

var STAGING = "__STAGING__";
var OUT = STAGING + "/out";

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
   console.noteln("<b>[SHO]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   writeLog();  // flush after every step so a crash still leaves evidence
}

function integrate(files, id, isCal, rejectHigh) {
   // isCal: bias/dark/flat masters (no normalization for bias/dark)
   log("integrate " + id + ": " + files.length + " frames, first=" +
       File.extractName(files[0]));
   var II = new ImageIntegration;
   II.images = files.map(function (f) { return [true, f, "", ""]; });
   II.combination = ImageIntegration.prototype.Average;
   // bias/darks/flats have no stars: PSF-based weighting (the modern
   // default) fails instantly on them — weight them equally instead
   // Equal weights for everything: PhotonScript's QA already graded and
   // culled these frames upstream. Both PSF weighting (dropped star-poor
   // SII) and noise weighting (computed 1e-6 relative weights and then
   // excluded ALL frames via the 0.005 minWeight floor) re-judge frames
   // we already judged. minWeight=0 disables the exclusion floor.
   II.weightMode = ImageIntegration.prototype.DontCare;
   II.minWeight = 0.0;
   II.generateIntegratedImage = true;
   II.generateRejectionMaps = false;
   II.rejection = files.length >= 15
      ? ImageIntegration.prototype.WinsorizedSigmaClip
      : (files.length >= 8 ? ImageIntegration.prototype.SigmaClip
                           : ImageIntegration.prototype.PercentileClip);
   II.normalization = isCal
      ? ImageIntegration.prototype.NoNormalization
      : ImageIntegration.prototype.AdditiveWithScaling;
   II.rejectionNormalization = isCal
      ? ImageIntegration.prototype.NoRejectionNormalization
      : ImageIntegration.prototype.Scale;
   II.generateDrizzleData = false;
   II.evaluateSNR = !isCal;
   if (rejectHigh !== undefined) II.sigmaHigh = rejectHigh;
   var ok = II.executeGlobal();
   log("executeGlobal(" + id + ") = " + ok);
   if (!ok)
      throw new Error("ImageIntegration failed for " + id +
                      " (see PixInsight console for the validation reason)");
   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var path = OUT + "/master/" + id + ".xisf";
   w.saveAs(path, false, false, false, false);
   w.forceClose();
   // close rejection maps if any
   ["rejection_low", "rejection_high", "slope"].forEach(function (rid) {
      var rw = ImageWindow.windowById(rid);
      if (!rw.isNull) rw.forceClose();
   });
   log("master saved: " + path + "  (" + files.length + " frames)");
   return path;
}

function integrateFlats(files, id, masterBias) {
   // calibrate flats with bias, then multiplicative integration
   var cal = files;
   if (masterBias) {
      var IC = new ImageCalibration;
      IC.targetFrames = files.map(function (f) { return [true, f]; });
      IC.masterBiasEnabled = true;  IC.masterBiasPath = masterBias;
      IC.masterDarkEnabled = false; IC.masterFlatEnabled = false;
      IC.outputDirectory = OUT + "/flatcal_" + id; ensureDir(IC.outputDirectory);
      IC.outputExtension = ".xisf"; IC.overwriteExistingFiles = true;
      if (!IC.executeGlobal()) throw new Error("flat calibration failed: " + id);
      cal = listFits(IC.outputDirectory);
   }
   var II = new ImageIntegration;
   II.images = cal.map(function (f) { return [true, f, "", ""]; });
   II.combination = ImageIntegration.prototype.Average;
   // flats have no stars either — same fix as integrate(): equal weights,
   // no PSF/SNR re-judging, or PixInsight's default PSFSignalWeight throws
   // "Zero or insignificant PSF Signal Weight estimate" and fails the master.
   II.weightMode = ImageIntegration.prototype.DontCare;
   II.minWeight = 0.0;
   II.evaluateSNR = false;
   II.rejection = ImageIntegration.prototype.PercentileClip;
   II.normalization = ImageIntegration.prototype.Multiplicative;
   II.rejectionNormalization = ImageIntegration.prototype.EqualizeFluxes;
   if (!II.executeGlobal()) throw new Error("flat integration failed: " + id);
   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var path = OUT + "/master/masterFlat_" + id + ".xisf";
   w.saveAs(path, false, false, false, false); w.forceClose();
   log("master flat saved: " + path);
   return path;
}


// crop stacking borders (unguided drift means each filter covers a
// slightly different footprint; edges are single-channel color fringes)
function cropBorders(view, frac) {
   var img = view.image;
   var dx = Math.round(img.width * frac);
   var dy = Math.round(img.height * frac);
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -dx; CR.rightMargin = -dx;
   CR.topMargin = -dy;  CR.bottomMargin = -dy;
   CR.executeOn(view, false);
}

function mtfv(m, x) {
   if (x <= 0) return 0;
   if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m);
}

// STF-style autostretch baked into the pixels of a GRAYSCALE view
function autoStretchGray(view) {
   var img = view.image;
   var med = img.median();
   var mad = img.MAD() * 1.4826;
   var c0 = Math.max(0, Math.min(1, med - 2.8 * mad));
   var m = mtfv(0.12, Math.max(1.0e-6, med - c0));
   var HT = new HistogramTransformation;
   HT.H = [[0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1],
           [c0, m, 1, 0, 1], [0, 0.5, 1, 0, 1]];
   HT.executeOn(view, false);
}

// single review image: R=SII, G=Ha, B=OIII, each channel autostretched,
// saved as masterSHO_review.xisf + .jpg next to the masters
function makeSHOReview() {
   var mdir = OUT + "/master/";
   var need = ["masterLight_SII", "masterLight_Ha", "masterLight_OIII"];
   for (var i = 0; i < need.length; ++i) {
      if (!File.exists(mdir + need[i] + ".xisf")) {
         log("SHO review skipped - missing " + need[i]);
         return;
      }
   }
   log("building masterSHO review (R=SII, G=Ha, B=OIII)...");
   var wins = [];
   for (var i = 0; i < need.length; ++i) {
      var w = ImageWindow.open(mdir + need[i] + ".xisf")[0];
      w.mainView.id = "SHO_ch" + i;
      cropBorders(w.mainView, 0.015);
      autoStretchGray(w.mainView);
      wins.push(w);
   }
   var ref = wins[0].mainView.image;
   var out = new ImageWindow(ref.width, ref.height, 3, 32, true, true, "masterSHO");
   var CB = new ChannelCombination;
   CB.colorSpace = ChannelCombination.prototype.RGB;
   CB.channels = [[true, "SHO_ch0"], [true, "SHO_ch1"], [true, "SHO_ch2"]];
   CB.executeOn(out.mainView, false);
   out.saveAs(mdir + "masterSHO_review.xisf", false, false, false, false);
   out.saveAs(mdir + "masterSHO_review.jpg", false, false, false, false);
   log("SHO review saved: " + mdir + "masterSHO_review.jpg");
   for (var i = 0; i < wins.length; ++i) wins[i].forceClose();
   out.forceClose();
}

function main() {
   console.show();
   log("staging: " + STAGING);
   ensureDir(OUT);

   var biasFiles = listFits(STAGING + "/BIAS");
   var darkFiles = listFits(STAGING + "/DARKS");
   var masterBias = biasFiles.length ? integrate(biasFiles, "masterBias", true) : null;
   var masterDark = darkFiles.length ? integrate(darkFiles, "masterDark", true) : null;
   log("bias: " + biasFiles.length + " - darks: " + darkFiles.length);

   var lightRoot = STAGING + "/LIGHTS";
   // searchDirectory matches FILES, not directories — probe known filters
   var FILTER_NAMES = ["Ha", "OIII", "SII", "L", "R", "G", "B",
                       "H", "O", "S", "UNKNOWN"];
   var filterDirs = [];
   for (var fi = 0; fi < FILTER_NAMES.length; ++fi) {
      var p = lightRoot + "/" + FILTER_NAMES[fi];
      if (File.directoryExists(p) && listFits(p).length > 0)
         filterDirs.push(p);
   }
   if (filterDirs.length == 0) throw new Error("no LIGHTS filter folders "
      + "found under " + lightRoot);

   var refImage = null;
   for (var i = 0; i < filterDirs.length; ++i) {
      var filt = filterDirs[i].split('/').pop();
      var lights = listFits(filterDirs[i]);
      if (!lights.length) continue;
      log("=== " + filt + ": " + lights.length + " lights ===");

      // per-filter master flat if staged
      var flatDir = STAGING + "/FLATS/" + filt;
      var masterFlat = File.directoryExists(flatDir) && listFits(flatDir).length
         ? integrateFlats(listFits(flatDir), filt, masterBias) : null;

      // calibrate
      var IC = new ImageCalibration;
      IC.targetFrames = lights.map(function (f) { return [true, f]; });
      IC.masterBiasEnabled = !!masterBias;
      if (masterBias) IC.masterBiasPath = masterBias;
      IC.masterDarkEnabled = !!masterDark;
      if (masterDark) { IC.masterDarkPath = masterDark; IC.optimizeDarks = true; }
      IC.masterFlatEnabled = !!masterFlat;
      if (masterFlat) IC.masterFlatPath = masterFlat;
      // add a fixed pedestal so a master dark with a HIGHER offset than the
      // lights (loose epoch match) cannot clip the background to zero. 1000 DN
      // at 16 bits = 0.0153 normalized; harmless constant, removed at stretch.
      IC.outputPedestal = 1000;
      if (ImageCalibration.prototype.OutputPedestal_Literal !== undefined)
         IC.outputPedestalMode = ImageCalibration.prototype.OutputPedestal_Literal;
      IC.outputDirectory = OUT + "/cal/" + filt; ensureDir(IC.outputDirectory);
      IC.outputExtension = ".xisf"; IC.overwriteExistingFiles = true;
      if (!IC.executeGlobal()) throw new Error("calibration failed: " + filt);
      var calFiles = listFits(IC.outputDirectory);

      // cosmetic correction (dark substitute / hot pixel cleanup)
      var CC = new CosmeticCorrection;
      CC.targetFrames = calFiles.map(function (f) { return [true, f]; });
      CC.useAutoDetect = true; CC.hotAutoCheck = true; CC.hotAutoValue = 3.0;
      CC.coldAutoCheck = true; CC.coldAutoValue = 3.0;
      CC.outputDir = OUT + "/cc/" + filt; ensureDir(CC.outputDir);
      CC.overwrite = true;
      if (!CC.executeGlobal()) throw new Error("cosmetic correction failed: " + filt);
      var ccFiles = listFits(CC.outputDir);

      if (!refImage) refImage = ccFiles[Math.floor(ccFiles.length / 2)];

      // register to the shared reference
      var SA = new StarAlignment;
      SA.referenceImage = refImage; SA.referenceIsFile = true;
      SA.targets = ccFiles.map(function (f) { return [true, true, f]; });
      SA.outputDirectory = OUT + "/reg/" + filt; ensureDir(SA.outputDirectory);
      SA.outputExtension = ".xisf"; SA.overwriteExistingFiles = true;
      // narrowband (esp. SII) is star-poor: the default detector finds <3
      // stars and every frame fails to register. Make it more sensitive.
      SA.structureLayers = 6;             // span more star scales
      SA.noiseReductionFilterRadius = 2;  // damp narrowband noise false-positives
      SA.sensitivity = 0.85;              // >0.5 = detect fainter stars
      SA.peakResponse = 0.40;             // lower = less selective, keeps faint stars
      SA.useTriangleSimilarity = true;    // robust matching when few stars exist
      if (!SA.executeGlobal()) throw new Error("registration failed: " + filt);
      var regFiles = listFits(SA.outputDirectory);

      // don't let one star-poor filter abort the whole run — skip if <3 frames
      // survived alignment (ImageIntegration needs >=3). Ha/OIII masters already
      // saved above are preserved; the aligned frames stay on disk for manual work.
      if (regFiles.length < 3) {
         log("WARNING: " + filt + " has only " + regFiles.length +
             " registered frame(s) after alignment - skipping integration " +
             "(need >=3). Registered data kept in " + OUT + "/reg/" + filt);
         continue;
      }
      integrate(regFiles, "masterLight_" + filt, false);
   }
   makeSHOReview();
   log("DONE - masters in " + OUT + "/master (single review image: masterSHO_review.jpg)");
}

try {
   main();
   log("EXIT OK");
} catch (e) {
   log("ERROR: " + e.toString());
   console.criticalln("[SHO] FAILED: " + e.toString());
}
writeLog();
