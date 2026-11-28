// PhotonScript blend vivid (PJSR). Template: photonscript/integration/blend.py
// (render_vivid) replaces the CONFIG line and the two library markers below
// and writes the result as blend_vivid_run.js into the blend run folder.
// Do not run this template directly.
//
// The finish vivid look of deploy/finish_osc.js (vividFinish: the saturation
// curve from SAT_MID to VIVID_SAT_MID, the pink / warm / blue chroma lift
// through the luminance-range mask that spares the background and the
// brightest core, optional LocalHistogramEqualization) applied to the
// stretched L blend <name>_blend.xisf. The blend's stretch has no saturation
// curve, so SAT_MID is 0.5 (none). No star split: the stars stay in the
// image (the mask leaves the brightest pixels alone). No crop.
// Out (FINAL): <name>_blend_vivid.{xisf,tif,jpg}, <name>_blend_vivid_steps.json;
// LOGDIR/blend_vivid.log ending EXIT OK (the vivid image was saved) or ERROR.
// The natural blend is only read. PJSR rules (HANDBOOK sec 6): pure ASCII;
// no slash-star inside a line comment.

#include <pjsr/DataType.jsh>
#include <pjsr/ColorSpace.jsh>
#include <pjsr/UndoFlag.jsh>
#include <pjsr/SampleType.jsh>

//__CONFIG__

function cfgNum(v, d) { return (v === undefined || v === null) ? d : v; }

// The globals finish_osc.js's functions read (same names, same meaning).
var NAME       = CONFIG.name;            // <name>_blend: outputs <name>_blend_vivid.*
var MASTER     = CONFIG.input;           // the stretched blend (xisf)
var FINAL      = CONFIG.final;
var LOGDIR     = CONFIG.out;
var STAGING    = CONFIG.out;
var RC_MODE    = "off";
var USE_RC     = false;
var SAT_MID         = cfgNum(CONFIG.sat_mid, 0.5);
var BG_TARGET       = cfgNum(CONFIG.bg_target, 0.12);
var STAR_STRENGTH   = 1.0;
var EDGE_FRAC       = 0.0;
var FRAME           = null;
var VIVID           = "on";
var VIVID_SAT_MID   = cfgNum(CONFIG.vivid_sat_mid, 0.72);
var VIVID_HUE_BOOST = cfgNum(CONFIG.vivid_hue_boost, 0.25);
var VIVID_LHE       = cfgNum(CONFIG.vivid_lhe, 0.0);
var HOO = "off";
var PALETTE = "off";

//__FINISH_LIB__

// ---------------------------------------------------------------- overrides
// (function declarations: these replace the finish_osc.js ones above)

function writeLog() {
   try {
      ensureDir(LOGDIR);
      var f = new File; f.createForWriting(LOGDIR + "/blend_vivid.log");
      for (var i = 0; i < LOGLINES.length; ++i) f.outTextLn(LOGLINES[i]);
      f.close();
   } catch (e) { console.criticalln("log write failed: " + e); }
}
function log(s) {
   console.noteln("<b>[VIVID]</b> " + s); console.flush();
   LOGLINES.push((new Date).toISOString() + "  " + s);
   writeLog();
}

//__VIVID_LIB__

function writeVividSteps(result) {
   try {
      ensureDir(FINAL);
      var doc = { name: NAME + "_vivid", input: MASTER, result: result,
                  finished_utc: (new Date).toISOString(), steps: STEPS };
      var f = new File; f.createForWriting(FINAL + "/" + NAME + "_vivid_steps.json");
      f.outTextLn(JSON.stringify(doc, null, 2));
      f.close();
   } catch (e) { console.criticalln("steps json write failed: " + e); }
}

function main() {
   console.show();
   log("blend vivid: " + MASTER);
   if (!File.exists(MASTER)) throw new Error("no stretched blend at " + MASTER);
   ensureDir(FINAL);
   var ws = ImageWindow.open(MASTER);
   if (!ws.length) throw new Error("could not open " + MASTER);
   for (var i = 1; i < ws.length; ++i) try { ws[i].forceClose(); } catch (e0) {}
   var w = ws[0];
   if (!w.mainView.image.isColor) {
      try { w.forceClose(); } catch (e1) {}
      throw new Error("the blend is not a color image");
   }
   var kw = null;
   try { kw = w.keywords; } catch (e2) { kw = null; }
   VIVID_SRC = { starless: w, stars: null };   // vividFinish closes it
   vividFinish(kw, true);
   var made = File.exists(FINAL + "/" + NAME + "_vivid.jpg");
   writeVividSteps(made ? "ok" : "failed");
   log(made ? "EXIT OK" : "ERROR: no " + NAME + "_vivid.jpg (see above)");
}

try {
   main();
} catch (e) {
   log("ERROR: " + e);
   writeVividSteps("failed");
}
