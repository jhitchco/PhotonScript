// PhotonScript color combine of mono (RC16) masters (PJSR). Template:
// photonscript/integration/mono_color.py replaces the CONFIG line, the
// solver include and the finish library marker below and writes the result
// as color_run.js (into the integrate run folder, or a Finish folder for
// `photonscript finish --mono-run`). Do not run this template directly.
//
// The per-filter masters of one mono run are registered to ONE reference by
// integrate_stack.js, so they combine pixel for pixel. Per CONFIG item:
//   rgb / lrgb: R, G, B masters -> ChannelCombination (linear; else the
//     channels are copied in JS) -> the finish chain of finish_osc.js:
//     edge crop -> gradient removal -> plate solve -> color calibration
//     (SPCC against Gaia DR3/SP when solved and configured, else BN +
//     ColorCalibration) -> SCNR green -> <Name>_linear.xisf ->
//     BlurXTerminator / GraXpert deconvolution -> NoiseXTerminator / GraXpert
//     / MLT noise reduction -> star split (StarXTerminator, else StarNet2),
//     stretch + core HDR on the starless image, stars screened back (no star
//     tool: linked stretch + core HDR).
//     lrgb also finishes the L master the same way (no color steps; BXT on
//     L) and puts its stretched lightness into the stretched RGB with
//     LRGBCombination (LRGB_LIGHTNESS, LRGB_SATURATION; else a JS lightness
//     replacement) -> <Target>_LRGB, else <Target>_RGB.
//   sho (R = SII, G = Ha, B = OIII) / hoo (R = Ha, G = B = OIII): OIII and
//     SII linear-fitted to Ha (LinearFit, else a median / MAD match) ->
//     ChannelCombination -> edge crop -> gradient removal ->
//     BackgroundNeutralization (no photometric color for narrowband) ->
//     deconvolution -> noise reduction -> star split -> stretch the starless
//     image -> sho: SCNR green + a hue curve to the gold / blue look -> stars
//     screened back: the stretched RGB stars of this run when an rgb / lrgb
//     item made them, else the narrowband stars as white Ha stars (no star
//     tool: the stars stay in) -> <Target>_SHO / <Target>_HOO.
// Out (FINAL): <Name>.{xisf,tif,jpg}, <Name>_linear.xisf, <Name>_steps.json
// per item; CONFIG.steps_file for the whole run; LOGDIR/color.log ending
// EXIT OK (every item made) or EXIT WITH n FAILED ITEM(S); timing_color.csv.
// An item that cannot be made (a master missing, sizes differ) is SKIPPED
// with a "SKIPPED [<Name>]: <why>" line; the other items still run.
//
// Every optional step is guarded exactly as in finish_osc.js (a missing or
// failing process is logged and skipped, never fatal). PJSR rules (HANDBOOK
// sec 6): pure ASCII; no slash-star inside a line comment; pjsr headers
// before the solver includes; every Crop clears the astrometric solution.

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

function cfgNum(v, d) { return (v === undefined || v === null) ? d : v; }

// The globals finish_osc.js's functions read (same names, same meaning).
var STAGING    = CONFIG.staging;
var NAME       = "";
var MASTER     = "";
var FINAL      = CONFIG.final;
var LOGDIR     = CONFIG.out;
var PI_LIBRARY = CONFIG.pi_library || "";
var RA_DEG     = cfgNum(CONFIG.ra_deg, NaN);
var DEC_DEG    = cfgNum(CONFIG.dec_deg, NaN);
var FOCAL_MM   = CONFIG.focal_mm;
var PIXEL_UM   = CONFIG.pixel_um;
var GRADIENT   = CONFIG.gradient;
var RC_MODE        = CONFIG.rc_mode;
var USE_RC         = (RC_MODE !== "off");
var BXT_STARS      = CONFIG.bxt_stars;
var BXT_NONSTELLAR = CONFIG.bxt_nonstellar;
var NXT_DENOISE    = CONFIG.nxt_denoise;
var NXT_DETAIL     = 0.15;
var DENOISE         = CONFIG.denoise;
var DECONV          = CONFIG.deconv;
var DECONV_STRENGTH = CONFIG.deconv_strength;
var GRAXPERT        = CONFIG.graxpert || "";
var GRAXPERT_VERSION = CONFIG.graxpert_version || "";
var GRAXPERT_AI     = CONFIG.graxpert_ai || "";
var GRAXPERT_GPU    = CONFIG.graxpert_gpu || "";
var GRAXPERT_TIMEOUT_MIN = CONFIG.graxpert_timeout_min;
var STARS         = CONFIG.stars;
var STAR_STRENGTH = CONFIG.star_strength;
var STAR_FLOOR_SIGMA = 2.0;
var STARNET_PRE_BG   = 0.25;
var HOO = "off";                      // the OSC extras of finish_osc.js are never used here
var PALETTE  = "off";
var GOLD_MIX = 0.6;
var GOLD_HUE = [[0, 0], [1, 1]];
var COLOR       = CONFIG.color;
var SPCC_QE     = CONFIG.spcc_qe || "";
var SPCC_RED    = CONFIG.spcc_red || "";
var SPCC_GREEN  = CONFIG.spcc_green || "";
var SPCC_BLUE   = CONFIG.spcc_blue || "";
var SPCC_WHITE  = CONFIG.spcc_white || "";
var BG_TARGET    = CONFIG.bg_target;
var SHADOW_SIGMA = CONFIG.shadow_sigma;
var SCNR_AMOUNT  = CONFIG.scnr;
var SAT_MID      = CONFIG.sat_mid;
var HDR_LAYERS   = CONFIG.hdr_layers;
var HDR_LO      = 0.55;
var HDR_SPAN    = 0.35;
var FRAME = null;
var EDGE_FRAC = 0.015;
var MASTERS = null;

// Mono color settings
var LRGB_LIGHTNESS  = cfgNum(CONFIG.lrgb_lightness, 0.5);    // LRGBCombination mL (0.5 = as is)
var LRGB_SATURATION = cfgNum(CONFIG.lrgb_saturation, 0.45);  // LRGBCombination mc (below 0.5 = more)
var SHO_SCNR        = cfgNum(CONFIG.sho_scnr, 0.8);          // SCNR green on the stretched SHO
// Hue curve after the SHO SCNR (CurvesTransformation H, hue 0 = red, 1/6 =
// yellow, 1/3 = green, 1/2 = cyan, 2/3 = blue): yellow and what is left of
// green go to orange-gold, cyan to blue. Monotonic, ends fixed.
var SHO_HUE = [[0, 0], [0.08, 0.08], [0.17, 0.12], [0.33, 0.14], [0.5, 0.58], [0.67, 0.67], [1, 1]];
var STEPSFILE = CONFIG.steps_file;
var MC_TIMINGFILE = LOGDIR + "/timing_color.csv";

//__FINISH_LIB__

// ---------------------------------------------------------------- overrides
// (function declarations: these replace the finish_osc.js ones above)

function writeLog() {
   try {
      ensureDir(LOGDIR);
      var f = new File; f.createForWriting(LOGDIR + "/color.log");
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("log write failed: " + e); }
}
function log(s) {
   console.noteln("<b>[COLOR]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   writeLog();
}

// ---------------------------------------------------------------- helpers

var MC_RECORDS = [];
var MC_TIMING = ["scope,stage,start_utc,end_utc,minutes"];
var STARS_FILE = "";    // stretched RGB star layer of this run (for the narrowband items)

function mcWriteText(path, lines) {
   try {
      var f = new File; f.createForWriting(path);
      for (var i = 0; i < lines.length; ++i) f.outTextLn(lines[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + path + " (" + e + ")"); }
}
function mcClose(w) { try { if (w && !w.isNull) w.forceClose(); } catch (e) {} }
function mcWindowIds() {
   var o = {}, ws = ImageWindow.windows;
   for (var i = 0; i < ws.length; ++i) o[ws[i].mainView.id] = true;
   return o;
}
function mcNewWindowsSince(before) {
   var out = [], ws = ImageWindow.windows;
   for (var i = 0; i < ws.length; ++i) if (!before[ws[i].mainView.id]) out.push(ws[i]);
   return out;
}
function mcOpen(path, what) {
   if (!path || !File.exists(path)) throw new Error(what + ": no master at " + path);
   var ws = ImageWindow.open(path);
   if (!ws.length) throw new Error(what + ": could not open " + path);
   for (var i = 1; i < ws.length; ++i) mcClose(ws[i]);
   return ws[0];
}
function sameGeometry(a, b) {
   return a.width === b.width && a.height === b.height;
}
function writeItemSteps(item, rec) {
   try {
      ensureDir(FINAL);
      var doc = { name: item.name, kind: item.kind, inputs: item.channels, luminance: item.L || null,
                  result: rec.status, tag: stepTag(), finished_utc: (new Date).toISOString(),
                  finish: { steps: STEPS }, l_steps: rec.l_steps || null,
                  rcastro: { mode: RC_MODE, bxt: RC.bxt || null, nxt: RC.nxt || null, sxt: RC.sxt || null } };
      var f = new File; f.createForWriting(FINAL + "/" + item.name + "_steps.json");
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}
function writeSummary(result) {
   try {
      ensureDir(FINAL);
      var doc = { result: result, finished_utc: (new Date).toISOString(), target: CONFIG.target,
                  lrgb_lightness: LRGB_LIGHTNESS, lrgb_saturation: LRGB_SATURATION, sho_scnr: SHO_SCNR,
                  items: MC_RECORDS };
      var f = new File; f.createForWriting(STEPSFILE);
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("color steps json write failed: " + e); }
}

// Linear fit of one mono master to the reference (narrowband channel
// balance): LinearFit when installed, else background and noise matched in
// JS (x' = (x - med) * madRef / mad + medRef). Records rec.fit[what].
function fitTo(win, refWin, what, rec) {
   rec.fit = rec.fit || {};
   if (typeof LinearFit === "function") {
      try {
         var P = new LinearFit;
         P.referenceViewId = refWin.mainView.id;
         try { P.rejectLow = 0.0; } catch (e1) {}
         try { P.rejectHigh = 0.92; } catch (e2) {}
         if (!P.executeOn(win.mainView, false)) throw new Error("returned false");
         rec.fit[what] = "LinearFit to " + refWin.mainView.id + " (reject high 0.92)";
         log("  " + what + ": LinearFit to " + refWin.mainView.id);
         return;
      } catch (e) { log("  " + what + ": LinearFit failed (" + e + "); median / MAD match"); }
   }
   try {
      var a = win.mainView.image, r = refWin.mainView.image;
      var med = a.median(), mad = Math.max(1.0e-9, a.MAD());
      var medR = r.median(), madR = Math.max(1.0e-9, r.MAD());
      var k = madR / mad;
      pixelMath(win.mainView, "(" + "$T - " + med.toExponential(6) + ") * " + k.toFixed(6) + " + " + medR.toExponential(6));
      rec.fit[what] = "median / MAD match (scale " + k.toFixed(4) + ")";
      log("  " + what + ": median / MAD match to the reference, scale " + k.toFixed(4));
   } catch (e3) {
      rec.fit[what] = "none (" + e3 + ")";
      log("  " + what + ": channel balance skipped (" + e3 + ")");
   }
}

// Three mono windows (same registration) -> a new RGB window. ChannelCombination
// when installed, else the samples are copied in JS. The inputs stay open.
function combineChannels(wins, id, rec) {
   var W = wins[0].mainView.image.width, H = wins[0].mainView.image.height;
   for (var i = 0; i < 3; ++i) {
      var im = wins[i].mainView.image;
      if (im.numberOfChannels !== 1) throw new Error(wins[i].mainView.id + " is not a mono master");
      if (im.width !== W || im.height !== H)
         throw new Error(wins[i].mainView.id + " is " + im.width + "x" + im.height + ", " +
                         wins[0].mainView.id + " is " + W + "x" + H + " (not one registration)");
   }
   if (typeof ChannelCombination === "function") {
      try {
         var P = new ChannelCombination;
         P.colorSpace = ChannelCombination.prototype.RGB;
         P.channels = [[true, wins[0].mainView.id], [true, wins[1].mainView.id], [true, wins[2].mainView.id]];
         var before = mcWindowIds();
         if (!P.executeGlobal()) throw new Error("returned false");
         var made = mcNewWindowsSince(before);
         if (!made.length) throw new Error("made no image");
         for (var j = 0; j < made.length - 1; ++j) mcClose(made[j]);
         var w = made[made.length - 1];
         if (!w.mainView.image.isColor || !sameGeometry(w.mainView.image, wins[0].mainView.image)) {
            mcClose(w); throw new Error("result is not a " + W + "x" + H + " RGB image");
         }
         rec.combine = "ChannelCombination";
         return w;
      } catch (e) { log("  ChannelCombination failed (" + e + "); copying the channels"); }
   }
   var out = new ImageWindow(W, H, 3, 32, true, true, id);
   var v = out.mainView, rect = new Rect(W, H), buf = new Float32Array(W * H);
   v.beginProcess(UndoFlag_NoSwapFile);
   for (var c = 0; c < 3; ++c) {
      wins[c].mainView.image.getSamples(buf, rect, 0);
      v.image.setSamples(buf, rect, c);
   }
   v.endProcess();
   rec.combine = "JS channel copy";
   return out;
}

// The keywords of the first master (instrument, exposure ...) on the result.
function copyKeywords(dst, src) {
   try { dst.keywords = src.keywords; } catch (e) { log("  keywords not copied (" + e + ")"); }
}

function scnrGreen(view, amount) {
   var S = new SCNR;
   S.amount = amount;
   S.protectionMethod = SCNR.prototype.AverageNeutral;
   S.colorToRemove = SCNR.prototype.Green;
   S.preserveLightness = true;
   if (!S.executeOn(view, false)) throw new Error("SCNR returned false");
}

function neutralizeBackground(view) {
   try {
      var BN = new BackgroundNeutralization;
      if (!BN.executeOn(view, false)) throw new Error("returned false");
      log("color: narrowband, BackgroundNeutralization only (no photometric calibration)");
      step("color", "BN", "ran", { reason: "narrowband" });
   } catch (e) {
      log("BackgroundNeutralization skipped (" + e + ")");
      step("color", "BN", "failed", { error: "" + e });
   }
}

// finish_osc.js starRecombine, plus the stretched star layer saved to
// STARS_FILE when keepStars (the narrowband items of this run reuse it).
function starRecombineKeep(view, split, keepStars) {
   var ok = false;
   try {
      var slv = split.starless.mainView, stv = split.stars.mainView;
      var p = stretch(slv, "starless");
      recoverCore(slv);
      trySave(split.starless, FINAL + "/" + NAME + "_starless.xisf", "starless (stretched)");
      var sigma = view.image.MAD() * 1.4826;
      var cs = Math.max(0, Math.min(0.5, STAR_FLOOR_SIGMA * sigma));
      applyHT(stv, cs, p.m);
      if (keepStars) {
         var sf = FINAL + "/_" + NAME + "_stars_work.xisf";
         if (trySave(split.stars, sf, "stretched star layer (for the narrowband items)")) STARS_FILE = sf;
      }
      var k = STAR_STRENGTH.toFixed(3);
      pixelMath(slv, "~(~$T * ~(" + stv.id + " * " + k + "))");
      view.beginProcess(UndoFlag_NoSwapFile);
      view.image.assign(slv.image);
      view.endProcess();
      log("star reduction: stars screened back at " + k + " (star floor " + cs.toFixed(6) + ")");
      var ss = { star_strength: STAR_STRENGTH, star_floor: +cs.toFixed(6), prestretch_m: +split.preM.toFixed(6),
                 starless_file: NAME + "_starless.xisf", kept_for_narrowband: !!(keepStars && STARS_FILE) };
      if (split.errors.length) ss.fallback_from = split.errors.join("; ");
      step("star_reduction", split.tool, "ran", ss);
      ok = true;
   } catch (e) {
      log("star reduction skipped: recombine failed (" + e + "); normal stretch instead");
      step("star_reduction", split.tool, "failed", { error: "" + e });
   }
   try { split.starless.forceClose(); } catch (e1) {}
   try { split.stars.forceClose(); } catch (e2) {}
   return ok;
}

// The finish chain on a LINEAR window after the color step: deconvolution,
// noise reduction, star split + stretch (or the plain stretch).
function finishLinear(v, keepStars) {
   deconvolve(v);
   denoise(v);
   var split = starSplit(v);
   if (!split || !starRecombineKeep(v, split, keepStars)) {
      stretch(v);
      recoverCore(v);
   }
}

// LRGB, the L part: the same chain on the L master (mono: no color steps,
// BlurXTerminator on L). Returns the stretched L window (caller closes it)
// and its STEPS in rec.l_steps.
function finishLuminance(item, rec) {
   var keepSteps = STEPS, keepRc = RC, keepName = NAME;
   STEPS = []; RC = {}; NAME = item.name + "_Lum";
   var lw = null;
   try {
      lw = mcOpen(item.L, "L");
      var lv = lw.mainView;
      log("[" + item.name + "] luminance " + item.L);
      cropEdges(lv, EDGE_FRAC);
      removeGradient(lv);
      deconvolve(lv);
      denoise(lv);
      var split = starSplit(lv);
      if (!split || !starRecombine(lv, split)) stretch(lv, "L");
      rec.l_steps = { steps: STEPS, rcastro: { bxt: RC.bxt || null, nxt: RC.nxt || null, sxt: RC.sxt || null } };
      return lw;
   } catch (e) {
      mcClose(lw);
      rec.l_steps = { steps: STEPS, error: "" + e };
      throw e;
   } finally {
      STEPS = keepSteps; RC = keepRc; NAME = keepName;
   }
}

// Stretched L into the stretched RGB: LRGBCombination (lightness
// LRGB_LIGHTNESS, saturation LRGB_SATURATION), else a JS lightness
// replacement (each pixel's RGB scaled so its Rec.709 luminance equals L).
function lrgbCombine(v, lw) {
   var lv = lw.mainView;
   if (!sameGeometry(v.image, lv.image))
      throw new Error("L is " + lv.image.width + "x" + lv.image.height + ", RGB is " +
                      v.image.width + "x" + v.image.height + " (not one registration)");
   if (typeof LRGBCombination === "function") {
      try {
         var P = new LRGBCombination;
         P.channels = [[false, "", 1.0], [false, "", 1.0], [false, "", 1.0], [true, lv.id, 1.0]];
         P.mL = LRGB_LIGHTNESS;
         P.mc = LRGB_SATURATION;
         try { P.clipHighlights = true; } catch (e1) {}
         try { P.noiseReduction = false; } catch (e2) {}
         if (!P.executeOn(v, false)) throw new Error("returned false");
         log("LRGB: LRGBCombination (lightness " + LRGB_LIGHTNESS + ", saturation " + LRGB_SATURATION + ")");
         step("lrgb", "LRGBCombination", "ran", { lightness: LRGB_LIGHTNESS, saturation: LRGB_SATURATION });
         return;
      } catch (e) { log("LRGBCombination failed (" + e + "); JS lightness replacement"); }
   }
   var W = v.image.width, H = v.image.height, N = W * H, rect = new Rect(W, H);
   var c = [], ch, i, L = new Float32Array(N);
   lv.image.getSamples(L, rect, 0);
   for (ch = 0; ch < 3; ++ch) { c.push(new Float32Array(N)); v.image.getSamples(c[ch], rect, ch); }
   for (i = 0; i < N; ++i) {
      var Y = 0.2126 * c[0][i] + 0.7152 * c[1][i] + 0.0722 * c[2][i];
      var k = L[i] / Math.max(1.0e-5, Y);
      for (ch = 0; ch < 3; ++ch) { var x = c[ch][i] * k; c[ch][i] = x > 1 ? 1 : (x < 0 ? 0 : x); }
   }
   v.beginProcess(UndoFlag_NoSwapFile);
   for (ch = 0; ch < 3; ++ch) v.image.setSamples(c[ch], rect, ch);
   v.endProcess();
   log("LRGB: JS lightness replacement");
   step("lrgb", "JS lightness", "fallback", { reason: "LRGBCombination missing or failed" });
}

// Narrowband stars: the stretched RGB star layer of this run when it exists
// and matches, else the narrowband image's own stars as white Ha stars.
function narrowbandStars(item, split, p, sigma, rec) {
   if (item.stars_from === "rgb" && STARS_FILE && File.exists(STARS_FILE)) {
      var sw = null;
      try {
         sw = mcOpen(STARS_FILE, "RGB stars");
         if (!sameGeometry(sw.mainView.image, split.starless.mainView.image) || !sw.mainView.image.isColor)
            throw new Error("star layer geometry differs");
         rec.stars = "RGB stars of this run";
         return sw;
      } catch (e) { mcClose(sw); log("  RGB star layer not used (" + e + "); Ha stars"); }
   }
   var stv = split.stars.mainView;
   var cs = Math.max(0, Math.min(0.5, STAR_FLOOR_SIGMA * sigma));
   applyHT(stv, cs, p.m);
   pixelMath(stv, "$T[" + item.ha_channel + "]");
   rec.stars = "Ha stars (channel " + item.ha_channel + ")";
   return null;
}

function narrowbandLook(view, item, rec) {
   if (item.kind !== "sho") return;
   try {
      scnrGreen(view, SHO_SCNR);
      log("SHO: SCNR green " + SHO_SCNR);
      rec.scnr = SHO_SCNR;
   } catch (e) { log("SHO SCNR skipped (" + e + ")"); rec.scnr = "failed: " + e; }
   try {
      var CT = new CurvesTransformation;
      CT.H = SHO_HUE;
      try { CT.Ht = CurvesTransformation.prototype.Linear; } catch (e1) {}
      if (!CT.executeOn(view, false)) throw new Error("CurvesTransformation returned false");
      log("SHO: hue curve (green / yellow -> gold, cyan -> blue)");
      rec.hue_curve = SHO_HUE;
   } catch (e2) { log("SHO hue curve skipped (" + e2 + ")"); rec.hue_curve = "failed: " + e2; }
}

// Narrowband after the linear steps: split, stretch + look on the starless
// image, stars screened back (no split: stretch + look on the whole image).
function finishNarrowband(v, item) {
   deconvolve(v);
   denoise(v);
   var look = {};
   var sigma = v.image.MAD() * 1.4826;
   var split = starSplit(v);
   if (split) {
      var sw = null;
      try {
         var slv = split.starless.mainView;
         var p = stretch(slv, item.kind + " starless");
         narrowbandLook(slv, item, look);
         trySave(split.starless, FINAL + "/" + NAME + "_starless.xisf", "starless (stretched)");
         sw = narrowbandStars(item, split, p, sigma, look);
         var sid = sw ? sw.mainView.id : split.stars.mainView.id;
         var k = STAR_STRENGTH.toFixed(3);
         pixelMath(slv, "~(~$T * ~(" + sid + " * " + k + "))");
         v.beginProcess(UndoFlag_NoSwapFile);
         v.image.assign(slv.image);
         v.endProcess();
         log(item.kind.toUpperCase() + ": " + look.stars + " screened back at " + k);
         step("star_reduction", split.tool, "ran", { star_strength: STAR_STRENGTH, stars: look.stars });
         step("palette", "PixelMath", "ran", look);
         return;
      } catch (e) {
         log("narrowband star recombine failed (" + e + "); stretching the whole image");
         step("star_reduction", split.tool, "failed", { error: "" + e });
      } finally {
         mcClose(sw);
         try { split.starless.forceClose(); } catch (e1) {}
         try { split.stars.forceClose(); } catch (e2) {}
      }
   }
   stretch(v, item.kind);
   narrowbandLook(v, item, look);
   look.stars = "in the image (no star split)";
   step("palette", "PixelMath", "ran", look);
}

function saveOutputs(w, item) {
   var v = w.mainView;
   try {
      var st = v.image, mm = [];
      for (var q = 0; q < st.numberOfChannels; ++q) { st.selectedChannel = q; mm.push(st.mean().toFixed(4)); }
      st.resetSelections();
      log("final channel means R/G/B: " + mm.join(" / "));
   } catch (e) { log("stats skipped (" + e + ")"); }
   labelOutput(w);
   var ok = trySave(w, FINAL + "/" + item.name + ".xisf", "xisf (32-bit)");
   var to16 = false;
   try {
      var SF = new SampleFormatConversion;
      SF.format = SampleFormatConversion.prototype.To16Bit;
      SF.executeOn(v, false);
      to16 = true;
   } catch (e2) { log("16-bit conversion skipped (" + e2 + ")"); }
   ok = trySave(w, FINAL + "/" + item.name + ".tif", to16 ? "tif (16-bit)" : "tif (32-bit float)") && ok;
   ok = trySave(w, FINAL + "/" + item.name + ".jpg", "jpg") && ok;
   if (!ok) throw new Error("an output did not save");
}

// ---------------------------------------------------------------- per item

function colorItem(item) {
   NAME = item.name; MASTER = item.channels.R; STEPS = []; RC = {};
   var rec = { name: item.name, kind: item.kind, inputs: item.channels, luminance: item.L || null,
               stars_from: item.stars_from || "", status: "failed", outputs: [] };
   MC_RECORDS.push(rec);
   var t0 = new Date;
   log("STAGE START " + item.name + " / color");
   var ins = [], w = null, lw = null;
   try {
      var order = ["R", "G", "B"], opened = {};
      for (var i = 0; i < 3; ++i) {
         var p = item.channels[order[i]];
         if (!opened[p]) opened[p] = mcOpen(p, item.kind.toUpperCase() + " " + order[i]);
         ins.push(opened[p]);
      }
      log("[" + item.name + "] R " + item.channels.R + ", G " + item.channels.G + ", B " + item.channels.B +
          (item.L ? ", L " + item.L : ""));
      if (item.kind === "sho" || item.kind === "hoo") {
         var ref = ins[item.ha_channel], done = {};
         for (var c = 0; c < 3; ++c) {
            var id = ins[c].mainView.id;
            if (ins[c] === ref || done[id]) continue;
            done[id] = true;
            fitTo(ins[c], ref, order[c], rec);
         }
      }
      w = combineChannels(ins, "PS_" + item.kind, rec);
      copyKeywords(w, ins[0]);
      var seen = [];
      for (var j = 0; j < ins.length; ++j) if (seen.indexOf(ins[j]) < 0) { seen.push(ins[j]); mcClose(ins[j]); }
      ins = [];
      step("combine", rec.combine, "ran", { R: item.channels.R, G: item.channels.G, B: item.channels.B, fit: rec.fit || null });
      var v = w.mainView;
      cropEdges(v, EDGE_FRAC);
      removeGradient(v);
      if (item.kind === "rgb" || item.kind === "lrgb") {
         var solved = plateSolve(w);
         colorCalibrate(v, solved);
         removeGreen(v);
         trySave(w, FINAL + "/" + NAME + "_linear.xisf", "linear (color-calibrated)");
         finishLinear(v, !!item.keep_stars);
         if (item.kind === "lrgb") {
            lw = finishLuminance(item, rec);
            lrgbCombine(v, lw);
            mcClose(lw); lw = null;
         }
      } else {
         step("plate_solve", "ImageSolver", "skipped", { reason: "narrowband: no photometric color" });
         neutralizeBackground(v);
         trySave(w, FINAL + "/" + NAME + "_linear.xisf", "linear (narrowband combined)");
         finishNarrowband(v, item);
      }
      log("steps: " + stepTag());
      saveOutputs(w, item);
      rec.outputs = [item.name + ".xisf", item.name + ".tif", item.name + ".jpg"];
      rec.status = "ok";
      rec.tag = stepTag();
      log("[" + item.name + "] done");
   } catch (e) {
      rec.status = "failed";
      rec.error = ("" + e).replace(/^Error: /, "");
      log("SKIPPED [" + item.name + "]: " + rec.error + " (no " + item.name + ")");
   } finally {
      for (var k = 0; k < ins.length; ++k) mcClose(ins[k]);
      mcClose(lw);
      mcClose(w);
      rec.steps = STEPS;
      writeItemSteps(item, rec);
      var t1 = new Date, m = ((t1.getTime() - t0.getTime()) / 60000).toFixed(2);
      MC_TIMING.push(item.name + ",color," + t0.toISOString() + "," + t1.toISOString() + "," + m);
      mcWriteText(MC_TIMINGFILE, MC_TIMING);
      log("STAGE END   " + item.name + " / color (" + m + " min)");
   }
   return rec.status === "ok";
}

function colorMain() {
   console.show();
   ensureDir(FINAL);
   log("mono color: " + CONFIG.items.length + " item(s) for " + CONFIG.target + " -> " + FINAL);
   log("RC Astro (finish_use_rcastro " + RC_MODE + "): BlurXTerminator " +
       (rcInstalled("BlurXTerminator") ? "installed" : "not installed") + ", NoiseXTerminator " +
       (rcInstalled("NoiseXTerminator") ? "installed" : "not installed") + ", StarXTerminator " +
       (rcInstalled("StarXTerminator") ? "installed" : "not installed"));
   var made = 0;
   for (var i = 0; i < CONFIG.items.length; ++i)
      if (colorItem(CONFIG.items[i])) ++made;
   if (STARS_FILE) removeQuiet(STARS_FILE);
   writeSummary(made === CONFIG.items.length ? "ok" : (made ? "partial" : "failed"));
   log("DONE - " + made + " of " + CONFIG.items.length + " color image(s)");
   return CONFIG.items.length - made;
}

try {
   var NFAILED = colorMain();
   log(NFAILED ? ("EXIT WITH " + NFAILED + " FAILED ITEM(S)") : "EXIT OK");
} catch (e) {
   writeSummary("error");
   log("ERROR: " + e.toString()); console.criticalln("[COLOR] FAILED: " + e.toString());
}
