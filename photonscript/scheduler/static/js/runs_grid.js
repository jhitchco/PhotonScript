// 2026-10-24: Imaging Runs sub grid, grouped (rig, then target / filter,
// time order within). RC16 and Piggy-600 subs were one grid interleaved by
// time, unreadable on a two-rig night of ~670 subs.
//
//   RunsGrid.group(subs, {rigOf, stateOf}) -> [{rig, label, counts, groups:
//       [{target, filter, subs, counts}]}]; rigs RC16 first, then the
//       others by name; groups by target name ('?' last), then filter in
//       FILTER_ORDER; subs by time. counts = {total, accepted, review,
//       rejected, offtarget, hours}.
//   RunsGrid.flat(sections) -> the subs in drawn order (the lightbox and
//       shift-click selection walk this order).
//   RunsGrid.countsLine(counts) -> "295 subs . 4.9 h . 10 accepted ..." text.
//   RunsGrid.badge(s) -> {cls, text, title} | null: one short in-card badge
//       for the pointing / roof / unguided flags (the old caption tags ran
//       across neighbouring cards).
//   RunsGrid.loadingText(phase, info) -> the night loading line.
(function (root) {
    'use strict';
    const FILTER_ORDER = ['L', 'R', 'G', 'B', 'Ha', 'OIII', 'SII', 'OSC'];
    const RIG_LABEL = {rc16: 'RC16', piggyback: 'Piggy-600'};

    function rigLabel(r) { return RIG_LABEL[r] || r; }
    function filterRank(f) {
        const i = FILTER_ORDER.indexOf(f);
        return i < 0 ? FILTER_ORDER.length : i;
    }
    function targetOf(s) {
        return (s.target && s.target !== '?') ? s.target : '?';
    }
    function emptyCounts() {
        return {total: 0, accepted: 0, review: 0, rejected: 0, offtarget: 0,
                hours: 0};
    }
    function add(c, s, st) {
        c.total += 1;
        c.hours += (Number(s.exp_s) || 0) / 3600;
        if (st === 'accepted' || st === 'syncing') c.accepted += 1;
        else if (st === 'review') c.review += 1;
        else if (st === 'offtarget') { c.offtarget += 1; c.rejected += 1; }
        else c.rejected += 1;
    }
    function group(subs, opts) {
        opts = opts || {};
        const rigOf = opts.rigOf || (s => s.rig || 'rc16');
        const stateOf = opts.stateOf || (s => !s.passed_qa ? 'rejected'
            : !s.reviewed ? 'review' : 'accepted');
        const rigs = new Map();
        (subs || []).forEach(s => {
            const r = rigOf(s);
            if (!rigs.has(r)) rigs.set(r, {rig: r, label: rigLabel(r),
                                           counts: emptyCounts(), map: new Map()});
            const sec = rigs.get(r);
            const st = stateOf(s);
            add(sec.counts, s, st);
            const k = targetOf(s) + '\u0000' + (s.filter || '?');
            if (!sec.map.has(k)) sec.map.set(k, {target: targetOf(s),
                filter: s.filter || '?', subs: [], counts: emptyCounts()});
            const g = sec.map.get(k);
            g.subs.push(s);
            add(g.counts, s, st);
        });
        const out = Array.from(rigs.values()).sort((a, b) =>
            (a.rig === 'rc16' ? -1 : 0) - (b.rig === 'rc16' ? -1 : 0) ||
            (a.rig < b.rig ? -1 : a.rig > b.rig ? 1 : 0));
        out.forEach(sec => {
            sec.groups = Array.from(sec.map.values()).sort((a, b) =>
                ((a.target === '?') - (b.target === '?')) ||
                a.target.localeCompare(b.target) ||
                filterRank(a.filter) - filterRank(b.filter) ||
                a.filter.localeCompare(b.filter));
            sec.groups.forEach(g => g.subs.sort((a, b) =>
                String(a.time || '').localeCompare(String(b.time || '')) ||
                String(a.file || '').localeCompare(String(b.file || ''))));
            delete sec.map;
        });
        return out;
    }
    function flat(sections) {
        const out = [];
        (sections || []).forEach(sec => (sec.groups || []).forEach(g =>
            g.subs.forEach(s => out.push(s))));
        return out;
    }
    function countsLine(c) {
        const parts = [c.total + ' subs', c.hours.toFixed(1) + ' h'];
        if (c.accepted) parts.push(c.accepted + ' accepted');
        if (c.review) parts.push(c.review + ' to review');
        if (c.rejected) parts.push(c.rejected + ' rejected' +
            (c.offtarget ? ' (' + c.offtarget + ' off target)' : ''));
        return parts.join(' · ');
    }
    function fmtOff(x) {
        return x == null ? '' : (x < 120 ? Math.round(x) + "'" : Math.round(x / 60) + '°');
    }
    function badge(s) {
        const p = s.pointing;
        if (p && (p.flag === 'off-target' || p.flag === 'flag')) {
            const solved = p.solved_ra != null;
            const off = p.flag === 'off-target';
            return {cls: off ? 'off' : 'flag',
                    text: (off ? 'off ' : 'off? ') + fmtOff(p.off_target_arcmin) +
                          (p.off_target_dir ? ' ' + p.off_target_dir : ''),
                    title: (solved ? (off ? 'off target (plate solve)' : 'off target: worth a look (plate solve)')
                            : (off ? 'gross miss above the header-only reject limit (mount position, no plate solve)'
                                   : 'off target (header, unconfirmed): mount model error up to about 1 deg; the dawn plate solve confirms or clears it')) +
                           ' ' + fmtOff(p.off_target_arcmin) + ' ' + (p.off_target_dir || '') + ' (PS-67)'};
        }
        if (s.qa_flag === 'roof-closed') {
            return {cls: 'roof', text: 'roof', title: 'shot while the roof was closed or the mount parked (PS-71)'};
        }
        if (s.qa_flag === 'unguided-in-name' || s.guide_nocorr) {
            return {cls: 'flag', text: 'unguided', title: 'PHD2 said Guiding but sent no corrections during this sub (PS-155); judged on unguided limits (PS-165)'};
        }
        return null;
    }
    function kb(n) {
        return n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.round(n / 1024) + ' KB';
    }
    function loadingText(phase, info) {
        info = info || {};
        const secs = info.ms != null ? ' (' + (info.ms / 1000).toFixed(1) + ' s)' : '';
        if (phase === 'fetch') return 'Loading night ' + (info.date || '') + ': waiting for the scope PC' + secs;
        if (phase === 'body') return 'Loading night ' + (info.date || '') + ': ' + kb(info.bytes || 0) + ' received' + secs;
        if (phase === 'draw') return 'Drawing ' + (info.subs || 0) + ' subs' + secs;
        if (phase === 'error') return 'Could not load night ' + (info.date || '') + ': ' + (info.error || 'unknown error');
        return '';
    }
    const api = {group: group, flat: flat, countsLine: countsLine, badge: badge,
                 loadingText: loadingText, rigLabel: rigLabel,
                 FILTER_ORDER: FILTER_ORDER};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    else root.RunsGrid = api;
})(typeof window !== 'undefined' ? window : this);
