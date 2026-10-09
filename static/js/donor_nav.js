/* donor_nav.js -- closes the module bar's Setup menu when you click elsewhere or press Escape. Nothing else. */
(function () {
  'use strict';
  var menu = document.querySelector('[data-dm-setup]');
  if (!menu) return;
  document.addEventListener('click', function (e) {
    if (menu.open && !menu.contains(e.target)) menu.open = false;
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && menu.open) { menu.open = false; var s = menu.querySelector('summary'); if (s) s.focus(); }
  });
})();
