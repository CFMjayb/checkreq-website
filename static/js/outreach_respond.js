// outreach_respond.js -- 26-156: records a ONE-TAP email answer (respond.html, state "auto").
//
// Jay (2026-10-06): tapping an answer in the email should record it right away, show what was
// recorded, and let the person change it -- no extra "confirm" tap.
//
// Why this is a script and not the server recording on the page load: mail-security scanners and
// link previews FETCH links automatically, so a server that recorded an answer on a plain GET would
// record answers nobody gave (and a scanner that follows both buttons would leave the last one).
// The server therefore still never records on a GET. This page submits its hidden form (a POST)
// itself; a scanner that only fetches the HTML never runs it. Residual risk, accepted and logged:
// a scanner that fully renders pages in a real browser would run it too -- every answer made this
// way is stored with via = email_button and the caller's address and user agent, and the person
// can always change it.
//
// Safeguards: an automated browser (navigator.webdriver) is NOT auto-submitted -- it is shown the
// ordinary confirm form; a page opened in a background tab waits until it is actually shown;
// with scripting off the confirm form is simply what is on the page (it is visible by default).
(function () {
  'use strict';
  var root = document.documentElement;
  root.classList.add('oa-js');            // hides the confirm form + shows "Recording your answer..." (outreach.css)

  function reveal() { root.classList.add('oa-reveal'); }

  if (navigator.webdriver) { reveal(); return; }

  var done = false;
  function go() {
    if (done) return;
    var form = document.getElementById('oa-auto-form');
    if (!form) { reveal(); return; }
    done = true;
    form.submit();
  }
  function start() { setTimeout(go, 300); }

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  ready(function () {
    if (document.hidden) {
      document.addEventListener('visibilitychange', function () { if (!document.hidden) start(); });
    } else {
      start();
    }
  });
})();
