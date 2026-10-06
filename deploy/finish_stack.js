// PhotonScript library finish (PJSR), PS-22. Template: `photonscript integrate`
// fills the solver include and the CONFIG line and writes finish_run.js into
// the run folder. Do not run this template directly.
//
// Generalized from Staging/M31_OSC4/finish_osc4_v4b.js (the v3 knobs: darker
// sky, SCNR green, HDR core blend, guarded saves, 16-bit TIF):
//   per master: crop 1.5% registration edges -> gradient removal
//   (GradientCorrection, else ABE degree 1) -> plate solve (ImageSolver,
//   spiral search around the target) -> color: SPCC, else
//   BackgroundNeutralization + ColorCalibration (color masters only) ->
//   SCNR green (color) -> <name>_linear.xisf -> [BlurX / NoiseXTerminator]
//   -> linked stretch + saturation -> HDR core blend (color) ->
//   <name>_final.xisf / .tif (16-bit) / .jpg in out/final/.
// Every optional step is guarded: a missing or failing process is logged
// and skipped, never fatal. Log out/finish.log, ends EXIT OK or EXIT WITH n
// FAILED MASTER(S).
//
// PJSR rules (HANDBOOK sec 6): pure ASCII; no slash-star sequence inside a
// line comment; the pjsr headers come BEFORE the AdP solver includes
// (AstronomicalCatalogs.jsh uses DataType_Double at load time, 2026-09-26);
// the astrometric solution is cleared before every Crop.

#include <pjsr/DataType.jsh>
#include <pjsr/ColorSpace.jsh>
#include <pjsr/UndoFlag.jsh>
#include <pjsr/SampleType.jsh>
#include <pjsr/FrameStyle.jsh>
#include <pjsr/Sizer.jsh>
#include <pjsr/StdButton.jsh>
#include <pjsr/StdCursor.jsh>
#include <pjsr/StdIcon.jsh>
#include <pjsr/TextAlign.jsh>
#include <pjsr/NumericControl.jsh>
#include <pjsr/SectionBar.jsh>
//__SOLVER_INCLUDE__

//__CONFIG__

var RA_DEG = CONFIG.ra_deg === null ? NaN : CONFIG.ra_deg;
var DEC_DEG = CONFIG.dec_deg === null ? NaN : CONFIG.dec_deg;
var FOCAL_MM = CONFIG.focal_mm;
var PIXEL_UM = CONFIG.pixel_um;
var GRADIENT = CONFIG.gradient;          // auto | abe | none
var USE_RC = CONFIG.use_rc;
var BG_TARGET = CONFIG.bg_target;        // stretched background level
var SHADOW_SIGMA = CONFIG.shadow_sigma;  // black point = median - k * MAD
var SCNR_AMOUNT = CONFIG.scnr;           // 0 = off
var SAT_MID = CONFIG.sat_mid;
var HDR_LAYERS = CONFIG.hdr_layers;      // 0 = off
var HDR_LO = 0.55;
var HDR_SPAN = 0.35;
var LOGFILE = CONFIG.out + "/finish.log";
var FINAL = CONFIG.out + "/final";
var NAME = "";

var LOGLINES = [];
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function writeLog() {
   try {
      ensureDir(CONFIG.out);
      var f = new File; f.createForWriting(LOGFILE);
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("log write failed: " + e); }
}
function log(s) {
   console.noteln("<b>[FINISH]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   writeLog();
}
function mtfv(m, x) { if (x <= 0) return 0; if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m); }

// Crop invalidates a WCS and PixInsight then stops on a Yes/No dialog:
// clear the solution first, every time.
function clearWcs(view) {
   try { view.window.clearAstrometricSolution(); } catch (e) {}
}
function cropEdges(view, frac) {
   clearWcs(view);
   var img = view.image;
   var dx = Math.round(img.width * frac), dy = Math.round(img.height * frac);
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -dx; CR.rightMargin = -dx; CR.topMargin = -dy; CR.bottomMargin = -dy;
   CR.executeOn(view, false);
   log("cropped " + dx + "x" + dy + " px registration edges -> " + view.image.width + "x" + view.image.height);
}

function removeGradient(view) {
   if (GRADIENT === "none") { log("gradient removal: skipped (gradient none)"); return; }
   if (GRADIENT === "auto" && typeof GradientCorrection !== "undefined") {
      try {
         var GC = new GradientCorrection;
         if (!GC.executeOn(view, false)) throw new Error("returned false");
         log("gradient removal: GradientCorrection (defaults)");
         return;
      } catch (e) { log("GradientCorrection failed (" + e + "); trying ABE"); }
   }
   try {
      // Degree 1: a plane, so a large galaxy halo is not fitted away.
      var ABE = new AutomaticBackgroundExtractor;
      ABE.polyDegree = 1;
      ABE.targetCorrection = AutomaticBackgroundExtractor.prototype.Subtract;
      ABE.normalize = true;
      ABE.discardModel = true;
      ABE.replaceTarget = true;
      if (!ABE.executeOn(view, false)) throw new Error("returned false");
      log("gradient removal: ABE degree 1 (subtract)");
   } catch (e) { log("gradient removal skipped: ABE failed (" + e + ")"); }
}

// ImageSolver with a spiral search: the frame center is often NOT the named
// target (the Piggy-600 rides the RC16's pointing).
function plateSolve(window) {
   if (typeof ImageSolver === "undefined") {
      log("plate solve: ImageSolver script not found in this PixInsight install; skipped");
      return false;
   }
   if (isNaN(RA_DEG) || isNaN(DEC_DEG)) {
      log("plate solve: no target coordinates; skipped");
      return false;
   }
   var scale = (PIXEL_UM / FOCAL_MM) * 206.265;
   var offsets = [[0, 0]];
   var step = 0.6;
   for (var r = 1; r <= 2; ++r)
      for (var i = -r; i <= r; ++i)
         for (var j = -r; j <= r; ++j)
            if (Math.max(Math.abs(i), Math.abs(j)) === r) offsets.push([i * step, j * step]);
   for (var k = 0; k < offsets.length; ++k) {
      var dDec = offsets[k][1];
      var dRa = offsets[k][0] / Math.max(0.2, Math.cos((DEC_DEG + dDec) * Math.PI / 180));
      var ra = (RA_DEG + dRa + 360) % 360, dec = Math.max(-89.9, Math.min(89.9, DEC_DEG + dDec));
      try {
         var solver = new ImageSolver();
         solver.Init(window, false);
         solver.metadata.ra = ra;
         solver.metadata.dec = dec;
         solver.metadata.focal = FOCAL_MM;
         solver.metadata.useFocal = true;
         solver.metadata.xpixsz = PIXEL_UM;
         solver.metadata.resolution = scale / 3600;
         try { solver.solverCfg.showStars = false; } catch (e1) {}
         try { solver.solverCfg.showDistortion = false; } catch (e2) {}
         try { solver.solverCfg.generateErrorImg = false; } catch (e3) {}
         try { solver.solverCfg.generateDistortModel = false; } catch (e5) {}
         try { solver.solverCfg.distortionCorrection = true; } catch (e4) {}
         if (solver.SolveImage(window)) {
            log("plate solve: OK on try " + (k + 1) + " (seed RA " + ra.toFixed(3) +
                " Dec " + dec.toFixed(3) + ", " + scale.toFixed(2) + "\"/px)");
            return true;
         }
      } catch (e) { log("  solve try " + (k + 1) + " failed: " + e); }
   }
   log("plate solve: FAILED after " + offsets.length + " seeds");
   return false;
}

function colorCalibrate(view, solved) {
   if (!view.image.isColor) { log("color: mono master, no color calibration"); return; }
   if (solved && typeof SpectrophotometricColorCalibration !== "undefined") {
      try {
         var SP = new SpectrophotometricColorCalibration;
         try { SP.neutralizeBackground = true; } catch (e1) {}
         if (!SP.executeOn(view, false)) throw new Error("returned false");
         log("color: SPCC applied");
         return;
      } catch (e) { log("SPCC failed (" + e + "); falling back to BN + ColorCalibration"); }
   } else if (!solved) log("color: no astrometric solution -> BN + ColorCalibration fallback");
   try {
      var BN = new BackgroundNeutralization;
      BN.executeOn(view, false);
      var CC = new ColorCalibration;
      CC.executeOn(view, false);
      log("color: BackgroundNeutralization + ColorCalibration (whole image references)");
   } catch (e) { log("color calibration skipped (" + e + ")"); }
}

function removeGreen(view) {
   if (!(SCNR_AMOUNT > 0) || !view.image.isColor) return;
   try {
      var S = new SCNR;
      S.amount = SCNR_AMOUNT;
      S.protectionMethod = SCNR.prototype.AverageNeutral;
      S.colorToRemove = SCNR.prototype.Green;
      S.preserveLightness = true;
      S.executeOn(view, false);
      log("SCNR: green removed, amount " + SCNR_AMOUNT);
   } catch (e) { log("SCNR skipped (" + e + ")"); }
}

function rcTools(view) {
   if (!USE_RC) { log("RC Astro tools: disabled"); return; }
   if (typeof BlurXTerminator !== "undefined") {
      try {
         var BX = new BlurXTerminator;
         try { BX.correct_only = false; BX.sharpen_stars = 0.25;
               BX.sharpen_nonstellar = 0.50; BX.adjust_halos = 0.0; } catch (e1) {}
         BX.executeOn(view, false);
         log("BlurXTerminator: applied (stars 0.25, nonstellar 0.50)");
      } catch (e) { log("BlurXTerminator failed (" + e + ")"); }
   } else log("BlurXTerminator: not installed");
   if (typeof NoiseXTerminator !== "undefined") {
      try {
         var NX = new NoiseXTerminator;
         try { NX.denoise = 0.80; NX.detail = 0.15; } catch (e2) {}
         NX.executeOn(view, false);
         log("NoiseXTerminator: applied (denoise 0.80)");
      } catch (e) { log("NoiseXTerminator failed (" + e + ")"); }
   } else log("NoiseXTerminator: not installed");
}

// Linked stretch after color calibration (unlinked would undo the color work).
function stretch(view) {
   var img = view.image;
   var med = img.median();
   var mad = img.MAD() * 1.4826;
   var c0 = Math.max(0, Math.min(1, med - SHADOW_SIGMA * mad));
   var m = mtfv(BG_TARGET, Math.max(1.0e-6, med - c0));
   var HT = new HistogramTransformation;
   HT.H = [[0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1],
           [c0, m, 1, 0, 1], [0, 0.5, 1, 0, 1]];
   HT.executeOn(view, false);
   log("stretch: linked, shadows " + c0.toFixed(5) + ", midtones " + m.toFixed(5));
   if (!img.isColor) return;
   try {
      var CT = new CurvesTransformation;
      CT.S = [[0, 0], [0.5, SAT_MID], [1, 1]];
      CT.executeOn(view, false);
      log("saturation: gentle S boost");
   } catch (e) { log("saturation skipped (" + e + ")"); }
}

// Core recovery: HDRMultiscaleTransform on a copy, blended back only where
// the image is bright, so the arms and the sky keep the normal stretch.
function recoverCore(view) {
   if (!(HDR_LAYERS > 0) || !view.image.isColor) return;
   var dup = null;
   try {
      var img = view.image;
      dup = new ImageWindow(img.width, img.height, img.numberOfChannels, 32, true, img.isColor, "PS_HDR");
      dup.mainView.beginProcess(UndoFlag_NoSwapFile);
      dup.mainView.image.assign(img);
      dup.mainView.endProcess();
      var H = new HDRMultiscaleTransform;
      H.numberOfLayers = HDR_LAYERS;
      H.numberOfIterations = 1;
      H.invertedIterations = true;
      H.overdrive = 0;
      H.medianTransform = false;
      H.toLightness = true;
      H.preserveHue = false;
      H.luminanceMask = true;
      H.deringing = false;
      H.executeOn(dup.mainView, false);
      var W = img.width, Hh = img.height, N = W * Hh, rect = new Rect(W, Hh);
      var hdr = dup.mainView.image;
      var c = [], h = [];
      for (var ch = 0; ch < 3; ++ch) {
         c.push(new Float32Array(N)); img.getSamples(c[ch], rect, ch);
         h.push(new Float32Array(N)); hdr.getSamples(h[ch], rect, ch);
      }
      var chromaBefore = 0, chromaAfter = 0, nb = 0;
      for (var i = 0; i < N; i += 97) { chromaBefore += Math.abs(c[0][i] - c[1][i]) + Math.abs(c[2][i] - c[1][i]); ++nb; }
      for (var i = 0; i < N; ++i) {
         var L = 0.2126 * c[0][i] + 0.7152 * c[1][i] + 0.0722 * c[2][i];
         var k = (L - HDR_LO) / HDR_SPAN;
         if (k <= 0) continue;
         if (k > 1) k = 1;
         k = k * k * (3 - 2 * k);
         c[0][i] = c[0][i] * (1 - k) + h[0][i] * k;
         c[1][i] = c[1][i] * (1 - k) + h[1][i] * k;
         c[2][i] = c[2][i] * (1 - k) + h[2][i] * k;
      }
      for (var i = 0; i < N; i += 97) chromaAfter += Math.abs(c[0][i] - c[1][i]) + Math.abs(c[2][i] - c[1][i]);
      if (chromaAfter < 0.5 * chromaBefore) throw new Error("blend lost color; target left unchanged");
      view.beginProcess(UndoFlag_NoSwapFile);
      for (var ch = 0; ch < 3; ++ch) img.setSamples(c[ch], rect, ch);
      view.endProcess();
      log("core: HDRMultiscaleTransform " + HDR_LAYERS + " layers, blended above L=" + HDR_LO);
   } catch (e) { log("core HDR skipped (" + e + ")"); }
   if (dup) try { dup.forceClose(); } catch (e2) {}
}

function trySave(w, path, label) {
   try {
      if (w.saveAs(path, false, false, false, false)) { log("saved " + label + ": " + path); return true; }
      log("save FAILED (returned false): " + path);
   } catch (e) { log("save FAILED " + path + " (" + e + ")"); }
   return false;
}

function finishOne(src) {
   log("finish: " + src + "  name " + NAME + "  RA " + RA_DEG + " Dec " + DEC_DEG);
   if (!File.exists(src)) throw new Error("no master at " + src);
   ensureDir(FINAL);
   var ws = ImageWindow.open(src);
   if (!ws.length) throw new Error("could not open " + src);
   var w = ws[0];
   var v = w.mainView;
   cropEdges(v, 0.015);
   removeGradient(v);
   var solved = plateSolve(w);
   colorCalibrate(v, solved);
   removeGreen(v);
   trySave(w, FINAL + "/" + NAME + "_linear.xisf", "linear");
   rcTools(v);
   stretch(v);
   recoverCore(v);
   trySave(w, FINAL + "/" + NAME + "_final.xisf", "final xisf (32-bit)");
   var to16 = false;
   try {
      var SF = new SampleFormatConversion;
      SF.format = SampleFormatConversion.prototype.To16Bit;
      SF.executeOn(v, false);
      to16 = true;
   } catch (e) { log("16-bit conversion skipped (" + e + ")"); }
   trySave(w, FINAL + "/" + NAME + "_final.tif", to16 ? "tif (16-bit)" : "tif (32-bit float)");
   trySave(w, FINAL + "/" + NAME + "_final.jpg", "jpg");
   w.forceClose();
}

console.show();
var NFAIL = 0;
for (var mi = 0; mi < CONFIG.masters.length; ++mi) {
   NAME = CONFIG.masters[mi].name;
   var tf0 = Date.now();
   log("STAGE START " + NAME + " / finish");
   try { finishOne(CONFIG.masters[mi].path); }
   catch (e) { ++NFAIL; log("ERROR (" + NAME + "): " + e.toString()); console.criticalln("[FINISH] FAILED: " + e.toString()); }
   log("STAGE END   " + NAME + " / finish (" + ((Date.now() - tf0) / 60000).toFixed(2) + " min)");
}
log(NFAIL ? ("EXIT WITH " + NFAIL + " FAILED MASTER(S)") : "EXIT OK");
