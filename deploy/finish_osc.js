// PhotonScript OSC finishing pipeline (PJSR): masterOSC.xisf -> finished image.
// Launched by run-finish-osc.ps1, which fills the __PLACEHOLDERS__ and, when
// PixInsight's ImageSolver script exists, the solver include below.
//
//   crop registration edges -> gradient removal (GradientCorrection, else ABE)
//   -> plate solve (ImageSolver, spiral search around the target) -> SPCC
//   (fallback: BackgroundNeutralization + ColorCalibration) -> [BlurXTerminator]
//   -> [NoiseXTerminator] -> linked auto-stretch -> gentle saturation
//   -> out/final/<Name>_final.{xisf,tif,jpg} + <Name>_linear.xisf
//
// Every optional step is guarded: if a process or third-party tool is missing
// or fails, it is logged and skipped, never fatal. The log is out/finish.log.
// Pure ASCII; no slash-star inside line comments (HANDBOOK sec 6).

//__SOLVER_INCLUDE__

#include <pjsr/DataType.jsh>

var STAGING   = "__STAGING__";
var NAME      = "__NAME__";
var RA_DEG    = __RA__;          // target coordinates (deg); NaN = unknown
var DEC_DEG   = __DEC__;
var FOCAL_MM  = __FOCAL__;
var PIXEL_UM  = __PIXEL__;
var GRADIENT  = "__GRADIENT__";  // auto | abe | none
var USE_RC    = __USE_RC__;      // true: use BlurX/NoiseXTerminator if installed
var OUT = STAGING + "/out";
var FINAL = OUT + "/final";

var LOGLINES = [];
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function writeLog() {
   try {
      ensureDir(OUT);
      var f = new File; f.createForWriting(OUT + "/finish.log");
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

// ---------------------------------------------------------------- steps

function cropEdges(view, frac) {
   var img = view.image;
   var dx = Math.round(img.width * frac), dy = Math.round(img.height * frac);
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -dx; CR.rightMargin = -dx; CR.topMargin = -dy; CR.bottomMargin = -dy;
   CR.executeOn(view, false);
   log("cropped " + dx + "x" + dy + " px registration edges -> " +
       view.image.width + "x" + view.image.height);
}

function removeGradient(view) {
   if (GRADIENT === "none") { log("gradient removal: skipped (-Gradient none)"); return; }
   if (GRADIENT === "auto" && typeof GradientCorrection !== "undefined") {
      try {
         var GC = new GradientCorrection;
         if (!GC.executeOn(view, false)) throw new Error("returned false");
         log("gradient removal: GradientCorrection (defaults)");
         return;
      } catch (e) { log("GradientCorrection failed (" + e + "); trying ABE"); }
   }
   try {
      // Degree 1: a plane. Low on purpose: M31's halo fills a corner and a
      // higher-order model would fit (and subtract) the galaxy itself.
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

// ImageSolver with a spiral search: the frame centre is usually NOT the named
// target (Piggy-600 rides the RC16's pointing), so try the target first, then
// rings of offsets out to ~1.2 deg.
function plateSolve(window) {
   if (typeof ImageSolver === "undefined") {
      log("plate solve: ImageSolver script not found in this PixInsight install; skipped");
      return false;
   }
   if (isNaN(RA_DEG) || isNaN(DEC_DEG)) {
      log("plate solve: no target coordinates (-RaDeg/-DecDeg or a known -Target); skipped");
      return false;
   }
   var scale = (PIXEL_UM / FOCAL_MM) * 206.265;   // arcsec/px
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
         if (solver.SolveImage(window)) {   // SolveImage writes the WCS keywords/properties itself
            log("plate solve: OK on try " + (k + 1) + " (seed RA " + ra.toFixed(3) +
                " Dec " + dec.toFixed(3) + ", " + scale.toFixed(2) + "\"/px)");
            return true;
         }
      } catch (e) {
         log("  solve try " + (k + 1) + " failed: " + e);
      }
   }
   log("plate solve: FAILED after " + offsets.length + " seeds");
   return false;
}

function colorCalibrate(view, solved) {
   if (solved && typeof SpectrophotometricColorCalibration !== "undefined") {
      try {
         var SP = new SpectrophotometricColorCalibration;
         // Defaults: Average Spiral Galaxy white reference, background
         // neutralization on. Filter/QE curves = whatever the SPCC defaults are
         // in this PI install; set Sony IMX571 + OSC curves once in the GUI if
         // the color looks off, and re-run.
         try { SP.neutralizeBackground = true; } catch (e1) {}
         if (!SP.executeOn(view, false)) throw new Error("returned false");
         log("color: SPCC applied");
         return;
      } catch (e) { log("SPCC failed (" + e + "); falling back to BN + ColorCalibration"); }
   } else if (!solved) {
      log("color: no astrometric solution -> BN + ColorCalibration fallback");
   }
   try {
      var BN = new BackgroundNeutralization;
      BN.executeOn(view, false);
      var CC = new ColorCalibration;
      CC.executeOn(view, false);
      log("color: BackgroundNeutralization + ColorCalibration (whole image references)");
   } catch (e) { log("color calibration skipped (" + e + ")"); }
}

function rcTools(view) {
   if (!USE_RC) { log("RC Astro tools: disabled (-NoRC)"); return; }
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
   var c0 = Math.max(0, Math.min(1, med - 2.8 * mad));
   var m = mtfv(0.20, Math.max(1.0e-6, med - c0));
   var HT = new HistogramTransformation;
   HT.H = [[0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1],
           [c0, m, 1, 0, 1], [0, 0.5, 1, 0, 1]];
   HT.executeOn(view, false);
   log("stretch: linked, shadows " + c0.toFixed(5) + ", midtones " + m.toFixed(5));
   try {
      var CT = new CurvesTransformation;
      CT.S = [[0, 0], [0.5, 0.60], [1, 1]];
      CT.executeOn(view, false);
      log("saturation: gentle S boost");
   } catch (e) { log("saturation skipped (" + e + ")"); }
}

function main() {
   console.show();
   var src = OUT + "/master/masterOSC.xisf";
   log("finish: " + src + "  target=" + NAME + "  RA=" + RA_DEG + " Dec=" + DEC_DEG);
   if (!File.exists(src)) throw new Error("no master at " + src + " - run run-integration-osc.ps1 first");
   ensureDir(FINAL);
   var ws = ImageWindow.open(src);
   if (!ws.length) throw new Error("could not open " + src);
   var w = ws[0];
   var v = w.mainView;

   cropEdges(v, 0.015);
   removeGradient(v);
   var solved = plateSolve(w);
   colorCalibrate(v, solved);
   w.saveAs(FINAL + "/" + NAME + "_linear.xisf", false, false, false, false);
   log("saved linear (color-calibrated): " + NAME + "_linear.xisf");

   rcTools(v);
   stretch(v);
   w.saveAs(FINAL + "/" + NAME + "_final.xisf", false, false, false, false);
   var SF = new SampleFormatConversion;
   SF.format = SampleFormatConversion.prototype.ToInteger16;
   try { SF.executeOn(v, false); } catch (e) { log("16-bit conversion skipped (" + e + ")"); }
   w.saveAs(FINAL + "/" + NAME + "_final.tif", false, false, false, false);
   w.saveAs(FINAL + "/" + NAME + "_final.jpg", false, false, false, false);
   log("saved " + NAME + "_final.xisf / .tif (16-bit) / .jpg in " + FINAL);
   w.forceClose();
   log("DONE");
}

try { main(); log("EXIT OK"); }
catch (e) { log("ERROR: " + e.toString()); console.criticalln("[FINISH] FAILED: " + e.toString()); }
writeLog();
