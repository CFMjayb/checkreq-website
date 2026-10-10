// donor_person.js -- Beacon Donor Management, small page helpers. No inline handlers anywhere (the page's CSP rules
// and Beacon's own standing rule); everything is wired here by class and data attribute.
//
//  1. Buttons that submit a form pulse while the request is in flight (showButtonLoading from submit_loading.js).
//  2. The person picker: type, pick someone at THIS parish, the hidden field carries the id. The search endpoint
//     (/people/api/search) derives the parish on the server; nothing here ever sends a parish id.
//  3. Add-person form: show the person fields or the organization field, never both.
(function () {
  'use strict';

  // 1. Pulse on submit. A form with data-confirm is handled by confirm_submit.js first; a refused confirm cancels the
  //    submit before this runs, so we only pulse when the submit really goes ahead.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.classList || e.defaultPrevented) { return; }
    if (!form.closest('.dm-page')) { return; }
    var btn = e.submitter || form.querySelector('button[type="submit"]');
    if (btn && typeof window.showButtonLoading === 'function' && !btn.classList.contains('dm-linklike')) {
      // Let the browser start the submit, then disable: a disabled submitter is not sent with the form.
      window.setTimeout(function () { window.showButtonLoading(btn); }, 0);
    }
  });

  // 2. Person picker
  function initPicker(box) {
    var input = box.querySelector('.dm-picker-input');
    var hidden = box.querySelector('input[type="hidden"]');
    var list = box.querySelector('.dm-picker-list');
    if (!input || !hidden || !list) { return; }
    var timer = null;
    var seq = 0;

    function close() { list.hidden = true; list.textContent = ''; }

    function render(rows) {
      list.textContent = '';
      if (!rows.length) {
        var none = document.createElement('div');
        none.className = 'dm-sub';
        none.style.padding = '8px 12px';
        none.textContent = 'No one at this parish matches.';
        list.appendChild(none);
      }
      rows.forEach(function (r) {
        var b = document.createElement('button');
        b.type = 'button';
        b.textContent = r.name;
        if (r.detail) {
          var s = document.createElement('span');
          s.className = 'dm-sub';
          s.textContent = r.detail;
          b.appendChild(s);
        }
        b.addEventListener('click', function () {
          hidden.value = r.id;
          input.value = r.name;
          close();
        });
        list.appendChild(b);
      });
      list.hidden = false;
    }

    input.addEventListener('input', function () {
      hidden.value = '';                          // typing again clears an earlier choice
      var q = input.value.trim();
      window.clearTimeout(timer);
      if (q.length < 2) { close(); return; }
      timer = window.setTimeout(function () {
        var mine = ++seq;
        fetch('/people/api/search?q=' + encodeURIComponent(q), { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
          .then(function (r) { return r.ok ? r.json() : { results: [] }; })
          .then(function (data) { if (mine === seq) { render(data.results || []); } })
          .catch(function () { if (mine === seq) { close(); } });
      }, 200);
    });
    input.addEventListener('keydown', function (e) { if (e.key === 'Escape') { close(); } });
    document.addEventListener('click', function (e) { if (!box.contains(e.target)) { close(); } });
  }
  Array.prototype.forEach.call(document.querySelectorAll('.dm-picker'), initPicker);

  // 3. Person / organization toggle on the add form
  var radios = document.querySelectorAll('input[name="record_type"]');
  if (radios.length) {
    var sync = function () {
      var checked = document.querySelector('input[name="record_type"]:checked');
      var kind = checked ? checked.value : 'person';
      Array.prototype.forEach.call(document.querySelectorAll('[data-for]'), function (el) {
        el.hidden = el.getAttribute('data-for') !== kind;
      });
    };
    Array.prototype.forEach.call(radios, function (r) { r.addEventListener('change', sync); });
    sync();
  }

  // 4. The Personal tab's one Edit button. The screen is ONE set of fields, read-only until Edit: this flips each control
  //    marked data-ed between read-only and editable IN PLACE (nothing is copied or moved), shows what is edit-only, and
  //    Cancel puts every value back. Controls marked data-locked (a minor's dates the role may not see) never unlock.
  var root = document.querySelector('.dm-page');
  var pform = document.getElementById('dm-personal');
  if (root && pform && pform.hasAttribute('data-editable')) {
    var TEXTISH = { text: 1, search: 1, email: 1, tel: 1, url: 1, number: 1 };
    var firstNewRow = pform.querySelector('.dm-newrow');

    var setEditing = function (on) {
      root.classList.toggle('dm-editing', on);
      pform.classList.toggle('dm-ro', !on);
      Array.prototype.forEach.call(pform.querySelectorAll('[data-ed]'), function (el) {
        if (el.hasAttribute('data-locked')) { return; }
        if (el.tagName === 'INPUT' && TEXTISH[el.type]) { el.readOnly = !on; } else { el.disabled = !on; }
      });
    };

    var editBtn = document.querySelector('[data-dm-edit]');
    if (editBtn) {
      editBtn.addEventListener('click', function () {
        setEditing(true);
        var first = pform.querySelector('input[data-ed]:not([data-locked]):not([type="hidden"])');
        if (first) { first.focus(); }
      });
    }
    Array.prototype.forEach.call(document.querySelectorAll('[data-dm-cancel]'), function (b) {
      b.addEventListener('click', function () {
        pform.reset();                                   // every value back to what the page loaded with
        Array.prototype.forEach.call(pform.querySelectorAll('.dm-newrow'), function (row) { if (row !== firstNewRow) { row.remove(); } });
        setEditing(false);
      });
    });

    // "+ Another" adds one more blank add-contact row.
    var more = pform.querySelector('[data-add-contact]');
    if (more && firstNewRow) {
      more.addEventListener('click', function () {
        var rows = pform.querySelectorAll('.dm-newrow');
        var copy = rows[rows.length - 1].cloneNode(true);
        Array.prototype.forEach.call(copy.querySelectorAll('input[type="text"]'), function (i) { i.value = ''; });
        Array.prototype.forEach.call(copy.querySelectorAll('select'), function (s) { s.selectedIndex = 0; });
        more.closest('.dm-add').parentNode.insertBefore(copy, more.closest('.dm-add'));
        var inp = copy.querySelector('input[type="text"]');
        if (inp) { inp.focus(); }
      });
    }
  }

  // 4. The System tab's "Turn on" button for a person with no email address: it cannot be turned on, so a click says why
  //    right beside the button instead of sending anything. (Without script the form still posts and the server gives the
  //    same message.)
  document.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('[data-needs-email]') : null;
    if (!btn) { return; }
    e.preventDefault();
    var msg = btn.parentNode.querySelector('[data-needs-email-msg]');
    if (msg) { msg.hidden = false; }
  });
}());
