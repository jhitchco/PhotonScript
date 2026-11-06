// Integration readiness on the Targets pages (moved off the Runs sidebar,
// 2026-10-11). Data only from the existing endpoints, nothing new computed
// server-side:
//   GET /api/integration/readiness  (scheduler/readiness.readiness_report:
//       every ACTIVE project, one entry per rig; a non-RC16 entry has "rig")
//   GET /api/sync                   (library_synced: the desktop has it)
//
//   Readiness.load() -> Promise<{byKey: {key: [entry, ...]}, synced: bool}>
//   Readiness.key(name) -> comparison key (mirror of target_names.target_key)
//   Readiness.rig(entry) -> 'rc16' | 'piggyback' | ...
//   Readiness.command(entry) -> photonscript integrate --target "T" --rig R
//   Readiness.counts(entry) -> 'L 74 R 69' ('' when no accepted lights)
//   Readiness.chip(entries, synced) -> compact HTML for a Targets card
//   Readiness.detail(entry, synced, copyBtn) -> fuller HTML for the target
//       page: per-filter lights / flats, darks, bias, desktop sync and the
//       command (copy button), the legacy ps1 staging script as a hint
(function () {
    'use strict';
    const RIG = {piggyback: 'Piggy-600', rc16: 'RC16'};
    const OK = '#4ade80', PEND = '#eab308';

    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }
    function key(name) {
        return String(name || '').toLowerCase().replace(/\u2019/g, "'")
            .replace(/[^0-9a-z]+/g, '');
    }
    function rig(t) { return (t && t.rig) || 'rc16'; }
    function rigLabel(t) { return RIG[rig(t)] || rig(t); }
    function command(t) {
        return 'photonscript integrate --target "' + t.target + '" --rig ' + rig(t);
    }
    function lights(t) {
        return Object.values(t.filters || {})
            .reduce((a, v) => a + (v.accepted_in_library || 0), 0);
    }
    function counts(t) {
        return Object.entries(t.filters || {})
            .filter(([f, v]) => v.accepted_in_library > 0)
            .map(([f, v]) => f + ' ' + v.accepted_in_library).join(' ');
    }
    // lights accepted + Library synced = on the desktop, ready to stack
    function state(t, synced) {
        const n = lights(t);
        if (!n) return ['none', 'no accepted lights yet', ''];
        return synced ? ['ready', 'ready', OK] : ['syncing', 'sync in progress', PEND];
    }

    function chip(entries, synced) {
        if (!entries || !entries.length) return '';
        return entries.map(t => {
            const [s, word, col] = state(t, synced);
            const c = counts(t);
            return '<div title="integration readiness (' + esc(rigLabel(t)) + '): ' +
                'accepted lights in the Library per filter">' +
                esc(rigLabel(t)) + ' ' + (s === 'none'
                    ? '<span style="opacity:.6;">' + word + '</span>'
                    : esc(c) + ' <b style="color:' + col + ';">' + word + '</b>') +
                '</div>';
        }).join('');
    }

    function detail(t, synced, copyBtn) {
        const FC = (window.SubTiles && SubTiles.FILTER_COLORS) || {};
        const [s, word, col] = state(t, synced);
        const fl = Object.entries(t.filters || {}).map(([f, v]) =>
            '<b style="color:' + (FC[f] || '#94a3b8') + ';">' + esc(f) + '</b> ' +
            v.accepted_in_library + '/' + v.planned + ' lights \u00b7 ' + v.flats +
            ' flats').join(' &nbsp;\u00b7&nbsp; ');
        const dk = Object.entries(t.darks_by_exposure || {}).map(([e, n]) =>
            e + ': ' + n).join(', ');
        const cmd = command(t);
        const cp = copyBtn || (() => '');
        return '<div style="margin-bottom:.8rem;">' +
            '<div><b>' + esc(rigLabel(t)) + '</b> \u00b7 <b class="' +
            (t.ready ? 'ok' : 'pend') + '">' +
            (t.ready ? 'ready to integrate' : 'not ready yet') + '</b> \u00b7 ' +
            t.lights_in_library + ' lights in the Library \u00b7 darks ' + esc(dk || '-') +
            ' \u00b7 bias ' + t.bias + '</div>' +
            '<div style="margin:.3rem 0;">' + (fl || '<span class="dim">no planned ' +
                'filters</span>') + '</div>' +
            '<div>desktop sync: ' + (synced
                ? '<span style="color:' + OK + ';">\u2713 Library synced</span>'
                : '<span style="color:' + PEND + ';">\u23f3 sync in progress</span>') +
            ' \u00b7 ' + (s === 'none' ? '<span class="dim">' + word + '</span>'
                : '<b style="color:' + col + ';">lights ' + (s === 'ready'
                    ? 'ready on desktop' : 'accepted, sync in progress') + '</b>') +
            '</div>' +
            '<div style="margin-top:.3rem;"><code class="rdy-cmd">' + esc(cmd) + '</code> ' +
            cp('copy command', cmd) + '</div>' +
            (t.command ? '<div class="dim" style="margin-top:.2rem;font-size:.75rem;">' +
                'legacy staging script (WBPP by hand): <code>' + esc(t.command) +
                '</code> ' + cp('copy', t.command) + '</div>' : '') +
            '</div>';
    }

    async function load() {
        const [rd, sync] = await Promise.all([
            fetch('/api/integration/readiness').then(r => r.json()),
            fetch('/api/sync').then(r => r.json()).catch(() => ({}))]);
        const byKey = {};
        (rd.targets || []).forEach(t => {
            (byKey[key(t.target)] = byKey[key(t.target)] || []).push(t);
        });
        return {byKey: byKey, synced: sync.library_synced === true};
    }

    window.Readiness = {load: load, key: key, rig: rig, command: command,
                        counts: counts, chip: chip, detail: detail};
})();
