// PS-33: the integrator ledger line on the goal cards (dashboard), the
// Targets cards and the per-target page. Data: GET /api/integrations/summary
// (scheduler/integrations.summary): per goal + rig the latest ledger and the
// approved hours that arrived since it.
//
//   Integrations.load() -> Promise<{project_id: [row, ...]}>
//   Integrations.html(rows, opts) -> one short HTML block ('' when none)
//       opts.paths = true adds the run folder / packet paths (target page)
//   Integrations.fill(map, idPrefix) fills every element with id
//       idPrefix + project_id
(function () {
    'use strict';
    const RIG = {piggyback: 'Piggy-600', rc16: 'RC16'};

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }

    async function load() {
        const out = {};
        try {
            const d = await (await fetch('/api/integrations/summary')).json();
            (d.integrations || []).forEach(r => {
                (out[r.project_id] = out[r.project_id] || []).push(r);
            });
        } catch (e) { /* scheduler without ledgers: no line */ }
        return out;
    }

    function row(r, opts) {
        const L = r.latest || {};
        const col = L.integrated_ok === false ? '#f87171' : (L.integrated_ok ? '#4ade80' : '#eab308');
        const tip = ['run ' + L.run, L.run_dir ? 'folder ' + L.run_dir : '',
                     L.packet ? 'packet ' + L.packet : '', L.final ? 'final ' + L.final : '',
                     L.calibration ? 'calibration ' + L.calibration : '',
                     L.trigger ? 'trigger: ' + L.trigger : ''].filter(Boolean).join(' | ');
        let h = '<div title="' + esc(tip) + '"><span style="color:' + col + ';">&#9679;</span> ' +
            '<b>' + esc(RIG[r.rig] || r.rig) + '</b> ' + esc(L.headline || '') +
            (r.versions > 1 ? ' <span style="opacity:.6;">(' + r.versions + ' versions)</span>' : '') +
            ' &middot; <span style="color:' + (r.new_data_h >= 1 ? '#7dd3fc' : 'inherit') +
            ';">new data since: ' + Number(r.new_data_h || 0).toFixed(1) + ' h</span>' +
            (L.verdict ? ' &middot; verdict ' + esc(L.verdict) : '') +
            (L.open_asks ? ' &middot; ' + L.open_asks + ' open ask' + (L.open_asks > 1 ? 's' : '') : '') +
            '</div>';
        if (opts && opts.paths) {
            const p = [['run folder', L.run_dir], ['AstroBin packet', L.packet], ['final', L.final]]
                .filter(x => x[1]);
            if (p.length) {
                h += '<div style="opacity:.75;font-size:.72rem;margin-left:1rem;">' +
                    p.map(x => esc(x[0]) + ' <code>' + esc(x[1]) + '</code>').join('<br>') + '</div>';
            }
        }
        return h;
    }

    function html(rows, opts) {
        if (!rows || !rows.length) return '';
        return rows.map(r => row(r, opts)).join('');
    }

    function fill(map, idPrefix, opts) {
        Object.keys(map).forEach(pid => {
            const el = document.getElementById(idPrefix + pid);
            if (el) el.innerHTML = html(map[pid], opts);
        });
    }

    window.Integrations = {load: load, html: html, fill: fill};
})();
