/* PhotonScript auto-arm switches (PS-125) on the dashboard's Tonight's Run card.
 *
 * Two checkboxes ("Auto-arm every evening", "Noon re-arm" + guided toggle)
 * save through POST /api/config, the same .env write the System page does,
 * so a change here survives a restart exactly like a System page edit.
 * Turning a switch ON asks for a confirm first. Status, next action time and
 * the last auto-arm decision come from GET /api/auto-arm (routers/auto_arm.py).
 * window.autoArmSideloadOk() lets the manual Arm buttons confirm before
 * replacing a sideloaded sequence.
 */
(function () {
    "use strict";

    var SIDELOAD_WARN = 'A sideloaded sequence is loaded in NINA; arming replaces it.';
    var ON_CONFIRM = {
        PS_AUTO_ARM_ENABLED: 'Turn ON auto-arm every evening?\n\nWhen the armer is idle, ' +
            'PhotonScript arms tonight on its own once the window opens (it skips, ' +
            'and tells you why, when NINA is running or a sideload is loaded).',
        PS_NOON_ARM_ENABLED: 'Turn ON noon re-arm?\n\nAt 12:00 local an idle armer arms ' +
            'tonight early (it also forces coolers off). It skips, and tells you why, ' +
            'when NINA is running or a sideload is loaded.'
    };
    var last = null;

    function $(id) { return document.getElementById(id); }

    async function getJSON(url, opts) {
        var r = await fetch(url, opts);
        var body;
        try { body = await r.json(); } catch (e) { body = {detail: 'HTTP ' + r.status}; }
        body._status = r.status;
        return body;
    }

    function render(d) {
        last = d;
        var ev = d.auto_arm || {}, na = d.noon_arm || {};
        $('autoArmEvening').checked = !!ev.enabled;
        $('autoArmNoon').checked = !!na.enabled;
        $('autoArmNoonGuided').checked = !!na.guided;
        $('autoArmEveningNext').textContent = '(' + (ev.next || 'off') + ')';
        $('autoArmNoonNext').textContent = '(' + (na.next || 'off') + ')';
        var chip = d.chip || '';
        var ld = d.last_decision;
        var chipEl = $('autoArmChip');
        chipEl.textContent = chip;
        chipEl.style.color = (ld && ld.value === 'skipped') ? '#fbbf24' : '';
        if (d.sideload) {
            chipEl.textContent = chip + ' | Sideload loaded tonight: ' +
                (d.sideload.value || '?') + ' (' + (d.sideload.rig || '?') + ')';
        }
    }

    async function refresh() {
        if (!$('autoArmBox')) return;
        try {
            var d = await getJSON('/api/auto-arm');
            if (d._status === 200) render(d);
        } catch (e) { /* keep last view */ }
    }

    async function save(cb) {
        var env = cb.dataset.env;
        var on = cb.checked;
        if (on && ON_CONFIRM[env] && !confirm(ON_CONFIRM[env])) {
            cb.checked = false;
            return;
        }
        var body = {};
        body[env] = on ? 'true' : 'false';
        var r = await getJSON('/api/config', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body)});
        if (r._status !== 200) {
            alert('Could not save ' + env + ': ' + (r.detail || r._status));
            cb.checked = !on;
        }
        refresh();
    }

    /* Resolve true when arming may go ahead: no sideload tonight, or the
     * user confirmed replacing it. */
    window.autoArmSideloadOk = async function () {
        try {
            var d = await getJSON('/api/auto-arm');
            if (d._status === 200) last = d;
        } catch (e) { /* fall back to the last view */ }
        if (last && last.sideload) {
            return confirm(SIDELOAD_WARN + '\n\nLoaded: ' + (last.sideload.value || '?') +
                           ' on ' + (last.sideload.rig || '?') + '. Arm anyway?');
        }
        return true;
    };

    document.addEventListener('DOMContentLoaded', function () {
        ['autoArmEvening', 'autoArmNoon', 'autoArmNoonGuided'].forEach(function (id) {
            var cb = $(id);
            if (cb) cb.addEventListener('change', function () { save(cb); });
        });
        refresh();
        setInterval(refresh, 30000);
    });
})();
