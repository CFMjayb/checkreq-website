/* 26-162 Onboarding & Offboarding: small helpers for the board, the case page and the new-case page (no inline handlers: CSP).
   - a button with data-hr-dialog="<id>" opens that <dialog>; data-hr-close closes it;
   - a select with data-hr-toggle="<id>" shows the element with that id only when its value is "yes";
   - a select with data-hr-nav-select goes to the address in its chosen option (the board's Type filter);
   - Waive / Not applicable buttons reveal the reason box of their row before submitting;
   - Enter in an item's note box does nothing (it used to press the first button, Done, so a typed reason + Enter marked
     the item Done instead of Waived);
   - Standing UI-UX Rule 6: every action button pulses while its post is in flight, in the dialogs too. */
(function () {
  document.querySelectorAll('[data-hr-dialog]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var d = document.getElementById(btn.getAttribute('data-hr-dialog'));
      if (d && d.showModal) { d.showModal(); var f = d.querySelector('input, select, textarea'); if (f) f.focus(); }
    });
  });
  document.querySelectorAll('[data-hr-close]').forEach(function (b) {
    b.addEventListener('click', function () { var d = b.closest('dialog'); if (d) d.close(); });
  });
  document.querySelectorAll('[data-hr-toggle]').forEach(function (sel) {
    var target = document.getElementById(sel.getAttribute('data-hr-toggle'));
    function paint() { if (target) target.hidden = sel.value !== 'yes'; }
    sel.addEventListener('change', paint); paint();
  });
  document.querySelectorAll('select[data-hr-nav-select]').forEach(function (sel) {
    sel.addEventListener('change', function () { if (sel.value) window.location.href = sel.value; });
  });
  document.querySelectorAll('form[data-hr-needs-note]').forEach(function (form) {
    var note = form.querySelector('textarea[name=note], input[name=note]');
    if (note) {
      note.addEventListener('input', function () { note.classList.remove('needs'); });
    }
    form.addEventListener('submit', function (e) {
      var btn = e.submitter;
      if (!btn || !note) return;
      var needs = btn.value === 'waived' || btn.value === 'not_applicable';
      if (needs && !note.value.trim()) {
        e.preventDefault();
        note.hidden = false; note.required = true; note.focus();
        note.classList.add('needs');
        note.placeholder = 'Say why (required)';
      }
    });
  });
  // "Show not applicable items": items marked not applicable are hidden until this is pressed (Jay, 2026-10-25). The choice is
  // remembered on this PC; if storage is unavailable the items simply start hidden.
  document.querySelectorAll('[data-hr-toggle-na]').forEach(function (btn) {
    var page = btn.closest('.hr-page');
    var n = btn.getAttribute('data-count');
    function paint(on) {
      if (page) page.classList.toggle('show-na', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.textContent = (on ? 'Hide' : 'Show') + ' not applicable items (' + n + ')';
    }
    var saved = false;
    try { saved = localStorage.getItem('hr-show-na') === '1'; } catch (e) { /* not remembered */ }
    paint(saved);
    btn.addEventListener('click', function () {
      var on = !(page && page.classList.contains('show-na'));
      paint(on);
      try { localStorage.setItem('hr-show-na', on ? '1' : '0'); } catch (e) { /* not remembered */ }
    });
  });
  // Rule 6: the clicked button pulses while the post is in flight. The button's own name/value is copied into a hidden
  // field first, because a disabled button is not sent with the form.
  document.querySelectorAll('.hr-page form, dialog.hr-dialog form').forEach(function (form) {
    form.addEventListener('submit', function (e) {
      if (e.defaultPrevented) return;
      var btn = e.submitter;
      if (!btn || typeof window.showButtonLoading !== 'function') return;
      if (btn.name) {
        var h = document.createElement('input');
        h.type = 'hidden'; h.name = btn.name; h.value = btn.value; h.setAttribute('data-hr-copy', '1');
        form.appendChild(h);
      }
      window.showButtonLoading(btn);
    });
  });
  // Coming back with the Back button can restore the page with a pulsing, disabled button: put the buttons back.
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;
    document.querySelectorAll('.btn-loading').forEach(function (b) { b.classList.remove('btn-loading'); b.disabled = false; });
    document.querySelectorAll('input[data-hr-copy]').forEach(function (h) { h.remove(); });
  });
})();
