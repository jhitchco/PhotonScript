/* fold.js: shared folding for the dashboard pages (UI tidy, 2026-11-23).
 *
 * Markup: <details class="fold" data-fold="page-section"> with a <summary>
 * holding the heading, an optional <span class="fold-dot"></span> and an
 * optional <span class="fold-sum"></span> one-line summary.
 *
 *   Fold.init(root)            restore the open / closed state remembered per
 *                              data-fold key (localStorage, best effort) and
 *                              remember the user's own clicks from now on.
 *   Fold.status(el, level, t)  set the dot (ok | info | warn | fail) and the
 *                              summary text; warn / fail opens the section
 *                              (problems start open) unless opts.noOpen.
 *   Fold.esc(v)                HTML escape for anything put into innerHTML.
 *
 * Only user clicks are saved, so a section opened because of a problem does
 * not stay open once the problem is gone. Pure ASCII on purpose.
 */
(function () {
    "use strict";
    var PREFIX = "fold:";

    function load(key) {
        try { return window.localStorage.getItem(PREFIX + key); } catch (e) { return null; }
    }
    function save(key, val) {
        try { window.localStorage.setItem(PREFIX + key, val); } catch (e) { /* private mode */ }
    }
    function el(x) { return typeof x === "string" ? document.getElementById(x) : x; }
    function esc(v) {
        return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
            return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c];
        });
    }

    function wire(d) {
        if (!d || d.getAttribute("data-fold-wired")) return;
        d.setAttribute("data-fold-wired", "1");
        var key = d.getAttribute("data-fold");
        if (key) {
            var s = load(key);
            if (s === "open") d.open = true;
            else if (s === "closed") d.open = false;
        }
        var sum = d.querySelector(":scope > summary");
        if (sum && key) {
            sum.addEventListener("click", function (ev) {
                // a link or button inside the summary does its own job, no toggle
                var t = ev.target;
                var ctl = t && t.closest ? t.closest("a,button,input,select") : null;
                if (ctl && sum.contains(ctl)) return;
                save(key, d.open ? "closed" : "open");   // state after this click
            });
        }
    }

    function init(root) {
        var r = el(root) || document;
        var list = r.querySelectorAll("details.fold");
        for (var i = 0; i < list.length; i++) wire(list[i]);
    }

    function status(target, level, text, opts) {
        var d = el(target);
        if (!d) return;
        if (d.tagName !== "DETAILS") d = d.closest ? d.closest("details.fold") : null;
        if (!d) return;
        wire(d);   // restore the remembered state first, then a problem may open it
        var lv = String(level || "").toLowerCase();
        if (lv === "pass" || lv === "go") lv = "ok";
        if (lv === "error") lv = "fail";
        var sum = d.querySelector(":scope > summary");
        if (!sum) return;
        var dot = sum.querySelector(".fold-dot");
        if (dot) dot.className = "fold-dot" + (lv ? " " + lv : "");
        var line = sum.querySelector(".fold-sum");
        if (line && text != null) {
            line.textContent = String(text);
            line.title = String(text);
        }
        if ((lv === "warn" || lv === "fail") && !(opts && opts.noOpen)) d.open = true;
    }

    // A deep link (/system#syncHygienePanel, /guiding#auditSec) opens the
    // folds around and directly inside its target so the link lands on content.
    function openHash() {
        var h = (window.location.hash || "").slice(1);
        if (!h) return;
        var t = null;
        try { t = document.getElementById(decodeURIComponent(h)); } catch (e) { t = null; }
        if (!t) return;
        var inner = t.tagName === "DETAILS" ? t : t.querySelector(":scope > details.fold");
        if (inner) inner.open = true;
        var p = t.parentElement;
        while (p) {
            if (p.tagName === "DETAILS") p.open = true;
            p = p.parentElement;
        }
        if (t.scrollIntoView) t.scrollIntoView();
    }

    window.Fold = {init: init, status: status, esc: esc, wire: wire, openHash: openHash};
    function boot() { init(document); openHash(); }
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
    } else {
        boot();
    }
    window.addEventListener("hashchange", openHash);
})();
