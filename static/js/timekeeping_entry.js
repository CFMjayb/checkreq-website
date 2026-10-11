/* Timekeeping entry screen (26-158 rework, 2026-10-11). No inline handlers (CSP).
 *
 *  - every employee starts COLLAPSED to one line with a period-total box per category;
 *    the arrow opens the days (remembered for this browser tab across a save);
 *  - boxes are text boxes that only take xx.xx (days up to 24, totals up to 300) so every digit shows;
 *  - ONE rule between a period total and daily hours for the same person and category: while a total is
 *    typed the days are locked, while any day is typed the total is locked (it shows the days' sum).
 *    Clear one to use the other. Nothing is ever discarded silently;
 *  - live totals: per category, per day, per employee and a grand total in the title row and the bottom bar;
 *  - the form posts ONLY what changed (a 14-day grid can pass the server's 1,000-field limit), and a total
 *    that was replaced by days is posted blank so the server takes the old line off;
 *  - Enter moves to the next box instead of saving; leaving with unsaved changes asks first.
 */
(function () {
  'use strict';
  var form = document.querySelector('[data-te-form]');
  if (!form) return;
  var table = form.querySelector('table.te-table');
  if (!table) return;

  var grandEls = document.querySelectorAll('[data-te-grand]');
  var msgEl = document.querySelector('[data-te-msg]');
  var submitting = false;

  function num(v) { var n = parseFloat(String(v).trim()); return isFinite(n) ? n : 0; }
  function blank(v) { return String(v).trim() === ''; }
  function fmt(n) { return (Math.round(n * 100) / 100).toFixed(2); }
  function same(a, b) {
    var ab = blank(a), bb = blank(b);
    if (ab || bb) return ab && bb;
    return Math.abs(num(a) - num(b)) < 0.005;
  }

  // ---- the boxes only take xx.xx -------------------------------------------------------------------
  function clean(el, intDigits) {
    var v = el.value.replace(/[^0-9.]/g, '');
    var dot = v.indexOf('.');
    if (dot !== -1) v = v.slice(0, dot + 1) + v.slice(dot + 1).replace(/\./g, '');
    var parts = v.split('.');
    if (parts[0].length > intDigits) parts[0] = parts[0].slice(0, intDigits);
    if (parts.length > 1) parts[1] = parts[1].slice(0, 2);
    v = parts.length > 1 ? parts[0] + '.' + parts[1] : parts[0];
    if (el.value !== v) el.value = v;
  }
  function settle(el, max) {
    if (blank(el.value)) { el.value = ''; return; }
    var n = num(el.value);
    if (n > max) n = max;
    el.value = n > 0 ? fmt(n) : '';   // a zero is the same as nothing typed
  }

  // ---- the model: one cell per employee and category ----------------------------------------------------
  var cells = {};
  function cellFor(s, c) {
    var k = s + '_' + c;
    if (!cells[k]) cells[k] = { s: s, c: c, days: [], total: null, ctot: null };
    return cells[k];
  }
  table.querySelectorAll('input.te-day').forEach(function (el) { cellFor(el.dataset.s, el.dataset.c).days.push(el); });
  table.querySelectorAll('input.te-total').forEach(function (el) {
    var cell = cellFor(el.dataset.s, el.dataset.c);
    cell.total = el;
    cell.editable = el.dataset.editable === '1';
    cell.initialTotal = el.dataset.initialTotal || '';
    cell.initialMode = el.dataset.initialMode || 'total';
    cell.mode = cell.initialMode;
  });
  table.querySelectorAll('[data-te-ctotal]').forEach(function (el) { cellFor(el.dataset.s, el.dataset.c).ctot = el; });
  var cellList = Object.keys(cells).map(function (k) { return cells[k]; }).filter(function (c) { return c.total; });

  function daySum(cell) { return cell.days.reduce(function (a, el) { return a + num(el.value); }, 0); }
  function anyDay(cell) { return cell.days.some(function (el) { return !blank(el.value); }); }

  // Which of the two is open for typing; the other is read-only (not disabled: it still shows its value).
  function applyMode(cell) {
    var wasDays = cell.mode === 'days';
    var inDays = anyDay(cell);
    cell.mode = inDays ? 'days' : 'total';
    var locked = cell.total.dataset.locked === '1';   // the period is locked, or the diocese owns this total
    if (inDays) {
      cell.total.value = fmt(daySum(cell));
      cell.total.readOnly = true;
      cell.total.classList.add('is-sum');
      cell.total.setAttribute('data-tip', 'Entered by day. Clear the days to type a period total instead.');
    } else {
      if (wasDays) cell.total.value = cell.initialMode === 'days' ? cell.initialTotal : '';
      cell.total.classList.remove('is-sum');
      cell.total.removeAttribute('data-tip');
      cell.total.readOnly = locked || !cell.editable;
      if (locked || !cell.editable) {
        if (cell.total.dataset.lockTip) cell.total.setAttribute('data-tip', cell.total.dataset.lockTip);
      }
    }
    var totalTyped = !inDays && !blank(cell.total.value);
    cell.days.forEach(function (el) {
      if (el.disabled) return;
      el.readOnly = totalTyped;
      if (totalTyped) el.setAttribute('data-tip', 'A period total is typed. Clear it to enter days.');
      else el.removeAttribute('data-tip');
    });
  }

  // ---- live totals ---------------------------------------------------------------------------------------
  function recalc() {
    var grand = 0;
    table.querySelectorAll('tbody.te-emp').forEach(function (tb) {
      var s = tb.dataset.staff, etot = 0;
      var perDay = {};
      cellList.forEach(function (cell) {
        if (cell.s !== s) return;
        var v = cell.mode === 'days' ? daySum(cell) : num(cell.total.value);
        if (cell.ctot) cell.ctot.textContent = fmt(v);
        etot += v;
        cell.days.forEach(function (el) { var i = el.dataset.i; perDay[i] = (perDay[i] || 0) + num(el.value); });
      });
      tb.querySelectorAll('[data-te-daytotal]').forEach(function (td) {
        var v = perDay[td.dataset.i] || 0;
        td.textContent = v > 0 ? fmt(v) : '';
      });
      tb.querySelectorAll('[data-te-etotal]').forEach(function (el) { el.textContent = fmt(etot); });
      grand += etot;
    });
    grandEls.forEach(function (el) { el.textContent = fmt(grand); });
    var n = changedCount();
    if (msgEl) {
      msgEl.textContent = n ? (n + (n === 1 ? ' change not saved yet' : ' changes not saved yet')) : '';
      msgEl.className = 'ff-msg' + (n ? ' attn' : '');
    }
  }

  // ---- what changed -----------------------------------------------------------------------------------
  function changedCount() {
    var n = 0;
    cellList.forEach(function (cell) {
      cell.days.forEach(function (el) { if (!el.disabled && !same(el.value, el.defaultValue)) n++; });
      if (totalPost(cell) !== null) n++;
    });
    return n;
  }
  // null = post nothing for this total; otherwise the text to post (blank = take the total off)
  function totalPost(cell) {
    if (!cell.editable || cell.total.dataset.locked === '1') return null;
    if (cell.mode === 'total') return same(cell.total.value, cell.initialTotal) ? null : cell.total.value.trim();
    if (cell.initialMode === 'total' && !blank(cell.initialTotal)) return '';
    return null;
  }

  // ---- wiring ----------------------------------------------------------------------------------------
  cellList.forEach(function (cell) {
    cell.total.addEventListener('input', function () { clean(cell.total, 3); applyMode(cell); recalc(); });
    cell.total.addEventListener('blur', function () { if (!cell.total.readOnly) { settle(cell.total, 300); applyMode(cell); recalc(); } });
    cell.days.forEach(function (el) {
      el.addEventListener('input', function () { clean(el, 2); applyMode(cell); recalc(); });
      el.addEventListener('blur', function () { if (!el.readOnly) { settle(el, 24); applyMode(cell); recalc(); } });
    });
    applyMode(cell);
  });
  recalc();

  // Enter goes to the next box (it never saves by accident).
  form.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter' || !(e.target && e.target.matches && e.target.matches('input.te-day, input.te-total'))) return;
    e.preventDefault();
    var boxes = Array.prototype.slice.call(table.querySelectorAll('input.te-day, input.te-total'))
      .filter(function (b) { return !b.readOnly && !b.disabled && b.offsetParent !== null; });
    var i = boxes.indexOf(e.target);
    if (i !== -1 && boxes[i + 1]) { boxes[i + 1].focus(); boxes[i + 1].select(); }
  });

  // ---- expand / collapse -------------------------------------------------------------------------------
  var memoKey = 'te-open:' + location.pathname;
  function remember() {
    try {
      var open = Array.prototype.slice.call(table.querySelectorAll('tbody.te-emp.is-open')).map(function (t) { return t.dataset.staff; });
      sessionStorage.setItem(memoKey, JSON.stringify(open));
    } catch (e) { /* not remembered */ }
  }
  function setOpen(tb, open) {
    tb.classList.toggle('is-open', open);
    var btn = tb.querySelector('[data-te-toggle]');
    if (btn) { btn.setAttribute('aria-expanded', open ? 'true' : 'false'); btn.innerHTML = open ? '&#9662;' : '&#9656;'; }
  }
  table.querySelectorAll('tbody.te-emp').forEach(function (tb) {
    var btn = tb.querySelector('[data-te-toggle]');
    if (btn) btn.addEventListener('click', function () { setOpen(tb, !tb.classList.contains('is-open')); remember(); });
  });
  try {
    var saved = JSON.parse(sessionStorage.getItem(memoKey) || 'null');
    if (Array.isArray(saved)) table.querySelectorAll('tbody.te-emp').forEach(function (tb) { if (saved.indexOf(tb.dataset.staff) !== -1) setOpen(tb, true); });
  } catch (e) { /* start collapsed */ }
  document.querySelectorAll('[data-te-expand]').forEach(function (b) {
    b.addEventListener('click', function () { table.querySelectorAll('tbody.te-emp').forEach(function (tb) { setOpen(tb, true); }); remember(); });
  });
  document.querySelectorAll('[data-te-collapse]').forEach(function (b) {
    b.addEventListener('click', function () { table.querySelectorAll('tbody.te-emp').forEach(function (tb) { setOpen(tb, false); }); remember(); });
  });

  // ---- save: post only what changed ---------------------------------------------------------------------
  form.addEventListener('submit', function (e) {
    form.querySelectorAll('input.te-gen').forEach(function (g) { g.remove(); });
    // settle every box first (a click on Save does not always blur the box that was being typed in)
    cellList.forEach(function (cell) {
      cell.days.forEach(function (el) { if (!el.readOnly && !el.disabled) settle(el, 24); });
      if (!cell.total.readOnly) settle(cell.total, 300);
      applyMode(cell);
    });
    recalc();
    if (changedCount() === 0) {
      e.preventDefault();
      if (msgEl) { msgEl.textContent = 'Nothing has changed.'; msgEl.className = 'ff-msg ok'; }
      return;
    }
    cellList.forEach(function (cell) {
      cell.days.forEach(function (el) { el.disabled = el.disabled || same(el.value, el.defaultValue); });
      var post = totalPost(cell);
      if (post !== null) {
        var h = document.createElement('input');
        h.type = 'hidden'; h.className = 'te-gen';
        h.name = 'total_' + cell.s + '_' + cell.c; h.value = post;
        form.appendChild(h);
      }
    });
    submitting = true;
    if (window.showButtonLoading) {
      try { window.showButtonLoading(e.submitter || document.querySelector('[data-te-save]')); } catch (err) { /* cosmetic */ }
    }
  });
  window.addEventListener('beforeunload', function (e) {
    if (!submitting && changedCount() > 0) { e.preventDefault(); e.returnValue = ''; }
  });
})();
