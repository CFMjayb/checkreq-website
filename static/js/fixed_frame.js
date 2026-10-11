/* The fixed frame (see static/css/fixed_frame.css). No inline handlers (CSP).
 *
 *  - fits the frame to the window, so only the rows scroll;
 *  - sorts on a click of any column heading that has data-sort (the cell's data-v, else its text);
 *  - a multi-check filter [data-ff-multi="<attr>"] (a checkbox per kind of row; remembered on this PC);
 *  - a search box [data-ff-search] and any number of selects [data-ff-filter="<attr>"] hide rows
 *    (a row's tr.dataset.<attr> must equal the select's value; "" shows all);
 *  - [data-ff-count] shows how many rows are showing;
 *  - a gold Actions drop-down (button[data-ff-dropdown] + .ff-dropdown-menu) holds a screen's actions;
 *  - a button with data-ff-dialog="<id>" opens a <dialog>; a three-dot row menu lists the links in the row's .ff-menu-src.
 */
(function () {
  'use strict';

  var shell = document.querySelector('.ff-shell');
  if (!shell) return;
  var table = shell.querySelector('table.ff-table');
  var tbody = table ? table.querySelector('tbody') : null;

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
  window.addEventListener('load', fit);   // the sticky header / banners can shift the offset after first paint

  // ---- an Actions drop-down: a button with data-ff-dropdown toggles the .ff-dropdown-menu right after it ----
  function closeDropdowns() {
    document.querySelectorAll('.ff-dropdown-menu').forEach(function (m) { m.hidden = true; });
    document.querySelectorAll('[data-ff-dropdown]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
  }
  document.querySelectorAll('[data-ff-dropdown]').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var menu = btn.nextElementSibling;
      var wasOpen = menu && !menu.hidden;
      closeDropdowns();
      if (menu && !wasOpen) { menu.hidden = false; btn.setAttribute('aria-expanded', 'true'); }
    });
  });
  document.addEventListener('click', function () { setTimeout(closeDropdowns, 0); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') closeDropdowns(); });

  // ---- pop-ups: a button with data-ff-dialog="<id>" opens that <dialog>; data-ff-close closes it ----
  document.querySelectorAll('[data-ff-dialog]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var d = document.getElementById(btn.dataset.ffDialog);
      if (!d) return;
      if (d.showModal) d.showModal(); else d.setAttribute('open', '');
      var first = d.querySelector('select, input[type=text]');
      if (first) first.focus();
    });
  });
  document.querySelectorAll('[data-ff-close]').forEach(function (b) {
    b.addEventListener('click', function () { var d = b.closest('dialog'); if (d) d.close(); });
  });

  if (!table || !tbody) return;
  function rows() { return Array.prototype.slice.call(tbody.querySelectorAll('tr.ff-row')); }
  rows().forEach(function (tr, i) { tr.dataset.i = i; });

  // ---- the three-dot row menu ---------------------------------------------------
  var menu = null, menuBtn = null;
  function closeMenu() {
    if (menu) { menu.remove(); menu = null; }
    if (menuBtn) { menuBtn.setAttribute('aria-expanded', 'false'); menuBtn = null; }
  }
  tbody.addEventListener('click', function (e) {
    var btn = e.target.closest && e.target.closest('.ff-menu-btn');
    if (!btn) return;
    if (menuBtn === btn) { closeMenu(); return; }
    closeMenu();
    var src = btn.parentNode.querySelector('.ff-menu-src');
    if (!src) return;
    menu = document.createElement('div');
    menu.className = 'ff-menu';
    menu.setAttribute('role', 'menu');
    Array.prototype.slice.call(src.children).forEach(function (c) { menu.appendChild(c.cloneNode(true)); });
    document.body.appendChild(menu);
    menuBtn = btn;
    btn.setAttribute('aria-expanded', 'true');
    var r = btn.getBoundingClientRect();
    var w = menu.offsetWidth || 170;
    var left = Math.min(Math.max(8, r.right - w), window.innerWidth - w - 8);
    var top = r.bottom + 2;
    if (top + menu.offsetHeight > window.innerHeight - 8) top = r.top - menu.offsetHeight - 2;
    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
  });
  document.addEventListener('click', function (e) {
    if (menu && !menu.contains(e.target) && !(e.target.closest && e.target.closest('.ff-menu-btn'))) closeMenu();
  });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') closeMenu(); });
  var scroller = shell.querySelector('.ff-scroll');
  if (scroller) scroller.addEventListener('scroll', closeMenu);
  window.addEventListener('resize', closeMenu);

  // ---- sorting ----------------------------------------------------------------
  var collator = (window.Intl && Intl.Collator) ? new Intl.Collator(undefined, { sensitivity: 'base', numeric: true }) : null;
  function cmp(a, b) {
    if (typeof a === 'number' && typeof b === 'number') return a - b;
    a = String(a); b = String(b);
    return collator ? collator.compare(a, b) : (a < b ? -1 : (a > b ? 1 : 0));
  }
  function cellValue(tr, idx) {
    var td = tr.cells[idx];
    if (!td) return '';
    var v = td.dataset.v !== undefined ? td.dataset.v : td.textContent.trim();
    return /^-?\d+(\.\d+)?$/.test(v) ? parseFloat(v) : v;
  }
  table.querySelectorAll('thead th[data-sort]').forEach(function (th) {
    th.addEventListener('click', function () {
      var dir = th.dataset.dir === 'asc' ? 'desc' : 'asc';
      var idx = th.cellIndex;
      var list = rows();
      list.sort(function (a, b) {
        var c = cmp(cellValue(a, idx), cellValue(b, idx));
        if (c === 0) c = parseInt(a.dataset.i, 10) - parseInt(b.dataset.i, 10);
        return dir === 'desc' ? -c : c;
      });
      list.forEach(function (tr) { tbody.appendChild(tr); });
      table.querySelectorAll('thead th[data-sort]').forEach(function (h) {
        h.dataset.dir = h === th ? dir : '';
        var arrow = h.querySelector('.arrow');
        if (arrow) arrow.innerHTML = h === th ? (dir === 'asc' ? '&#9650;' : '&#9660;') : '';
      });
    });
  });

  // ---- search and filters -----------------------------------------------------
  var search = shell.querySelector('[data-ff-search]');
  var filters = Array.prototype.slice.call(shell.querySelectorAll('[data-ff-filter]'));
  var counter = shell.querySelector('[data-ff-count]');
  // a multi-check filter: [data-ff-multi="<attr>"] holds a button, a panel and one checkbox per kind of row;
  // a row shows when the checkbox whose value equals its data-<attr> is ticked. The choice is remembered.
  var multis = Array.prototype.slice.call(shell.querySelectorAll('[data-ff-multi]')).map(function (box) {
    var attr = box.dataset.ffMulti;
    var key = 'ff-multi:' + location.pathname.replace(/\d+/g, 'N') + ':' + attr;
    var checks = Array.prototype.slice.call(box.querySelectorAll('input[type=checkbox]'));
    var btn = box.querySelector('.ff-multi-btn');
    var panel = box.querySelector('.ff-multi-panel');
    var label = box.querySelector('[data-ff-multi-label]');
    try {
      var saved = JSON.parse(localStorage.getItem(key) || 'null');
      if (Array.isArray(saved)) checks.forEach(function (cb) { cb.checked = saved.indexOf(cb.value) === -1; });
    } catch (e) { /* storage may be unavailable: the filter simply starts with everything ticked */ }
    function text(cb) { return cb.parentNode.textContent.replace(/\(.*\)/, '').trim(); }
    function paintLabel() {
      var off = checks.filter(function (cb) { return !cb.checked; });
      label.textContent = off.length === 0 ? 'All' : (off.length === 1 ? 'All except ' + text(off[0]) : (checks.length - off.length) + ' of ' + checks.length);
    }
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = !panel.hidden;
      panel.hidden = open;
      btn.setAttribute('aria-expanded', open ? 'false' : 'true');
    });
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', function () { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { panel.hidden = true; btn.setAttribute('aria-expanded', 'false'); } });
    checks.forEach(function (cb) {
      cb.addEventListener('change', function () {
        paintLabel();
        try { localStorage.setItem(key, JSON.stringify(checks.filter(function (x) { return !x.checked; }).map(function (x) { return x.value; }))); } catch (e) { /* not remembered */ }
        apply();
      });
    });
    paintLabel();
    return { attr: attr, checks: checks };
  });
  function apply() {
    var q = search ? search.value.trim().toLowerCase() : '';
    var shown = 0, all = rows();
    all.forEach(function (tr) {
      var ok = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
      filters.forEach(function (sel) {
        if (ok && sel.value && tr.dataset[sel.dataset.ffFilter] !== sel.value) ok = false;
      });
      multis.forEach(function (m) {
        if (!ok) return;
        var kind = tr.dataset[m.attr];
        m.checks.forEach(function (cb) { if (cb.value === kind && !cb.checked) ok = false; });
      });
      tr.hidden = !ok;
      if (ok) shown++;
    });
    if (counter) counter.textContent = shown + ' shown' + (shown === all.length ? '' : ' of ' + all.length);
  }
  if (search) search.addEventListener('input', apply);
  filters.forEach(function (sel) { sel.addEventListener('change', apply); });
  apply();
})();
