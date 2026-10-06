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
//
// PS-142 (campaign review):
//   Integrations.loadStatus(calibration) -> Promise<{project_id: goal}>
//       GET /api/integrations/status: Acquiring / Ready to process /
//       Processing / Processed (vN) / Published (vN) per goal and rig
//   Integrations.chip(goal) -> the status chip HTML ('' when none)
//   Integrations.campaign(lineId, reviewId, opts) fills idPrefix lineId +
//       project_id with chip + ledger line, and (reviewId) the compact
//       review panel of every goal with a ledger
//   Integrations.review(el, projectId, opts) renders the review panel:
//       latest verdict + notes, asks with Approve / Decline. opts.compact
//       shows open asks only; opts.onChange runs after a plan change.
//       Approve of a plan-changing ask shows the exposure-plan diff
//       (GET /api/integrations/asks/{id}/proposal) and, only after a
//       confirm, applies it through PATCH /api/projects2/{id}, then marks
//       the ask applied.
(function () {
    'use strict';
    const RIG = {piggyback: 'Piggy-600', rc16: 'RC16'};
    const STATE_COL = {acquiring: '#94a3b8', ready: '#7dd3fc', processing: '#eab308',
                       processed: '#4ade80', published: '#c084fc'};
    const NL = String.fromCharCode(10);

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
            const p = [['run folder', L.run_dir], ['AstroBin packet', L.packet], ['final', L.final],
                       ['AstroBin', L.astrobin_url]].filter(x => x[1]);
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

    // --- PS-142: campaign status chip ---------------------------------------------
    async function loadStatus(calibration) {
        const out = {};
        try {
            const d = await (await fetch('/api/integrations/status' +
                                         (calibration ? '?calibration=true' : ''))).json();
            (d.goals || []).forEach(g => { out[g.project_id] = g; });
        } catch (e) { /* older scheduler: no chip */ }
        return out;
    }

    function chipOne(s, withRig) {
        const col = STATE_COL[s.state] || '#94a3b8';
        const label = (withRig ? (RIG[s.rig] || s.rig) + ': ' : '') + s.label;
        const link = s.state === 'published' && s.astrobin_url;
        const inner = '<span title="' + esc(s.detail || '') + '" style="display:inline-block;' +
            'padding:.05rem .45rem;border-radius:999px;font-size:.72rem;font-weight:600;' +
            'border:1px solid ' + col + ';color:' + col + ';margin-right:.3rem;">' +
            esc(label) + '</span>';
        return link ? '<a href="' + esc(s.astrobin_url) + '" target="_blank" rel="noopener" ' +
            'style="text-decoration:none;">' + inner + '</a>' : inner;
    }

    function chip(g) {
        if (!g || !g.rigs || !g.rigs.length) return '';
        const multi = g.rigs.length > 1;
        return g.rigs.map(s => chipOne(s, multi)).join('');
    }

    // --- PS-142: review panel ------------------------------------------------------
    async function post(url, body) {
        const r = await fetch(url, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                    body: JSON.stringify(body || {})});
        return {ok: r.ok, body: await r.json().catch(() => ({}))};
    }

    function fmtPlan(p) {
        if (!p) return '(none)';
        if (typeof p !== 'object') return String(p);
        return p.count + ' x ' + p.exposure_s + ' s (' + p.hours + ' h)' +
            (p.short ? ' + short ' + p.short : '');
    }

    function askText(a) {
        const bits = [a.type.replace('_', ' ')];
        if (a.rig) bits.push(RIG[a.rig] || a.rig);
        if (a.filter) bits.push(a.filter);
        if (a.hours) bits.push('+' + a.hours + ' h');
        if (a.exposure_s) bits.push(a.exposure_s + ' s');
        if (a.count) bits.push(a.count + ' subs');
        if (a.driving_rig) bits.push((RIG[a.driving_rig] || a.driving_rig) + ' drives');
        if (a.what && a.what.length) bits.push(a.what.join(', '));
        if (a.ticket) bits.push(a.ticket);
        return bits.join(' ');
    }

    async function approve(a, projectId, rerender, opts) {
        let pr;
        try {
            pr = await (await fetch('/api/integrations/asks/' + encodeURIComponent(a.id) +
                                    '/proposal')).json();
        } catch (e) { alert('Could not read the proposal: ' + e); return; }
        if (!pr.plan_change) {
            const r = await post('/api/integrations/asks/' + encodeURIComponent(a.id),
                                 {decision: 'approve'});
            if (!r.ok) alert('Not saved: ' + (r.body.detail || 'error'));
            rerender();
            return;
        }
        const lines = (pr.diff || []).map(d => d.plan + ': ' + fmtPlan(d.before) + ' -> ' +
                                          fmtPlan(d.after));
        const msg = 'Approve "' + askText(a) + '" and change the goal?' + NL + NL +
            (pr.note ? pr.note + NL + NL : '') +
            (lines.length ? lines.join(NL) : '(the plan does not change)') + NL + NL +
            'OK applies it to the goal (PATCH /api/projects2). Cancel changes nothing.';
        if (!confirm(msg)) return;
        const r = await fetch('/api/projects2/' + encodeURIComponent(pr.project_id || projectId), {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(pr.patch)});
        if (!r.ok) {
            const b = await r.json().catch(() => ({}));
            alert('The goal was not changed: ' + (b.detail || ('HTTP ' + r.status)));
            return;
        }
        const s = await post('/api/integrations/asks/' + encodeURIComponent(a.id),
                             {decision: 'applied', patch: pr.patch});
        if (!s.ok) alert('Goal changed, but the ask was not marked applied: ' +
                         (s.body.detail || 'error'));
        rerender();
        if (opts && opts.onChange) opts.onChange();
    }

    async function decline(a, rerender) {
        const r = await post('/api/integrations/asks/' + encodeURIComponent(a.id),
                             {decision: 'decline'});
        if (!r.ok) alert('Not saved: ' + (r.body.detail || 'error'));
        rerender();
    }

    function btn(label, col) {
        return '<button type="button" style="font-size:.7rem;padding:.05rem .45rem;margin-left:.3rem;' +
            'background:transparent;border:1px solid ' + col + ';color:' + col +
            ';border-radius:4px;cursor:pointer;">' + label + '</button>';
    }

    function renderRig(r, compact) {
        const L = r.latest || {};
        let h = '<div style="margin:.25rem 0;">';
        if (!compact) {
            h += '<div><b>' + esc(RIG[r.rig] || r.rig) + '</b> ' + esc(L.headline || '') +
                (L.astrobin_url ? ' &middot; <a href="' + esc(L.astrobin_url) +
                 '" target="_blank" rel="noopener" style="color:#c084fc;">AstroBin</a>' : '') +
                '</div>';
        }
        if (r.verdict || r.notes) {
            h += '<div>' + (compact ? '<b>' + esc(RIG[r.rig] || r.rig) + '</b> ' : '') +
                'verdict <b>' + esc(r.verdict || '-') + '</b>' +
                (r.review_version ? ' <span style="opacity:.6;">(v' + r.review_version + ')</span>' : '') +
                (compact && r.notes ? ' <span style="opacity:.75;" title="' + esc(r.notes) + '">' +
                 esc(r.notes.split(NL).slice(-1)[0]) + '</span>' : '') + '</div>';
            if (!compact && r.notes) {
                h += '<div style="white-space:pre-wrap;opacity:.8;font-size:.78rem;margin:.2rem 0 .3rem 1rem;">' +
                    esc(r.notes) + '</div>';
            }
        } else if (!compact) {
            h += '<div style="opacity:.6;">No review written yet.</div>';
        }
        const asks = (r.asks || []).filter(a => !compact || a.status === 'open');
        asks.forEach(a => {
            const open = a.status === 'open';
            h += '<div data-ask="' + esc(a.id) + '" style="margin-left:1rem;' +
                (open ? '' : 'opacity:.55;') + '" title="' + esc((a.why || a.note || '') +
                (a.effect ? ' | Approve: ' + a.effect : '')) + '">' +
                '&#8226; ' + esc(askText(a)) + ' <span style="opacity:.6;">(v' + a.version +
                (open ? '' : ', ' + esc(a.status)) + ')</span>' +
                (a.plan_change && open ? ' <span style="color:#eab308;font-size:.7rem;">changes the plan</span>' : '') +
                (open ? '<span data-approve>' + btn('Approve', '#4ade80') + '</span>' +
                        '<span data-decline>' + btn('Decline', '#f87171') + '</span>' : '') +
                '</div>';
        });
        return h + '</div>';
    }

    async function review(el, projectId, opts) {
        if (!el || !projectId) return;
        opts = opts || {};
        let d;
        try {
            const r = await fetch('/api/integrations/review?project_id=' +
                                  encodeURIComponent(projectId));
            if (!r.ok) { el.innerHTML = ''; return; }
            d = await r.json();
        } catch (e) { el.innerHTML = ''; return; }
        const rigs = (d.rigs || []).filter(r => !opts.compact || r.open_asks || r.verdict);
        if (!rigs.length) {
            el.innerHTML = opts.compact ? '' :
                '<span style="opacity:.6;">No ledger yet: the review appears after the first integration.</span>';
            return;
        }
        el.innerHTML = rigs.map(r => renderRig(r, opts.compact)).join('');
        const byId = {};
        rigs.forEach(r => (r.asks || []).forEach(a => { byId[a.id] = a; }));
        const rerender = () => review(el, projectId, opts);
        el.querySelectorAll('[data-ask]').forEach(div => {
            const a = byId[div.getAttribute('data-ask')];
            const ap = div.querySelector('[data-approve] button');
            const de = div.querySelector('[data-decline] button');
            if (ap) ap.addEventListener('click', () => approve(a, projectId, rerender, opts));
            if (de) de.addEventListener('click', () => decline(a, rerender));
        });
    }

    async function campaign(lineId, reviewId, opts) {
        const [m, st] = await Promise.all([load(), loadStatus(false)]);
        Object.keys(Object.assign({}, m, st)).forEach(pid => {
            const el = document.getElementById(lineId + pid);
            if (el) el.innerHTML = chip(st[pid]) + html(m[pid], opts);
            if (reviewId && m[pid]) review(document.getElementById(reviewId + pid), pid,
                                           Object.assign({compact: true}, opts || {}));
        });
        return {summary: m, status: st};
    }

    window.Integrations = {load: load, html: html, fill: fill, loadStatus: loadStatus,
                           chip: chip, review: review, campaign: campaign};
})();
