/* The checklist setup screen for one person (26-162; templates/hr_case_setup.html). No inline handlers (CSP); text only via textContent.
 *
 *  - the person pick lists (every row's "Assigned to", and the Add dialog) are filled here from the JSON on the page;
 *  - only assignments that changed are posted (the unchanged selects are disabled just before the post);
 *  - Change / Cross out open their dialog filled from the row's data-* values, and point the form at that step;
 *  - the search box hides rows that do not match. */
(function () {
  'use strict';
  var peopleEl = document.getElementById('csPeople');
  if (!peopleEl) return;
  var people = JSON.parse(peopleEl.textContent || '[]');
  var body = document.getElementById('csBody');
  var assignForm = document.getElementById('assignForm');
  var saveBtn = document.getElementById('csSaveAssign');
  var msg = document.getElementById('csMsg');
  var shown = document.getElementById('csShown');
  var search = document.getElementById('csSearch');
  var submitting = false;

  function opt(value, label) { var o = document.createElement('option'); o.value = value; o.textContent = label; return o; }
  function fillPeople(sel, current) {
    sel.textContent = '';
    sel.appendChild(opt('', 'Nobody in particular'));
    people.forEach(function (p) { sel.appendChild(opt(String(p.id), p.name)); });
    sel.value = current || '';
    if (sel.value !== (current || '')) sel.value = '';
  }
  var assigns = Array.prototype.slice.call(document.querySelectorAll('select[data-cs-assign]'));
  assigns.forEach(function (sel) { fillPeople(sel, sel.getAttribute('data-orig')); });
  var addSel = document.getElementById('addAssignee');
  if (addSel) fillPeople(addSel, '');

  // ---- unsaved assignments ----------------------------------------------------------------------------------------------
  function pending() {
    return assigns.filter(function (s) { return s.value !== (s.getAttribute('data-orig') || ''); });
  }
  function paint() {
    var n = pending().length;
    saveBtn.disabled = n === 0;
    msg.textContent = n === 0 ? 'No unsaved assignments' : 'Not saved yet: ' + n + (n === 1 ? ' assignment' : ' assignments') + ' changed';
    msg.className = 'ff-msg' + (n ? ' attn' : '');
  }
  assigns.forEach(function (s) { s.addEventListener('change', paint); });
  assignForm.addEventListener('submit', function () {
    submitting = true;
    assigns.forEach(function (s) { if (s.value === (s.getAttribute('data-orig') || '')) s.disabled = true; });
  });
  window.addEventListener('beforeunload', function (e) {
    if (!submitting && pending().length) { e.preventDefault(); e.returnValue = ''; }
  });
  paint();

  // ---- the pop-ups ------------------------------------------------------------------------------------------------------
  function show(id) { var d = document.getElementById(id); if (d && d.showModal) d.showModal(); return d; }
  function val(id, v) { var e = document.getElementById(id); if (e) e.value = v || ''; }
  var base = (document.getElementById('assignForm').getAttribute('action') || '').replace(/\/setup\/assign$/, '');
  document.getElementById('csAdd').addEventListener('click', function () {
    var d = show('addDialog'); var f = d && d.querySelector('input[name=label]'); if (f) f.focus();
  });
  body.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-cs-change], button[data-cs-cross]') : null;
    if (!b) return;
    var key = b.getAttribute('data-key');
    if (b.hasAttribute('data-cs-change')) {
      document.getElementById('changeForm').action = base + '/setup/item/' + encodeURIComponent(key) + '/change';
      val('chLabel', b.getAttribute('data-label')); val('chSection', b.getAttribute('data-section')); val('chOwner', b.getAttribute('data-owner'));
      val('chDue', b.getAttribute('data-due')); val('chHard', b.getAttribute('data-hard')); val('chDoc', b.getAttribute('data-doc'));
      document.getElementById('chRequired').checked = !!b.getAttribute('data-required');
      show('changeDialog');
    } else {
      document.getElementById('crossForm').action = base + '/setup/item/' + encodeURIComponent(key) + '/status';
      document.getElementById('crLabel').textContent = b.getAttribute('data-label') || '';
      val('crNote', '');
      var d = show('crossDialog'); var n = document.getElementById('crNote'); if (n) n.focus();
    }
  });

  // ---- search -----------------------------------------------------------------------------------------------------------
  function applySearch() {
    var q = (search.value || '').trim().toLowerCase();
    var rows = Array.prototype.slice.call(body.querySelectorAll('tr.cs-row'));
    var n = 0;
    rows.forEach(function (tr) {
      var hide = !!q && (tr.getAttribute('data-text') || '').indexOf(q) === -1;
      tr.hidden = hide;
      if (!hide && !(tr.classList.contains('is-na-item') && !document.querySelector('.hr-page.show-na'))) n++;
    });
    shown.textContent = n + (n === 1 ? ' step shown' : ' steps shown');
  }
  search.addEventListener('input', applySearch);
  var toggle = document.querySelector('[data-hr-toggle-na]');
  if (toggle) toggle.addEventListener('click', function () { setTimeout(applySearch, 0); });
  applySearch();
})();
