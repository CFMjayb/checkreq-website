/* Roster Review: the Move to Onboarding pop-up (26-158 / 26-162). No inline handlers (CSP).
 *
 * Each new-hire row's Approve button carries data-onb-open, the person's name (data-who) and the approve address (formaction).
 * Clicking it opens ONE shared <dialog id="onboardDialog"> instead of submitting: the form's address is set to that row's approve
 * address, the person's name is shown, and the "Move to Onboarding" box starts UNTICKED every time. Confirm posts the form.
 * Without script the same button simply submits the (hidden) form with the box unticked, which is a plain approve.
 */
(function () {
  'use strict';

  var dlg = document.getElementById('onboardDialog');
  var form = document.getElementById('onboardForm');
  if (!dlg || !form) return;
  var who = document.getElementById('onboardWho');
  var box = form.querySelector('input[name="move_to_onboarding"]');

  document.querySelectorAll('[data-onb-open]').forEach(function (btn) {
    btn.addEventListener('click', function (e) {
      e.preventDefault();
      form.setAttribute('action', btn.getAttribute('formaction') || '');
      if (who) who.textContent = btn.getAttribute('data-who') || '';
      if (box) box.checked = false;
      if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
      if (box) box.focus();
    });
  });
  document.querySelectorAll('[data-onb-close]').forEach(function (b) {
    b.addEventListener('click', function () {
      if (dlg.close) dlg.close(); else dlg.removeAttribute('open');
    });
  });
})();
