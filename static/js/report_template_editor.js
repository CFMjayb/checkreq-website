// report_template_editor.js -- 26-149 Phase 3: the Report Template editor
// (templates/admin_report_template_edit.html, routes in report_template_editor.py).
//
// Two batched-save grids (Lines, Recipients) with dirty tracking, a live
// "accounts this mask matches" preview, the "Start from chart of accounts"
// drafter, and the QuickBooks budget picker. Options and Schedule are plain
// form posts (rt-plain-form) and only get the loading pulse here.
//
// Nothing is saved until a Save button is clicked; leaving the page with
// unsaved grid changes asks first.
//
// 2026-09-23: templates have a Report Type (Actual vs Budget / Fund Summary).
// Options rows tagged data-rtype show only for their type; a Fund Summary
// template's Lines grid has a free-text Fund group column instead of the
// Revenue/Expense Section select, and "Load current Fund Account Masks"
// instead of the chart-of-accounts drafter.
(function () {
  'use strict';

  function pulse(btn) { if (window.showButtonLoading && btn) window.showButtonLoading(btn); else if (btn) btn.disabled = true; }
  function unpulse(btn) { if (btn) { btn.disabled = false; btn.classList.remove('btn-loading'); } }
  function headers() {
    var h = window.csrfHeader ? window.csrfHeader() : {};
    h['Content-Type'] = 'application/json';
    return h;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function postJson(url, body) {
    return fetch(url, { method: 'POST', headers: headers(), credentials: 'same-origin',
                        body: JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) { var e = new Error(j.error || ('Request failed (HTTP ' + r.status + ').')); e.body = j; throw e; }
          return j;
        });
      });
  }

  var banner = document.getElementById('rtBanner');
  function showBanner(kind, html) {
    if (!banner) return;
    banner.hidden = false;
    banner.className = 'banner ' + (kind === 'error' ? 'banner-error' : kind === 'ok' ? 'banner-success' : 'banner-info');
    banner.innerHTML = html;
  }

  // Plain form posts: pulse after confirm_submit.js's own check has passed.
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f.classList || !f.classList.contains('rt-plain-form') || e.defaultPrevented) return;
    pulse(e.submitter || f.querySelector('button[type="submit"]'));
  });

  // ── Report type: show only the options that belong to it ─────────────────
  var typeSel = document.getElementById('report_type');
  function applyType() {
    if (!typeSel) return;
    var t = typeSel.value;
    Array.prototype.forEach.call(document.querySelectorAll('[data-rtype]'), function (el) {
      el.hidden = el.getAttribute('data-rtype') !== t;
    });
  }
  if (typeSel) { typeSel.addEventListener('change', applyType); applyType(); }

  // ── Budget picker (Options) ───────────────────────────────────────────────
  var budgetSel = document.getElementById('budget_name');
  var budgetHint = document.getElementById('budgetHint');
  if (budgetSel) {
    fetch('/admin/report-templates/api/qbo-reference', { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { if (!r.ok) throw new Error(j.error || 'HTTP ' + r.status); return j; }); })
      .then(function (j) {
        var current = budgetSel.getAttribute('data-current') || '';
        var have = {};
        Array.prototype.forEach.call(budgetSel.options, function (o) { have[o.value] = true; });
        (j.budgets || []).forEach(function (b) {
          if (have[b.name]) return;
          var o = document.createElement('option');
          o.value = b.name;
          o.textContent = b.name + '  (' + b.start_date + ' to ' + b.end_date + (b.active ? '' : ', inactive') + ')';
          if (b.name === current) o.selected = true;
          budgetSel.appendChild(o);
        });
        budgetHint.textContent = (j.budgets || []).length
          ? 'Budgets found in QuickBooks: ' + j.budgets.length + '. Leave on Auto-pick unless more than one is active for the year.'
          : 'No Profit & Loss budgets found in QuickBooks for this entity -- budget columns will be blank.';
      })
      .catch(function (err) {
        budgetHint.textContent = "Couldn't load budget names from QuickBooks (" + err.message + '). You can still save; the current choice is kept.';
      });
  }

  var dataEl = document.getElementById('rtData');
  if (!dataEl) return;                       // new-template page: Options only
  var DATA = JSON.parse(dataEl.textContent);
  var TID = DATA.templateId;
  var FUND = DATA.reportType === 'fund_summary';
  var newSeq = 0;

  // ── Generic grid ──────────────────────────────────────────────────────────
  // cfg: {body, saveBtn, stateEl, fields: [{name, type, options}], rowHtml, url, key}
  function Grid(cfg) {
    this.cfg = cfg;
    this.rows = [];                          // {key, orig, cur, el}
  }
  Grid.prototype.load = function (records) {
    var self = this;
    this.rows = [];
    this.cfg.body.innerHTML = '';
    records.forEach(function (r) { self.add(r, false); });
    this.refreshState();
  };
  Grid.prototype.add = function (rec, isNew) {
    var key = rec.id ? String(rec.id) : 'new-' + (++newSeq);
    var row = { key: key, id: rec.id || 0, updated_at: rec.updated_at || '',
                orig: isNew ? null : JSON.stringify(this.values(rec)), cur: this.values(rec) };
    var tr = document.createElement('tr');
    tr.setAttribute('data-key', key);
    tr.innerHTML = this.cfg.rowHtml(rec);
    this.cfg.body.appendChild(tr);
    row.el = tr;
    var self = this;
    tr.addEventListener('input', function () { self.read(row); });
    tr.addEventListener('change', function () { self.read(row); });
    this.rows.push(row);
    if (this.cfg.afterAdd) this.cfg.afterAdd(row);
    this.read(row);
    return row;
  };
  Grid.prototype.values = function (rec) {
    var v = {};
    this.cfg.fields.forEach(function (f) {
      v[f] = f === 'is_active' ? rec[f] !== false : (rec[f] == null ? '' : String(rec[f]));
    });
    return v;
  };
  Grid.prototype.read = function (row) {
    var v = {};
    Array.prototype.forEach.call(row.el.querySelectorAll('[data-field]'), function (inp) {
      var f = inp.getAttribute('data-field');
      v[f] = inp.type === 'checkbox' ? inp.checked : inp.value.trim();
    });
    row.cur = v;
    var dirty = row.orig === null || JSON.stringify(v) !== row.orig;
    row.el.classList.toggle('row-dirty', dirty);
    row.el.classList.toggle('rt-row-inactive', v.is_active === false);
    this.refreshState();
  };
  Grid.prototype.dirtyRows = function () {
    return this.rows.filter(function (r) { return r.orig === null || JSON.stringify(r.cur) !== r.orig; });
  };
  Grid.prototype.refreshState = function () {
    var n = this.dirtyRows().length;
    this.cfg.saveBtn.disabled = n === 0;
    this.cfg.stateEl.className = 'save-state' + (n ? ' dirty' : '');
    this.cfg.stateEl.textContent = n ? n + ' unsaved change' + (n === 1 ? '' : 's') : 'No changes';
  };
  Grid.prototype.setRowMsg = function (key, text, kind) {
    var row = this.rows.filter(function (r) { return r.key === key; })[0];
    if (!row) return;
    var cell = row.el.lastElementChild;
    var msg = cell.querySelector('.row-msg');
    if (!msg) { msg = document.createElement('div'); msg.className = 'row-msg'; cell.appendChild(msg); }
    msg.className = 'row-msg ' + (kind || 'err');
    msg.textContent = text || '';
  };
  Grid.prototype.clearMsgs = function () {
    Array.prototype.forEach.call(this.cfg.body.querySelectorAll('.row-msg'), function (m) { m.remove(); });
  };
  Grid.prototype.save = function () {
    var self = this, btn = this.cfg.saveBtn;
    var rows = this.dirtyRows().map(function (r) {
      var o = { key: r.key, id: r.id, updated_at: r.updated_at };
      Object.keys(r.cur).forEach(function (k) { o[k] = r.cur[k]; });
      return o;
    });
    if (!rows.length) return;
    this.clearMsgs();
    pulse(btn);
    postJson(this.cfg.url, { rows: rows }).then(function (j) {
      self.load(j[self.cfg.resultKey]);
      showBanner('ok', esc(j.saved) + ' ' + self.cfg.noun + (j.saved === 1 ? '' : 's') + ' saved.');
      if (self.cfg.afterSave) self.cfg.afterSave();
    }).catch(function (err) {
      var errs = (err.body && err.body.row_errors) || {};
      Object.keys(errs).forEach(function (k) { self.setRowMsg(k, errs[k], 'err'); });
      showBanner('error', esc(err.message));
    }).then(function () { unpulse(btn); self.refreshState(); });
  };

  // ── Lines ─────────────────────────────────────────────────────────────────
  function sectionSelect(v) {
    return '<select data-field="section">' + ['Revenue', 'Expense'].map(function (s) {
      return '<option' + (s === v ? ' selected' : '') + '>' + s + '</option>';
    }).join('') + '</select>';
  }
  function groupCell(r) {
    return FUND
      ? '<input type="text" data-field="fund_group" value="' + esc(r.fund_group) + '" placeholder="e.g. Unrestricted Net Assets">'
      : sectionSelect(r.section || 'Expense');
  }
  var lines = new Grid({
    body: document.getElementById('linesBody'),
    saveBtn: document.getElementById('lineSaveBtn'),
    stateEl: document.getElementById('lineSaveState'),
    fields: ['is_active', FUND ? 'fund_group' : 'section', 'line_label', 'account_mask', 'sort_order'],
    url: '/admin/report-templates/' + TID + '/lines/save',
    resultKey: 'lines', noun: 'line',
    rowHtml: function (r) {
      return '<td class="col-allow"><input type="checkbox" data-field="is_active"' + (r.is_active === false ? '' : ' checked') + '></td>' +
        '<td class="rt-col-section">' + groupCell(r) + '</td>' +
        '<td class="col-display"><input type="text" data-field="line_label" value="' + esc(r.line_label) + '" placeholder="' + (FUND ? 'e.g. Temporarily Restricted Endowments' : 'e.g. Diocesan House') + '"></td>' +
        '<td class="rt-col-mask"><input type="text" data-field="account_mask" value="' + esc(r.account_mask) + '" placeholder="' + (FUND ? 'e.g. 3050.4*' : 'e.g. 667*, 6680-6689') + '"></td>' +
        '<td class="col-sort"><input type="number" class="num" data-field="sort_order" value="' + esc(r.sort_order == null ? 100 : r.sort_order) + '"></td>' +
        '<td class="rt-col-matches"><button type="button" class="linkish rt-match-btn" title="Show the accounts this line picks up">Preview</button></td>';
    },
    afterAdd: function (row) {
      row.el.querySelector('.rt-match-btn').addEventListener('click', function () { toggleDetail(row); });
    },
    afterSave: function () { lastPreview = null; document.getElementById('previewSummary').innerHTML = ''; }
  });
  lines.load(DATA.lines || []);

  document.getElementById('lineAddBtn').addEventListener('click', function () {
    var max = lines.rows.reduce(function (m, r) { return Math.max(m, parseInt(r.cur.sort_order, 10) || 0); }, 0);
    var row = lines.add({ section: 'Expense', fund_group: '', line_label: '', account_mask: '', sort_order: max + 10, is_active: true }, true);
    row.el.querySelector('[data-field="line_label"]').focus();
  });
  document.getElementById('lineSaveBtn').addEventListener('click', function () { lines.save(); });

  // ── Preview ───────────────────────────────────────────────────────────────
  var lastPreview = null;
  function previewRows() {
    return lines.rows.map(function (r) {
      return { key: r.key, id: r.id, section: FUND ? 'Equity' : r.cur.section, line_label: r.cur.line_label || '(unnamed line)',
               account_mask: r.cur.account_mask, sort_order: r.cur.sort_order, is_active: r.cur.is_active };
    });
  }
  function runPreview(btn, refresh) {
    pulse(btn);
    return postJson('/admin/report-templates/' + TID + '/preview',
                    { rows: previewRows(), refresh: !!refresh, full_entity: !!DATA.fullEntity })
      .then(function (j) { lastPreview = j; renderPreview(j); return j; })
      .catch(function (err) { showBanner('error', esc(err.message)); throw err; })
      .then(function (j) { unpulse(btn); return j; }, function (e) { unpulse(btn); throw e; });
  }
  function renderPreview(j) {
    lines.rows.forEach(function (r) {
      var btn = r.el.querySelector('.rt-match-btn');
      var res = j.lines[r.key];
      if (j.invalid && j.invalid[r.key]) { btn.textContent = 'Invalid mask'; btn.className = 'linkish rt-match-btn rt-bad'; btn.title = j.invalid[r.key]; return; }
      if (!res) { btn.textContent = r.cur.is_active ? 'Preview' : 'Inactive'; btn.className = 'linkish rt-match-btn'; return; }
      btn.textContent = res.count + ' account' + (res.count === 1 ? '' : 's') + (res.section_mismatch ? ' (' + res.section_mismatch + ' off-section)' : '');
      btn.className = 'linkish rt-match-btn' + (res.count === 0 || res.section_mismatch ? ' rt-bad' : '');
      btn.title = 'Show the accounts this line picks up';
    });
    var parts = [];
    parts.push('<p class="setup-hint">Checked against ' + esc(j.account_count) +
      (FUND ? ' active QuickBooks equity accounts (the only accounts a Fund Summary reads).</p>'
            : ' QuickBooks accounts (active and inactive, as the report does).</p>'));
    var empty = lines.rows.filter(function (r) { return j.lines[r.key] && j.lines[r.key].count === 0; });
    if (empty.length) parts.push('<div class="banner banner-error">' + empty.length + ' active line' + (empty.length === 1 ? ' matches' : 's match') + ' no accounts at all.</div>');
    if (j.overlaps && j.overlaps.length) {
      parts.push('<div class="banner banner-info"><strong>' + j.overlaps.length + ' account' + (j.overlaps.length === 1 ? ' is' : 's are') +
        ' matched by more than one line</strong> &mdash; each is reported only under the first line (by Sort):<ul class="popup-list">' +
        j.overlaps.slice(0, 25).map(function (o) { return '<li>' + esc(o.acct_num) + ' ' + esc(o.name) + ' &rarr; ' + o.lines.map(esc).join(', ') + '</li>'; }).join('') +
        (j.overlaps.length > 25 ? '<li>&hellip;and ' + (j.overlaps.length - 25) + ' more</li>' : '') + '</ul></div>');
    }
    if (DATA.fullEntity) {
      var un = j.unmapped || [];
      parts.push('<div class="banner ' + (un.length ? 'banner-info' : 'banner-success') + '">' +
        (un.length ? '<strong>' + un.length + ' revenue/expense account' + (un.length === 1 ? '' : 's') +
          ' not on any line</strong> &mdash; they will appear under "Unmapped Accounts":<ul class="popup-list">' +
          un.slice(0, 25).map(function (a) { return '<li>' + esc(a.acct_num) + ' ' + esc(a.name) + (a.active ? '' : ' <em>(inactive)</em>') + '</li>'; }).join('') +
          (un.length > 25 ? '<li>&hellip;and ' + (un.length - 25) + ' more</li>' : '') + '</ul>'
          : 'Every revenue and expense account is on a line.') + '</div>');
    }
    document.getElementById('previewSummary').innerHTML = parts.join('');
    // refresh any open detail rows
    lines.rows.forEach(function (r) { if (r.detailEl) fillDetail(r); });
  }
  function fillDetail(row) {
    var res = lastPreview && lastPreview.lines[row.key];
    var td = row.detailEl.firstElementChild;
    if (lastPreview && lastPreview.invalid && lastPreview.invalid[row.key]) { td.innerHTML = '<span class="rt-bad">' + esc(lastPreview.invalid[row.key]) + '</span>'; return; }
    if (!res) { td.innerHTML = '<span class="sub">Inactive lines aren\'t matched.</span>'; return; }
    if (!res.matches.length) { td.innerHTML = '<span class="rt-bad">No QuickBooks account matches this mask.</span>'; return; }
    td.innerHTML = '<table class="rt-match-table"><thead><tr><th>Account</th><th>Name</th><th>Type</th><th>Parent</th></tr></thead><tbody>' +
      res.matches.map(function (m) {
        return '<tr class="' + (m.wrong_section ? 'rt-bad' : '') + '"><td>' + esc(m.acct_num) + '</td><td>' + esc(m.name) +
          (m.active ? '' : ' <em>(inactive)</em>') + '</td><td>' + esc(m.classification) +
          (m.wrong_section ? ' &mdash; not ' + esc(row.cur.section) : '') + '</td><td>' + esc(m.parent_acct_num) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }
  function toggleDetail(row) {
    if (row.detailEl) { row.detailEl.remove(); row.detailEl = null; return; }
    var tr = document.createElement('tr');
    tr.className = 'rt-detail-row';
    tr.innerHTML = '<td colspan="6"><span class="sub">Loading accounts from QuickBooks&hellip;</span></td>';
    row.el.parentNode.insertBefore(tr, row.el.nextSibling);
    row.detailEl = tr;
    var btn = row.el.querySelector('.rt-match-btn');
    runPreview(btn).then(function () { if (row.detailEl) fillDetail(row); }, function () {
      if (row.detailEl) row.detailEl.firstElementChild.innerHTML = '<span class="rt-bad">Preview failed -- see the message above.</span>';
    });
  }
  document.getElementById('previewAllBtn').addEventListener('click', function () { runPreview(this); });

  // ── Start from chart of accounts ──────────────────────────────────────────
  var starterOut = document.getElementById('starterResults');
  var starterBtn = document.getElementById('starterDraftBtn');
  if (starterBtn) starterBtn.addEventListener('click', function () {
    var btn = this;
    pulse(btn);
    postJson('/admin/report-templates/' + TID + '/starter-lines', {
      section: document.getElementById('starterSection').value,
      prefix: document.getElementById('starterPrefix').value,
      include_inactive: document.getElementById('starterInactive').checked,
      existing_masks: lines.rows.filter(function (r) { return r.cur.is_active && r.cur.account_mask; })
                                .map(function (r) { return r.cur.account_mask; })
    }).then(function (j) {
      var s = j.lines || [];
      if (!s.length) { starterOut.innerHTML = '<p class="sub">No uncovered top-level accounts match that filter.</p>'; return; }
      starterOut.innerHTML = '<div class="setup-toolbar"><label class="rt-inline-check"><input type="checkbox" id="starterAll" checked> Select all (' + s.length + ')</label>' +
        '<div class="spacer"></div><button type="button" class="btn btn-primary btn-sm" id="starterAddBtn">Add Selected to Grid</button></div>' +
        '<div class="rt-starter-list">' + s.map(function (x, i) {
          return '<label><input type="checkbox" class="starter-pick" data-i="' + i + '" checked> <span class="rt-sec rt-sec-' + x.section.toLowerCase() + '">' + esc(x.section) + '</span> ' +
            '<strong>' + esc(x.line_label) + '</strong> <code>' + esc(x.account_mask) + '</code>' +
            (x.sub_accounts ? ' <span class="sub">+' + x.sub_accounts + ' sub-account' + (x.sub_accounts === 1 ? '' : 's') + '</span>' : '') + '</label>';
        }).join('') + '</div>';
      document.getElementById('starterAll').addEventListener('change', function () {
        var on = this.checked;
        Array.prototype.forEach.call(starterOut.querySelectorAll('.starter-pick'), function (c) { c.checked = on; });
      });
      document.getElementById('starterAddBtn').addEventListener('click', function () {
        var base = lines.rows.reduce(function (m, r) { return Math.max(m, parseInt(r.cur.sort_order, 10) || 0); }, 0);
        var added = 0;
        Array.prototype.forEach.call(starterOut.querySelectorAll('.starter-pick:checked'), function (c) {
          var x = s[parseInt(c.getAttribute('data-i'), 10)];
          added += 1;
          lines.add({ section: x.section, line_label: x.line_label, account_mask: x.account_mask,
                      sort_order: base + added * 10, is_active: true }, true);
        });
        starterOut.innerHTML = '<p class="sub">' + added + ' line' + (added === 1 ? '' : 's') +
          ' added to the grid below, not saved yet. Review them, then click Save Lines.</p>';
      });
    }).catch(function (err) {
      starterOut.innerHTML = '<p class="rt-bad">' + esc(err.message) + '</p>';
    }).then(function () { unpulse(btn); });
  });

  // ── Load current Fund Account Masks (Fund Summary templates) ─────────────
  var fundBtn = document.getElementById('fundMaskLoadBtn');
  var fundOut = document.getElementById('fundMaskResults');
  if (fundBtn) fundBtn.addEventListener('click', function () {
    var btn = this;
    pulse(btn);
    postJson('/admin/report-templates/' + TID + '/fund-mask-lines', {}).then(function (j) {
      var have = {};
      lines.rows.forEach(function (r) { if (r.cur.is_active && r.cur.account_mask) have[r.cur.account_mask.trim()] = true; });
      var added = 0, skipped = 0;
      (j.lines || []).forEach(function (x) {
        if (have[x.account_mask]) { skipped += 1; return; }
        lines.add({ fund_group: x.fund_group, line_label: x.line_label, account_mask: x.account_mask,
                    sort_order: x.sort_order, is_active: true }, true);
        added += 1;
      });
      fundOut.innerHTML = '<p class="sub">' + (j.lines && j.lines.length
        ? added + ' line' + (added === 1 ? '' : 's') + ' added to the grid below, not saved yet' +
          (skipped ? ' (' + skipped + ' already on the template)' : '') + '. Review them, then click Save Lines.'
        : 'No Fund Account Masks are on file for ' + esc(j.company) + '.') + '</p>';
    }).catch(function (err) {
      fundOut.innerHTML = '<p class="rt-bad">' + esc(err.message) + '</p>';
    }).then(function () { unpulse(btn); });
  });

  // ── Recipients ────────────────────────────────────────────────────────────
  var recips = new Grid({
    body: document.getElementById('recipBody'),
    saveBtn: document.getElementById('recipSaveBtn'),
    stateEl: document.getElementById('recipSaveState'),
    fields: ['is_active', 'recipient_type', 'email', 'name'],
    url: '/admin/report-templates/' + TID + '/recipients/save',
    resultKey: 'recipients', noun: 'recipient',
    rowHtml: function (r) {
      var t = r.recipient_type || 'to';
      return '<td class="col-allow"><input type="checkbox" data-field="is_active"' + (r.is_active === false ? '' : ' checked') + '></td>' +
        '<td class="rt-col-section"><select data-field="recipient_type"><option value="to"' + (t === 'to' ? ' selected' : '') +
        '>To</option><option value="cc"' + (t === 'cc' ? ' selected' : '') + '>Cc</option></select></td>' +
        '<td class="col-email"><input type="email" data-field="email" value="' + esc(r.email) + '" placeholder="name@example.org"></td>' +
        '<td class="col-display"><input type="text" data-field="name" value="' + esc(r.name) + '"></td>';
    }
  });
  recips.load(DATA.recipients || []);
  document.getElementById('recipAddBtn').addEventListener('click', function () {
    var row = recips.add({ recipient_type: 'to', email: '', name: '', is_active: true }, true);
    row.el.querySelector('[data-field="email"]').focus();
  });
  document.getElementById('recipSaveBtn').addEventListener('click', function () { recips.save(); });

  // ── Unsaved-changes guard ─────────────────────────────────────────────────
  window.addEventListener('beforeunload', function (e) {
    if (lines.dirtyRows().length || recips.dirtyRows().length) { e.preventDefault(); e.returnValue = ''; }
  });
  // A plain form post (Options/Schedule/Clone/...) is a deliberate navigation:
  // warn only if the grids hold unsaved work, via the same beforeunload above.
})();
