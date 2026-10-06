// PhotonScript OSC finishing pipeline (PJSR): masterOSC.xisf -> finished image.
// Launched by run-finish-osc.ps1, which fills the __PLACEHOLDERS__ and, when
// PixInsight's ImageSolver script exists, the solver include below.
//
//   crop registration edges -> gradient removal (GradientCorrection, else ABE)
//   -> plate solve (ImageSolver, spiral search around the target)
//   -> color: SPCC against Gaia DR3/SP when that database is configured
//      (fallback: BackgroundNeutralization + ColorCalibration) -> SCNR green
//   -> deconvolution: BlurXTerminator, else GraXpert CLI, else skipped
//   -> noise reduction: NoiseXTerminator, else GraXpert CLI, else built-in
//      MultiscaleLinearTransform through a linear mask
//   -> star reduction if StarNet2 is installed: split the linear image into
//      starless + stars, stretch the starless image, core HDR blend on it,
//      screen the gently stretched stars back at -StarStrength
//      (else: linked stretch -> core HDR blend on the whole image)
//   -> gentle saturation (part of the stretch) -> [framing crop]
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
      if (g.ok) log("color: " + g.why);
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

// Deconvolution (linear): BlurXTerminator if installed, else GraXpert
// (deconv-obj, then deconv-stellar at half strength), else skipped. There is
// no built-in fallback on purpose: classic Deconvolution needs a measured PSF
// and a tuned deringing mask, and a wrong guess rings around every star.
function deconvolve(view) {
   if (DECONV !== "on") {
      log("deconvolution: off (-Deconv off)");
      step("deconvolution", "none", "skipped", { reason: "-Deconv off" });
      return;
   }
   if (USE_RC && typeof BlurXTerminator !== "undefined") {
      try {
         var BX = new BlurXTerminator;
         try { BX.correct_only = false; BX.sharpen_stars = 0.25;
               BX.sharpen_nonstellar = 0.50; BX.adjust_halos = 0.0; } catch (e1) {}
         BX.executeOn(view, false);
         log("deconvolution: BlurXTerminator (stars 0.25, nonstellar 0.50)");
         step("deconvolution", "BlurXTerminator", "ran", { stars: 0.25, nonstellar: 0.50 });
         return;
      } catch (e) { log("BlurXTerminator failed (" + e + ")"); }
   } else log(USE_RC ? "BlurXTerminator: not installed" : "RC Astro tools: disabled (-NoRC)");
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
         step("deconvolution", "GraXpert", "ran",
              { commands: done, strength: DECONV_STRENGTH, version: GRAXPERT_VERSION });
         return;
      }
      step("deconvolution", "GraXpert", "failed", { version: GRAXPERT_VERSION });
      log("deconvolution: skipped (GraXpert failed; no built-in fallback)");
      return;
   }
   log("deconvolution: skipped (no BlurXTerminator, GraXpert not found; no built-in fallback)");
   step("deconvolution", "none", "skipped", { reason: "no BlurXTerminator or GraXpert" });
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
      return;
   }
   if (USE_RC && typeof NoiseXTerminator !== "undefined") {
      try {
         var NX = new NoiseXTerminator;
         try { NX.denoise = DENOISE; NX.detail = 0.15; } catch (e1) {}
         NX.executeOn(view, false);
         log("noise reduction: NoiseXTerminator (denoise " + DENOISE + ")");
         step("noise_reduction", "NoiseXTerminator", "ran", { denoise: DENOISE, detail: 0.15 });
         return;
      } catch (e) { log("NoiseXTerminator failed (" + e + ")"); }
   } else log(USE_RC ? "NoiseXTerminator: not installed" : "RC Astro tools: disabled (-NoRC)");
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

function pixelMath(view, expr) {
   var PM = new PixelMath;
   PM.expression = expr;
   PM.useSingleExpression = true;
   PM.createNewImage = false;
   PM.rescale = false;
   PM.truncate = true;
   if (!PM.executeOn(view, false)) throw new Error("PixelMath returned false: " + expr);
}

// Star reduction, part 1 (PS-40): split the LINEAR image into starless and
// stars with StarNet2. StarNet2 is trained on stretched data, so the copy gets
// a reversible pre-stretch (midtones only, no clipping), StarNet2 runs, and
// the inverse midtones transform takes the starless image back to linear.
// stars = max(0, linear - starless). Returns null (and leaves the view alone)
// if StarNet2 is missing, disabled or fails.
function starSplit(view) {
   if (STARS !== "on") {
      log("star reduction: off (-StarReduction off)");
      step("star_reduction", "none", "skipped", { reason: "-StarReduction off" });
      return null;
   }
   if (typeof StarNet2 === "undefined") {
      log("star reduction: StarNet2 not installed; skipped");
      step("star_reduction", "StarNet2", "skipped", { reason: "not installed" });
      return null;
   }
   var sl = null, st = null;
   try {
      var mPre = mtfv(STARNET_PRE_BG, Math.max(1.0e-6, view.image.median()));
      sl = dupWindow(view, "PS_starless");
      applyHT(sl.mainView, 0, mPre);
      var SN = new StarNet2;
      try { SN.mask = false; } catch (e1) {}      // replace the target with the starless image
      try { SN.linear = false; } catch (e2) {}    // input is already pre-stretched
      if (!SN.executeOn(sl.mainView, false)) throw new Error("StarNet2 returned false");
      applyHT(sl.mainView, 0, 1 - mPre);          // inverse midtones: back to linear
      st = dupWindow(view, "PS_stars");
      pixelMath(st.mainView, "max(0, $T - " + sl.mainView.id + ")");
      log("star reduction: StarNet2 split (pre-stretch midtones " + mPre.toFixed(5) + ")");
      return { starless: sl, stars: st, preM: mPre };
   } catch (e) {
      log("star reduction skipped: StarNet2 split failed (" + e + ")");
      step("star_reduction", "StarNet2", "failed", { error: "" + e });
      if (sl) try { sl.forceClose(); } catch (e3) {}
      if (st) try { st.forceClose(); } catch (e4) {}
      return null;
   }
}

// Star reduction, part 2: stretch the starless image with the normal knobs
// (it has no stars, so its own statistics give a slightly harder stretch),
// recover the core on it, save it, stretch the stars gently (floor at
// STAR_FLOOR_SIGMA x the linear noise so StarNet2 residue does not come back
// as grain, same midtones), then screen them back at STAR_STRENGTH:
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
      var k = STAR_STRENGTH.toFixed(3);
      pixelMath(slv, "~(~$T * ~(" + stv.id + " * " + k + "))");
      view.beginProcess(UndoFlag_NoSwapFile);
      view.image.assign(slv.image);
      view.endProcess();
      log("star reduction: stars screened back at " + k + " (star floor " + cs.toFixed(6) + ")");
      step("star_reduction", "StarNet2", "ran",
           { star_strength: STAR_STRENGTH, star_floor: +cs.toFixed(6), prestretch_m: +split.preM.toFixed(6),
             starless_file: NAME + "_starless.xisf" });
      ok = true;
   } catch (e) {
      log("star reduction skipped: recombine failed (" + e + "); normal stretch instead");
      step("star_reduction", "StarNet2", "failed", { error: "" + e });
   }
   try { split.starless.forceClose(); } catch (e1) {}
   try { split.stars.forceClose(); } catch (e2) {}
   return ok;
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

   deconvolve(v);
   denoise(v);
   var split = starSplit(v);
   if (!split || !starRecombine(v, split)) {
      stretch(v);
      recoverCore(v);
   }
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
