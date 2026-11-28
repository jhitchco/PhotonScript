// PS-80 / PS-6 / PS-17: review viewer tools for the runs-page lightbox.
//
//   Star overlay (PS-80, S key): the grader's own stars from the PS-80 star
//     sidecar (GET /api/runs/{date}/stars) drawn as an SVG over the preview,
//     viewBox = native frame, so it follows the PS-16 click / wheel zoom.
//     Circles about 2 x HFR, colored by HFR or by ecc (C key): green below
//     90% of the rig's gate, yellow up to the gate, red above (gates from
//     GET /api/qa/thresholds). Colored by ecc the marks are ellipses turned
//     by theta, so the stretch direction shows. Hover a star: HFR, ecc, x, y.
//   Loupe (PS-6, L key): hover shows a 256 px full-resolution crop under the
//     cursor (GET /api/runs/{date}/crop, asked for after the cursor pauses
//     120 ms), click pins it, 1:1 / 2:1 toggle, crosshair, native x / y and
//     the nearest star's HFR / ecc. OSC subs show the 2x2 superpixel.
//   3x3 (PS-17, M key): corners, edge midpoints and center at 1:1 in one
//     server-composed PNG (GET /api/runs/{date}/mosaic), 128 / 256 px tiles,
//     per-tile HFR / ecc from the star sidecar (GET .../mosaic-info).
//
// Self-contained: it hooks the lightbox by watching #lbImg (src = the sub,
// style = the PS-16 zoom), so the page script needs no calls into it. State
// (overlay on, color mode, loupe scale, tile size) is remembered per
// browser. Pure ASCII; no framework, no CDN.
(function () {
    'use strict';
    const NS = 'http://www.w3.org/2000/svg';
    const COL = {good: '#4ade80', near: '#eab308', bad: '#f87171', none: '#94a3b8'};
    const LOUPE_PX = 256, PAUSE_MS = 120, STARS_DELAY_MS = 200;

    function lsGet(k, d) {
        try { const v = localStorage.getItem('pv.' + k); return v === null ? d : v; }
        catch (e) { return d; }
    }
    function lsSet(k, v) {
        try { localStorage.setItem('pv.' + k, String(v)); } catch (e) { /* no storage */ }
    }

    const st = {
        on: lsGet('stars', '1') === '1',
        mode: lsGet('color', 'hfr') === 'ecc' ? 'ecc' : 'hfr',
        loupe: false, pinned: false,
        loupeScale: lsGet('loupeScale', '1') === '2' ? 2 : 1,
        mosaic: false,
        mosaicSize: lsGet('mosaicSize', '256') === '128' ? 128 : 256,
        date: null, file: null, stars: null, starsNote: '', frame: null,
        seq: 0
    };
    const thrCache = {};
    let img, wrap, svg, bar, legend, tip, loupe, loupeImg, loupeTxt, mos;
    let starsTimer = null, loupeTimer = null, loupeSeq = 0, loupeUrl = null;
    let lastFx = 0.5, lastFy = 0.5;

    function el(tag, css, text) {
        const e = document.createElement(tag);
        if (css) e.style.cssText = css;
        if (text != null) e.textContent = text;
        return e;
    }
    function btn(label, title, fn) {
        const b = el('button', '', label);
        b.type = 'button';
        b.className = 'btn btn-secondary';
        b.title = title;
        b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
        return b;
    }
    function lightboxOpen() {
        const lb = document.getElementById('lightbox');
        return lb && !lb.hidden;
    }
    function rec() {
        const list = window._shownSubs || [];
        for (let i = 0; i < list.length; i++) {
            if (list[i] && list[i].file === st.file) return list[i];
        }
        return null;
    }
    function rigOf() { const r = rec(); return (r && r.rig) || ''; }
    function fmt(v, nd) { return v == null ? 'n/a' : Number(v).toFixed(nd); }

    // ------------------------------------------------------------ the sub
    function parseSrc(src) {
        try {
            const u = new URL(src, window.location.href);
            const m = u.pathname.match(/^\/api\/runs\/([^/]+)\/thumb$/);
            if (!m) return null;
            return {date: decodeURIComponent(m[1]), file: u.searchParams.get('file')};
        } catch (e) { return null; }
    }
    function onSrc() {
        const p = parseSrc(img.getAttribute('src') || '');
        if (!p || !p.file) return;
        if (p.date === st.date && p.file === st.file) return;
        st.date = p.date; st.file = p.file;
        st.stars = null; st.frame = null; st.starsNote = '';
        st.seq += 1;
        st.pinned = false;
        hideLoupe();
        draw();
        clearTimeout(starsTimer);
        if (st.on) starsTimer = setTimeout(loadStars, STARS_DELAY_MS);
        if (st.mosaic) showMosaic();
    }

    // ------------------------------------------------------- PS-80 overlay
    async function loadThresholds() {
        const r = rec() || {};
        const key = [r.rig || 'rc16', r.target || '', r.filter || ''].join('|');
        if (thrCache[key]) return thrCache[key];
        try {
            const q = '?rig=' + encodeURIComponent(r.rig || 'rc16') +
                '&target=' + encodeURIComponent(r.target || '') +
                '&filter=' + encodeURIComponent(r.filter || '');
            const j = await (await fetch('/api/qa/thresholds' + q)).json();
            const t = j.thresholds || {};
            thrCache[key] = {hfr: t.hfr_max, ecc: t.ecc_max};
        } catch (e) { thrCache[key] = {hfr: null, ecc: null}; }
        return thrCache[key];
    }
    async function loadStars() {
        if (!st.file || st.stars) return;
        const seq = st.seq, date = st.date, file = st.file;
        const slow = setTimeout(() => {
            if (seq === st.seq) { st.starsNote = 'measuring stars (first view of this sub)...'; paintLegend(null); }
        }, 600);
        let t = null, why = '';
        try {
            const rig = rigOf();
            const resp = await fetch('/api/runs/' + encodeURIComponent(date) + '/stars?file=' +
                encodeURIComponent(file) + (rig ? '&rig=' + encodeURIComponent(rig) : ''));
            if (resp.ok) t = await resp.json();
            else why = resp.status === 404 ? 'no star data (FITS not on this PC)' : 'star data unavailable';
        } catch (e) { why = 'star data unavailable'; }
        clearTimeout(slow);
        if (seq !== st.seq) return;
        st.thr = await loadThresholds();
        if (seq !== st.seq) return;
        st.stars = t;
        if (t && t.w && t.h) st.frame = {w: t.w, h: t.h};
        st.starsNote = why;
        draw();
        if (t && t.on_view && st.mosaic) showMosaic();   // readouts now exist
    }
    function cls(v, lim) {
        if (v == null || !lim) return 'none';
        if (v < 0.9 * lim) return 'good';
        return v <= lim ? 'near' : 'bad';
    }
    function draw() {
        if (!svg) return;
        while (svg.firstChild) svg.removeChild(svg.firstChild);
        const t = st.stars;
        if (!st.on || !t || !t.w || !t.h || !t.x) { paintLegend(null); return; }
        svg.setAttribute('viewBox', '0 0 ' + t.w + ' ' + t.h);
        const lim = (st.thr || {})[st.mode];
        const perPx = (img.clientWidth || 1) / t.w;      // screen px per native px at 1x
        const floor = 4 / perPx;
        const counts = {good: 0, near: 0, bad: 0, none: 0};
        const frag = document.createDocumentFragment();
        const n = Math.min(t.x.length, t.y.length);
        for (let i = 0; i < n; i++) {
            const x = t.x[i], y = t.y[i];
            if (x == null || y == null) continue;
            const hfr = t.hfr ? t.hfr[i] : null, ecc = t.ecc ? t.ecc[i] : null;
            const th = t.theta ? t.theta[i] : null;
            const c = cls(st.mode === 'ecc' ? ecc : hfr, lim);
            counts[c] += 1;
            const r = Math.max(2 * (hfr || 2), floor);
            let m;
            if (st.mode === 'ecc' && ecc != null && th != null) {
                m = document.createElementNS(NS, 'ellipse');
                m.setAttribute('rx', r.toFixed(1));
                m.setAttribute('ry', Math.max(r * Math.sqrt(Math.max(0, 1 - ecc * ecc)), r * 0.25).toFixed(1));
                m.setAttribute('cx', x); m.setAttribute('cy', y);
                m.setAttribute('transform', 'rotate(' + (th * 180 / Math.PI).toFixed(1) + ' ' + x + ' ' + y + ')');
            } else {
                m = document.createElementNS(NS, 'circle');
                m.setAttribute('r', r.toFixed(1));
                m.setAttribute('cx', x); m.setAttribute('cy', y);
            }
            m.setAttribute('fill', 'none');
            m.setAttribute('stroke', COL[c]);
            m.setAttribute('stroke-width', '1.5');
            m.setAttribute('vector-effect', 'non-scaling-stroke');
            frag.appendChild(m);
        }
        svg.appendChild(frag);
        paintLegend(counts, lim);
    }
    function paintLegend(counts, lim) {
        if (!legend) return;
        legend.textContent = '';
        if (!st.on) { legend.appendChild(el('span', 'opacity:.6', 'star overlay off')); return; }
        if (!counts) {
            legend.appendChild(el('span', 'opacity:.6', st.starsNote || (st.file ? 'loading stars...' : '')));
            return;
        }
        const t = st.stars, unit = st.mode === 'ecc' ? '' : ' px';
        legend.appendChild(el('span', '', (t.n || t.x.length) + ' stars by ' +
            (st.mode === 'ecc' ? 'ecc' : 'HFR') + ': '));
        const parts = [['good', lim ? '< ' + fmt(0.9 * lim, 2) + unit : ''],
                       ['near', lim ? 'to ' + fmt(lim, 2) + unit : ''],
                       ['bad', lim ? '> ' + fmt(lim, 2) + unit : '']];
        parts.forEach(([k, txt]) => {
            legend.appendChild(el('b', 'color:' + COL[k] + ';margin-left:.4rem', String(counts[k])));
            if (txt) legend.appendChild(el('span', 'opacity:.7;margin-left:.2rem', txt));
        });
        if (!lim) legend.appendChild(el('span', 'opacity:.6;margin-left:.4rem', '(no gate for this rig)'));
        const src = [t.grader || '', t.on_view ? 'measured on view' : ''].filter(Boolean).join(', ');
        if (src) legend.appendChild(el('span', 'opacity:.55;margin-left:.6rem', src));
    }

    // native px under the cursor (getBoundingClientRect includes the zoom)
    function nativeAt(e) {
        const f = st.frame;
        const r = img.getBoundingClientRect();
        if (!r.width || !r.height) return null;
        const fx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
        const fy = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
        return {fx: fx, fy: fy, perPx: f ? r.width / f.w : null,
                x: f ? fx * f.w : null, y: f ? fy * f.h : null};
    }
    function nearestStar(x, y, maxD) {
        const t = st.stars;
        if (!t || !t.x || x == null) return null;
        let best = -1, bd = maxD * maxD;
        for (let i = 0; i < t.x.length; i++) {
            if (t.x[i] == null) continue;
            const dx = t.x[i] - x, dy = t.y[i] - y, d = dx * dx + dy * dy;
            if (d < bd) { bd = d; best = i; }
        }
        if (best < 0) return null;
        return {x: t.x[best], y: t.y[best], hfr: t.hfr ? t.hfr[best] : null,
                ecc: t.ecc ? t.ecc[best] : null};
    }
    function starText(s) {
        return 'HFR ' + fmt(s.hfr, 2) + ' px, ecc ' + fmt(s.ecc, 2) +
            ' at ' + Math.round(s.x) + ', ' + Math.round(s.y);
    }
    function onMove(e) {
        const p = nativeAt(e);
        if (!p) return;
        lastFx = p.fx; lastFy = p.fy;
        if (st.loupe && !st.pinned) {
            placeLoupe(e.clientX, e.clientY);
            clearTimeout(loupeTimer);
            loupeTimer = setTimeout(() => fetchCrop(p.fx, p.fy), PAUSE_MS);
        }
        if (st.on && st.stars && p.x != null && !st.loupe) {
            const s = nearestStar(p.x, p.y, Math.max(12 / (p.perPx || 1), 6));
            if (s) {
                tip.textContent = starText(s);
                tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 260) + 'px';
                tip.style.top = (e.clientY + 14) + 'px';
                tip.hidden = false;
                return;
            }
        }
        tip.hidden = true;
    }

    // ----------------------------------------------------------- PS-6 loupe
    function placeLoupe(cx, cy) {
        const W = LOUPE_PX + 12, H = LOUPE_PX + 52;
        let x = cx + 24, y = cy - H / 2;
        if (x + W > window.innerWidth - 8) x = cx - W - 24;
        y = Math.max(8, Math.min(y, window.innerHeight - H - 8));
        loupe.style.left = Math.max(8, x) + 'px';
        loupe.style.top = y + 'px';
        loupe.hidden = false;
    }
    function hideLoupe() {
        if (loupe) loupe.hidden = true;
        clearTimeout(loupeTimer);
    }
    async function fetchCrop(fx, fy) {
        if (!st.file) return;
        const seq = ++loupeSeq, file = st.file;
        loupeTxt.textContent = 'loading...';
        let resp;
        try {
            resp = await fetch('/api/runs/' + encodeURIComponent(st.date) + '/crop?file=' +
                encodeURIComponent(file) + '&fx=' + fx.toFixed(4) + '&fy=' + fy.toFixed(4) +
                '&size=' + LOUPE_PX + '&scale=' + st.loupeScale);
        } catch (e) { if (seq === loupeSeq) loupeTxt.textContent = 'crop unavailable'; return; }
        if (seq !== loupeSeq || file !== st.file) return;
        if (!resp.ok) {
            loupeTxt.textContent = resp.status === 404 ? 'no FITS on this PC: no full-res crop'
                : resp.status === 503 ? 'busy, move again' : 'crop unavailable';
            return;
        }
        const blob = await resp.blob();
        if (seq !== loupeSeq) return;
        const h = (k) => Number(resp.headers.get(k));
        const x0 = h('X-Crop-X0'), y0 = h('X-Crop-Y0'), n = h('X-Crop-N');
        if (!st.frame && h('X-Frame-W')) st.frame = {w: h('X-Frame-W'), h: h('X-Frame-H')};
        if (loupeUrl) URL.revokeObjectURL(loupeUrl);
        loupeUrl = URL.createObjectURL(blob);
        loupeImg.src = loupeUrl;
        const cx = x0 + n / 2, cy = y0 + n / 2;
        let txt = 'x ' + Math.round(cx) + ', y ' + Math.round(cy) + ' (' + st.loupeScale + ':1' +
            (resp.headers.get('X-Crop-OSC') === '1' ? ', OSC superpixel' : '') + ')';
        const s = nearestStar(cx, cy, n / 2);
        if (s) txt += ' | nearest star ' + starText(s);
        loupeTxt.textContent = txt;
    }
    function onClickCapture(e) {
        if (!st.loupe) return;              // PS-16 click-to-zoom stays as is
        e.stopPropagation(); e.preventDefault();
        st.pinned = !st.pinned;
        loupe.style.borderColor = st.pinned ? '#38bdf8' : '#334155';
        if (!st.pinned) onMove(e);
        else {
            placeLoupe(e.clientX, e.clientY);
            const p = nativeAt(e);
            if (p) fetchCrop(p.fx, p.fy);
        }
        paintBar();
    }
    function toggleLoupe() {
        st.loupe = !st.loupe; st.pinned = false;
        if (!st.loupe) hideLoupe();
        else tip.hidden = true;
        loupe.style.borderColor = '#334155';
        wrap.style.cursor = st.loupe ? 'crosshair' : '';
        img.style.cursor = st.loupe ? 'crosshair' : '';
        paintBar();
    }

    // ------------------------------------------------------------ PS-17 3x3
    async function showMosaic() {
        if (!st.file) return;
        mos.hidden = false;
        const size = st.mosaicSize, date = st.date, file = st.file, seq = st.seq;
        const box = mos.querySelector('.pv-mbox'), note = mos.querySelector('.pv-mnote');
        box.textContent = '';
        note.textContent = 'loading 3x3...';
        const q = '?file=' + encodeURIComponent(file) + '&size=' + size;
        const rig = rigOf();
        let info = null;
        try {
            const r = await fetch('/api/runs/' + encodeURIComponent(date) + '/mosaic-info' + q +
                (rig ? '&rig=' + encodeURIComponent(rig) : ''));
            if (r.ok) info = await r.json();
        } catch (e) { info = null; }
        if (seq !== st.seq || !st.mosaic) return;
        if (!info || !info.fits) {
            note.textContent = 'no FITS on this PC: no 3x3 from the full-res frame';
            return;
        }
        const pic = el('img', 'display:block;max-width:none;max-height:none;border-radius:0;cursor:default;' +
            'width:' + (3 * size + 4) + 'px;height:' + (3 * size + 4) + 'px;');
        pic.alt = '3x3 corners, edges and center at 1:1';
        pic.onerror = () => { note.textContent = '3x3 unavailable (busy? press M twice to retry)'; };
        pic.src = '/api/runs/' + encodeURIComponent(date) + '/mosaic' + q;
        box.appendChild(pic);
        const edge = 3 * size + 4;
        (info.tiles || []).forEach((tl, i) => {
            const z = (info.zones || [])[i] || null;
            const r = Math.floor(i / 3), c = i % 3;
            let txt = tl.zone;
            if (z && (z.hfr != null || z.ecc != null)) {
                txt += (z.hfr != null ? ' HFR ' + fmt(z.hfr, 2) : '') +
                    (z.ecc != null ? ' ecc ' + fmt(z.ecc, 2) : '') +
                    (z.n != null ? ' (' + z.n + ')' : '');
            }
            const lab = el('div', 'position:absolute;padding:1px 4px;font-size:11px;line-height:1.3;' +
                'background:rgba(2,6,23,.72);color:#e2e8f0;border-radius:3px;pointer-events:none;' +
                'left:' + ((c * (size + 2)) / edge * 100) + '%;top:' + ((r * (size + 2)) / edge * 100) + '%;', txt);
            lab.title = 'tile at x ' + tl.x0 + ', y ' + tl.y0;
            box.appendChild(lab);
        });
        note.textContent = info.w + ' x ' + info.h + ' px frame, ' + size + ' px tiles at 1:1' +
            (info.osc ? ', OSC superpixel' : '') + ' | readouts: ' +
            (info.source === 'stars' ? 'median of the zone\'s stars (star sidecar' +
                (info.on_view ? ', measured on view' : '') + ')'
             : info.source === 'corner_ecc' ? 'corner ecc of the backfill grader'
             : 'none (no star data for this sub)');
    }
    function toggleMosaic() {
        st.mosaic = !st.mosaic;
        if (st.mosaic) showMosaic(); else mos.hidden = true;
        paintBar();
    }

    // ---------------------------------------------------------------- bar
    let bStars, bMode, bLoupe, bScale, bMos;
    function paintBar() {
        if (!bar) return;
        bStars.textContent = 'Stars ' + (st.on ? 'on' : 'off') + ' (S)';
        bMode.textContent = 'Color: ' + (st.mode === 'ecc' ? 'ecc' : 'HFR') + ' (C)';
        bLoupe.textContent = 'Loupe ' + (st.loupe ? (st.pinned ? 'pinned' : 'on') : 'off') + ' (L)';
        bScale.textContent = st.loupeScale + ':1';
        bMos.textContent = '3x3 ' + (st.mosaic ? 'on' : 'off') + ' (M)';
        const sz = mos && mos.querySelector('.pv-msize');
        if (sz) sz.textContent = st.mosaicSize + ' px tiles';
    }
    function toggleStars() {
        st.on = !st.on; lsSet('stars', st.on ? '1' : '0');
        if (st.on && !st.stars) loadStars();
        draw(); paintBar();
    }
    function toggleMode() {
        st.mode = st.mode === 'ecc' ? 'hfr' : 'ecc'; lsSet('color', st.mode);
        draw(); paintBar();
    }
    function toggleScale() {
        st.loupeScale = st.loupeScale === 2 ? 1 : 2; lsSet('loupeScale', st.loupeScale);
        if (st.loupe && !loupe.hidden) fetchCrop(lastFx, lastFy);
        paintBar();
    }
    function toggleSize() {
        st.mosaicSize = st.mosaicSize === 256 ? 128 : 256; lsSet('mosaicSize', st.mosaicSize);
        if (st.mosaic) showMosaic();
        paintBar();
    }

    function typingIn(e) {
        const t = e.target;
        return t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' ||
                     t.tagName === 'SELECT' || t.isContentEditable);
    }
    function onKey(e) {
        if (!lightboxOpen() || typingIn(e) || e.ctrlKey || e.metaKey || e.altKey) return;
        const k = (e.key || '').toLowerCase();
        if (k === 's') toggleStars();
        else if (k === 'c') toggleMode();
        else if (k === 'l') toggleLoupe();
        else if (k === 'm') toggleMosaic();
    }

    // --------------------------------------------------------------- init
    function init() {
        img = document.getElementById('lbImg');
        const lb = document.getElementById('lightbox');
        if (!img || !lb || document.getElementById('pvWrap')) return;
        wrap = el('span', 'position:relative;display:inline-block;line-height:0;');
        wrap.id = 'pvWrap';
        img.parentNode.insertBefore(wrap, img);
        wrap.appendChild(img);
        svg = document.createElementNS(NS, 'svg');
        svg.id = 'pvStars';
        svg.setAttribute('preserveAspectRatio', 'none');
        svg.style.cssText = 'position:absolute;left:0;top:0;width:100%;height:100%;' +
            'pointer-events:none;overflow:visible;transition:transform .04s linear;';
        wrap.appendChild(svg);
        // follow the PS-16 zoom: same transform on the same box
        new MutationObserver((ms) => {
            for (const m of ms) {
                if (m.attributeName === 'src') onSrc();
                else if (m.attributeName === 'style') {
                    svg.style.transform = img.style.transform;
                    svg.style.transformOrigin = img.style.transformOrigin;
                }
            }
        }).observe(img, {attributes: true, attributeFilter: ['src', 'style']});
        img.addEventListener('load', draw);
        img.addEventListener('mousemove', onMove);
        img.addEventListener('mouseleave', () => {
            tip.hidden = true;
            if (st.loupe && !st.pinned) hideLoupe();
        });
        wrap.addEventListener('click', onClickCapture, true);
        window.addEventListener('resize', draw);

        bar = el('div', 'display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;font-size:.78rem;');
        bar.id = 'pvBar';
        bStars = btn('', 'PS-80: circles on the stars the grader measured (S)', toggleStars);
        bMode = btn('', 'color the marks by HFR or by eccentricity; ecc draws ellipses along the stretch (C)', toggleMode);
        bLoupe = btn('', 'PS-6: hover for a full-resolution crop, click to pin (L)', toggleLoupe);
        bScale = btn('', 'loupe 1:1 or 2:1 (nearest neighbor)', toggleScale);
        bMos = btn('', 'PS-17: corners, edges and center at 1:1 (M)', toggleMosaic);
        legend = el('span', 'opacity:.9');
        legend.id = 'pvLegend';
        [bStars, bMode, bLoupe, bScale, bMos, legend].forEach((b) => bar.appendChild(b));
        const lbBar = document.getElementById('lbBar');
        if (lbBar && lbBar.parentNode === lb) lb.insertBefore(bar, lbBar.nextSibling);
        else lb.appendChild(bar);

        tip = el('div', 'position:fixed;z-index:70;pointer-events:none;background:#0f172a;color:#e2e8f0;' +
            'border:1px solid #334155;border-radius:4px;padding:2px 6px;font-size:.75rem;white-space:nowrap;');
        tip.hidden = true;
        lb.appendChild(tip);

        loupe = el('div', 'position:fixed;z-index:65;background:#020617;border:2px solid #334155;' +
            'border-radius:6px;padding:4px;width:' + (LOUPE_PX + 4) + 'px;');
        loupe.id = 'pvLoupe';
        loupe.hidden = true;
        const lbox = el('div', 'position:relative;width:' + LOUPE_PX + 'px;height:' + LOUPE_PX + 'px;' +
            'background:#000;line-height:0;');
        loupeImg = el('img', 'display:block;width:' + LOUPE_PX + 'px;height:' + LOUPE_PX + 'px;' +
            'max-width:none;max-height:none;border-radius:0;cursor:default;image-rendering:pixelated;');
        loupeImg.alt = 'full-resolution crop';
        lbox.appendChild(loupeImg);
        lbox.appendChild(el('div', 'position:absolute;left:50%;top:0;bottom:0;width:1px;background:rgba(56,189,248,.45);'));
        lbox.appendChild(el('div', 'position:absolute;top:50%;left:0;right:0;height:1px;background:rgba(56,189,248,.45);'));
        loupe.appendChild(lbox);
        loupeTxt = el('div', 'font-size:.7rem;line-height:1.3;margin-top:3px;color:#cbd5e1;');
        loupe.appendChild(loupeTxt);
        loupe.addEventListener('click', (e) => e.stopPropagation());
        lb.appendChild(loupe);

        mos = el('div', 'position:fixed;z-index:60;left:50%;top:50%;transform:translate(-50%,-50%);' +
            'background:#020617;border:1px solid #334155;border-radius:8px;padding:8px;' +
            'max-width:96vw;max-height:94vh;overflow:auto;');
        mos.id = 'pvMosaic';
        mos.hidden = true;
        const head = el('div', 'display:flex;gap:.5rem;align-items:center;margin-bottom:6px;font-size:.8rem;');
        head.appendChild(el('b', '', '3x3 at 1:1'));
        const bSize = btn('', 'tile size 128 or 256 px', toggleSize);
        bSize.className += ' pv-msize';
        head.appendChild(bSize);
        head.appendChild(btn('close (M)', 'back to the full frame', toggleMosaic));
        mos.appendChild(head);
        mos.appendChild(Object.assign(el('div', 'position:relative;display:inline-block;line-height:0;'),
                                      {className: 'pv-mbox'}));
        mos.appendChild(Object.assign(el('div', 'font-size:.72rem;margin-top:4px;color:#94a3b8;'),
                                      {className: 'pv-mnote'}));
        mos.addEventListener('click', (e) => e.stopPropagation());
        lb.appendChild(mos);

        document.addEventListener('keydown', onKey);
        paintBar();
        paintLegend(null);
        onSrc();
    }

    window.PSViewer = {state: st, redraw: draw, init: init};
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
