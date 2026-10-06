// PS-111: mosaic preview, shared by the dashboard goal card and the Mosaic
// page. Geometry comes from /api/mosaics (scheduler/mosaic.py summaries):
// every outline is a list of [east, north] tangent-plane degrees around the
// mosaic center. Drawn north up, east LEFT (as the DSS cutouts and the
// Targets page are).
//
//   MosaicPreview.svg(m, {w, h, wide})  -> SVG string
//       wide false: the panels (fill = progress, the next panel in amber)
//       wide true:  the Piggy-600 frame with the panel block inside it
//   MosaicPreview.points(corners, cx, cy, scale) -> "x,y x,y ..."
(function () {
    'use strict';
    function points(corners, cx, cy, s) {
        return corners.map(c => (cx - c[0] * s).toFixed(1) + ',' +
                                (cy - c[1] * s).toFixed(1)).join(' ');
    }
    function extent(polys) {
        let e = 1e-6, n = 1e-6;
        polys.forEach(p => p.forEach(c => {
            e = Math.max(e, Math.abs(c[0]));
            n = Math.max(n, Math.abs(c[1]));
        }));
        return [e, n];
    }
    function svg(m, opt) {
        opt = opt || {};
        const W = opt.w || 260, H = opt.h || 150, wide = !!opt.wide;
        const pv = m.preview || {};
        const panels = m.panels || [];
        const piggy = (pv.piggy || {}).corners || [];
        const polys = wide && piggy.length ? [piggy]
            : panels.map(p => p.corners).concat([pv.outline || []]);
        const ext = extent(polys);
        const s = Math.min((W / 2 - 6) / ext[0], (H / 2 - 6) / ext[1]);
        const cx = W / 2, cy = H / 2;
        const next = m.next_panel;
        let out = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W +
            '" height="' + H + '" style="background:#0b1220;border-radius:6px;">';
        if (wide && piggy.length) {
            out += '<polygon points="' + points(piggy, cx, cy, s) +
                '" fill="rgba(244,114,182,.06)" stroke="#f472b6" ' +
                'stroke-dasharray="4 3" stroke-width="1.2"/>' +
                '<text x="6" y="12" font-size="9" fill="#f472b6">Piggy-600 frame</text>';
        }
        panels.forEach(p => {
            const a = Math.max(0.06, Math.min(1, (p.pct || 0) / 100) * 0.55);
            const isNext = p.name === next;
            out += '<polygon points="' + points(p.corners, cx, cy, s) +
                '" fill="rgba(74,222,128,' + a.toFixed(2) + ')" stroke="' +
                (isNext ? '#fbbf24' : '#22d3ee') + '" stroke-width="' +
                (isNext ? 1.6 : 1) + '"/>';
            if (!wide) {
                const x = cx - p.east_deg * s, y = cy - p.north_deg * s;
                out += '<text x="' + x.toFixed(1) + '" y="' + (y + 3).toFixed(1) +
                    '" font-size="10" text-anchor="middle" fill="#e2e8f0">P' +
                    p.panel + ' ' + (p.pct || 0) + '%</text>';
            }
        });
        out += '<text x="' + (W - 6) + '" y="' + (H - 5) +
            '" font-size="8" text-anchor="end" fill="#64748b">N up, E left</text>';
        return out + '</svg>';
    }
    window.MosaicPreview = {svg: svg, points: points};
})();
