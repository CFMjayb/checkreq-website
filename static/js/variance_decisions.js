/* Tie-out decisions on the Variance page (26-158, 2026-10-22).
   A decision pick list submits its own little form when it changes (the line's decision, or "all of this person's lines").
   After a decision is saved the line's note box appears (a check number, a date). Editing it saves with the same form,
   but only while a decision is picked. */
(function () {
  'use strict';

  function submitIfPicked(select) {
    if (select && select.form) { select.form.submit(); }
  }

  document.addEventListener('change', function (e) {
    var t = e.target;
    if (!t || !t.classList) { return; }
    if (t.classList.contains('vd-auto')) {
      submitIfPicked(t);
    } else if (t.classList.contains('vd-note')) {
      var sel = t.form && t.form.querySelector('select[name="decision"]');
      if (sel && sel.value) { t.form.submit(); }
    }
  });
})();
