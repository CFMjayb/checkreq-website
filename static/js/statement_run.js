// statement_run.js -- giving statements, the staff run page. The Build button asks the server for the next few statements again and again until the run is done.
// The server builds a few per call (one browser start each), under a lock for the run, so closing the page loses nothing: pressing Build again carries on.
// No inline handlers; the CSRF header comes from csrf.js. Text is set with textContent only.
(function () {
  var btn = document.getElementById('stm-build');
  if (!btn) return;
  var bar = document.getElementById('stm-bar');
  var msg = document.getElementById('stm-msg');
  var running = false;

  function say(text) { if (msg) msg.textContent = text; }
  function setNum(id, v) { var el = document.getElementById(id); if (el) el.textContent = String(v); }

  function pause(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

  async function build() {
    if (running) return;
    running = true;
    btn.disabled = true;
    say('Building…');
    var url = btn.getAttribute('data-url');
    var stalled = 0;
    try {
      for (var i = 0; i < 400; i++) {
        var res = await fetch(url, { method: 'POST', headers: Object.assign({ 'Accept': 'application/json' }, window.csrfHeader ? window.csrfHeader() : {}), credentials: 'same-origin' });
        var data = null;
        try { data = await res.json(); } catch (e) { data = null; }
        if (!res.ok || !data) { say((data && data.error) || 'That did not work. Press Build to try again.'); break; }
        if (data.busy) { say('Another builder is working on this run. Waiting…'); await pause(3000); if (++stalled > 20) { say('Still busy. Press Build again in a minute.'); break; } continue; }
        if (data.renderer_busy) { say('The PDF builder is busy with something else. Waiting…'); await pause(4000); if (++stalled > 20) { say('Still busy. Press Build again in a minute.'); break; } continue; }
        stalled = 0;
        var total = (data.built || 0) + (data.failed || 0) + (data.remaining || 0);
        if (bar) { bar.max = total || 1; bar.value = data.built || 0; }
        setNum('stm-built', data.built || 0); setNum('stm-planned', data.remaining || 0); setNum('stm-failed', data.failed || 0);
        say((data.built || 0) + ' of ' + total + ' built' + (data.failed ? ', ' + data.failed + ' failed' : '') + '…');
        if (data.done || !data.remaining) {
          say('Done: ' + (data.built || 0) + ' built' + (data.failed ? ', ' + data.failed + ' failed' : '') + '.');
          setTimeout(function () { window.location.assign(btn.getAttribute('data-reload') || window.location.pathname); }, 700);
          return;
        }
      }
    } catch (e) {
      say('The connection was lost. Press Build to carry on.');
    }
    running = false;
    btn.disabled = false;
  }

  btn.addEventListener('click', build);
})();
