/* donor_users.js -- the Users > Manage user page. "Copy roles from" and "Clear all" only change which boxes are ticked
   (nothing is sent until Save), and never touch a box that is greyed out because this person may not give that role. */
(function () {
  'use strict';
  var form = document.getElementById('dm-roleform');
  if (!form) { return; }

  function boxes() { return form.querySelectorAll('input[type="checkbox"][name="role_key"]:not(:disabled)'); }

  var go = document.getElementById('dm-copy-go');
  var from = document.getElementById('dm-copy-from');
  if (go && from) {
    go.addEventListener('click', function () {
      var opt = from.options[from.selectedIndex];
      if (!opt || !opt.value) { return; }
      var wanted = (opt.getAttribute('data-roles') || '').split(',').filter(Boolean);
      Array.prototype.forEach.call(boxes(), function (b) { b.checked = wanted.indexOf(b.value) !== -1; });
    });
  }

  var clear = document.getElementById('dm-clear-all');
  if (clear) {
    clear.addEventListener('click', function () {
      Array.prototype.forEach.call(boxes(), function (b) { b.checked = false; });
    });
  }
}());
