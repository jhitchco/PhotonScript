// PhotonScript OSC channel split (PJSR), PS-265. Template: `photonscript
// osc-split` replaces the CONFIG line below and writes the result into the
// split folder as split_run.js. Do not run this template directly.
//
// A dual-band (LP) OSC master gives narrowband masters, a broadband
// (IR-cut) OSC master gives R, G and B masters, all in the geometry of the
// source masters (same registration, nothing is resampled):
//   kind "hoo": master_Ha = R, master_OIII = (G + B) / 2
//   kind "rgb": master_R, master_G, master_B
// so `photonscript finish --mono-run <split folder>` makes the HOO image
// with the RGB stars (finish_mono_color.js) from one Seestar run.
// Every item is optional: a failure is logged and the others go on. The log
// ends EXIT OK when at least one master was written.
//
// PJSR rules (HANDBOOK sec 6): pure ASCII; no slash-star sequence inside a
// line comment.

#include <pjsr/DataType.jsh>

//__CONFIG__

var OUT = CONFIG.out;
var LOGFILE = OUT + "/split.log";
var LOGLINES = [];
function ensureDir(d) { if (!File.directoryExists(d)) File.createDirectory(d, true); }
function log(s) {
   console.noteln("<b>[SPLIT]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   try {
      ensureDir(OUT);
      var f = new File; f.createForWriting(LOGFILE);
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("could not write " + LOGFILE + " (" + e + ")"); }
}

// one mono image from an expression on the source view, saved as path
function extract(view, expr, id, path) {
   var PM = new PixelMath;
   PM.expression = expr;
   PM.useSingleExpression = true;
   PM.createNewImage = true;
   PM.showNewImage = false;
   PM.newImageId = id;
   PM.newImageWidth = 0; PM.newImageHeight = 0;
   PM.newImageColorSpace = PixelMath.prototype.Gray;
   PM.newImageSampleFormat = PixelMath.prototype.f32;
   PM.rescale = false;
   PM.truncate = true;
   if (!PM.executeOn(view, false)) throw new Error("PixelMath returned false (" + expr + ")");
   var w = ImageWindow.windowById(id);
   if (w.isNull) throw new Error("no image " + id);
   try {
      // keep the source master's keywords (DATE-OBS, site, XPIXSZ, FOCALLEN,
      // RA / DEC: the plate solve seeds from them; 2026-11-21 NGC 281 split
      // masters without them failed all 25 solve seeds), new FILTER, no
      // BAYERPAT (a mono channel)
      var kw = [];
      try {
         var src = view.window.keywords;
         for (var k = 0; k < src.length; ++k)
            if (src[k].name !== "FILTER" && src[k].name !== "BAYERPAT" && src[k].name !== "HISTORY") kw.push(src[k]);
      } catch (e1) {}
      kw.push(new FITSKeyword("FILTER", "'" + id.replace(/^PS_/, "") + "'", "PS-265 channel of an OSC master"));
      kw.push(new FITSKeyword("HISTORY", "", "PhotonScript osc-split: " + expr + " of " + File.extractName(CONFIG_SRC)));
      w.keywords = kw;
   } catch (e0) {}
   if (File.exists(path)) File.remove(path);
   if (!w.saveAs(path, false, false, false, false)) throw new Error("could not write " + path);
   w.forceClose();
   log("saved " + path + " (" + expr + ")");
}

var CONFIG_SRC = "";
var MADE = 0;
function splitOne(it) {
   CONFIG_SRC = it.src;
   if (!File.exists(it.src)) throw new Error("no master at " + it.src);
   var ws = ImageWindow.open(it.src);
   if (!ws.length) throw new Error("could not open " + it.src);
   var w = ws[0];
   try {
      if (!w.mainView.image.isColor) throw new Error(File.extractName(it.src) + " is not a color master");
      var M = OUT + "/master/";
      ensureDir(OUT + "/master");
      if (it.kind === "hoo") {
         extract(w.mainView, "$T[0]", "PS_Ha", M + "master_Ha.xisf");
         extract(w.mainView, "($T[1] + $T[2]) / 2", "PS_OIII", M + "master_OIII.xisf");
         MADE += 2;
      } else {
         extract(w.mainView, "$T[0]", "PS_R", M + "master_R.xisf");
         extract(w.mainView, "$T[1]", "PS_G", M + "master_G.xisf");
         extract(w.mainView, "$T[2]", "PS_B", M + "master_B.xisf");
         MADE += 3;
      }
   } finally { w.forceClose(); }
}

function main() {
   console.show();
   log("osc-split into " + OUT + ": " + CONFIG.items.length + " item(s)");
   for (var i = 0; i < CONFIG.items.length; ++i) {
      var it = CONFIG.items[i];
      try { log(it.kind + " from " + it.src); splitOne(it); }
      catch (e) { log("SKIPPED [" + it.kind + "]: " + ("" + e).replace(/^Error: /, "")); }
   }
   if (!MADE) throw new Error("no master written");
   log("DONE - " + MADE + " master(s)");
}

try { main(); log("EXIT OK"); }
catch (e) { log("ERROR: " + e.toString()); console.criticalln("[SPLIT] FAILED: " + e.toString()); }
