"""Text-level guards for the OSC finish pipeline (deploy/finish_osc.js, a PJSR
script, and deploy/run-finish-osc.ps1). Neither PixInsight nor PowerShell runs
in CI, so these check the rules the scripts depend on: pure ASCII, PJSR
comment and include rules, every optional step guarded, and every placeholder
the script uses filled in by the launcher."""

from __future__ import annotations

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def _read(name: str) -> str:
    return (DEPLOY / name).read_bytes().decode("ascii")   # also: pure ASCII


JS = "finish_osc.js"
PS1 = "run-finish-osc.ps1"

# Processes / scripts that may be absent from a PixInsight install (third-party
# modules, separately installed databases, or newer-version processes).
OPTIONAL = [
    "GradientCorrection",
    "ImageSolver",
    "SpectrophotometricColorCalibration",
    "Gaia",
    "BlurXTerminator",
    "NoiseXTerminator",
    "MultiscaleLinearTransform",
    "StarNet2",
    "StarXTerminator",
    "ExternalProcess",
]


def _strip_line_comments(src: str) -> str:
    return "\n".join("" if ln.lstrip().startswith("//") else ln for ln in src.splitlines())


def _functions(src: str) -> dict[str, tuple[int, int]]:
    """name -> (start, end) offsets of each top-level `function name(...) {...}`."""
    out = {}
    for m in re.finditer(r"^function (\w+)\s*\([^)]*\)\s*\{", src, re.M):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        out[m.group(1)] = (m.start(), i)
    return out


def _in_try(src: str, pos: int, start: int) -> bool:
    """True if offset pos sits inside an open `try {` block that opened after start."""
    stack = []
    i = start
    while i < pos:
        if src.startswith("try", i) and re.match(r"try\s*\{", src[i:]) and not src[i - 1].isalnum():
            j = src.index("{", i)
            stack.append("try")
            i = j + 1
            continue
        c = src[i]
        if c == "{":
            stack.append("other")
        elif c == "}" and stack:
            stack.pop()
        i += 1
    return "try" in stack


def _guarded(src: str, pos: int, funcs: dict[str, tuple[int, int]], depth: int = 0) -> bool:
    owner = next((n for n, (a, b) in funcs.items() if a <= pos < b), None)
    if owner is None:
        return _in_try(src, pos, 0)
    if _in_try(src, pos, funcs[owner][0]):
        return True
    if depth >= 2:
        return False
    # Unguarded inside its function: every call site must be guarded instead.
    calls = [m.start() for m in re.finditer(r"(?<![\w.])" + owner + r"\(", src)
             if not src[max(0, m.start() - 9):m.start()].endswith("function ")]
    return bool(calls) and all(_guarded(src, c, funcs, depth + 1) for c in calls)


def test_js_is_ascii_and_has_no_block_comment_opener_in_line_comments():
    s = _read(JS)
    for n, ln in enumerate(s.splitlines(), 1):
        if "//" in ln:
            assert "/*" not in ln.split("//", 1)[1], f"line {n}: slash-star inside a // comment"
    assert chr(0x2014) not in s   # no em dashes


def test_ps1_is_ascii():
    _read(PS1)


def test_pjsr_includes_precede_the_solver_include():
    s = _read(JS)
    first_pjsr = s.index("#include <pjsr/DataType.jsh>")
    assert first_pjsr < s.index("//__SOLVER_INCLUDE__")
    assert "#include <pjsr/UndoFlag.jsh>" in s   # recoverCore uses UndoFlag_NoSwapFile


def test_every_placeholder_is_filled_by_the_launcher():
    js = _read(JS)
    ps1 = _read(PS1)
    used = set(re.findall(r"__[A-Z][A-Z0-9_]*__", _strip_line_comments(js)))
    filled = set(re.findall(r"Replace\('(__[A-Z][A-Z0-9_]*__)'", ps1))
    assert used, "no placeholders found"
    assert used - filled == set(), f"unfilled placeholders: {sorted(used - filled)}"
    assert "Replace('//__SOLVER_INCLUDE__'" in ps1


def test_every_optional_process_is_detected_before_use():
    s = _read(JS)
    for p in OPTIONAL:
        if p == "ExternalProcess":   # core PJSR object; GRAXPERT != "" is the guard
            assert "if (GRAXPERT)" in s
            continue
        if re.search(r"new " + p + r"\b", s):
            assert (f'typeof {p} !== "undefined"' in s or f"typeof {p} === \"undefined\"" in s
                    or f'typeof {p} === "function"' in s), p


def test_every_optional_process_runs_inside_a_try():
    s = _strip_line_comments(_read(JS))
    funcs = _functions(s)
    for p in OPTIONAL:
        for m in re.finditer(r"new " + p + r"\b", s):
            assert _guarded(s, m.start(), funcs), f"unguarded: new {p} at offset {m.start()}"


def test_main_run_is_wrapped_and_always_writes_log_and_steps():
    s = _read(JS)
    assert re.search(r'try \{ main\(\); writeSteps\("ok"\); \}', s)
    assert 'writeSteps("error: "' in s
    # one master (run-finish-osc.ps1) or every master of an integrate run;
    # EXIT OK only when all of them finished
    assert "var RUN = MASTERS ? MASTERS : [{ name: NAME, path: MASTER }];" in s
    assert 'log(NFAIL ? ("EXIT WITH " + NFAIL + " FAILED MASTER(S)") : "EXIT OK");' in s


def test_astrometric_solution_cleared_before_every_crop():
    s = _strip_line_comments(_read(JS))
    funcs = _functions(s)
    for m in re.finditer(r"new Crop\b", s):
        name, (a, _b) = next((n, ab) for n, ab in funcs.items() if ab[0] <= m.start() < ab[1])
        body = s[a:m.start()]
        assert "clearWcs(view)" in body, f"{name}: Crop without clearing the WCS first"
    assert "clearAstrometricSolution()" in s


# --- PS-46: SPCC with Gaia DR3/SP -------------------------------------------

def test_spcc_uses_gaia_dr3sp_and_probes_the_database():
    s = _read(JS)
    assert "Gaia.prototype.DataRelease_3_SP" in s
    assert 'G.command = "get-info"' in s and "G.isValid" in s
    assert 'SP.catalogId = "GaiaDR3SP"' in s
    assert "color: SPCC applied" in s                 # the ticket's log line
    assert 'step("color", "SPCC", "ran"' in s
    assert 'step("color", "BN+CC", "fallback"' in s   # fallback is logged as such
    assert "-> BN + ColorCalibration fallback" in s


def test_spcc_curves_come_from_the_pixinsight_library():
    s = _read(JS)
    for prop in ("deviceQECurve", "redFilterTrCurve", "greenFilterTrCurve",
                 "blueFilterTrCurve", "whiteReferenceSpectrum"):
        assert f'"{prop}"' in s
    assert 'xspdCurve(c[2], c[3], c[4])' in s
    ps1 = _read(PS1)
    assert '[string]$SpccQE = "Sony IMX411/455/461/533/571"' in ps1
    assert '[string]$SpccRed = "Sony Color Sensor R-UVIRcut"' in ps1
    assert '[string]$SpccWhite = "Average Spiral Galaxy"' in ps1
    assert '$piLibrary = Join-Path $piRoot "library"' in ps1


def test_xspd_regex_matches_the_library_format():
    # Mirror of the JS regex, against the line shape PixInsight's filters.xspd uses.
    line = '<Filter name="Sony IMX411/455/461/533/571" channel="Q" data="402,0.7219,404,0.7367"/>'
    wref = '<WhiteRef name="Average Spiral Galaxy" default="W" data="200.5,0.0715,201.5,0.0689"/>'
    for tag, name, text, want in (("Filter", "Sony IMX411/455/461/533/571", line, "402,0.7219,404,0.7367"),
                                  ("WhiteRef", "Average Spiral Galaxy", wref, "200.5,0.0715,201.5,0.0689")):
        esc = re.sub(r"([.+?^${}()|\[\]\\*])", r"\\\1", name)
        m = re.search("<" + tag + r'\s+name="' + esc + r'"[^>]*?\sdata="([^"]*)"', text)
        assert m and m.group(1) == want


def test_ps1_color_params_and_defaults():
    ps1 = _read(PS1)
    assert "[ValidateSet('auto','spcc','basic')][string]$Color = 'auto'" in ps1
    assert "[double]$BgTarget = 0.12" in ps1
    assert "[double]$ShadowSigma = 2.0" in ps1
    assert "[int]$HdrLayers = 7" in ps1


def test_ps1_explicit_outdir_never_overwrites():
    ps1 = _read(PS1)
    assert "already has files - pick a new folder" in ps1
    assert '"--automation-mode", "--run=$runjs", "--force-exit"' in ps1
    assert "PixInsight is already running" in ps1


# --- PS-41: noise reduction + deconvolution ---------------------------------

def _func(s: str, name: str) -> str:
    a, b = _functions(s)[name]
    return s[a:b]


def test_deconv_then_denoise_on_the_linear_image_before_the_stretch():
    s = _read(JS)
    main = _func(s, "main")
    order = [main.index(x) for x in ("colorCalibrate(v, solved)", "_linear.xisf", "deconvolve(v)",
                                     "denoise(v)", "stretch(")]
    assert order == sorted(order)


def test_tool_order_rc_then_graxpert_then_builtin():
    s = _read(JS)
    d = _func(s, "deconvolve")
    assert d.index("BlurXTerminator") < d.index("graxpert(view") < d.index("no built-in fallback")
    n = _func(s, "denoise")
    assert n.index("NoiseXTerminator") < n.index('graxpert(view, "denoising"') < n.index("mltDenoise(view")
    assert 'typeof MultiscaleLinearTransform === "undefined"' in n
    assert "if (GRAXPERT)" in d and "if (GRAXPERT)" in n


def test_graxpert_cli_call_is_bounded_and_checked():
    g = _func(_read(JS), "graxpert")
    assert '"-cli", "-cmd", cmd, "-output", outBase, "-strength"' in g
    assert "GRAXPERT_TIMEOUT_MIN * 60000" in g and "P.kill()" in g
    assert "P.exitCode !== 0" in g
    assert "a.width !== b.width" in g          # size check before replacing pixels
    assert "not applied" in g                  # background sanity check
    assert "finally" in g and "removeQuiet(inF)" in g


def test_ps1_noise_and_deconv_params():
    ps1 = _read(PS1)
    assert "[double]$Denoise = 0.5" in ps1
    assert "[ValidateSet('on','off')][string]$Deconv = 'on'" in ps1
    assert "[double]$DeconvStrength = 0.5" in ps1
    assert '[string]$GraXpert = ""' in ps1
    assert "[switch]$NoGraXpert" in ps1
    assert "[int]$GraXpertTimeoutMin = 30" in ps1
    assert "$env:PS_GRAXPERT" in ps1
    assert "@('Denoise', $Denoise, 0, 1)" in ps1


def test_steps_record_tool_and_settings():
    s = _read(JS)
    for rec in ('step("deconvolution", "GraXpert", "ran"', 'step("deconvolution", "BlurXTerminator", "ran"',
                'step("noise_reduction", "GraXpert", "ran"', 'step("noise_reduction", "MLT", "ran"',
                'step("noise_reduction", "NoiseXTerminator", "ran"', 'step("deconvolution", "none", "skipped"'):
        assert rec in s, rec
    assert "version: GRAXPERT_VERSION" in s


# --- PS-40: star reduction ---------------------------------------------------

def test_star_reduction_after_denoise_and_falls_back_to_the_normal_stretch():
    main = _func(_read(JS), "main")
    assert main.index("denoise(v)") < main.index("starSplit(v)") < main.index("frameCrop(v")
    assert "if (!split || !starRecombine(v, split)) {" in main
    assert main.index("starRecombine(v, split)") < main.index("stretch(v)")


def test_starnet2_split_is_reversible_and_screen_blend_matches_the_ticket():
    s = _read(JS)
    split = _func(s, "starSplit")
    assert 'typeof StarNet2 !== "undefined"' in split
    pre = _func(s, "starlessPre")       # PS-186: StarXTerminator or StarNet2 on the pre-stretched copy
    assert "applyHT(w.mainView, 0, mPre)" in pre and "applyHT(w.mainView, 0, 1 - mPre)" in pre
    assert '"max(0, $T - " + sl.mainView.id + ")"' in split
    rec = _func(s, "starRecombine")
    assert '"~(~$T * ~(" + stv.id + " * " + k + "))"' in rec
    assert '_starless.xisf' in rec
    assert rec.index("stretch(slv") < rec.index("recoverCore(slv)") < rec.index("view.image.assign")
    assert 'step("star_reduction", split.tool, "ran"' in rec


def test_mtf_prestretch_is_exactly_invertible():
    # The split relies on mtf(1 - m, mtf(m, x)) == x (HistogramTransformation midtones).
    def mtf(m, x):
        return ((m - 1) * x) / ((2 * m - 1) * x - m)
    for m in (0.002, 0.05, 0.3):
        for x in (0.0001, 0.01, 0.2, 0.9):
            assert abs(mtf(1 - m, mtf(m, x)) - x) < 1e-9


def test_ps1_star_params():
    ps1 = _read(PS1)
    assert "[ValidateSet('on','off')][string]$StarReduction = 'on'" in ps1
    assert "[double]$StarStrength = 0.7" in ps1
    assert "@('StarStrength', $StarStrength, 0, 1)" in ps1
    assert 'StarNet2*.dll' in ps1


# --- PS-22 integrate uses this finish too: mono masters skip the color steps --

def test_mono_masters_skip_the_color_steps():
    s = _read(JS)
    for fn in ("colorCalibrate", "removeGreen", "recoverCore"):
        assert "!view.image.isColor" in _func(s, fn), fn
    assert "SAT_MID !== 0.5 && img.isColor" in _func(s, "stretch")


# --- gold-blue "HOO-style" palette for OSC finishes (finish_palette / --palette gold) --

def test_gold_palette_is_off_by_default_and_filled_by_the_launcher():
    s = _read(JS)
    assert 'var PALETTE  = "__PALETTE__";' in s and "var GOLD_MIX = __GOLD_MIX__;" in s
    ps1 = _read(PS1)
    assert "[ValidateSet('off','gold')][string]$Palette = 'off'" in ps1
    assert "[double]$GoldMix = 0.6" in ps1
    assert "Replace('__PALETTE__', $Palette)" in ps1 and "Replace('__GOLD_MIX__', (JsNum $GoldMix))" in ps1
    assert "@('GoldMix', $GoldMix, 0, 1)" in ps1


def test_gold_palette_copy_point_and_order_in_main():
    main = _func(_read(JS), "main")
    # same linear, color-calibrated copy point as HOO: after color, before SCNR
    assert main.index("colorCalibrate(v, solved)") < main.index("paletteSource(v)") < main.index("removeGreen(v)")
    # the natural image is saved first, the palette image after it
    assert main.index('"_final.jpg", "jpg")') < main.index("if (palWin) paletteFinish(palWin);")


def test_gold_palette_source_is_guarded():
    src = _func(_read(JS), "paletteSource")
    assert 'PALETTE !== "gold"' in src and "!view.image.isColor" in src
    assert 'step("palette", "PixelMath", "skipped"' in src
    assert src.index("try {") < src.index("dupWindow(view")
    assert 'step("palette", "PixelMath", "failed"' in src


def test_gold_palette_pixelmath_mapping():
    s = _read(JS)
    ex = _func(s, "goldExpressions")
    # Ha = R, OIII = k x mean(G, B) (the HOO extraction), R = Ha, G = mix, B = OIII
    assert '" * ($T[1] + $T[2]) / 2"' in ex and '"$T[0]"' in ex
    assert 'GOLD_MIX.toFixed(3) + " * $T[0] + " + (1 - GOLD_MIX).toFixed(3) + " * " + o' in ex
    assert "hooScale(" in _func(s, "hooFinish") and "hooScale(tv.image)" in _func(s, "paletteFinish")
    fin = _func(s, "paletteFinish")
    for x in ("PM.expression = ex[0];", "PM.expression1 = ex[1];", "PM.expression2 = ex[2];",
              "PM.useSingleExpression = false;"):
        assert x in fin, x


def test_gold_palette_starless_with_natural_stars_screened_back():
    fin = _func(_read(JS), "paletteFinish")
    assert 'USE_RC && rcInstalled("StarXTerminator")' in fin and 'typeof StarNet2 !== "undefined"' in fin
    assert "starlessPre(v, mPre, tools[t])" in fin
    assert '"max(0, $T - " + sl.mainView.id + ")"' in fin
    assert '"~(~$T * ~(" + stars.mainView.id + " * " + ks + "))"' in fin
    # mapping on the starless view, then stretch + saturation, hue curve, then the stars
    order = [fin.index(x) for x in ("var tv = sl ? sl.mainView : v;", "PM.executeOn(tv, false)",
                                    "stretch(tv,", "CT.H = GOLD_HUE", "applyHT(stars.mainView",
                                    "frameCrop(v, EDGE_FRAC)", '"_gold.xisf"', '"_gold.jpg", "gold jpg")')]
    assert order == sorted(order)
    assert 'st.stars = "in the image (no star split)"' in fin


def test_gold_palette_every_failure_is_logged_and_the_copies_closed():
    fin = _func(_read(JS), "paletteFinish")
    assert fin.index("try {") < fin.index("new PixelMath")
    assert 'step("palette", "PixelMath", "ran", st)' in fin
    assert 'step("palette", "PixelMath", "failed", st)' in fin
    assert "RC.nxt = keepNxt; RC.sxt = keepSxt;" in fin      # natural image's RC records kept
    for w in ("sl.forceClose()", "stars.forceClose()", "win.forceClose()"):
        assert w in fin, w
    assert "nxtDenoise(v)" in fin and 'rcInstalled("NoiseXTerminator")' in fin


def _gold_hue():
    s = _read(JS)
    m = re.search(r"^var GOLD_HUE = (\[.*\]);", s, re.M)
    import json as _json
    return _json.loads(m.group(1))


def test_gold_hue_curve_is_monotonic_and_moves_the_right_way():
    pts = _gold_hue()
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    assert pts[0] == [0, 0] and pts[-1] == [1, 1]
    assert xs == sorted(xs) and ys == sorted(ys)

    def f(h):
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if x0 <= h <= x1:
                return y0 + (y1 - y0) * (h - x0) / (x1 - x0)
    assert f(0.04) > 0.04 and f(0.15) < 0.15       # red-orange and yellow toward gold
    assert 0.5 < f(0.6) < 0.6                       # azure toward teal-blue


def test_gold_mapping_puts_ha_in_orange_and_oiii_in_blue():
    import colorsys
    mix = 0.6

    def rgb(ha, o):
        return ha, mix * ha + (1 - mix) * o, o
    h_ha = colorsys.rgb_to_hsv(*rgb(1.0, 0.0))[0] * 360
    h_o = colorsys.rgb_to_hsv(*rgb(0.0, 1.0))[0] * 360
    assert 20 < h_ha < 50            # orange-gold, not pure red
    assert 190 < h_o < 240           # blue
    assert rgb(0.3, 0.3) == (0.3, 0.3, 0.3)   # equal background stays neutral
