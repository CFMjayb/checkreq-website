// sma_run.js -- 26-129 SMA letters (plan revision 12, step 1): the check sheet (admin_sma_run.html).
//
//   * The Build button builds the waiting letters a few at a time (about 6 per call, so a 300-second request never
//     holds a whole run). The server keeps a per-run lock and spends no more merges than the account has left; this
//     script just keeps calling until nothing is left. If the tab is closed halfway, reload and press Build again:
//     only unbuilt letters are picked up, so nothing is built twice.
//   * The filter buttons show or hide rows of the check sheet.
// Everything works without this script except Build; nothing typed by an admin is put into the page as markup.
(function () {
  'use strict';
  var root = document.getElementById('sm-run');
  if (!root) return;
  var runId = root.getAttribute('data-run');
  var progress = document.getElementById('sm-progress');

  // ---- filters ----------------------------------------------------------------
  var buttons = Array.prototype.slice.call(document.querySelectorAll('[data-filter]'));
  var rows = Array.prototype.slice.call(document.querySelectorAll('#sm-table tbody tr[data-status]'));
  function apply(kind) {
    buttons.forEach(function (b) { b.classList.toggle('is-on', b.getAttribute('data-filter') === kind); });
    rows.forEach(function (tr) {
      var st = tr.getAttribute('data-status');
      var blocks = parseInt(tr.getAttribute('data-blocks'), 10) || 0;
      var warns = parseInt(tr.getAttribute('data-warns'), 10) || 0;
      var show = true;
      if (kind === 'attention') show = st !== 'excluded' && blocks > 0;
      else if (kind === 'warnings') show = st !== 'excluded' && warns > 0;
      else if (kind === 'ready') show = st === 'created' && blocks === 0;
      else if (kind === 'unbuilt') show = st === 'draft';
      else if (kind === 'excluded') show = st === 'excluded';
      tr.hidden = !show;
    });
  }
  buttons.forEach(function (b) { b.addEventListener('click', function () { apply(b.getAttribute('data-filter')); }); });

  // ---- chunked build ---------------------------------------------------------
  function say(msg, bad) {
    progress.hidden = false;
    progress.className = 'sm-progress' + (bad ? ' sm-bad' : '');
    progress.textContent = msg;
  }
  function post(url) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: window.csrfHeader ? window.csrfHeader() : {} })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) { return { ok: r.ok, status: r.status, body: b }; });
      });
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  var building = false;
  async function buildLoop(btn) {
    if (building) return;
    building = true;
    var built = 0, failed = 0, busy = 0, errors = [];
    say('Building letters…');
    for (;;) {
      var res = await post('/admin/sma-letters/' + encodeURIComponent(runId) + '/build-chunk');
      if (!res.ok) { say(res.body.error || 'Building stopped. Reload the page and try again.', true); building = false; if (btn) btn.disabled = false; return; }
      var b = res.body;
      if (b.busy) {
        if (++busy > 30) { say('Another build of this run is still running. Reload in a minute.', true); building = false; if (btn) btn.disabled = false; return; }
        await sleep(2000);
        continue;
      }
      built += b.built || 0;
      failed += b.failed || 0;
      (b.errors || []).forEach(function (e) { if (errors.length < 5) errors.push(e); });
      say('Building letters… ' + built + ' built' + (failed ? ', ' + failed + ' failed' : '') + (b.remaining ? ', ' + b.remaining + ' to go' : ''));
      if (!b.remaining || (!b.built && !b.failed)) break;
      if (b.failed && !b.built) break;
    }
    building = false;
    var msg = built + ' letter' + (built === 1 ? '' : 's') + ' built.' + (failed ? ' ' + failed + ' failed: ' + errors.join(' | ') : '');
    location.href = '/admin/sma-letters/' + encodeURIComponent(runId) + (failed ? '?error=' : '?msg=') + encodeURIComponent(msg);
  }
  var buildBtn = document.getElementById('sm-build');
  if (buildBtn) buildBtn.addEventListener('click', function () {
    if (typeof showButtonLoading === 'function') showButtonLoading(buildBtn);
    buildLoop(buildBtn);
  });
})();
