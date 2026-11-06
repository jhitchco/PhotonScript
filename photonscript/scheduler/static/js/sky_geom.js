// PS-81 / PS-115: sky geometry shared by the Targets page (target.html) and
// the runs-page lightbox Target section (runs.html). The Python mirror is
// scheduler/refimage.gnomonic_px, which the tests check.
//
//   SkyGeom.tanPx(ra, dec, ra0, dec0, fov, W, H) -> [x, y] or null
//       pixel of (ra, dec) (deg) on a TAN cutout centered on (ra0, dec0),
//       fov = the WIDTH in degrees, north up, east left; null behind the
//       tangent plane.
(function () {
    'use strict';
    function tanPx(ra, dec, ra0, dec0, fov, W, H) {
        const r = Math.PI / 180;
        const cosc = Math.sin(dec0 * r) * Math.sin(dec * r) +
            Math.cos(dec0 * r) * Math.cos(dec * r) * Math.cos((ra - ra0) * r);
        if (cosc <= 0) return null;
        const x = Math.cos(dec * r) * Math.sin((ra - ra0) * r) / cosc;
        const y = (Math.cos(dec0 * r) * Math.sin(dec * r) -
                   Math.sin(dec0 * r) * Math.cos(dec * r) * Math.cos((ra - ra0) * r)) / cosc;
        return [W / 2 - (x / r) * W / fov, H / 2 - (y / r) * W / fov];
    }
    window.SkyGeom = {tanPx: tanPx};
})();
