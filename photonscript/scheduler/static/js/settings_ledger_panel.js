/* PhotonScript settings ledger panel (System page).
 *
 * GET /api/settings-ledger/status: the newest rule results for every
 * program (NINA #1 / #2, PHD2, TheSky, PhotonScript's config), when each
 * app was last read and last changed, and the exact fix for every rule that
 * fails or warns. The diff viewer asks GET /api/settings-ledger/diff for
 * "what changed since the last good night" (or any two nights / snapshots).
 * "Snapshot now" POSTs /api/settings-ledger/snapshot: it only READS the
 * programs (nothing is ever written to NINA, PHD2 or TheSky).
 */
(function () {
    "use strict";

    function esc(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
                    "'": '&#39;'}[c];
        });
    }
    function t(z) {
        if (!z) return '-';
        try { return new Date(z).toLocaleString(); } catch (e) { return z; }
    }
    var COLORS = {fail: '#f87171', warn: '#fde047', unknown: '#94a3b8',
                  info: '#7dd3fc', pass: '#86efac', na: '#64748b'};
    function badge(s) {
        var c = COLORS[s] || '#94a3b8';
        return '<span style="color:' + c + ';border:1px solid ' + c +
            ';border-radius:4px;padding:0 .35rem;font-size:.75em;font-weight:700;">' +
            esc(String(s || '').toUpperCase()) + '</span>';
    }
    function short(v) {
        if (v == null) return '-';
        var s = typeof v === 'object' ? JSON.stringify(v) : String(v);
        return s.length > 90 ? s.slice(0, 87) + '...' : s;
    }

    function rowsTable(rows, withFix) {
        if (!rows.length) return '<div style="opacity:.7;">none</div>';
        return '<table class="bd" style="border-collapse:collapse;width:100%;margin:.3rem 0;">' +
            '<tr style="opacity:.6;"><th></th><th style="text-align:left;">App</th>' +
            '<th style="text-align:left;">Setting</th><th style="text-align:left;">Now</th>' +
            '<th style="text-align:left;">Want</th><th style="text-align:left;">Since</th></tr>' +
            rows.map(function (r) {
                var extra = '';
                if (withFix && (r.fix || r.why || r.note)) {
                    extra = '<tr><td></td><td colspan="5" style="padding:0 0 .5rem 0;opacity:.85;' +
                        'font-size:.85em;">' +
                        (r.fix ? '<div><b>Fix:</b> ' + esc(r.fix) + '</div>' : '') +
                        (r.why ? '<div style="opacity:.75;">' + esc(r.why) + '</div>' : '') +
                        (r.note ? '<div style="opacity:.6;">' + esc(r.note) + '</div>' : '') +
                        '</td></tr>';
                }
                return '<tr><td style="padding-right:.5rem;">' + badge(r.status) + '</td>' +
                    '<td style="padding-right:.8rem;">' + esc(r.app) + '</td>' +
                    '<td style="padding-right:.8rem;">' + esc(r.label) + '</td>' +
                    '<td style="padding-right:.8rem;">' + esc(r.current == null ? '-' : r.current) + '</td>' +
                    '<td style="padding-right:.8rem;">' + esc(r.desired) + '</td>' +
                    '<td style="opacity:.6;font-size:.85em;">' + esc(t(r.since_utc)) + '</td></tr>' + extra;
            }).join('') + '</table>';
    }

    function appsTable(apps) {
        var keys = Object.keys(apps || {});
        if (!keys.length) return '';
        return '<table class="bd" style="border-collapse:collapse;margin:.3rem 0;">' +
            '<tr style="opacity:.6;"><th style="text-align:left;padding-right:1rem;">Program</th>' +
            '<th style="text-align:left;padding-right:1rem;">Read</th>' +
            '<th style="text-align:left;padding-right:1rem;">Last change</th>' +
            '<th style="text-align:left;">Note</th></tr>' +
            keys.map(function (k) {
                var a = apps[k];
                return '<tr><td style="padding-right:1rem;">' + esc(a.label) + '</td>' +
                    '<td style="padding-right:1rem;">' + esc(t(a.read_utc)) +
                    (a.carried ? ' <span style="color:#fde047;">(not read now)</span>' : '') + '</td>' +
                    '<td style="padding-right:1rem;">' + esc(t(a.last_change_utc)) + '</td>' +
                    '<td style="opacity:.7;font-size:.85em;">' + esc(a.source || a.note || '') + '</td></tr>';
            }).join('') + '</table>';
    }

    function renderDiff(el, d) {
        if (!d.ok) { el.innerHTML = '<div style="opacity:.7;">' + esc(d.note || 'no snapshot') + '</div>'; return; }
        var html = '<div style="margin:.3rem 0;">From <b>' + esc(d.a.id) + '</b> (' + esc(d.a.night) +
            ') to <b>' + esc(d.b.id) + '</b> (' + esc(d.b.night) + '): ' + esc(d.changed) +
            ' settings changed</div>';
        Object.keys(d.diff || {}).forEach(function (app) {
            var rows = d.diff[app];
            html += '<div style="margin-top:.4rem;color:#7dd3fc;">' + esc(app) + '</div>' +
                '<table class="bd" style="border-collapse:collapse;font-size:.85em;">' +
                Object.keys(rows).filter(function (k) { return k !== '_truncated'; }).map(function (k) {
                    return '<tr><td style="padding-right:1rem;font-family:monospace;">' + esc(k) + '</td>' +
                        '<td style="padding-right:1rem;color:#fca5a5;">' + esc(short(rows[k][0])) + '</td>' +
                        '<td style="color:#86efac;">' + esc(short(rows[k][1])) + '</td></tr>';
                }).join('') + '</table>' +
                (rows._truncated ? '<div style="opacity:.6;">and ' + esc(rows._truncated) + ' more</div>' : '');
        });
        el.innerHTML = html;
    }

    function loadDiff(root, a, b) {
        var out = root.querySelector('.sl-diff');
        out.textContent = 'loading...';
        fetch('/api/settings-ledger/diff?a=' + encodeURIComponent(a) + '&b=' + encodeURIComponent(b))
            .then(function (r) { return r.json(); })
            .then(function (d) { renderDiff(out, d); })
            .catch(function (e) { out.textContent = 'error: ' + e; });
    }

    function render(root, d) {
        if (!d.ok) {
            root.innerHTML = '<div style="opacity:.8;">' + esc(d.note || 'no snapshot yet') +
                (d.enabled === false ? ' (settings_ledger_enabled is off)' : '') + '</div>' +
                '<button class="btn sl-take" style="margin-top:.4rem;">Snapshot now</button>';
            bind(root);
            return;
        }
        var rows = d.rows || [];
        var bad = rows.filter(function (r) { return r.status === 'fail' || r.status === 'warn'; });
        var unk = rows.filter(function (r) { return r.status === 'unknown'; });
        var rest = rows.filter(function (r) { return ['fail', 'warn', 'unknown'].indexOf(r.status) < 0; });
        var c = d.counts || {};
        var nights = (d.nights || []).slice().reverse();
        var opts = ['<option value="last_good">last good night' +
                    (d.last_good ? ' (' + esc(d.last_good.night) + ')' : ' (none)') + '</option>']
            .concat(nights.map(function (n) { return '<option value="' + esc(n) + '">' + esc(n) + '</option>'; }));
        root.innerHTML =
            '<div style="margin-bottom:.4rem;">Night <b>' + esc(d.night) + '</b>, snapshot <b>' +
            esc(d.id) + '</b> (' + esc(t(d.snapshot_utc)) + '), checked ' + esc(t(d.checked_utc)) +
            ' (' + esc(d.reason) + '). ' + badge('fail') + ' ' + esc(c.fail || 0) + ' ' +
            badge('warn') + ' ' + esc(c.warn || 0) + ' ' + badge('unknown') + ' ' + esc(c.unknown || 0) +
            ' ' + badge('pass') + ' ' + esc(c.pass || 0) +
            ' <button class="btn sl-take" style="margin-left:.6rem;">Snapshot now</button>' +
            ' <span class="sl-take-status" style="opacity:.7;"></span></div>' +
            appsTable(d.apps) +
            '<h3 style="margin:.6rem 0 .2rem;font-size:.95rem;">Wrong now (with the fix)</h3>' +
            rowsTable(bad, true) +
            (unk.length ? '<details style="margin-top:.4rem;"><summary>Not checked (' + unk.length +
                ')</summary>' + rowsTable(unk, true) + '</details>' : '') +
            '<details style="margin-top:.4rem;"><summary>Every other rule (' + rest.length +
            ')</summary>' + rowsTable(rest, false) + '</details>' +
            '<h3 style="margin:.8rem 0 .2rem;font-size:.95rem;">What changed</h3>' +
            '<div>From <select class="sl-a">' + opts.join('') + '</select> to ' +
            '<select class="sl-b"><option value="latest">now</option>' +
            nights.map(function (n) { return '<option value="' + esc(n) + '">' + esc(n) + '</option>'; }).join('') +
            '</select> <button class="btn sl-go">Show</button></div>' +
            '<div class="sl-diff" style="margin-top:.4rem;"></div>' +
            (d.rules_lint && d.rules_lint.length ? '<div style="color:#fde047;margin-top:.4rem;">Rules file: ' +
                esc(d.rules_lint.join('; ')) + '</div>' : '');
        bind(root);
        if (d.last_good) loadDiff(root, 'last_good', 'latest');
    }

    function bind(root) {
        var take = root.querySelector('.sl-take');
        if (take) take.onclick = function () {
            take.disabled = true;
            var st = root.querySelector('.sl-take-status');
            if (st) st.textContent = 'reading every program...';
            fetch('/api/settings-ledger/snapshot', {method: 'POST'})
                .then(function (r) { return r.json(); })
                .then(function () { load(root); })
                .catch(function (e) { if (st) st.textContent = 'error: ' + e; take.disabled = false; });
        };
        var go = root.querySelector('.sl-go');
        if (go) go.onclick = function () {
            loadDiff(root, root.querySelector('.sl-a').value, root.querySelector('.sl-b').value);
        };
    }

    function load(root) {
        fetch('/api/settings-ledger/status')
            .then(function (r) { return r.json(); })
            .then(function (d) { render(root, d); })
            .catch(function (e) { root.textContent = 'error: ' + e; });
    }

    window.SettingsLedger = {
        load: function (id) {
            var root = document.getElementById(id);
            if (root) load(root);
        }
    };
})();
