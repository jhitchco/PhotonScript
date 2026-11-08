// 2026-11-08: the noon-to-noon altitude chart, shared by the dashboard
// (tonight's schedule cards) and the Targets page (target.html, "In the sky
// tonight"). Data: GET /api/target/altitude?name=&ra_hours=&dec_degrees=
// (15-min grid from local noon, astro-dark fractions, now, transit, 30 deg
// crossings).
//
//   altChartSVG(d, opts)   -> SVG string. opts.fluid: scale to the container
//                             width (viewBox) instead of the fixed 300 px.
//   AltChart.summary(d)    -> {now_alt, transit_local, up_from, up_to, ...}
//                             for the one-line text under the chart.
(function () {
    'use strict';

    function altChartSVG(d, opts) {
        opts = opts || {};
        const W = 300, H = 88, PAD_B = 14, n = d.alts.length;
        const plotH = H - PAD_B;
        const x = i => i / (n - 1) * W;
        const y = a => plotH - Math.max(0, Math.min(90, a)) / 90 * plotH;
        // Daytime base is deliberately lighter so the night block pops
        let svg = opts.fluid
            ? '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" ' +
              'preserveAspectRatio="xMidYMid meet" style="background:#1a2942;' +
              'border-radius:6px;display:block;">'
            : '<svg width="' + W + '" height="' + H +
              '" style="background:#1a2942;border-radius:6px;">';
        if (d.dark_start_frac !== null && d.dark_end_frac !== null) {
            const dx = d.dark_start_frac * W,
                  dw = (d.dark_end_frac - d.dark_start_frac) * W;
            svg += '<rect x="' + dx + '" y="0" width="' + dw + '" height="' + plotH +
                   '" fill="#060b18"/>' +
                   '<line x1="' + dx + '" y1="0" x2="' + dx + '" y2="' + plotH +
                   '" stroke="#334155" stroke-width="1"/>' +
                   '<line x1="' + (dx + dw) + '" y1="0" x2="' + (dx + dw) + '" y2="' + plotH +
                   '" stroke="#334155" stroke-width="1"/>' +
                   '<text x="' + (dx + dw / 2) + '" y="10" fill="#475569" font-size="8" ' +
                   'text-anchor="middle">NIGHT</text>';
        }
        // altitude gridlines 30/60 with labels
        for (const g of [30, 60])
            svg += '<line x1="0" y1="' + y(g) + '" x2="' + W + '" y2="' + y(g) +
                   '" stroke="#475569" stroke-dasharray="3,3" opacity="' +
                   (g === 30 ? '1' : '.4') + '"/>' +
                   '<text x="2" y="' + (y(g) - 2) + '" fill="#64748b" font-size="7">' +
                   g + '°</text>';
        svg += '<polyline fill="none" stroke="#7dd3fc" stroke-width="1.8" points="' +
               d.alts.map((a, i) => x(i) + ',' + y(a)).join(' ') + '"/>';
        // 30° crossings: markers + local times below the axis
        for (const c of (d.cross30 || [])) {
            const cx = c.frac * W;
            svg += '<circle cx="' + cx + '" cy="' + y(30) + '" r="3" fill="#fbbf24"/>' +
                   '<text x="' + cx + '" y="' + (H - 3) + '" fill="#fbbf24" font-size="8" ' +
                   'text-anchor="middle">' + (c.dir === 'up' ? '↑' : '↓') + c.local + '</text>';
        }
        if (d.now_frac !== null && d.now_frac !== undefined) {
            svg += '<line x1="' + d.now_frac * W + '" y1="0" x2="' + d.now_frac * W +
                   '" y2="' + plotH + '" stroke="#e2e8f0" stroke-dasharray="2,3" opacity=".7"/>';
            if (opts.nowDot) {
                const a = altAt(d, d.now_frac);
                svg += '<circle cx="' + d.now_frac * W + '" cy="' + y(a) +
                       '" r="3" fill="#e2e8f0" stroke="#0f172a" stroke-width="1"/>';
            }
        }
        // meridian-flip tick: the target crosses the meridian at transit, so the
        // mount flips sides of the pier here and imaging resumes on the far side.
        if (d.transit_frac !== null && d.transit_frac !== undefined) {
            const mfx = d.transit_frac * W;
            const lblx = Math.max(14, Math.min(mfx, W - 16));
            svg += '<line x1="' + mfx + '" y1="0" x2="' + mfx + '" y2="' + plotH +
                   '" stroke="#f472b6" stroke-width="1" stroke-dasharray="3,2" opacity=".6"/>' +
                   '<text x="' + lblx + '" y="' + (plotH - 3) + '" fill="#f472b6" ' +
                   'font-size="8" text-anchor="middle">⇄ flip</text>';
        }
        // transit dot + altitude label
        svg += '<circle cx="' + d.transit_frac * W + '" cy="' + y(d.transit_alt) +
               '" r="2.5" fill="#e2e8f0"/>' +
               '<text x="' + Math.min(d.transit_frac * W + 5, W - 22) + '" y="' +
               (y(d.transit_alt) - 4) + '" fill="#e2e8f0" font-size="9">' +
               d.transit_alt + '°</text>';
        svg += '</svg>';
        return svg;
    }

    // altitude at a 0..1 position along the noon-to-noon axis (linear)
    function altAt(d, frac) {
        const n = d.alts.length, p = Math.max(0, Math.min(1, frac)) * (n - 1);
        const i = Math.min(n - 2, Math.floor(p)), t = p - i;
        return d.alts[i] + (d.alts[i + 1] - d.alts[i]) * t;
    }

    // local clock time of an axis fraction (the axis starts at local noon)
    function localAt(d, frac) {
        let mins = Math.round(((d.start_local_hour || 12) + frac * 24) * 60) % 1440;
        const h = Math.floor(mins / 60), m = mins % 60;
        const h12 = h % 12 === 0 ? 12 : h % 12;
        return h12 + ':' + String(m).padStart(2, '0') + (h < 12 ? ' AM' : ' PM');
    }

    function summary(d) {
        const out = {transit_alt: d.transit_alt,
                     transit_local: localAt(d, d.transit_frac)};
        if (d.now_frac !== null && d.now_frac !== undefined)
            out.now_alt = Math.round(altAt(d, d.now_frac));
        const c = d.cross30 || [];
        const up = c.find(x => x.dir === 'up'), down = c.find(x => x.dir === 'down');
        out.up_from = up ? up.local : null;
        out.up_to = down ? down.local : null;
        out.never_up = !c.length && d.transit_alt < (d.min_altitude || 30);
        out.always_up = !c.length && !out.never_up;
        return out;
    }

    window.altChartSVG = altChartSVG;
    window.AltChart = {svg: altChartSVG, altAt: altAt, localAt: localAt,
                       summary: summary};
})();
