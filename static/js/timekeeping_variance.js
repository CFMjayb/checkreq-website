/* Hours variance report (templates/timekeeping_variance.html): the boxes in the bottom bar.
 * A box before each kind of line on the right of the bar: ticked = those lines are listed, unticked = hidden.
 * Every row carries its kind in data-kind; a box carries the kind it controls in data-vkind. The choice is remembered on this PC
 * (the boxes stay in view, so it is never hidden which lines are off). No inline handlers (CSP). */
(function () {
  'use strict';

  var shell = document.querySelector('.ff-shell');
  if (!shell) return;
  var tbody = shell.querySelector('table.ff-table tbody');
  var checks = Array.prototype.slice.call(shell.querySelectorAll('.ff-foot input[data-vkind]'));
  if (!tbody || !checks.length) return;

  var KEY = 'variance:kinds-off';

  try {
    var saved = JSON.parse(localStorage.getItem(KEY) || 'null');
    if (Array.isArray(saved)) checks.forEach(function (cb) { cb.checked = saved.indexOf(cb.dataset.vkind) === -1; });
  } catch (e) { /* storage may be unavailable: everything simply starts ticked */ }

  function apply() {
    var off = {};
    checks.forEach(function (cb) { if (!cb.checked) off[cb.dataset.vkind] = true; });
    var all = Array.prototype.slice.call(tbody.querySelectorAll('tr[data-kind]'));
    var shown = 0;
    all.forEach(function (tr) {
      var hide = off[tr.dataset.kind] === true;
      tr.hidden = hide;
      if (!hide) shown++;
    });
    var none = tbody.querySelector('tr[data-v-none]');
    if (none) none.hidden = !(all.length > 0 && shown === 0);
  }

  checks.forEach(function (cb) {
    cb.addEventListener('change', function () {
      try {
        localStorage.setItem(KEY, JSON.stringify(checks.filter(function (x) { return !x.checked; }).map(function (x) { return x.dataset.vkind; })));
      } catch (e) { /* not remembered */ }
      apply();
    });
  });
  apply();
})();
