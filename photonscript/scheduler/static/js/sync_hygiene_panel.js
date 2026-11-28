/* PhotonScript sync hygiene panel (PS-43).
 *
 * Fills every element with class "sync-hygiene" from GET /api/sync/hygiene:
 * top folders of the Syncthing backlog by pending bytes, each tagged astro
 * or other, a warning when non-astronomy folders are being synced (with the
 * exact ignore patterns and where to add them), Syncthing folder errors and
 * a short diagnosis. OBSERVE ONLY: nothing here changes Syncthing; the
 * ignore text is for Jeremy to review and paste himself.
 *
 * data-mode="compact" (dashboard Desktop sync strip): warning + top 6.
 * data-mode="full"    (System page): every folder, error samples, the
 *                     generated ignore text with a Copy button.
 */
(function () {
    "use strict";

    function esc(v) {
        return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
                    "'": '&#39;'}[c];
        });
    }
    function gb(b) {
        b = Number(b) || 0;
        if (b >= 1e9) return (b / 1e9).toFixed(1) + ' GB';
        if (b >= 1e6) return (b / 1e6).toFixed(0) + ' MB';
        return (b / 1e3).toFixed(0) + ' KB';
    }
    function ago(s) {
        if (s == null) return '';
        if (s < 120) return s + ' s ago';
        if (s < 7200) return Math.round(s / 60) + ' min ago';
        return (s / 3600).toFixed(1) + ' h ago';
    }
    function tag(cls) {
        var col = cls === 'astro' ? '#4ade80' : '#f87171';
        return '<span style="color:' + col + ';border:1px solid ' + col +
            ';border-radius:4px;padding:0 .3rem;font-size:.75em;">' +
            esc(cls) + '</span>';
    }

    function renderWarning(d, full) {
        var w = d.warning;
        if (!w) return '';
        var pats = (w.patterns || []).map(function (p) {
            return '<code>' + esc(p) + '</code>';
        }).join(' ');
        return '<div style="border:1px solid #f87171;border-radius:6px;' +
            'padding:.5rem .7rem;margin:.4rem 0;background:rgba(248,113,113,.08);">' +
            '<b style="color:#f87171;">Warning: non-astronomy folders are being synced.</b> ' +
            esc(w.text) +
            '<div style="margin-top:.3rem;">Add the ignore pattern(s) ' + pats +
            ' in ' + esc(w.where) + '.</div>' +
            '<div style="opacity:.75;margin-top:.2rem;">' + esc(w.note) + '</div>' +
            (full ? '' : '<div style="margin-top:.2rem;"><a href="/system#syncHygienePanel" ' +
             'style="color:#7dd3fc;">Details and the ignore text on the System page</a></div>') +
            '</div>';
    }

    function renderFolders(rows, full) {
        if (!rows.length) return '';
        var list = full ? rows : rows.slice(0, 6);
        return '<table class="bd" style="border-collapse:collapse;margin:.3rem 0;">' +
            '<tr style="opacity:.6;"><th style="text-align:left;padding-right:1rem;">Folder</th>' +
            '<th style="text-align:right;padding-right:1rem;">Files</th>' +
            '<th style="text-align:right;padding-right:1rem;">Pending</th><th></th>' +
            (full ? '<th style="text-align:left;">Why / largest subfolders</th>' : '') + '</tr>' +
            list.map(function (r) {
                var subs = (r.top_subfolders || []).map(function (s) {
                    return esc(s.folder) + ' (' + s.files + ', ' + gb(s.bytes) + ')';
                }).join('; ');
                return '<tr><td style="padding-right:1rem;"><b>' + esc(r.folder) + '</b></td>' +
                    '<td style="text-align:right;padding-right:1rem;">' + r.files + '</td>' +
                    '<td style="text-align:right;padding-right:1rem;">' + gb(r.bytes) + '</td>' +
                    '<td>' + tag(r['class']) + '</td>' +
                    (full ? '<td style="opacity:.8;">' + esc(r.reason) +
                     (subs ? '<br><span style="opacity:.75;">' + subs + '</span>' : '') +
                     '</td>' : '') + '</tr>';
            }).join('') + '</table>';
    }

    function renderErrors(d, full) {
        var e = d.errors;
        var out = '';
        if (d.errors_error) {
            out += '<div style="opacity:.7;">Folder errors: could not read (' +
                esc(d.errors_error) + ')</div>';
        }
        if (!e) return out;
        if (!e.total) return out + '<div style="opacity:.7;">Syncthing folder errors: none</div>';
        out += '<div><b style="color:#eab308;">Syncthing folder errors: ' + e.total + '</b> ' +
            (e.by_kind || []).map(function (k) {
                return esc(k.text) + ': ' + k.count;
            }).join('; ') + '</div>';
        if (full && (e.samples || []).length) {
            out += '<details style="margin:.2rem 0 0 .8rem;"><summary style="cursor:pointer;">' +
                'Sample failed items (' + e.samples.length + ')</summary>' +
                e.samples.map(function (s) {
                    return '<div><code>' + esc(s.path) + '</code>: ' + esc(s.error) + '</div>';
                }).join('') + '</details>';
        }
        return out;
    }

    function renderIgnore(d) {
        var s = d.ignore_suggestion || {};
        if (!s.text) return '<div style="opacity:.7;">No ignore patterns suggested ' +
            '(every pending folder looks like astronomy data).</div>';
        return '<div style="margin-top:.5rem;"><b>Suggested ignore patterns</b> ' +
            '(generated for review, not applied; ' + (s.files || 0) + ' files, ' +
            gb(s.bytes) + ' would drop out of the backlog)' +
            ' <button class="btn btn-secondary sh-copy" type="button">Copy</button>' +
            '<pre class="sh-text" style="background:#0b1220;border:1px solid #334155;' +
            'border-radius:5px;padding:.5rem;white-space:pre-wrap;">' + esc(s.text) + '</pre>' +
            ((s.review || []).length ? '<div style="color:#eab308;">' +
             s.review.map(esc).join('<br>') + '</div>' : '') +
            '<div style="opacity:.75;">Where: ' + esc(s.where) + '. ' + esc(s.note) + '</div></div>';
    }

    function render(el, d) {
        var full = el.getAttribute('data-mode') === 'full';
        if (!d || !d.configured) {
            el.innerHTML = full ? '<div class="bd" style="opacity:.7;">Syncthing is not ' +
                'configured (Sync settings below).</div>' : '';
            el.hidden = !full;
            return;
        }
        var c = d.census || {};
        var head;
        if (!c.available) {
            head = c.error ? 'Backlog census failed: ' + esc(c.error) +
                ' (retries in 15 min)' : (c.refreshing ? 'Backlog census running...'
                : 'Backlog census not run yet (it runs hourly, not while armed).');
        } else {
            head = 'Backlog by folder: ' + c.listed_files + ' files, ' + gb(d.total_bytes) +
                ' (astro ' + d.astro.files + ' files / ' + gb(d.astro.bytes) +
                ', other ' + d.other.files + ' files / ' + gb(d.other.bytes) + ')' +
                '<span style="opacity:.6;"> . census ' + ago(c.age_s) +
                (c.complete ? '' : ', stopped at the page limit') +
                (c.refreshing ? ', refreshing' : '') + '</span>';
        }
        var html = renderWarning(d, full) +
            '<div class="bd">' + head + '</div>' +
            renderFolders(d.folders || [], full) +
            (full && d.more_folders ? '<div class="bd">+' + d.more_folders + ' more folder(s)</div>' : '') +
            '<div class="bd">' + renderErrors(d, full) + '</div>' +
            ((d.diagnosis || []).length ? '<ul class="bd" style="margin:.3rem 0 0 1rem;">' +
             d.diagnosis.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') +
             '</ul>' : '') +
            (full ? renderIgnore(d) : '');
        // Compact mode stays out of the way when there is nothing to say.
        var quiet = !full && !d.warning && !(d.errors && d.errors.total) &&
            !(d.diagnosis || []).length;
        el.hidden = quiet;
        el.innerHTML = html;
        var btn = el.querySelector('.sh-copy');
        if (btn) btn.onclick = function () {
            var t = (d.ignore_suggestion || {}).text || '';
            try {
                navigator.clipboard.writeText(t);
                btn.textContent = 'Copied';
            } catch (e) { btn.textContent = 'Select the text to copy'; }
        };
    }

    async function load() {
        var els = document.querySelectorAll('.sync-hygiene');
        if (!els.length) return;
        var d;
        try { d = await (await fetch('/api/sync/hygiene')).json(); }
        catch (e) { return; }
        els.forEach(function (el) { render(el, d); });
    }

    window.loadSyncHygiene = load;
    load();
    setInterval(load, 120000);
})();
