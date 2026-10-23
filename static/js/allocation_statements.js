/* allocation_statements.js -- the Allocation Statements board (allocation_statements.py, templates/admin_allocation_statements.html).

   1. Fills each church's row from QuickBooks, four at a time (the page lists every church at once; this fills the figures in).
   2. Generate all / Generate checked / Email checked: one church per request (a statement is a real PDF render, a few seconds each),
      with progress in the footer. Generate then downloads the new statements in one zip. Email asks first.
   Everything shown is put on the page as text (never as markup), so a church name or an email address cannot run as a script. */
(function () {
  'use strict';
  var data = document.getElementById('asData');
  var table = document.querySelector('table.ff-table');
  if (!data || !table) return;
  var year = data.dataset.year;
  var isDev = data.dataset.dev === '1';
  var logOk = data.dataset.logOk === '1';
  var tbody = table.querySelector('tbody');
  var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr.ff-row'));
  var loadMsg = document.getElementById('asLoadMsg');
  var runMsg = document.getElementById('asRunMsg');
  var btnAll = document.getElementById('asGenerateAll');
  var btnChecked = document.getElementById('asGenerateChecked');
  var btnEmail = document.getElementById('asEmailChecked');
  var checkAll = document.getElementById('asCheckAll');
  var search = document.querySelector('[data-ff-search]');
  var loaded = 0, running = false;

  function money(n) { return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
  function cell(tr, cls) { return tr.querySelector('.' + cls); }
  function setText(el, text, sortValue) { el.textContent = text; if (sortValue !== undefined) el.dataset.v = String(sortValue); }
  function refilter() { if (search) search.dispatchEvent(new Event('input')); }   // fixed_frame.js re-applies its filters on this event
  function status(tr, state, label, badge) {
    tr.dataset.state = state;
    tr.classList.remove('is-ok', 'is-miss', 'is-attn', 'is-idle');
    tr.classList.add({ paid: 'is-ok', owes: 'is-miss', error: 'is-attn', none: 'is-idle', nolink: 'is-idle' }[state] || 'is-idle');
    var td = cell(tr, 'as-status');
    td.textContent = '';
    var span = document.createElement('span');
    span.className = 'badge ' + badge;
    span.textContent = label;
    td.appendChild(span);
    td.dataset.v = label;
  }
  function checkbox(tr) { return tr.querySelector('.as-check'); }
  function name(tr) { return tr.children[1].textContent.replace(/\s+/g, ' ').trim(); }

  // ---- 1. fill the rows (six churches at a time) -----------------------------------------------------------
  function fillRow(tr, d) {
    var cb = checkbox(tr);
    if (!d || d.ok === false) { status(tr, 'error', 'Problem', 'badge-rejected'); setText(cell(tr, 'as-alloc'), '—', -1); return; }
    if (!d.linked) {
      status(tr, 'nolink', 'No QuickBooks link', 'badge-cancelled');
      ['as-alloc', 'as-paid', 'as-balance'].forEach(function (c) { setText(cell(tr, c), '—', -1); });
      return;
    }
    if (d.error) {
      status(tr, 'error', 'QuickBooks problem', 'badge-rejected');
      cell(tr, 'as-status').dataset.tip = d.error;
      ['as-alloc', 'as-paid', 'as-balance'].forEach(function (c) { setText(cell(tr, c), '—', -1); });
      return;
    }
    if (!d.has_statement) {
      status(tr, 'none', 'No ' + year + ' allocation', 'badge-cancelled');
      ['as-alloc', 'as-paid', 'as-balance'].forEach(function (c) { setText(cell(tr, c), '—', -1); });
      return;
    }
    setText(cell(tr, 'as-alloc'), money(d.original), d.original);
    setText(cell(tr, 'as-paid'), money(d.paid), d.paid);
    setText(cell(tr, 'as-balance'), money(d.balance), d.balance);
    if (d.balance > 0.004) status(tr, 'owes', 'Balance due', 'badge-pending');
    else status(tr, 'paid', 'Paid in full', 'badge-approved');
    var emails = d.emails || [];
    var em = cell(tr, 'as-email');
    tr.dataset.emails = emails.join(', ');
    if (emails.length) {
      em.textContent = emails[0] + (emails.length > 1 ? ' +' + (emails.length - 1) : '');
      em.dataset.tip = emails.join(', ');
      em.dataset.v = emails[0];
    } else {
      em.textContent = 'none on file';
      em.classList.add('c-muted');
    }
    cb.disabled = false;
  }

  function fetchRow(tr) {
    return fetch('/admin/allocation-statements/row/' + tr.dataset.id + '?year=' + encodeURIComponent(year), { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) { fillRow(tr, d); })
      .catch(function () { fillRow(tr, { ok: false }); });
  }

  function loadAll() {
    var queue = rows.slice(), active = 0;
    function paint() {
      loadMsg.textContent = loaded >= rows.length
        ? 'All ' + rows.length + ' churches loaded from QuickBooks'
        : 'Loading figures from QuickBooks: ' + loaded + ' of ' + rows.length;
    }
    function next() {
      while (active < 6 && queue.length) {
        var tr = queue.shift();
        active++;
        fetchRow(tr).then(function () {
          active--; loaded++; paint(); buttons();
          if (loaded >= rows.length) { refilter(); } else { next(); }
        });
      }
    }
    if (!rows.length) { paint(); return; }
    paint(); next();
  }

  // ---- 2. who is ticked, what the buttons may do ----------------------------------------------------------------
  function ticked() { return rows.filter(function (tr) { var cb = checkbox(tr); return cb && cb.checked && !cb.disabled && !tr.hidden; }); }
  function available() { return rows.filter(function (tr) { var cb = checkbox(tr); return cb && !cb.disabled; }); }
  function buttons() {
    // A church can only be ticked once its own row has loaded, so the "checked" buttons need no wait for the
    // rest of the list. "Generate all" does wait, so "all" really means every church that has a statement.
    var n = ticked().length;
    if (btnAll) btnAll.disabled = running || loaded < rows.length || available().length === 0;
    if (btnChecked) btnChecked.disabled = running || n === 0;
    if (btnEmail) btnEmail.disabled = running || n === 0 || !logOk;      // no log, no email (see allocation_statements.py)
    if (checkAll) checkAll.disabled = running || available().length === 0;
  }
  tbody.addEventListener('change', function (e) { if (e.target.classList && e.target.classList.contains('as-check')) buttons(); });
  if (checkAll) checkAll.addEventListener('change', function () {
    rows.forEach(function (tr) { var cb = checkbox(tr); if (cb && !cb.disabled && !tr.hidden) cb.checked = checkAll.checked; });
    buttons();
  });
  if (search) search.addEventListener('input', function () { setTimeout(buttons, 0); });
  var yearSel = document.getElementById('asYear');
  if (yearSel) yearSel.addEventListener('change', function () { yearSel.form.submit(); });

  // ---- 3. generate / email, one church at a time ---------------------------------------------------------------------
  function post(tr, action) {
    return fetch('/admin/allocation-statements/build/' + tr.dataset.id + '?year=' + encodeURIComponent(year) + '&action=' + action,
      { method: 'POST', credentials: 'same-origin', headers: window.csrfHeader ? window.csrfHeader() : {} })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'The server gave an unexpected answer (' + r.status + ').' }; }); })
      .catch(function () { return { ok: false, error: 'The connection was lost.' }; });
  }

  function applyResult(tr, d, action) {
    if (d.generated_at) setText(cell(tr, 'as-generated'), d.generated_at, d.generated_at);
    if (typeof d.balance === 'number') setText(cell(tr, 'as-balance'), money(d.balance), d.balance);
    var em = cell(tr, 'as-emailed');
    if (action === 'email') {
      if (d.ok && d.emailed_at) {
        em.textContent = d.emailed_at; em.dataset.v = d.emailed_at; em.dataset.tip = 'Sent to ' + (d.emailed_to || '');
        tr.dataset.emailed = '1';
      } else if (d.failed) {
        em.textContent = '';
        var s = document.createElement('span'); s.className = 'ph-state-attn'; s.textContent = 'Failed';
        em.appendChild(s); em.dataset.tip = d.error || '';
      }
    }
  }

  function run(action, list) {
    if (running || !list.length) return;
    running = true; buttons();
    var total = list.length, done = 0, ok = 0, bad = [], okIds = [];
    runMsg.className = 'ff-msg';
    window.onbeforeunload = function () { return 'Statements are still being built.'; };
    function finish() {
      running = false; window.onbeforeunload = null; buttons();
      var verb = action === 'email' ? 'Emailed' : 'Generated';
      var text = verb + ' ' + ok + ' of ' + total + '.';
      if (bad.length) text += ' ' + bad.length + ' did not go through: ' + bad.slice(0, 3).join('; ') + (bad.length > 3 ? '; …' : '');
      runMsg.textContent = text;
      runMsg.className = 'ff-msg ' + (bad.length ? 'attn' : 'ok');
      if (action === 'generate' && okIds.length) {
        var url = '/admin/allocation-statements/download.zip?year=' + encodeURIComponent(year) + '&ids=' + okIds.join(',');
        var a = document.createElement('a'); a.href = url; a.textContent = ' Download the zip again';
        runMsg.appendChild(a);
        window.location.href = url;
      }
    }
    (function next() {
      if (!list.length) { finish(); return; }
      var tr = list.shift();
      runMsg.textContent = (action === 'email' ? 'Emailing ' : 'Generating ') + (done + 1) + ' of ' + total + ': ' + name(tr);
      post(tr, action).then(function (d) {
        applyResult(tr, d, action);
        if (d.ok) { ok++; okIds.push(tr.dataset.id); } else { bad.push(name(tr) + ' (' + (d.error || 'failed') + ')'); }
        done++; next();
      });
    })();
  }

  if (btnAll) btnAll.addEventListener('click', function () { run('generate', available()); });
  if (btnChecked) btnChecked.addEventListener('click', function () { run('generate', ticked()); });

  // ---- 4. email: always asks first ---------------------------------------------------------------------------------------
  var dialog = document.getElementById('asEmailDialog');
  var dialogText = document.getElementById('asEmailText');
  var pendingEmail = [];
  function askEmail(list) {
    if (!list.length || running) return;
    var withEmail = list.filter(function (tr) { return tr.dataset.emails; });
    var without = list.length - withEmail.length;
    var again = withEmail.filter(function (tr) { return tr.dataset.emailed === '1'; }).length;
    var text = 'Email the ' + year + ' ' + data.dataset.line + ' statement to ' + withEmail.length + ' church' + (withEmail.length === 1 ? '' : 'es') + '.';
    if (without) text += ' ' + without + ' ticked ' + (without === 1 ? 'church has' : 'churches have') + ' no email address on its QuickBooks record and will be skipped.';
    if (again) text += ' ' + again + ' of them ' + (again === 1 ? 'was' : 'were') + ' already emailed a ' + year + ' statement and will get another.';
    text += isDev ? ' This is the development site: every email goes to the test address instead.' : ' Each church gets its own email with its statement attached.';
    dialogText.textContent = text;
    pendingEmail = withEmail;
    document.getElementById('asEmailGo').disabled = withEmail.length === 0;
    dialog.showModal();
  }
  if (btnEmail) btnEmail.addEventListener('click', function () { askEmail(ticked()); });
  document.getElementById('asEmailGo').addEventListener('click', function () {
    dialog.close();
    run('email', pendingEmail.slice());
  });

  // ---- a row's three-dot menu -------------------------------------------------------------------------------------------------
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-as-act]') : null;
    if (!b) return;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));   // closes the three-dot menu the button was in
    var tr = rows.filter(function (r) { return r.dataset.id === b.dataset.id; })[0];
    if (!tr || running) return;
    if (checkbox(tr).disabled) { runMsg.textContent = name(tr) + ' has no ' + year + ' allocation to build a statement from.'; runMsg.className = 'ff-msg attn'; return; }
    if (b.dataset.asAct === 'email') askEmail([tr]); else run('generate', [tr]);
  });

  loadAll();
})();
