// PhotonScript HDR combine of mono exposure-group masters (PJSR). Template:
// `photonscript integrate` replaces the CONFIG line below and writes the
// result into the run folder as hdr_run.js; it runs after the integration
// and before the finish. Do not run this template directly.
//
// PS-177 stacks each exposure length of a mono filter on its own
// (master_L = 300 s, master_L_30s = 30 s). Per filter with two or more of
// those masters (registered to the same reference by integrate_stack.js):
//   longest master = base; each shorter master in turn fills the pixels the
//   longer one saturates:
//   -> HDRComposition (PixInsight) when the process exists and METHOD is
//      auto or hdrcomposition
//   -> else (missing, failed, or a result that fails the sanity check) the
//      fallback: fit the short master to the long one by least squares over
//      pixels that are real signal and well below saturation in the longer
//      master (slope must stay within 3x of the exposure ratio, else the
//      exposure ratio itself with a median offset), then replace the pixels
//      above THRESHOLD x the long master's maximum with the scaled short
//      data through a smoothstep blend that starts BLEND_WIDTH below it
//      (mask lightly blurred with Convolution when that works), and rescale
//      the result into 0..1 once at the end
//   -> out/master/master_<F>_hdr.xisf (keywords of the longest master plus
//      PSHDR = the method)
// Records: out/master/hdr_steps.json (per filter: inputs, method, fit,
// threshold, status), out/hdr.log ending EXIT OK or ERROR, out/timing_hdr.csv.
// A filter that cannot be combined (sizes differ, a file is missing, both
// methods failed) is SKIPPED with a "SKIPPED [<F>_hdr]: <why>" line and the
// finish keeps that filter's single-exposure masters.
//
// PJSR rules (HANDBOOK sec 6): pure ASCII; no slash-star sequence inside a
// line comment; no Crop here (nothing to clear).

#include <pjsr/DataType.jsh>
#include <pjsr/UndoFlag.jsh>

//__CONFIG__

var OUT = CONFIG.out;
var LOGFILE = OUT + "/hdr.log";
var TIMINGFILE = OUT + "/timing_hdr.csv";
var STEPSFILE = OUT + "/master/hdr_steps.json";
var METHOD = CONFIG.method || "auto";             // auto | hdrcomposition | fit
var THRESHOLD = CONFIG.threshold || 0.80;         // of the longer master's maximum
var BLEND_WIDTH = CONFIG.blend_width || 0.15;     // smoothstep width below THRESHOLD (same unit)
var MASK_SIGMA = (CONFIG.mask_sigma === undefined) ? 2.0 : CONFIG.mask_sigma;   // px; 0 = no blur
var MAX_SAMPLES = 2000000;                        // fit samples (strided)

var LOGLINES = [];
var T0 = Date.now();
var RECORDS = [];
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function writeText(path, lines) {
   try {
      var f = new File; f.createForWriting(path);
      for (var i = 0; i < lines.length; ++i) f.outTextLn(lines[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + path + " (" + e + ")"); }
}
function log(s) {
   console.noteln("<b>[HDR]</b> " + s); console.flush();
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
function writeSteps(result) {
   try {
      ensureDir(OUT + "/master");
      var doc = { result: result, finished_utc: (new Date).toISOString(), method: METHOD,
                  threshold: THRESHOLD, blend_width: BLEND_WIDTH, mask_sigma: MASK_SIGMA,
                  items: RECORDS };
      var f = new File; f.createForWriting(STEPSFILE);
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}
function closeQuiet(w) { try { if (w && !w.isNull) w.forceClose(); } catch (e) {} }
function removeQuiet(path) { try { if (File.exists(path)) File.remove(path); } catch (e) {} }

function openOne(path, what) {
   if (!File.exists(path)) throw new Error(what + ": no file at " + path);
   var ws = ImageWindow.open(path);
   if (!ws.length) throw new Error(what + ": could not open " + path);
   for (var i = 1; i < ws.length; ++i) closeQuiet(ws[i]);
   return ws[0];
}
function samples(img) {
   var a = new Float32Array(img.width * img.height);
   img.getSamples(a, new Rect(img.width, img.height), 0);
   return a;
}
function windowFrom(arr, W, H, id) {
   var w = new ImageWindow(W, H, 1, 32, true, false, id);
   var v = w.mainView;
   v.beginProcess(UndoFlag_NoSwapFile);
   v.image.setSamples(arr, new Rect(W, H), 0);
   v.endProcess();
   return w;
}
function windowIds() {
   var o = {}, ws = ImageWindow.windows;
   for (var i = 0; i < ws.length; ++i) o[ws[i].mainView.id] = true;
   return o;
}
function newWindowsSince(before) {
   var out = [], ws = ImageWindow.windows;
   for (var i = 0; i < ws.length; ++i) if (!before[ws[i].mainView.id]) out.push(ws[i]);
   return out;
}
function median(a) {
   if (!a.length) return NaN;
   var b = Array.prototype.slice.call(a).sort(function (x, y) { return x - y; });
   return b[Math.floor(b.length / 2)];
}
function arrMax(a) { var m = -Infinity; for (var i = 0; i < a.length; ++i) if (a[i] > m) m = a[i]; return m; }
function smoothstep(t) { if (t <= 0) return 0; if (t >= 1) return 1; return t * t * (3 - 2 * t); }

// Copy the longest master's keywords onto the result and label it.
function labelResult(w, longWin, method) {
   try {
      var kw = [];
      try { kw = longWin.keywords; } catch (e1) {}
      kw.push(new FITSKeyword("PSHDR", "'" + method + "'", "PhotonScript HDR combine of exposure masters"));
      w.keywords = kw;
   } catch (e) { log("  keywords not copied (" + e + ")"); }
}

// ---------------------------------------------------------------- HDRComposition

function setIf(P, name, v, rec) {
   if (name in P) { P[name] = v; rec[name] = v; return true; }
   return false;
}

function viaHDRComposition(item, longWin, rec) {
   if (typeof HDRComposition !== "function")
      throw new Error("HDRComposition is not installed");
   var settings = {};
   var P = new HDRComposition;
   P.images = item.inputs.map(function (x) { return [true, x.path]; });
   setIf(P, "maskBinarizingThreshold", THRESHOLD, settings);
   setIf(P, "maskSmoothness", 7, settings);
   setIf(P, "maskGrowth", 1, settings);
   setIf(P, "autoExclusions", true, settings);
   setIf(P, "outputMasks", false, settings);
   setIf(P, "closePreviousImages", false, settings);
   setIf(P, "generate64BitResult", false, settings);
   rec.settings = settings;
   var before = windowIds();
   var ok = P.executeGlobal();
   var made = newWindowsSince(before);
   if (!ok) { for (var i = 0; i < made.length; ++i) closeQuiet(made[i]); throw new Error("HDRComposition returned false"); }
   if (!made.length) throw new Error("HDRComposition made no image");
   var w = made[made.length - 1];
   for (var j = 0; j < made.length - 1; ++j) closeQuiet(made[j]);
   var img = w.mainView.image, L = longWin.mainView.image;
   if (img.width !== L.width || img.height !== L.height) {
      closeQuiet(w); throw new Error("HDRComposition result is " + img.width + "x" + img.height +
                                     ", the masters are " + L.width + "x" + L.height);
   }
   var mx = img.maximum(), mn = img.minimum();
   if (!(mx > mn)) { closeQuiet(w); throw new Error("HDRComposition result is flat (" + mn + ".." + mx + ")"); }
   rec.result_range = [mn, mx];
   return w;
}

// ---------------------------------------------------------------- fallback

// Least squares cur = a * s + b over pixels where the longer master `prev`
// is real signal (above median + 5 MAD) and below the blend start t0.
function fitShort(cur, s, prev, t0, ratio, rec) {
   var n = prev.length, stride = Math.max(1, Math.floor(n / MAX_SAMPLES));
   var probe = [];
   for (var i = 0; i < n; i += stride * 16) probe.push(prev[i]);
   var med = median(probe);
   var dev = [];
   for (var k = 0; k < probe.length; ++k) dev.push(Math.abs(probe[k] - med));
   var mad = median(dev) * 1.4826;
   var lo = med + 5 * mad;
   function solve(clip, a0, b0, sig0) {
      var sx = 0, sy = 0, sxx = 0, sxy = 0, m = 0, xs = [], ys = [];
      for (var i = 0; i < n; i += stride) {
         var p = prev[i];
         if (!(p > lo && p < t0)) continue;
         var x = s[i], y = cur[i];
         if (clip && Math.abs(y - (a0 * x + b0)) > 3 * sig0) continue;
         sx += x; sy += y; sxx += x * x; sxy += x * y; ++m;
      }
      var den = m * sxx - sx * sx;
      if (m < 1000 || !(Math.abs(den) > 0)) return { n: m, ok: false };
      var a = (m * sxy - sx * sy) / den, b = (sy - a * sx) / m;
      var r2 = 0;
      for (var j = 0; j < n; j += stride) {
         var q = prev[j];
         if (!(q > lo && q < t0)) continue;
         var e = cur[j] - (a * s[j] + b);
         if (clip && Math.abs(cur[j] - (a0 * s[j] + b0)) > 3 * sig0) continue;
         r2 += e * e;
      }
      return { n: m, ok: true, a: a, b: b, sigma: Math.sqrt(r2 / m) };
   }
   var f = solve(false, 0, 0, 0);
   if (f.ok) {
      var f2 = solve(true, f.a, f.b, f.sigma);
      if (f2.ok) f = f2;
   }
   rec.fit = { samples: f.n, signal_floor: lo, ratio: ratio };
   if (f.ok && f.a > ratio / 3 && f.a < ratio * 3) {
      rec.fit.a = f.a; rec.fit.b = f.b; rec.fit.sigma = f.sigma; rec.fit.how = "least squares";
      return { a: f.a, b: f.b };
   }
   // exposure ratio, offset from the medians of the same pixels
   var cs = [], ss = [];
   for (var t = 0; t < n; t += stride * 4) {
      var pv = prev[t];
      if (pv > lo && pv < t0) { cs.push(cur[t]); ss.push(s[t]); }
   }
   var b2 = (cs.length ? median(cs) - ratio * median(ss) : 0);
   rec.fit.a = ratio; rec.fit.b = b2;
   rec.fit.how = "exposure ratio (" + (f.ok ? "fitted slope " + f.a.toFixed(3) + " out of range" :
                                        "too few samples: " + f.n) + ")";
   return { a: ratio, b: b2 };
}

function blurMask(mask, W, H, rec) {
   if (!(MASK_SIGMA > 0)) { rec.mask_blur = "off"; return mask; }
   var w = null;
   try {
      w = windowFrom(mask, W, H, "hdr_mask");
      var C = new Convolution;
      C.mode = Convolution.prototype.Parametric;
      C.sigma = MASK_SIGMA; C.shape = 2; C.aspectRatio = 1; C.rotationAngle = 0;
      if (!C.executeOn(w.mainView, false)) throw new Error("Convolution returned false");
      var out = samples(w.mainView.image);
      rec.mask_blur = "Convolution sigma " + MASK_SIGMA;
      return out;
   } catch (e) {
      rec.mask_blur = "skipped (" + e + ")";
      log("  mask blur skipped (" + e + ")");
      return mask;
   } finally { closeQuiet(w); }
}

function viaFit(item, wins, rec) {
   var W = wins[0].mainView.image.width, H = wins[0].mainView.image.height;
   var cur = samples(wins[0].mainView.image);       // in units of the longest master
   var prev = cur;
   var expLong = item.inputs[0].exp;
   rec.fits = [];
   for (var i = 1; i < wins.length; ++i) {
      var s = samples(wins[i].mainView.image);
      var sat = arrMax(prev);
      var t1 = THRESHOLD * sat, t0 = Math.max(0, t1 - BLEND_WIDTH * sat);
      var ratio = expLong / item.inputs[i].exp;
      var r = { input: item.inputs[i].stack, saturation: sat, t0: t0, t1: t1 };
      var ab = fitShort(cur, s, prev, t0, ratio, r);
      var mask = new Float32Array(W * H), nRep = 0;
      for (var k = 0; k < mask.length; ++k) {
         var m = smoothstep((prev[k] - t0) / (t1 - t0));
         mask[k] = m; if (m > 0) ++nRep;
      }
      mask = blurMask(mask, W, H, r);
      var next = new Float32Array(W * H);
      for (var j = 0; j < next.length; ++j) {
         var mm = mask[j];
         next[j] = (1 - mm) * cur[j] + mm * (ab.a * s[j] + ab.b);
      }
      r.replaced_px = nRep;
      r.replaced_frac = nRep / (W * H);
      log("  [" + item.filter + "] " + item.inputs[i].stack + ": a " + ab.a.toFixed(4) + ", b " + ab.b.toExponential(3) +
          " (" + r.fit.how + "), " + nRep + " px above " + t0.toExponential(3) + " blended");
      rec.fits.push(r);
      cur = next;
      prev = s;
   }
   var mx = arrMax(cur), scale = mx > 1 ? 1 / mx : 1;
   for (var q = 0; q < cur.length; ++q) { var v = cur[q] * scale; cur[q] = v < 0 ? 0 : (v > 1 ? 1 : v); }
   rec.rescale = scale;
   return windowFrom(cur, W, H, "hdr_" + item.filter);
}

// ---------------------------------------------------------------- per filter

function combine(item) {
   var S = item.name;
   var rec = { filter: item.filter, name: S, output: item.output, inputs: item.inputs,
               method: "", status: "failed", steps: [] };
   RECORDS.push(rec);
   TSTART(S, "hdr combine");
   var wins = [], result = null;
   try {
      for (var i = 0; i < item.inputs.length; ++i)
         wins.push(openOne(item.inputs[i].path, item.inputs[i].stack));
      var W = wins[0].mainView.image.width, H = wins[0].mainView.image.height;
      for (var j = 1; j < wins.length; ++j) {
         var im = wins[j].mainView.image;
         if (im.width !== W || im.height !== H || im.numberOfChannels !== 1)
            throw new Error(item.inputs[j].stack + " is " + im.width + "x" + im.height + "x" + im.numberOfChannels +
                            ", " + item.inputs[0].stack + " is " + W + "x" + H + " mono (not one registration)");
      }
      log("[" + S + "] " + item.inputs.map(function (x) { return x.stack + " (" + x.exp + " s)"; }).join(" + "));
      if (METHOD !== "fit") {
         var hrec = {};
         try {
            result = viaHDRComposition(item, wins[0], hrec);
            rec.method = "HDRComposition";
            rec.steps.push({ step: "hdr", tool: "HDRComposition", status: "ran", settings: hrec.settings || {},
                             result_range: hrec.result_range });
         } catch (e) {
            rec.steps.push({ step: "hdr", tool: "HDRComposition", status: (typeof HDRComposition === "function") ? "failed" : "skipped",
                             error: "" + e });
            log("[" + S + "] HDRComposition: " + e + (METHOD === "auto" ? "; using the fitted replacement" : ""));
            if (METHOD !== "auto") throw e;
         }
      }
      if (!result) {
         var frec = {};
         result = viaFit(item, wins, frec);
         rec.method = "fit";
         rec.steps.push({ step: "hdr", tool: "fit+replace", status: METHOD === "fit" ? "ran" : "fallback",
                          settings: { threshold: THRESHOLD, blend_width: BLEND_WIDTH, mask_sigma: MASK_SIGMA },
                          fits: frec.fits, rescale: frec.rescale });
      }
      labelResult(result, wins[0], rec.method);
      removeQuiet(item.output);
      if (!result.saveAs(item.output, false, false, false, false)) throw new Error("save returned false: " + item.output);
      rec.status = "ok";
      log("[" + S + "] saved " + item.output + " (" + rec.method + ")");
   } catch (e2) {
      rec.status = "failed";
      rec.error = ("" + e2).replace(/^Error: /, "");
      log("SKIPPED [" + S + "]: " + rec.error + " (no master_" + S + "; the finish keeps the single-exposure masters)");
   } finally {
      closeQuiet(result);
      for (var c = 0; c < wins.length; ++c) closeQuiet(wins[c]);
      TEND(S, "hdr combine");
   }
   return rec.status === "ok";
}

function main() {
   console.show();
   log("HDR combine: " + CONFIG.items.length + " filter(s), method " + METHOD + ", threshold " + THRESHOLD);
   var made = 0;
   for (var i = 0; i < CONFIG.items.length; ++i)
      if (combine(CONFIG.items[i])) ++made;
   TIMING.push("all,hdr script total," + new Date(T0).toISOString() + "," + (new Date).toISOString() + "," + ((Date.now() - T0) / 60000).toFixed(2));
   writeText(TIMINGFILE, TIMING);
   writeSteps(made === CONFIG.items.length ? "ok" : (made ? "partial" : "failed"));
   log("DONE - " + made + " of " + CONFIG.items.length + " HDR master(s)");
}

try { main(); log("EXIT OK"); }
catch (e) { writeSteps("error"); log("ERROR: " + e.toString()); console.criticalln("[HDR] FAILED: " + e.toString()); }
