// report_templates.js -- 26-149 "Run Now" on the Report Templates admin screen.
//
// The run route returns the .xlsx itself, so a plain form POST would leave the
// button spinning forever (the page never navigates). Instead: fetch the file,
// hand it to the browser as a download, and report the engine's own
// tie-out / hold result in the status banner.
(function () {
  var status = document.getElementById('reportRunStatus');

  function show(kind, text) {
    status.hidden = false;
    status.className = 'banner ' + (kind === 'error' ? 'banner-error' : 'banner-info');
    status.textContent = text;
  }

  function filenameFrom(resp, fallback) {
    var disp = resp.headers.get('Content-Disposition') || '';
    var m = disp.match(/filename="?([^";]+)"?/);
    return m ? m[1] : fallback;
  }

  document.querySelectorAll('form.report-run-form').forEach(function (form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var btn = form.querySelector('button[type="submit"]');
      var name = form.getAttribute('data-template-name') || 'Report';
      var month = form.querySelector('input[name="month"]').value;
      if (window.showButtonLoading && btn) { window.showButtonLoading(btn); }
      else if (btn) { btn.disabled = true; }
      show('info', 'Building ' + name + ' for ' + month + '…');

      fetch(form.action, {
        method: 'POST',
        headers: window.csrfHeader ? window.csrfHeader() : {},
        body: new FormData(form),
        credentials: 'same-origin'
      }).then(function (resp) {
        if (!resp.ok) {
          return resp.json().catch(function () { return {}; }).then(function (j) {
            throw new Error(j.error || ('The report could not be built (HTTP ' + resp.status + ').'));
          });
        }
        var holds = parseInt(resp.headers.get('X-Report-Holds') || '0', 10);
        var tie = resp.headers.get('X-Report-Tie-Out') || '';
        var fname = filenameFrom(resp, name + ' ' + month + '.xlsx');
        return resp.blob().then(function (blob) {
          var url = URL.createObjectURL(blob);
          var a = document.createElement('a');
          a.href = url; a.download = fname;
          document.body.appendChild(a); a.click(); a.remove();
          setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
          var msg = 'Downloaded ' + fname + '. ' +
            (tie === 'ok' ? 'Tie-out to QuickBooks passed.' : 'Tie-out to QuickBooks FAILED - see the first page.');
          if (holds > 0) {
            msg += ' ' + holds + ' hold' + (holds === 1 ? '' : 's') +
              ' raised (shown at the top of the report) - it would not be sent automatically.';
          }
          show(tie === 'ok' && holds === 0 ? 'info' : 'error', msg);
        });
      }).catch(function (err) {
        show('error', err.message || String(err));
      }).then(function () {
        if (btn) { btn.disabled = false; btn.classList.remove('btn-loading'); }
      });
    });
  });
})();

// 26-149 Phase 3: Clone / Deactivate / Activate are plain form posts. Pulse the
// button (Standing UI-UX Rule 6) only once confirm_submit.js's own
// document-level data-confirm check has passed -- this listener is registered
// after it (base.html loads that script first), so a cancelled confirm() is
// already visible here as defaultPrevented and the button is left alone.
document.addEventListener('submit', function (e) {
  var form = e.target;
  if (!form.classList || !form.classList.contains('rt-plain-form') || e.defaultPrevented) return;
  if (window.showButtonLoading) {
    window.showButtonLoading(e.submitter || form.querySelector('button[type="submit"]'));
  }
});
