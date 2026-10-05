/* PhotonScript TheSky / TPoint panel (PS-104), the sibling of phd2_panels.js.
 *
 * Fills the Guiding tab's "TheSky / TPoint" section (#tpointSec). REPORT
 * ONLY: the only POST is the manual TPoint record, which PhotonScript keeps
 * in its own data folder. Routes (routers/thesky.py):
 *   audit      GET  /api/thesky/audit[?refresh=1]
 *   imagelink  GET  /api/thesky/imagelink-check[?refresh=1][&thesky=1]
 *   pointing   GET  /api/thesky/pointing?nights=14
 *   rotation   GET  /api/rotation/report?nights=14 (PS-97, routers/rotation.py)
 *   manual     GET  /api/thesky/manual, POST /api/thesky/manual
 * Row rendering follows PHD2's audit table (status, current, desired, why,
 * fix), without Apply buttons.
 */
(function () {
    "use strict";

    var busy = {};
    var esc = (window.PHD2 && PHD2.esc) || function (v) {
        return String(v == null ? '-' : v).replace(/[&<>"]/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
        });
    };
    function $(id) { return document.getElementById(id); }
    function setText(id, txt) { var el = $(id); if (el) el.textContent = txt; }
    function setHTML(id, html) { var el = $(id); if (el) el.innerHTML = html; }
    function stamp() { return new Date().toLocaleTimeString(); }
    function chip(status) {
        var s = String(status || '').toLowerCase();
        var cls = {fail: 'badge-fail', warn: 'badge-warn', pass: 'badge-pass'}[s] || 'badge-unknown';
        return '<span class="badge ' + cls + '">' + esc(status || '?') + '</span>';
    }
    async function getJSON(url) {
        var r = await fetch(url);
        try { return await r.json(); } catch (e) { return {ok: false, note: 'HTTP ' + r.status}; }
    }

    // ---- 1. the audit rows ---------------------------------------------------
    function renderAudit(a) {
        var c = a.counts || {}, rb = a.rebuild || {};
        var rbChip = rb.current === 'REBUILD' ? '<span class="badge badge-fail">REBUILD</span>' :
            rb.current === 'watch' ? '<span class="badge badge-warn">watch</span>' :
            rb.current === 'no' ? '<span class="badge badge-pass">no</span>' : chip('unknown');
        setHTML('tsInfo', esc(a.title) + ' | ' + esc(a.reason) + ' at ' + esc(a.t_utc) + (a.cached ? ' (cached)' : '') +
            ' | <b>' + esc(c.fail) + ' fail</b>, ' + esc(c.warn) + ' warn, ' + esc(c.pass) + ' pass, ' + esc(c.unknown) + ' unknown' +
            '<br>Rebuild the TPoint model: ' + rbChip + ((rb.reasons || []).length ? ' ' + esc(rb.reasons.join('; ')) : '') +
            (a.first_slew ? '<br>NINA first slew, 14-night median: <b>' + esc(a.first_slew) + '</b>' : '') +
            (a.desired_error ? '<br><span class="badge badge-fail">desired file: ' + esc(a.desired_error) + '</span>' : ''));
        var groups = {}, order = [];
        (a.rows || []).forEach(function (r) {
            if (!groups[r.group]) { groups[r.group] = []; order.push(r.group); }
            groups[r.group].push(r);
        });
        // PS-119: fails / warns keep a left border, pass / info rows fold
        // behind "N passing" (PHD2.passing from phd2_panels.js)
        var pp = (window.PHD2 && PHD2.passing) || null;
        var html = '', nOk = 0;
        order.forEach(function (g) {
            var allOk = pp && groups[g].every(function (r) { return pp.isOk(r.status); });
            html += '<div class="g-grp' + (allOk ? ' g-grp-ok' : '') + '"><h3 class="g-group">' + esc(g) +
                '</h3><table class="g-tbl"><tr><th></th><th>Setting</th>' +
                '<th>Current (source)</th><th>Desired</th><th>Why</th><th>Fix / note</th></tr>';
            groups[g].forEach(function (r) {
                if (pp && pp.isOk(r.status)) nOk += 1;
                html += '<tr id="ts-' + esc(r.id) + '" class="' + (pp ? pp.rowClass(r.status) : '') + '"><td>' +
                    chip(r.status) + '</td><td>' + esc(r.label) +
                    ' <span class="g-dim" title="how sure the read is on the site build">' + esc(r.confidence) + '</span></td><td>' +
                    esc(r.current) + ' <span class="g-dim">(' + esc(r.source_text || r.source) + ')</span></td><td>' + esc(r.desired) +
                    '</td><td class="g-why">' + esc(r.why) + '</td><td>' + esc(r.note || (r.status === 'pass' ? '' : r.fix)) +
                    (r.note && r.status !== 'pass' && r.status !== 'info' ? '<br><span class="g-dim">Fix: ' + esc(r.fix) + '</span>' : '') +
                    '</td></tr>';
            });
            html += '</table></div>';
        });
        setHTML('tsRows', html + (pp ? pp.html('tpointSec', nOk) : ''));
        if (pp) pp.wire('tsRows', 'tpointSec');
    }
    async function loadAudit(refresh) {
        if (!$('tsInfo')) return;
        busy.audit = true;
        if (refresh) setText('tsStatus', ' auditing (read only)...');
        try {
            renderAudit(await getJSON('/api/thesky/audit' + (refresh ? '?refresh=1' : '')));
            if (refresh) {
                setText('tsStatus', ' done ' + stamp());
                if (window.PHD2 && PHD2.loadAttention) PHD2.loadAttention();
            }
        } catch (e) { setText('tsInfo', 'error: ' + e); }
        busy.audit = false;
    }

    // ---- 2. the Image Link check ---------------------------------------------
    function renderImagelink(d) {
        if (!d || (!d.astap && !d.thesky)) {
            setHTML('tsImagelink', esc((d && d.note) || 'no Image Link check yet') +
                ' (ASTAP check now solves the newest RC16 L frame)');
            return;
        }
        var a = d.astap || {}, t = d.thesky, cmp = d.compare || {};
        var html = '<b>' + esc(d.file) + '</b> (' + esc(d.filter) + ', night ' + esc(d.night) + ', checked ' + esc(d.t_utc) + ')' +
            '<br>ASTAP: native ' + esc(a.native_scale) + '"/px (frame bin ' + esc(a.frame_bin) + '), angle ' + esc(a.pa) +
            ' deg, parity ' + (a.parity === -1 ? 'normal' : a.parity === 1 ? 'mirrored' : '-') +
            '<br>TheSky Automated Image Link scale ' + esc(cmp.ails_image_scale) + ' vs expected ' + esc(cmp.expected_ails_scale) +
            ' (native x bin ' + esc(cmp.run_binning) + ')' + (cmp.diff_pct != null ? ', ' + esc(cmp.diff_pct) + '%' : '');
        if (t) {
            html += '<br>TheSky Image Link on a copy: ' + (t.skipped ? '<span class="g-dim">' + esc(t.note) + '</span>' :
                (t.succeeded ? chip('PASS') + ' scale ' + esc(t.image_scale) + '"/px, RMS ' + esc(t.solution_rms) +
                    ', ' + esc(t.solution_stars) + ' stars matched of ' + esc(t.catalog_stars) :
                    chip('FAIL') + ' ' + esc([t.error_code ? 'error ' + t.error_code : '', t.error_text, t.exec_error, t.note]
                        .filter(function (x) { return x; }).join('; '))));
        }
        if (d.note) html += '<br><span class="g-dim">' + esc(d.note) + '</span>';
        setHTML('tsImagelink', html);
    }
    async function loadImagelink(mode) {
        if (!$('tsImagelink')) return;
        var url = '/api/thesky/imagelink-check';
        if (mode === 'astap') url += '?refresh=1';
        if (mode === 'thesky') {
            if (!confirm('Run TheSky\'s Image Link on a temporary copy of the newest RC16 L frame? ' +
                'It never touches the camera or the mount, and runs only while the armer is idle.')) return;
            url += '?thesky=1';
        }
        busy.imagelink = true;
        if (mode) setText('tsIlStatus', mode === 'thesky' ? ' TheSky is solving (up to a minute)...' : ' ASTAP is solving...');
        try {
            var d = await getJSON(url);
            if (mode && d.ok === false && !d.astap && d.note) setText('tsIlStatus', ' ' + d.note);
            else if (mode) setText('tsIlStatus', ' done ' + stamp());
            renderImagelink(d);
            if (mode) loadAudit(true);
        } catch (e) { setText('tsIlStatus', ' error: ' + e); }
        busy.imagelink = false;
    }

    // ---- 3. first-slew trend -------------------------------------------------
    function statRow(name, s) {
        s = s || {};
        return '<tr><td>' + esc(name) + '</td><td>' + esc(s.n) + '</td><td>' + esc(s.median_arcmin) + '</td><td>' +
            esc(s.p90_arcmin) + '</td><td>' + esc(s.median_east_arcmin) + ' / ' + esc(s.median_north_arcmin) +
            '</td><td>' + esc(s.median_attempts) + '</td></tr>';
    }
    async function loadPointing() {
        if (!$('tsPointing')) return;
        try {
            var p = await getJSON('/api/thesky/pointing?nights=14');
            if (!p.runs) {
                setHTML('tsPointing', p.logs_dir_found ? 'No NINA Center runs in the last 14 nights.' :
                    'NINA logs folder not found on this machine.');
                return;
            }
            var head = '<table class="g-tbl"><tr><th>Group</th><th>n</th><th>Median \'</th><th>p90 \'</th>' +
                '<th>Median E / N \'</th><th>Attempts</th></tr>';
            var html = head + statRow('All', p.overall);
            Object.keys(p.by_side || {}).forEach(function (k) {
                html += statRow((k === 'E' ? 'East' : 'West') + ' of meridian', p.by_side[k]);
            });
            ['by_dec', 'by_ha'].forEach(function (key) {
                Object.keys(p[key] || {}).forEach(function (k) {
                    if ((p[key][k] || {}).n) html += statRow(k, p[key][k]);
                });
            });
            html += '</table>';
            if ((p.side_src || []).join() === 'ha') {
                html += '<div class="g-dim">Side from the hour angle (the log names no pier side).</div>';
            }
            var ps = p.ps67 || {};
            html += '<div>PS-67 mount vs plate solve (after centering): ' +
                (ps.n ? 'median ' + esc(ps.median_arcmin) + '\' (n=' + esc(ps.n) + ')' : '<span class="g-dim">not on disk</span>') + '</div>';
            var rows = (p.per_night || []).map(function (n) {
                return '<tr><td>' + esc(n.night) + '</td><td>' + esc(n.n) + '</td><td>' + esc(n.median_arcmin) +
                    '</td><td>' + esc(n.p90_arcmin) + '</td><td>' + esc(n.ps67_median_arcmin) + '</td></tr>';
            }).join('');
            if (rows) html += '<table class="g-tbl"><tr><th>Night</th><th>n</th><th>Median \'</th><th>p90 \'</th>' +
                '<th>PS-67 median \'</th></tr>' + rows + '</table>';
            setHTML('tsPointing', html);
        } catch (e) { setText('tsPointing', 'error: ' + e); }
    }

    // ---- 3b. field rotation (PS-97) ------------------------------------------
    function rotStat(name, s) {
        s = s || {};
        if (!s.n) return '';
        return '<tr><td>' + esc(name) + '</td><td>' + esc(s.n) + '</td><td>' + esc(s.median_abs_rate_deg_h) +
            '</td><td>' + esc(s.median_envelope_deg_h) + '</td><td>' + esc(s.median_min_polar_arcmin) +
            '</td><td>' + esc(s.median_dec) + '</td></tr>';
    }
    function costCell(c) {
        c = c || {};
        return c.corner_px == null ? '-' : esc(c.corner_px) + ' px (' + esc(c.corner_arcsec) + '")';
    }
    function renderRotation(d) {
        var v = d.verdict || {}, p = d.polar || {};
        var lvl = {ok: 'PASS', warn: 'WARN'}[v.level] || 'unknown';
        var html = chip(lvl) + ' <b>' + esc(v.text) + '</b>' +
            '<div class="g-dim">Polar error ' + (p.source ? 'MA ' + esc(p.ma_arcmin) + '\' ME ' + esc(p.me_arcmin) +
                '\' = ' + esc(p.total_arcmin) + '\' (' + esc(p.source) + (p.model_date ? ', model ' + esc(p.model_date) : '') + ')'
                : 'not entered (TPoint record below)') +
            '; split ' + esc((d.split || {}).value) + '; verdict from ' + esc(d.main_era) + '</div>';
        var head = '<table class="g-tbl"><tr><th>Group</th><th>Blocks</th><th>Measured |deg/h|</th>' +
            '<th>Polar predicts up to</th><th>Needs polar error \'</th><th>Dec</th></tr>';
        ['before', 'after', 'all'].forEach(function (e) {
            var s = (d.eras || {})[e];
            if (!s || !s.n || (e === 'all' && (d.eras.before || d.eras.after))) return;
            var t = rotStat('All', s) + rotStat('RC16', (s.by_rig || {}).rc16) + rotStat('Piggy-600', (s.by_rig || {}).piggyback) +
                rotStat('Pier East', (s.by_pier || {}).East) + rotStat('Pier West', (s.by_pier || {}).West);
            ['by_dec', 'by_ha'].forEach(function (k) {
                Object.keys(s[k] || {}).forEach(function (b) { t += rotStat(b, s[k][b]); });
            });
            var a = s.rig_agreement || {}, f = s.polar_fit;
            html += '<h4>' + esc(e === 'all' ? 'All nights' : e === 'after' ? 'After the split' : 'Before the split') + '</h4>' +
                head + t + '</table><div class="g-dim">Rigs: ' +
                (a.n ? (a.agree ? 'agree' : 'DISAGREE') + ' (' + esc(a.n) + ' pairs, median diff ' + esc(a.median_abs_diff_deg_h) + ' deg/h)' : 'no overlapping pairs') +
                (f ? '; polar fit ' + esc(f.total_arcmin) + '\' (MA ' + esc(f.ma_arcmin) + ' ME ' + esc(f.me_arcmin) +
                    ', resid ' + esc(f.resid_rms_deg_h) + ' deg/h)' : '') + '</div>';
        });
        var rows = (d.blocks || []).filter(function (b) { return b.rate_deg_h != null; }).map(function (b) {
            return '<tr><td>' + esc(b.night) + '</td><td>' + esc(b.rig === 'piggyback' ? 'Piggy' : 'RC16') + '</td><td>' +
                esc(b.target) + '</td><td>' + esc(b.pier) + '</td><td>' + esc(b.dec) + '</td><td>' + esc(b.ha_start_h) +
                ' to ' + esc(b.ha_end_h) + '</td><td>' + esc(b.rate_deg_h) + ' +/- ' + esc(b.err_deg_h) +
                '</td><td>' + esc(b.resid_deg) + '</td><td>' + esc(b.pred_deg_h) + ' / ' + esc(b.pred_env_deg_h) +
                '</td><td>' + esc(b.source) + '</td></tr>';
        }).join('');
        if (rows) html += '<table class="g-tbl"><tr><th>Night</th><th>Rig</th><th>Target</th><th>Pier</th><th>Dec</th>' +
            '<th>HA h</th><th>deg/h</th><th>Resid deg</th><th>Predicted / max</th><th>From</th></tr>' + rows + '</table>';
        var cost = d.cost || {};
        var crow = Object.keys(cost).map(function (rig) {
            var c = cost[rig] || {}, m = c.measured || {}, pr = c.predicted || {};
            return '<tr><td>' + esc(rig === 'piggyback' ? 'Piggy-600' : 'RC16') + ' (' + esc(c.scale_arcsec) + '"/px)</td><td>' +
                esc(c.sub_s) + ' s: ' + costCell(m.sub) + '</td><td>' + esc(c.night_h) + ' h: ' + costCell(m.night) +
                '</td><td>' + costCell(pr.sub) + ' / ' + costCell(pr.night) + '</td></tr>';
        }).join('');
        if (crow) html += '<table class="g-tbl"><tr><th>Corner cost</th><th>Measured, one sub</th><th>Measured, a night</th>' +
            '<th>Predicted sub / night</th></tr>' + crow + '</table>' +
            '<div class="g-dim">Arc a corner star moves about the frame center; a pivot at the OAG guide star off the frame edge doubles it.</div>';
        (d.notes || []).forEach(function (n) { html += '<div class="g-dim">' + esc(n) + '</div>'; });
        setHTML('tsRotation', html);
    }
    async function loadRotation() {
        if (!$('tsRotation')) return;
        try {
            renderRotation(await getJSON('/api/rotation/report?nights=14'));
        } catch (e) { setText('tsRotation', 'error: ' + e); }
    }

    // ---- 4. the manual TPoint record ----------------------------------------
    var FIELDS = [
        ['model_date', 'Model date (YYYY-MM-DD)', 'date'], ['points', 'Points', 'number'],
        ['rms_arcsec', 'RMS (arcsec)', 'number'], ['polar_az_arcmin', 'Polar error az (arcmin)', 'number'],
        ['polar_alt_arcmin', 'Polar error alt (arcmin)', 'number'], ['run_binning', 'Run binning', 'number'],
        ['model_active', 'TPoint model on', 'bool'], ['protrack_on', 'ProTrack on', 'bool'],
        ['allsky_automated', 'Use All Sky Image Link on', 'bool'], ['allsky_db_installed', 'All Sky database installed', 'bool'],
        ['ucac4_installed', 'UCAC4 installed', 'bool'], ['gaia_installed', 'Gaia installed', 'bool'],
        ['equipment_changed_on', 'Equipment changed on (YYYY-MM-DD)', 'date'], ['notes', 'Notes', 'text']
    ];
    function fieldHTML(f, rec) {
        var v = rec[f[0]], id = 'tsM_' + f[0];
        var input;
        if (f[2] === 'bool') {
            input = '<select id="' + id + '"><option value="">?</option>' +
                '<option value="true"' + (v === true ? ' selected' : '') + '>yes</option>' +
                '<option value="false"' + (v === false ? ' selected' : '') + '>no</option></select>';
        } else {
            input = '<input id="' + id + '" type="' + (f[2] === 'number' ? 'number' : 'text') + '" step="any" value="' +
                esc(v == null ? '' : v) + '"' + (f[2] === 'date' ? ' placeholder="YYYY-MM-DD"' : '') + '>';
        }
        return '<label class="g-stat"><span class="k">' + esc(f[1]) + '</span>' + input + '</label>';
    }
    async function loadManual() {
        if (!$('tsManualFields')) return;
        try {
            var d = await getJSON('/api/thesky/manual');
            var rec = d.record || {};
            setHTML('tsManualFields', FIELDS.map(function (f) { return fieldHTML(f, rec); }).join(''));
            setText('tsManualStatus', rec.entered_at ? ' last entered ' + rec.entered_at +
                ' (reads unknown after ' + d.max_age_days + ' d)' : ' no record yet');
        } catch (e) { setText('tsManualStatus', ' error: ' + e); }
    }
    async function saveManual() {
        var body = {};
        FIELDS.forEach(function (f) {
            var el = $('tsM_' + f[0]);
            if (!el || el.value === '') return;
            body[f[0]] = f[2] === 'bool' ? el.value === 'true' : f[2] === 'number' ? Number(el.value) : el.value;
        });
        setText('tsManualStatus', ' saving...');
        try {
            var r = await fetch('/api/thesky/manual', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                       body: JSON.stringify(body)});
            var d = await r.json();
            setText('tsManualStatus', d.ok ? ' saved ' + stamp() : ' not saved: ' + (d.problems || []).join('; '));
            if (d.ok) { loadManual(); loadAudit(true); }
        } catch (e) { setText('tsManualStatus', ' error: ' + e); }
    }

    function on(id, fn) { var el = $(id); if (el) el.onclick = fn; }
    function init() {
        if (!$('tpointSec')) return;
        on('tsRefresh', function () { loadAudit(true); });
        on('tsAstap', function () { loadImagelink('astap'); });
        on('tsThesky', function () { loadImagelink('thesky'); });
        on('tsManualSave', saveManual);
        loadAudit(false);
        loadImagelink('');
        loadPointing();
        loadRotation();
        loadManual();
        // the stored audit every 60 s (never re-run on a timer, never while
        // an action is in flight or the tab is hidden)
        setInterval(function () {
            if (document.hidden || busy.audit || busy.imagelink) return;
            loadAudit(false);
        }, 60000);
    }

    window.THESKY = {init: init, loadAudit: loadAudit, loadImagelink: loadImagelink,
                     loadPointing: loadPointing, loadRotation: loadRotation,
                     loadManual: loadManual};
})();
