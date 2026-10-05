/* PhotonScript Sideload box (PS-123) on the dashboard's Target Goals card.
 *
 * Builds a hand-made night (recipe), shows its lint and container tree per
 * rig, and loads it into NINA #1 / NINA #2 after a confirm. LOAD ONLY:
 * nothing is started; Start stays in NINA. Routes (routers/sideload.py):
 *   preview  GET  /api/sequence/sideload/preview?recipe=&exclude=&at=
 *   load     POST /api/sequence/sideload?rig=&recipe=&exclude=&at=
 * Exclude checkboxes come from /api/tonight.
 */
(function () {
    "use strict";

    var lastPreview = null;
    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v == null ? '-' : v).replace(/[&<>"]/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
        });
    }
    async function getJSON(url, opts) {
        var r = await fetch(url, opts);
        var body;
        try { body = await r.json(); } catch (e) { body = {detail: 'HTTP ' + r.status}; }
        body._status = r.status;
        return body;
    }

    function query() {
        var p = new URLSearchParams();
        p.set('recipe', $('sideloadRecipe').value);
        document.querySelectorAll('#sideloadExclude input:checked').forEach(function (cb) {
            p.append('exclude', cb.value);
        });
        return p;
    }

    async function loadTargets() {
        var box = $('sideloadExclude');
        if (!box) return;
        var d = await getJSON('/api/tonight');
        var names = (d.targets || []).map(function (t) { return t.name; });
        if (!names.length) { box.textContent = 'no targets planned tonight'; return; }
        box.innerHTML = 'Exclude: ' + names.map(function (n) {
            return '<label style="margin-right:.8rem;white-space:nowrap;">' +
                '<input type="checkbox" value="' + esc(n) + '"> ' + esc(n) + '</label>';
        }).join('');
    }

    function renderRig(r) {
        if (r.error) {
            return '<div><b>' + esc(r.label) + '</b>: <span style="color:#ef4444;">' +
                esc(r.error) + '</span></div>';
        }
        var l = r.lint || {};
        var chip = l.ok
            ? '<span style="color:#22c55e;">lint PASS</span>'
            : '<span style="color:#ef4444;">lint FAIL</span>';
        var tree = (r.tree || []).map(function (n) {
            return '<div style="padding-left:' + (n.depth * 1.1) + 'em;">' +
                esc(n.type) + ' :: ' + esc(n.name) + '</div>';
        }).join('');
        var finds = (l.findings || []).map(function (f) {
            return '<div style="color:' + (f.level === 'ERROR' ? '#ef4444' : '#eab308') + ';">' +
                esc(f.level) + ' [' + esc(f.rule) + '] ' + esc(f.detail) + '</div>';
        }).join('');
        var extra = r.field ? ' Tracking test field: ' + esc(r.field.name) + '.' : '';
        if (r.excluded && r.excluded.length) extra += ' Excluded: ' + esc(r.excluded.join(', ')) + '.';
        return '<div style="margin:.5rem 0;"><b>' + esc(r.label) + '</b> ' + chip + ' (' +
            esc(l.errors) + ' error(s), ' + esc(l.warnings) + ' warning(s)) ' +
            '<code>' + esc(r.name) + '</code>.' + extra +
            '<div class="bd" style="margin:.3rem 0 0 .6rem;">' + finds + '</div>' +
            '<details style="margin:.3rem 0 0 .6rem;"><summary class="bd" style="cursor:pointer;">' +
            'Container tree</summary><div class="bd" style="font-family:monospace;font-size:.8rem;">' +
            tree + '</div></details>' +
            '<button class="btn btn-secondary" data-sideload-rig="' + esc(r.rig) + '"' +
            (l.ok ? '' : ' disabled') + ' style="margin-top:.3rem;">Load into ' +
            esc(r.label) + ' (no start)</button></div>';
    }

    async function preview() {
        var out = $('sideloadResult');
        out.textContent = 'Building and linting...';
        var d = await getJSON('/api/sequence/sideload/preview?' + query().toString());
        lastPreview = d;
        if (d._status !== 200) { out.textContent = d.detail || ('HTTP ' + d._status); return; }
        var rigs = d.rigs || {};
        out.innerHTML = '<div class="bd">Armer: ' + esc(d.armer) + '. ' + esc(d.note) + '</div>' +
            Object.keys(rigs).map(function (k) { return renderRig(rigs[k]); }).join('');
    }

    async function load(rig) {
        var r = lastPreview && lastPreview.rigs && lastPreview.rigs[rig];
        var name = r ? r.name : rig;
        if (!confirm('Load "' + name + '" into ' + (r ? r.label : rig) + '?\n\n' +
                     'This replaces the sequence loaded in that NINA. It is NOT ' +
                     'started: press Start in NINA when ready.')) return;
        var p = query();
        p.set('rig', rig);
        var d = await getJSON('/api/sequence/sideload?' + p.toString(), {method: 'POST'});
        var msg = d._status === 200
            ? 'Loaded into ' + (r ? r.label : rig) + ' (not started). Copy: ' + d.file
            : 'Refused (' + d._status + '): ' + (d.detail || '');
        var el = document.createElement('div');
        el.style.color = d._status === 200 ? '#22c55e' : '#ef4444';
        el.textContent = msg;
        $('sideloadResult').appendChild(el);
    }

    document.addEventListener('DOMContentLoaded', function () {
        var box = $('sideloadBox');
        if (!box) return;
        var loaded = false;
        box.addEventListener('toggle', function () {
            if (box.open && !loaded) { loaded = true; loadTargets(); }
        });
        $('sideloadPreview').addEventListener('click', preview);
        $('sideloadResult').addEventListener('click', function (ev) {
            var rig = ev.target && ev.target.getAttribute('data-sideload-rig');
            if (rig) load(rig);
        });
    });
})();
