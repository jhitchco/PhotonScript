/* PhotonScript "Where is it" panel + Pause / Resume (PS-64), top of the dashboard.
 *
 * Data: GET /api/night/where (routers/where.py), fetched by the dashboard's
 * existing 10 s top-strip poll (loadStrip calls window.loadWherePanel); the
 * existing 1 s clock ticker calls window.tickWherePanel so the seconds-left
 * and dawn countdowns move between polls. No timers of its own.
 * Pause: POST /api/arm/pause {piggy, when} after the inline confirm box
 * (Piggy-600 keep is the default, stop after the current sub is the
 * default). Resume: POST /api/arm/resume after a confirm. A watched
 * sideloaded night (PS-136) pauses alert-only: nothing is sent to NINA.
 * PS-143: an "On target" cell (separation of the mount, or a fresh RC16
 * plate solve, from the planned center; red chip once the off-target alert
 * condition holds) and Restart tonight from now (POST /api/arm/restart after
 * a confirm; refused while watching a sideload: use the sideload preview).
 */
(function () {
    "use strict";

    var last = null, lastAt = 0, busy = false;

    function $(id) { return document.getElementById(id); }

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
        });
    }

    function dur(s) {
        if (s == null || isNaN(s)) return '-';
        s = Math.max(0, Math.round(s));
        var h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, x = s % 60;
        if (h) return h + 'h' + String(m).padStart(2, '0') + 'm';
        if (m) return m + 'm' + String(x).padStart(2, '0') + 's';
        return x + 's';
    }

    function since() { return (Date.now() - lastAt) / 1000; }

    function cell(label, html, title) {
        return '<div class="wp-cell"' + (title ? ' title="' + esc(title) + '"' : '') + '>' +
            '<div class="wp-label">' + label + '</div><div class="wp-val">' + html + '</div></div>';
    }

    function expHtml(e, sub, idPrefix) {
        var txt = sub ? 'sub ' + sub.n + ' of ' + sub.of : '';
        if (!e) return (txt || '-') + ' <span class="wp-dim">(camera not read)</span>';
        if (e.exposing) {
            var pct = e.progress != null ? Math.round(e.progress * 100) : null;
            txt += (txt ? ' &middot; ' : '') + '<span id="' + idPrefix + 'Left">' +
                (e.left_s != null ? dur(e.left_s - since()) + ' left' : 'exposing') + '</span>' +
                (e.total_s ? ' of ' + Math.round(e.total_s) + ' s' : '');
            txt += '<div class="wp-bar"><div id="' + idPrefix + 'Bar" style="width:' +
                (pct != null ? pct : 0) + '%"></div></div>';
            return txt;
        }
        return (txt || '-') + ' <span class="wp-dim">(' + esc(e.state || 'idle') + ')</span>';
    }

    function offTargetHtml(o) {
        if (!o) return '<span class="wp-dim">-</span>';
        if (o.mode === 'off') return '<span class="wp-dim">off</span>';
        var e = o.expected || {}, g = o.solve || o.mount;
        if (!g) return '<span class="wp-dim">' + esc(o.reason || 'no position') + '</span>';
        var cls = {ok: 'wp-ot-ok', watch: 'wp-ot-watch', off: 'wp-ot-off'}[o.status] || '';
        var word = {ok: 'on target', watch: 'off, watching', off: 'OFF TARGET'}[o.status] ||
            (o.imaging ? 'checking' : 'not imaging');
        var h = Number(o.sep_arcmin).toFixed(1) + '&prime; ' + esc(g.dir || '') +
            ' <span class="wp-dim">(' + esc(o.source || 'mount') + ', limit ' +
            esc(o.limit_arcmin) + '&prime;)</span>';
        h += ' <span class="wp-ot ' + cls + '">' + esc(word) + '</span>';
        if (o.solve && o.mount) h += '<div class="wp-dim">mount ' + Number(o.mount.arcmin).toFixed(1) +
            '&prime;, solve ' + esc(o.solve.age_min) + ' min old</div>';
        if (e.source && e.source !== 'target') h += '<div class="wp-dim">vs ' + esc(e.source) + '</div>';
        if (o.streak) h += '<div class="wp-warn">' + esc(o.streak.subs) + ' sub(s), ' +
            esc(o.streak.minutes) + ' min over the limit</div>';
        if (!o.imaging && o.reason) h += '<div class="wp-dim">' + esc(o.reason) + '</div>';
        return h;
    }

    function render(d) {
        var box = $('wherePanelBody');
        if (!box) return;
        var a = d.armer || {}, r = d.rc16 || {}, g = d.guiding || {}, c = d.cooler || {};
        var p = d.piggy, dw = d.dawn || {}, sf = d.safety || {};
        var tgt = r.target ? esc(r.target) : '<span class="wp-dim">none running</span>';
        if (r.mosaic) tgt += ' <span class="wp-chip">' + esc(r.mosaic.name) + ' P' +
            esc(r.mosaic.panel) + '/' + esc(r.mosaic.of) + '</span>';
        var now = r.running ? esc(r.running) : (r.sequence_running === false
            ? 'NINA #1 idle' : (r.nina_error ? 'NINA #1 not read' : '-'));
        var next = (r.next && r.next.length) ? esc(r.next.join(', ')) : '-';
        var gl = esc(g.label || '-');
        if (g.mode === 'guided') {
            gl += ' &middot; ' + esc(g.state || '?');
            if (g.rms != null && (g.state === 'guiding' || g.state === 'settling'))
                gl += ' ' + Number(g.rms).toFixed(2) + (g.rms_units === 'px' ? ' px' : '&Prime;');
        }
        function cool(x) {
            if (!x) return '-';
            var t = x.temp_c != null ? Number(x.temp_c).toFixed(1) + ' C' : '?';
            var s = (x.cooler_on ? 'ON' : 'OFF') + ' ' + t +
                (x.setpoint_c != null ? ' (set ' + x.setpoint_c + ')' : '') +
                (x.power != null ? ' ' + Math.round(x.power) + '%' : '');
            return esc(s) + (x.note ? '<div class="wp-warn">' + esc(x.note) + '</div>' : '');
        }
        var roof = sf.roof === 'open' ? '<b style="color:#4ade80;">OPEN (safe)</b>'
            : sf.roof === 'closed' ? '<b style="color:#f87171;">CLOSED (unsafe)</b>'
            : '<span class="wp-dim">unknown</span>';
        var html = cell('Target', tgt) +
            cell('Filter', esc(r.filter || '-')) +
            cell('RC16 exposure', expHtml(r.exposure, r.sub, 'wpRc')) +
            cell('Now', now) +
            cell('Next', next, 'next items in NINA #1\'s sequence') +
            cell('On target', offTargetHtml(d.off_target),
                 'separation of the mount (or a fresh RC16 plate solve) from the planned center (PS-143)') +
            cell('Guiding', gl) +
            cell('Cooler', 'RC16 ' + cool(c.rc16) + (c.piggyback ? '<br>Piggy ' + cool(c.piggyback) : '')) +
            cell('Roof', roof);
        if (p) {
            var sp = p.split || {}, mo = sp.motion || {};
            var guard = (sp.settle_gate ? 'settle gate on' : 'settle gate off') +
                (sp.abort_on_move ? ', abort on move' : '') +
                (mo.slewing ? ' &middot; <b style="color:#fbbf24;">mount slewing</b>' :
                 (mo.still_s != null ? ' &middot; still ' + dur(mo.still_s) : '')) +
                (sp.last_gate && sp.last_gate.verdict ? ' &middot; last gate ' + esc(sp.last_gate.verdict) : '');
            html += cell('Piggy-600', (p.running ? esc(p.running) : (p.sequence_running === false
                ? 'idle' : '-')) + '<br>' + expHtml(p.exposure, p.sub, 'wpPg') +
                '<div class="wp-dim">' + guard + '</div>');
        }
        html += cell('Dawn', '<span id="wpDawn"></span>');
        box.innerHTML = html;
        var st = $('wherePanelState');
        if (st) {
            var txt = a.state || '-';
            if (a.state === 'PAUSED_OPERATOR') {
                var ph = (a.pause || {}).phase;
                txt = ph === 'stopping' ? 'PAUSING (waiting for the sub to end)' : 'PAUSED';
                if ((a.restart || {}).pending) txt = 'RESTARTING (waiting for the sub to end)';
            } else if (a.watch_paused) {
                txt = 'WATCHING, alerts paused';
            }
            st.textContent = txt;
            st.style.color = (a.state === 'PAUSED_OPERATOR' || a.watch_paused) ? '#fbbf24'
                : a.state === 'RUNNING' ? '#4ade80' : '#94a3b8';
            st.title = a.detail || '';
        }
        var pb = $('pauseBtn'), rb = $('resumeBtn');
        if (pb) pb.hidden = !a.can_pause;
        if (rb) rb.hidden = !a.can_resume;
        var xb = $('restartBtn');
        if (xb) xb.hidden = !a.can_restart;
        tick();
    }

    function tick() {
        if (!last) return;
        var s = since();
        [['wpRc', (last.rc16 || {}).exposure], ['wpPg', (last.piggy || {}).exposure]].forEach(function (x) {
            var e = x[1];
            if (!e || !e.exposing || e.left_s == null) return;
            var el = $(x[0] + 'Left'), bar = $(x[0] + 'Bar');
            var left = e.left_s - s;
            if (el) el.textContent = left > 0 ? dur(left) + ' left' : 'downloading';
            if (bar && e.total_s) bar.style.width =
                Math.max(0, Math.min(100, (1 - left / e.total_s) * 100)).toFixed(1) + '%';
        });
        var dw = last.dawn || {}, el = $('wpDawn');
        if (el) {
            var parts = [];
            if (dw.dusk_in_s != null && dw.dusk_in_s - s > 0) parts.push('astro dark in ' + dur(dw.dusk_in_s - s));
            if (dw.dawn_in_s != null) parts.push(dw.dawn_in_s - s > 0 ? 'dawn in ' + dur(dw.dawn_in_s - s) : 'past astro dawn');
            if (dw.shutdown_in_s != null && dw.shutdown_in_s - s > 0) parts.push('shutdown in ' + dur(dw.shutdown_in_s - s));
            el.textContent = parts.join(' / ') || '-';
        }
    }

    async function load() {
        try {
            var r = await fetch('/api/night/where');
            if (!r.ok) throw new Error('HTTP ' + r.status);
            last = await r.json();
            lastAt = Date.now();
            render(last);
        } catch (e) {
            var box = $('wherePanelBody');
            if (box && !last) box.textContent = 'Live panel unavailable: ' + e.message;
        }
    }

    function showPauseBox(show) {
        var b = $('pauseConfirm');
        if (!b) return;
        b.hidden = !show;
        if (!show) return;
        var watching = last && last.armer && last.armer.state === 'WATCHING';
        $('pauseConfirmText').textContent = watching
            ? 'Pause the watched night? Alert-only: the guiding watchdog is muted. ' +
              'Nothing is sent to NINA; pause the sideloaded sequence in NINA itself.'
            : 'Pause tonight? NINA #1 stops its sequence (after the current sub by default). ' +
              'Tracking, cooler and PHD2 stay on; nothing is parked. Resume re-dispatches the remainder.';
        $('pauseOptions').hidden = !!watching;
        var hasPiggy = !!(last && last.piggy);
        $('pausePiggyRow').hidden = !hasPiggy;
    }

    async function post(url, body) {
        var r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                  body: JSON.stringify(body || {})});
        var d;
        try { d = await r.json(); } catch (e) { d = {detail: 'HTTP ' + r.status}; }
        if (!r.ok) alert('Refused: ' + (d.detail || r.status));
        return d;
    }

    async function doPause() {
        if (busy) return;
        busy = true;
        try {
            var piggy = ($('pausePiggyPause') && $('pausePiggyPause').checked) ? 'pause' : 'keep';
            var when = ($('pauseNow') && $('pauseNow').checked) ? 'now' : 'after_exposure';
            await post('/api/arm/pause', {piggy: piggy, when: when});
            showPauseBox(false);
            await load();
            if (window.refreshArm) window.refreshArm();
        } finally { busy = false; }
    }

    async function doResume() {
        if (busy) return;
        var a = (last && last.armer) || {};
        var msg = a.state === 'WATCHING'
            ? 'Resume the watched night? Guiding alerts come back on.'
            : ((a.pause || {}).phase === 'stopping'
               ? 'Cancel the pause? NINA #1 has not been stopped yet; it keeps running.'
               : 'Resume tonight? The remainder of the night is re-dispatched to NINA #1 ' +
                 '(slew, center, guiding start again)' +
                 ((a.pause || {}).piggy === 'pause' ? ', and the Piggy-600 companion is re-dispatched.' : '.'));
        if (!confirm(msg)) return;
        busy = true;
        try {
            await post('/api/arm/resume', {});
            await load();
            if (window.refreshArm) window.refreshArm();
        } finally { busy = false; }
    }

    async function doRestart() {
        if (busy) return;
        var a = (last && last.armer) || {};
        if (a.state === 'WATCHING') {
            alert('This is a sideloaded night: NINA runs it, PhotonScript never re-dispatches it. ' +
                  'Use the sideload preview below (Sideload a custom night) to build the rest of tonight.');
            var sb = $('sideloadBox');
            if (sb) { sb.open = true; sb.scrollIntoView({behavior: 'smooth'}); }
            return;
        }
        var stopped = a.state === 'PAUSED_OPERATOR' && (a.pause || {}).phase === 'paused';
        var msg = 'Restart tonight from now? ' +
            (stopped ? 'NINA #1 is already stopped. '
                     : 'NINA #1 stops after the current sub. ') +
            'The rest of tonight is re-planned from the current goals and re-dispatched ' +
            '(slew, center, AF and guiding start again). Nothing warms, parks or turns a ' +
            'cooler off; the Piggy-600 keeps imaging.';
        if (!confirm(msg)) return;
        busy = true;
        try {
            var d = await post('/api/arm/restart', {when: 'after_exposure'});
            if (d && d.ok === false) return;
            await load();
            if (window.refreshArm) window.refreshArm();
        } finally { busy = false; }
    }

    function init() {
        if (!$('wherePanel')) return;
        $('pauseBtn').addEventListener('click', function () { showPauseBox(true); });
        $('pauseCancel').addEventListener('click', function () { showPauseBox(false); });
        $('pauseGo').addEventListener('click', doPause);
        $('resumeBtn').addEventListener('click', doResume);
        if ($('restartBtn')) $('restartBtn').addEventListener('click', doRestart);
        load();
    }

    window.loadWherePanel = load;
    window.tickWherePanel = tick;
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
