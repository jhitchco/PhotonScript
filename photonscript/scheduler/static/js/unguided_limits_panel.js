/* PhotonScript learned unguided limits panel, Guiding tab.
 *
 * Fills #ulInfo / #ulRows from GET /api/tracking/unguided-limits
 * (routers/tracking.py, scheduler/unguided_limits.py): per rig and sky
 * region the subs and passes per sub length, the drift rate, the learned
 * limit (measured or drift model), its confidence and the probe step, plus
 * the dark lengths the table adds to the night quota. Refresh = GET
 * ?refresh=true (re-reads the nights' subs logs; nothing else written).
 */
var UNGUIDEDLIMITS = (function () {
    "use strict";

    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v == null ? '-' : v).replace(/[&<>"]/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
        });
    }
    function modeChip(m) {
        var cls = {apply: 'badge-pass', advise: 'badge-warn', off: 'badge-unknown'}[m] || 'badge-unknown';
        return '<span class="badge ' + cls + '">' + esc(m) + '</span>';
    }
    function limitTxt(r) {
        if (r.fails_all) return '<b>trails at every step</b>';
        if (!r.limit_s) return 'undecided';
        return '<b>' + esc(r.limit_s) + ' s</b> <span class="g-dim">(' + esc(r.source) + ', ' +
            esc(r.confidence) + ')</span>';
    }

    function render(d) {
        var html = 'mode ' + modeChip(d.mode) + ' probe ' + modeChip(d.probe) + ' ';
        var rigs = d.rigs || {};
        var names = Object.keys(rigs);
        if (!names.length) {
            $('ulInfo').innerHTML = html + 'No unguided evidence yet.';
            $('ulRows').innerHTML = '';
            return;
        }
        html += 'refreshed ' + esc(d.refreshed_at) + ', evidence since ' + esc(d.since || 'the lookback start') + '.';
        $('ulInfo').innerHTML = html;
        var out = '';
        names.forEach(function (rg) {
            var t = rigs[rg] || {};
            var ex = t.excluded || {};
            var exTxt = Object.keys(ex).sort().map(function (k) { return esc(k) + ' ' + esc(ex[k]); }).join(', ') || 'none';
            out += '<h3 class="g-group">' + esc(rg) + '</h3><div class="bd">ecc gate ' + esc(t.gate_ecc) +
                ', pass rate ' + esc(t.pass_rate) + ', ' + esc(t.n_used) + ' subs used; excluded ' + exTxt +
                (t.dark_lengths && t.dark_lengths.length ? '; darks owed for ' +
                    t.dark_lengths.map(function (x) { return esc(x) + ' s'; }).join(', ') : '') + '</div>';
            var rows = t.rows || [];
            if (!rows.length) { out += '<div class="bd">no unguided evidence</div>'; return; }
            out += '<table class="g-tbl"><tr><th>Scope</th><th>Region</th><th>Subs</th><th>Passed per length</th>' +
                '<th>Drift "/min</th><th>Alt</th><th>Limit</th><th>Probe</th></tr>';
            rows.forEach(function (r) {
                var lens = Object.keys(r.lengths || {}).map(function (k) {
                    return esc(k) + ' s ' + esc(r.lengths[k]);
                }).join(', ');
                out += '<tr><td>' + esc(r.scope) + '</td><td>' + esc(r.label) + '</td><td>' + esc(r.n) +
                    '</td><td>' + lens + '</td><td>' + esc(r.drift_arcsec_min) + '</td><td>' + esc(r.alt_med) +
                    '</td><td>' + limitTxt(r) + '</td><td>' + (r.probe_s ? esc(r.probe_s) + ' s' : '-') + '</td></tr>';
            });
            out += '</table>';
        });
        $('ulRows').innerHTML = out;
    }

    async function load(refresh) {
        var btn = $('ulRefresh');
        if (btn) btn.disabled = true;
        try {
            var r = await fetch('/api/tracking/unguided-limits' + (refresh ? '?refresh=true' : ''));
            if (!r.ok) throw new Error('HTTP ' + r.status);
            render(await r.json());
        } catch (e) {
            $('ulInfo').textContent = 'Unguided limits unavailable: ' + e.message;
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    function init() {
        if (!$('ulInfo')) return;
        var btn = $('ulRefresh');
        if (btn) btn.addEventListener('click', function () { load(true); });
        load(false);
    }

    return {init: init, render: render};
})();
