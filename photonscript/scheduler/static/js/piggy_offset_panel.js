/* PhotonScript Piggy-600 boresight offset panel (PS-26), Guiding tab.
 *
 * Fills #poInfo from GET /api/piggy-offset (routers/piggy_offset.py): the
 * offset per pier side (median, scatter, rotation, pairs, nights), a pair
 * scatter plot, the per-night medians, and each Piggy-driven project's
 * center plan with its frame-center option (PUT
 * /api/piggy-offset/frame-center/{id}). Re-measure = GET ?refresh=true
 * (stored solves only, never ASTAP).
 */
var PIGGYOFFSET = (function () {
    "use strict";

    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v == null ? '-' : v).replace(/[&<>"]/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
        });
    }
    function num(v, d) { return v == null ? '-' : Number(v).toFixed(d == null ? 2 : d); }
    function sgn(v) { return v == null ? '-' : (v >= 0 ? '+' : '') + Number(v).toFixed(2) + "'"; }
    var PIER_COLOR = {East: 'var(--accent, #4aa3ff)', West: 'var(--warn, #e0a030)'};

    function modeChip(m) {
        var cls = {on: 'badge-pass', preview: 'badge-warn', off: 'badge-unknown'}[m] || 'badge-unknown';
        return '<span class="badge ' + cls + '">mode ' + esc(m) + '</span>';
    }

    function scatter(pairs, piers) {
        if (!pairs || !pairs.length) return '';
        var W = 260, H = 260, pad = 22;
        var lim = 1;
        pairs.forEach(function (p) { lim = Math.max(lim, Math.abs(p.east), Math.abs(p.north)); });
        lim = Math.ceil(lim * 1.1);
        function sx(e) { return W / 2 - e / lim * (W / 2 - pad); }  // east to the left (sky view)
        function sy(n) { return H / 2 - n / lim * (H / 2 - pad); }
        var s = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H +
            '" role="img" aria-label="Piggy-600 center offsets from the RC16 center">' +
            '<line x1="' + pad + '" y1="' + H / 2 + '" x2="' + (W - pad) + '" y2="' + H / 2 + '" stroke="currentColor" stroke-opacity=".25"/>' +
            '<line x1="' + W / 2 + '" y1="' + pad + '" x2="' + W / 2 + '" y2="' + (H - pad) + '" stroke="currentColor" stroke-opacity=".25"/>' +
            '<text x="' + W / 2 + '" y="12" text-anchor="middle" font-size="10" fill="currentColor">N</text>' +
            '<text x="8" y="' + (H / 2 + 4) + '" font-size="10" fill="currentColor">E</text>' +
            '<text x="' + (W - 4) + '" y="' + (H - 4) + '" text-anchor="end" font-size="9" fill="currentColor" fill-opacity=".6">+/-' + lim + "'</text>";
        pairs.forEach(function (p) {
            s += '<circle cx="' + sx(p.east).toFixed(1) + '" cy="' + sy(p.north).toFixed(1) + '" r="2.2" fill="' +
                (PIER_COLOR[p.pier] || 'currentColor') + '" fill-opacity=".6"><title>' + esc(p.night) + ' ' + esc(p.pier) +
                ': E ' + num(p.east) + "' N " + num(p.north) + "'</title></circle>";
        });
        ['East', 'West'].forEach(function (k) {
            var m = (piers || {})[k];
            if (!m || m.east == null) return;
            s += '<circle cx="' + sx(m.east).toFixed(1) + '" cy="' + sy(m.north).toFixed(1) + '" r="5" fill="none" stroke="' +
                PIER_COLOR[k] + '" stroke-width="2"' + (m.inferred ? ' stroke-dasharray="2 2"' : '') + '><title>pier ' + k +
                (m.inferred ? ' (inferred)' : ' median') + '</title></circle>';
        });
        return s + '</svg><div class="g-dim">Each dot: one Piggy sub vs its RC16 sub (blue pier East, amber pier West); rings: medians (dashed = inferred).</div>';
    }

    function render(d) {
        var st = d.store || {}, piers = st.piers || {};
        var html = modeChip(d.mode) + ' ';
        if (!d.store) {
            html += '<b>No measurement stored yet.</b> Re-measure reads the pointing sidecars; pairs need both rigs plate-solved by the dawn pass (PS-67).';
        } else {
            html += '<b>' + esc(st.n_pairs) + ' pair(s)</b> over ' + esc(st.nights_scanned) + ' night(s) (' +
                esc(st.first_night) + ' to ' + esc(st.last_night) + '), measured ' + esc(st.measured_at) +
                '; a side needs ' + esc(d.min_pairs) + ' pairs; max shift ' + esc(d.max_shift_arcmin) + "'.";
            if (st.n_no_pier) html += ' <span class="g-dim">' + esc(st.n_no_pier) + ' pair(s) without a pier side.</span>';
            var rows = ['East', 'West'].map(function (k) {
                var s = piers[k];
                if (!s) return '<tr><td>' + k + '</td><td colspan="7">no pairs</td></tr>';
                return '<tr><td>' + k + (s.inferred ? ' <span class="badge badge-unknown">inferred</span>' : '') +
                    '</td><td>' + sgn(s.east) + '</td><td>' + sgn(s.north) + '</td><td>' + esc(s.total) +
                    "'</td><td>" + esc(s.sigma_east) + "' / " + esc(s.sigma_north) + "'</td><td>" +
                    (s.night_sigma_east == null ? '-' : esc(s.night_sigma_east) + "' / " + esc(s.night_sigma_north) + "'") +
                    '</td><td>' + esc(s.rot) + (s.sigma_rot != null ? ' +/- ' + esc(s.sigma_rot) : '') + '</td><td>' +
                    esc(s.n_pairs) + (s.n_rejected ? ' (' + esc(s.n_rejected) + ' clipped)' : '') + ' / ' + esc(s.n_nights) + '</td></tr>';
            }).join('');
            html += '<table class="g-tbl"><tr><th>Pier</th><th>Piggy E of RC16</th><th>Piggy N of RC16</th><th>Total</th>' +
                '<th>Pair sigma E/N</th><th>Night sigma E/N</th><th>Rotation deg</th><th>Pairs / nights</th></tr>' + rows + '</table>';
            var nrows = '';
            ['East', 'West'].forEach(function (k) {
                ((piers[k] || {}).nights || []).forEach(function (n) {
                    nrows += '<tr><td>' + esc(n.night) + '</td><td>' + k + '</td><td>' + sgn(n.east) + '</td><td>' +
                        sgn(n.north) + '</td><td>' + esc(n.n) + '</td></tr>';
                });
            });
            html += '<div style="display:flex;flex-wrap:wrap;gap:1rem;align-items:flex-start;">' +
                '<div>' + scatter(st.pairs, piers) + '</div>' +
                (nrows ? '<div><table class="g-tbl"><tr><th>Night</th><th>Pier</th><th>E</th><th>N</th><th>Pairs</th></tr>' +
                    nrows + '</table></div>' : '') + '</div>';
        }
        var projs = d.projects || [];
        if (projs.length) {
            html += '<h4>Piggy-driven targets</h4><table class="g-tbl"><tr><th>Target</th><th>Tonight</th>' +
                '<th>RC16 center pier West / East</th><th>Frame center (RA h, Dec deg)</th></tr>';
            projs.forEach(function (p) {
                var bp = (p.plan || {}).by_pier || {}, w = bp.West, e = bp.East, fc = p.frame_center || {};
                var cen = (w && e) ? num(w.ra_hours, 4) + 'h ' + num(w.dec_degrees, 3) + ' / ' +
                    num(e.ra_hours, 4) + 'h ' + num(e.dec_degrees, 3) : '-';
                html += '<tr><td>' + esc(p.name) + (p.active ? '' : ' (inactive)') + '</td><td>' +
                    esc((p.plan || {}).applied ? 'shifted' : (d.mode === 'preview' && w ? 'preview' : 'no shift')) +
                    '<div class="g-dim">' + esc((p.plan || {}).note) + '</div></td><td>' + cen + '</td><td>' +
                    '<input id="poRa_' + esc(p.id) + '" type="number" step="any" style="width:6em" placeholder="' + num(p.ra_hours, 4) +
                    '" value="' + (fc.ra_hours == null ? '' : esc(fc.ra_hours)) + '"> ' +
                    '<input id="poDec_' + esc(p.id) + '" type="number" step="any" style="width:6em" placeholder="' + num(p.dec_degrees, 3) +
                    '" value="' + (fc.dec_degrees == null ? '' : esc(fc.dec_degrees)) + '"> ' +
                    '<button class="btn btn-secondary btn-sm" data-po-save="' + esc(p.id) + '">Save</button> ' +
                    '<button class="btn btn-secondary btn-sm" data-po-clear="' + esc(p.id) + '" title="Center on the target">Clear</button></td></tr>';
            });
            html += '</table><div class="g-dim">Frame center: the point the Piggy-600 frame is centered on (blank = the target).</div>';
        } else {
            html += '<div class="g-dim">No project has driving rig piggyback.</div>';
        }
        $('poInfo').innerHTML = html;
        Array.prototype.forEach.call(document.querySelectorAll('[data-po-save]'), function (b) {
            b.onclick = function () { saveCenter(b.getAttribute('data-po-save'), false); };
        });
        Array.prototype.forEach.call(document.querySelectorAll('[data-po-clear]'), function (b) {
            b.onclick = function () { saveCenter(b.getAttribute('data-po-clear'), true); };
        });
    }

    async function load(refresh) {
        if (!$('poInfo')) return;
        $('poStatus').textContent = refresh ? ' measuring...' : '';
        try {
            var r = await fetch('/api/piggy-offset' + (refresh ? '?refresh=true' : ''));
            render(await r.json());
            $('poStatus').textContent = refresh ? ' re-measured ' + new Date().toLocaleTimeString() : '';
        } catch (e) { $('poInfo').textContent = 'error: ' + e; }
    }

    async function saveCenter(id, clear) {
        var body = {};
        if (!clear) {
            var ra = $('poRa_' + id).value, dec = $('poDec_' + id).value;
            if (ra === '' || dec === '') { $('poStatus').textContent = ' enter RA and Dec, or Clear'; return; }
            body = {ra_hours: Number(ra), dec_degrees: Number(dec)};
        }
        try {
            var r = await fetch('/api/piggy-offset/frame-center/' + encodeURIComponent(id),
                {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
            var d = await r.json();
            $('poStatus').textContent = d.ok ? ' saved' : ' not saved: ' + (d.note || r.status);
            if (d.ok) load(false);
        } catch (e) { $('poStatus').textContent = ' error: ' + e; }
    }

    function init() {
        if (!$('poInfo')) return;
        var b = $('poRefresh');
        if (b) b.onclick = function () { load(true); };
        load(false);
    }
    return {init: init, render: render};
})();
