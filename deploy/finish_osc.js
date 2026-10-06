// PhotonScript OSC finishing pipeline (PJSR): masterOSC.xisf -> finished image.
// Launched by run-finish-osc.ps1, which fills the __PLACEHOLDERS__ and, when
// PixInsight's ImageSolver script exists, the solver include below.
//
//   crop registration edges -> gradient removal (GradientCorrection, else ABE)
//   -> plate solve (ImageSolver, spiral search around the target)
//   -> color: SPCC against Gaia DR3/SP when that database is configured
//      (fallback: BackgroundNeutralization + ColorCalibration) -> SCNR green
//   -> [BlurXTerminator] -> [NoiseXTerminator] -> linked stretch
//   -> gentle saturation -> core HDR blend -> [framing crop]
//   -> <Final>/<Name>_final.{xisf,tif,jpg} + <Name>_linear.xisf
//   + <Name>_final_steps.json (which step ran with which tool and settings)
//
// Every optional step is guarded: if a process or third-party tool is missing
// or fails, it is logged and skipped, never fatal.
// Pure ASCII; no slash-star inside line comments (HANDBOOK sec 6).

// pjsr headers MUST precede the AdP solver includes (AstronomicalCatalogs.jsh
// uses DataType_Double at load time; 2026-09-26 run error).
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

var STAGING    = "__STAGING__";
var NAME       = "__NAME__";
var MASTER     = "__MASTER__";      // input master (linear, debayered)
var FINAL      = "__FINAL__";       // output folder
var LOGDIR     = "__LOGDIR__";      // finish.log goes here
var PI_LIBRARY = "__PI_LIBRARY__";  // PixInsight library dir (filters.xspd, white-references.xspd)
var RA_DEG    = __RA__;          // target coordinates (deg); NaN = unknown
var DEC_DEG   = __DEC__;
var FOCAL_MM  = __FOCAL__;
var PIXEL_UM  = __PIXEL__;
var GRADIENT  = "__GRADIENT__";  // auto | abe | none
var USE_RC    = __USE_RC__;      // true: use BlurX/NoiseXTerminator if installed
// Color (PS-46): auto = SPCC when solved and Gaia DR3/SP is configured, else
// BN + ColorCalibration; spcc = try SPCC even if the Gaia probe says no;
// basic = always BN + ColorCalibration.
var COLOR       = "__COLOR__";
var SPCC_QE     = "__SPCC_QE__";      // filters.xspd names (Piggy-600 AP26CC = IMX571)
var SPCC_RED    = "__SPCC_RED__";
var SPCC_GREEN  = "__SPCC_GREEN__";
var SPCC_BLUE   = "__SPCC_BLUE__";
var SPCC_WHITE  = "__SPCC_WHITE__";   // white-references.xspd name
// Stretch and look (from the M31_OSC3/OSC4 v3..v4b finishes)
var BG_TARGET    = __BG_TARGET__;     // stretched background level (0..1)
var SHADOW_SIGMA = __SHADOW_SIGMA__;  // black point = median - SHADOW_SIGMA * MAD
var SCNR_AMOUNT  = __SCNR__;          // green removal after color calibration (0 = off)
var SAT_MID      = __SAT_MID__;       // saturation curve midpoint (0.5 = none)
var HDR_LAYERS   = __HDR_LAYERS__;    // HDRMultiscaleTransform layers for the core (0 = off)
var HDR_LO      = 0.55;   // lightness where the HDR blend starts
var HDR_SPAN    = 0.35;   // blend is full HDR at HDR_LO + HDR_SPAN
// Framing crop on the ORIGINAL master (fractions of width/height), applied
// after the stretch so gradient removal still sees the whole field. Null = none.
var FRAME = __FRAME__;
var EDGE_FRAC = 0.015;

var LOGLINES = [];
var STEPS = [];   // one record per pipeline step, written to <Name>_final_steps.json
// Steps whose tool goes into the output label (PSFINISH keyword, json "tag").
var LABEL_STEPS = { color: 1, deconvolution: 1, noise_reduction: 1, star_reduction: 1 };
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function writeLog() {
   try {
      ensureDir(LOGDIR);
      var f = new File; f.createForWriting(LOGDIR + "/finish.log");
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("log write failed: " + e); }
}
function log(s) {
   console.noteln("<b>[FINISH]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   writeLog();
}
// status: ran | fallback | skipped | failed
function step(name, tool, status, settings) {
   STEPS.push({ step: name, tool: tool, status: status, settings: settings || {} });
}
function stepTag() {
   var t = [];
   for (var i = 0; i < STEPS.length; ++i) {
      var s = STEPS[i];
      if (LABEL_STEPS[s.step] && (s.status === "ran" || s.status === "fallback")) t.push(s.tool);
   }
   return t.length ? t.join("+") : "basic";
}
function writeSteps(result) {
   try {
      ensureDir(FINAL);
      var doc = { name: NAME, master: MASTER, result: result, tag: stepTag(),
                  finished_utc: (new Date).toISOString(), finish: { steps: STEPS } };
      var f = new File; f.createForWriting(FINAL + "/" + NAME + "_final_steps.json");
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}

function mtfv(m, x) { if (x <= 0) return 0; if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m); }

// A Crop invalidates the WCS; clear it first so PixInsight does not stop on a
// Yes/No dialog in an unattended run.
function clearSolution(view) {
   try { view.window.clearAstrometricSolution(); } catch (e) { log("clearAstrometricSolution: " + e); }
}

// ---------------------------------------------------------------- steps

function cropEdges(view, frac) {
   clearSolution(view);
   var img = view.image;
   var dx = Math.round(img.width * frac), dy = Math.round(img.height * frac);
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -dx; CR.rightMargin = -dx; CR.topMargin = -dy; CR.bottomMargin = -dy;
   CR.executeOn(view, false);
   log("cropped " + dx + "x" + dy + " px registration edges -> " +
       view.image.width + "x" + view.image.height);
   step("edge_crop", "Crop", "ran", { px_x: dx, px_y: dy });
}

function removeGradient(view) {
   if (GRADIENT === "none") {
      log("gradient removal: skipped (-Gradient none)");
      step("gradient", "none", "skipped", { reason: "-Gradient none" });
      return;
   }
   if (GRADIENT === "auto" && typeof GradientCorrection !== "undefined") {
      try {
         var GC = new GradientCorrection;
         if (!GC.executeOn(view, false)) throw new Error("returned false");
         log("gradient removal: GradientCorrection (defaults)");
         step("gradient", "GradientCorrection", "ran", {});
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
      step("gradient", "ABE", "ran", { poly_degree: 1 });
   } catch (e) {
      log("gradient removal skipped: ABE failed (" + e + ")");
      step("gradient", "ABE", "failed", { error: "" + e });
   }
}

// ImageSolver with a spiral search: the frame centre is usually NOT the named
// target (Piggy-600 rides the RC16's pointing), so try the target first, then
// rings of offsets out to ~1.2 deg.
function plateSolve(window) {
   if (typeof ImageSolver === "undefined") {
      log("plate solve: ImageSolver script not found in this PixInsight install; skipped");
      step("plate_solve", "ImageSolver", "skipped", { reason: "not installed" });
      return false;
   }
   if (isNaN(RA_DEG) || isNaN(DEC_DEG)) {
      log("plate solve: no target coordinates (-RaDeg/-DecDeg or a known -Target); skipped");
      step("plate_solve", "ImageSolver", "skipped", { reason: "no coordinates" });
      return false;
   }
   var scale = (PIXEL_UM / FOCAL_MM) * 206.265;   // arcsec/px
   var offsets = [[0, 0]];
   var stepDeg = 0.6;
   for (var r = 1; r <= 2; ++r)
      for (var i = -r; i <= r; ++i)
         for (var j = -r; j <= r; ++j)
            if (Math.max(Math.abs(i), Math.abs(j)) === r) offsets.push([i * stepDeg, j * stepDeg]);
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
            step("plate_solve", "ImageSolver", "ran", { try_n: k + 1, arcsec_px: +scale.toFixed(3) });
            return true;
         }
      } catch (e) {
         log("  solve try " + (k + 1) + " failed: " + e);
      }
   }
   log("plate solve: FAILED after " + offsets.length + " seeds");
   step("plate_solve", "ImageSolver", "failed", { seeds: offsets.length });
   return false;
}

// Is the local Gaia DR3/SP (XPSD) database selected in PixInsight
// (Process > Gaia > wrench icon)? Same get-info probe ImageSolver uses.
function gaiaDr3SpReady() {
   if (typeof Gaia === "undefined") return { ok: false, why: "Gaia process not installed" };
   try {
      var G = new Gaia;
      G.command = "get-info";
      G.dataRelease = Gaia.prototype.DataRelease_3_SP;
      try { G.verbosity = 0; } catch (e1) {}
      G.executeGlobal();
      if (G.isValid) return { ok: true, why: "Gaia DR3/SP database configured" };
      return { ok: false, why: "Gaia DR3/SP database files not selected (Gaia > wrench icon)" };
   } catch (e) { return { ok: false, why: "Gaia DR3/SP probe failed (" + e + ")" }; }
}

// Read one curve from a PixInsight XSPD file (filters.xspd, white-references.xspd):
// <Filter name="..." channel="R" data="400,0.088,..."/>. Returns "" if absent.
function xspdCurve(file, tag, name) {
   try {
      var path = PI_LIBRARY + "/" + file;
      if (!File.exists(path)) return "";
      var txt = File.readTextFile(path);
      var esc = name.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, "\\*");
      var re = new RegExp("<" + tag + "\\s+name=\"" + esc + "\"[^>]*?\\sdata=\"([^\"]*)\"");
      var m = re.exec(txt);
      return m ? m[1] : "";
   } catch (e) { log("  xspd read " + file + " failed (" + e + ")"); return ""; }
}

function spcc(view) {
   var SP = new SpectrophotometricColorCalibration;
   var used = { catalog: "GaiaDR3SP" };
   try { SP.catalogId = "GaiaDR3SP"; } catch (e0) { used.catalog = "default"; }
   // Sensor QE, OSC filter curves and white reference from PixInsight's own
   // spectrum databases; a curve that is not found keeps the SPCC default.
   var curves = [
      ["deviceQECurve", "deviceQECurveName", "filters.xspd", "Filter", SPCC_QE, "qe"],
      ["redFilterTrCurve", "redFilterName", "filters.xspd", "Filter", SPCC_RED, "red"],
      ["greenFilterTrCurve", "greenFilterName", "filters.xspd", "Filter", SPCC_GREEN, "green"],
      ["blueFilterTrCurve", "blueFilterName", "filters.xspd", "Filter", SPCC_BLUE, "blue"],
      ["whiteReferenceSpectrum", "whiteReferenceName", "white-references.xspd", "WhiteRef", SPCC_WHITE, "white"]
   ];
   for (var i = 0; i < curves.length; ++i) {
      var c = curves[i];
      used[c[5]] = "default";
      if (!c[4]) continue;
      var data = xspdCurve(c[2], c[3], c[4]);
      if (!data) { log("  SPCC: curve '" + c[4] + "' not in " + c[2] + "; keeping the SPCC default"); continue; }
      try { SP[c[0]] = data; SP[c[1]] = c[4]; used[c[5]] = c[4]; }
      catch (e1) { log("  SPCC: could not set " + c[0] + " (" + e1 + ")"); }
   }
   try { SP.neutralizeBackground = true; } catch (e2) {}
   try { SP.generateGraphs = false; } catch (e3) {}
   try { SP.generateStarMaps = false; } catch (e4) {}
   try { SP.generateTextFiles = false; } catch (e5) {}
   if (!SP.executeOn(view, false)) throw new Error("returned false");
   return used;
}

function colorCalibrate(view, solved) {
   var why = "";
   if (COLOR === "basic") why = "-Color basic";
   else if (!solved) why = "no astrometric solution";
   else if (typeof SpectrophotometricColorCalibration === "undefined") why = "SPCC process not installed";
   if (!why) {
      var g = gaiaDr3SpReady();
      log("color: " + g.why);
      if (!g.ok && COLOR !== "spcc") why = g.why;
   }
   if (!why) {
      try {
         var used = spcc(view);
         log("color: SPCC applied (catalog " + used.catalog + ", QE " + used.qe + ", RGB " +
             used.red + " / " + used.green + " / " + used.blue + ", white " + used.white + ")");
         step("color", "SPCC", "ran", used);
         return;
      } catch (e) { why = "SPCC failed (" + e + ")"; }
   }
   log("color: " + why + " -> BN + ColorCalibration fallback");
   try {
      var BN = new BackgroundNeutralization;
      BN.executeOn(view, false);
      var CC = new ColorCalibration;
      CC.executeOn(view, false);
      log("color: BackgroundNeutralization + ColorCalibration (whole image references)");
      step("color", "BN+CC", "fallback", { reason: why });
   } catch (e2) {
      log("color calibration skipped (" + e2 + ")");
      step("color", "BN+CC", "failed", { reason: why, error: "" + e2 });
   }
}

function removeGreen(view) {
   if (!(SCNR_AMOUNT > 0)) { step("scnr", "SCNR", "skipped", { amount: 0 }); return; }
   try {
      var S = new SCNR;
      S.amount = SCNR_AMOUNT;
      S.protectionMethod = SCNR.prototype.AverageNeutral;
      S.colorToRemove = SCNR.prototype.Green;
      S.preserveLightness = true;
      S.executeOn(view, false);
      log("SCNR: green removed, amount " + SCNR_AMOUNT);
      step("scnr", "SCNR", "ran", { amount: SCNR_AMOUNT });
   } catch (e) { log("SCNR skipped (" + e + ")"); step("scnr", "SCNR", "failed", { error: "" + e }); }
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
         step("deconvolution", "BlurXTerminator", "ran", { stars: 0.25, nonstellar: 0.50 });
      } catch (e) { log("BlurXTerminator failed (" + e + ")"); }
   } else log("BlurXTerminator: not installed");
   if (typeof NoiseXTerminator !== "undefined") {
      try {
         var NX = new NoiseXTerminator;
         try { NX.denoise = 0.80; NX.detail = 0.15; } catch (e2) {}
         NX.executeOn(view, false);
         log("NoiseXTerminator: applied (denoise 0.80)");
         step("noise_reduction", "NoiseXTerminator", "ran", { denoise: 0.80 });
      } catch (e) { log("NoiseXTerminator failed (" + e + ")"); }
   } else log("NoiseXTerminator: not installed");
}

// Linked stretch after color calibration (unlinked would undo the color work).
// Returns the shadows/midtones it applied.
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
   step("stretch", "HistogramTransformation", "ran",
        { shadows: +c0.toFixed(6), midtones: +m.toFixed(6), bg_target: BG_TARGET, shadow_sigma: SHADOW_SIGMA });
   if (SAT_MID !== 0.5) {
      try {
         var CT = new CurvesTransformation;
         CT.S = [[0, 0], [0.5, SAT_MID], [1, 1]];
         CT.executeOn(view, false);
         log("saturation: S curve midpoint " + SAT_MID);
         step("saturation", "CurvesTransformation", "ran", { s_mid: SAT_MID });
      } catch (e) { log("saturation skipped (" + e + ")"); }
   }
   return { c0: c0, m: m };
}

// Core recovery: run HDRMultiscaleTransform on a copy, then blend the copy back
// only where the image is bright (lightness above HDR_LO), so the arms and the
// sky keep the normal stretch. If anything fails the target view is untouched.
function recoverCore(view) {
   if (!(HDR_LAYERS > 0)) { step("core_hdr", "HDRMultiscaleTransform", "skipped", { layers: 0 }); return; }
   var dup = null;
   try {
      var img = view.image;
      dup = new ImageWindow(img.width, img.height, img.numberOfChannels, 32, true,
                            img.isColor, "PS_HDR");
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
      // Blend per channel in JS (PixelMath with CIEL($T) returned gray, 2026-09-26)
      var W = img.width, Hh = img.height, N = W * Hh, rect = new Rect(W, Hh);
      var hdr = dup.mainView.image;
      var c = [], h = [], ch, i;
      for (ch = 0; ch < 3; ++ch) {
         c.push(new Float32Array(N)); img.getSamples(c[ch], rect, ch);
         h.push(new Float32Array(N)); hdr.getSamples(h[ch], rect, ch);
      }
      var chromaBefore = 0, chromaAfter = 0, nb = 0;
      for (i = 0; i < N; i += 97) { chromaBefore += Math.abs(c[0][i] - c[1][i]) + Math.abs(c[2][i] - c[1][i]); ++nb; }
      for (i = 0; i < N; ++i) {
         var L = 0.2126 * c[0][i] + 0.7152 * c[1][i] + 0.0722 * c[2][i];
         var k = (L - HDR_LO) / HDR_SPAN;
         if (k <= 0) continue;
         if (k > 1) k = 1;
         k = k * k * (3 - 2 * k);
         c[0][i] = c[0][i] * (1 - k) + h[0][i] * k;
         c[1][i] = c[1][i] * (1 - k) + h[1][i] * k;
         c[2][i] = c[2][i] * (1 - k) + h[2][i] * k;
      }
      for (i = 0; i < N; i += 97) chromaAfter += Math.abs(c[0][i] - c[1][i]) + Math.abs(c[2][i] - c[1][i]);
      if (chromaAfter < 0.5 * chromaBefore) throw new Error("blend lost color (chroma " + (chromaAfter / nb).toFixed(5) + " vs " + (chromaBefore / nb).toFixed(5) + "); target left unchanged");
      view.beginProcess(UndoFlag_NoSwapFile);
      for (ch = 0; ch < 3; ++ch) img.setSamples(c[ch], rect, ch);
      view.endProcess();
      log("core blend: mean chroma " + (chromaBefore / nb).toFixed(5) + " -> " + (chromaAfter / nb).toFixed(5));
      log("core: HDRMultiscaleTransform " + HDR_LAYERS + " layers, blended above L=" + HDR_LO);
      step("core_hdr", "HDRMultiscaleTransform", "ran", { layers: HDR_LAYERS, from_l: HDR_LO });
   } catch (e) {
      log("core HDR skipped (" + e + ")");
      step("core_hdr", "HDRMultiscaleTransform", "failed", { error: "" + e });
   }
   if (dup) try { dup.forceClose(); } catch (e2) {}
}

function frameCrop(view, edgeFrac) {
   if (!FRAME) return;
   try {
      clearSolution(view);
      var img = view.image;
      // the edge crop already removed edgeFrac on every side; map FRAME into it
      var W0 = img.width / (1 - 2 * edgeFrac), H0 = img.height / (1 - 2 * edgeFrac);
      var ex = Math.round(W0 * edgeFrac), ey = Math.round(H0 * edgeFrac);
      var l = Math.max(0, Math.round(W0 * FRAME.left) - ex);
      var t = Math.max(0, Math.round(H0 * FRAME.top) - ey);
      var r = Math.max(0, img.width - (Math.round(W0 * FRAME.right) - ex));
      var b = Math.max(0, img.height - (Math.round(H0 * FRAME.bottom) - ey));
      var CR = new Crop;
      CR.mode = Crop.prototype.AbsolutePixels;
      CR.leftMargin = -l; CR.topMargin = -t; CR.rightMargin = -r; CR.bottomMargin = -b;
      CR.executeOn(view, false);
      log("framing crop -> " + view.image.width + "x" + view.image.height);
      step("frame_crop", "Crop", "ran", FRAME);
   } catch (e) { log("framing crop skipped (" + e + ")"); step("frame_crop", "Crop", "failed", { error: "" + e }); }
}

function trySave(w, path, label) {
   try {
      if (w.saveAs(path, false, false, false, false)) { log("saved " + label + ": " + path); return true; }
      log("save FAILED (returned false): " + path);
   } catch (e) { log("save FAILED " + path + " (" + e + ")"); }
   return false;
}

// Label the output with the steps that ran (FITS keyword PSFINISH; the full
// record is <Name>_final_steps.json next to it).
function labelOutput(w) {
   try {
      var kw = w.keywords;
      kw.push(new FITSKeyword("PSFINISH", "'" + stepTag().substring(0, 66) + "'", "PhotonScript finish steps"));
      w.keywords = kw;
   } catch (e) { log("PSFINISH keyword skipped (" + e + ")"); }
}

function main() {
   console.show();
   log("finish: " + MASTER + "  target=" + NAME + "  RA=" + RA_DEG + " Dec=" + DEC_DEG);
   log("output: " + FINAL);
   if (!File.exists(MASTER)) throw new Error("no master at " + MASTER + " - run run-integration-osc.ps1 first");
   ensureDir(FINAL);
   var ws = ImageWindow.open(MASTER);
   if (!ws.length) throw new Error("could not open " + MASTER);
   var w = ws[0];
   var v = w.mainView;

   cropEdges(v, EDGE_FRAC);
   removeGradient(v);
   var solved = plateSolve(w);
   colorCalibrate(v, solved);
   removeGreen(v);
   trySave(w, FINAL + "/" + NAME + "_linear.xisf", "linear (color-calibrated)");

   rcTools(v);
   stretch(v);
   recoverCore(v);
   frameCrop(v, EDGE_FRAC);
   try {
      var st = v.image, mm = [];
      for (var q = 0; q < 3; ++q) { st.selectedChannel = q; mm.push(st.mean().toFixed(4)); }
      st.resetSelections();
      log("final channel means R/G/B: " + mm.join(" / "));
   } catch (e) { log("stats skipped (" + e + ")"); }
   log("steps: " + stepTag());
   labelOutput(w);
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
   log("DONE");
}

try { main(); writeSteps("ok"); log("EXIT OK"); }
catch (e) {
   writeSteps("error: " + e.toString());
   log("ERROR: " + e.toString()); console.criticalln("[FINISH] FAILED: " + e.toString());
}
writeLog();
