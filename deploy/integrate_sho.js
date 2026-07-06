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
   // NoiseEvaluation instead of PSF weighting for lights: PSF weights
   // silently EXCLUDE star-poor narrowband frames (SII used 8 of 16);
   // noise weighting keeps every registered frame with sane weights
   II.weightMode = isCal ? ImageIntegration.prototype.DontCare
                         : ImageIntegration.prototype.NoiseEvaluation;
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
      if (!SA.executeGlobal()) throw new Error("registration failed: " + filt);
      var regFiles = listFits(SA.outputDirectory);

      integrate(regFiles, "masterLight_" + filt, false);
   }
   log("DONE - masters in " + OUT + "/master (open them and autostretch)");
}

try {
   main();
   log("EXIT OK");
} catch (e) {
   log("ERROR: " + e.toString());
   console.criticalln("[SHO] FAILED: " + e.toString());
}
writeLog();
