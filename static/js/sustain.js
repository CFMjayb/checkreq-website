/* Parish Sustainability Review: the 10 years / all years and Table switches on each chart, and the "Can we afford a priest?" calculator.
   The calculator does no arithmetic of its own: every change is sent to POST /sustainability/project and the answer, tiles, chart and
   table come back already drawn, so the numbers on the page always come from the one tested function. No inline handlers (the page's
   security rules). */
(function () {
  var root = document.getElementById('svRoot');
  if (!root) return;
  var base = root.getAttribute('data-base') || '/sustainability';   /* the parish's own routes, or the diocese's for one parish */
  document.documentElement.classList.add('sv-js');

  /* ---------------------------------------------------------------- chart switches */
  document.addEventListener('click', function (ev) {
    var t = ev.target.closest ? ev.target.closest('.sv-range, .sv-tbtn') : null;
    if (!t) return;
    var card = t.closest('.sv-chart, .sv-proj');
    if (!card) return;
    if (t.classList.contains('sv-range')) {
      card.setAttribute('data-range', t.getAttribute('data-range'));
      Array.prototype.forEach.call(card.querySelectorAll('.sv-range'), function (b) {
        var on = b === t;
        b.classList.toggle('is-active', on);
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
    } else {
      var table = card.getAttribute('data-mode') !== 'table';
      card.setAttribute('data-mode', table ? 'table' : 'chart');
      t.setAttribute('aria-pressed', table ? 'true' : 'false');
      t.textContent = table ? 'Chart' : 'Table';
    }
  });

  /* ---------------------------------------------------------------- calculator */
  var form = document.getElementById('svForm');
  if (!form) return;
  var resultBox = document.getElementById('svResult');
  var errorBox = document.getElementById('svError');
  var msg = document.getElementById('svScenMsg');
  var sheetOn = false;
  var seq = 0;
  var timer = null;
  var $ = function (id) { return document.getElementById(id); };

  function num(el) {
    var v = el.value.trim();
    if (v === '') return null;
    var n = Number(v);
    return isFinite(n) ? n : NaN;
  }
  function inputs() {
    return {
      salary: num($('svSalary')),
      health_annual: num($('svHealth')),
      income_growth: Number($('svIncome').value) / 100,
      salary_growth: Number($('svRaise').value) / 100,
      other_exp_growth: Number($('svOther').value) / 100,
      safe_draw_rate: Number($('svDraw').value) / 100,
      included: $('svIncluded').checked,
      count_inv_as_draw: $('svDrawSwitch').checked
    };
  }
  function fill(f) {   /* put the server's values into the form fields */
    if (!f) return;
    $('svSalary').value = f.salary;
    $('svHealth').value = f.health_annual;
    $('svIncome').value = f.income_growth;
    $('svRaise').value = f.salary_growth;
    $('svOther').value = f.other_exp_growth;
    $('svDraw').value = f.safe_draw_rate;
    $('svIncluded').checked = !!f.included;
    $('svDrawSwitch').checked = !!f.count_inv_as_draw;
    $('svPension').textContent = f.pension_rate;
    outputs();
  }
  function outputs() {
    [['svIncome', 'svIncomeOut'], ['svRaise', 'svRaiseOut'], ['svOther', 'svOtherOut'], ['svDraw', 'svDrawOut']].forEach(function (p) {
      $(p[1]).textContent = Number($(p[0]).value) + '%';
    });
  }
  function lockSheetFields(on) {   /* spreadsheet mode fixes the rates and switches; salary and safe draw stay editable */
    ['svHealth', 'svIncome', 'svRaise', 'svOther', 'svIncluded', 'svDrawSwitch'].forEach(function (id) { $(id).disabled = on; });
  }
  function headers() { return Object.assign({ 'Content-Type': 'application/json' }, window.csrfHeader ? window.csrfHeader() : {}); }

  function post(url, body) {
    return fetch(url, { method: 'POST', credentials: 'same-origin', headers: headers(), body: JSON.stringify(body) })
      .then(function (r) { return r.json().catch(function () { return { ok: false, error: 'The server sent something unexpected.' }; }); });
  }

  function recalc(opts) {
    opts = opts || {};
    var mine = ++seq;
    var body = { inputs: opts.inputs || inputs(), spreadsheet_mode: sheetOn };
    post(base + '/project', body).then(function (data) {
      if (mine !== seq) return;            /* a newer request is already on its way */
      if (data.form && (opts.refill || sheetOn)) fill(data.form);
      if (data.ok) {
        resultBox.innerHTML = data.html;
        errorBox.hidden = true;
      } else {
        errorBox.textContent = data.error || 'Could not work this out.';
        errorBox.hidden = false;
        if (!data.form) return;            /* keep the last good answer on screen beside the message */
      }
    }).catch(function () {
      if (mine !== seq) return;
      errorBox.textContent = 'Could not reach the server. Check your connection and try again.';
      errorBox.hidden = false;
    });
  }
  function later() { clearTimeout(timer); timer = setTimeout(recalc, 200); }

  form.addEventListener('submit', function (e) { e.preventDefault(); });
  form.addEventListener('input', function (e) {
    if (e.target.id === 'svScenName') return;
    outputs();
    later();
  });
  form.addEventListener('change', function (e) {
    if (e.target.id === 'svScenName' || e.target.id === 'svScenPick') return;
    later();
  });

  /* ---------------------------------------------------------------- spreadsheet mode (diocesan staff) */
  var sheet = $('svSheet');
  if (sheet) {
    sheet.addEventListener('click', function (e) {
      e.preventDefault();
      sheetOn = !sheetOn;
      sheet.setAttribute('aria-pressed', sheetOn ? 'true' : 'false');
      sheet.textContent = sheetOn ? 'Leave spreadsheet mode' : 'Spreadsheet mode';
      sheet.parentNode.classList.toggle('is-on', sheetOn);
      lockSheetFields(sheetOn);
      recalc({ refill: true });
    });
  }

  /* ---------------------------------------------------------------- saved scenarios */
  function say(text, bad) { msg.textContent = text; msg.style.color = bad ? '#b3261e' : ''; }
  function refreshList(selectId) {
    return fetch(base + '/scenarios', { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      var pick = $('svScenPick');
      var list = d.scenarios || [];
      pick.innerHTML = '';
      var first = document.createElement('option');
      first.value = '';
      first.textContent = list.length ? 'Choose one' : 'None saved yet';
      pick.appendChild(first);
      list.forEach(function (s) {
        var o = document.createElement('option');
        o.value = String(s.id);
        o.textContent = s.name + (s.created_by ? ' (' + s.created_by + ')' : '');
        pick.appendChild(o);
      });
      if (selectId) pick.value = String(selectId);
    });
  }
  $('svSave').addEventListener('click', function () {
    var name = $('svScenName').value.trim();
    if (!name) { say('Type a name for the scenario first.', true); return; }
    post(base + '/scenarios', { name: name, inputs: inputs() }).then(function (d) {
      if (!d.ok) { say(d.error || 'Could not save.', true); return; }
      say('Saved "' + d.name + '".', false);
      $('svScenName').value = '';
      refreshList(d.id);
    });
  });
  $('svLoad').addEventListener('click', function () {
    var id = $('svScenPick').value;
    if (!id) { say('Choose a saved scenario first.', true); return; }
    fetch(base + '/scenarios/' + encodeURIComponent(id), { credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (d) {
      if (d.error) { say(d.error, true); return; }
      sheetOn = false;
      if (sheet) { sheet.setAttribute('aria-pressed', 'false'); sheet.textContent = 'Spreadsheet mode'; sheet.parentNode.classList.remove('is-on'); }
      lockSheetFields(false);
      say('Loaded "' + d.name + '".', false);
      recalc({ inputs: d.inputs, refill: true });
    });
  });
  $('svDelete').addEventListener('click', function () {
    var pick = $('svScenPick');
    var id = pick.value;
    if (!id) { say('Choose a saved scenario first.', true); return; }
    if (!window.confirm('Delete the saved scenario "' + pick.options[pick.selectedIndex].textContent + '"?')) return;
    post(base + '/scenarios/' + encodeURIComponent(id) + '/delete', {}).then(function (d) {
      if (!d.ok) { say(d.error || 'Could not delete.', true); return; }
      say('Deleted.', false);
      refreshList();
    });
  });
})();
