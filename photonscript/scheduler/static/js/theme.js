/* PhotonScript — dark/light theme toggle.
 *
 * - Applies the saved theme to <html> as early as possible (this file is
 *   loaded synchronously in <head>) to avoid a flash of the wrong theme.
 * - Injects a small toggle button into the page <header> once the DOM is
 *   ready, so no per-template markup is needed.
 * - Persists the choice in localStorage under "ps-theme".
 *
 * Default is the dark night-time theme; light mode is opt-in and sticky.
 */
(function () {
    "use strict";
    var KEY = "ps-theme";
    var root = document.documentElement;

    function apply(theme) {
        if (theme === "light") {
            root.setAttribute("data-theme", "light");
        } else {
            root.removeAttribute("data-theme");
        }
    }

    function current() {
        return root.getAttribute("data-theme") === "light" ? "light" : "dark";
    }

    var saved = null;
    try { saved = localStorage.getItem(KEY); } catch (e) { /* private mode */ }
    apply(saved === "light" ? "light" : "dark");

    function updateButton() {
        var btn = document.getElementById("themeToggle");
        if (!btn) return;
        var isLight = current() === "light";
        // Show the mode you'll switch TO.
        btn.textContent = isLight ? "☾ Dark" : "☀ Light";
        var label = isLight ? "Switch to dark mode" : "Switch to light mode";
        btn.setAttribute("aria-label", label);
        btn.title = label;
    }

    function setTheme(theme) {
        apply(theme);
        try { localStorage.setItem(KEY, theme); } catch (e) { /* ignore */ }
        updateButton();
    }

    function inject() {
        if (document.getElementById("themeToggle")) return;
        var header = document.querySelector("header");
        if (!header) return;
        // Prefer the right-hand nav cluster; fall back to the header itself.
        var host = header.children.length > 1 ? header.lastElementChild : header;
        var btn = document.createElement("button");
        btn.id = "themeToggle";
        btn.type = "button";
        btn.className = "theme-toggle";
        btn.addEventListener("click", function () {
            setTheme(current() === "light" ? "dark" : "light");
        });
        host.appendChild(btn);
        updateButton();
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", inject);
    } else {
        inject();
    }
})();
