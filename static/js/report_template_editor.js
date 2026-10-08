// report_template_editor.js -- 26-149 Phase 3: the Report Template editor
// (templates/admin_report_template_edit.html, routes in report_template_editor.py).
//
// Two batched-save grids (Lines, Recipients) with dirty tracking, a live
// "accounts this mask matches" preview, the "Start from chart of accounts"
// drafter, and the QuickBooks budget picker. Options and Schedule save in
// place with fetch() too (see "In-place saves" at the bottom), so every
// section saves only itself and nothing on the page ever reloads under
// another section's unsaved work. (Before 2026-10-07 they were plain form
// posts that redirected, and one Save Schedule click wiped unsaved lines
// and recipients.)
//
// Nothing is saved until a Save button is clicked; leaving the page with
// unsaved changes in any section asks first.
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

  // Plain form posts: pulse after confirm_submit.js's own check has passed. (Options and Schedule on an
  // existing template carry data-ajax: they save in place below and pulse for themselves.)
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f.classList || !f.classList.contains('rt-plain-form') || e.defaultPrevented) return;
    if (f.hasAttribute('data-ajax')) return;
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
  // "No budget comparison" is a choice in this same list (value __none__): the report shows year-to-date
  // actuals only, and QuickBooks' budget is never read. Its explanation replaces the budget-count hint.
  var NO_BUDGET = '__none__';
  var budgetHintLoaded = budgetHint ? budgetHint.textContent : '';
  function showBudgetHint() {
    if (!budgetHint) return;
    budgetHint.textContent = budgetSel.value === NO_BUDGET
      ? 'No budget comparison: the report shows year-to-date actuals only, with no budget, variance or annual budget columns. QuickBooks’ budget is not read.'
      : budgetHintLoaded;
  }
  if (budgetSel) {
    budgetSel.addEventListener('change', showBudgetHint);
    showBudgetHint();
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
        budgetHintLoaded = (j.budgets || []).length
          ? 'Budgets found in QuickBooks: ' + j.budgets.length + '. Leave on Auto-pick unless more than one is active for the year.'
          : 'No Profit & Loss budgets found in QuickBooks for this entity -- budget columns will be blank. (Choose No budget comparison to drop them.)';
        showBudgetHint();
      })
      .catch(function (err) {
        budgetHintLoaded = "Couldn't load budget names from QuickBooks (" + err.message + '). You can still save; the current choice is kept.';
        showBudgetHint();
      });
  }

  var dataEl = document.getElementById('rtData');
  if (!dataEl) return;                       // new-template page: Options only
  var DATA = JSON.parse(dataEl.textContent);
  var TID = DATA.templateId;
  var FUND = DATA.reportType === 'fund_summary';
  var OPTS = !!DATA.lineOptions;               // % charged / Class / Show columns (Actual vs Budget, migration 071)
  var newSeq = 0;

  // A line's class selection is always compared and sent as the sorted list of class ids
  // (names are display-only and re-read from QuickBooks by the server).
  function classIds(v) {
    if (typeof v === 'string') { try { v = v.trim() ? JSON.parse(v) : []; } catch (e) { v = []; } }
    return (Array.isArray(v) ? v : []).map(function (e) {
      return String(e && typeof e === 'object' ? e.id : e == null ? '' : e);
    }).filter(function (id, i, a) { return a.indexOf(id) === i; }).sort();
  }
  function classKey(v) { return JSON.stringify(classIds(v)); }

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
  // Every row starts with a remove button. A row that was never saved is dropped on the spot
  // (nothing to lose). A saved row is only MARKED for removal -- struck through, inputs
  // locked, with an Undo -- and is deleted when the grid's Save button is clicked.
  var REMOVE_CELL = '<td class="rt-col-remove"><button type="button" class="rt-remove" data-remove ' +
    'title="Remove this row" aria-label="Remove this row">&#10005;</button></td>';
  Grid.prototype.toggleRemove = function (row) {
    if (!row.id) {                           // never saved: drop it now
      if (row.detailEl) { row.detailEl.remove(); row.detailEl = null; }
      row.el.remove();
      this.rows = this.rows.filter(function (r) { return r !== row; });
      if (this.cfg.onRowGone) this.cfg.onRowGone(row);
      this.refreshState();
      return;
    }
    row.removed = !row.removed;
    var tr = row.el, btn = tr.querySelector('[data-remove]');
    tr.classList.toggle('rt-row-removed', row.removed);
    Array.prototype.forEach.call(tr.querySelectorAll('input, select, button'), function (el) {
      if (el === btn) return;
      if (row.removed) el.setAttribute('disabled', 'disabled'); else el.removeAttribute('disabled');
    });
    btn.innerHTML = row.removed ? '&#8634;' : '&#10005;';
    btn.title = row.removed ? 'Undo -- keep this row' : 'Remove this row';
    btn.setAttribute('aria-label', btn.title);
    if (row.removed) {
      if (row.detailEl) { row.detailEl.remove(); row.detailEl = null; }
      if (this.cfg.onRowGone) this.cfg.onRowGone(row);
    }
    this.read(row);                          // refreshes the dirty highlight and the unsaved count
  };
  Grid.prototype.add = function (rec, isNew) {
    var key = rec.id ? String(rec.id) : 'new-' + (++newSeq);
    var row = { key: key, id: rec.id || 0, updated_at: rec.updated_at || '', rec: rec, removed: false,
                orig: isNew ? null : JSON.stringify(this.values(rec)), cur: this.values(rec) };
    var tr = document.createElement('tr');
    tr.setAttribute('data-key', key);
    tr.innerHTML = REMOVE_CELL + this.cfg.rowHtml(rec);       // first cell: the Lines table is wider than many screens, so a last-column button would sit off-screen
    this.cfg.body.appendChild(tr);
    row.el = tr;
    var self = this;
    tr.addEventListener('input', function () { self.read(row); });
    tr.addEventListener('change', function () { self.read(row); });
    tr.querySelector('[data-remove]').addEventListener('click', function () { self.toggleRemove(row); });
    this.rows.push(row);
    if (this.cfg.afterAdd) this.cfg.afterAdd(row);
    this.read(row);
    return row;
  };
  Grid.prototype.values = function (rec) {
    var v = {};
    this.cfg.fields.forEach(function (f) {
      v[f] = f === 'is_active' ? rec[f] !== false
           : f === 'class_filter' ? classKey(rec[f])
           : (rec[f] == null ? '' : String(rec[f]));
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
    var dirty = row.orig === null || row.removed || JSON.stringify(v) !== row.orig;
    row.el.classList.toggle('row-dirty', dirty);
    row.el.classList.toggle('rt-row-inactive', v.is_active === false);
    this.refreshState();
  };
  Grid.prototype.dirtyRows = function () {
    return this.rows.filter(function (r) { return r.orig === null || r.removed || JSON.stringify(r.cur) !== r.orig; });
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
      if (r.removed) { o._delete = true; return o; }      // a removal carries nothing else
      Object.keys(r.cur).forEach(function (k) { o[k] = r.cur[k]; });
      return self.cfg.payload ? self.cfg.payload(o) : o;
    });
    if (!rows.length) return;
    this.clearMsgs();
    if (this.cfg.validateRow) {              // refuse locally what the server would refuse
      var bad = false;
      this.dirtyRows().forEach(function (r) {
        if (r.removed) return;               // going away, so nothing to validate
        var m = self.cfg.validateRow(r);
        if (m) { self.setRowMsg(r.key, m, 'err'); bad = true; }
      });
      if (bad) { showBanner('error', 'Fix the highlighted lines, then save again.'); return; }
    }
    var gone = this.dirtyRows().filter(function (r) { return r.removed; }).length;
    if (gone && !window.confirm('Permanently remove ' + gone + ' ' + this.cfg.noun + (gone === 1 ? '' : 's') +
                                '? This cannot be undone.')) return;
    pulse(btn);
    postJson(this.cfg.url, { rows: rows }).then(function (j) {
      self.load(j[self.cfg.resultKey]);
      var parts = [];
      if (j.saved) parts.push(esc(j.saved) + ' ' + self.cfg.noun + (j.saved === 1 ? '' : 's') + ' saved');
      if (j.removed) parts.push(esc(j.removed) + ' removed');
      showBanner('ok', parts.length ? parts.join(', ') + '.' : 'Nothing needed changing.');
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
  // ── Class picker (Actual vs Budget lines) ─────────────────────────────────
  // One popover, appended to <body> with position:fixed so the table's scroll box can't
  // clip it, re-pointed at whichever row's button was clicked.
  var classData = null;          // [{id, name, active}] once loaded from QuickBooks
  var classErr = '';
  var classLoading = false;
  var pop = null, popRow = null;

  function classNameOf(row, id) {
    if (id === '') return '(No class)';
    var hit = (classData || []).filter(function (c) { return c.id === id; })[0];
    if (hit) return hit.name;
    return (row.classNames && row.classNames[id]) || ('class ' + id);
  }
  function rowClassIds(row) { return classIds(row.el.querySelector('[data-field="class_filter"]').value); }
  function refreshClassBtn(row) {
    var btn = row.el.querySelector('.rt-class-btn');
    if (!btn) return;
    var ids = rowClassIds(row);
    btn.textContent = !ids.length ? 'All classes' : ids.length === 1 ? classNameOf(row, ids[0]) : ids.length + ' classes';
    btn.title = ids.length ? ids.map(function (i) { return classNameOf(row, i); }).join('\n') : 'Any class';
    btn.classList.toggle('rt-class-set', ids.length > 0);
  }
  function closePicker() { if (pop) { pop.remove(); pop = null; } popRow = null; }
  function loadClasses(refresh) {
    classLoading = true; classErr = '';
    return fetch('/admin/report-templates/api/classes' + (refresh ? '?refresh=1' : ''), { credentials: 'same-origin' })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j; }); })
      .then(function (j) { classData = j.classes || []; })
      .catch(function (e) { classErr = e.message; })
      .then(function () { classLoading = false; if (pop) renderPicker(); lines.rows.forEach(refreshClassBtn); });
  }
  function setSelected(ids) {
    var inp = popRow.el.querySelector('[data-field="class_filter"]');
    inp.value = classKey(ids);
    inp.dispatchEvent(new Event('change', { bubbles: true }));     // the grid's dirty tracking listens on the row
    refreshClassBtn(popRow);
  }
  function renderPicker() {
    if (!pop || !popRow) return;
    var list = pop.querySelector('.rt-class-list');
    var q = (pop.querySelector('.rt-class-search').value || '').toLowerCase();
    if (classLoading) { list.innerHTML = '<p class="sub">Loading classes from QuickBooks&hellip;</p>'; return; }
    if (classErr) {
      list.innerHTML = '<p class="rt-bad">' + esc(classErr) + '</p><button type="button" class="btn btn-secondary btn-sm" data-act="retry">Retry</button>';
      return;
    }
    var sel = rowClassIds(popRow), known = {};
    (classData || []).forEach(function (c) { known[c.id] = true; });
    var items = [{ id: '', name: '(No class)', active: true }].concat(classData || []);
    sel.forEach(function (id) { if (id !== '' && !known[id]) items.push({ id: id, name: classNameOf(popRow, id), missing: true }); });
    var out = [];
    items.forEach(function (c) {
      if (q && c.name.toLowerCase().indexOf(q) < 0) return;
      out.push('<label class="rt-class-opt"><input type="checkbox" data-id="' + esc(c.id) + '"' + (sel.indexOf(c.id) >= 0 ? ' checked' : '') + '> ' + esc(c.name) +
        (c.missing ? ' <em>(not in QuickBooks)</em>' : (c.active === false ? ' <em>(inactive)</em>' : '')) + '</label>');
    });
    list.innerHTML = out.length ? out.join('') : '<p class="sub">No classes match.</p>';
  }
  function openPicker(row, btn) {
    if (popRow === row) { closePicker(); return; }
    closePicker();
    popRow = row;
    pop = document.createElement('div');
    pop.className = 'rt-class-pop';
    pop.innerHTML = '<input type="search" class="rt-class-search" placeholder="Search classes" autocomplete="off">' +
      '<div class="rt-class-list"></div>' +
      '<div class="rt-class-foot"><button type="button" class="linkish" data-act="clear">Clear (all classes)</button>' +
      '<span class="sub">A parent class does not include its sub-classes.</span></div>';
    document.body.appendChild(pop);
    var r = btn.getBoundingClientRect(), h = Math.min(pop.offsetHeight || 300, 360);
    pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + 'px';
    pop.style.top = ((r.bottom + 4 + h > window.innerHeight && r.top - h - 4 > 0) ? r.top - h - 4 : r.bottom + 4) + 'px';
    pop.querySelector('.rt-class-search').addEventListener('input', renderPicker);
    pop.addEventListener('change', function (e) {
      var cb = e.target;
      if (!cb.matches || !cb.matches('input[type="checkbox"]')) return;
      var id = cb.getAttribute('data-id');
      var ids = rowClassIds(popRow).filter(function (x) { return x !== id; });
      if (cb.checked) ids.push(id);
      setSelected(ids);
    });
    pop.addEventListener('click', function (e) {
      var act = e.target.getAttribute && e.target.getAttribute('data-act');
      if (act === 'clear') { setSelected([]); renderPicker(); }
      if (act === 'retry') { loadClasses(true); renderPicker(); }
    });
    if (classData === null && !classLoading) loadClasses(false);   // sets classLoading at once
    renderPicker();
    pop.querySelector('.rt-class-search').focus();
  }
  document.addEventListener('mousedown', function (e) {
    if (pop && !(pop.contains(e.target) || (e.target.closest && e.target.closest('.rt-class-btn')))) closePicker();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closePicker(); });
  window.addEventListener('resize', closePicker);
  window.addEventListener('scroll', function (e) { if (pop && !pop.contains(e.target)) closePicker(); }, true);

  function lowPct(inp) {
    var n = Number(inp.value), low = n > 0 && n < 1;
    inp.classList.toggle('rt-pct-low', low);
    inp.title = low ? n + '% is under 1%. Did you mean ' + (n * 100) + '%?'
                    : 'The share of this line actual and budget charged to the report (100 = all of it)';
  }

  var LINE_FIELDS = ['is_active', FUND ? 'fund_group' : 'section', 'line_label', 'account_mask']
    .concat(OPTS ? ['class_filter', 'charge_pct', 'display_mode'] : []).concat(['sort_order']);   // = column order
  var LINE_COLS = document.querySelectorAll('#linesTable thead th').length;
  var lines = new Grid({
    body: document.getElementById('linesBody'),
    saveBtn: document.getElementById('lineSaveBtn'),
    stateEl: document.getElementById('lineSaveState'),
    fields: LINE_FIELDS,
    url: '/admin/report-templates/' + TID + '/lines/save',
    resultKey: 'lines', noun: 'line',
    onRowGone: function (row) { if (popRow === row) closePicker(); },
    payload: function (o) {                  // send class ids only; the server reads the names from QuickBooks
      if (OPTS) o.class_filter = classIds(o.class_filter);
      return o;
    },
    validateRow: function (row) {
      if (!OPTS) return null;
      var inp = row.el.querySelector('[data-field="charge_pct"]');
      if (inp.validity && inp.validity.badInput) return 'Enter % charged as a plain number (for example 40), not 40%.';
      var t = inp.value.trim();
      if (!t) return '% charged is required (enter 100 for the whole line).';
      var n = Number(t);
      if (!isFinite(n) || n <= 0 || n > 100) return '% charged must be above 0 and at most 100.';
      return null;
    },
    rowHtml: function (r) {
      var opts = '';
      if (OPTS) {
        var pct = (r.charge_pct == null || r.charge_pct === '') ? '100' : r.charge_pct;
        var mode = r.display_mode === 'sum' ? 'sum' : 'detail';
        opts =
          '<td class="rt-col-class"><input type="hidden" data-field="class_filter" value="' + esc(classKey(r.class_filter)) + '">' +
            '<button type="button" class="rt-class-btn" title="Any class">All classes</button></td>' +
          '<td class="rt-col-pct"><input type="number" class="num" data-field="charge_pct" min="0" max="100" step="any" value="' + esc(pct) + '"></td>' +
          '<td class="rt-col-show' + (DATA.showAccounts === false ? ' rt-muted' : '') + '"' +
            (DATA.showAccounts === false ? ' title="Every line shows its total only, because Show each account under its line is off in Options."' : '') + '>' +
            '<select data-field="display_mode"><option value="detail"' + (mode === 'detail' ? ' selected' : '') + '>Detail</option>' +
            '<option value="sum"' + (mode === 'sum' ? ' selected' : '') + '>Sum</option></select></td>';
      }
      return '<td class="col-allow"><input type="checkbox" data-field="is_active"' + (r.is_active === false ? '' : ' checked') + '></td>' +
        '<td class="rt-col-section">' + groupCell(r) + '</td>' +
        '<td class="col-display"><input type="text" data-field="line_label" value="' + esc(r.line_label) + '" placeholder="' + (FUND ? 'e.g. Temporarily Restricted Endowments' : 'e.g. Diocesan House') + '"></td>' +
        '<td class="rt-col-mask"><input type="text" data-field="account_mask" value="' + esc(r.account_mask) + '" placeholder="' + (FUND ? 'e.g. 3050.4*' : 'e.g. 667*, 6680-6689') + '"></td>' +
        opts +
        '<td class="col-sort"><input type="number" class="num" data-field="sort_order" value="' + esc(r.sort_order == null ? 100 : r.sort_order) + '"></td>' +
        '<td class="rt-col-matches"><button type="button" class="linkish rt-match-btn" title="Show the accounts this line picks up">Preview</button></td>';
    },
    afterAdd: function (row) {
      row.el.querySelector('.rt-match-btn').addEventListener('click', function () { toggleDetail(row); });
      if (OPTS) {
        row.classNames = {};
        (Array.isArray(row.rec.class_filter) ? row.rec.class_filter : []).forEach(function (e) {
          if (e && typeof e === 'object') row.classNames[String(e.id)] = e.name;
        });
        refreshClassBtn(row);
        row.el.querySelector('.rt-class-btn').addEventListener('click', function () { openPicker(row, this); });
        var p = row.el.querySelector('[data-field="charge_pct"]');
        p.addEventListener('input', function () { lowPct(p); });
        lowPct(p);
      }
    },
    afterSave: function () { closePicker(); lastPreview = null; document.getElementById('previewSummary').innerHTML = ''; }
  });
  lines.load(DATA.lines || []);

  document.getElementById('lineAddBtn').addEventListener('click', function () {
    var max = lines.rows.reduce(function (m, r) { return Math.max(m, parseInt(r.cur.sort_order, 10) || 0); }, 0);
    var row = lines.add({ section: 'Expense', fund_group: '', line_label: '', account_mask: '', sort_order: max + 10, is_active: true,
                          charge_pct: '100', class_filter: [], display_mode: 'detail' }, true);
    row.el.querySelector('[data-field="line_label"]').focus();
  });
  document.getElementById('lineSaveBtn').addEventListener('click', function () { lines.save(); });

  // ── Preview ───────────────────────────────────────────────────────────────
  var lastPreview = null;
  function liveLines() { return lines.rows.filter(function (r) { return !r.removed; }); }   // not marked for removal
  function previewRows() {
    return liveLines().map(function (r) {
      return { key: r.key, id: r.id, section: FUND ? 'Equity' : r.cur.section, line_label: r.cur.line_label || '(unnamed line)',
               account_mask: r.cur.account_mask, sort_order: r.cur.sort_order, is_active: r.cur.is_active,
               class_filter: OPTS ? r.cur.class_filter : '[]' };
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
    liveLines().forEach(function (r) {
      var btn = r.el.querySelector('.rt-match-btn');
      var res = j.lines[r.key];
      if (j.invalid && j.invalid[r.key]) { btn.textContent = 'Invalid mask'; btn.className = 'linkish rt-match-btn rt-bad'; btn.title = j.invalid[r.key]; return; }
      if (!res) { btn.textContent = r.cur.is_active ? 'Preview' : 'Inactive'; btn.className = 'linkish rt-match-btn'; return; }
      var byClass = OPTS && classIds(r.cur.class_filter).length;
      btn.textContent = res.count + ' account' + (res.count === 1 ? '' : 's') + (byClass ? ', by class' : '') +
        (res.section_mismatch ? ' (' + res.section_mismatch + ' off-section)' : '');
      btn.className = 'linkish rt-match-btn' + (res.count === 0 || res.section_mismatch ? ' rt-bad' : '');
      btn.title = byClass ? 'The accounts this line picks up. Only transactions in the selected classes count, so an account here may still show nothing.'
                          : 'Show the accounts this line picks up';
    });
    var parts = [];
    parts.push('<p class="setup-hint">Checked against ' + esc(j.account_count) +
      (FUND ? ' active QuickBooks equity accounts (the only accounts a Fund Summary reads).</p>'
            : ' QuickBooks accounts (active and inactive, as the report does).</p>'));
    var shadowed = j.shadowed_lines || [];
    var empty = liveLines().filter(function (r) {
      return j.lines[r.key] && j.lines[r.key].count === 0 && shadowed.indexOf(r.cur.line_label || '(unnamed line)') < 0;
    });
    if (empty.length) parts.push('<div class="banner banner-error">' + empty.length + ' active line' + (empty.length === 1 ? ' matches' : 's match') + ' no accounts at all.</div>');
    if (shadowed.length) {
      parts.push('<div class="banner banner-error"><strong>' + shadowed.length + ' line' + (shadowed.length === 1 ? ' can' : 's can') +
        ' never receive anything:</strong> an earlier line (by Sort) already takes every transaction on ' + (shadowed.length === 1 ? 'its' : 'their') +
        ' accounts &mdash; ' + shadowed.map(esc).join(', ') + '. Move the line with a class selection above the one without.</div>');
    }
    if (j.partial_overlaps && j.partial_overlaps.length) {
      parts.push('<div class="banner banner-info"><strong>' + j.partial_overlaps.length + ' account' + (j.partial_overlaps.length === 1 ? ' has' : 's have') +
        ' class selections that partly overlap</strong> &mdash; a transaction in both goes to the first line (by Sort):<ul class="popup-list">' +
        j.partial_overlaps.slice(0, 15).map(function (o) { return '<li>' + esc(o.acct_num) + ' ' + esc(o.name) + ' &rarr; ' + o.lines.map(esc).join(', ') + '</li>'; }).join('') + '</ul></div>');
    }
    if (OPTS && DATA.fullEntity && liveLines().some(function (r) {
          return r.cur.is_active && (Number(r.cur.charge_pct) < 100 || classIds(r.cur.class_filter).length); })) {
      parts.push('<div class="banner banner-info">This is a whole-entity template: with a % under 100 or a class selection, the net is shown <em>as charged</em> and will not equal QuickBooks\' net income. The report still checks the unscaled ledger against it.</div>');
    }
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
    tr.innerHTML = '<td colspan="' + LINE_COLS + '"><span class="sub">Loading accounts from QuickBooks&hellip;</span></td>';
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
      existing_masks: liveLines().filter(function (r) { return r.cur.is_active && r.cur.account_mask; })
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
                      sort_order: base + added * 10, is_active: true,
                      charge_pct: '100', class_filter: [], display_mode: 'detail' }, true);
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
      liveLines().forEach(function (r) { if (r.cur.is_active && r.cur.account_mask) have[r.cur.account_mask.trim()] = true; });
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

  // ── In-place saves: Options and Schedule ──────────────────────────────────
  // Each saves only itself, with fetch(), and the page never reloads -- so unsaved Lines and Recipients
  // survive it. The result shows right beside the button (the Schedule section sits at the very bottom,
  // far below the banner at the top, so a banner alone would go unseen).
  var optForm = document.querySelector('form.rt-options[data-ajax]');
  var schedForm = document.querySelector('form.rt-schedule[data-ajax]');

  function snap(form) { return form ? new URLSearchParams(new FormData(form)).toString() : ''; }
  var optBase = snap(optForm), schedBase = snap(schedForm);          // what each form held when last saved
  function optDirty() { return !!optForm && snap(optForm) !== optBase; }
  function schedDirty() { return !!schedForm && snap(schedForm) !== schedBase; }
  function gridChanges() { return lines.dirtyRows().length + recips.dirtyRows().length; }
  function unsavedCount() { return gridChanges() + (optDirty() ? 1 : 0) + (schedDirty() ? 1 : 0); }

  function formMsg(form, kind, text) {
    var el = form.querySelector('[data-form-msg]');
    if (!el) return;
    el.className = 'save-state' + (kind ? ' ' + kind : '');
    el.textContent = text || '';
  }
  function postForm(form) {
    var h = window.csrfHeader ? window.csrfHeader() : {};
    h['Accept'] = 'application/json';
    h['Content-Type'] = 'application/x-www-form-urlencoded';
    return fetch(form.getAttribute('action'), { method: 'POST', headers: h, credentials: 'same-origin',
                                                body: snap(form) })
      .then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (!j) throw new Error("Couldn't save: Beacon sent back something unexpected, usually because your sign-in " +
                                  'expired. Nothing on this page was changed or lost. Sign in again in another tab, then save again.');
          if (!r.ok || j.ok !== true) throw new Error(j.error || ('Request failed (HTTP ' + r.status + ').'));
          return j;
        });
      });
  }
  function wireInPlaceSave(form, before, onSaved, rebase) {
    if (!form) return;
    var btn = form.querySelector('button[type="submit"]');
    form.addEventListener('submit', function (e) {
      e.preventDefault();                      // never a page navigation
      var stop = before ? before() : null;
      if (stop) { formMsg(form, 'err', stop); return; }
      formMsg(form, '', 'Saving…');
      pulse(btn);
      postForm(form).then(function (j) {
        onSaved(j);
        rebase();
      }).catch(function (err) {
        formMsg(form, 'err', err.message);
      }).then(function () { unpulse(btn); });
    });
    // Any further edit makes an old "Saved." out of date.
    ['input', 'change'].forEach(function (t) { form.addEventListener(t, function () { formMsg(form, '', ''); }); });
  }

  var typeLoaded = DATA.reportType;
  wireInPlaceSave(optForm,
    function () {
      // A different report type rebuilds the Lines columns, which means a reload -- never over unsaved work.
      if (typeSel && typeSel.value !== typeLoaded && gridChanges()) {
        return 'Changing the report type rebuilds the Lines columns. Save (or undo) your unsaved Lines and Recipients first, then change it.';
      }
      return null;
    },
    function (j) {
      var upd = optForm.querySelector('[name="updated_at"]');          // the stale-edit stamp for the NEXT save
      if (upd && j.updated_at) upd.value = j.updated_at;
      if (j.name) {
        var h1 = document.querySelector('.rt-header h1');
        if (h1 && h1.firstChild && h1.firstChild.nodeType === 3) h1.firstChild.nodeValue = j.name + ' ';
        document.title = j.name + ' — Beacon';
      }
      if (j.report_type && j.report_type !== typeLoaded) {
        if (gridChanges()) {                                           // edited while the save was in flight
          formMsg(optForm, 'ok', 'Saved. The report type changed: save your Lines and Recipients, then reload this page so the Lines columns match.');
          return;
        }
        window.location.replace(window.location.pathname + '?saved=options');
        return;
      }
      var wasFull = !!DATA.fullEntity;
      DATA.fullEntity = !!j.full_entity;                               // what the Lines section reads
      DATA.showAccounts = !!j.show_accounts_under_lines;
      if (wasFull !== DATA.fullEntity) { lastPreview = null; document.getElementById('previewSummary').innerHTML = ''; }
      if (OPTS) lines.rows.forEach(function (r) {
        var td = r.el.querySelector('.rt-col-show');
        if (!td) return;
        td.classList.toggle('rt-muted', !DATA.showAccounts);
        if (DATA.showAccounts) td.removeAttribute('title');
        else td.title = 'Every line shows its total only, because Show each account under its line is off in Options.';
      });
      formMsg(optForm, 'ok', 'Options saved.');
    },
    function () { optBase = snap(optForm); });

  wireInPlaceSave(schedForm, null,
    function () {
      var note = schedForm.querySelector('[data-not-saved]');
      if (note) note.remove();
      formMsg(schedForm, 'ok', 'Schedule saved.');
    },
    function () { schedBase = snap(schedForm); });

  // ── Unsaved-changes guards ────────────────────────────────────────────────
  // Clone and Activate/Deactivate (data-leaves-page) really do leave or reload the page. The browser's own
  // "Leave site?" prompt is not dependable everywhere (some embedded browsers never show it), so ask here too.
  var leaving = false;
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f || !f.hasAttribute || !f.hasAttribute('data-leaves-page')) return;
    var n = unsavedCount();
    if (n && !window.confirm('You have ' + n + ' unsaved change' + (n === 1 ? '' : 's') + ' on this page (Lines, Recipients, Options or Schedule). ' +
                             'Going on will lose ' + (n === 1 ? 'it' : 'them') + '. Continue anyway?')) {
      e.preventDefault();
      e.stopPropagation();                     // and skip that button's own confirmation
    }
  }, true);
  document.addEventListener('submit', function (e) {               // after confirm_submit.js has had its say
    var f = e.target;
    if (f && f.hasAttribute && f.hasAttribute('data-leaves-page')) leaving = !e.defaultPrevented;
  });
  window.addEventListener('beforeunload', function (e) {
    if (!leaving && unsavedCount()) { e.preventDefault(); e.returnValue = ''; }     // already asked above if we are leaving on purpose
  });
})();
