/* The parish Staff screen's two shared pop-ups (templates/timekeeping_roster.html), 2026-10-25. No inline handlers (CSP).
 *
 *  - Edit: a row's Edit button (data-staff-edit, with the person in data- attributes) fills the shared #editStaffDialog and points
 *    its form at that person.
 *  - Status change: a row's Status change button (data-staff-status) opens #statusStaffDialog for that person. Step 1 asks what the
 *    change is, the effective date and a note; Review checks them and shows one plain sentence; Confirm sends. "Other" needs a note.
 *    An active person gets the leaving reasons, an inactive person the coming-back ones.
 * The server checks everything again; this only makes the form easy to use.
 */
(function () {
  'use strict';

  function show(d) { if (d.showModal) d.showModal(); else d.setAttribute('open', ''); }

  // ---- Edit ----------------------------------------------------------------------
  var editDialog = document.getElementById('editStaffDialog');
  if (editDialog) {
    var editForm = editDialog.querySelector('[data-staff-edit-form]');
    document.querySelectorAll('[data-staff-edit]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        editForm.action = '/timekeeping/roster/' + btn.dataset.id + '/update';
        editForm.elements.first_name.value = btn.dataset.first || '';
        editForm.elements.last_name.value = btn.dataset.last || '';
        editForm.elements.position.value = btn.dataset.position || '';
        editForm.elements.employee_number.value = btn.dataset.emp || '';
        show(editDialog);
        editForm.elements.first_name.focus();
      });
    });
  }

  // ---- Status change -------------------------------------------------------------
  var dlg = document.getElementById('statusStaffDialog');
  if (!dlg) return;
  var form = dlg.querySelector('[data-staff-status-form]');
  var step1 = dlg.querySelector('[data-status-step="1"]');
  var step2 = dlg.querySelector('[data-status-step="2"]');
  var title = dlg.querySelector('[data-status-title]');
  var help = dlg.querySelector('[data-status-help]');
  var dateLabel = dlg.querySelector('[data-status-date-label]');
  var err = dlg.querySelector('[data-status-error]');
  var summary = dlg.querySelector('[data-status-summary]');
  var selects = {
    deactivate: dlg.querySelector('[data-reason-select="deactivate"]'),
    reactivate: dlg.querySelector('[data-reason-select="reactivate"]'),
    stay: dlg.querySelector('[data-reason-select="stay"]')
  };
  var stayFlag = dlg.querySelector('[data-stay-flag]');
  var current = { name: '', mode: 'deactivate', leaving: '' };

  function setStep(n) {
    step1.hidden = n !== 1;
    step2.hidden = n !== 2;
  }
  function activeSelect() { return selects[current.mode]; }
  function fmtDate(iso) {
    var p = (iso || '').split('-');
    return p.length === 3 ? p[1] + '/' + p[2] + '/' + p[0].slice(2) : iso;
  }

  document.querySelectorAll('[data-staff-status]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      current.name = btn.dataset.name;
      // "stay" (26-158): someone whose termination was approved but whose last day has not come ("Keep on staff") cancels the leaving date
      current.mode = btn.dataset.mode === 'stay' ? 'stay' : (btn.dataset.active === '1' ? 'deactivate' : 'reactivate');
      current.leaving = btn.dataset.leaving || '';
      stayFlag.value = current.mode === 'stay' ? '1' : '';
      form.action = '/timekeeping/roster/' + btn.dataset.id + '/toggle-active';
      Object.keys(selects).forEach(function (k) {
        var on = k === current.mode;
        selects[k].hidden = !on;
        selects[k].disabled = !on;
        selects[k].value = '';
      });
      title.textContent = current.mode === 'deactivate' ? 'Status change: ' + current.name
        : current.mode === 'stay' ? 'Keep ' + current.name + ' on staff' : 'Bring ' + current.name + ' back';
      help.textContent = current.mode === 'deactivate'
        ? 'Tell the diocese why ' + current.name + ' is no longer on staff, and the last day of work.'
          + (current.leaving ? ' ' + current.name + ' is already set to leave on ' + current.leaving + '; the date you enter replaces it once the diocese approves.' : '')
        : current.mode === 'stay'
          ? current.name + ' is set to leave on ' + current.leaving + '. Tell the diocese why they are staying; if it approves, the leaving date is cancelled.'
          : 'Tell the diocese why ' + current.name + ' is returning, and the date they return.';
      dateLabel.textContent = current.mode === 'deactivate' ? 'Last day of work' : (current.mode === 'stay' ? 'As of' : 'Return date');
      form.elements.note.value = '';
      err.hidden = true;
      setStep(1);
      show(dlg);
      activeSelect().focus();
    });
  });

  dlg.querySelector('[data-status-review]').addEventListener('click', function () {
    var sel = activeSelect();
    var note = form.elements.note.value.trim();
    var date = form.elements.effective_date.value;
    var problem = '';
    if (!sel.value) problem = 'Choose what the change is.';
    else if (!date) problem = 'Enter the date.';
    else if (sel.value === 'other' && !note) problem = 'Explain the change in the note.';
    if (problem) { err.textContent = problem; err.hidden = false; return; }
    err.hidden = true;
    var reason = sel.options[sel.selectedIndex].text;
    summary.textContent = current.mode === 'deactivate'
      ? 'You are asking the diocese to make ' + current.name + ' inactive as of ' + fmtDate(date) + '. Reason: ' + reason + '.' + (note ? ' Note: ' + note : '')
        + ' If that last day is today or later, ' + current.name + ' stays on staff until it has passed.'
      : current.mode === 'stay'
        ? 'You are asking the diocese to keep ' + current.name + ' on staff and cancel the leaving date (' + current.leaving + '). Reason: ' + reason + '.' + (note ? ' Note: ' + note : '')
        : 'You are asking the diocese to bring ' + current.name + ' back as of ' + fmtDate(date) + '. Reason: ' + reason + '.' + (note ? ' Note: ' + note : '');
    setStep(2);
  });
  dlg.querySelector('[data-status-back]').addEventListener('click', function () { setStep(1); });
  dlg.addEventListener('close', function () { setStep(1); });
})();
