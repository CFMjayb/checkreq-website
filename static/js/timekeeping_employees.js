/* Employees (HR) screen. No inline handlers (CSP).
 *
 *  - fits the frame to the window, so only the rows scroll;
 *  - marks a row changed the moment anything in it changes;
 *  - Save posts ONLY the changed rows (untouched rows are disabled just before
 *    submit): Starlette refuses a form with 1,000 or more fields, and one set per
 *    row across the roster is well over that;
 *  - the three-dot menu marks a row inactive or active (nothing is written until
 *    Save Changes);
 *  - click a column heading to sort.
 */
(function () {
  'use strict';

  var shell = document.getElementById('empShell');
  var form = document.getElementById('empForm');
  if (!shell || !form) return;

  var tbody = form.querySelector('tbody');
  var scroller = form.querySelector('.emp-scroll');
  var saveBtn = document.getElementById('empSave');
  var changedEl = document.getElementById('empChanged');
  var menu = document.getElementById('empMenu');
  var menuToggle = document.getElementById('empMenuToggle');
  var menuRow = null;
  var leaving = false;           // set once we are deliberately navigating away

  function rows() { return Array.prototype.slice.call(tbody.querySelectorAll('tr.emp-row')); }

  // ---- fit the frame to the window -------------------------------------------
  function fit() {
    var top = shell.getBoundingClientRect().top + window.pageYOffset;
    var below = parseFloat(window.getComputedStyle(shell.parentNode).paddingBottom) || 0;
    var footer = document.querySelector('body > footer, footer');
    if (footer) {
      var cs = window.getComputedStyle(footer);
      below += footer.offsetHeight + (parseFloat(cs.marginTop) || 0);
    }
    shell.style.height = Math.max(window.innerHeight - top - below, 320) + 'px';
  }
  window.addEventListener('resize', fit);
  fit();
  // the sticky header / banners can change the offset a little after first paint
  window.addEventListener('load', fit);

  // ---- row state --------------------------------------------------------------
  function actionInput(tr) { return tr.querySelector('input.emp-action'); }
  function originalActive(tr) { return tr.dataset.active === '1'; }
  function effectiveActive(tr) {
    var a = actionInput(tr).value;
    if (a === 'deactivate') return false;
    if (a === 'reactivate') return true;
    return originalActive(tr);
  }
  function fieldsDirty(tr) {
    var dirty = false;
    tr.querySelectorAll('input.emp-in').forEach(function (i) { if (i.value !== i.defaultValue) dirty = true; });
    tr.querySelectorAll('input[type=checkbox]').forEach(function (i) { if (i.checked !== i.defaultChecked) dirty = true; });
    return dirty;
  }
  function isDirty(tr) { return fieldsDirty(tr) || actionInput(tr).value !== ''; }

  function paint(tr) {
    var active = effectiveActive(tr);
    tr.classList.toggle('is-active', active);
    tr.classList.toggle('is-inactive', !active);
    tr.classList.toggle('row-dirty', isDirty(tr));
    // an inactive row cannot be edited until it is activated
    tr.querySelectorAll('input.emp-in').forEach(function (i) {
      i.readOnly = !active;
      i.tabIndex = active ? 0 : -1;
    });
    var cb = tr.querySelector('input[type=checkbox]');
    if (cb) cb.tabIndex = active ? 0 : -1;
  }

  function dirtyRows() { return rows().filter(isDirty); }

  function refresh() {
    var n = dirtyRows().length;
    saveBtn.disabled = n === 0;
    changedEl.hidden = n === 0;
    changedEl.textContent = n + (n === 1 ? ' change' : ' changes');
  }

  rows().forEach(paint);
  refresh();

  // keep a locked row from being toggled by keyboard / click on the checkbox
  tbody.addEventListener('click', function (e) {
    var cb = e.target.closest && e.target.closest('input[type=checkbox]');
    if (cb) {
      var tr = cb.closest('tr.emp-row');
      if (tr && !effectiveActive(tr)) { e.preventDefault(); }
    }
  });
  tbody.addEventListener('input', function (e) {
    var tr = e.target.closest('tr.emp-row');
    if (tr) { paint(tr); refresh(); }
  });
  tbody.addEventListener('change', function (e) {
    var tr = e.target.closest('tr.emp-row');
    if (tr) { paint(tr); refresh(); }
  });

  // Enter in a text box must not submit the whole form
  form.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); }
  });

  // ---- the three-dot menu -----------------------------------------------------
  function closeMenu() {
    if (menuRow) {
      var b = menuRow.querySelector('.emp-menu-btn');
      if (b) b.setAttribute('aria-expanded', 'false');
    }
    menu.hidden = true;
    menuRow = null;
  }
  tbody.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('.emp-menu-btn');
    if (!btn) return;
    var tr = btn.closest('tr.emp-row');
    if (menuRow === tr && !menu.hidden) { closeMenu(); return; }
    closeMenu();
    menuRow = tr;
    btn.setAttribute('aria-expanded', 'true');
    menuToggle.textContent = effectiveActive(tr) ? 'Inactivate' : 'Activate';
    menu.hidden = false;
    var r = btn.getBoundingClientRect();
    var w = menu.offsetWidth || 150;
    var left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    var top = r.bottom + 2;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = r.top - menu.offsetHeight - 2;
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
  });
  menuToggle.addEventListener('click', function () {
    var tr = menuRow;
    if (!tr) return;
    var newActive = !effectiveActive(tr);
    var act = actionInput(tr);
    // back to the stored state = no pending change; otherwise record the change
    act.value = (newActive === originalActive(tr)) ? '' : (newActive ? 'reactivate' : 'deactivate');
    paint(tr);
    refresh();
    closeMenu();
  });
  document.addEventListener('click', function (e) {
    if (!menu.hidden && !menu.contains(e.target) && !(e.target.closest && e.target.closest('.emp-menu-btn'))) closeMenu();
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeMenu(); });
  scroller.addEventListener('scroll', closeMenu);
  window.addEventListener('resize', closeMenu);

  // ---- sorting ----------------------------------------------------------------
  var collator = (window.Intl && Intl.Collator) ? new Intl.Collator(undefined, { sensitivity: 'base', numeric: true }) : null;
  function cmp(a, b) {
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    a = String(a); b = String(b);
    return collator ? collator.compare(a, b) : (a < b ? -1 : (a > b ? 1 : 0));
  }
  function val(tr, key) {
    var inputs = tr.querySelectorAll('input.emp-in');   // last, first, emp #
    switch (key) {
      case 'parish': return tr.querySelector('.c-parish').dataset.v || '';
      case 'last': return inputs[0].value.trim();
      case 'first': return inputs[1].value.trim();
      case 'emp': var s = inputs[2].value.trim(); return /^\d+$/.test(s) ? parseInt(s, 10) : s;
      case 'hours': return tr.querySelector('input[type=checkbox]').checked ? 1 : 0;
      case 'eff': return tr.querySelector('.c-date').dataset.v || '';
    }
    return '';
  }
  var secondary = { last: ['first'], first: ['last'], parish: ['last', 'first'], emp: ['last', 'first'], hours: ['last', 'first'], eff: ['last', 'first'] };
  function sortBy(key, dir) {
    var list = rows();
    list.sort(function (a, b) {
      var c = cmp(val(a, key), val(b, key));
      if (c === 0) {
        (secondary[key] || []).some(function (k) { c = cmp(val(a, k), val(b, k)); return c !== 0; });
      }
      if (c === 0) c = parseInt(a.dataset.id, 10) - parseInt(b.dataset.id, 10);
      return dir === 'desc' ? -c : c;
    });
    list.forEach(function (tr) { tbody.appendChild(tr); });
    form.querySelectorAll('thead th[data-sort]').forEach(function (th) {
      var on = th.dataset.sort === key;
      th.dataset.dir = on ? dir : '';
      th.querySelector('.arrow').innerHTML = on ? (dir === 'asc' ? '&#9650;' : '&#9660;') : '';
    });
    closeMenu();
  }
  form.querySelectorAll('thead th[data-sort]').forEach(function (th) {
    th.addEventListener('click', function () {
      var key = th.dataset.sort;
      sortBy(key, th.dataset.dir === 'asc' ? 'desc' : 'asc');
    });
  });

  // ---- Save: post only the changed rows ---------------------------------------
  form.addEventListener('submit', function (e) {
    var changed = dirtyRows();
    if (!changed.length) { e.preventDefault(); return; }
    var keep = new Set(changed);
    rows().forEach(function (tr) {
      if (!keep.has(tr)) tr.querySelectorAll('input').forEach(function (i) { i.disabled = true; });
    });
    leaving = true;
    if (window.showButtonLoading) window.showButtonLoading(e.submitter || saveBtn);
  });

  // ---- do not lose unsaved edits ---------------------------------------------
  window.addEventListener('beforeunload', function (e) {
    if (!leaving && dirtyRows().length) { e.preventDefault(); e.returnValue = ''; }
  });
  document.querySelectorAll('#empFilters select[data-autosubmit]').forEach(function (sel) {
    var before = sel.value;
    sel.addEventListener('change', function () {
      var n = dirtyRows().length;
      if (n && !window.confirm('You have ' + n + ' unsaved ' + (n === 1 ? 'change' : 'changes') + '. Discard ' + (n === 1 ? 'it' : 'them') + '?')) {
        sel.value = before;
        return;
      }
      leaving = true;
      sel.form.submit();
    });
  });
  var filters = document.getElementById('empFilters');
  if (filters) {
    filters.addEventListener('submit', function (e) {
      var n = dirtyRows().length;
      if (n && !window.confirm('You have ' + n + ' unsaved ' + (n === 1 ? 'change' : 'changes') + '. Discard ' + (n === 1 ? 'it' : 'them') + '?')) {
        e.preventDefault();
        return;
      }
      leaving = true;
    });
  }

  // a page restored from the back/forward cache could still have disabled rows
  window.addEventListener('pageshow', function (e) { if (e.persisted) window.location.reload(); });

  // ---- Add employee pop-up ----------------------------------------------------
  var dlg = document.getElementById('empAddDialog');
  var addBtn = document.getElementById('empAddBtn');
  var addCancel = document.getElementById('empAddCancel');
  if (dlg && addBtn) {
    addBtn.addEventListener('click', function () {
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
      var first = dlg.querySelector('select, input[type=text]');
      if (first) first.focus();
    });
    if (addCancel) addCancel.addEventListener('click', function () { dlg.close(); });
    var addForm = document.getElementById('empAddForm');
    if (addForm) addForm.addEventListener('submit', function (e) {
      leaving = true;
      if (window.showButtonLoading) window.showButtonLoading(e.submitter);
    });
  }
})();
