/* manage_parishes.js -- Manage Parishes pop-ups (the Portal and Finance buttons).
   Each button is a normal link to a page that shows the form, so everything works without this file. With it, a click opens the same form
   in a pop-up: the page is fetched with ?fragment=1 (just the form) and shown in the <dialog id="mp-dialog">. The form inside is an ordinary
   form: saving posts and comes back to Manage Parishes with a banner. Cancel, Escape and a click outside the box close it. No inline handlers. */
(function () {
  var dlg = document.getElementById('mp-dialog');
  if (!dlg || typeof dlg.showModal !== 'function') return;          // no <dialog> support: the links just open the full page
  var body = dlg.querySelector('.mp-dialog-body');

  document.addEventListener('click', function (e) {
    var link = e.target.closest ? e.target.closest('a[data-popup]') : null;
    if (!link) return;
    e.preventDefault();
    body.textContent = 'Loading...';
    if (!dlg.open) dlg.showModal();
    fetch(link.getAttribute('href') + '?fragment=1', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(String(r.status)); return r.text(); })
      .then(function (html) { body.innerHTML = html; var first = body.querySelector('input:not([type=hidden]), select'); if (first) first.focus(); })
      .catch(function () { body.textContent = 'This could not be loaded. Close it and try again.'; });
  });

  dlg.addEventListener('click', function (e) {
    if (e.target === dlg || (e.target.closest && e.target.closest('[data-popup-close]'))) dlg.close();
  });

  dlg.addEventListener('submit', function (e) {                       // the same busy pulse every other Beacon form shows
    if (e.defaultPrevented || typeof window.showButtonLoading !== 'function') return;
    var btn = e.submitter || e.target.querySelector('button[type="submit"]');
    if (btn) window.showButtonLoading(btn);
  });
})();
