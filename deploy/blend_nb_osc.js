// PhotonScript two-rig NARROWBAND blend (PJSR): the RC16 Ha / OIII / SII
// masters into the Piggy-600 color image (the Heart Nebula). Template:
// `photonscript blend --mode nb` (or auto, when the RC16 run has an Ha
// master) replaces the CONFIG line, the solver include and the three library
// markers below and writes the result into a NEW blend run folder as
// blend_run.js. Do not run this template directly.
//
// Libraries (copied in at render time, never edited here):
//   finish_osc.js        PS_FINISH_LIB: deconvolution, noise reduction, star
//                        split + stretch + core HDR, the gold palette
//   finish_mono_color.js PS_MONO_LIB: colorItem, the SHO / HOO mapping
//   blend_rc16_osc.js    PS_BLEND_LIB: plate solve, Resample, StarAlignment,
//                        WCS affine fallback, footprint mask, fits, CIE Lab
//
//   open the OSC master (linear color) and the RC16 Ha (needed), OIII and
//   SII (optional; the HDR masters when integrate made them); the RC16 L
//   when the run has one (it then carries the luminance, else Ha does)
//   -> [PS-186] BlurXTerminator on the RC16 luminance source
//   -> plate solve both (ImageSolver; an existing solution is kept)
//   -> product 1, OSC scale (HaRGB + OIII):
//      Ha and OIII as one image, Resample to the OSC scale, StarAlignment
//      onto the OSC (fallback: affine warp through both solutions)
//      -> footprint mask (inset + feather, as PS-153)
//      -> inside the footprint box, per channel, least squares
//           X ~ a + b NB + c C
//         X = OSC R with NB = Ha, X = OSC G and B with NB = OIII,
//         C = min(R, G, B) of the OSC: the continuum (stars and dust are in
//         all three channels, line emission is not). Then
//           X' = X (1 - w m) + (a + b NB + c C) w m + boost m b max(0, NB - NB_bg)
//         w = ha_weight / oiii_weight, m = mask, NB_bg the 2nd percentile of
//         NB in the footprint. The model matches X in background and scale
//         (no seam), takes the line structure from the RC16 and keeps the
//         continuum (star color) from the OSC; the boost adds line on top.
//         Guards: too few samples or b <= 0 = that channel is left as it
//         was; c < 0 = refit without C.
//      -> CIE Lab: L = L (1 - k) + L*(lum fitted to CIE Y) k,
//         k = lum weight x mask
//      -> <Name>_nb_linear.xisf, <Name>_nb_mask.xisf, <Name>_nb_blend.jpg and
//         <Name>_osc_ab.jpg (the same plain linked stretch, for A/B)
//   -> natural HaRGB: the finish_osc.js chain on the enhanced linear image
//      (deconvolution, noise reduction, star split, stretch, core HDR)
//      -> <Name>_haRGB.{xisf,tif,jpg} + <Name>_haRGB_final_steps.json
//   -> [PALETTE gold] finish_osc.js paletteFinish on a copy of the enhanced
//      linear image -> <Name>_haRGB_gold.{xisf,jpg}
//   -> product 2, RC16 scale (CORE): the OSC cropped around the footprint,
//      Resampled up, StarAlignment onto the RC16 Ha (fallback: the WCS
//      affine) -> its stars (StarXTerminator / StarNet2 split, stretched)
//      -> finish_mono_color.js colorItem per CONFIG item: SHO (R SII, G Ha,
//         B OIII) and HOO (R Ha, G = B = OIII) from the RC16 masters, with
//         the OSC stars screened back -> <Name>_core_SHO / _HOO .{xisf,tif,jpg}
//   + <Name>_blend_steps.json, out/timing_pi.csv, out/blend.log ending EXIT OK
//     (every product made) or ERROR.
//
// Every optional step is guarded: a missing process or a failure is logged
// and skipped (or falls back), never fatal unless nothing could be opened.
// PJSR rules (HANDBOOK sec 6): pure ASCII; no slash-star inside a line
// comment; pjsr headers before the AdP solver includes; clear the
// astrometric solution before every Crop.

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

// The globals finish_osc.js and finish_mono_color.js functions read (same
// names, same meaning; CONFIG.finish is the finish settings of the config).
var FIN        = CONFIG.finish;
var OUT        = CONFIG.out;
var WORK       = OUT + "/work";
var STAGING    = FIN.staging;
var NAME       = CONFIG.name;
var MASTER     = CONFIG.osc.path;
var FINAL      = OUT + "/final";
var LOGDIR     = OUT;
var PI_LIBRARY = FIN.pi_library || "";
var RA_DEG     = cfgNum(CONFIG.ra_deg, NaN);
var DEC_DEG    = cfgNum(CONFIG.dec_deg, NaN);
var FOCAL_MM   = FIN.focal_mm;
var PIXEL_UM   = FIN.pixel_um;
var GRADIENT   = FIN.gradient;
var RC_MODE        = FIN.rc_mode;
var USE_RC         = (RC_MODE !== "off");
var BXT_STARS      = FIN.bxt_stars;
var BXT_NONSTELLAR = FIN.bxt_nonstellar;
var NXT_DENOISE    = FIN.nxt_denoise;
var NXT_DETAIL     = 0.15;
var DENOISE         = FIN.denoise;
var DECONV          = FIN.deconv;
var DECONV_STRENGTH = FIN.deconv_strength;
var GRAXPERT        = FIN.graxpert || "";
var GRAXPERT_VERSION = FIN.graxpert_version || "";
var GRAXPERT_AI     = FIN.graxpert_ai || "";
var GRAXPERT_GPU    = FIN.graxpert_gpu || "";
var GRAXPERT_TIMEOUT_MIN = FIN.graxpert_timeout_min;
var STARS         = FIN.stars;
var STAR_STRENGTH = FIN.star_strength;
var STAR_FLOOR_SIGMA = 2.0;
var STARNET_PRE_BG   = 0.25;
var HOO = "off";                      // the HOO of finish_osc.js is the core HOO here
var PALETTE  = CONFIG.palette;        // gold | off
var GOLD_MIX = cfgNum(FIN.gold_mix, 0.6);
// The finish_osc.js gold hue curve (tests/test_integration/test_nb_blend.py
// keeps the two equal).
var GOLD_HUE = [[0, 0], [0.04, 0.07], [0.15, 0.115], [0.5, 0.5], [0.6, 0.55], [0.7, 0.64], [1, 1]];
var COLOR       = FIN.color;
var SPCC_QE     = FIN.spcc_qe || "";
var SPCC_RED    = FIN.spcc_red || "";
var SPCC_GREEN  = FIN.spcc_green || "";
var SPCC_BLUE   = FIN.spcc_blue || "";
var SPCC_WHITE  = FIN.spcc_white || "";
var BG_TARGET    = FIN.bg_target;
var SHADOW_SIGMA = FIN.shadow_sigma;
var SCNR_AMOUNT  = FIN.scnr;
var SAT_MID      = FIN.sat_mid;
var HDR_LAYERS   = FIN.hdr_layers;
var HDR_LO      = 0.55;
var HDR_SPAN    = 0.35;
var FRAME = null;
var EDGE_FRAC = 0.015;
var MASTERS = null;
// finish_mono_color.js settings (the core SHO / HOO)
var LRGB_LIGHTNESS  = cfgNum(FIN.lrgb_lightness, 0.5);
var LRGB_SATURATION = cfgNum(FIN.lrgb_saturation, 0.45);
var SHO_SCNR        = cfgNum(FIN.sho_scnr, 0.8);
// The finish_mono_color.js SHO hue curve (kept equal by the same test).
var SHO_HUE = [[0, 0], [0.08, 0.08], [0.17, 0.12], [0.33, 0.14], [0.5, 0.58], [0.67, 0.67], [1, 1]];
var STEPSFILE = FIN.steps_file;
var MC_TIMINGFILE = OUT + "/timing_color.csv";
// blend settings (blend_rc16_osc.js functions read CONFIG and LIN)
var STAGE = "linear";
var LIN = true;
var HA_W = CONFIG.ha_weight;          // Ha model share of OSC R inside the mask (0..1)
var OIII_W = CONFIG.oiii_weight;      // OIII model share of OSC G and B (0..1)
var HA_BOOST = CONFIG.ha_boost;       // extra Ha line on top of R (0 = none)
var OIII_BOOST = CONFIG.oiii_boost;   // extra OIII line on top of G and B
var LUM_W = CONFIG.lum.weight;        // luminance source share of CIE L inside the mask
var TIMINGFILE = OUT + "/timing_pi.csv";

//__FINISH_LIB__

//__MONO_LIB__

//__BLEND_LIB__

// ---------------------------------------------------------------- overrides
// (function declarations: these replace the library ones above)

var T0 = Date.now();
function writeLog() {
   try {
      ensureDir(LOGDIR);
      var f = new File; f.createForWriting(LOGDIR + "/blend.log");
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("log write failed: " + e); }
}
function log(s) {
   console.noteln("<b>[NB BLEND]</b> " + s); console.flush();
   var mins = ((Date.now() - T0) / 60000).toFixed(1);
   LOGLINES.push((new Date).toISOString() + "  [+" + mins + "m]  " + s);
   writeLog();
}

// ---------------------------------------------------------------- records

var BLEND_STEPS = STEPS;      // the blend's own steps (the finish swaps STEPS per image)
var CHANNELS = { R: [], G: [], B: [], L: [] };   // which RC16 master went into which channel
var TIMING = ["scope,stage,start_utc,end_utc,minutes"];
var TOPEN = {};
function writeText(path, lines) {
   try {
      var f = new File; f.createForWriting(path);
      for (var i = 0; i < lines.length; ++i) f.outTextLn(lines[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + path + " (" + e + ")"); }
}
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
function writeBlendSteps(result, products) {
   try {
      ensureDir(FINAL);
      var doc = { name: CONFIG.name, mode: "nb", stage: STAGE, result: result,
                  finished_utc: (new Date).toISOString(), osc: CONFIG.osc.path, rc16: CONFIG.nb,
                  luminance: CONFIG.lum.filter, palette: PALETTE,
                  weights: { ha: HA_W, oiii: OIII_W, lum: LUM_W, ha_boost: HA_BOOST, oiii_boost: OIII_BOOST },
                  channels: CHANNELS, products: products || {}, blend: { steps: BLEND_STEPS },
                  rcastro: { rc16_bxt: RC16_BXT } };
      var f = new File; f.createForWriting(FINAL + "/" + CONFIG.name + "_blend_steps.json");
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}

// ---------------------------------------------------------------- math (pure JS)

// The pixels of a full-frame array inside box [x0, y0, x1, y1) (exclusive ends).
function subBox(a, W, box) {
   var bw = box[2] - box[0], bh = box[3] - box[1], o = new Float32Array(bw * bh);
   for (var y = 0; y < bh; ++y) {
      var s = (box[1] + y) * W + box[0];
      for (var x = 0; x < bw; ++x) o[y * bw + x] = a[s + x];
   }
   return o;
}

// The continuum estimate C = min(R, G, B) per pixel.
function minOf3(r, g, b, n) {
   var c = new Float32Array(n);
   for (var i = 0; i < n; ++i) c[i] = Math.min(r[i], Math.min(g[i], b[i]));
   return c;
}

// y = a + b x1 + c x2 (withC; else c = 0) over the pixels where use(i),
// sampled, 3 sigma clipped (4 rounds). Unsaturated samples only.
function fit2(y, x1, x2, use, n, withC) {
   var stride = Math.max(1, Math.floor(n / 400000)), idx = [];
   for (var i = 0; i < n; i += stride)
      if (use(i) && y[i] > 0 && y[i] < 0.95 && x1[i] > 0 && x1[i] < 0.95) idx.push(i);
   if (idx.length < 200) throw new Error("only " + idx.length + " overlap samples for the fit");
   var keep = idx, p = [0, 0, 0], rms = 0, k, j;
   for (var it = 0; it < 4; ++it) {
      var M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]], v = [0, 0, 0];
      for (k = 0; k < keep.length; ++k) {
         j = keep[k];
         var r = [1, x1[j], withC ? x2[j] : 0];
         for (var q = 0; q < 3; ++q) {
            for (var s = 0; s < 3; ++s) M[q][s] += r[q] * r[s];
            v[q] += r[q] * y[j];
         }
      }
      if (!withC) M[2][2] = 1;
      p = solve3(M, v);
      var ss = 0;
      for (k = 0; k < keep.length; ++k) {
         j = keep[k];
         var e = y[j] - (p[0] + p[1] * x1[j] + (withC ? p[2] * x2[j] : 0));
         ss += e * e;
      }
      rms = Math.sqrt(ss / keep.length);
      var next = [];
      for (k = 0; k < idx.length; ++k) {
         j = idx[k];
         var e2 = y[j] - (p[0] + p[1] * x1[j] + (withC ? p[2] * x2[j] : 0));
         if (Math.abs(e2) <= 3 * rms) next.push(j);
      }
      if (next.length < 200) break;
      keep = next;
   }
   return { a: p[0], b: p[1], c: withC ? p[2] : 0, rms: rms, n: keep.length };
}

// One OSC channel X (box array, changed in place) from a narrowband box
// array: the continuum-aware model and blend in the header. Returns the
// record (status ran | skipped). Never throws.
function nbChannel(X, NB, C, m, n, w, boost, what, nbName) {
   var rec = { channel: what, from: nbName, weight: w, boost: boost, status: "skipped" };
   if (!(w > 0) && !(boost > 0)) { rec.reason = "weight and boost 0"; return rec; }
   var inM = function (i) { return m[i] > 0.5; };
   var f = null;
   try {
      f = fit2(X, NB, C, inM, n, true);
      rec.continuum = "C = min(R, G, B)";
      if (!(f.c >= 0)) { f = fit2(X, NB, C, inM, n, false); rec.continuum = "none (c < 0, refit without C)"; }
   } catch (e) { rec.reason = "fit failed (" + e + ")"; return rec; }
   rec.fit = { a: f.a, b: f.b, c: f.c, rms: f.rms, n: f.n };
   if (!(f.b > 0)) { rec.reason = "no positive " + nbName + " correlation (b " + f.b + ")"; return rec; }
   var bg = quantile(NB, inM, n, 0.02);
   var dsum = 0, dn = 0;
   for (var i = 0; i < n; ++i) {
      var mi = m[i];
      if (!(mi > 0)) continue;
      var model = f.a + f.b * NB[i] + f.c * C[i];
      var k = w * mi;
      var v = X[i] * (1 - k) + model * k + boost * mi * f.b * Math.max(0, NB[i] - bg);
      v = v < 0 ? 0 : (v > 1 ? 1 : v);
      if (mi > 0.99) { dsum += Math.abs(v - X[i]); ++dn; }
      X[i] = v;
   }
   rec.status = "ran";
   rec.nb_bg = bg;
   rec.mean_abs_change = dn ? dsum / dn : null;
   return rec;
}

// ---------------------------------------------------------------- images

function boxRect(box) { return new Rect(box[0], box[1], box[2], box[3]); }
function getBox(img, c, box) {
   var a = new Float32Array((box[2] - box[0]) * (box[3] - box[1]));
   img.getSamples(a, boxRect(box), c);
   return a;
}

// Stretch parameters the way finish_osc.js stretch() computes them (linked),
// without applying them; for the plain A/B looks.
function plainParams(view) {
   var img = view.image;
   var med = img.median(), mad = img.MAD() * 1.4826;
   var c0 = Math.max(0, Math.min(1, med - SHADOW_SIGMA * mad));
   return { c0: c0, m: mtfv(BG_TARGET, Math.max(1.0e-6, med - c0)) };
}
function plainLook(view, path, label, p) {
   var w = dupWindow(view, "PS_LOOK");
   try {
      applyHT(w.mainView, p.c0, p.m);
      trySave(w, path, label);
   } catch (e) { log(label + ": skipped (" + e + ")"); }
   closeQuiet(w);
}

// The RC16 narrowband masters (Ha needed; OIII / SII optional, left out with
// a log line when missing, mono-mismatched or another size than Ha).
function nbOpen() {
   var out = { Ha: openOne(CONFIG.nb.Ha, "RC16 Ha"), OIII: null, SII: null, used: ["Ha"] };
   var g = out.Ha.mainView.image;
   if (g.isColor) throw new Error("the RC16 Ha master is not mono: " + CONFIG.nb.Ha);
   var names = ["OIII", "SII"];
   for (var i = 0; i < names.length; ++i) {
      var f = names[i];
      if (!CONFIG.nb[f]) { step("open_" + f, "none", "skipped", { reason: "no " + f + " master" }); continue; }
      try {
         var w = openOne(CONFIG.nb[f], "RC16 " + f), im = w.mainView.image;
         if (im.isColor || im.width !== g.width || im.height !== g.height) {
            log("RC16 " + f + ": " + im.width + "x" + im.height + (im.isColor ? " color" : "") +
                ", Ha is " + g.width + "x" + g.height + "; left out");
            step("open_" + f, "ImageWindow", "skipped", { reason: "size or channels differ from Ha" });
            closeQuiet(w);
            continue;
         }
         out[f] = w;
         out.used.push(f);
      } catch (e) {
         log("RC16 " + f + ": could not open (" + e + "); left out");
         step("open_" + f, "ImageWindow", "failed", { error: "" + e });
      }
   }
   return out;
}

// The registration of an RC16-grid window onto the OSC: Resample it (in
// place) to the OSC scale, StarAlignment onto the saved OSC reference, else
// the affine warp through the solutions (Ha's: every RC16 master shares its
// pixel grid). Returns { arrs (full OSC frame, 0 outside), how }.
function registerAtOsc(win, tag, osc, ha, refPath) {
   var s = CONFIG.rc16_scale / CONFIG.osc.scale;
   var oW = osc.mainView.image.width, oH = osc.mainView.image.height;
   var rW = ha.mainView.image.width, rH = ha.mainView.image.height;
   resampleBy(win.mainView, s);
   step("resample_" + tag, "Resample", "ran", { factor: +s.toFixed(5),
        size: [win.mainView.image.width, win.mainView.image.height] });
   var tgtPath = WORK + "/" + tag + "_at_osc.xisf";
   try {
      trySave(win, tgtPath, "work: " + tag + " at OSC scale");
      var rw = starAlign(refPath, tgtPath, WORK + "/reg", 0.5, 5);
      var arrs = channels(rw.mainView.image);
      closeQuiet(rw);
      step("register_" + tag, "StarAlignment", "ran", { reference: "OSC", target: tag + " resampled" });
      return { arrs: arrs, how: "StarAlignment" };
   } catch (e) {
      log("StarAlignment of " + tag + " at OSC scale failed (" + e + "); trying the astrometric solutions");
      try {
         var T = invertAffine(wcsAffine(ha, osc, 0, 0, rW - 1, rH - 1));
         var img = win.mainView.image;
         var out = warp(channels(img), img.width, img.height, T, img.width / rW, oW, oH);
         log("  WCS affine: " + T.n + " points, rms " + T.rms.toFixed(3) + " px");
         step("register_" + tag, "WCS affine (bilinear)", "fallback",
              { points: T.n, rms_px: +T.rms.toFixed(3), error_sa: "" + e });
         return { arrs: out, how: "WCS affine" };
      } catch (e2) {
         step("register_" + tag, "none", "failed", { error_sa: "" + e, error_wcs: "" + e2 });
         throw new Error("could not register " + tag + " onto the OSC (StarAlignment: " + e + "; WCS: " + e2 + ")");
      }
   }
}

// ---------------------------------------------------------------- products

// Product 1: the OSC frame with Ha in R, OIII in G and B and the luminance
// source in CIE L inside the RC16 footprint. Returns { box, enc (window,
// caller closes it), rec }.
function productNbFull(osc, nb, lum) {
   var oImg = osc.mainView.image, W = oImg.width, H = oImg.height;
   var rec = { registration: "" };
   ensureDir(WORK);
   var refPath = WORK + "/osc_ref.xisf";
   var od = dupWindow(osc.mainView, "PS_OSC_REF");
   trySave(od, refPath, "work: OSC reference"); closeQuiet(od);

   TSTART("full", "register RC16 narrowband at OSC scale");
   var stack = null, reg = null;
   try {
      if (nb.OIII) {
         var crec = {};
         stack = combineChannels([nb.Ha, nb.OIII, nb.Ha], "PS_NB_STACK", crec);
         step("nb_stack", crec.combine || "ChannelCombination", "ran", { channels: ["Ha", "OIII", "Ha"] });
      } else stack = dupWindow(nb.Ha.mainView, "PS_NB_STACK");
      reg = registerAtOsc(stack, "rc16_nb", osc, nb.Ha, refPath);
   } finally { closeQuiet(stack); }
   rec.registration = reg.how;
   var lumReg = null;
   if (lum.filter !== "Ha") {
      var ld = dupWindow(lum.win.mainView, "PS_LUM_DOWN");
      try { lumReg = registerAtOsc(ld, "rc16_" + lum.filter, osc, nb.Ha, refPath).arrs[0]; }
      catch (el) { log("RC16 " + lum.filter + " not registered (" + el + "); luminance from Ha"); }
      closeQuiet(ld);
   }
   TEND("full", "register RC16 narrowband at OSC scale");

   TSTART("full", "mask + channel fits + blend");
   var fp = footprintMask(reg.arrs[0], W, H);
   var box = fp.box, n = (box[2] - box[0]) * (box[3] - box[1]);
   var mb = subBox(fp.m, W, box);
   var Hab = subBox(reg.arrs[0], W, box);
   var Ob = (nb.OIII && reg.arrs.length > 1) ? subBox(reg.arrs[1], W, box) : null;
   var Lsb = lumReg ? subBox(lumReg, W, box) : Hab;
   reg = null; lumReg = null;
   var R = getBox(oImg, 0, box), G = getBox(oImg, 1, box), B = getBox(oImg, 2, box);
   var C = minOf3(R, G, B, n);
   var recs = [nbChannel(R, Hab, C, mb, n, HA_W, HA_BOOST, "R", "Ha")];
   if (Ob) {
      recs.push(nbChannel(G, Ob, C, mb, n, OIII_W, OIII_BOOST, "G", "OIII"));
      recs.push(nbChannel(B, Ob, C, mb, n, OIII_W, OIII_BOOST, "B", "OIII"));
   } else {
      recs.push({ channel: "G", from: "OIII", status: "skipped", reason: "no OIII master" });
      recs.push({ channel: "B", from: "OIII", status: "skipped", reason: "no OIII master" });
   }
   for (var r = 0; r < recs.length; ++r) {
      var x = recs[r];
      if (x.status === "ran") CHANNELS[x.channel].push(x.from);
      log("channel " + x.channel + " <- " + x.from + ": " + x.status +
          (x.fit ? " (a " + x.fit.a.toExponential(3) + ", b " + x.fit.b.toFixed(4) + ", c " + x.fit.c.toFixed(4) +
                   ", " + x.fit.n + " px)" : "") + (x.reason ? " [" + x.reason + "]" : ""));
      step("nb_" + x.channel, "least squares X = a + b NB + c min(R,G,B)", x.status, x);
   }
   rec.channel_fits = recs;
   var enc = dupWindow(osc.mainView, "PS_NB_ENC");
   enc.mainView.beginProcess(UndoFlag_NoSwapFile);
   enc.mainView.image.setSamples(R, boxRect(box), 0);
   enc.mainView.image.setSamples(G, boxRect(box), 1);
   enc.mainView.image.setSamples(B, boxRect(box), 2);
   enc.mainView.endProcess();
   R = G = B = C = null;

   // luminance: the RC16 source fitted to CIE Y of the enhanced image, L*
   var lrec = { from: lum.filter, weight: LUM_W, status: "skipped" };
   if (LUM_W > 0) {
      var lab = null;
      try {
         lab = labOf(enc.mainView, "ENC");
         var Lb = getBox(lab.L.mainView.image, 0, box), Yb = getBox(lab.Y.mainView.image, 0, box);
         var fit = linearFit(Lsb, Yb, function (i) { return mb[i] > 0.5; }, n);
         var dsum = 0, dn = 0;
         for (var i = 0; i < n; ++i) {
            var k = LUM_W * mb[i];
            if (!(k > 0)) continue;
            var lr = lstar(fit.a + fit.b * Lsb[i]);
            var nl = Lb[i] * (1 - k) + lr * k;
            if (mb[i] > 0.99) { dsum += Math.abs(nl - Lb[i]); ++dn; }
            Lb[i] = nl;
         }
         var lv = lab.L.mainView;
         lv.beginProcess(UndoFlag_NoSwapFile);
         lv.image.setSamples(Lb, boxRect(box), 0);
         lv.endProcess();
         var CC = new ChannelCombination;
         CC.colorSpace = ChannelCombination.prototype.CIELab;
         CC.channels = [[true, lab.L.mainView.id], [true, lab.a.mainView.id], [true, lab.b.mainView.id]];
         if (!CC.executeOn(enc.mainView, false)) throw new Error("ChannelCombination CIELab returned false");
         lrec.status = "ran";
         lrec.fit = { a: fit.a, b: fit.b, rms: fit.rms, n: fit.n };
         lrec.mean_abs_dl = dn ? +(dsum / dn).toFixed(5) : null;
         CHANNELS.L.push(lum.filter);
         log("luminance <- " + lum.filter + " at " + LUM_W + ": fit " + fit.a.toExponential(3) + " + " +
             fit.b.toFixed(4) + " * " + lum.filter);
      } catch (e) {
         lrec.status = "failed"; lrec.error = "" + e;
         log("luminance blend skipped (" + e + "); the channel blend stands");
      }
      closeLab(lab);
   } else lrec.reason = "lum weight 0";
   step("nb_luminance", "ChannelExtraction + ChannelCombination (CIE Lab)", lrec.status, lrec);
   rec.luminance = lrec;
   rec.channels = CHANNELS;
   TEND("full", "mask + channel fits + blend");

   TSTART("full", "save");
   var mw = windowFrom([fp.m], W, H, "PS_MASK");
   trySave(mw, FINAL + "/" + CONFIG.name + "_nb_mask.xisf", "mask"); closeQuiet(mw);
   fp = null;
   trySave(enc, FINAL + "/" + CONFIG.name + "_nb_linear.xisf", "narrowband blend (linear)");
   var p = plainParams(osc.mainView);      // the OSC's stretch for both: a fair A/B
   plainLook(osc.mainView, FINAL + "/" + CONFIG.name + "_osc_ab.jpg", "OSC alone (plain stretch)", p);
   plainLook(enc.mainView, FINAL + "/" + CONFIG.name + "_nb_blend.jpg", "narrowband blend (plain stretch)", p);
   TEND("full", "save");
   rec.ok = true;
   return { box: box, enc: enc, rec: rec };
}

// The natural HaRGB and the gold palette: the finish_osc.js chain on the
// enhanced linear image (it is already gradient-removed and color
// calibrated: no crop, gradient or color steps). Closes encWin.
function finishNatural(encWin) {
   var keepSteps = STEPS, keepRc = RC;
   STEPS = []; RC = {};
   NAME = CONFIG.name + "_haRGB";
   MASTER = FINAL + "/" + CONFIG.name + "_nb_linear.xisf";
   var res = { name: NAME, ok: false, palette: PALETTE };
   var palWin = null;
   TSTART("natural", "finish chain");
   try {
      var v = encWin.mainView;
      palWin = paletteSource(v);
      deconvolve(v);
      denoise(v);
      var split = starSplit(v);
      if (!split || !starRecombine(v, split)) {
         stretch(v);
         recoverCore(v);
      }
      log("steps: " + stepTag());
      labelOutput(encWin);
      var ok = trySave(encWin, FINAL + "/" + NAME + ".xisf", "HaRGB xisf (32-bit)");
      try {
         var SF = new SampleFormatConversion;
         SF.format = SampleFormatConversion.prototype.To16Bit;
         SF.executeOn(v, false);
      } catch (e1) { log("16-bit conversion skipped (" + e1 + ")"); }
      ok = trySave(encWin, FINAL + "/" + NAME + ".tif", "HaRGB tif") && ok;
      ok = trySave(encWin, FINAL + "/" + NAME + ".jpg", "HaRGB jpg") && ok;
      res.ok = ok;
      if (!ok) res.error = "an output did not save";
   } catch (e) {
      res.error = "" + e;
      log("natural HaRGB failed (" + e + ")");
   }
   closeQuiet(encWin);
   TEND("natural", "finish chain");
   if (palWin) {
      TSTART("natural", "gold palette");
      paletteFinish(palWin);
      res.gold = File.exists(FINAL + "/" + NAME + "_gold.jpg");
      TEND("natural", "gold palette");
   }
   res.tag = stepTag();
   writeSteps(res.ok ? "ok" : "error: " + (res.error || "failed"));
   STEPS = keepSteps; RC = keepRc;
   NAME = CONFIG.name; MASTER = CONFIG.osc.path;
   return res;
}

// Product 2 part 1: the OSC on the RC16 Ha grid (crop around the footprint,
// Resample up, StarAlignment onto the Ha master; fallback: the WCS affine),
// edge-cropped like colorItem crops the narrowband combine, split into
// starless + stars, the stars stretched with the starless midtones and saved
// as STARS_FILE for colorItem (stars_from "rgb"). Returns the record.
function coreStars(osc, ha, box) {
   var rImg = ha.mainView.image, rW = rImg.width, rH = rImg.height;
   var up = CONFIG.osc.scale / CONFIG.rc16_scale;
   var col = null, how = "", rec = { stars: "none" };
   TSTART("core", "register OSC at RC16 scale");
   if (box) {
      var oW = osc.mainView.image.width, oH = osc.mainView.image.height;
      var mx = Math.round((box[2] - box[0]) * CONFIG.core_margin_frac), my = Math.round((box[3] - box[1]) * CONFIG.core_margin_frac);
      var x0 = Math.max(0, box[0] - mx), y0 = Math.max(0, box[1] - my);
      var x1 = Math.min(oW, box[2] + mx), y1 = Math.min(oH, box[3] + my);
      var cw = dupWindow(osc.mainView, "PS_OSC_CORE");
      try {
         cropTo(cw.mainView, x0, y0, x1, y1);
         resampleBy(cw.mainView, up);
         step("crop_upsample_osc", "Crop + Resample", "ran", { box: [x0, y0, x1, y1], factor: +up.toFixed(4) });
         ensureDir(WORK);
         var tp = WORK + "/osc_core_up.xisf";
         trySave(cw, tp, "work: OSC core upsampled");
         var rw = starAlign(CONFIG.nb.Ha, tp, WORK + "/reg_core", 0.5, 6);
         col = channels(rw.mainView.image);
         closeQuiet(rw);
         how = "StarAlignment";
         step("register_core", "StarAlignment", "ran", { reference: "RC16 Ha", target: "OSC crop upsampled" });
      } catch (e) {
         log("core StarAlignment failed (" + e + "); trying the astrometric solutions");
         step("register_core", "StarAlignment", "failed", { error: "" + e });
      }
      closeQuiet(cw);
   }
   if (!col) {
      try {
         var T = wcsAffine(ha, osc, 0, 0, rW - 1, rH - 1);
         col = warp(channels(osc.mainView.image), osc.mainView.image.width, osc.mainView.image.height, T, 1, rW, rH);
         how = "WCS affine";
         log("  core WCS affine: " + T.n + " points, rms " + T.rms.toFixed(3) + " OSC px");
         step("register_core", "WCS affine (bilinear)", "fallback", { points: T.n, rms_px: +T.rms.toFixed(3) });
      } catch (e2) {
         step("register_core", "none", "failed", { error_wcs: "" + e2 });
         TEND("core", "register OSC at RC16 scale");
         rec.registration = "failed";
         rec.error = "could not put the OSC on the RC16 grid (" + e2 + ")";
         log("core: " + rec.error + "; the SHO / HOO keep their own stars");
         return rec;
      }
   }
   rec.registration = how;
   TEND("core", "register OSC at RC16 scale");

   TSTART("core", "OSC star layer");
   var cwin = windowFrom(col, rW, rH, "PS_CORE_OSC");
   col = null;
   try {
      var v = cwin.mainView;
      cropEdges(v, EDGE_FRAC);
      var sigma = v.image.MAD() * 1.4826;
      var split = starSplit(v);
      if (split) {
         try {
            var p = stretch(split.starless.mainView, "core OSC starless");
            var cs = Math.max(0, Math.min(0.5, STAR_FLOOR_SIGMA * sigma));
            applyHT(split.stars.mainView, cs, p.m);
            var sf = FINAL + "/_" + CONFIG.name + "_core_stars_work.xisf";
            if (trySave(split.stars, sf, "OSC star layer at RC16 scale (for SHO / HOO)")) STARS_FILE = sf;
            rec.stars = STARS_FILE ? "OSC (" + split.tool + ")" : "none (save failed)";
            rec.star_floor = +cs.toFixed(6);
         } finally {
            closeQuiet(split.starless);
            closeQuiet(split.stars);
         }
      } else rec.stars = "none (no star tool)";
   } catch (e3) {
      rec.stars = "none (" + e3 + ")";
      log("core OSC star layer skipped (" + e3 + ")");
   }
   closeQuiet(cwin);
   step("core_stars", "star split of the OSC core", STARS_FILE ? "ran" : "skipped", rec);
   TEND("core", "OSC star layer");
   return rec;
}

// Product 2 part 2: colorItem (finish_mono_color.js) per SHO / HOO item from
// the RC16 masters on disk, with the OSC stars (STARS_FILE) when made.
function coreItems() {
   var made = [];
   for (var i = 0; i < CONFIG.items.length; ++i) {
      var it = CONFIG.items[i];
      try { if (colorItem(it)) made.push(it.name); }
      catch (e) { log("core " + it.name + " failed (" + e + ")"); }
   }
   writeSummary(made.length === CONFIG.items.length ? "ok" : (made.length ? "partial" : "failed"));
   STEPS = BLEND_STEPS; RC = {};
   NAME = CONFIG.name; MASTER = CONFIG.osc.path;
   var recs = [];
   for (var j = 0; j < MC_RECORDS.length; ++j)
      recs.push({ name: MC_RECORDS[j].name, kind: MC_RECORDS[j].kind, status: MC_RECORDS[j].status,
                  stars: MC_RECORDS[j].stars || null, error: MC_RECORDS[j].error || null });
   step("core_color", "colorItem (finish_mono_color.js)", made.length ? "ran" : "failed",
        { made: made, items: recs });
   return { made: made, items: recs };
}

// ---------------------------------------------------------------- main

function main() {
   console.show();
   log("narrowband blend: " + CONFIG.name + "  ha " + HA_W + " (+" + HA_BOOST + ")  oiii " + OIII_W +
       " (+" + OIII_BOOST + ")  luminance " + CONFIG.lum.filter + " " + LUM_W + "  palette " + PALETTE);
   log("OSC:  " + CONFIG.osc.path);
   for (var f in CONFIG.nb) if (CONFIG.nb[f]) log("RC16 " + f + ": " + CONFIG.nb[f]);
   if (CONFIG.lum.filter !== "Ha") log("RC16 " + CONFIG.lum.filter + ": " + CONFIG.lum.path);
   ensureDir(FINAL); ensureDir(WORK);
   var products = {};

   TSTART("inputs", "open + plate solve");
   var osc = openOne(CONFIG.osc.path, "OSC");
   if (!osc.mainView.image.isColor) throw new Error("the OSC master is not a color image: " + CONFIG.osc.path);
   var nb = nbOpen();
   var lum = { filter: "Ha", win: nb.Ha };
   if (CONFIG.lum.filter !== "Ha") {
      try {
         var lw = openOne(CONFIG.lum.path, "RC16 " + CONFIG.lum.filter), li = lw.mainView.image;
         if (li.isColor || li.width !== nb.Ha.mainView.image.width || li.height !== nb.Ha.mainView.image.height) {
            closeQuiet(lw);
            throw new Error("size or channels differ from Ha");
         }
         lum = { filter: CONFIG.lum.filter, win: lw };
      } catch (el) {
         log("RC16 " + CONFIG.lum.filter + " not used for the luminance (" + el + "); Ha instead");
         step("luminance_source", "Ha", "fallback", { wanted: CONFIG.lum.filter, error: "" + el });
      }
   }
   step("rc16_inputs", "masters", "ran", { narrowband: nb.used, luminance: lum.filter });
   try { rc16Bxt({ win: lum.win, used: [lum.filter] }); }
   catch (eb) { log("RC16 BlurXTerminator skipped (" + eb + ")"); step("rc16_bxt", "BlurXTerminator", "failed", { error: "" + eb }); }
   solveSeeds(osc, "osc", CONFIG.osc);
   solveSeeds(nb.Ha, "rc16", { scale: CONFIG.rc16_scale, pixel_um: CONFIG.rc16_pixel_um,
                               step_deg: CONFIG.rc16_step_deg, rings: 2 });
   TEND("inputs", "open + plate solve");

   var full = null;
   try {
      full = productNbFull(osc, nb, lum);
      products.full = full.rec;
   } catch (e) {
      products.full = { ok: false, error: "" + e };
      log("ERROR (full field): " + e);
   }
   if (full) {
      try { products.natural = finishNatural(full.enc); }
      catch (en) { products.natural = { ok: false, error: "" + en }; log("ERROR (HaRGB): " + en); }
      STEPS = BLEND_STEPS; RC = {}; NAME = CONFIG.name;
   } else products.natural = { ok: false, skipped: true, error: "no enhanced image (full field failed)" };

   var wantCore = CONFIG.core && CONFIG.items.length > 0;
   if (wantCore) {
      try { products.core = coreStars(osc, nb.Ha, full ? full.box : null); }
      catch (ec) { products.core = { stars: "none", error: "" + ec }; log("core star layer failed (" + ec + ")"); }
   } else {
      step("core", "none", "skipped", { reason: CONFIG.core ? "no SHO / HOO item (needs OIII)" : "core off" });
      products.core = { ok: null, skipped: true };
   }
   if (lum.win !== nb.Ha) closeQuiet(lum.win);
   closeQuiet(nb.Ha); closeQuiet(nb.OIII); closeQuiet(nb.SII); closeQuiet(osc);
   if (wantCore) {
      TSTART("core", "SHO / HOO");
      var ci = coreItems();
      products.core.made = ci.made;
      products.core.items = ci.items;
      products.core.ok = ci.made.length === CONFIG.items.length;
      TEND("core", "SHO / HOO");
   }
   if (STARS_FILE) removeQuiet(STARS_FILE);
   if (!CONFIG.keep_work) {
      var junk = [WORK + "/osc_ref.xisf", WORK + "/rc16_nb_at_osc.xisf", WORK + "/reg/rc16_nb_at_osc_r.xisf",
                  WORK + "/rc16_L_at_osc.xisf", WORK + "/reg/rc16_L_at_osc_r.xisf",
                  WORK + "/osc_core_up.xisf", WORK + "/reg_core/osc_core_up_r.xisf"];
      for (var j = 0; j < junk.length; ++j) removeQuiet(junk[j]);
   }
   return products;
}

var PRODUCTS = {};
try {
   PRODUCTS = main();
   var bad = (PRODUCTS.full && PRODUCTS.full.ok === false) || (PRODUCTS.natural && PRODUCTS.natural.ok === false) ||
             (PRODUCTS.core && PRODUCTS.core.ok === false);
   STEPS = BLEND_STEPS;
   writeBlendSteps(bad ? "partial" : "ok", PRODUCTS);
   log(bad ? "ERROR: a product failed (see above)" : "EXIT OK");
} catch (e) {
   STEPS = BLEND_STEPS;
   writeBlendSteps("error: " + e.toString(), PRODUCTS);
   log("ERROR: " + e.toString());
   console.criticalln("[NB BLEND] FAILED: " + e.toString());
}
writeLog();
