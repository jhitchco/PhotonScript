// PhotonScript finishing pipeline (PJSR): master -> finished image. The ONE
// finish implementation, with two launchers that fill the __PLACEHOLDERS__
// and, when PixInsight's ImageSolver script exists, the solver include below:
//   deploy/run-finish-osc.ps1   one OSC master (MASTERS = null)
//   photonscript integrate      every master of a run (MASTERS = a list;
//                               photonscript/integration/pjsr.py render_finish)
// Mono masters (RC16 per filter) skip the color steps (color calibration,
// SCNR, core HDR blend, saturation).
//
//   crop registration edges -> gradient removal (GradientCorrection, else ABE)
//   -> plate solve (ImageSolver, spiral search around the target)
//   -> color: SPCC against Gaia DR3/SP when that database is configured
//      (fallback: BackgroundNeutralization + ColorCalibration) -> SCNR green
//   -> deconvolution: BlurXTerminator (a correct-only pass, then the sharpen
//      pass with BXT_STARS / BXT_NONSTELLAR, PSF auto), else GraXpert CLI,
//      else skipped
//   -> noise reduction: NoiseXTerminator (NXT_DENOISE, detail 0.15), else
//      GraXpert CLI, else built-in MultiscaleLinearTransform through a
//      linear mask
//   -> star reduction: split the linear image into starless + stars with
//      StarXTerminator, else StarNet2; stretch the starless image, core HDR
//      blend on it, screen the gently stretched stars back at -StarStrength
//      (neither installed: linked stretch -> core HDR blend on the whole image)
//   -> gentle saturation (part of the stretch) -> [framing crop]
//   -> [HOO = on, color masters only (PS-161): a second, HOO-mapped image
//      from the color-calibrated linear data, Ha = R, OIII = mean of G and B
//      scaled to the Ha background, R = Ha, G = B = OIII, same linked
//      stretch and framing -> <Name>_hoo.{xisf,jpg} beside the natural one]
//   -> [PALETTE = gold, color masters only: a gold-blue "HOO-style" image
//      from the same linear copy, R = Ha, G = GOLD_MIX x Ha + (1 - GOLD_MIX)
//      x OIII, B = OIII, mapped on the starless copy with the natural RGB
//      stars screened back (StarXTerminator, else StarNet2; neither: the
//      whole image), linked stretch + saturation, a gentle hue curve
//      (Ha orange-gold, OIII teal-blue) -> <Name>_gold.{xisf,jpg}]
//   -> [VIVID = on, color masters only: after the natural final is saved, a
//      second look from the same stretched starless image (copied just
//      before the stars go back; no star split: the stretched image): more
//      saturation (VIVID_SAT_MID instead of SAT_MID) and a selective chroma
//      lift (pink / magenta everywhere, warm yellow in the core region, blue
//      in the outer arms, VIVID_HUE_BOOST) through a luminance-range mask
//      that leaves the sky background and the brightest core alone, optional
//      LocalHistogramEqualization through the same mask (VIVID_LHE, 0 = off),
//      the natural star layer screened back -> <Name>_vivid.{xisf,tif,jpg}]
//
// Why deconvolution and noise reduction sit here (PS-41): both model the
// LINEAR signal. Deconvolution inverts a convolution, which only holds before
// the non-linear stretch; it goes first so it sharpens real detail and not
// the smoothing left by noise reduction. Noise reduction on linear data sees
// noise that is still uniform and unamplified; after a stretch the faint
// background noise is boosted far more than the bright core. Both run after
// color calibration so SPCC measures untouched star photometry.
//   -> <Final>/<Name>_final.{xisf,tif,jpg} + <Name>_linear.xisf
//   + <Name>_final_steps.json (which step ran with which tool and settings)
//
// Every optional step is guarded: if a process or third-party tool is missing
// or fails, it is logged and skipped, never fatal.
// RC Astro (PS-186): BlurXTerminator / NoiseXTerminator / StarXTerminator are
// detected at run time (typeof X === "function"), never assumed: the docs and
// scripts can be installed while the module itself is not. RC_MODE auto = use
// what is installed; on = the same, but a missing tool is logged as a
// WARNING; off = never. The steps json gets an "rcastro" record: per tool
// used / partial / skipped / failed, why, settings, and the version / AI model
// file when the process exposes them.
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
// RC Astro (PS-186): auto | on | off (config finish_use_rcastro)
var RC_MODE        = "__RC_MODE__";
var USE_RC         = (RC_MODE !== "off");
var BXT_STARS      = __BXT_STARS__;        // BlurXTerminator sharpen_stars (0..1)
var BXT_NONSTELLAR = __BXT_NONSTELLAR__;   // BlurXTerminator sharpen_nonstellar (0..1)
var NXT_DENOISE    = __NXT_DENOISE__;      // NoiseXTerminator denoise (0..1; DENOISE 0 still = no NR)
var NXT_DETAIL     = 0.15;                 // NoiseXTerminator detail
// Noise reduction + deconvolution (PS-41)
var DENOISE         = __DENOISE__;          // 0..1 strength; 0 = off
var DECONV          = "__DECONV__";         // on | off
var DECONV_STRENGTH = __DECONV_STRENGTH__;  // 0..1 (GraXpert object strength; stellar gets half)
var GRAXPERT        = "__GRAXPERT__";       // GraXpert executable; "" = not found / disabled
var GRAXPERT_VERSION = "__GRAXPERT_VERSION__";
var GRAXPERT_AI     = "__GRAXPERT_AI__";    // -ai_version passed to GraXpert; "" = its default
var GRAXPERT_GPU    = "__GRAXPERT_GPU__";   // -gpu true|false; "" = its default
var GRAXPERT_TIMEOUT_MIN = __GRAXPERT_TIMEOUT_MIN__;
// Star reduction (PS-40)
var STARS         = "__STARS__";          // on | off (needs the StarNet2 module)
var STAR_STRENGTH = __STAR_STRENGTH__;    // k in ~(~starless * ~(stars * k)); 1 = stars unchanged
var STAR_FLOOR_SIGMA = 2.0;               // stars below this x linear noise are dropped
var STARNET_PRE_BG   = 0.25;              // background level of StarNet2's reversible pre-stretch
// OSC HOO extraction (PS-161): on = also write <Name>_hoo.{xisf,jpg}
var HOO = "__HOO__";                      // on | off
// Gold-blue palette for OSC finishes: off | gold (config finish_palette)
var PALETTE  = "__PALETTE__";
var GOLD_MIX = __GOLD_MIX__;              // Ha share of the green channel (0..1); OIII gets the rest
var PALETTE_WHY = "__PALETTE_WHY__";      // why the palette is off (finish_palette_kinds); "" = not asked
// Vivid variant (config finish_vivid): on = also <Name>_vivid.{xisf,tif,jpg}
var VIVID           = "__VIVID__";          // on | off
var VIVID_SAT_MID   = __VIVID_SAT_MID__;    // saturation midpoint of the vivid look (the natural has SAT_MID)
var VIVID_HUE_BOOST = __VIVID_HUE_BOOST__;  // chroma lift of the selected hues (0 = none, 0.25 = up to x1.25)
var VIVID_LHE       = __VIVID_LHE__;        // LocalHistogramEqualization amount through the mask (0 = off)
// Hue curve after the gold stretch (CurvesTransformation H, linear, hue
// 0 = red, 1/6 = yellow, 1/2 = cyan, 2/3 = blue): red-orange and yellow move
// toward orange-gold, azure toward teal-blue. Monotonic, ends fixed.
var GOLD_HUE = [[0, 0], [0.04, 0.07], [0.15, 0.115], [0.5, 0.5], [0.6, 0.55], [0.7, 0.64], [1, 1]];
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
// null = the one MASTER / NAME above; else [{name, path}, ...], each finished
// in turn with its own <name>_final_steps.json (log ends EXIT OK only when
// every master finished).
var MASTERS = __MASTERS__;

// ==== PS_FINISH_LIB_BEGIN (deploy/finish_mono_color.js reuses the functions from here
// to PS_FINISH_LIB_END; keep them free of placeholders)
var LOGLINES = [];
var STEPS = [];   // one record per pipeline step, written to <Name>_final_steps.json
var RC = {};      // PS-186: per RC Astro tool (bxt, nxt, sxt): status, why, settings
var PALETTE_SKIP_WHY = "";   // why the palette is off (finish_osc.js main sets it from PALETTE_WHY)
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
      if (!LABEL_STEPS[s.step] || (s.status !== "ran" && s.status !== "fallback")) continue;
      var lab = s.tool;
      if (s.tool === "GraXpert" || s.tool === "MLT") lab += (s.step === "deconvolution") ? "-deconv" : "-NR";
      t.push(lab);
   }
   return t.length ? t.join("+") : "basic";
}
function writeSteps(result) {
   try {
      ensureDir(FINAL);
      var doc = { name: NAME, master: MASTER, result: result, tag: stepTag(),
                  finished_utc: (new Date).toISOString(), finish: { steps: STEPS },
                  rcastro: { mode: RC_MODE, bxt: RC.bxt || null, nxt: RC.nxt || null, sxt: RC.sxt || null } };
      var f = new File; f.createForWriting(FINAL + "/" + NAME + "_final_steps.json");
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}

function mtfv(m, x) { if (x <= 0) return 0; if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m); }

// A Crop invalidates the WCS; clear it first so PixInsight does not stop on a
// Yes/No dialog in an unattended run.
function clearWcs(view) {
   try { view.window.clearAstrometricSolution(); } catch (e) { log("clearAstrometricSolution: " + e); }
}

// ---------------------------------------------------------------- steps

function cropEdges(view, frac) {
   clearWcs(view);
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
      // The enum constant is not exposed as prototype.Subtract on every
      // build (1.9.5 threw "unsigned integer value expected"); 1 = subtract.
      var ABEP = AutomaticBackgroundExtractor.prototype;
      ABE.targetCorrection = (ABEP.Subtract !== undefined) ? ABEP.Subtract :
         ((ABEP.Correction_Subtract !== undefined) ? ABEP.Correction_Subtract : 1);
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
   // the STAGE lines let the runner's hang watchdog use its shorter solve
   // limit (pixinsight_solve_stall_min): ImageSolver logs nothing here, and
   // with no Gaia database it falls back to a VizieR download that can hang
   log("STAGE START " + NAME + " / plate solve");
   var solved = false;
   try { solved = plateSolveSeeds(window); }
   finally { log("STAGE END   " + NAME + " / plate solve"); }
   return solved;
}

function plateSolveSeeds(window) {
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
   if (!view.image.isColor) {
      log("color: mono master, no color calibration");
      step("color", "none", "skipped", { reason: "mono master" });
      return;
   }
   var why = "";
   if (COLOR === "basic") why = "-Color basic";
   else if (!solved) why = "no astrometric solution";
   else if (typeof SpectrophotometricColorCalibration === "undefined") why = "SPCC process not installed";
   if (!why) {
      // a STAGE of its own for the hang watchdog's solve limit (see plateSolve)
      log("STAGE START " + NAME + " / SPCC");
      try {
         var g = gaiaDr3SpReady();
         if (g.ok) log("color: " + g.why);
         if (!g.ok && COLOR !== "spcc") why = g.why;
         if (!why) {
            try {
               var used = spcc(view);
               log("color: SPCC applied (catalog " + used.catalog + ", QE " + used.qe + ", RGB " +
                   used.red + " / " + used.green + " / " + used.blue + ", white " + used.white + ")");
               step("color", "SPCC", "ran", used);
               return;
            } catch (e) { why = "SPCC failed (" + e + ")"; }
         }
      } finally { log("STAGE END   " + NAME + " / SPCC"); }
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
   if (!view.image.isColor) { step("scnr", "SCNR", "skipped", { reason: "mono master" }); return; }
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

// A float copy of a view in a new hidden window (caller closes it).
function dupWindow(view, id) {
   var img = view.image;
   var w = new ImageWindow(img.width, img.height, img.numberOfChannels, 32, true,
                           img.isColor, id);
   w.mainView.beginProcess(UndoFlag_NoSwapFile);
   w.mainView.image.assign(img);
   w.mainView.endProcess();
   return w;
}

function removeQuiet(path) { try { if (File.exists(path)) File.remove(path); } catch (e) {} }

// Run one GraXpert CLI command on the view, in place:
//   GraXpert <in.fits> -cli -cmd <cmd> -output <base> -strength <s> [-ai_version v] [-gpu b]
// Throws on any failure; the view is only replaced when GraXpert returned 0
// and its output has the same size and a plausible background level.
function graxpert(view, cmd, strength) {
   var tmp = FINAL + "/_graxpert_tmp";
   ensureDir(tmp);
   var inF = tmp + "/in_" + cmd + ".fits", outBase = tmp + "/out_" + cmd;
   var cands = [outBase + ".fits", outBase + ".fit", outBase + ".xisf", outBase + ".tif", outBase + ".tiff",
                tmp + "/in_" + cmd + "_GraXpert.fits"];
   var dup = null, res = null;
   try {
      dup = dupWindow(view, "PS_GX_IN");
      if (!dup.saveAs(inF, false, false, false, false)) throw new Error("could not write " + inF);
      dup.forceClose(); dup = null;
      for (var c = 0; c < cands.length; ++c) removeQuiet(cands[c]);
      var args = [inF, "-cli", "-cmd", cmd, "-output", outBase, "-strength", strength.toFixed(2)];
      if (GRAXPERT_AI) args.push("-ai_version", GRAXPERT_AI);
      if (GRAXPERT_GPU) args.push("-gpu", GRAXPERT_GPU);
      log("  GraXpert " + cmd + " (strength " + strength.toFixed(2) + ")...");
      var P = new ExternalProcess;
      P.start(GRAXPERT, args);
      if (!P.waitForStarted()) throw new Error("did not start");
      var t0 = Date.now();
      while (!P.waitForFinished(1000)) {
         processEvents();
         if (Date.now() - t0 > GRAXPERT_TIMEOUT_MIN * 60000) {
            try { P.kill(); } catch (ek) { try { P.terminate(); } catch (et) {} }
            throw new Error("timed out after " + GRAXPERT_TIMEOUT_MIN + " min");
         }
      }
      if (P.exitCode !== 0) {
         var err = "";
         try { err = ("" + P.stderr).replace(/\s+/g, " ").slice(-200); } catch (es) {}
         throw new Error("exit code " + P.exitCode + (err ? ": " + err : ""));
      }
      var outF = "";
      for (c = 0; c < cands.length && !outF; ++c) if (File.exists(cands[c])) outF = cands[c];
      if (!outF) throw new Error("no output file next to " + outBase);
      var ws = ImageWindow.open(outF);
      if (!ws.length) throw new Error("could not open " + outF);
      res = ws[0];
      var a = view.image, b = res.mainView.image;
      if (a.width !== b.width || a.height !== b.height || a.numberOfChannels !== b.numberOfChannels)
         throw new Error("output is " + b.width + "x" + b.height + "x" + b.numberOfChannels +
                         ", expected " + a.width + "x" + a.height + "x" + a.numberOfChannels);
      var m0 = a.median(), m1 = b.median();
      if (!(m1 > 0.5 * m0 - 1e-4 && m1 < 2 * m0 + 1e-4))
         throw new Error("output background " + m1.toFixed(5) + " vs input " + m0.toFixed(5) + "; not applied");
      view.beginProcess(UndoFlag_NoSwapFile);
      view.image.assign(b);
      view.endProcess();
      return (Date.now() - t0) / 1000;
   } finally {
      if (dup) try { dup.forceClose(); } catch (e1) {}
      if (res) try { res.forceClose(); } catch (e2) {}
      removeQuiet(inF);
      for (var k = 0; k < cands.length; ++k) removeQuiet(cands[k]);
      try { File.removeDirectory(tmp); } catch (e3) {}
   }
}

// ---------------------------------------------------------------- RC Astro (PS-186)

// Is the RC Astro process installed in THIS PixInsight? A literal typeof per
// tool (an undeclared name is "undefined", never an error).
function rcInstalled(tool) {
   if (tool === "BlurXTerminator") return typeof BlurXTerminator === "function";
   if (tool === "NoiseXTerminator") return typeof NoiseXTerminator === "function";
   if (tool === "StarXTerminator") return typeof StarXTerminator === "function";
   return false;
}

// Should the step use this RC Astro tool? Records why not in RC[key] (and the
// log; a WARNING when RC_MODE is on and the tool is missing).
function rcUse(tool, key) {
   if (!USE_RC) {
      RC[key] = { tool: tool, status: "skipped", why: "finish_use_rcastro off" };
      log(tool + ": not used (finish_use_rcastro off)");
      return false;
   }
   if (!rcInstalled(tool)) {
      RC[key] = { tool: tool, status: "skipped", why: "not installed (no " + tool + " process in this PixInsight)" };
      log((RC_MODE === "on" ? "WARNING: finish_use_rcastro on but " : "") + tool +
          ": not installed; using the fallback");
      return false;
   }
   return true;
}

// Set one process parameter; set[prop] records it when the assignment worked.
function trySet(P, prop, val, set) {
   try { P[prop] = val; set[prop] = val; } catch (e) { log("  " + prop + " not set (" + e + ")"); }
}

// Version and AI model file of an RC Astro instance, when PJSR exposes them.
function rcInfo(P, info) {
   try { if (P.ai_file) info.ai_model = "" + P.ai_file; } catch (e1) {}
   try { if (typeof P.processVersion === "function") info.version = "" + P.processVersion(); } catch (e2) {}
   try { if (!info.version && P.version !== undefined && P.version !== null) info.version = "" + P.version; } catch (e3) {}
   return info;
}

// BlurXTerminator on the LINEAR image: a correct-only pass (PSF / aberration
// correction across the field, no sharpening), then the sharpen pass
// (BXT_STARS, BXT_NONSTELLAR, halos untouched, nonstellar PSF measured by
// BXT). Returns the passes that ran; RC.bxt holds the record.
function bxtDeconvolve(view) {
   var rec = { tool: "BlurXTerminator", status: "failed", passes: [], settings: {} };
   RC.bxt = rec;
   try {
      var B1 = new BlurXTerminator;
      var s1 = {};
      trySet(B1, "correct_only", true, s1);
      if (!B1.executeOn(view, false)) throw new Error("returned false");
      rcInfo(B1, rec);
      rec.passes.push("correct_only");
      rec.settings.correct = s1;
      log("deconvolution: BlurXTerminator correct-only pass done");
   } catch (e1) {
      log("BlurXTerminator correct-only pass failed (" + e1 + ")");
      rec.correct_error = "" + e1;
   }
   try {
      var B2 = new BlurXTerminator;
      var s2 = {};
      trySet(B2, "correct_only", false, s2);
      trySet(B2, "correct_first", false, s2);        // the first pass already corrected
      trySet(B2, "sharpen_stars", BXT_STARS, s2);
      trySet(B2, "sharpen_nonstellar", BXT_NONSTELLAR, s2);
      trySet(B2, "adjust_halos", 0.0, s2);
      trySet(B2, "auto_nonstellar_psf", true, s2);   // PSF auto
      if (!B2.executeOn(view, false)) throw new Error("returned false");
      rcInfo(B2, rec);
      rec.passes.push("sharpen");
      rec.settings.sharpen = s2;
      log("deconvolution: BlurXTerminator sharpen pass (stars " + BXT_STARS + ", nonstellar " +
          BXT_NONSTELLAR + ", PSF auto)");
   } catch (e2) {
      log("BlurXTerminator sharpen pass failed (" + e2 + ")");
      rec.sharpen_error = "" + e2;
   }
   rec.status = rec.passes.length === 2 ? "used" : (rec.passes.length ? "partial" : "failed");
   if (rec.status !== "used") rec.why = rec.passes.length ? "sharpen pass failed" : "both passes failed";
   return rec.passes;
}

// NoiseXTerminator on the LINEAR image (NXT_DENOISE, NXT_DETAIL). Throws on failure.
function nxtDenoise(view) {
   var rec = { tool: "NoiseXTerminator", status: "failed", settings: {} };
   RC.nxt = rec;
   var NX = new NoiseXTerminator;
   trySet(NX, "denoise", NXT_DENOISE, rec.settings);
   trySet(NX, "detail", NXT_DETAIL, rec.settings);
   if (!NX.executeOn(view, false)) throw new Error("returned false");
   rcInfo(NX, rec);
   rec.status = "used";
   return rec;
}

// Deconvolution (linear): BlurXTerminator if installed, else GraXpert
// (deconv-obj, then deconv-stellar at half strength), else skipped. There is
// no built-in fallback on purpose: classic Deconvolution needs a measured PSF
// and a tuned deringing mask, and a wrong guess rings around every star.
function deconvolve(view) {
   if (DECONV !== "on") {
      log("deconvolution: off (-Deconv off)");
      step("deconvolution", "none", "skipped", { reason: "-Deconv off" });
      RC.bxt = { tool: "BlurXTerminator", status: "skipped", why: "-Deconv off" };
      return;
   }
   var bxtNote = "";
   if (rcUse("BlurXTerminator", "bxt")) {
      var passes = [];
      try { passes = bxtDeconvolve(view); }
      catch (e) { log("BlurXTerminator failed (" + e + ")"); RC.bxt = { tool: "BlurXTerminator", status: "failed", why: "" + e }; }
      if (passes.length === 2) {
         var st = { stars: BXT_STARS, nonstellar: BXT_NONSTELLAR, psf: "auto", passes: passes };
         if (RC.bxt.version) st.version = RC.bxt.version;
         if (RC.bxt.ai_model) st.ai_model = RC.bxt.ai_model;
         step("deconvolution", "BlurXTerminator", "ran", st);
         return;
      }
      bxtNote = passes.length ? "BlurXTerminator corrected only (sharpen pass failed)" : "BlurXTerminator failed";
      log(bxtNote + "; trying the fallback");
   }
   if (GRAXPERT) {
      var done = [];
      var jobs = [["deconv-obj", DECONV_STRENGTH], ["deconv-stellar", DECONV_STRENGTH / 2]];
      for (var j = 0; j < jobs.length; ++j) {
         try {
            var sec = graxpert(view, jobs[j][0], jobs[j][1]);
            log("deconvolution: GraXpert " + jobs[j][0] + " strength " + jobs[j][1].toFixed(2) +
                " (" + sec.toFixed(0) + " s)");
            done.push(jobs[j][0]);
         } catch (e2) { log("GraXpert " + jobs[j][0] + " failed (" + e2 + ")"); }
      }
      if (done.length) {
         var gs = { commands: done, strength: DECONV_STRENGTH, version: GRAXPERT_VERSION };
         if (bxtNote) gs.rcastro = bxtNote;
         step("deconvolution", "GraXpert", "ran", gs);
         return;
      }
      step("deconvolution", "GraXpert", "failed", { version: GRAXPERT_VERSION });
      log("deconvolution: skipped (GraXpert failed; no built-in fallback)");
      return;
   }
   log("deconvolution: skipped (no BlurXTerminator, GraXpert not found; no built-in fallback)");
   step("deconvolution", "none", "skipped", { reason: bxtNote || "no BlurXTerminator or GraXpert" });
}

// Built-in noise reduction: MultiscaleLinearTransform on the linear image,
// 4 starlet layers, through an inverted linear mask so the galaxy and stars
// are protected and the background gets the full amount.
function mltDenoise(view, amount) {
   var M = new MultiscaleLinearTransform;
   // enabled, biasEnabled, bias, noiseReductionEnabled, threshold, amount, iterations
   var rows = [[true, true, 0.0, true, 3.0, amount, 3],
               [true, true, 0.0, true, 2.0, amount, 2],
               [true, true, 0.0, true, 1.0, amount, 2],
               [true, true, 0.0, true, 0.5, amount, 1],
               [true, true, 0.0, false, 3.0, 1.0, 1]];
   M.layers = rows;
   try { M.transform = MultiscaleLinearTransform.prototype.StarletTransform; } catch (e1) {}
   try { M.linearMask = true; M.linearMaskAmpFactor = 100; M.linearMaskSmoothness = 1.0;
         M.linearMaskInverted = true; M.linearMaskPreview = false; } catch (e2) { log("  MLT linear mask not set (" + e2 + ")"); }
   if (!M.executeOn(view, false)) throw new Error("returned false");
}

// Noise reduction (linear): NoiseXTerminator if installed, else GraXpert
// denoising, else the built-in MultiscaleLinearTransform.
function denoise(view) {
   if (!(DENOISE > 0)) {
      log("noise reduction: off (-Denoise 0)");
      step("noise_reduction", "none", "skipped", { reason: "-Denoise 0" });
      RC.nxt = { tool: "NoiseXTerminator", status: "skipped", why: "-Denoise 0" };
      return;
   }
   if (rcUse("NoiseXTerminator", "nxt")) {
      try {
         var r = nxtDenoise(view);
         log("noise reduction: NoiseXTerminator (denoise " + NXT_DENOISE + ", detail " + NXT_DETAIL + ")");
         var ns = { denoise: NXT_DENOISE, detail: NXT_DETAIL };
         if (r.version) ns.version = r.version;
         if (r.ai_model) ns.ai_model = r.ai_model;
         step("noise_reduction", "NoiseXTerminator", "ran", ns);
         return;
      } catch (e) {
         log("NoiseXTerminator failed (" + e + "); trying the fallback");
         RC.nxt = { tool: "NoiseXTerminator", status: "failed", why: "" + e };
      }
   }
   if (GRAXPERT) {
      try {
         var sec = graxpert(view, "denoising", DENOISE);
         log("noise reduction: GraXpert denoising strength " + DENOISE + " (" + sec.toFixed(0) + " s)");
         step("noise_reduction", "GraXpert", "ran", { strength: DENOISE, version: GRAXPERT_VERSION });
         return;
      } catch (e2) { log("GraXpert denoising failed (" + e2 + "); using the built-in"); }
   } else log("GraXpert: not found (pass -GraXpert <exe>); using the built-in noise reduction");
   if (typeof MultiscaleLinearTransform === "undefined") {
      log("noise reduction: skipped (MultiscaleLinearTransform not available)");
      step("noise_reduction", "none", "skipped", { reason: "no tool" });
      return;
   }
   try {
      var amt = Math.min(1, DENOISE);
      mltDenoise(view, amt);
      log("noise reduction: MultiscaleLinearTransform (4 layers, thresholds 3/2/1/0.5, amount " +
          amt.toFixed(2) + ", inverted linear mask)");
      step("noise_reduction", "MLT", "ran", { amount: amt, thresholds: [3, 2, 1, 0.5], linear_mask: true });
   } catch (e3) {
      log("noise reduction skipped: MultiscaleLinearTransform failed (" + e3 + ")");
      step("noise_reduction", "MLT", "failed", { error: "" + e3 });
   }
}

// Linked HistogramTransformation: shadows c0, midtones m, highlights 1.
function applyHT(view, c0, m) {
   var HT = new HistogramTransformation;
   HT.H = [[0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1],
           [c0, m, 1, 0, 1], [0, 0.5, 1, 0, 1]];
   if (!HT.executeOn(view, false)) throw new Error("HistogramTransformation returned false");
}

// Linked stretch after color calibration (unlinked would undo the color work).
// Returns the shadows/midtones it applied.
function stretch(view, what) {
   var img = view.image;
   var med = img.median();
   var mad = img.MAD() * 1.4826;
   var c0 = Math.max(0, Math.min(1, med - SHADOW_SIGMA * mad));
   var m = mtfv(BG_TARGET, Math.max(1.0e-6, med - c0));
   applyHT(view, c0, m);
   log("stretch" + (what ? " (" + what + ")" : "") + ": linked, shadows " + c0.toFixed(5) + ", midtones " + m.toFixed(5));
   step("stretch", "HistogramTransformation", "ran",
        { image: what || "full", shadows: +c0.toFixed(6), midtones: +m.toFixed(6), bg_target: BG_TARGET, shadow_sigma: SHADOW_SIGMA });
   if (SAT_MID !== 0.5 && img.isColor) {
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
   if (!view.image.isColor) { step("core_hdr", "HDRMultiscaleTransform", "skipped", { reason: "mono master" }); return; }
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

function pixelMath(view, expr) {
   var PM = new PixelMath;
   PM.expression = expr;
   PM.useSingleExpression = true;
   PM.createNewImage = false;
   PM.rescale = false;
   PM.truncate = true;
   if (!PM.executeOn(view, false)) throw new Error("PixelMath returned false: " + expr);
}

// Star reduction, part 1 (PS-40, PS-186): split the LINEAR image into
// starless and stars with StarXTerminator, else StarNet2. Both are trained on
// stretched data, so the copy gets a reversible pre-stretch (midtones only, no
// clipping), the tool runs, and the inverse midtones transform takes the
// starless image back to linear. stars = max(0, linear - starless), the exact
// complement, so the screen recombination at the end (part 2) is the
// unscreen-style workflow: SXT runs with unscreen on and no star image of its
// own. A failed StarXTerminator falls back to StarNet2. Returns null (and
// leaves the view alone) if no tool is installed, star reduction is off or
// every tool fails.
function starlessPre(view, mPre, tool) {
   var w = dupWindow(view, "PS_starless");
   try {
      applyHT(w.mainView, 0, mPre);
      var set = {};
      if (tool === "StarXTerminator") {
         var SX = new StarXTerminator;
         trySet(SX, "stars", false, set);       // starless only; stars = linear - starless
         trySet(SX, "unscreen", true, set);
         trySet(SX, "overlap", 0.20, set);
         if (!SX.executeOn(w.mainView, false)) throw new Error("StarXTerminator returned false");
         var info = rcInfo(SX, {});
         RC.sxt = { tool: "StarXTerminator", status: "used", settings: set };
         if (info.version) RC.sxt.version = info.version;
         if (info.ai_model) RC.sxt.ai_model = info.ai_model;
         try {   // a stray star image (older SXT ignores stars = false)
            var extra = ImageWindow.windowById(w.mainView.id + "_stars");
            if (extra && !extra.isNull) extra.forceClose();
         } catch (e0) {}
      } else {
         var SN = new StarNet2;
         try { SN.mask = false; } catch (e1) {}      // replace the target with the starless image
         try { SN.linear = false; } catch (e2) {}    // input is already pre-stretched
         if (!SN.executeOn(w.mainView, false)) throw new Error("StarNet2 returned false");
      }
      applyHT(w.mainView, 0, 1 - mPre);              // inverse midtones: back to linear
      return w;
   } catch (e) {
      try { w.forceClose(); } catch (e3) {}
      throw e;
   }
}

function starSplit(view) {
   if (STARS !== "on") {
      log("star reduction: off (-StarReduction off)");
      step("star_reduction", "none", "skipped", { reason: "-StarReduction off" });
      RC.sxt = { tool: "StarXTerminator", status: "skipped", why: "-StarReduction off" };
      return null;
   }
   var tools = [];
   if (rcUse("StarXTerminator", "sxt")) tools.push("StarXTerminator");
   if (typeof StarNet2 !== "undefined") tools.push("StarNet2");
   if (!tools.length) {
      log("star reduction: neither StarXTerminator nor StarNet2 installed; skipped");
      step("star_reduction", "StarNet2", "skipped", { reason: "not installed" });
      return null;
   }
   var mPre = mtfv(STARNET_PRE_BG, Math.max(1.0e-6, view.image.median()));
   var errors = [];
   for (var t = 0; t < tools.length; ++t) {
      var sl = null, st = null;
      try {
         sl = starlessPre(view, mPre, tools[t]);
         st = dupWindow(view, "PS_stars");
         pixelMath(st.mainView, "max(0, $T - " + sl.mainView.id + ")");
         log("star reduction: " + tools[t] + " split (pre-stretch midtones " + mPre.toFixed(5) + ")");
         return { starless: sl, stars: st, preM: mPre, tool: tools[t], errors: errors };
      } catch (e) {
         log("star reduction: " + tools[t] + " split failed (" + e + ")");
         errors.push(tools[t] + ": " + e);
         if (tools[t] === "StarXTerminator") RC.sxt = { tool: "StarXTerminator", status: "failed", why: "" + e };
         if (sl) try { sl.forceClose(); } catch (e3) {}
         if (st) try { st.forceClose(); } catch (e4) {}
      }
   }
   log("star reduction skipped: every split failed");
   step("star_reduction", tools[tools.length - 1], "failed", { error: errors.join("; ") });
   return null;
}

// Star reduction, part 2: stretch the starless image with the normal knobs
// (it has no stars, so its own statistics give a slightly harder stretch),
// recover the core on it, save it, stretch the stars gently (floor at
// STAR_FLOOR_SIGMA x the linear noise so star-removal residue does not come
// back as grain, same midtones), then screen them back at STAR_STRENGTH:
//   final = ~(~starless * ~(stars * k))
// The result replaces the view only at the end; on any failure the view is
// still linear and the caller does the normal stretch.
function starRecombine(view, split) {
   var ok = false;
   try {
      var slv = split.starless.mainView, stv = split.stars.mainView;
      var p = stretch(slv, "starless");
      recoverCore(slv);
      trySave(split.starless, FINAL + "/" + NAME + "_starless.xisf", "starless (stretched)");
      var sigma = view.image.MAD() * 1.4826;
      var cs = Math.max(0, Math.min(0.5, STAR_FLOOR_SIGMA * sigma));
      applyHT(stv, cs, p.m);
      // vivid variant (finish_osc.js only): copy the stretched starless + stars
      if (typeof vividCapture === "function") vividCapture(slv, stv);
      var k = STAR_STRENGTH.toFixed(3);
      pixelMath(slv, "~(~$T * ~(" + stv.id + " * " + k + "))");
      view.beginProcess(UndoFlag_NoSwapFile);
      view.image.assign(slv.image);
      view.endProcess();
      log("star reduction: stars screened back at " + k + " (star floor " + cs.toFixed(6) + ")");
      var ss = { star_strength: STAR_STRENGTH, star_floor: +cs.toFixed(6), prestretch_m: +split.preM.toFixed(6),
                 starless_file: NAME + "_starless.xisf" };
      if (split.errors.length) ss.fallback_from = split.errors.join("; ");
      if (split.tool === "StarXTerminator") {
         ss.unscreen = true;
         if (RC.sxt && RC.sxt.version) ss.version = RC.sxt.version;
         if (RC.sxt && RC.sxt.ai_model) ss.ai_model = RC.sxt.ai_model;
      }
      step("star_reduction", split.tool, "ran", ss);
      ok = true;
   } catch (e) {
      log("star reduction skipped: recombine failed (" + e + "); normal stretch instead");
      step("star_reduction", split.tool, "failed", { error: "" + e });
      if (split.tool === "StarXTerminator" && RC.sxt) { RC.sxt.status = "failed"; RC.sxt.why = "recombine failed (" + e + ")"; }
   }
   try { split.starless.forceClose(); } catch (e1) {}
   try { split.stars.forceClose(); } catch (e2) {}
   return ok;
}

// HOO, part 1 (PS-161): copy the color-calibrated LINEAR image before SCNR
// (green removal would bias OIII = mean of G and B). Returns the copy, or
// null when HOO is off, the master is mono or the copy fails.
function hooSource(view) {
   if (HOO !== "on") {
      step("hoo", "PixelMath", "skipped", { reason: "-Hoo off" });
      return null;
   }
   if (!view.image.isColor) {
      step("hoo", "PixelMath", "skipped", { reason: "mono master" });
      return null;
   }
   try {
      return dupWindow(view, "PS_hoo");
   } catch (e) {
      log("HOO skipped: copy failed (" + e + ")");
      step("hoo", "PixelMath", "failed", { error: "" + e });
      return null;
   }
}

// The HOO OIII scale (also used by the gold palette): k = median(R) over the
// median of mean(G, B), kept in 0.1..10.
function hooScale(img) {
   img.selectedChannel = 0; var mR = img.median();
   img.selectedChannel = 1; var mG = img.median();
   img.selectedChannel = 2; var mB = img.median();
   img.resetSelections();
   return Math.max(0.1, Math.min(10, mR / Math.max(1.0e-6, 0.5 * (mG + mB))));
}

// HOO, part 2: Ha = R, OIII = k x mean(G, B) with k = median(R) over the
// median of mean(G, B) (OIII lifted to the Ha background, k kept in 0.1..10),
// then the same linked stretch and framing as the natural image. Saves
// <Name>_hoo.xisf and <Name>_hoo.jpg; any failure is logged and skipped. The
// copy is always closed.
function hooFinish(win) {
   try {
      var v = win.mainView;
      var k = hooScale(v.image);
      var PM = new PixelMath;
      PM.useSingleExpression = false;
      PM.expression = "$T[0]";
      PM.expression1 = k.toFixed(5) + " * ($T[1] + $T[2]) / 2";
      PM.expression2 = k.toFixed(5) + " * ($T[1] + $T[2]) / 2";
      PM.createNewImage = false;
      PM.rescale = false;
      PM.truncate = true;
      if (!PM.executeOn(v, false)) throw new Error("PixelMath returned false");
      stretch(v, "hoo");
      frameCrop(v, EDGE_FRAC);
      trySave(win, FINAL + "/" + NAME + "_hoo.xisf", "HOO xisf (32-bit)");
      try {
         var SF = new SampleFormatConversion;
         SF.format = SampleFormatConversion.prototype.To16Bit;
         SF.executeOn(v, false);
      } catch (e1) { log("HOO 16-bit conversion skipped (" + e1 + ")"); }
      trySave(win, FINAL + "/" + NAME + "_hoo.jpg", "HOO jpg");
      log("HOO: Ha = R, OIII = " + k.toFixed(3) + " x mean(G, B)");
      step("hoo", "PixelMath", "ran", { oiii_scale: +k.toFixed(5), file: NAME + "_hoo.jpg" });
   } catch (e) {
      log("HOO skipped (" + e + ")");
      step("hoo", "PixelMath", "failed", { error: "" + e });
   }
   try { win.forceClose(); } catch (e2) {}
}

// Gold palette, part 1: the same color-calibrated LINEAR copy as HOO (before
// SCNR, which would bias OIII). Returns the copy, or null when the palette is
// off, the master is mono or the copy fails.
function paletteSource(view) {
   if (PALETTE !== "gold") {
      if (PALETTE_SKIP_WHY) log(PALETTE_SKIP_WHY);
      step("palette", "PixelMath", "skipped", { reason: PALETTE_SKIP_WHY || "-Palette off" });
      return null;
   }
   if (!view.image.isColor) {
      step("palette", "PixelMath", "skipped", { palette: PALETTE, reason: "mono master" });
      return null;
   }
   try {
      return dupWindow(view, "PS_gold");
   } catch (e) {
      log("gold palette skipped: copy failed (" + e + ")");
      step("palette", "PixelMath", "failed", { palette: PALETTE, error: "" + e });
      return null;
   }
}

// The gold-blue mapping as PixelMath expressions (R, G, B), OIII scaled by k:
//   R = Ha, G = GOLD_MIX x Ha + (1 - GOLD_MIX) x OIII, B = OIII
function goldExpressions(k) {
   var o = k.toFixed(5) + " * ($T[1] + $T[2]) / 2";
   return ["$T[0]",
           GOLD_MIX.toFixed(3) + " * $T[0] + " + (1 - GOLD_MIX).toFixed(3) + " * " + o,
           o];
}

// Gold palette, part 2: NoiseXTerminator on the copy when installed (the
// GraXpert / MLT fallbacks stay with the natural image), split the stars off
// (StarXTerminator, else StarNet2, on the reversible pre-stretch like the
// star reduction), map Ha / OIII to gold-blue on the starless (else whole)
// image, linked stretch + saturation, hue curve, screen the natural RGB stars
// back, framing crop, save <Name>_gold.{xisf,jpg}. Any failure is logged and
// skipped; the RC Astro records of the natural image are kept; every copy is
// closed.
function paletteFinish(win) {
   var st = { palette: PALETTE, ha_in_green: GOLD_MIX, file: NAME + "_gold.jpg" };
   var keepNxt = RC.nxt, keepSxt = RC.sxt;
   var sl = null, stars = null;
   try {
      var v = win.mainView;
      st.noise_reduction = "none";
      if (DENOISE > 0 && USE_RC && rcInstalled("NoiseXTerminator")) {
         try { nxtDenoise(v); st.noise_reduction = "NoiseXTerminator"; }
         catch (e0) { log("gold palette: NoiseXTerminator skipped (" + e0 + ")"); }
      }
      var sigma = v.image.MAD() * 1.4826;
      var tools = [];
      if (USE_RC && rcInstalled("StarXTerminator")) tools.push("StarXTerminator");
      if (typeof StarNet2 !== "undefined") tools.push("StarNet2");
      var mPre = mtfv(STARNET_PRE_BG, Math.max(1.0e-6, v.image.median()));
      for (var t = 0; t < tools.length && !sl; ++t) {
         try {
            sl = starlessPre(v, mPre, tools[t]);
            stars = dupWindow(v, "PS_gold_stars");
            pixelMath(stars.mainView, "max(0, $T - " + sl.mainView.id + ")");
            st.stars = tools[t];
         } catch (e1) {
            log("gold palette: " + tools[t] + " split failed (" + e1 + ")");
            if (sl) try { sl.forceClose(); } catch (e2) {}
            if (stars) try { stars.forceClose(); } catch (e3) {}
            sl = null; stars = null;
         }
      }
      if (!sl) st.stars = "in the image (no star split)";
      var tv = sl ? sl.mainView : v;
      var k = hooScale(tv.image);
      var ex = goldExpressions(k);
      var PM = new PixelMath;
      PM.useSingleExpression = false;
      PM.expression = ex[0];
      PM.expression1 = ex[1];
      PM.expression2 = ex[2];
      PM.createNewImage = false;
      PM.rescale = false;
      PM.truncate = true;
      if (!PM.executeOn(tv, false)) throw new Error("PixelMath returned false");
      st.oiii_scale = +k.toFixed(5);
      st.expressions = ex;
      var p = stretch(tv, sl ? "gold starless" : "gold");
      try {
         var CT = new CurvesTransformation;
         CT.H = GOLD_HUE;
         try { CT.Ht = CurvesTransformation.prototype.Linear; } catch (e4) {}
         if (!CT.executeOn(tv, false)) throw new Error("CurvesTransformation returned false");
         st.hue_curve = GOLD_HUE;
      } catch (e5) {
         log("gold palette: hue curve skipped (" + e5 + ")");
         st.hue_curve = "failed: " + e5;
      }
      if (sl) {
         var cs = Math.max(0, Math.min(0.5, STAR_FLOOR_SIGMA * sigma));
         applyHT(stars.mainView, cs, p.m);
         var ks = (STARS === "on" ? STAR_STRENGTH : 1).toFixed(3);
         pixelMath(tv, "~(~$T * ~(" + stars.mainView.id + " * " + ks + "))");
         v.beginProcess(UndoFlag_NoSwapFile);
         v.image.assign(tv.image);
         v.endProcess();
         st.star_strength = +ks;
         st.star_floor = +cs.toFixed(6);
      }
      frameCrop(v, EDGE_FRAC);
      trySave(win, FINAL + "/" + NAME + "_gold.xisf", "gold xisf (32-bit)");
      try {
         var SF = new SampleFormatConversion;
         SF.format = SampleFormatConversion.prototype.To16Bit;
         SF.executeOn(v, false);
      } catch (e6) { log("gold 16-bit conversion skipped (" + e6 + ")"); }
      trySave(win, FINAL + "/" + NAME + "_gold.jpg", "gold jpg");
      log("gold palette: R = Ha, G = " + GOLD_MIX.toFixed(2) + " Ha + " + (1 - GOLD_MIX).toFixed(2) +
          " OIII, B = OIII (OIII = " + k.toFixed(3) + " x mean(G, B)); stars: " + st.stars);
      step("palette", "PixelMath", "ran", st);
   } catch (e) {
      log("gold palette skipped (" + e + ")");
      st.error = "" + e;
      step("palette", "PixelMath", "failed", st);
   }
   RC.nxt = keepNxt; RC.sxt = keepSxt;
   if (sl) try { sl.forceClose(); } catch (e7) {}
   if (stars) try { stars.forceClose(); } catch (e8) {}
   try { win.forceClose(); } catch (e9) {}
}

function frameCrop(view, edgeFrac) {
   if (!FRAME) return;
   try {
      clearWcs(view);
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

// ==== PS_FINISH_LIB_END

// ==== PS_VIVID_LIB_BEGIN (deploy/blend_vivid.js reuses the vivid section from here to
// PS_VIVID_LIB_END on the stretched blend; keep it free of placeholders)
// ---------------------------------------------------------------- vivid variant
// More color for galaxies (pink HII regions, warm core, blue arms) without
// a narrowband mapping. Runs after the natural final is saved, on copies
// only: the natural output is never touched, and any failure is logged as
// SKIPPED and leaves it as it is.

// ==== PS_VIVID_MATH_BEGIN (plain math, no PixInsight objects; tests run it in node)
// Luminance-range mask: 0 on the sky background (up to BG + 0.02), full from
// BG + 0.12, back to 0 over the brightest core (L 0.70 to 0.90).
var VIVID_LO_START = 0.02, VIVID_LO_SPAN = 0.10;
var VIVID_HI_START = 0.70, VIVID_HI_SPAN = 0.20;
// Core region (warm yellows) vs outer arms (blue), by stretched luminance.
var VIVID_CORE_LO = 0.30, VIVID_CORE_HI = 0.55;
// Hue bands (HSV hue 0..1: 0 red, 1/6 yellow, 1/2 cyan, 2/3 blue, 5/6
// magenta): [center, half width]
var VIVID_PINK = [0.94, 0.12], VIVID_WARM = [0.10, 0.08], VIVID_BLUE = [0.60, 0.10];

function vividSmooth(e0, e1, x) {
   if (e1 <= e0) return x >= e1 ? 1 : 0;
   var t = (x - e0) / (e1 - e0);
   if (t <= 0) return 0;
   if (t >= 1) return 1;
   return t * t * (3 - 2 * t);
}
function vividMask(L, bg) {
   return vividSmooth(bg + VIVID_LO_START, bg + VIVID_LO_START + VIVID_LO_SPAN, L) *
          (1 - vividSmooth(VIVID_HI_START, VIVID_HI_START + VIVID_HI_SPAN, L));
}
// The saturation curve that takes an image already saturated with the
// natural S curve (midpoint natMid) to one with midpoint vivMid. Each S curve
// is modeled as the power curve through (0.5, mid), so the step between them
// is the power curve x^(ln vivMid / ln natMid). Points for CurvesTransformation.S.
function vividSatCurve(natMid, vivMid) {
   var n = Math.max(0.01, Math.min(0.99, natMid)), w = Math.max(0.01, Math.min(0.99, vivMid));
   var e = Math.log(w) / Math.log(n);
   var xs = [0.05, 0.15, 0.3, 0.5, 0.7, 0.85], pts = [[0, 0]];
   for (var i = 0; i < xs.length; ++i) pts.push([xs[i], +Math.pow(xs[i], e).toFixed(5)]);
   pts.push([1, 1]);
   return pts;
}
// HSV hue 0..1; -1 for a gray pixel.
function vividHue(r, g, b) {
   var mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
   if (d <= 1.0e-6) return -1;
   var h;
   if (mx === r) h = (g - b) / d;
   else if (mx === g) h = 2 + (b - r) / d;
   else h = 4 + (r - g) / d;
   h /= 6;
   return h < 0 ? h + 1 : h;
}
function vividBump(h, band) {
   var d = Math.abs(h - band[0]);
   if (d > 0.5) d = 1 - d;
   if (d >= band[1]) return 0;
   return 0.5 * (1 + Math.cos(Math.PI * d / band[1]));
}
// 0..1: how much of the hue lift a pixel of hue h at luminance L gets.
function vividHueWeight(h, L) {
   if (h < 0) return 0;
   var wc = vividSmooth(VIVID_CORE_LO, VIVID_CORE_HI, L);
   return Math.min(1, vividBump(h, VIVID_PINK) + wc * vividBump(h, VIVID_WARM) + (1 - wc) * vividBump(h, VIVID_BLUE));
}
// Largest chroma gain about Y that keeps channel value x inside 0..1.
function vividGainCap(x, Y) {
   var d = x - Y;
   if (d > 1.0e-9) return (1 - Y) / d;
   if (d < -1.0e-9) return Y / -d;
   return 1.0e9;
}
// c: the stretched image (3 channel arrays, changed in place), s: the same
// image after the vivid saturation curve. Per pixel: the hue lift on s
// (chroma about its luminance x (1 + boost x hue weight), capped so no channel
// clips, hue kept), then c + mask x (lifted - c). Returns the mask and the
// mean chroma before / after (every 97th pixel).
function vividBlend(c, s, N, bg, boost) {
   var m = new Float32Array(N), sumW = 0, cb = 0, ca = 0, ns = 0;
   for (var i = 0; i < N; ++i) {
      var r0 = c[0][i], g0 = c[1][i], b0 = c[2][i];
      var L = 0.2126 * r0 + 0.7152 * g0 + 0.0722 * b0;
      var w = vividMask(L, bg);
      m[i] = w;
      sumW += w;
      var smp = (i % 97 === 0);
      if (smp) { cb += Math.abs(r0 - g0) + Math.abs(b0 - g0); ++ns; }
      if (w > 0) {
         var r = s[0][i], g = s[1][i], b = s[2][i];
         var k = boost > 0 ? boost * vividHueWeight(vividHue(r, g, b), L) : 0;
         if (k > 0) {
            var Y = 0.2126 * r + 0.7152 * g + 0.0722 * b;
            var gain = Math.min(1 + k, vividGainCap(r, Y), vividGainCap(g, Y), vividGainCap(b, Y));
            r = Y + (r - Y) * gain; g = Y + (g - Y) * gain; b = Y + (b - Y) * gain;
         }
         r0 += w * (r - r0); g0 += w * (g - g0); b0 += w * (b - b0);
         c[0][i] = r0; c[1][i] = g0; c[2][i] = b0;
      }
      if (smp) ca += Math.abs(r0 - g0) + Math.abs(b0 - g0);
   }
   return { mask: m, mean_mask: N ? sumW / N : 0, chroma_before: ns ? cb / ns : 0, chroma_after: ns ? ca / ns : 0 };
}
// c + m x (e - c), per channel (the local contrast pass through the mask).
function vividMix(c, e, m, N) {
   for (var ch = 0; ch < c.length; ++ch)
      for (var i = 0; i < N; ++i) if (m[i] > 0) c[ch][i] += m[i] * (e[ch][i] - c[ch][i]);
}
// ==== PS_VIVID_MATH_END

var VIVID_SRC = null;   // { starless: window, stars: window or null }
var VIVID_ERR = "";

function vividRelease() {
   if (!VIVID_SRC) return;
   try { VIVID_SRC.starless.forceClose(); } catch (e) {}
   if (VIVID_SRC.stars) try { VIVID_SRC.stars.forceClose(); } catch (e2) {}
   VIVID_SRC = null;
}

// Copy the stretched (starless) image and, with a star split, the stretched
// star layer, before the natural image gets them back. Never throws.
function vividCapture(view, starsView) {
   if (VIVID !== "on" || VIVID_SRC) return;
   var sl = null;
   try {
      if (!view.image.isColor) return;
      sl = dupWindow(view, "PS_vivid");
      var st = starsView ? dupWindow(starsView, "PS_vivid_stars") : null;
      VIVID_SRC = { starless: sl, stars: st };
   } catch (e) {
      if (sl) try { sl.forceClose(); } catch (e2) {}
      VIVID_ERR = "copy failed (" + e + ")";
      log("vivid: " + VIVID_ERR);
   }
}

// The vivid look on the copies, saved as <Name>_vivid.{xisf,tif,jpg}; kw = the
// natural final's FITS keywords, to16 = the natural tif was 16-bit.
function vividFinish(kw, to16) {
   if (VIVID !== "on") {
      step("vivid", "CurvesTransformation", "skipped", { reason: "-Vivid off" });
      return;
   }
   var src = VIVID_SRC;
   VIVID_SRC = null;
   if (!src) {
      var why = VIVID_ERR || "mono master";
      if (VIVID_ERR) log("vivid SKIPPED (" + VIVID_ERR + "); the natural final is unaffected");
      step("vivid", "CurvesTransformation", VIVID_ERR ? "failed" : "skipped", { reason: why });
      return;
   }
   var n0 = STEPS.length, dup = null, lhe = null, win = src.starless;
   var st = { sat_mid: VIVID_SAT_MID, natural_sat_mid: SAT_MID, hue_boost: VIVID_HUE_BOOST,
              lhe: VIVID_LHE, file: NAME + "_vivid.jpg",
              mask: { background: [+(BG_TARGET + VIVID_LO_START).toFixed(4), +(BG_TARGET + VIVID_LO_START + VIVID_LO_SPAN).toFixed(4)],
                      core: [VIVID_HI_START, VIVID_HI_START + VIVID_HI_SPAN] },
              hues: { pink: VIVID_PINK, warm_core: VIVID_WARM, blue_arms: VIVID_BLUE },
              stars: src.stars ? "natural star layer screened back" : "in the image (no star split)" };
   try {
      var v = win.mainView, img = v.image;
      var W = img.width, H = img.height, N = W * H, rect = new Rect(W, H), ch;
      dup = dupWindow(v, "PS_vivid_sat");
      if (VIVID_SAT_MID !== SAT_MID) {
         var curve = vividSatCurve(SAT_MID, VIVID_SAT_MID);
         var CT = new CurvesTransformation;
         CT.S = curve;
         if (!CT.executeOn(dup.mainView, false)) throw new Error("CurvesTransformation returned false");
         st.sat_curve = curve;
      }
      var c = [], s = [];
      for (ch = 0; ch < 3; ++ch) {
         c.push(new Float32Array(N)); img.getSamples(c[ch], rect, ch);
         s.push(new Float32Array(N)); dup.mainView.image.getSamples(s[ch], rect, ch);
      }
      try { dup.forceClose(); } catch (e0) {}
      dup = null;
      var r = vividBlend(c, s, N, BG_TARGET, VIVID_HUE_BOOST);
      s = null;
      v.beginProcess(UndoFlag_NoSwapFile);
      for (ch = 0; ch < 3; ++ch) img.setSamples(c[ch], rect, ch);
      v.endProcess();
      st.mean_mask = +r.mean_mask.toFixed(4);
      st.chroma = [+r.chroma_before.toFixed(5), +r.chroma_after.toFixed(5)];
      log("vivid: saturation " + SAT_MID + " -> " + VIVID_SAT_MID + ", hue lift " + VIVID_HUE_BOOST +
          " (mean mask " + r.mean_mask.toFixed(3) + ", chroma " + r.chroma_before.toFixed(5) + " -> " +
          r.chroma_after.toFixed(5) + ")");
      if (VIVID_LHE > 0) {
         try {
            if (typeof LocalHistogramEqualization === "undefined") throw new Error("not installed");
            lhe = dupWindow(v, "PS_vivid_lhe");
            var LH = new LocalHistogramEqualization, set = {};
            trySet(LH, "radius", 96, set);
            trySet(LH, "slopeLimit", 1.5, set);
            trySet(LH, "amount", VIVID_LHE, set);
            if (!LH.executeOn(lhe.mainView, false)) throw new Error("LocalHistogramEqualization returned false");
            var lh = [];
            for (ch = 0; ch < 3; ++ch) { lh.push(new Float32Array(N)); lhe.mainView.image.getSamples(lh[ch], rect, ch); }
            vividMix(c, lh, r.mask, N);
            lh = null;
            v.beginProcess(UndoFlag_NoSwapFile);
            for (ch = 0; ch < 3; ++ch) img.setSamples(c[ch], rect, ch);
            v.endProcess();
            st.local_contrast = { tool: "LocalHistogramEqualization", settings: set };
            log("vivid: local contrast (LocalHistogramEqualization amount " + VIVID_LHE + ") through the mask");
         } catch (e1) {
            log("vivid: local contrast SKIPPED (" + e1 + ")");
            st.local_contrast = "skipped: " + e1;
         }
      } else st.local_contrast = "off";
      c = null;
      if (src.stars) {
         var k = STAR_STRENGTH.toFixed(3);
         pixelMath(v, "~(~$T * ~(" + src.stars.mainView.id + " * " + k + "))");
         st.star_strength = +k;
      }
      frameCrop(v, EDGE_FRAC);
      if (kw) try { win.keywords = kw; } catch (e2) {}
      trySave(win, FINAL + "/" + NAME + "_vivid.xisf", "vivid xisf (32-bit)");
      try {
         if (to16) {
            var SF = new SampleFormatConversion;
            SF.format = SampleFormatConversion.prototype.To16Bit;
            SF.executeOn(v, false);
         }
      } catch (e3) { log("vivid 16-bit conversion skipped (" + e3 + ")"); }
      trySave(win, FINAL + "/" + NAME + "_vivid.tif", "vivid tif");
      trySave(win, FINAL + "/" + NAME + "_vivid.jpg", "vivid jpg");
      st.sub_steps = STEPS.splice(n0, STEPS.length - n0);
      log("vivid: saved " + NAME + "_vivid.{xisf,tif,jpg} (stars: " + st.stars + ")");
      step("vivid", "CurvesTransformation", "ran", st);
   } catch (e) {
      st.sub_steps = STEPS.splice(n0, STEPS.length - n0);
      st.error = "" + e;
      log("vivid SKIPPED (" + e + "); the natural final is unaffected");
      step("vivid", "CurvesTransformation", "failed", st);
   }
   if (dup) try { dup.forceClose(); } catch (e4) {}
   if (lhe) try { lhe.forceClose(); } catch (e5) {}
   try { win.forceClose(); } catch (e6) {}
   if (src.stars) try { src.stars.forceClose(); } catch (e7) {}
}
// ==== PS_VIVID_LIB_END

function main() {
   console.show();
   log("finish: " + MASTER + "  target=" + NAME + "  RA=" + RA_DEG + " Dec=" + DEC_DEG);
   log("output: " + FINAL);
   if (!File.exists(MASTER)) throw new Error("no master at " + MASTER + " (integration not run or failed)");
   ensureDir(FINAL);
   var ws = ImageWindow.open(MASTER);
   if (!ws.length) throw new Error("could not open " + MASTER);
   var w = ws[0];
   var v = w.mainView;

   log("RC Astro (finish_use_rcastro " + RC_MODE + "): BlurXTerminator " +
       (rcInstalled("BlurXTerminator") ? "installed" : "not installed") + ", NoiseXTerminator " +
       (rcInstalled("NoiseXTerminator") ? "installed" : "not installed") + ", StarXTerminator " +
       (rcInstalled("StarXTerminator") ? "installed" : "not installed"));
   cropEdges(v, EDGE_FRAC);
   removeGradient(v);
   var solved = plateSolve(w);
   colorCalibrate(v, solved);
   var hooWin = hooSource(v);   // PS-161: before SCNR
   PALETTE_SKIP_WHY = PALETTE_WHY;
   var palWin = paletteSource(v);   // gold palette: the same linear copy point
   removeGreen(v);
   trySave(w, FINAL + "/" + NAME + "_linear.xisf", "linear (color-calibrated)");

   deconvolve(v);
   denoise(v);
   var split = starSplit(v);
   if (!split || !starRecombine(v, split)) {
      vividRelease();   // a copy from a recombine that failed afterwards
      stretch(v);
      recoverCore(v);
   }
   vividCapture(v, null);   // no star split: the vivid look starts from the stretched image
   frameCrop(v, EDGE_FRAC);
   try {
      var st = v.image, mm = [];
      for (var q = 0; q < st.numberOfChannels; ++q) { st.selectedChannel = q; mm.push(st.mean().toFixed(4)); }
      st.resetSelections();
      log("final channel means" + (mm.length === 3 ? " R/G/B" : "") + ": " + mm.join(" / "));
   } catch (e) { log("stats skipped (" + e + ")"); }
   log("steps: " + stepTag());
   labelOutput(w);
   var finalKw = null;
   try { finalKw = w.keywords; } catch (e) {}
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
   vividFinish(finalKw, to16);
   if (hooWin) hooFinish(hooWin);
   if (palWin) paletteFinish(palWin);
   log("DONE");
}

var RUN = MASTERS ? MASTERS : [{ name: NAME, path: MASTER }];
var NFAIL = 0;
for (var mi = 0; mi < RUN.length; ++mi) {
   NAME = RUN[mi].name; MASTER = RUN[mi].path; STEPS = []; RC = {}; vividRelease(); VIVID_ERR = "";
   var tf0 = Date.now();
   if (MASTERS) log("STAGE START " + NAME + " / finish");
   try { main(); writeSteps("ok"); }
   catch (e) {
      ++NFAIL;
      writeSteps("error: " + e.toString());
      log("ERROR (" + NAME + "): " + e.toString()); console.criticalln("[FINISH] FAILED: " + e.toString());
   }
   if (MASTERS) log("STAGE END   " + NAME + " / finish (" + ((Date.now() - tf0) / 60000).toFixed(2) + " min)");
}
log(NFAIL ? ("EXIT WITH " + NFAIL + " FAILED MASTER(S)") : "EXIT OK");
writeLog();
