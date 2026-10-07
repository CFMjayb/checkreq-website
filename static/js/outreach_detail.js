// outreach_detail.js -- 26-156: a poll's page (admin_outreach_detail.html).
//
//   * Sending runs in chunks (about 40 emails per call) so a 300-second request never holds a
//     whole send. The server freezes the recipient list on "Send now"; this script then keeps
//     calling send-chunk until nothing is left. If the tab is closed halfway, reloading the page
//     of a poll that is still "Sending" simply continues (sending is resumable and never
//     double-sends: only unsent people are picked up, under a per-poll lock).
//   * "Remind people who have not answered" calls remind-chunk while it keeps sending; the
//     server never re-reminds anyone reminded in the last hour.
//   * The People table can be filtered.
(function () {
  'use strict';
  var root = document.getElementById('oa-detail');
  if (!root) return;
  var cid = root.getAttribute('data-poll');
  var status = root.getAttribute('data-status');
  var total = parseInt(root.getAttribute('data-total'), 10) || 0;
  var progress = document.getElementById('oa-progress');

  function say(msg, bad) {
    progress.hidden = false;
    progress.className = 'oa-progress' + (bad ? ' oa-bad' : '');
    progress.textContent = msg;
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function post(url) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: window.csrfHeader ? window.csrfHeader() : {} })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) { return { ok: r.ok, status: r.status, body: b }; });
      });
  }
  function busy(btn, on) {
    if (!btn) return;
    if (on && typeof showButtonLoading === 'function') showButtonLoading(btn);
    if (!on) { btn.disabled = false; btn.classList.remove('btn-loading'); }
  }
  function reload(msg) {
    var url = '/admin/polls/' + encodeURIComponent(cid);
    location.href = msg ? url + '?msg=' + encodeURIComponent(msg) : url;
  }

  var sending = false;
  async function sendLoop(retry) {
    if (sending) return;
    sending = true;
    var handled = 0, failed = 0, busyTries = 0;
    say('Sending…');
    for (;;) {
      var res = await post('/admin/polls/' + encodeURIComponent(cid) + '/send-chunk' + (retry ? '?retry=1' : ''));
      if (!res.ok) { say(res.body.error || 'Sending stopped. Reload the page to continue.', true); sending = false; return; }
      var b = res.body;
      if (b.busy) {
        if (++busyTries > 30) { say('Another send for this poll is still running. Reload in a minute.', true); sending = false; return; }
        await sleep(2000);
        continue;
      }
      handled += (b.sent || 0) + (b.failed || 0) + (b.suppressed || 0);
      failed += (b.failed || 0);
      say('Sending… ' + handled + (retry || !total ? '' : ' of ' + total) + ' handled' + (failed ? ' (' + failed + ' failed)' : ''));
      // Normal send: done when nothing is unsent. Retry: stop when a pass delivered nothing new
      // (a persistently failing address would otherwise be retried forever).
      if (b.remaining === 0 && !retry) break;
      if (retry && !b.sent) break;
    }
    sending = false;
    reload('Sending finished.' + (failed ? ' ' + failed + ' email(s) failed: see the list below.' : ''));
  }

  var resumeBtn = document.getElementById('oa-resume');
  if (resumeBtn) resumeBtn.addEventListener('click', function () { busy(resumeBtn, true); sendLoop(false); });
  if (status === 'sending') sendLoop(false);   // the page of a poll that is mid-send continues it

  var retryBtn = document.getElementById('oa-retry');
  if (retryBtn) retryBtn.addEventListener('click', function () {
    busy(retryBtn, true);
    sendLoop(true);
  });

  var remindBtn = document.getElementById('oa-remind');
  if (remindBtn) remindBtn.addEventListener('click', async function () {
    if (!confirm('Send a reminder email to everyone who has not answered yet?')) return;
    busy(remindBtn, true);
    var sent = 0, failed = 0;
    say('Sending reminders…');
    for (;;) {
      var res = await post('/admin/polls/' + encodeURIComponent(cid) + '/remind-chunk');
      if (!res.ok) { say(res.body.error || 'Reminders stopped.', true); busy(remindBtn, false); return; }
      sent += res.body.sent || 0; failed += res.body.failed || 0;
      say('Sending reminders… ' + sent + ' sent' + (failed ? ', ' + failed + ' failed' : ''));
      if (!res.body.sent) break;
    }
    reload(sent ? 'Reminded ' + sent + (sent === 1 ? ' person.' : ' people.') + (failed ? ' ' + failed + ' failed.' : '')
                : (failed ? failed + ' reminder(s) failed.' : 'Nobody needed a reminder right now (everyone has answered or was reminded in the last hour).'));
  });

  // ---- People table filter -----------------------------------------------
  var groups = {
    all: null,
    awaiting: ['awaiting', 'unsent'],
    responded: ['responded'],
    problem: ['failed', 'suppressed', 'unsent', 'excluded']
  };
  Array.prototype.forEach.call(document.querySelectorAll('[data-filter]'), function (btn) {
    btn.addEventListener('click', function () {
      Array.prototype.forEach.call(document.querySelectorAll('[data-filter]'), function (b) { b.classList.toggle('is-on', b === btn); });
      var want = groups[btn.getAttribute('data-filter')];
      Array.prototype.forEach.call(document.querySelectorAll('#oa-people tbody tr'), function (tr) {
        tr.hidden = !!want && want.indexOf(tr.getAttribute('data-state')) === -1;
      });
    });
  });
})();
