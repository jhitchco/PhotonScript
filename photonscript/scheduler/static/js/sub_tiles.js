// PS-81: sub thumbnail tiles for the Targets page (rows from /api/subs).
// PS-80 plans the same tile for the runs grid: whichever lands second adopts
// this file instead of keeping its own copy in runs.html.
//
//   SubTiles.html(row, {best: bool, canonical: "Heart Nebula"}) -> HTML string
//   SubTiles.lazy(container, maxInFlight = 2)  starts loading the container's
//       <img data-src> tiles as they scroll into view, at most maxInFlight
//       thumbnail requests at a time (thumbnails serialize on the scope PC).
(function () {
    'use strict';
    const FILTER_COLORS = {Ha: '#f87171', OIII: '#22d3ee', SII: '#eab308',
                           L: '#e2e8f0', R: '#f87171', G: '#4ade80',
                           B: '#60a5fa', OSC: '#c084fc'};
    const VERDICT_STYLE = {
        approved: 'outline:2px solid #4ade80;',
        pending: 'outline:2px dashed #eab308;',
        rejected: 'outline:2px solid #f87171;opacity:.55;'
    };
    function esc(s) {
        return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
            "'": '&#39;'}[c]));
    }
    function num(v, d) {
        return v == null ? '\u2013' : Number(v).toFixed(d);
    }
    function html(r, opts) {
        opts = opts || {};
        const m = r.metrics || {};
        const tip = [r.date + ' ' + (r.time || '').slice(11, 19) + ' UTC',
                     r.rig + ' \u00b7 ' + r.filter + ' ' + Math.round(r.exp_s || 0) + 's',
                     r.verdict + ' (' + r.verdict_by + ')']
            .concat(r.reasons || []);
        if (opts.canonical && r.target_raw && r.target_raw !== opts.canonical) {
            tip.push('recorded as: ' + r.target_raw);
        }
        const fc = FILTER_COLORS[r.filter] || '#94a3b8';
        return '<figure class="sub-tile v-' + esc(r.verdict) + '">' +
            (opts.best ? '<span class="best-star" title="best sub for ' +
             esc(r.rig + ' ' + r.filter) + ' (lowest HFR, round stars)">&#9733;</span>' : '') +
            '<a href="/runs/' + esc(r.date) + '" title="' + esc(tip.join('\n')) + '">' +
            '<img alt="" data-src="' + esc(r.thumb) + '" style="' +
            (VERDICT_STYLE[r.verdict] || '') + '"></a>' +
            '<figcaption>' + esc(r.date.slice(5)) + ' ' +
            esc((r.time || '').slice(11, 16)) + ' \u00b7 <b style="color:' + fc + ';">' +
            esc(r.filter) + '</b> ' + Math.round(r.exp_s || 0) + 's' +
            (r.hdr_short ? ' <span title="HDR short sub">HDR</span>' : '') +
            '<br>HFR ' + num(m.hfr, 2) + ' \u00b7 ecc ' + num(m.ecc, 2) +
            ' \u00b7 &#9733;' + (m.stars == null ? '\u2013' : Math.round(m.stars)) +
            (r.verdict === 'rejected' && (r.reason_codes || []).length
             ? '<br><span class="rej">' + esc(r.reason_codes.join(', ')) + '</span>' : '') +
            (opts.canonical && r.target_raw && r.target_raw !== opts.canonical
             ? '<br><span class="dim" title="' + esc(r.target_raw) + '">as ' +
               esc(r.target_raw.slice(0, 22)) + (r.target_raw.length > 22 ? '\u2026' : '') +
               '</span>' : '') +
            '</figcaption></figure>';
    }
    function lazy(container, maxInFlight) {
        const limit = maxInFlight || 2;
        let inFlight = 0;
        const queue = [];
        function pump() {
            while (inFlight < limit && queue.length) {
                const img = queue.shift();
                if (!img.isConnected || img.src) continue;
                inFlight++;
                const done = () => { inFlight--; pump(); };
                img.onload = done;
                img.onerror = () => { img.style.opacity = '.25'; done(); };
                img.src = img.dataset.src;
            }
        }
        const imgs = Array.from(container.querySelectorAll('img[data-src]'));
        if (!('IntersectionObserver' in window)) {
            imgs.forEach(i => queue.push(i));
            pump();
            return;
        }
        const io = new IntersectionObserver(entries => {
            entries.forEach(e => {
                if (e.isIntersecting) {
                    io.unobserve(e.target);
                    queue.push(e.target);
                }
            });
            pump();
        }, {rootMargin: '200px'});
        imgs.forEach(i => io.observe(i));
    }
    window.SubTiles = {html: html, lazy: lazy, esc: esc, FILTER_COLORS: FILTER_COLORS};
})();
