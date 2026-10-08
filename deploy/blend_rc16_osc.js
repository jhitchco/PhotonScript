// PhotonScript two-rig blend (PJSR), PS-153: put the RC16 luminance core into
// the Piggy-600 color image. Template: `photonscript blend` replaces the
// CONFIG line (and, when PixInsight's ImageSolver exists, the solver include)
// and writes the result into a NEW blend run folder as blend_run.js. Do not
// run this template directly.
//
//   open the OSC master (color) and the RC16 master(s) (L, else the mean of
//   the given LRGB masters as luminance)
//   -> plate solve both (ImageSolver; an existing solution is kept)
//   -> product 1, OSC scale:
//      Resample the RC16 luminance down to the OSC scale
//      -> StarAlignment with the OSC as reference (fallback: affine warp
//         fitted through both astrometric solutions)
//      -> footprint mask: registered pixels > 0, inset from the RC16 edge,
//         feathered (distance transform + smoothstep) so the seam is soft;
//         optional luminance mask (bright structure only)
//      -> linear fit of the RC16 luminance to the OSC CIE Y inside the
//         footprint (background and scale match)
//      -> CIE Lab: L = L_osc * (1 - k) + L_rc16 * k, k = WEIGHT * mask,
//         a and b from the OSC (ChannelExtraction / ChannelCombination)
//      -> <Name>_blend_linear.xisf, stretched <Name>_blend.{xisf,tif,jpg},
//         <Name>_osc_ab.jpg (the OSC alone at the same stretch), the mask
//   -> product 2, RC16 scale (CORE): crop the OSC around the footprint,
//      Resample it up to the RC16 scale, StarAlignment with the RC16
//      luminance as reference (fallback: affine warp through the solutions)
//      -> L from the RC16 (fitted to the OSC Y), a and b from the OSC
//      -> <Name>_core_linear.xisf, stretched <Name>_core.{xisf,tif,jpg}
//   + <Name>_blend_steps.json (which step ran with which tool and settings),
//   out/timing_pi.csv, out/blend.log ending EXIT OK or ERROR.
//
// STAGE "linear" (default): inputs are linear masters, the fit runs against
// CIE Y and the RC16 goes through the CIE L* curve before the blend; outputs
// get a linked stretch. STAGE "final": inputs are already stretched, the fit
// runs against CIE L directly and nothing is stretched again.
//
// Every optional step is guarded: a missing process or a failure is logged
// and skipped (or falls back), never fatal unless no registration worked.
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

var OUT = CONFIG.out;
var FINAL = OUT + "/final";
var WORK = OUT + "/work";
var NAME = CONFIG.name;
var STAGE = CONFIG.stage;               // linear | final
var LIN = (STAGE !== "final");
var WEIGHT = CONFIG.weight;             // RC16 share of L inside the mask (0..1)
var LOGFILE = OUT + "/blend.log";
var TIMINGFILE = OUT + "/timing_pi.csv";

var LOGLINES = [];
var STEPS = [];
var T0 = Date.now();
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function writeText(path, lines) {
   try {
      var f = new File; f.createForWriting(path);
      for (var i = 0; i < lines.length; ++i) f.outTextLn(lines[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + path + " (" + e + ")"); }
}
function log(s) {
   console.noteln("<b>[BLEND]</b> " + s); console.flush();
   var mins = ((Date.now() - T0) / 60000).toFixed(1);
   LOGLINES.push((new Date).toISOString() + "  [+" + mins + "m]  " + s);
   ensureDir(OUT);
   writeText(LOGFILE, LOGLINES);
}
// status: ran | fallback | skipped | failed
function step(name, tool, status, settings) {
   STEPS.push({ step: name, tool: tool, status: status, settings: settings || {} });
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
function writeSteps(result, extra) {
   try {
      ensureDir(FINAL);
      var doc = { name: NAME, stage: STAGE, result: result, finished_utc: (new Date).toISOString(),
                  osc: CONFIG.osc.path, rc16: CONFIG.rc16, weight: WEIGHT,
                  products: extra || {}, blend: { steps: STEPS } };
      var f = new File; f.createForWriting(FINAL + "/" + NAME + "_blend_steps.json");
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}
function removeQuiet(path) { try { if (File.exists(path)) File.remove(path); } catch (e) {} }
function closeQuiet(w) { try { if (w && !w.isNull) w.forceClose(); } catch (e) {} }

// ---------------------------------------------------------------- images

function openOne(path, what) {
   if (!File.exists(path)) throw new Error(what + ": no file at " + path);
   var ws = ImageWindow.open(path);
   if (!ws.length) throw new Error(what + ": could not open " + path);
   for (var i = 1; i < ws.length; ++i) closeQuiet(ws[i]);
   return ws[0];
}

function newWindow(w, h, nc, id) {
   return new ImageWindow(w, h, nc, 32, true, nc >= 3, id);
}

function channels(img) {
   var W = img.width, H = img.height, rect = new Rect(W, H), out = [];
   for (var c = 0; c < img.numberOfChannels; ++c) {
      var a = new Float32Array(W * H); img.getSamples(a, rect, c); out.push(a);
   }
   return out;
}

function putChannels(win, arrs) {
   var v = win.mainView, img = v.image, rect = new Rect(img.width, img.height);
   v.beginProcess(UndoFlag_NoSwapFile);
   for (var c = 0; c < arrs.length; ++c) img.setSamples(arrs[c], rect, c);
   v.endProcess();
}

function windowFrom(arrs, W, H, id) {
   var w = newWindow(W, H, arrs.length, id);
   putChannels(w, arrs);
   return w;
}

// A float copy of a view in a new window (no keywords, no astrometric solution).
function dupWindow(view, id) {
   var img = view.image;
   var w = newWindow(img.width, img.height, img.numberOfChannels, id);
   w.mainView.beginProcess(UndoFlag_NoSwapFile);
   w.mainView.image.assign(img);
   w.mainView.endProcess();
   return w;
}

function trySave(w, path, label) {
   try {
      if (w.saveAs(path, false, false, false, false)) { log("saved " + label + ": " + path); return true; }
      log("save FAILED (returned false): " + path);
   } catch (e) { log("save FAILED " + path + " (" + e + ")"); }
   return false;
}

// A Crop invalidates the WCS; clear it first so PixInsight does not stop on a
// Yes/No dialog in an unattended run.
function clearWcs(view) {
   try { view.window.clearAstrometricSolution(); } catch (e) { log("clearAstrometricSolution: " + e); }
}

function cropTo(view, x0, y0, x1, y1) {
   clearWcs(view);
   var img = view.image;
   var CR = new Crop;
   CR.mode = Crop.prototype.AbsolutePixels;
   CR.leftMargin = -x0; CR.topMargin = -y0;
   CR.rightMargin = -(img.width - x1); CR.bottomMargin = -(img.height - y1);
   if (!CR.executeOn(view, false)) throw new Error("Crop returned false");
}

function resampleBy(view, f) {
   clearWcs(view);
   var R = new Resample;
   R.xSize = f; R.ySize = f;
   R.mode = Resample.prototype.RelativeDimensions;
   R.absoluteMode = Resample.prototype.ForceWidthAndHeight;
   try { R.interpolation = Resample.prototype.Auto; } catch (e) {}
   if (!R.executeOn(view, false)) throw new Error("Resample returned false");
}

// ---------------------------------------------------------------- astrometry

function hasSolution(w) {
   try { return !!w.hasAstrometricSolution; } catch (e) { return false; }
}

// ImageSolver with a spiral search around the target (the Piggy-600 frame
// centre is usually not the target; the RC16 is close to it).
function plateSolve(w, what, p) {
   if (hasSolution(w) && !CONFIG.resolve) {
      log("plate solve " + what + ": already solved (kept)");
      step("plate_solve_" + what, "existing", "ran", {});
      return true;
   }
   if (!CONFIG.solve) {
      step("plate_solve_" + what, "ImageSolver", "skipped", { reason: "solve off" });
      return hasSolution(w);
   }
   if (typeof ImageSolver === "undefined") {
      log("plate solve " + what + ": ImageSolver script not found in this PixInsight install; skipped");
      step("plate_solve_" + what, "ImageSolver", "skipped", { reason: "not installed" });
      return hasSolution(w);
   }
   if (isNaN(CONFIG.ra_deg) || isNaN(CONFIG.dec_deg) || CONFIG.ra_deg === null || CONFIG.dec_deg === null) {
      log("plate solve " + what + ": no target coordinates; skipped");
      step("plate_solve_" + what, "ImageSolver", "skipped", { reason: "no coordinates" });
      return hasSolution(w);
   }
   var focal = (p.pixel_um / p.scale) * 206.265;
   var offsets = [[0, 0]];
   for (var r = 1; r <= p.rings; ++r)
      for (var i = -r; i <= r; ++i)
         for (var j = -r; j <= r; ++j)
            if (Math.max(Math.abs(i), Math.abs(j)) === r) offsets.push([i * p.step_deg, j * p.step_deg]);
   for (var k = 0; k < offsets.length; ++k) {
      var dDec = offsets[k][1];
      var dRa = offsets[k][0] / Math.max(0.2, Math.cos((CONFIG.dec_deg + dDec) * Math.PI / 180));
      var ra = (CONFIG.ra_deg + dRa + 360) % 360;
      var dec = Math.max(-89.9, Math.min(89.9, CONFIG.dec_deg + dDec));
      try {
         var solver = new ImageSolver();
         solver.Init(w, false);
         solver.metadata.ra = ra;
         solver.metadata.dec = dec;
         solver.metadata.focal = focal;
         solver.metadata.useFocal = true;
         solver.metadata.xpixsz = p.pixel_um;
         solver.metadata.resolution = p.scale / 3600;
         try { solver.solverCfg.showStars = false; } catch (e1) {}
         try { solver.solverCfg.showDistortion = false; } catch (e2) {}
         try { solver.solverCfg.generateErrorImg = false; } catch (e3) {}
         try { solver.solverCfg.generateDistortModel = false; } catch (e4) {}
         try { solver.solverCfg.distortionCorrection = true; } catch (e5) {}
         if (solver.SolveImage(w)) {
            log("plate solve " + what + ": OK on try " + (k + 1) + " (" + p.scale + "\"/px nominal)");
            step("plate_solve_" + what, "ImageSolver", "ran", { try_n: k + 1, arcsec_px: p.scale });
            return true;
         }
      } catch (e) { log("  " + what + " solve try " + (k + 1) + " failed: " + e); }
   }
   log("plate solve " + what + ": FAILED after " + offsets.length + " seeds");
   step("plate_solve_" + what, "ImageSolver", "failed", { seeds: offsets.length });
   return false;
}

// image pixel <-> sky through the window's astrometric solution: the native
// methods when this PixInsight has them, else WCSmetadata.jsh's ImageMetadata.
var MD_CACHE = {};
function metadataOf(w) {
   var id = w.mainView.id;
   if (MD_CACHE[id]) return MD_CACHE[id];
   if (typeof ImageMetadata === "undefined") return null;
   var md = new ImageMetadata();
   md.ExtractMetadata(w);
   MD_CACHE[id] = md;
   return md;
}
function pixToSky(w, x, y) {
   try { var p = w.imageToCelestial(new Point(x, y)); if (p) return [p.x, p.y]; } catch (e) {}
   try { var md = metadataOf(w); var q = md ? md.Convert_I_RD(new Point(x, y)) : null; if (q) return [q.x, q.y]; } catch (e2) {}
   return null;
}
function skyToPix(w, ra, dec) {
   try { var p = w.celestialToImage(new Point(ra, dec)); if (p) return [p.x, p.y]; } catch (e) {}
   try { var md = metadataOf(w); var q = md ? md.Convert_RD_I(new Point(ra, dec)) : null; if (q) return [q.x, q.y]; } catch (e2) {}
   return null;
}

// Solve the 3x3 system M a = b (Gauss with partial pivoting).
function solve3(M, b) {
   var A = [M[0].concat([b[0]]), M[1].concat([b[1]]), M[2].concat([b[2]])];
   for (var c = 0; c < 3; ++c) {
      var piv = c;
      for (var r = c + 1; r < 3; ++r) if (Math.abs(A[r][c]) > Math.abs(A[piv][c])) piv = r;
      var t = A[c]; A[c] = A[piv]; A[piv] = t;
      if (Math.abs(A[c][c]) < 1e-12) throw new Error("singular affine fit");
      for (var r2 = 0; r2 < 3; ++r2) {
         if (r2 === c) continue;
         var f = A[r2][c] / A[c][c];
         for (var k = c; k < 4; ++k) A[r2][k] -= f * A[c][k];
      }
   }
   return [A[0][3] / A[0][0], A[1][3] / A[1][1], A[2][3] / A[2][2]];
}

// Affine dst pixel -> src pixel fitted through both solutions on a grid over
// the dst rectangle. Returns { a: [u0,ux,uy], b: [v0,vx,vy], rms, n }.
function wcsAffine(dstWin, srcWin, x0, y0, x1, y1) {
   var pts = [], N = 9;
   for (var i = 0; i < N; ++i)
      for (var j = 0; j < N; ++j) {
         var x = x0 + (x1 - x0) * i / (N - 1), y = y0 + (y1 - y0) * j / (N - 1);
         var s = pixToSky(dstWin, x, y);
         if (!s) continue;
         var u = skyToPix(srcWin, s[0], s[1]);
         if (!u) continue;
         pts.push([x, y, u[0], u[1]]);
      }
   if (pts.length < 6) throw new Error("only " + pts.length + " WCS points (no usable astrometric solution)");
   var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
   var M = [[0, 0, 0], [0, 0, 0], [0, 0, 0]], bu = [0, 0, 0], bv = [0, 0, 0];
   for (var n = 0; n < pts.length; ++n) {
      var r = [1, pts[n][0] - cx, pts[n][1] - cy];
      for (var p = 0; p < 3; ++p) {
         for (var q = 0; q < 3; ++q) M[p][q] += r[p] * r[q];
         bu[p] += r[p] * pts[n][2]; bv[p] += r[p] * pts[n][3];
      }
   }
   var a = solve3(M, bu), b = solve3(M, bv);
   a[0] -= a[1] * cx + a[2] * cy; b[0] -= b[1] * cx + b[2] * cy;
   var ss = 0;
   for (var m = 0; m < pts.length; ++m) {
      var du = a[0] + a[1] * pts[m][0] + a[2] * pts[m][1] - pts[m][2];
      var dv = b[0] + b[1] * pts[m][0] + b[2] * pts[m][1] - pts[m][3];
      ss += du * du + dv * dv;
   }
   return { a: a, b: b, rms: Math.sqrt(ss / pts.length), n: pts.length };
}

// Inverse of an affine { a, b } (keeps rms and n).
function invertAffine(T) {
   var a = T.a, b = T.b, det = a[1] * b[2] - a[2] * b[1];
   if (Math.abs(det) < 1e-12) throw new Error("singular affine");
   return { a: [(a[2] * b[0] - b[2] * a[0]) / det, b[2] / det, -a[2] / det],
            b: [(b[1] * a[0] - a[1] * b[0]) / det, -b[1] / det, a[1] / det], rms: T.rms, n: T.n };
}

// Bilinear warp: out(x, y) = src(u, v) with (u, v) = affine(x, y) * k, where k
// rescales original src pixels to the (resampled) src array. 0 outside.
function warp(src, sW, sH, T, k, dW, dH) {
   var out = [], c, x, y;
   for (c = 0; c < src.length; ++c) out.push(new Float32Array(dW * dH));
   for (y = 0; y < dH; ++y) {
      for (x = 0; x < dW; ++x) {
         var u = (T.a[0] + T.a[1] * x + T.a[2] * y + 0.5) * k - 0.5;
         var v = (T.b[0] + T.b[1] * x + T.b[2] * y + 0.5) * k - 0.5;
         if (u < 0 || v < 0 || u > sW - 1 || v > sH - 1) continue;
         var iu = Math.floor(u), iv = Math.floor(v);
         if (iu >= sW - 1) iu = sW - 2;
         if (iv >= sH - 1) iv = sH - 2;
         var fu = u - iu, fv = v - iv, i0 = iv * sW + iu, o = y * dW + x;
         for (c = 0; c < src.length; ++c) {
            var s = src[c];
            out[c][o] = (s[i0] * (1 - fu) + s[i0 + 1] * fu) * (1 - fv) +
                        (s[i0 + sW] * (1 - fu) + s[i0 + sW + 1] * fu) * fv;
         }
      }
   }
   return out;
}

// StarAlignment of one file onto a reference file; returns the registered
// window (reference geometry, 0 outside the target) or throws.
function starAlign(refPath, tgtPath, outDir, sensitivity, layers) {
   ensureDir(outDir);
   var SA = new StarAlignment;
   SA.referenceImage = refPath; SA.referenceIsFile = true;
   SA.targets = [[true, true, tgtPath]];
   SA.outputDirectory = outDir;
   SA.outputExtension = ".xisf"; SA.overwriteExistingFiles = true;
   try { SA.outputPostfix = "_r"; } catch (e0) {}
   SA.distortionCorrection = false;
   SA.structureLayers = layers;
   SA.sensitivity = sensitivity;
   SA.useTriangleSimilarity = true;
   try { SA.generateMasks = false; SA.generateDrizzleData = false; } catch (e1) {}
   if (!SA.executeGlobal()) throw new Error("StarAlignment returned false");
   var outPath = outDir + "/" + File.extractName(tgtPath) + "_r.xisf";
   if (!File.exists(outPath)) throw new Error("StarAlignment wrote no " + outPath);
   return openOne(outPath, "registered");
}

// ---------------------------------------------------------------- color space

// CIE Lab (and, for linear data, CIE Y) of an RGB view via ChannelExtraction.
// Returns { L: win, a: win, b: win, Y: win|null }.
function labOf(view, tag) {
   var ids = { L: "PS_" + tag + "_L", a: "PS_" + tag + "_a", b: "PS_" + tag + "_b", Y: "PS_" + tag + "_Y" };
   var CE = new ChannelExtraction;
   CE.colorSpace = ChannelExtraction.prototype.CIELab;
   CE.channels = [[true, ids.L], [true, ids.a], [true, ids.b]];
   try { CE.sampleFormat = ChannelExtraction.prototype.f32; } catch (e) {}
   if (!CE.executeOn(view, false)) throw new Error("ChannelExtraction CIELab returned false");
   var res = { L: ImageWindow.windowById(ids.L), a: ImageWindow.windowById(ids.a),
               b: ImageWindow.windowById(ids.b), Y: null };
   if (res.L.isNull || res.a.isNull || res.b.isNull) throw new Error("ChannelExtraction made no Lab windows");
   if (LIN) {
      var CY = new ChannelExtraction;
      CY.colorSpace = ChannelExtraction.prototype.CIEXYZ;
      CY.channels = [[false, ""], [true, ids.Y], [false, ""]];
      try { CY.sampleFormat = ChannelExtraction.prototype.f32; } catch (e2) {}
      if (!CY.executeOn(view, false)) throw new Error("ChannelExtraction CIEXYZ returned false");
      res.Y = ImageWindow.windowById(ids.Y);
      if (res.Y.isNull) throw new Error("ChannelExtraction made no Y window");
   }
   return res;
}

function closeLab(lab) { if (lab) { closeQuiet(lab.L); closeQuiet(lab.a); closeQuiet(lab.b); closeQuiet(lab.Y); } }

// CIE L* (0..1) of a CIE Y (0..1), the curve PixInsight's Lab uses.
function lstar(y) {
   if (y <= 0) return 0;
   if (y >= 1) return 1;
   return y > 0.008856 ? 1.16 * Math.pow(y, 1 / 3) - 0.16 : 9.033 * y;
}

// L (from Lab windows, replaced by newL) recombined with their a and b into
// the RGB view (same size).
function labCombine(view, lab, newL) {
   putChannels(lab.L, [newL]);
   var CC = new ChannelCombination;
   CC.colorSpace = ChannelCombination.prototype.CIELab;
   CC.channels = [[true, lab.L.mainView.id], [true, lab.a.mainView.id], [true, lab.b.mainView.id]];
   if (!CC.executeOn(view, false)) throw new Error("ChannelCombination CIELab returned false");
}

// ---------------------------------------------------------------- fit + mask

// y = a + b * x over the pixels where use(i) is true (sampled), sigma clipped.
function linearFit(x, y, use, n) {
   var stride = Math.max(1, Math.floor(n / 400000));
   var idx = [];
   for (var i = 0; i < n; i += stride)
      if (use(i) && x[i] > 0 && y[i] > 0 && x[i] < 0.95 && y[i] < 0.95) idx.push(i);
   if (idx.length < 200) throw new Error("only " + idx.length + " overlap samples for the fit");
   var a = 0, b = 1, rms = 0, keep = idx;
   for (var it = 0; it < 4; ++it) {
      var sx = 0, sy = 0, sxx = 0, sxy = 0, m = keep.length;
      for (var k = 0; k < m; ++k) { var xv = x[keep[k]], yv = y[keep[k]]; sx += xv; sy += yv; sxx += xv * xv; sxy += xv * yv; }
      var den = m * sxx - sx * sx;
      if (Math.abs(den) < 1e-20) throw new Error("degenerate fit");
      b = (m * sxy - sx * sy) / den; a = (sy - b * sx) / m;
      var ss = 0;
      for (k = 0; k < m; ++k) { var r = y[keep[k]] - (a + b * x[keep[k]]); ss += r * r; }
      rms = Math.sqrt(ss / m);
      var next = [];
      for (k = 0; k < idx.length; ++k) { var r2 = y[idx[k]] - (a + b * x[idx[k]]); if (Math.abs(r2) <= 3 * rms) next.push(idx[k]); }
      if (next.length < 200) break;
      keep = next;
   }
   if (!(b > 0)) throw new Error("fit slope " + b + " (no positive correlation: registration wrong?)");
   return { a: a, b: b, rms: rms, n: keep.length };
}

// Feathered footprint mask from a registered image (0 outside the RC16
// field). Inside the bounding box: chamfer distance to the nearest outside
// pixel, mask = smoothstep((d - inset) / feather). Returns { m, box }.
function footprintMask(reg, W, H) {
   var x0 = W, y0 = H, x1 = -1, y1 = -1, x, y, i;
   for (y = 0; y < H; ++y)
      for (x = 0; x < W; ++x)
         if (reg[y * W + x] > 0) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
   if (x1 < 0) throw new Error("registered RC16 image is empty");
   var bw = x1 - x0 + 3, bh = y1 - y0 + 3;   // one outside pixel on each side
   var d = new Float32Array(bw * bh), BIG = 1e9;
   for (y = 0; y < bh; ++y)
      for (x = 0; x < bw; ++x) {
         var gx = x0 - 1 + x, gy = y0 - 1 + y;
         var inside = gx >= 0 && gy >= 0 && gx < W && gy < H && reg[gy * W + gx] > 0;
         d[y * bw + x] = inside ? BIG : 0;
      }
   for (y = 0; y < bh; ++y)
      for (x = 0; x < bw; ++x) {
         i = y * bw + x; if (d[i] === 0) continue;
         var v = d[i];
         if (x > 0) v = Math.min(v, d[i - 1] + 1);
         if (y > 0) { v = Math.min(v, d[i - bw] + 1);
            if (x > 0) v = Math.min(v, d[i - bw - 1] + 1.4142);
            if (x < bw - 1) v = Math.min(v, d[i - bw + 1] + 1.4142); }
         d[i] = v;
      }
   for (y = bh - 1; y >= 0; --y)
      for (x = bw - 1; x >= 0; --x) {
         i = y * bw + x; if (d[i] === 0) continue;
         var w = d[i];
         if (x < bw - 1) w = Math.min(w, d[i + 1] + 1);
         if (y < bh - 1) { w = Math.min(w, d[i + bw] + 1);
            if (x < bw - 1) w = Math.min(w, d[i + bw + 1] + 1.4142);
            if (x > 0) w = Math.min(w, d[i + bw - 1] + 1.4142); }
         d[i] = w;
      }
   var shortSide = Math.min(x1 - x0 + 1, y1 - y0 + 1);
   var inset = Math.max(1, CONFIG.inset_frac * shortSide), feather = Math.max(1, CONFIG.feather_frac * shortSide);
   var m = new Float32Array(W * H);
   for (y = 0; y < bh; ++y)
      for (x = 0; x < bw; ++x) {
         var gx2 = x0 - 1 + x, gy2 = y0 - 1 + y;
         if (gx2 < 0 || gy2 < 0 || gx2 >= W || gy2 >= H) continue;
         var t = (d[y * bw + x] - inset) / feather;
         if (t <= 0) continue;
         if (t > 1) t = 1;
         m[gy2 * W + gx2] = t * t * (3 - 2 * t);
      }
   log("footprint: " + (x1 - x0 + 1) + "x" + (y1 - y0 + 1) + " px at (" + x0 + "," + y0 + "), inset " +
       inset.toFixed(1) + " px, feather " + feather.toFixed(1) + " px");
   step("mask", "footprint (chamfer distance + smoothstep)", "ran",
        { box: [x0, y0, x1, y1], inset_px: +inset.toFixed(1), feather_px: +feather.toFixed(1) });
   return { m: m, box: [x0, y0, x1 + 1, y1 + 1] };
}

function quantile(arr, use, n, q) {
   var s = [], stride = Math.max(1, Math.floor(n / 200000));
   for (var i = 0; i < n; i += stride) if (use(i)) s.push(arr[i]);
   if (!s.length) return 0;
   s.sort(function (p, r) { return p - r; });
   return s[Math.min(s.length - 1, Math.floor(q * s.length))];
}

// ---------------------------------------------------------------- stretch

function mtfv(m, x) { if (x <= 0) return 0; if (x >= 1) return 1;
   return ((m - 1) * x) / (((2 * m - 1) * x) - m); }

function stretchParams(view) {
   var img = view.image;
   var med = img.median(), mad = img.MAD() * 1.4826;
   var c0 = Math.max(0, Math.min(1, med - CONFIG.shadow_sigma * mad));
   return { c0: c0, m: mtfv(CONFIG.bg_target, Math.max(1.0e-6, med - c0)) };
}

function applyHT(view, p) {
   var HT = new HistogramTransformation;
   HT.H = [[0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1], [0, 0.5, 1, 0, 1],
           [p.c0, p.m, 1, 0, 1], [0, 0.5, 1, 0, 1]];
   if (!HT.executeOn(view, false)) throw new Error("HistogramTransformation returned false");
}

// Save <base>.xisf (+ .tif 16-bit + .jpg) of a view, stretched when LIN.
function saveLook(w, base, label, p) {
   try {
      if (LIN) applyHT(w.mainView, p || stretchParams(w.mainView));
      trySave(w, base + ".xisf", label + " xisf");
      try {
         var SF = new SampleFormatConversion;
         SF.format = SampleFormatConversion.prototype.To16Bit;
         SF.executeOn(w.mainView, false);
      } catch (e) { log("16-bit conversion skipped (" + e + ")"); }
      trySave(w, base + ".tif", label + " tif");
      trySave(w, base + ".jpg", label + " jpg");
   } catch (e2) { log(label + ": look skipped (" + e2 + ")"); }
}

// ---------------------------------------------------------------- inputs

// RC16 luminance: the L master, else the mean of the given masters (same
// geometry: integrate registers every RC16 stack to one reference).
function rc16Luminance() {
   var list = CONFIG.rc16, lum = null, used = [];
   for (var i = 0; i < list.length; ++i) if (list[i].filter === "L") { lum = [list[i]]; break; }
   if (!lum) lum = list;
   var w0 = openOne(lum[0].path, "RC16 " + lum[0].filter);
   used.push(lum[0].filter);
   if (lum.length === 1 && !w0.mainView.image.isColor) return { win: w0, used: used };
   var img0 = w0.mainView.image, W = img0.width, H = img0.height, N = W * H;
   var acc = new Float32Array(N), cnt = 0, rect = new Rect(W, H), tmp = new Float32Array(N), c, k;
   for (c = 0; c < img0.numberOfChannels; ++c) { img0.getSamples(tmp, rect, c); for (k = 0; k < N; ++k) acc[k] += tmp[k]; ++cnt; }
   for (i = 1; i < lum.length; ++i) {
      var wi = openOne(lum[i].path, "RC16 " + lum[i].filter), im = wi.mainView.image;
      if (im.width !== W || im.height !== H) { log("RC16 " + lum[i].filter + ": size differs from " + lum[0].filter + "; left out"); closeQuiet(wi); continue; }
      for (c = 0; c < im.numberOfChannels; ++c) { im.getSamples(tmp, rect, c); for (k = 0; k < N; ++k) acc[k] += tmp[k]; ++cnt; }
      used.push(lum[i].filter);
      closeQuiet(wi);
   }
   for (k = 0; k < N; ++k) acc[k] /= cnt;
   var lw = windowFrom([acc], W, H, "PS_RCLUM");
   // keep the first master's astrometric solution source for the WCS fallback
   return { win: lw, used: used, solved_src: w0 };
}

// ---------------------------------------------------------------- products

// Product 1: the OSC frame with the RC16 core in L.
function productFull(osc, rc) {
   var oImg = osc.mainView.image, W = oImg.width, H = oImg.height, N = W * H;
   var rImg = rc.win.mainView.image, rW = rImg.width, rH = rImg.height;
   var s = CONFIG.rc16_scale / CONFIG.osc.scale;    // RC16 -> OSC size factor (~0.18)
   var reg = null, how = "";

   TSTART("full", "register RC16 at OSC scale");
   var down = dupWindow(rc.win.mainView, "PS_RC_DOWN");
   try {
      resampleBy(down.mainView, s);
      step("resample_rc16", "Resample", "ran", { factor: +s.toFixed(5), size: [down.mainView.image.width, down.mainView.image.height] });
   } catch (e) { closeQuiet(down); throw new Error("Resample of the RC16 failed (" + e + ")"); }
   ensureDir(WORK);
   var refPath = WORK + "/osc_ref.xisf", tgtPath = WORK + "/rc16_at_osc.xisf";
   try {
      var od = dupWindow(osc.mainView, "PS_OSC_REF");
      trySave(od, refPath, "work: OSC reference"); closeQuiet(od);
      trySave(down, tgtPath, "work: RC16 at OSC scale");
      var rw = starAlign(refPath, tgtPath, WORK + "/reg", 0.5, 5);
      reg = channels(rw.mainView.image)[0];
      closeQuiet(rw);
      how = "StarAlignment";
      step("register_full", "StarAlignment", "ran", { reference: "OSC", target: "RC16 resampled" });
   } catch (e2) {
      log("StarAlignment at OSC scale failed (" + e2 + "); trying the astrometric solutions");
      try {
         // fitted over the RC16 field (where it matters), then inverted
         var T = invertAffine(wcsAffine(rc.solved_src || rc.win, osc, 0, 0, rW - 1, rH - 1));
         var dImg = down.mainView.image;
         reg = warp(channels(dImg), dImg.width, dImg.height, T, dImg.width / rW, W, H)[0];
         how = "WCS affine";
         log("  WCS affine: " + T.n + " points, rms " + T.rms.toFixed(3) + " px");
         step("register_full", "WCS affine (bilinear)", "fallback", { points: T.n, rms_px: +T.rms.toFixed(3), error_sa: "" + e2 });
      } catch (e3) {
         step("register_full", "none", "failed", { error_sa: "" + e2, error_wcs: "" + e3 });
         closeQuiet(down);
         throw new Error("could not register the RC16 onto the OSC (StarAlignment: " + e2 + "; WCS: " + e3 + ")");
      }
   }
   closeQuiet(down);
   TEND("full", "register RC16 at OSC scale");

   TSTART("full", "mask + fit + Lab blend");
   var fp = footprintMask(reg, W, H);
   var m = fp.m;
   var lab = labOf(osc.mainView, "OSC");
   var Losc = channels(lab.L.mainView.image)[0];
   var target = LIN ? channels(lab.Y.mainView.image)[0] : Losc;
   var fit = linearFit(reg, target, function (i) { return m[i] > 0.5; }, N);
   log("fit RC16 -> OSC " + (LIN ? "CIE Y" : "CIE L") + ": " + fit.a.toExponential(3) + " + " + fit.b.toFixed(4) +
       " * rc16 (rms " + fit.rms.toExponential(3) + ", " + fit.n + " px)");
   step("linear_fit", "least squares in the footprint (LinearFit model)", "ran",
        { against: LIN ? "CIE Y" : "CIE L", offset: fit.a, scale: fit.b, rms: fit.rms, n: fit.n });
   if (CONFIG.lum_mask) {
      var inFp = function (i) { return m[i] > 0.5; };
      var lo = quantile(Losc, inFp, N, 0.5), hi = quantile(Losc, inFp, N, 0.99);
      hi = lo + Math.max(1e-4, (hi - lo) * 0.5);
      for (var j = 0; j < N; ++j) {
         if (m[j] <= 0) continue;
         var t = (Losc[j] - lo) / (hi - lo); t = t < 0 ? 0 : (t > 1 ? 1 : t);
         m[j] *= t * t * (3 - 2 * t);
      }
      log("luminance mask: L " + lo.toFixed(4) + " .. " + hi.toFixed(4));
      step("lum_mask", "smoothstep on OSC CIE L", "ran", { lo: +lo.toFixed(5), hi: +hi.toFixed(5) });
   } else step("lum_mask", "none", "skipped", { reason: "off" });
   var newL = new Float32Array(N), dsum = 0, dn = 0;
   for (var i = 0; i < N; ++i) {
      var k = WEIGHT * m[i];
      if (k <= 0) { newL[i] = Losc[i]; continue; }
      var v = fit.a + fit.b * reg[i];
      var lr = LIN ? lstar(v) : Math.max(0, Math.min(1, v));
      newL[i] = Losc[i] * (1 - k) + lr * k;
      if (m[i] > 0.99) { dsum += Math.abs(lr - Losc[i]); ++dn; }
   }
   if (dn) log("mean |L_rc16 - L_osc| inside the full mask: " + (dsum / dn).toFixed(4));
   var outW = dupWindow(osc.mainView, "PS_BLEND");
   labCombine(outW.mainView, lab, newL);
   closeLab(lab);
   step("lab_blend", "ChannelExtraction + ChannelCombination (CIE Lab)", "ran",
        { weight: WEIGHT, registration: how, mean_abs_dl: dn ? +(dsum / dn).toFixed(5) : null });
   TEND("full", "mask + fit + Lab blend");

   TSTART("full", "save");
   var mw = windowFrom([m], W, H, "PS_MASK");
   trySave(mw, FINAL + "/" + NAME + "_blend_mask.xisf", "mask"); closeQuiet(mw);
   if (LIN) trySave(outW, FINAL + "/" + NAME + "_blend_linear.xisf", "blend (linear)");
   var p = LIN ? stretchParams(osc.mainView) : null;   // the OSC's stretch for both: a fair A/B
   var ab = dupWindow(osc.mainView, "PS_OSC_AB");
   saveLook(ab, FINAL + "/" + NAME + "_osc_ab", "OSC alone (same stretch)", p);
   closeQuiet(ab);
   saveLook(outW, FINAL + "/" + NAME + "_blend", "blend", p);
   closeQuiet(outW);
   TEND("full", "save");
   return { box: fp.box, registration: how, fit: fit };
}

// Product 2: RC16 geometry and scale, L from the RC16, color from the OSC.
function productCore(osc, rc, box) {
   var rImg = rc.win.mainView.image, rW = rImg.width, rH = rImg.height, N = rW * rH;
   var up = CONFIG.osc.scale / CONFIG.rc16_scale;   // ~5.5
   var col = null, how = "";
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
         var rp = WORK + "/rc16_lum.xisf", tp = WORK + "/osc_core_up.xisf";
         var rd = dupWindow(rc.win.mainView, "PS_RC_REF");
         trySave(rd, rp, "work: RC16 luminance"); closeQuiet(rd);
         trySave(cw, tp, "work: OSC core upsampled");
         var rw = starAlign(rp, tp, WORK + "/reg_core", 0.5, 6);
         col = channels(rw.mainView.image);
         closeQuiet(rw);
         how = "StarAlignment";
         step("register_core", "StarAlignment", "ran", { reference: "RC16", target: "OSC crop upsampled" });
      } catch (e) {
         log("core StarAlignment failed (" + e + "); trying the astrometric solutions");
         step("register_core", "StarAlignment", "failed", { error: "" + e });
      }
      closeQuiet(cw);
   }
   if (!col) {
      try {
         var T = wcsAffine(rc.solved_src || rc.win, osc, 0, 0, rW - 1, rH - 1);
         col = warp(channels(osc.mainView.image), osc.mainView.image.width, osc.mainView.image.height, T, 1, rW, rH);
         how = "WCS affine";
         log("  core WCS affine: " + T.n + " points, rms " + T.rms.toFixed(3) + " OSC px");
         step("register_core", "WCS affine (bilinear)", "fallback", { points: T.n, rms_px: +T.rms.toFixed(3) });
      } catch (e2) {
         step("register_core", "none", "failed", { error_wcs: "" + e2 });
         TEND("core", "register OSC at RC16 scale");
         throw new Error("core: could not put the OSC on the RC16 grid (" + e2 + ")");
      }
   }
   TEND("core", "register OSC at RC16 scale");

   TSTART("core", "fit + Lab combine");
   var cwin = windowFrom(col, rW, rH, "PS_CORE");
   col = null;
   var lab = labOf(cwin.mainView, "CORE");
   var target = channels((LIN ? lab.Y : lab.L).mainView.image)[0];
   var rcl = channels(rImg)[0];
   var fit = linearFit(rcl, target, function (i) { return target[i] > 0; }, N);
   log("core fit RC16 -> OSC " + (LIN ? "CIE Y" : "CIE L") + ": " + fit.a.toExponential(3) + " + " + fit.b.toFixed(4) + " * rc16");
   step("core_fit", "least squares (LinearFit model)", "ran", { offset: fit.a, scale: fit.b, rms: fit.rms, n: fit.n });
   var newL = new Float32Array(N);
   for (var i = 0; i < N; ++i) {
      var v = fit.a + fit.b * rcl[i];
      newL[i] = LIN ? lstar(v) : Math.max(0, Math.min(1, v));
   }
   labCombine(cwin.mainView, lab, newL);
   closeLab(lab);
   step("core_lab", "ChannelCombination (CIE Lab): L RC16, a b OSC", "ran", { registration: how });
   TEND("core", "fit + Lab combine");
   TSTART("core", "save");
   if (LIN) trySave(cwin, FINAL + "/" + NAME + "_core_linear.xisf", "core (linear)");
   saveLook(cwin, FINAL + "/" + NAME + "_core", "core", null);
   closeQuiet(cwin);
   TEND("core", "save");
   return { registration: how, size: [rW, rH] };
}

// ---------------------------------------------------------------- main

function main() {
   console.show();
   log("blend: " + NAME + "  stage " + STAGE + "  weight " + WEIGHT);
   log("OSC:  " + CONFIG.osc.path);
   for (var i = 0; i < CONFIG.rc16.length; ++i) log("RC16 " + CONFIG.rc16[i].filter + ": " + CONFIG.rc16[i].path);
   ensureDir(FINAL); ensureDir(WORK);
   var products = {};

   TSTART("inputs", "open + plate solve");
   var osc = openOne(CONFIG.osc.path, "OSC");
   if (!osc.mainView.image.isColor) throw new Error("the OSC master is not a color image: " + CONFIG.osc.path);
   var rc = rc16Luminance();
   log("RC16 luminance from " + rc.used.join("+") + ": " + rc.win.mainView.image.width + "x" + rc.win.mainView.image.height);
   step("rc16_luminance", rc.used.length > 1 ? "mean of masters" : "master", "ran", { filters: rc.used });
   plateSolve(osc, "osc", CONFIG.osc);
   plateSolve(rc.solved_src || rc.win, "rc16", { scale: CONFIG.rc16_scale, pixel_um: CONFIG.rc16_pixel_um,
                                                step_deg: CONFIG.rc16_step_deg, rings: 2 });
   TEND("inputs", "open + plate solve");

   var full = null;
   try { full = productFull(osc, rc); products.full = { ok: true, registration: full.registration }; }
   catch (e) {
      products.full = { ok: false, error: "" + e };
      log("ERROR (full field): " + e);
   }
   if (CONFIG.core) {
      try { products.core = productCore(osc, rc, full ? full.box : null); products.core.ok = true; }
      catch (e2) { products.core = { ok: false, error: "" + e2 }; log("ERROR (core): " + e2); }
   } else {
      step("core", "none", "skipped", { reason: "core off" });
      products.core = { ok: null, skipped: true };
   }
   closeQuiet(rc.solved_src); closeQuiet(rc.win); closeQuiet(osc);
   if (!CONFIG.keep_work) {
      var junk = [WORK + "/osc_ref.xisf", WORK + "/rc16_at_osc.xisf", WORK + "/reg/rc16_at_osc_r.xisf",
                  WORK + "/rc16_lum.xisf", WORK + "/osc_core_up.xisf", WORK + "/reg_core/osc_core_up_r.xisf"];
      for (var j = 0; j < junk.length; ++j) removeQuiet(junk[j]);
   }
   return products;
}

var PRODUCTS = {};
try {
   PRODUCTS = main();
   var bad = (PRODUCTS.full && PRODUCTS.full.ok === false) || (PRODUCTS.core && PRODUCTS.core.ok === false);
   writeSteps(bad ? "partial" : "ok", PRODUCTS);
   log(bad ? "ERROR: a product failed (see above)" : "EXIT OK");
} catch (e) {
   writeSteps("error: " + e.toString(), PRODUCTS);
   log("ERROR: " + e.toString());
   console.criticalln("[BLEND] FAILED: " + e.toString());
}
writeText(LOGFILE, LOGLINES);
