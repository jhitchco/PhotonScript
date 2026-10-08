// PhotonScript library integration (PJSR), PS-22. Template: `photonscript
// integrate` replaces the CONFIG line below with the run's settings and
// writes the result into the run folder as integrate_run.js. Do not run this
// template directly.
//
// Generalized from Staging/M31_OSC4/integrate_osc4_v4b.js (PS-109):
//   masters: bias (average), one master dark per dark length, one master
//   flat per filter (bias-calibrated, multiplicative)
//   per stack (OSC: one stack, every exposure group; mono: one per filter):
//     ImageCalibration per exposure group (bias + dark, optimizeDarks when
//     the dark length differs, flat if matched, 1000 DN literal pedestal)
//     -> CosmeticCorrection (hot only after a dark) -> Debayer (CFA only)
//     -> StarAlignment with distortion correction to ONE reference shared by
//     every stack -> LocalNormalization (drops only frames without a map;
//     falls back to AdditiveWithScaling) -> ImageIntegration with PSF Signal
//     Weight (falls back to equal weights on star-poor frames; PS-177: a
//     stack that mixes exposure lengths falls back to exposure-time weights,
//     never to equal weights; mono stacks hold one length each) and
//     Winsorized sigma clipping -> master_<stack>.xisf (+ masterOSC.xisf for
//     an OSC stack) and a review jpg
// Funnel checks: a stage may drop frames but never add them; every dropped
// frame is logged as "DROPPED at <stage>: <file>" (the packet reads these).
// Stage timing goes to out/timing_pi.csv; the log ends EXIT OK or ERROR.
//
// PJSR rules (HANDBOOK sec 6): pure ASCII; no slash-star sequence inside a
// line comment; clear own intermediates per run; no SubframeSelector; clear
// the astrometric solution before any Crop.

#include <pjsr/DataType.jsh>

//__CONFIG__

var STAGING = CONFIG.staging;
var OUT = CONFIG.out;
var LOGFILE = OUT + "/pipeline.log";
var TIMINGFILE = OUT + "/timing_pi.csv";

function listFits(dir) {
   if (!File.directoryExists(dir)) return [];
   var f = searchDirectory(dir + "/*.fits", false)
           .concat(searchDirectory(dir + "/*.fit", false))
           .concat(searchDirectory(dir + "/*.xisf", false));
   f.sort();
   return f;
}
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
var LOGLINES = [];
var T0 = Date.now();
function writeText(path, lines) {
   try {
      var f = new File; f.createForWriting(path);
      for (var i = 0; i < lines.length; ++i) f.outTextLn(lines[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + path + " (" + e + ")"); }
}
function log(s) {
   console.noteln("<b>[STACK]</b> " + s); console.flush();
   var mins = ((Date.now() - T0) / 60000).toFixed(1);
   LOGLINES.push((new Date).toISOString() + "  [+" + mins + "m]  " + s);
   ensureDir(OUT);
   writeText(LOGFILE, LOGLINES);
}
var TIMING = ["scope,stage,start_utc,end_utc,minutes"];
var TOPEN = {};
function TSTART(scope, stage) {
   TOPEN[scope + "|" + stage] = new Date;
   log("STAGE START " + scope + " / " + stage);
}
function TEND(scope, stage) {
   var k = scope + "|" + stage, t1 = new Date, t0 = TOPEN[k] || t1;
   var m = ((t1.getTime() - t0.getTime()) / 60000).toFixed(2);
   TIMING.push(scope + "," + stage + "," + t0.toISOString() + "," + t1.toISOString() + "," + m);
   log("STAGE END   " + scope + " / " + stage + " (" + m + " min)");
   writeText(TIMINGFILE, TIMING);
}

function assertFunnel(stage, nIn, nOut) {
   if (nOut > nIn)
      throw new Error(stage + " produced " + nOut + " frames from " + nIn +
                      " inputs - stale files in the output folder?");
}

function clearDir(d) {
   if (!File.directoryExists(d)) return 0;
   var n = 0;
   var globs = ["*.xisf", "*.xdrz", "*.xnml", "*.fits", "*.fit", "*.csv", "*.jpg"];
   for (var g = 0; g < globs.length; ++g) {
      var fs = searchDirectory(d + "/" + globs[g], false);
      for (var i = 0; i < fs.length; ++i) { File.remove(fs[i]); ++n; }
   }
   return n;
}

// Strip pipeline suffixes: <base>[_c][_cc][_d][_r] -> <base>
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
function logDrops(stage, inputs, outputs) {
   var outB = {};
   for (var j = 0; j < outputs.length; ++j) outB[baseOf(outputs[j])] = true;
   var lost = 0;
   for (var i = 0; i < inputs.length; ++i)
      if (!outB[baseOf(inputs[i])]) { log("  DROPPED at " + stage + ": " + File.extractName(inputs[i])); ++lost; }
   return lost;
}
function groupOf(stack, path) {
   var n = File.extractName(path);
   for (var g = 0; g < stack.groups.length; ++g)
      if (n.indexOf(stack.groups[g].tag) >= 0) return stack.groups[g].name;
   return "?";
}
function groupCounts(stack, files) {
   var c = {};
   for (var i = 0; i < files.length; ++i) { var g = groupOf(stack, files[i]); c[g] = (c[g] || 0) + 1; }
   var s = [];
   for (var k in c) s.push(k + "=" + c[k]);
   return s.join(" ");
}
function median(a) {
   if (!a.length) return NaN;
   var b = a.slice(0).sort(function (x, y) { return x - y; });
   var m = Math.floor(b.length / 2);
   return b.length % 2 ? b[m] : 0.5 * (b[m - 1] + b[m]);
}
function fmtArr(a, d) {
   try {
      var s = [];
      for (var i = 0; i < a.length; ++i) s.push(Number(a[i]).toFixed(d));
      return s.join(" / ");
   } catch (e) { return String(a); }
}
function statLine(path) {
   try {
      var ws = ImageWindow.open(path);
      if (!ws.length) return "unreadable";
      var img = ws[0].mainView.image;
      var s = "median " + (img.median() * 65535).toFixed(2) + " DN, MAD " + (img.MAD() * 65535).toFixed(3) + " DN";
      ws[0].forceClose();
      return s;
   } catch (e) { return "stats failed (" + e + ")"; }
}
function mtfv(m, x) { if (x <= 0) return 0; if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m); }

// Clear any astrometric solution first: Crop on a solved image raises a
// Yes/No dialog that stalls an unattended run.
function clearWcs(view) {
   try { view.window.clearAstrometricSolution(); } catch (e) {}
}
function cropBorders(view, frac) {
   clearWcs(view);
   var img = view.image;
   var dx = Math.round(img.width * frac), dy = Math.round(img.height * frac);
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -dx; CR.rightMargin = -dx; CR.topMargin = -dy; CR.bottomMargin = -dy;
   CR.executeOn(view, false);
}
function autoStretch(view) {
   var img = view.image;
   var rows = [];
   var nc = img.isColor ? 3 : 1;
   try {
      for (var c = 0; c < nc; ++c) {
         img.selectedChannel = c;
         var med = img.median(), mad = img.MAD() * 1.4826;
         var c0 = Math.max(0, Math.min(1, med - 2.8 * mad));
         rows.push([c0, mtfv(0.15, Math.max(1.0e-6, med - c0)), 1, 0, 1]);
      }
      img.resetSelections();
   } catch (e) {
      img.resetSelections();
      rows = [];
   }
   var HT = new HistogramTransformation;
   var id = [0, 0.5, 1, 0, 1];
   if (nc === 3 && rows.length === 3) HT.H = [rows[0], rows[1], rows[2], id, id];
   else if (rows.length) HT.H = [id, id, id, rows[0], id];
   else return;
   HT.executeOn(view, false);
}

// ---------------------------------------------------------------- masters

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
   if (!II.executeGlobal()) throw new Error("ImageIntegration failed for " + id);
   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var path = OUT + "/master/" + id + ".xisf";
   w.saveAs(path, false, false, false, false);
   w.forceClose();
   log("master saved: " + path + "  (" + statLine(path) + ")");
   return path;
}

function integrateFlat(flat, masterBias) {
   var files = listFits(STAGING + "/" + flat.dir);
   if (files.length < 3) { log("flats " + flat.filter + ": only " + files.length + " frames; no master flat"); return null; }
   var calDir = OUT + "/flatcal/" + flat.filter;
   ensureDir(calDir); clearDir(calDir);
   var cal = files;
   if (masterBias) {
      var IC = new ImageCalibration;
      IC.targetFrames = files.map(function (f) { return [true, f]; });
      if (CONFIG.cfa) { try { IC.enableCFA = true; } catch (e0) {} }
      IC.masterBiasEnabled = true; IC.masterBiasPath = masterBias;
      IC.masterDarkEnabled = false; IC.masterFlatEnabled = false;
      IC.outputPedestal = 0;
      IC.outputDirectory = calDir;
      IC.outputExtension = ".xisf"; IC.overwriteExistingFiles = true;
      if (!IC.executeGlobal()) throw new Error("flat calibration failed (" + flat.filter + ")");
      cal = listFits(calDir);
      assertFunnel("flat calibration " + flat.filter, files.length, cal.length);
   } else log("flats " + flat.filter + ": no master bias; integrating raw flats");
   var II = new ImageIntegration;
   II.images = cal.map(function (f) { return [true, f, "", ""]; });
   II.combination = ImageIntegration.prototype.Average;
   II.weightMode = ImageIntegration.prototype.DontCare;
   II.minWeight = 0.0; II.evaluateSNR = false;
   II.rejection = ImageIntegration.prototype.PercentileClip;
   II.normalization = ImageIntegration.prototype.Multiplicative;
   II.rejectionNormalization = ImageIntegration.prototype.EqualizeFluxes;
   II.generateRejectionMaps = false;
   if (!II.executeGlobal()) throw new Error("flat integration failed (" + flat.filter + ")");
   var w = ImageWindow.windowById("integration");
   var path = OUT + "/master/masterFlat_" + flat.filter + ".xisf";
   w.saveAs(path, false, false, false, false); w.forceClose();
   log("master flat saved: " + path + " from " + cal.length + " flats  (" + statLine(path) + ")");
   return path;
}

// ---------------------------------------------------------------- one stack

function calibrateGroup(stack, g, lights, masters, SOUT, scalingCsv) {
   var IC = new ImageCalibration;
   IC.targetFrames = lights.map(function (f) { return [true, f]; });
   if (CONFIG.cfa) {
      try { IC.enableCFA = true; } catch (e0) {}
      try { if (ImageCalibration.prototype[CONFIG.cfa] !== undefined) IC.cfaPattern = ImageCalibration.prototype[CONFIG.cfa]; } catch (e1) {}
   }
   var bias = masters.bias, dark = g.dark !== null ? masters.darks[String(g.dark)] : null;
   var flat = masters.flats[stack.filter] || null;
   IC.masterBiasEnabled = !!bias;
   if (bias) IC.masterBiasPath = bias;
   IC.masterDarkEnabled = !!dark;
   if (dark) {
      IC.masterDarkPath = dark;
      // the master dark is a raw integration (bias inside): subtract the bias
      // from it first, which dark scaling needs
      IC.calibrateDark = !!bias;
      IC.optimizeDarks = !!(g.optimize && bias);
   }
   IC.masterFlatEnabled = !!flat;
   if (flat) {
      IC.masterFlatPath = flat;
      IC.calibrateFlat = false;
      if (CONFIG.cfa) { try { IC.separateCFAFlatScalingFactors = true; } catch (e2) {} }
   }
   IC.outputPedestal = CONFIG.pedestal;
   if (ImageCalibration.prototype.OutputPedestal_Literal !== undefined)
      IC.outputPedestalMode = ImageCalibration.prototype.OutputPedestal_Literal;
   IC.outputDirectory = SOUT + "/cal"; ensureDir(IC.outputDirectory);
   IC.outputExtension = ".xisf"; IC.overwriteExistingFiles = true;
   log("[" + stack.name + " " + g.name + "] calibrate " + lights.length + " lights: " +
       (bias ? "bias" : "no bias") + " + " + (dark ? ("dark " + g.dark + " s" + (IC.optimizeDarks ? " (optimizeDarks)" : "")) : "no dark") +
       " + " + (flat ? "flat" : "no flat") + ", pedestal " + CONFIG.pedestal + " DN");
   if (!IC.executeGlobal()) throw new Error("calibration failed for " + stack.name + " " + g.name);
   var made = [];
   try {
      var od = IC.outputData, k = [];
      for (var i = 0; i < od.length; ++i) {
         var p = String(od[i][0]);
         if (p.length && File.exists(p)) made.push(p);
         if (od[i].length >= 4 && dark) {
            k.push(Number(od[i][1]));
            scalingCsv.push(stack.name + "," + g.name + "," + File.extractName(p) + "," +
                            Number(od[i][1]).toFixed(4) + "," + Number(od[i][2]).toFixed(4) + "," +
                            Number(od[i][3]).toFixed(4));
         }
      }
      if (k.length) log("[" + stack.name + " " + g.name + "] dark scaling k (first channel) median " + median(k).toFixed(4));
   } catch (e) { log("[" + stack.name + " " + g.name + "] outputData unreadable (" + e + ")"); }
   return made;
}

function runStack(stack, masters, reg) {
   var S = stack.name;
   TSTART(S, "stack total");
   var SOUT = OUT + "/" + S;
   ensureDir(SOUT);
   var stale = 0;
   ["cal", "cc", "debayer", "reg", "ln"].forEach(function (d) { stale += clearDir(SOUT + "/" + d); });
   stale += clearDir(SOUT);
   if (stale) log("[" + S + "] cleared " + stale + " stale files");

   var all = [], byGroup = [];
   for (var g = 0; g < stack.groups.length; ++g) {
      var L = listFits(STAGING + "/" + stack.groups[g].dir);
      byGroup.push(L); all = all.concat(L);
      log("[" + S + "] lights " + stack.groups[g].name + ": " + L.length);
   }
   if (!all.length) { log("[" + S + "] no lights; skipped"); TEND(S, "stack total"); return null; }

   var scalingCsv = ["stack,group,file,k0,k1,k2"];
   var nBefore = 0;
   for (var g = 0; g < stack.groups.length; ++g) {
      if (!byGroup[g].length) continue;
      TSTART(S, "calibration " + stack.groups[g].name);
      var made = calibrateGroup(stack, stack.groups[g], byGroup[g], masters, SOUT, scalingCsv);
      TEND(S, "calibration " + stack.groups[g].name);
      var nNow = listFits(SOUT + "/cal").length;
      assertFunnel("calibration " + stack.groups[g].name, byGroup[g].length, nNow - nBefore);
      assertFunnel("calibration " + stack.groups[g].name + " (outputData)", byGroup[g].length, made.length);
      nBefore = nNow;
   }
   writeText(SOUT + "/dark_scaling.csv", scalingCsv);
   var work = listFits(SOUT + "/cal");
   assertFunnel("calibration", all.length, work.length);
   logDrops("calibration", all, work);
   log("[" + S + "] stage calibration: " + all.length + " in -> " + work.length + " out (" + groupCounts(stack, work) + ")");

   TSTART(S, "cosmetic correction");
   var anyDark = false;
   for (var g = 0; g < stack.groups.length; ++g) if (stack.groups[g].dark !== null && masters.darks[String(stack.groups[g].dark)]) anyDark = true;
   var CC = new CosmeticCorrection;
   CC.targetFrames = work.map(function (f) { return [true, f]; });
   CC.cfa = !!CONFIG.cfa; CC.useAutoDetect = true;
   CC.hotAutoCheck = true; CC.hotAutoValue = anyDark ? 5.0 : 3.0;
   CC.coldAutoCheck = !anyDark; CC.coldAutoValue = 3.0;
   CC.outputDir = SOUT + "/cc"; ensureDir(CC.outputDir); CC.overwrite = true;
   if (!CC.executeGlobal()) throw new Error("cosmetic correction failed");
   var ccFiles = listFits(CC.outputDir);
   assertFunnel("cosmetic", work.length, ccFiles.length);
   logDrops("cosmetic", work, ccFiles);
   log("[" + S + "] stage cosmetic: " + work.length + " in -> " + ccFiles.length + " out");
   TEND(S, "cosmetic correction");

   var rgbFiles = ccFiles;
   if (CONFIG.cfa) {
      TSTART(S, "debayer");
      var DB = new Debayer;
      DB.cfaPattern = Debayer.prototype[CONFIG.cfa];
      DB.debayerMethod = Debayer.prototype.VNG;
      try { DB.evaluateNoise = true; } catch (e3) {}
      try { DB.evaluateSignal = true; } catch (e4) {}
      DB.targetItems = ccFiles.map(function (f) { return [true, f]; });
      DB.outputDirectory = SOUT + "/debayer"; ensureDir(DB.outputDirectory);
      DB.outputExtension = ".xisf"; DB.overwriteExistingFiles = true;
      if (!DB.executeGlobal()) throw new Error("debayer failed");
      rgbFiles = listFits(DB.outputDirectory);
      assertFunnel("debayer", ccFiles.length, rgbFiles.length);
      logDrops("debayer", ccFiles, rgbFiles);
      log("[" + S + "] stage debayer: " + ccFiles.length + " in -> " + rgbFiles.length + " out");
      TEND(S, "debayer");
   }

   // one registration reference for every stack, so mono masters align
   if (!reg.file) {
      var hit = findByBase(rgbFiles, CONFIG.reference);
      reg.file = hit || rgbFiles[Math.floor(rgbFiles.length / 2)];
      log("registration reference: " + File.extractName(reg.file) + (hit ? " (star QA pick)" : " (mid-stack; QA pick not in this stack)"));
   }
   TSTART(S, "star alignment");
   var SA = new StarAlignment;
   SA.referenceImage = reg.file; SA.referenceIsFile = true;
   SA.targets = rgbFiles.map(function (f) { return [true, true, f]; });
   SA.outputDirectory = SOUT + "/reg"; ensureDir(SA.outputDirectory);
   SA.outputExtension = ".xisf"; SA.overwriteExistingFiles = true;
   SA.distortionCorrection = true;
   SA.structureLayers = 5;
   SA.sensitivity = CONFIG.cfa ? 0.60 : 0.30;
   SA.peakResponse = 0.50;
   SA.useTriangleSimilarity = true;
   if (!SA.executeGlobal()) throw new Error("registration failed");
   var regFiles = listFits(SOUT + "/reg");
   assertFunnel("registration", rgbFiles.length, regFiles.length);
   logDrops("registration", rgbFiles, regFiles);
   log("[" + S + "] stage registration: " + rgbFiles.length + " in -> " + regFiles.length + " out (" + groupCounts(stack, regFiles) + ")");
   TEND(S, "star alignment");
   if (regFiles.length < 3) throw new Error("[" + S + "] too few registered frames (" + regFiles.length + ")");
   var nReg = regFiles.length;

   var lnData = null;
   TSTART(S, "local normalization");
   try {
      var lnRef = findByBase(regFiles, stack.reference || CONFIG.reference) || regFiles[Math.floor(regFiles.length / 2)];
      var LN = new LocalNormalization;
      LN.referencePathOrViewId = lnRef;
      LN.referenceIsView = false;
      LN.targetItems = regFiles.map(function (f) { return [true, f]; });
      LN.outputDirectory = SOUT + "/ln"; ensureDir(LN.outputDirectory);
      LN.overwriteExistingFiles = true;
      LN.generateNormalizationData = true;
      if (!LN.executeGlobal()) throw new Error("LocalNormalization returned false");
      var maps = [], missing = 0;
      for (var i = 0; i < regFiles.length; ++i) {
         var x = SOUT + "/ln/" + File.extractName(regFiles[i]) + ".xnml";
         if (!File.exists(x)) ++missing;
         maps.push(x);
      }
      if (missing) {
         if (missing > regFiles.length / 4) throw new Error(missing + " LN maps missing");
         var keepF = [], keepM = [];
         for (var i = 0; i < regFiles.length; ++i)
            if (File.exists(maps[i])) { keepF.push(regFiles[i]); keepM.push(maps[i]); }
            else log("  DROPPED at local normalization (no map): " + File.extractName(regFiles[i]));
         regFiles = keepF; maps = keepM;
      }
      lnData = maps;
      log("[" + S + "] stage local normalization: " + maps.length + " maps (ref " + File.extractName(lnRef) + ")");
   } catch (e) {
      log("[" + S + "] local normalization skipped (" + e + "); using AdditiveWithScaling");
      lnData = null;
   }
   TEND(S, "local normalization");

   TSTART(S, "integration");
   var rej = regFiles.length >= 15 ? "WinsorizedSigmaClip" : (regFiles.length >= 6 ? "SigmaClip" : "PercentileClip");
   function makeII(psf) {
      var II = new ImageIntegration;
      II.images = regFiles.map(function (f, i) { return [true, f, "", lnData ? lnData[i] : ""]; });
      II.combination = ImageIntegration.prototype.Average;
      II.weightMode = psf ? ImageIntegration.prototype.PSFSignalWeight
                          : (stack.mixed_exposures ? ImageIntegration.prototype.ExposureTimeWeight
                                                   : ImageIntegration.prototype.DontCare);
      II.minWeight = 0.0;
      II.generateIntegratedImage = true; II.generateRejectionMaps = false;
      II.rejection = ImageIntegration.prototype[rej];
      if (lnData) {
         II.normalization = ImageIntegration.prototype.LocalNormalization;
         II.rejectionNormalization = ImageIntegration.prototype.LocalRejectionNormalization;
      } else {
         II.normalization = ImageIntegration.prototype.AdditiveWithScaling;
         II.rejectionNormalization = ImageIntegration.prototype.Scale;
      }
      II.evaluateSNR = true;
      return II;
   }
   log("[" + S + "] integrating " + regFiles.length + " frames (" + groupCounts(stack, regFiles) + "), " +
       (lnData ? "LocalNormalization" : "AdditiveWithScaling") + ", PSFSignalWeight, " + rej);
   var II = makeII(true), ok = false;
   try { ok = II.executeGlobal(); } catch (e) { log("[" + S + "] PSF Signal Weight integration failed (" + e + ")"); }
   if (!ok) {
      // HANDBOOK lesson: star-poor narrowband frames fail PSF weighting
      log("[" + S + "] retrying with " + (stack.mixed_exposures
          ? "exposure-time weights (weightMode ExposureTimeWeight: this stack mixes exposure lengths)"
          : "equal weights (weightMode DontCare)"));
      II = makeII(false);
      if (!II.executeGlobal()) throw new Error("integration failed");
   }
   try { log("  noise estimates: " + fmtArr(II.finalNoiseEstimates, 7)); } catch (e5) {}
   try { log("  rejected low: " + fmtArr(II.totalRejectedLow, 0) + "  high: " + fmtArr(II.totalRejectedHigh, 0)); } catch (e9) {}
   try {
      var wcsv = ["file,group,w0,w1,w2"];
      var idt = II.imageData;
      for (var i = 0; i < idt.length && i < regFiles.length; ++i)
         wcsv.push(File.extractName(regFiles[i]) + "," + groupOf(stack, regFiles[i]) + "," +
                   Number(idt[i][0]).toFixed(5) + "," + Number(idt[i][1]).toFixed(5) + "," + Number(idt[i][2]).toFixed(5));
      writeText(SOUT + "/weights.csv", wcsv);
   } catch (e10) { log("  weights unavailable (" + e10 + ")"); }

   var w = ImageWindow.windowById("integration");
   ensureDir(OUT + "/master");
   var masterPath = OUT + "/master/master_" + S + ".xisf";
   w.saveAs(masterPath, false, false, false, false);
   if (CONFIG.cfa) w.saveAs(OUT + "/master/masterOSC.xisf", false, false, false, false);
   ["rejection_low", "rejection_high", "slope"].forEach(function (rid) {
      var rw = ImageWindow.windowById(rid); if (!rw.isNull) rw.forceClose(); });
   log("[" + S + "] master saved: " + masterPath + " (" + regFiles.length + " frames)");
   TEND(S, "integration");

   TSTART(S, "review jpg");
   try {
      cropBorders(w.mainView, 0.015);
      autoStretch(w.mainView);
      w.saveAs(OUT + "/master/master_" + S + "_review.jpg", false, false, false, false);
   } catch (e12) { log("[" + S + "] review jpg skipped (" + e12 + ")"); }
   w.forceClose();
   TEND(S, "review jpg");
   log("[" + S + "] funnel " + all.length + " staged -> " + work.length + " cal -> " + ccFiles.length + " cc -> " +
       rgbFiles.length + (CONFIG.cfa ? " debayer -> " : " -> ") + nReg + " reg -> " + regFiles.length + " integrated");
   TEND(S, "stack total");
   return masterPath;
}

function main() {
   console.show();
   log("staging: " + STAGING + "  target " + CONFIG.target + "  rig " + CONFIG.rig + "  CFA " + (CONFIG.cfa || "none"));
   ensureDir(OUT + "/master");
   var masters = { bias: null, darks: {}, flats: {} };
   TSTART("masters", "calibration masters");
   if (CONFIG.bias) {
      var bf = listFits(STAGING + "/" + CONFIG.bias.dir);
      if (bf.length >= 3) masters.bias = integrateCal(bf, "masterBias");
      else log("bias: only " + bf.length + " frames; no master bias");
   } else log("bias: none staged");
   for (var d = 0; d < CONFIG.darks.length; ++d) {
      var df = listFits(STAGING + "/" + CONFIG.darks[d].dir);
      if (df.length >= 3) masters.darks[String(CONFIG.darks[d].exp)] = integrateCal(df, "masterDark_" + CONFIG.darks[d].label);
      else log("darks " + CONFIG.darks[d].label + ": only " + df.length + " frames; no master dark");
   }
   for (var f = 0; f < CONFIG.flats.length; ++f) {
      try {
         var mf = integrateFlat(CONFIG.flats[f], masters.bias);
         if (mf) masters.flats[CONFIG.flats[f].filter] = mf;
      } catch (e) { log("master flat " + CONFIG.flats[f].filter + " skipped (" + e + "); that filter runs without a flat"); }
   }
   TEND("masters", "calibration masters");
   var reg = { file: null };
   var made = 0;
   for (var s = 0; s < CONFIG.stacks.length; ++s)
      if (runStack(CONFIG.stacks[s], masters, reg)) ++made;
   if (!made) throw new Error("no stack produced a master");
   TIMING.push("all,integration script total," + new Date(T0).toISOString() + "," + (new Date).toISOString() + "," + ((Date.now() - T0) / 60000).toFixed(2));
   writeText(TIMINGFILE, TIMING);
   log("DONE - " + made + " master(s), total " + ((Date.now() - T0) / 60000).toFixed(1) + " min");
}

try { main(); log("EXIT OK"); }
catch (e) { log("ERROR: " + e.toString()); console.criticalln("[STACK] FAILED: " + e.toString()); }
