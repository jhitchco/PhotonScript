/* PhotonScript guided-night readiness panel, Guiding tab (2026-10-26).
 *
 * Fills #grLine / #grItems / #grCommission from GET /api/guiding/readiness
 * (scheduler/guided_readiness.py): READY / READY with auto fixes / BLOCKED,
 * then the ordered list (blockers first in fix order, then what PhotonScript
 * fixes itself on the guided night, advice, context), each with its fix and
 * where to do it; an Apply button where the PS-89 audit can apply the row
 * (PHD2.applyAudit: dry run first, then asks). GET /api/guiding/commission
 * for the first guided night's guiding test. Refresh re-runs the PHD2
 * audit (reads PHD2, changes nothing).
 * Also used by the dashboard (GUIDEDREADY.line('elementId')).
 */
var GUIDEDREADY = (function () {
    "use strict";

    function $(id) { return document.getElementById(id); }
    function esc(v) {
        return String(v == null ? '-' : v).replace(/[&<>"]/g, function (c) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c];
        });
    }
    var LEVEL = {
        blocker: ['badge-fail', 'BLOCKER'],
        auto: ['badge-warn', 'AUTO'],
        advice: ['badge-unknown', 'ADVICE'],
        info: ['badge-unknown', 'INFO']
    };
    var STATUS_COL = {ready: '#4ade80', auto: '#4ade80', blocked: '#f87171', unknown: '#eab308'};

    function chip(level) {
        var c = LEVEL[level] || ['badge-unknown', level];
        return '<span class="badge ' + c[0] + '">' + esc(c[1]) + '</span>';
    }

    function itemHtml(it, n) {
        var act = it.apply_id ? ' <button class="btn btn-primary btn-sm" data-gr-apply="' +
            esc(it.apply_id) + '" title="PS-89 apply: dry run first, then asks">Apply</button>' : '';
        return '<div class="g-attn">' + chip(it.level) + ' <b>' + n + '. ' + esc(it.what) + '</b>: ' +
            esc(it.current) + act + '<div class="g-dim" style="margin-left:1.4rem;">' +
            esc(it.fix) + ' <i>[' + esc(it.where) + ']</i>' +
            (it.detail ? '<br>' + esc(it.detail) : '') + '</div></div>';
    }

    async function getJSON(url) {
        var r = await fetch(url, {cache: 'no-store'});
        if (!r.ok) throw new Error(url + ' ' + r.status);
        return r.json();
    }

    function render(d) {
        var col = STATUS_COL[d.status] || '#eab308';
        $('grLine').innerHTML = '<b style="color:' + col + ';">' + esc(d.line) + '</b>' +
            ' <span class="g-dim">| from the PHD2 audit of ' + esc((d.sources || {}).phd2_audit || 'never') +
            '; prep ' + esc((d.prep || {}).mode || '-') + '</span>';
        var items = d.items || [];
        $('grItems').innerHTML = items.length ? items.map(function (it, i) {
            return itemHtml(it, i + 1);
        }).join('') : '<div class="bd">Nothing stands between tonight and a guided night.</div>';
        Array.prototype.forEach.call(document.querySelectorAll('#guidedReady [data-gr-apply]'), function (b) {
            b.onclick = async function () {
                if (window.PHD2 && PHD2.applyAudit) {
                    await PHD2.applyAudit(b.getAttribute('data-gr-apply'), 'grStatus');
                    load(false);
                } else {
                    $('grStatus').textContent = 'use Apply in the Settings audit below';
                }
            };
        });
        var c = d.commission || {};
        var t = c.tonight || {};
        $('grCommission').innerHTML = '<b>Guided commissioning</b> (' + esc(c.mode) + '): ' +
            esc(c.text || '-') + (t.verdict ? ' | tonight ' + esc(t.verdict) : '') +
            (c.record && c.record.night ? ' | last test ' + esc(c.record.night) + ' ' +
                esc(c.record.verdict) + ' (RMS ' + esc(c.record.rms_arcsec) + '")' : '');
    }

    async function load(refresh) {
        if (!$('grItems')) return;
        $('grStatus').textContent = refresh ? 're-running the PHD2 audit...' : '';
        try {
            render(await getJSON('/api/guiding/readiness' + (refresh ? '?refresh=1' : '')));
            $('grStatus').textContent = '';
        } catch (e) {
            $('grLine').textContent = 'error: ' + e;
        }
    }

    // one line for the dashboard's Tonight's Run card
    async function line(id) {
        var el = $(id);
        if (!el) return;
        try {
            var d = await getJSON('/api/guiding/readiness?darks=0');
            var col = STATUS_COL[d.status] || '#eab308';
            var first = (d.items || []).filter(function (i) { return i.level === 'blocker'; })[0];
            el.innerHTML = '<a href="/guiding#guidedReady" style="color:' + col + ';text-decoration:none;">' +
                esc(d.line) + '</a>' + (first ? ' <span style="opacity:.7;">first fix: ' +
                esc(first.what) + '</span>' : '');
        } catch (e) {
            el.textContent = '';
        }
    }

    function init() {
        if ($('grRefresh')) $('grRefresh').onclick = function () { load(true); };
        load(false);
    }

    return {init: init, load: load, line: line};
})();
