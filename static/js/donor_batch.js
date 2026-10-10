// donor_batch.js -- Beacon Donor Management, the one-screen gift entry. No inline handlers (Beacon's standing rule).
//
//  * Envelope number: type it and press Enter (or leave the box) and the donor is filled in. A family shares one envelope, so
//    when several people match they are offered as buttons, the household's primary contact first. The lookup runs on the
//    server against THIS parish's people only; nothing here sends a parish id.
//  * After a line is saved the page reloads with the fund and type kept for the next gift (the server picks them). This script
//    only moves the cursor back to the envelope box so the keyboard flow never needs the mouse.
(function () {
  'use strict';
  var form = document.getElementById('dmEntry');
  if (!form) { return; }
  var env = document.getElementById('dmEnvelope');
  var msg = document.getElementById('dmEnvelopeMsg');
  var picker = form.querySelector('.dm-picker');
  var pickInput = picker ? picker.querySelector('.dm-picker-input') : null;
  var pickHidden = picker ? picker.querySelector('input[type="hidden"]') : null;
  var pickList = picker ? picker.querySelector('.dm-picker-list') : null;
  var amount = document.getElementById('dmAmount');
  var seq = 0;

  function say(text) { if (msg) { msg.textContent = text || ''; } }

  function choose(r) {
    if (pickHidden) { pickHidden.value = r.id; }
    if (pickInput) { pickInput.value = r.name; }
    if (pickList) { pickList.hidden = true; pickList.textContent = ''; }
    say('');
    if (amount) { amount.focus(); }
  }

  function offer(rows) {
    if (!pickList) { return; }
    pickList.textContent = '';
    rows.forEach(function (r) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = r.name + (r.primary ? ' (primary contact)' : '');
      b.addEventListener('click', function () { choose(r); });
      pickList.appendChild(b);
    });
    pickList.hidden = false;
  }

  function lookup() {
    var n = (env.value || '').trim();
    if (!n) { say(''); return; }
    var mine = ++seq;
    fetch(env.getAttribute('data-envelope-url') + '?n=' + encodeURIComponent(n), { headers: { 'Accept': 'application/json' }, credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { results: [] }; })
      .then(function (data) {
        if (mine !== seq) { return; }
        var rows = data.results || [];
        if (rows.length === 0) { say('No one at this parish has envelope ' + n + '.'); if (pickHidden) { pickHidden.value = ''; } }
        else if (rows.length === 1) { choose(rows[0]); }
        else { say(rows.length + ' people share envelope ' + n + '. Pick one.'); offer(rows); }
      })
      .catch(function () { if (mine === seq) { say('The envelope lookup did not work. Search for the donor by name instead.'); } });
  }

  if (env) {
    env.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); lookup(); }      // Enter in the envelope box looks it up, it does not save the line
    });
    env.addEventListener('change', lookup);
  }
  // A non-gift receipt (rent, a reimbursement) is coded to a GL account, not a fund: swap the pickers when the type changes. The picker that
  // is not in use is disabled so the browser does not send it, and hidden so the clerk does not see it.
  var typeSel = document.getElementById('dmType');
  function swapTarget() {
    if (!typeSel) { return; }
    var nc = typeSel.value === 'non_gift_receipt';
    form.querySelectorAll('.dm-gl-only, #dmGlField').forEach(function (el) {
      el.hidden = !nc;
      el.querySelectorAll('select').forEach(function (s) { s.disabled = !nc; });
    });
    form.querySelectorAll('.dm-fund-only, #dmFundField').forEach(function (el) {
      el.hidden = nc;
      el.querySelectorAll('select').forEach(function (s) { s.disabled = nc; });
    });
  }
  if (typeSel) { typeSel.addEventListener('change', swapTarget); swapTarget(); }
  // Back to the start of the next line once the page has reloaded after a save.
  if (env && window.location.hash === '#entry') { env.focus(); }
}());
