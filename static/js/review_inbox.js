// review_inbox.js -- Time Review's blue "Review Inbox" process button (Jay, 2026-10-25).
//
// Pressing it starts the payroll mailbox review (the form posts to .../review-inbox and asks for JSON). While the review runs a ring
// pulses around the button (class is-running, static/css/process_button.css); this script then asks .../review-inbox/state every few
// seconds and, when the review has finished, stops the ring, updates "Last updated" in the bottom bar and says what happened. A page
// that loads while a review is already running (started by someone else or by the schedule) shows the ring and polls the same way.
// Without scripts the form still posts and the page reloads with the usual banner. No inline handlers (CSP).
(function () {
  'use strict';

  var form = document.getElementById('reviewInboxForm');
  if (!form) return;
  var btn = document.getElementById('reviewInboxBtn');
  var msg = document.getElementById('reviewInboxMsg');
  var label = btn ? btn.querySelector('.btn-process-label') : null;
  if (!btn || !label) return;

  var stateUrl = form.getAttribute('data-state-url');
  var IDLE_LABEL = 'Review Inbox';
  var RUNNING_LABEL = 'Reviewing inbox…';
  var POLL_MS = 5000;
  var GIVE_UP_MS = 35 * 60 * 1000;     // the server treats a review reported running for 30 minutes as stuck
  var timer = null;
  var startedAt = 0;

  function setMessage(text, kind) {
    if (!msg) return;
    msg.className = 'ff-process-msg' + (kind ? ' ' + kind : '');
    msg.textContent = text || '';
  }

  function setRunning(on) {
    btn.classList.toggle('is-running', on);
    btn.disabled = on;
    btn.setAttribute('aria-busy', on ? 'true' : 'false');
    label.textContent = on ? RUNNING_LABEL : IDLE_LABEL;
  }

  // The bottom bar's "Last updated" line, rebuilt from the state the server reports.
  function paintFooter(s) {
    var el = document.querySelector('[data-ff-updated]');
    if (!el || !s) return;
    while (el.firstChild) el.removeChild(el.firstChild);
    function attn(text) {
      var sp = document.createElement('span');
      sp.className = 'ph-state-attn';
      sp.textContent = text;
      el.appendChild(sp);
      el.appendChild(document.createTextNode(' · '));
    }
    if (s.running) attn('Review running since ' + (s.running_since || ''));
    if (s.failed) attn('Last review failed ' + (s.failed_at || ''));
    el.appendChild(document.createTextNode('Last updated: '));
    var strong = document.createElement('strong');
    strong.textContent = s.last || 'never';
    el.appendChild(strong);
    if (s.detail) el.setAttribute('data-tip', s.detail); else el.removeAttribute('data-tip');
  }

  function stopPolling() {
    if (timer) { clearTimeout(timer); timer = null; }
  }

  function schedule() {
    stopPolling();
    timer = setTimeout(poll, POLL_MS);
  }

  function finished(s) {
    stopPolling();
    setRunning(false);
    if (s.failed) {
      setMessage('The review failed' + (s.failed_at ? ' at ' + s.failed_at : '') + '. Nothing was lost; it tries again on its own.', 'attn');
      return;
    }
    // New hours or items are on the board: reload once to show them (the Show filter is remembered). Not while a pop-up is open.
    if (s.changed && !document.querySelector('dialog[open]')) {
      setMessage('Finished' + (s.detail ? ': ' + s.detail : '') + '. Reloading to show the new hours…', 'ok');
      setTimeout(function () { window.location.reload(); }, 1500);
      return;
    }
    setMessage('Finished' + (s.detail ? ': ' + s.detail : '') + '.', 'ok');
    var a = document.createElement('a');
    a.href = '#';
    a.textContent = 'Refresh to see the new hours';
    a.addEventListener('click', function (e) { e.preventDefault(); window.location.reload(); });
    msg.appendChild(a);
  }

  function poll() {
    if (Date.now() - startedAt > GIVE_UP_MS) {
      stopPolling();
      setRunning(false);
      setMessage('Still not finished after 35 minutes. Refresh the page and check Last updated.', 'attn');
      return;
    }
    fetch(stateUrl, { credentials: 'same-origin', cache: 'no-store', headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error('state ' + r.status); return r.json(); })
      .then(function (s) {
        paintFooter(s);
        if (s.running) schedule(); else finished(s);
      })
      .catch(function () {
        stopPolling();
        setRunning(false);
        setMessage('Could not check on the review. Refresh the page to see where it stands.', 'attn');
      });
  }

  function begin() {
    startedAt = Date.now();
    setRunning(true);
    schedule();
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (btn.disabled) return;
    setMessage('', '');
    setRunning(true);
    fetch(form.action, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(new FormData(form)).toString()
    })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var s = d && d.state;
        paintFooter(s);
        if (s && s.running) {
          setMessage(d.code === 'busy' ? 'A review is already running. Waiting for it to finish.' : 'Reading the payroll mailbox. This takes a few minutes.', '');
          begin();
        } else {
          setRunning(false);
          setMessage((d && d.message) || 'Could not start the review.', d && d.ok ? 'ok' : 'attn');
        }
      })
      .catch(function () {
        setRunning(false);
        setMessage('Could not start the review. Refresh the page and try again.', 'attn');
      });
  });

  // The page loaded while a review was already running: show the ring and watch for the end.
  if (form.getAttribute('data-running') === '1') begin();
})();
