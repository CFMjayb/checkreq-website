/* Full-screen support (2026-08-02).
 *
 * Two entry points, both requesting fullscreen from inside a real click
 * (the browser refuses a bare, un-gestured requestFullscreen() call --
 * that restriction exists specifically so a page can't auto-fullscreen
 * itself to fake a native OS/login dialog):
 *   1. The "Continue" / "Continue as X" button on /login -- fullscreen is
 *      requested synchronously inside that same click, before the form's
 *      own GET navigation to /auth/route runs, so Chromium/Edge carry the
 *      fullscreen state through the whole Microsoft-login redirect chain
 *      and land back on /portal still fullscreen. Firefox/Safari are less
 *      consistent about preserving it across that cross-origin hop -- not
 *      something a Beacon-side script can control.
 *   2. #fullscreenToggleBtn, fixed just under the header on every page,
 *      for turning it on/off (or re-entering after an Esc-key exit) at any
 *      time, not just at sign-in.
 */
(function () {
  "use strict";

  function fsElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function requestFs(el) {
    var target = el || document.documentElement;
    var fn = target.requestFullscreen || target.webkitRequestFullscreen;
    if (fn) { try { fn.call(target).catch(function () {}); } catch (e) {} }
  }

  function exitFs() {
    var fn = document.exitFullscreen || document.webkitExitFullscreen;
    if (fn) { try { fn.call(document).catch(function () {}); } catch (e) {} }
  }

  function fullscreenSupported() {
    return !!(document.fullscreenEnabled || document.webkitFullscreenEnabled);
  }

  function updateToggleIcon() {
    // Toggled via style.display, not the `hidden` attribute/IDL property --
    // SVG child elements don't reliably reflect .hidden as a boolean across
    // browsers the way a plain HTMLElement does, so display is the one
    // mechanism guaranteed to actually show/hide them everywhere.
    var enterIcon = document.getElementById("fsIconEnter");
    var exitIcon = document.getElementById("fsIconExit");
    if (!enterIcon || !exitIcon) return;
    var inFs = !!fsElement();
    enterIcon.style.display = inFs ? "none" : "";
    exitIcon.style.display = inFs ? "" : "none";
    var btn = document.getElementById("fullscreenToggleBtn");
    if (btn) btn.setAttribute("aria-label", inFs ? "Exit full screen" : "Enter full screen");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("fullscreenToggleBtn");
    if (btn && fullscreenSupported()) {
      btn.hidden = false;
      btn.addEventListener("click", function () {
        if (fsElement()) exitFs(); else requestFs();
      });
    }

    // Sign-in "Continue" / "Continue as X" forms on /login -- request
    // fullscreen inside the submit event (still counts as the original
    // click's user gesture) without blocking the form's own navigation.
    document.querySelectorAll(".login-email-form").forEach(function (form) {
      form.addEventListener("submit", function () {
        if (fullscreenSupported()) requestFs();
      });
    });
  });

  document.addEventListener("fullscreenchange", updateToggleIcon);
  document.addEventListener("webkitfullscreenchange", updateToggleIcon);
})();
