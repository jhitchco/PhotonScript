// PhotonScript OSC core HDR combine (PJSR), 2026-10-14. Template:
// `photonscript integrate` replaces the CONFIG line below and writes the
// result into the run folder as osc_hdr_run.js; it runs after the
// integration, only when the core is clipped in the full OSC master
// (photonscript/integration/osc_hdr.py, core_master's plateau check). Do not
// run this template directly.
//
// Piggy-600 M31: 122 of 162 300 s subs burned the nucleus and the OSC stack
// holds every length in one master, so the core stays flat-topped there.
// integrate_stack.js also integrated the short subs (<= 180 s, same
// registration) into master_OSC_short. Here, inside a disk around the
// plateau only (CONFIG.item.core: x, y, region_px, feather_px):
//   per channel, fit short to full by least squares over pixels that are
//   real signal (above the box's 25th percentile) and below the blend start
//   (slope must stay within 3x of the exposure ratio, else the ratio itself
//   with a median offset), then replace through a smoothstep from
//   (THRESHOLD - BLEND_WIDTH) to THRESHOLD of the brightest channel's core
//   maximum, times the disk's feathered edge; one mask for all channels,
//   so the nucleus keeps its color
//   -> the whole image is rescaled by one factor when the new core goes
//   above 1 -> out/master/master_OSC_hdr.xisf (keywords of the full master
//   plus PSHDR = 'osc-core')
// Records: out/master/osc_hdr/osc_hdr_steps.json, out/osc_hdr.log ending
// EXIT OK, out/timing_osc_hdr.csv. Anything that fails is SKIPPED with a
// "SKIPPED [OSC_hdr]: <why>" line and no output file: the finish keeps
// master_OSC as before.
//
// PJSR rules (HANDBOOK sec 6): pure ASCII; no slash-star sequence inside a
// line comment; no Crop here (nothing to clear); every optional step guarded.

#include <pjsr/DataType.jsh>
#include <pjsr/UndoFlag.jsh>

//__CONFIG__

var OUT = CONFIG.out;
var LOGFILE = OUT + "/osc_hdr.log";
var TIMINGFILE = OUT + "/timing_osc_hdr.csv";
var STEPSFILE = CONFIG.steps;
var THRESHOLD = CONFIG.threshold || 0.80;       // of each channel's core maximum
var BLEND_WIDTH = CONFIG.blend_width || 0.15;   // smoothstep width below THRESHOLD
var CHANNELS = ["R", "G", "B"];

var LOGLINES = [];
var T0 = Date.now();
function writeText(path, lines) {
   try {
      var f = new File; f.createForWriting(path);
      for (var i = 0; i < lines.length; ++i) f.outTextLn(lines[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + path + " (" + e + ")"); }
}
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function log(s) {
   console.noteln("<b>[OSCHDR]</b> " + s); console.flush();
   var mins = ((Date.now() - T0) / 60000).toFixed(1);
   LOGLINES.push((new Date).toISOString() + "  [+" + mins + "m]  " + s);
   ensureDir(OUT);
   writeText(LOGFILE, LOGLINES);
}
var TIMING = ["scope,stage,start_utc,end_utc,minutes"];
function timing(stage, t0) {
   var t1 = new Date;
   TIMING.push("OSC_hdr," + stage + "," + t0.toISOString() + "," + t1.toISOString() + "," +
               ((t1.getTime() - t0.getTime()) / 60000).toFixed(2));
   writeText(TIMINGFILE, TIMING);
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

// PS_OSC_HDR_MATH begin (pure JS: tests run these in node)
function smoothstep(t) { if (t <= 0) return 0; if (t >= 1) return 1; return t * t * (3 - 2 * t); }
function arrMax(a) { var m = -Infinity; for (var i = 0; i < a.length; ++i) if (a[i] > m) m = a[i]; return m; }
function quantileOf(a, q) {
   var stride = Math.max(1, Math.floor(a.length / 200000)), b = [];
   for (var i = 0; i < a.length; i += stride) if (isFinite(a[i])) b.push(a[i]);
   if (!b.length) return NaN;
   b.sort(function (x, y) { return x - y; });
   return b[Math.min(b.length - 1, Math.max(0, Math.floor(q * (b.length - 1))))];
}
// 1 inside R - F, 0 outside R, a smoothstep across the feather
function regionWeight(d, R, F) {
   if (d >= R) return 0;
   if (F <= 0 || d <= R - F) return 1;
   return smoothstep((R - d) / F);
}
// U: the box (bw x bh, top-left x0, y0 in the image) of max over channels
// of value / that channel's core maximum. Returns the replacement weight
// per box pixel and how many are above zero.
function maskFor(U, bw, bh, x0, y0, cx, cy, R, F, t0, t1) {
   var mask = new Float32Array(bw * bh), n = 0;
   for (var yy = 0; yy < bh; ++yy)
      for (var xx = 0; xx < bw; ++xx) {
         var k = yy * bw + xx, dx = x0 + xx - cx, dy = y0 + yy - cy;
         var w = regionWeight(Math.sqrt(dx * dx + dy * dy), R, F);
         if (w <= 0) continue;
         var m = w * smoothstep((U[k] - t0) / (t1 - t0));
         mask[k] = m; if (m > 0) ++n;
      }
   return { mask: mask, n: n };
}
// Least squares L = a S + b over the box pixels the mask leaves alone that
// are real signal (L above the box's 25th percentile) and below the blend
// start (t0frac x sat). A 3 sigma clipped refit; the slope must stay within
// 3x of the exposure ratio, else the ratio itself with a median offset.
function fitChannel(L, S, mask, sat, t0frac, ratio) {
   var lo = quantileOf(L, 0.25), hi = t0frac * sat;
   function use(i) { return mask[i] === 0 && L[i] > lo && L[i] < hi && isFinite(S[i]) && isFinite(L[i]); }
   function solve(clip, a0, b0, s0) {
      var sx = 0, sy = 0, sxx = 0, sxy = 0, m = 0;
      for (var i = 0; i < L.length; ++i) {
         if (!use(i)) continue;
         if (clip && Math.abs(L[i] - (a0 * S[i] + b0)) > 3 * s0) continue;
         sx += S[i]; sy += L[i]; sxx += S[i] * S[i]; sxy += S[i] * L[i]; ++m;
      }
      var den = m * sxx - sx * sx;
      if (m < 50 || !(Math.abs(den) > 0)) return { n: m, ok: false };
      var a = (m * sxy - sx * sy) / den, b = (sy - a * sx) / m, r2 = 0;
      for (var j = 0; j < L.length; ++j) {
         if (!use(j)) continue;
         if (clip && Math.abs(L[j] - (a0 * S[j] + b0)) > 3 * s0) continue;
         var e = L[j] - (a * S[j] + b); r2 += e * e;
      }
      return { n: m, ok: true, a: a, b: b, sigma: Math.sqrt(r2 / m) };
   }
   var f = solve(false, 0, 0, 0);
   if (f.ok && f.sigma > 0) { var f2 = solve(true, f.a, f.b, f.sigma); if (f2.ok) f = f2; }
   if (f.ok && f.a > ratio / 3 && f.a < ratio * 3)
      return { a: f.a, b: f.b, n: f.n, sigma: f.sigma, lo: lo, how: "least squares" };
   var cs = [], ss = [];
   for (var k = 0; k < L.length; ++k) if (use(k)) { cs.push(L[k]); ss.push(S[k]); }
   cs.sort(function (x, y) { return x - y; }); ss.sort(function (x, y) { return x - y; });
   var b2 = cs.length ? cs[Math.floor(cs.length / 2)] - ratio * ss[Math.floor(ss.length / 2)] : 0;
   return { a: ratio, b: b2, n: f.n, lo: lo,
            how: "exposure ratio (" + (f.ok ? "fitted slope " + f.a.toFixed(3) + " out of range" :
                                        "too few samples: " + f.n) + ")" };
}
function blendChannel(L, S, mask, a, b) {
   var out = new Float32Array(L.length);
   for (var i = 0; i < L.length; ++i) {
      var m = mask[i];
      out[i] = m > 0 ? (1 - m) * L[i] + m * (a * S[i] + b) : L[i];
   }
   return out;
}
// PS_OSC_HDR_MATH end

function boxSamples(img, rect, c, n) {
   var a = new Float32Array(n);
   img.getSamples(a, rect, c);
   return a;
}

function combine(item) {
   var rec = { name: item.name, output: item.output, inputs: item.inputs, core: item.core,
               ratio: item.ratio, threshold: THRESHOLD, blend_width: BLEND_WIDTH,
               status: "failed", fits: [] };
   var t0 = new Date, lw = null, sw = null, rw = null;
   try {
      lw = openOne(item.inputs[0].path, item.inputs[0].stack);
      sw = openOne(item.inputs[1].path, item.inputs[1].stack);
      var LI = lw.mainView.image, SI = sw.mainView.image;
      var W = LI.width, H = LI.height, nc = LI.numberOfChannels;
      if (SI.width !== W || SI.height !== H || SI.numberOfChannels !== nc)
         throw new Error(item.inputs[1].stack + " is " + SI.width + "x" + SI.height + "x" + SI.numberOfChannels +
                         ", " + item.inputs[0].stack + " is " + W + "x" + H + "x" + nc + " (not one registration)");
      var cx = item.core.x, cy = item.core.y, R = item.core.region_px, F = item.core.feather_px;
      var x0 = Math.max(0, Math.floor(cx - R - 1)), x1 = Math.min(W, Math.ceil(cx + R + 2));
      var y0 = Math.max(0, Math.floor(cy - R - 1)), y1 = Math.min(H, Math.ceil(cy + R + 2));
      if (x1 - x0 < 8 || y1 - y0 < 8) throw new Error("core disk is off the frame (" + cx + ", " + cy + ")");
      var bw = x1 - x0, bh = y1 - y0, n = bw * bh, rect = new Rect(x0, y0, x1, y1);
      rec.box = [x0, y0, x1, y1];
      log("[" + item.name + "] " + item.inputs[0].stack + " + " + item.inputs[1].stack + ", core (" +
          cx.toFixed(1) + ", " + cy.toFixed(1) + "), disk " + R + " px, feather " + F + " px, ratio " + item.ratio);
      var L = [], S = [], sat = [];
      for (var c = 0; c < nc; ++c) {
         L.push(boxSamples(LI, rect, c, n));
         S.push(boxSamples(SI, rect, c, n));
         sat.push(arrMax(L[c]));
         if (!(sat[c] > 0)) throw new Error("channel " + c + " of the full master is empty in the core box");
      }
      rec.core_max = sat;
      var U = new Float32Array(n);
      for (var k = 0; k < n; ++k) {
         var u = 0;
         for (var c2 = 0; c2 < nc; ++c2) { var v = L[c2][k] / sat[c2]; if (v > u) u = v; }
         U[k] = u;
      }
      var tA = Math.max(0, THRESHOLD - BLEND_WIDTH);
      var M = maskFor(U, bw, bh, x0, y0, cx, cy, R, F, tA, THRESHOLD);
      rec.replaced_px = M.n;
      if (!M.n) throw new Error("nothing above " + tA.toFixed(2) + " of the core maximum inside the disk");
      var O = [], top = 0;
      for (var c3 = 0; c3 < nc; ++c3) {
         var f = fitChannel(L[c3], S[c3], M.mask, sat[c3], tA, item.ratio);
         f.channel = nc === 3 ? CHANNELS[c3] : String(c3);
         rec.fits.push(f);
         log("  [" + f.channel + "] a " + f.a.toFixed(4) + ", b " + f.b.toExponential(3) + " (" + f.how + ", " + f.n + " samples)");
         var o = blendChannel(L[c3], S[c3], M.mask, f.a, f.b);
         for (var q = 0; q < o.length; ++q) if (!isFinite(o[q])) throw new Error("non-finite result in channel " + f.channel);
         O.push(o);
         top = Math.max(top, arrMax(o));
      }
      var full = 0;
      try { full = LI.maximum(); } catch (e0) { full = 1; }
      var gm = Math.max(top, full), scale = gm > 1 ? 1 / gm : 1;
      rec.rescale = scale;
      rw = new ImageWindow(W, H, nc, 32, true, nc >= 3, "osc_core_hdr");
      var view = rw.mainView;
      view.beginProcess(UndoFlag_NoSwapFile);
      try {
         for (var c4 = 0; c4 < nc; ++c4) {
            var A = new Float32Array(W * H);
            LI.getSamples(A, new Rect(W, H), c4);
            if (scale !== 1) for (var p = 0; p < A.length; ++p) A[p] *= scale;
            var oc = O[c4];
            for (var yy = 0; yy < bh; ++yy) {
               var base = (y0 + yy) * W + x0, row = yy * bw;
               for (var xx = 0; xx < bw; ++xx) A[base + xx] = oc[row + xx] * scale;
            }
            view.image.setSamples(A, new Rect(W, H), c4);
            A = null;
         }
      } finally { view.endProcess(); }
      try {
         var kw = [];
         try { kw = lw.keywords; } catch (e1) {}
         kw.push(new FITSKeyword("PSHDR", "'osc-core'", "PhotonScript OSC core HDR from the short subs"));
         rw.keywords = kw;
      } catch (e2) { log("  keywords not copied (" + e2 + ")"); }
      removeQuiet(item.output);
      if (!rw.saveAs(item.output, false, false, false, false)) throw new Error("save returned false: " + item.output);
      rec.status = "ok";
      log("[" + item.name + "] saved " + item.output + " (" + M.n + " px replaced, rescale " + scale.toFixed(4) + ")");
   } catch (e) {
      rec.status = "failed";
      rec.error = ("" + e).replace(/^Error: /, "");
      removeQuiet(item.output);
      log("SKIPPED [" + item.name + "]: " + rec.error + " (no master_" + item.name + "; the finish keeps " +
          item.inputs[0].stack + ")");
   } finally {
      closeQuiet(rw); closeQuiet(sw); closeQuiet(lw);
      timing("core combine", t0);
   }
   return rec;
}

function writeSteps(result, rec) {
   try {
      var d = STEPSFILE.substring(0, STEPSFILE.lastIndexOf("/"));
      ensureDir(d);
      var f = new File; f.createForWriting(STEPSFILE);
      f.outTextLn(JSON.stringify({ result: result, finished_utc: (new Date).toISOString(),
                                   threshold: THRESHOLD, blend_width: BLEND_WIDTH, item: rec }, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}

function main() {
   console.show();
   log("OSC core combine: " + CONFIG.item.inputs[0].stack + " + " + CONFIG.item.inputs[1].stack +
       ", threshold " + THRESHOLD + ", blend " + BLEND_WIDTH);
   var rec = combine(CONFIG.item);
   writeSteps(rec.status, rec);
   log("DONE - " + (rec.status === "ok" ? "master_" + CONFIG.item.name + " made" : "not made"));
}

try { main(); log("EXIT OK"); }
catch (e) { writeSteps("error", { status: "failed", error: "" + e }); log("ERROR: " + e.toString()); console.criticalln("[OSCHDR] FAILED: " + e.toString()); }
