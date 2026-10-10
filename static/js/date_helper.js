// date_helper.js -- ONE date-entry helper for every date box in Donor Management: the staff screens and the member screens alike
// (Jay, 2026-10-10: "we need a much better helper for date entry - the current screen entry sucks").
//
// What it gives a box:
//   - a plain-words line under the box while you type ("Monday, May 2, 1988 - age 38"), so you see what the server will read;
//   - digits are enough: 0521988, 05021988 and 050288 are all read as 05/02/1988 (the SAME rules as donor_core.parse_date, which stays
//     the only judge: this file only helps, and with scripts off every box works exactly as before);
//   - the box turns red the moment what is typed cannot be a date, and says why; a date that could be read two ways is never guessed;
//   - when you leave the box (or submit) a readable date is rewritten as MM/DD/YYYY;
//   - a calendar button with month and year drop-downs, so a 1953 birth date is not forty clicks back;
//   - T fills today, Y yesterday, the up and down arrows step a day, Page Up and Page Down step a month.
// A box is picked up when it is a text input that has data-date, the placeholder MM/DD/YYYY, or a name ending in "date". data-nodate opts out.
// data-no-future (a date that may not be later than today), data-warn-future (a soft warning) and data-age (show the age) are optional;
// the usual names carry them by default. No inline handlers anywhere (Beacon's CSP); everything is wired here.
(function () {
  'use strict';

  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var EARLIEST = 1850, LATEST = 2200;                 // the server's own bounds (donor_core.parse_date)
  var HARD_FUTURE = { deposit_date: 1, gift_date: 1, postmark_date: 1, join_date: 1, married_on: 1 };   // the server refuses a later date
  var SOFT_FUTURE = { birth_date: 1, deceased_date: 1 };                                                 // the server allows it; we only warn

  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function today() { var n = new Date(); return new Date(n.getFullYear(), n.getMonth(), n.getDate()); }
  function daysIn(y, m) { return [31, ((y % 4 === 0 && y % 100 !== 0) || y % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]; }
  function calendarValid(y, m, d) { return y >= 1 && y <= 9999 && m >= 1 && m <= 12 && d >= 1 && d <= daysIn(y, m); }
  function real(y, m, d) { return calendarValid(y, m, d) && y >= EARLIEST && y <= LATEST; }
  function twoDigitYear(y) { return y < 100 ? y + (y > today().getFullYear() % 100 ? 1900 : 2000) : y; }
  function format(y, m, d) { return pad(m) + '/' + pad(d) + '/' + y; }

  // Same reading rules as donor_core._date_parts / parse_date. Returns {kind: 'empty'|'ok'|'ambiguous'|'early'|'notreal'|'partial'|'bad', y, m, d}.
  var PARTIAL = /^(\d{1,2}[\/\-. ]+(\d{1,2}([\/\-. ]+(\d{0,1}|\d{3}))?)?|\d{4}-(\d{1,2}(-\d{0,2})?)?|\d{1,5})$/;
  function read(text) {
    var s = String(text == null ? '' : text).trim();
    if (!s) { return { kind: 'empty' }; }
    var m = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s), y, mo, da;
    if (m) { y = +m[1]; mo = +m[2]; da = +m[3]; }
    else {
      m = /^(\d{1,2})[\/\-. ]+(\d{1,2})[\/\-. ]+(\d{2}|\d{4})$/.exec(s);
      if (m) { y = twoDigitYear(+m[3]); mo = +m[1]; da = +m[2]; }
    }
    if (m) {
      if (!calendarValid(y, mo, da)) { return { kind: 'notreal' }; }
      if (y < EARLIEST) { return { kind: 'early' }; }
      return { kind: 'ok', y: y, m: mo, d: da };
    }
    if (/^\d{6,8}$/.test(s)) {
      var cands = s.length === 6 ? [[s.slice(0, 2), s.slice(2, 4), s.slice(4, 6)]]
        : s.length === 7 ? [[s.slice(0, 1), s.slice(1, 3), s.slice(3, 7)], [s.slice(0, 2), s.slice(2, 3), s.slice(3, 7)]]
        : [[s.slice(0, 2), s.slice(2, 4), s.slice(4, 8)], [s.slice(4, 6), s.slice(6, 8), s.slice(0, 4)]];
      var seen = {}, found = [];
      cands.forEach(function (c) {
        var yy = twoDigitYear(+c[2]), mm = +c[0], dd = +c[1], key = yy + '-' + mm + '-' + dd;
        if (real(yy, mm, dd) && !seen[key]) { seen[key] = 1; found.push({ y: yy, m: mm, d: dd }); }
      });
      if (found.length === 1) { return { kind: 'ok', y: found[0].y, m: found[0].m, d: found[0].d }; }
      if (found.length > 1) { return { kind: 'ambiguous' }; }
      return { kind: 'bad' };
    }
    if (PARTIAL.test(s)) { return { kind: 'partial' }; }
    return { kind: 'bad' };
  }

  function dateOf(r) { return new Date(r.y, r.m - 1, r.d); }
  function words(r) { return WEEKDAYS[dateOf(r).getDay()] + ', ' + MONTHS[r.m - 1] + ' ' + r.d + ', ' + r.y; }
  function ageOn(r, now) {
    var a = now.getFullYear() - r.y;
    if (now.getMonth() + 1 < r.m || (now.getMonth() + 1 === r.m && now.getDate() < r.d)) { a -= 1; }
    return a;
  }
  function isFuture(r) { return dateOf(r).getTime() > today().getTime(); }

  if (typeof module !== 'undefined' && module.exports) { module.exports = { read: read, format: format, twoDigitYear: twoDigitYear }; }
  if (typeof document === 'undefined') { return; }

  // ── the box ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  var coarse = !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  var uid = 0;

  function isBox(el) { return !!el && el.tagName === 'INPUT' && el.hasAttribute('data-dh'); }
  function isDateInput(el) {
    return el.tagName === 'INPUT' && el.type === 'text' && !el.hasAttribute('data-nodate') &&
      (el.hasAttribute('data-date') || el.getAttribute('placeholder') === 'MM/DD/YYYY' || /(^|_)date$/.test(el.name || ''));
  }
  function hardFuture(el) { return el.hasAttribute('data-no-future') || !!HARD_FUTURE[el.name]; }
  function softFuture(el) { return el.hasAttribute('data-warn-future') || !!SOFT_FUTURE[el.name]; }
  function wantsAge(el) { return el.hasAttribute('data-age') || el.name === 'birth_date'; }
  function say(el) { return el.parentNode && el.parentNode.querySelector('.dm-date-say'); }
  function editable(el) { return !el.readOnly && !el.disabled; }

  // What to tell the person about what is in the box: {level: 'none'|'ok'|'warn'|'bad', text}.
  function judge(el, r, finished) {
    if (r.kind === 'empty') { return { level: 'none', text: '' }; }
    if (r.kind === 'ok') {
      var future = isFuture(r);
      if (future && hardFuture(el)) { return { level: 'bad', text: "That date can't be in the future." }; }
      var t = words(r);
      if (future) { return softFuture(el) ? { level: 'warn', text: t + ' - that is in the future. Check it.' } : { level: 'ok', text: t }; }
      if (wantsAge(el)) { t += ' · age ' + ageOn(r, today()); }
      return { level: 'ok', text: t };
    }
    if (r.kind === 'ambiguous') { return { level: 'bad', text: 'That could mean two different dates. Type it with slashes, like 10/9/1953.' }; }
    if (r.kind === 'early') { return { level: 'bad', text: 'That is too far in the past.' }; }
    if (r.kind === 'notreal') { return { level: 'bad', text: "That isn't a real date on the calendar." }; }
    if (r.kind === 'partial') { return finished ? { level: 'bad', text: 'Finish the date, like 10/9/1953.' } : { level: 'none', text: '' }; }
    return { level: 'bad', text: "That isn't a date. Use month, day, year, like 10/9/1953." };
  }

  function apply(el, finished, focused) {
    var j = judge(el, read(el.value), finished);
    var bubble = say(el);
    el.classList.toggle('dm-date-bad', j.level === 'bad');
    el.classList.toggle('dm-date-warn', j.level === 'warn');
    if (j.level === 'bad') { el.setAttribute('aria-invalid', 'true'); } else { el.removeAttribute('aria-invalid'); }
    if (!bubble) { return; }
    bubble.textContent = j.text;
    bubble.classList.toggle('dm-date-bad', j.level === 'bad');
    bubble.classList.toggle('dm-date-warn', j.level === 'warn');
    bubble.hidden = !(j.text && editable(el) && calFor !== el && (focused || j.level === 'bad' || j.level === 'warn'));
  }

  // Leaving the box (or submitting): a readable date is rewritten as MM/DD/YYYY.
  function commit(el, focused) {
    if (editable(el)) {
      var r = read(el.value);
      if (r.kind === 'ok') { el.value = format(r.y, r.m, r.d); }
    }
    apply(el, true, !!focused);
  }

  function setDate(el, d, focused) {
    el.value = format(d.getFullYear(), d.getMonth() + 1, d.getDate());
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    apply(el, true, focused !== false);
  }

  function addMonths(d, n) {
    var y = d.getFullYear(), m = d.getMonth() + n;
    y += Math.floor(m / 12); m = ((m % 12) + 12) % 12;
    return new Date(y, m, Math.min(d.getDate(), daysIn(y, m + 1)));
  }

  function enhance(root) {
    Array.prototype.forEach.call((root || document).querySelectorAll('input[type="text"]'), function (el) {
      if (el.hasAttribute('data-dh') || !isDateInput(el) || !el.parentNode) { return; }
      el.setAttribute('data-dh', '1');
      el.setAttribute('autocomplete', 'off');
      if (coarse) { el.setAttribute('inputmode', 'numeric'); }
      var wrap = document.createElement('span');
      wrap.className = 'dm-date';
      el.parentNode.insertBefore(wrap, el);
      wrap.appendChild(el);
      var bubble = document.createElement('span');
      bubble.className = 'dm-date-say';
      bubble.id = 'dm-date-say-' + (++uid);
      bubble.hidden = true;
      bubble.setAttribute('role', 'status');
      wrap.appendChild(bubble);
      el.setAttribute('aria-describedby', bubble.id);
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'dm-cal';
      btn.setAttribute('aria-label', 'Choose from a calendar');
      btn.setAttribute('aria-haspopup', 'dialog');
      btn.appendChild(icon());
      wrap.appendChild(btn);
    });
  }

  function icon() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', '16'); svg.setAttribute('height', '16'); svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('fill', 'none'); svg.setAttribute('stroke', 'currentColor'); svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round'); svg.setAttribute('stroke-linejoin', 'round'); svg.setAttribute('aria-hidden', 'true');
    var r = document.createElementNS(NS, 'rect');
    r.setAttribute('x', '3'); r.setAttribute('y', '4'); r.setAttribute('width', '18'); r.setAttribute('height', '17'); r.setAttribute('rx', '2');
    svg.appendChild(r);
    ['M3 9h18', 'M8 2v4', 'M16 2v4'].forEach(function (d) { var p = document.createElementNS(NS, 'path'); p.setAttribute('d', d); svg.appendChild(p); });
    return svg;
  }

  // ── the calendar ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

  var cal = null, calFor = null, calY = 0, calM = 1, calFocus = null, calHead = null, calGrid = null;

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text != null) { e.textContent = text; }
    return e;
  }

  function buildCal() {
    cal = el('div', 'dm-calpop');
    cal.hidden = true;
    cal.setAttribute('role', 'dialog');
    cal.setAttribute('aria-label', 'Choose a date');
    var head = el('div', 'dm-calhead');
    var prev = el('button', 'dm-calnav', '‹'); prev.type = 'button'; prev.setAttribute('aria-label', 'Previous month');
    var month = el('select'); month.setAttribute('aria-label', 'Month');
    MONTHS.forEach(function (n, i) { var o = el('option', null, n); o.value = String(i + 1); month.appendChild(o); });
    var year = el('select'); year.setAttribute('aria-label', 'Year');
    var next = el('button', 'dm-calnav', '›'); next.type = 'button'; next.setAttribute('aria-label', 'Next month');
    head.appendChild(prev); head.appendChild(month); head.appendChild(year); head.appendChild(next);
    calHead = { prev: prev, next: next, month: month, year: year };
    calGrid = el('div', 'dm-calgrid');
    var foot = el('div', 'dm-calfoot');
    var tb = el('button', null, 'Today'); tb.type = 'button'; tb.setAttribute('data-cal-today', '1');
    var cb = el('button', null, 'Clear'); cb.type = 'button'; cb.setAttribute('data-cal-clear', '1');
    foot.appendChild(tb); foot.appendChild(cb);
    cal.appendChild(head); cal.appendChild(calGrid); cal.appendChild(foot);
    document.body.appendChild(cal);

    prev.addEventListener('click', function () { shiftMonth(-1); });
    next.addEventListener('click', function () { shiftMonth(1); });
    month.addEventListener('change', function () { calM = +month.value; calFocus = null; renderGrid(); });
    year.addEventListener('change', function () { calY = +year.value; calFocus = null; renderGrid(); });
    tb.addEventListener('click', function () { if (calFor) { var f = calFor; closeCal(false); setDate(f, today(), true); f.focus(); } });
    cb.addEventListener('click', function () {
      if (!calFor) { return; }
      var f = calFor; closeCal(false);
      f.value = ''; f.dispatchEvent(new Event('input', { bubbles: true })); f.dispatchEvent(new Event('change', { bubbles: true }));
      apply(f, true, true); f.focus();
    });
    calGrid.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('button[data-day]') : null;
      if (!b || b.disabled || !calFor) { return; }
      var f = calFor; closeCal(false);
      setDate(f, new Date(calY, calM - 1, +b.getAttribute('data-day')), true);
      f.focus();
    });
    calGrid.addEventListener('keydown', function (e) {
      var b = e.target.closest ? e.target.closest('button[data-day]') : null;
      if (!b) { return; }
      var cur = new Date(calY, calM - 1, +b.getAttribute('data-day')), step = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 }[e.key], to = null;
      if (step) { to = new Date(cur.getFullYear(), cur.getMonth(), cur.getDate() + step); }
      else if (e.key === 'PageUp') { to = addMonths(cur, -1); }
      else if (e.key === 'PageDown') { to = addMonths(cur, 1); }
      else if (e.key === 'Home') { to = new Date(cur.getFullYear(), cur.getMonth(), 1); }
      else if (e.key === 'End') { to = new Date(cur.getFullYear(), cur.getMonth() + 1, 0); }
      if (!to) { return; }
      e.preventDefault();
      calY = to.getFullYear(); calM = to.getMonth() + 1; calFocus = to.getDate();
      renderGrid();
    });
    cal.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { e.preventDefault(); closeCal(true); }
    });
  }

  function shiftMonth(n) {
    var d = addMonths(new Date(calY, calM - 1, 1), n);
    calY = d.getFullYear(); calM = d.getMonth() + 1; calFocus = null;
    renderGrid();
  }

  function renderGrid() {
    var hard = calFor && hardFuture(calFor), now = today(), r = calFor ? read(calFor.value) : { kind: 'empty' };
    var lastYear = Math.max(now.getFullYear() + (hard ? 0 : 20), calY);
    if (+calHead.year.getAttribute('data-last') !== lastYear || !calHead.year.options.length) {
      calHead.year.textContent = '';
      for (var y = EARLIEST; y <= lastYear; y++) { var o = el('option', null, String(y)); o.value = String(y); calHead.year.appendChild(o); }
      calHead.year.setAttribute('data-last', String(lastYear));
    }
    calHead.month.value = String(calM);
    calHead.year.value = String(calY);
    calGrid.textContent = '';
    ['Su', 'Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa'].forEach(function (w) { calGrid.appendChild(el('span', 'dm-calwd', w)); });
    var first = new Date(calY, calM - 1, 1).getDay(), n = daysIn(calY, calM), want = null;
    for (var i = 0; i < first; i++) { calGrid.appendChild(el('span')); }
    for (var d = 1; d <= n; d++) {
      var b = el('button', 'dm-calday', String(d));
      b.type = 'button';
      b.setAttribute('data-day', String(d));
      var when = new Date(calY, calM - 1, d);
      b.setAttribute('aria-label', WEEKDAYS[when.getDay()] + ', ' + MONTHS[calM - 1] + ' ' + d + ', ' + calY);
      if (r.kind === 'ok' && r.y === calY && r.m === calM && r.d === d) { b.setAttribute('aria-selected', 'true'); }
      if (when.getTime() === now.getTime()) { b.classList.add('dm-caltoday'); }
      if (hard && when.getTime() > now.getTime()) { b.disabled = true; }
      calGrid.appendChild(b);
    }
    var target = calFocus || (r.kind === 'ok' && r.y === calY && r.m === calM ? r.d : (now.getFullYear() === calY && now.getMonth() + 1 === calM ? now.getDate() : 1));
    want = calGrid.querySelector('button[data-day="' + target + '"]:not(:disabled)') || calGrid.querySelector('button[data-day]:not(:disabled)');
    if (calFocus && want) { want.focus(); }
    calFocus = null;
    cal._want = want;
  }

  function openCal(input) {
    if (!editable(input)) { return; }
    if (!cal) { buildCal(); }
    var r = read(input.value), base = r.kind === 'ok' ? dateOf(r) : today();
    calFor = input;
    calY = base.getFullYear(); calM = base.getMonth() + 1; calFocus = null;
    var bubble = say(input);
    if (bubble) { bubble.hidden = true; }
    cal.hidden = false;
    cal.style.visibility = 'hidden';
    renderGrid();
    placeCal(input);
    cal.style.visibility = '';
    if (cal._want) { cal._want.focus(); }
  }

  function placeCal(input) {
    var rect = input.getBoundingClientRect(), sx = window.pageXOffset, sy = window.pageYOffset;
    var vw = document.documentElement.clientWidth, w = cal.offsetWidth, h = cal.offsetHeight;
    var left = Math.min(Math.max(rect.left, 8), Math.max(8, vw - w - 8));
    var top = rect.bottom + 4;
    if (top + h > window.innerHeight - 8 && rect.top - h - 4 > 8) { top = rect.top - h - 4; }
    cal.style.left = (left + sx) + 'px';
    cal.style.top = (top + sy) + 'px';
  }

  function closeCal(refocus) {
    if (!cal || cal.hidden) { return; }
    var f = calFor;
    cal.hidden = true;
    calFor = null;
    if (f) { apply(f, false, document.activeElement === f); if (refocus) { f.focus(); } }
  }

  // ── wiring (one set of document-level listeners; boxes added later are picked up by enhance()) ──────────────────────────────

  document.addEventListener('input', function (e) {
    var t = e.target;
    if (!isBox(t)) { return; }
    var v = t.value;
    if (/^\d{8}$/.test(v)) {                       // eight digits is a whole date: show it as MM/DD/YYYY right away
      var r = read(v);
      if (r.kind === 'ok') { t.value = format(r.y, r.m, r.d); }
    }
    apply(t, false, true);
  });
  document.addEventListener('focusin', function (e) {
    var t = e.target;
    if (isBox(t)) { apply(t, false, true); }
    else if (cal && !cal.hidden && !cal.contains(t) && !(t.closest && t.closest('.dm-date'))) { closeCal(false); }
  });
  document.addEventListener('focusout', function (e) {
    var t = e.target;
    if (isBox(t)) { commit(t, false); }
  });
  document.addEventListener('mousedown', function (e) {
    if (cal && !cal.hidden && !cal.contains(e.target) && !(e.target.closest && e.target.closest('.dm-cal'))) { closeCal(false); }
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('.dm-cal') : null;
    if (!b) { return; }
    e.preventDefault();
    var input = b.parentNode.querySelector('input[data-dh]');
    if (!input) { return; }
    if (cal && !cal.hidden && calFor === input) { closeCal(true); } else { openCal(input); }
  });
  document.addEventListener('keydown', function (e) {
    var t = e.target;
    if (!isBox(t) || !editable(t)) { return; }
    if (e.key === 'Escape' && cal && !cal.hidden) { e.preventDefault(); closeCal(true); return; }
    if (e.altKey && e.key === 'ArrowDown') { e.preventDefault(); openCal(t); return; }
    if (e.ctrlKey || e.metaKey || e.altKey) { return; }
    var k = e.key, r = read(t.value), base = r.kind === 'ok' ? dateOf(r) : today(), to = null;
    if (k === 't' || k === 'T') { to = today(); }
    else if (k === 'y' || k === 'Y') { var n = today(); to = new Date(n.getFullYear(), n.getMonth(), n.getDate() - 1); }
    else if (k === 'ArrowUp') { to = new Date(base.getFullYear(), base.getMonth(), base.getDate() + 1); }
    else if (k === 'ArrowDown') { to = new Date(base.getFullYear(), base.getMonth(), base.getDate() - 1); }
    else if (k === 'PageUp') { to = addMonths(base, -1); }
    else if (k === 'PageDown') { to = addMonths(base, 1); }
    if (!to) { return; }
    e.preventDefault();
    setDate(t, to, true);
  });
  document.addEventListener('submit', function (e) {
    Array.prototype.forEach.call(e.target.querySelectorAll ? e.target.querySelectorAll('input[data-dh]') : [], function (i) { commit(i, false); });
  }, true);
  document.addEventListener('reset', function (e) {
    var form = e.target;
    window.setTimeout(function () {
      Array.prototype.forEach.call(form.querySelectorAll ? form.querySelectorAll('input[data-dh]') : [], function (i) { apply(i, false, false); });
    }, 0);
  });
  window.addEventListener('resize', function () { if (cal && !cal.hidden && calFor) { placeCal(calFor); } });

  window.DateHelper = { enhance: enhance, read: read, format: format };
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', function () { enhance(document); }); }
  else { enhance(document); }
}());
