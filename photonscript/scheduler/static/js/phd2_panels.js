/* PhotonScript PHD2 panels (PS-103).
 *
 * One place for the PHD2 panel code: the Guiding tab (/guiding) renders every
 * section with it, and the System page shows a one-line status per panel
 * (PHD2.systemSummary) that links here. Each loader fills the element ids it
 * names and quietly does nothing when they are absent. Every action goes
 * through the existing /api/phd2/* routes:
 *   live        GET  /api/phd2/live                       (PS-103)
 *   audit       GET  /api/phd2/audit[?refresh=1], POST /api/phd2/audit/apply (PS-89)
 *   self-test   GET  /api/phd2/selftest?days=30, POST /api/phd2/selftest/run (PS-92)
 *   calibration GET  /api/phd2/calibration, POST /api/phd2/calibrate?mode= (PS-93)
 *   guard       GET  /api/phd2/guard, /api/phd2/hotpix, POST /api/phd2/hotpix/capture (PS-91)
 *   tuner       GET  /api/phd2/tuning                     (PS-90)
 *   guide log   GET  /api/phd2/analysis?date=&subs=false  (PS-88)
 */
(function () {
    "use strict";

    var state = {night: null, busy: {}};

    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v == null ? '-' : v).replace(/[&<>"]/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
        });
    }
    function num(v, d) {
        return (v == null || v === '' || isNaN(Number(v))) ? '-' : Number(v).toFixed(d);
    }
    function chip(status) {
        var s = String(status || '').toLowerCase();
        var cls = {fail: 'badge-fail', warn: 'badge-warn', pass: 'badge-pass'}[s] || 'badge-unknown';
        return '<span class="badge ' + cls + '">' + esc(status || '?') + '</span>';
    }
    async function getJSON(url) {
        var r = await fetch(url);
        return r.json();
    }
    // POST that always yields an object: refusals (409/400) carry {ok, note}.
    async function post(url, body) {
        var opt = {method: 'POST'};
        if (body !== undefined) {
            opt.headers = {'Content-Type': 'application/json'};
            opt.body = JSON.stringify(body);
        }
        var r = await fetch(url, opt);
        try { return await r.json(); } catch (e) { return {ok: false, note: 'HTTP ' + r.status}; }
    }
    function setText(id, txt) { var el = $(id); if (el) el.textContent = txt; }
    function stamp() { return new Date().toLocaleTimeString(); }

    // ---- 1. live PHD2 state --------------------------------------------------
    function tile(k, v, title) {
        return '<div class="g-stat"' + (title ? ' title="' + esc(title) + '"' : '') +
            '><span class="k">' + esc(k) + '</span><b>' + v + '</b></div>';
    }
    async function loadLive() {
        var box = $('liveInfo');
        if (!box) return;
        try {
            var d = await getJSON('/api/phd2/live');
            if (d.night) state.night = d.night;
            var g = d.guiding || {}, p = d.phd2 || {}, ops = d.phd2_ops || {};
            var px = g.units === 'px' || g.rms_total_arcsec == null;
            var u = px ? ' px' : '"';
            // samples = guide steps in the agent's RMS window: none yet means
            // the snapshot holds model defaults, not measurements
            var have = g.samples > 0;
            var rms = !have ? [null, null, null] : px ? [g.rms_ra_px, g.rms_dec_px, g.rms_total_px]
                         : [g.rms_ra_arcsec, g.rms_dec_arcsec, g.rms_total_arcsec];
            var exp = (p.ok && p.exposure_ms != null) ? p.exposure_ms
                : (have && g.guide_camera_exposure != null ? Math.round(g.guide_camera_exposure * 1000) : null);
            var bin = (p.ok && p.binning != null) ? p.binning : g.guide_binning;
            var scale = (p.ok && p.pixel_scale != null) ? p.pixel_scale : g.pixel_scale_arcsec;
            var lock = (p.ok && Array.isArray(p.lock_position))
                ? num(p.lock_position[0], 1) + ', ' + num(p.lock_position[1], 1) : 'none';
            var app = p.ok ? esc(p.app_state) : '<span class="g-dim">not reachable</span>';
            box.innerHTML = '<div class="g-stats">' +
                tile('PHD2 app state', app, p.ok ? 'read from PHD2 ' + (p.t_utc || '') : (p.note || 'no PHD2 read')) +
                tile('Agent guide state', esc(g.state), 'the RC16 agent\'s last PHD2 snapshot') +
                tile('RMS RA / Dec', num(rms[0], 2) + ' / ' + num(rms[1], 2) + u,
                     g.samples + ' guide steps in the window') +
                tile('RMS total', num(rms[2], 2) + u, px ? 'guide-camera pixels: pixel scale unknown' : 'arcsec') +
                tile('Star SNR', have ? num(g.snr, 1) : '-') +
                tile('Star HFD', num(g.hfd_px, 2) + ' px' + (g.saturated ? ' <span class="badge badge-warn">clipped</span>' : '')) +
                tile('Exposure', exp != null ? esc(exp) + ' ms' : '-') +
                tile('Binning', esc(bin)) +
                tile('Pixel scale', num(scale, 3) + '"/px', 'source: ' + (p.ok && p.pixel_scale != null ? 'PHD2' : (g.scale_source || '?'))) +
                tile('Lock position', esc(lock)) +
                tile('PHD2 held by', esc(ops.owner || 'nobody') + (ops.held_s != null ? ' <span class="g-dim">' + esc(ops.held_s) + ' s</span>' : ''),
                     'the phd2_ops lock: self-test, hot-pixel map, calibration manager, audit apply') +
                tile('Armer', esc(d.armer_state || '-') + (d.target ? ' <span class="g-dim">' + esc(d.target) +
                     (d.filter ? ' ' + esc(d.filter) : '') + '</span>' : '')) +
                '</div>' +
                (g.scale_warning ? '<div class="warn" style="margin-top:.4rem;">' + esc(g.scale_warning) + '</div>' : '') +
                (!p.ok && p.note ? '<div class="g-dim" style="margin-top:.4rem;">PHD2: ' + esc(p.note) +
                    ' (agent values above are its last snapshot)</div>' : '');
            setText('liveStatus', 'night ' + (d.night || '?') + ', updated ' + stamp());
        } catch (e) { box.textContent = 'error: ' + e; }
    }

    // ---- 2. PS-89 settings audit --------------------------------------------
    function renderAudit(a) {
        var c = a.counts || {};
        setHTML('auditInfo', esc(a.title) + ' | ' + esc(a.reason) + ' at ' + esc(a.t_utc) + (a.cached ? ' (cached)' : '') +
            ' | <b>' + esc(c.fail) + ' fail</b>, ' + esc(c.warn) + ' warn, ' + esc(c.pass) + ' pass, ' + esc(c.unknown) + ' unknown' +
            ' | PHD2 ' + esc(a.phd2_state || 'not reachable') + ', pe_owner ' + esc(a.pe_owner) + ', profile writes ' + (a.autofix ? 'ON' : 'off') +
            (a.desired_error ? '<br><span class="badge badge-fail">desired file: ' + esc(a.desired_error) + '</span>' : ''));
        var groups = {}, order = [];
        (a.rows || []).forEach(function (r) {
            if (!groups[r.group]) { groups[r.group] = []; order.push(r.group); }
            groups[r.group].push(r);
        });
        var html = '';
        order.forEach(function (g) {
            html += '<h3 class="g-group">' + esc(g) + '</h3><table class="g-tbl"><tr><th></th><th>Setting</th>' +
                '<th>Current (source)</th><th>Desired</th><th>Why</th><th>Fix</th><th></th></tr>';
            groups[g].forEach(function (r) {
                var btn = r.applicable ? '<button class="btn btn-secondary btn-sm" data-apply="' + esc(r.id) +
                    '" title="dry run, then apply ' + esc(r.target) + ' (' + esc(r.apply) + ')">Apply</button>' : '';
                html += '<tr><td>' + chip(r.status) + '</td><td>' + esc(r.label) + '</td><td>' + esc(r.current) +
                    ' <span class="g-dim">(' + esc(r.source) + ')</span></td><td>' + esc(r.desired) +
                    '</td><td class="g-why">' + esc(r.why) + '</td><td>' + esc(r.note || (r.status === 'pass' ? '' : r.fix)) +
                    '</td><td>' + btn + '</td></tr>';
            });
            html += '</table>';
        });
        setHTML('auditRows', html);
        Array.prototype.forEach.call(document.querySelectorAll('#auditRows [data-apply]'), function (b) {
            b.onclick = function () { applyAudit(b.getAttribute('data-apply')); };
        });
    }
    function setHTML(id, html) { var el = $(id); if (el) el.innerHTML = html; }
    async function loadAudit(refresh) {
        if (!$('auditInfo')) return;
        if (refresh) setText('auditStatus', ' auditing...');
        try {
            renderAudit(await getJSON('/api/phd2/audit' + (refresh ? '?refresh=1' : '')));
            if (refresh) setText('auditStatus', ' done ' + stamp());
        } catch (e) { setText('auditInfo', 'error: ' + e); }
    }
    function fmtResults(r) {
        return (r.results || []).map(function (x) {
            return x.id + ': ' + (x.ok ? 'ok' : 'not done') + (x.note ? ' (' + x.note + ')' : '');
        }).join('; ') || (r.note || 'no result');
    }
    // Dry run first; only a clean dry run offers the real apply.
    async function applyAudit(id) {
        state.busy.audit = true;
        try {
            setText('auditStatus', ' dry run for ' + id + '...');
            var dry = await post('/api/phd2/audit/apply', {ids: [id], dry_run: true});
            var lines = fmtResults(dry);
            setText('auditStatus', ' dry run: ' + lines);
            if (!dry.ok) return;
            if (!confirm('Dry run for ' + id + ':\n' + lines + '\n\nApply it to PHD2 now?')) return;
            setText('auditStatus', ' applying ' + id + '...');
            var r = await post('/api/phd2/audit/apply', {ids: [id], dry_run: false});
            setText('auditStatus', ' applied: ' + fmtResults(r));
            await loadAudit(true);
        } catch (e) {
            setText('auditStatus', ' error: ' + e);
        } finally { state.busy.audit = false; }
    }

    // ---- 3. PS-92 pulse self-test -------------------------------------------
    var DIRS = ['W', 'E', 'N', 'S'];
    async function loadSelftest() {
        if (!$('stLast')) return;
        try {
            var t = await getJSON('/api/phd2/selftest?days=30');
            var last = (t.results || []).filter(function (r) {
                return (r.kind || 'active') === 'active' && r.verdict !== 'SKIPPED';
            }).pop();
            setHTML('stLast', last ?
                'Last: ' + chip(last.verdict) + ' ' + esc(last.t_utc) + ' (' + esc(last.context) + ', pier ' + esc(last.pier_side) +
                ', Dec ' + esc(last.dec_deg) + ')<br>Ratio of expected: ' + DIRS.map(function (d) {
                    return d + ' <b>' + esc(((last.directions || {})[d] || {}).ratio) + '</b>';
                }).join(', ') + ((last.reasons || []).length ? '<br>' + esc(last.reasons.join('; ')) : '') :
                'No self-test in the last 30 nights.');
            var rows = (t.nights || []).map(function (n) {
                return '<tr><td>' + esc(n.night) + '</td><td>' + chip(n.verdict) + '</td><td>' + esc(n.pier_side) +
                    '</td><td>' + DIRS.map(function (d) { return esc((n.ratios || {})[d]); }).join(' / ') +
                    '</td><td>' + esc((n.reasons || []).join('; ')) + '</td></tr>';
            }).join('');
            setHTML('stTrend', rows ? '<table class="g-tbl"><tr><th>Night</th><th>Worst</th><th>Pier</th>' +
                '<th>W / E / N / S ratio</th><th>Why</th></tr>' + rows + '</table>' : '');
        } catch (e) { setText('stLast', 'error: ' + e); }
    }
    async function runSelftest() {
        if (!confirm('Run the pulse self-test now? The scope must be tracking on a star field and PHD2 not guiding.')) return;
        state.busy.selftest = true;
        setText('stStatus', ' running (1.5 to 4 min)...');
        try {
            var r = await post('/api/phd2/selftest/run');
            setText('stStatus', ' ' + (r.verdict || (r.ok === false ? 'not run' : '?')) + ': ' +
                ((r.reasons || []).join('; ') || r.note || ''));
        } catch (e) { setText('stStatus', ' error: ' + e); }
        state.busy.selftest = false;
        loadSelftest();
    }

    // ---- 4. PS-93 calibration -----------------------------------------------
    async function loadCalibration() {
        if (!$('pcInfo')) return;
        try {
            var c = await getJSON('/api/phd2/calibration');
            var r = c.record;
            var flip = Object.keys(c.flip || {}).map(function (p) {
                return p + ' ' + (c.flip[p].ok ? 'verified' : 'RUNAWAY');
            }).join(', ');
            setHTML('pcInfo', 'Mode <b>' + esc(c.mode) + '</b> | ' + (r ?
                'On record: ' + chip(r.grade) + ' ' + esc(r.t_utc) + ' (' + esc(r.source) + ', Dec ' + esc(r.dec_deg) +
                ', HA ' + esc(r.ha_hr) + ' h, pier ' + esc(r.pier_side) + ', ortho ' + esc(r.ortho_err_deg) + ' deg, ' +
                esc(c.age_days) + ' d old)' + (c.stale ? ' <span class="badge badge-warn">stale</span>' : '') +
                (((r.reasons || []).concat(r.warnings || [])).length ? '<br>' + esc((r.reasons || []).concat(r.warnings || []).join('; ')) : '') :
                'no calibration on record') +
                '<br>Recommended PHD2 Calibration Step: ' + (c.recommended_step_ms ? 'about <b>' + esc(c.recommended_step_ms) + ' ms</b>' : '-') +
                '<br>After the flip: ' + (flip ? esc(flip) : 'not checked yet') +
                (c.plan ? '<br>Plan: ' + esc(c.plan.status) + ' at ' + esc((c.plan.field || {}).name) + ' (' + esc(c.plan.reason) + ')' : '') +
                (c.request ? '<br>Requested: ' + esc(c.request.mode) + ' at ' + esc(c.request.t_utc) : ''));
            var rows = (c.history || []).slice(-15).reverse().map(function (h) {
                return '<tr><td>' + esc(h.t_utc) + '</td><td>' + chip(h.grade) + '</td><td>' + esc(h.context) +
                    '</td><td>' + esc(h.dec_deg) + ' / ' + esc(h.ha_hr) + '</td><td>' + esc(h.pier_side) + '</td><td>' + esc(h.ortho_err_deg) +
                    '</td><td>' + esc((h.reasons || []).concat(h.warnings || []).join('; ')) + '</td></tr>';
            }).join('');
            setHTML('pcHist', rows ? '<table class="g-tbl"><tr><th>UTC</th><th>Grade</th><th>Context</th><th>Dec / HA</th>' +
                '<th>Pier</th><th>Ortho deg</th><th>Why</th></tr>' + rows + '</table>' : '');
        } catch (e) { setText('pcInfo', 'error: ' + e); }
    }
    async function askCalibration(mode) {
        if (mode === 'now' && !confirm('Calibrate PHD2 now? While a night runs this re-dispatches the rest of the night with a calibration slot; otherwise it sends a calibration sequence to NINA (roof open, dark sky only).')) return;
        try {
            var r = await post('/api/phd2/calibrate?mode=' + mode);
            setText('pcStatus', ' ' + (r.ok ? 'ok' : 'not done') + ': ' + (r.note || ''));
        } catch (e) { setText('pcStatus', ' error: ' + e); }
        loadCalibration();
    }

    // ---- 5. PS-91 guide guard + hot-pixel map -------------------------------
    async function loadGuard() {
        if (!$('guardInfo')) return;
        try {
            var both = await Promise.all([getJSON('/api/phd2/guard'), getJSON('/api/phd2/hotpix')]);
            var g = both[0], h = both[1];
            setHTML('guardInfo', 'Guard ' + (g.enabled ? 'ON' : 'OFF') + ' | ' +
                (g.auto_recover ? 'auto-recover ON' : 'observe-only') +
                ' | PHD2 held by: ' + esc((g.phd2_ops || {}).owner || 'nobody') +
                '<br>Hot-pixel map: ' + (h.exists ? '<b>' + esc(h.count) + '</b> pixels, <b>' + esc(h.age_days) +
                ' d</b> old (bin ' + esc(h.binning) + ', ' + esc(h.exposure_ms) + ' ms, ' + esc(h.camera) + ')' : 'none yet') +
                (h.stale ? ' <span class="badge badge-warn">needs refresh: ' + esc(h.stale) + '</span>' : ''));
            var rows = (g.list || []).map(function (e) {
                var rec = (e.recoveries || []).map(function (r) {
                    return (r.ok === true ? 'ok' : r.ok === false ? 'FAILED' : 'skipped') + ': ' + esc(r.detail);
                }).join('; ');
                return '<tr><td>' + esc((e.start_utc || '').slice(11, 16)) + '-' + esc((e.end_utc || 'open').slice(11, 16)) +
                    'Z</td><td>' + esc(e.kind) + '</td><td>' + esc((e.codes || []).join(',')) + '</td><td>' + esc(e.detail) +
                    '</td><td>' + (rec || (e.observe_only ? 'observe-only' : '-')) + '</td></tr>';
            }).join('');
            setHTML('guardOut', '<b>' + esc(g.date) + ':</b> ' + esc(g.episodes) + ' episode(s)' +
                (g.episodes ? ', ' + esc(g.closed_minutes) + ' min' : '') +
                (rows ? '<table class="g-tbl"><tr><th>UTC</th><th>Kind</th><th>Detectors</th><th>Detail</th><th>Recovery</th></tr>' +
                    rows + '</table>' : ''));
        } catch (e) { setText('guardInfo', 'error: ' + e); }
    }
    async function captureHotpix() {
        if (!confirm('Build the guide-camera hot-pixel map now? Only with the roof closed or the guide camera capped.')) return;
        state.busy.guard = true;
        setText('guardStatus', ' capturing (about a minute)...');
        try {
            // refusals (night running, PHD2 guiding, PHD2 busy) come back as {ok:false, note}
            var r = await post('/api/phd2/hotpix/capture');
            setText('guardStatus', r.ok ? ' done: ' + r.count + ' hot pixels.' : ' refused: ' + (r.note || r.detail || '?'));
        } catch (e) { setText('guardStatus', ' error: ' + e); }
        state.busy.guard = false;
        loadGuard();
    }

    // ---- 6. PS-90 guide-star tuner ------------------------------------------
    async function loadTuning() {
        if (!$('tuneInfo')) return;
        try {
            var t = await getJSON('/api/phd2/tuning');
            var l = t.last || {}, rc = t.recommend || {}, b3 = rc.bin3 || {};
            var lc = (t.changes || []).slice(-1)[0];
            var lw = l.decision || l.would;
            setHTML('tuneInfo', 'Mode <b>' + esc(t.mode) + '</b> | night ' + esc(t.date) + ', ' + esc(t.measurements) + ' measurements' +
                (t.last ? '<br>Last ' + esc(l.t_utc) + ': ' + esc(l.target) + ' ' + esc(l.filter) + ', ' + esc(l.exposure_ms) + ' ms, peak ' +
                    esc(l.peak_frac != null ? Math.round(l.peak_frac * 1000) / 10 : null) + '%, SNR ' + esc(l.snr) + ', HFD ' + esc(l.hfd_px) + ' px' +
                    (l.clipped ? ' <span class="badge badge-warn">clipped</span>' : '') + (l.bit8 ? ' <span class="badge badge-fail">8-bit</span>' : '') +
                    (lw ? '<br>' + (l.decision ? 'Decision' : 'Would') + ': ' + esc(lw.reason) : '') : '') +
                '<br>Last change: ' + (lc ? esc(lc.t_utc) + ' ' + esc(lc.kind) + ' ' + esc(lc.from) + ' -> ' + esc(lc.to) + ' (' + esc(lc.reason) + ')' : 'none tonight') +
                '<br>Next night: ' + esc(rc.note) + (rc.change ? ' <span class="badge badge-warn">gain ' + esc(rc.gain) + '</span>' : '') +
                '<br>Bin 3: ' + esc(b3.note || 'no verdict yet'));
            var rows = Object.keys(t.by_filter || {}).map(function (f) {
                var v = t.by_filter[f];
                return '<tr><td>' + esc(f) + '</td><td>' + esc(v.measurements) + '</td><td>' + esc(v.exposure_ms) + '</td><td>' +
                    esc(v.peak_pct) + '</td><td>' + esc(v.snr) + '</td><td>' + esc(v.hfd_px) + '</td><td>' + esc(v.clipped) +
                    '</td><td>' + esc(v.in_band_pct) + '</td></tr>';
            }).join('');
            setHTML('tuneRows', rows ? '<table class="g-tbl"><tr><th>Filter</th><th>n</th><th>Exposure ms</th><th>Peak %</th>' +
                '<th>SNR</th><th>HFD px</th><th>Clipped</th><th>In band %</th></tr>' + rows + '</table>' : '');
        } catch (e) { setText('tuneInfo', 'error: ' + e); }
    }

    // ---- 7. PS-88 tonight's guide-log summary -------------------------------
    var SEV = {critical: 'badge-fail', warning: 'badge-warn', info: 'badge-unknown'};
    async function loadGuideLog() {
        var box = $('glogInfo');
        if (!box) return;
        var night = state.night || box.getAttribute('data-night') || '';
        var q = 'date=' + encodeURIComponent(night);
        var links = ' <a href="/runs/' + encodeURIComponent(night) + '">night report</a> | ' +
            '<a href="/api/phd2/analysis?' + q + '" target="_blank">full analysis (JSON)</a> | ' +
            '<a href="/api/phd2/log?' + q + '" target="_blank">raw guide log</a>';
        box.textContent = 'reading the guide log for ' + night + '...';
        try {
            var a = await getJSON('/api/phd2/analysis?' + q + '&subs=false');
            if (!a.ok) {
                box.innerHTML = esc(a.note || 'no PHD2 guide log for this night') + ' |' + links;
                setHTML('glogFindings', '');
                return;
            }
            var t = a.totals || {};
            box.innerHTML = '<b>' + esc(night) + ':</b> ' + esc(t.guiding_sessions) + ' sessions, ' + esc(t.guided_minutes) +
                ' min guided | RMS ' + esc(t.rms_total_arcsec) + '" (RA ' + esc(t.rms_ra_arcsec) + ', Dec ' + esc(t.rms_dec_arcsec) +
                '; settled ' + esc(t.settled_rms_total_arcsec) + '") | ' + esc(t.dropped_pct) + '% frames dropped, ' +
                esc(t.saturated_pct) + '% saturated | dithers back on lock ' + esc(t.dithers_recovered) + '/' + esc(t.dithers) +
                ' |' + links;
            setHTML('glogFindings', (a.findings || []).slice(0, 5).map(function (f) {
                return '<div class="g-finding"><span class="badge ' + (SEV[f.severity] || 'badge-unknown') + '">' +
                    esc(String(f.severity || '').toUpperCase()) + '</span> <b>' + esc(f.title) + '</b> ' + esc(f.detail) +
                    '<br><span class="g-dim">Fix: ' + esc(f.recommendation) + '</span></div>';
            }).join(''));
        } catch (e) { box.textContent = 'error: ' + e; }
    }

    // ---- System page: one line per panel, linking to the Guiding tab --------
    async function systemSummary(id) {
        var box = $(id);
        if (!box) return;
        function line(anchor, name, body) {
            return '<div class="g-sumline"><a href="/guiding#' + anchor + '">' + esc(name) + '</a>: ' + body + '</div>';
        }
        async function safe(url) { try { return await getJSON(url); } catch (e) { return null; } }
        var r = await Promise.all([safe('/api/phd2/audit'), safe('/api/phd2/selftest'), safe('/api/phd2/calibration'),
                                   safe('/api/phd2/guard'), safe('/api/phd2/hotpix'), safe('/api/phd2/tuning')]);
        var a = r[0], s = r[1], c = r[2], g = r[3], h = r[4], t = r[5];
        var na = '<span class="g-dim">unavailable</span>';
        var ac = (a && a.counts) || {};
        var rec = c && c.record;
        box.innerHTML =
            line('auditSec', 'Settings audit (PS-89)', a ? (ac.fail ? chip('FAIL') : ac.warn ? chip('WARN') : chip('PASS')) + ' ' +
                esc(ac.fail) + ' fail, ' + esc(ac.warn) + ' warn, ' + esc(ac.unknown) + ' unknown (' + esc(a.reason) + ' ' + esc(a.t_utc) + ')' : na) +
            line('selftestSec', 'Pulse self-test (PS-92)', s ? (s.verdict ? chip(s.verdict) + ' tonight, ' + esc(s.runs) + ' run(s)' : 'not run tonight') : na) +
            line('calSec', 'Calibration (PS-93)', c ? (rec ? chip(rec.grade) + ' ' + esc(c.age_days) + ' d old' +
                (c.stale ? ' <span class="badge badge-warn">stale</span>' : '') : 'none on record') + ', mode ' + esc(c.mode) : na) +
            line('guardSec', 'Guide guard (PS-91)', g ? (g.enabled ? 'ON' : 'OFF') + ', ' + esc(g.episodes) + ' episode(s) tonight; hot-pixel map ' +
                (h && h.exists ? esc(h.count) + ' px, ' + esc(h.age_days) + ' d old' : 'none') +
                (h && h.stale ? ' <span class="badge badge-warn">refresh</span>' : '') : na) +
            line('tuneSec', 'Guide star tuner (PS-90)', t ? 'mode ' + esc(t.mode) + ', ' + esc(t.measurements) + ' measurement(s) tonight' : na);
    }

    // ---- Guiding tab wiring + auto refresh ----------------------------------
    function on(id, fn) { var el = $(id); if (el) el.onclick = fn; }
    function initGuidingPage() {
        var gl = $('glogInfo');
        if (gl) state.night = gl.getAttribute('data-night') || null;
        on('liveRefresh', loadLive);
        on('auditRefresh', function () { loadAudit(true); });
        on('stRefresh', loadSelftest);
        on('stRun', runSelftest);
        on('pcRefresh', loadCalibration);
        on('pcNext', function () { askCalibration('next'); });
        on('pcNow', function () { askCalibration('now'); });
        on('guardRefresh', loadGuard);
        on('hotpixCapture', captureHotpix);
        on('tuneRefresh', loadTuning);
        on('glogRefresh', loadGuideLog);
        loadLive().then(loadGuideLog);
        loadAudit(false);
        loadSelftest();
        loadCalibration();
        loadGuard();
        loadTuning();
        // live state every 15 s, the stored records every 60 s; nothing while
        // the tab is hidden or while that section's action is in flight. The
        // guide-log analysis reads whole log files: on load and on Refresh only.
        setInterval(function () { if (!document.hidden) loadLive(); }, 15000);
        setInterval(function () {
            if (document.hidden) return;
            if (!state.busy.audit) loadAudit(false);
            if (!state.busy.selftest) loadSelftest();
            loadCalibration();
            if (!state.busy.guard) loadGuard();
            loadTuning();
        }, 60000);
    }

    window.PHD2 = {
        esc: esc, initGuidingPage: initGuidingPage, systemSummary: systemSummary,
        loadLive: loadLive, loadAudit: loadAudit, loadSelftest: loadSelftest,
        loadCalibration: loadCalibration, loadGuard: loadGuard, loadTuning: loadTuning,
        loadGuideLog: loadGuideLog
    };
})();
