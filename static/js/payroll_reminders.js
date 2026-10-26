/* Send reminder emails (templates/timekeeping_reminders.html). No inline handlers (CSP).
 *
 *  - "Tick all with a sender" / "Untick all" set the department boxes;
 *  - the count at the bottom follows what is ticked and typed (departments and addresses), and Send needs at least one department;
 *  - Send asks first, with the same numbers, then locks the button so a second click cannot send twice.
 * The server decides what is allowed; this only helps the person see what they are about to do.
 */
(function () {
  'use strict';

  var form = document.getElementById('remindForm');
  if (!form) return;
  var sendBtn = document.getElementById('remindSend');
  var countEl = document.getElementById('remCount');
  var serverEnabled = sendBtn ? !sendBtn.disabled : false;   // the server switches sending off (no log table, no reply address)

  function rows() { return Array.prototype.slice.call(form.querySelectorAll('tr.rem-row')); }
  function rowTicked(tr) { var cb = tr.querySelector('input.rem-send'); return !!(cb && cb.checked); }
  function addressCount(tr) {
    var n = tr.querySelectorAll('input.rem-address:checked').length;
    var extra = tr.querySelector('input.rem-extra');
    if (extra && extra.value.trim()) n += extra.value.split(/[,;\s]+/).filter(Boolean).length;
    return n;
  }
  function totals() {
    var depts = 0, addrs = 0, noAddress = 0;
    rows().forEach(function (tr) {
      if (!rowTicked(tr)) return;
      depts += 1;
      var n = addressCount(tr);
      addrs += n;
      if (n === 0) noAddress += 1;
    });
    return { depts: depts, addrs: addrs, noAddress: noAddress };
  }
  function refresh() {
    var t = totals();
    if (countEl) {
      countEl.textContent = t.depts + ' ticked, ' + t.addrs + ' address' + (t.addrs === 1 ? '' : 'es') +
        (t.noAddress ? ' (' + t.noAddress + ' ticked with no address)' : '');
    }
    if (sendBtn) sendBtn.disabled = !serverEnabled || t.depts === 0;
  }

  var tickSenders = document.getElementById('remTickSenders');
  if (tickSenders) tickSenders.addEventListener('click', function () {
    rows().forEach(function (tr) {
      var cb = tr.querySelector('input.rem-send');
      if (cb) cb.checked = tr.getAttribute('data-has-sender') === '1';
    });
    refresh();
  });
  var untickAll = document.getElementById('remUntickAll');
  if (untickAll) untickAll.addEventListener('click', function () {
    rows().forEach(function (tr) { var cb = tr.querySelector('input.rem-send'); if (cb) cb.checked = false; });
    refresh();
  });

  form.addEventListener('change', refresh);
  form.addEventListener('input', function (e) { if (e.target && e.target.classList && e.target.classList.contains('rem-extra')) refresh(); });

  form.addEventListener('submit', function (e) {
    var t = totals();
    if (t.depts === 0) { e.preventDefault(); return; }
    if (t.noAddress) {
      e.preventDefault();
      window.alert(t.noAddress + ' ticked department' + (t.noAddress === 1 ? ' has' : 's have') +
        ' no address ticked or typed. Tick an address, type one, or untick the department.');
      return;
    }
    var msg = 'Send the reminder to ' + t.depts + ' department' + (t.depts === 1 ? '' : 's') + ' (' + t.addrs + ' address' +
      (t.addrs === 1 ? '' : 'es') + ') now?';
    if (!window.confirm(msg)) { e.preventDefault(); return; }
    if (sendBtn) {
      if (window.showButtonLoading) window.showButtonLoading(e.submitter || sendBtn);
      else sendBtn.disabled = true;
    }
  });

  refresh();
})();
