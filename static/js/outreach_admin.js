// outreach_admin.js -- 26-156: the create / edit form for polls (admin_outreach_form.html).
//
// Builds the audience rows and the question rows from the JSON in #oa-init, keeps a live
// "who would be asked" preview (the server resolves it and refuses entities the person does not
// administer), and on submit writes the two JSON blobs the server re-validates. Everything the
// admin types is put into the page with textContent / setAttribute only -- never innerHTML --
// so nothing typed here can ever be interpreted as markup.
(function () {
  'use strict';
  var initEl = document.getElementById('oa-init');
  if (!initEl) return;
  var init = JSON.parse(initEl.textContent);
  var form = document.getElementById('oa-form');
  var audienceBox = document.getElementById('oa-audience');
  var questionsBox = document.getElementById('oa-questions');
  var previewBox = document.getElementById('oa-preview');
  var selectors = init.selectors;
  var questions = init.questions.map(normalizeQuestion);
  var parishCache = {};
  var previewTimer = null;
  var previewSeq = 0;

  // ---- tiny DOM helper ----------------------------------------------------
  function h(tag, attrs, kids) {
    var el = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (k === 'text') el.textContent = v;
      else if (k === 'class') el.className = v;
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else if (v !== false && v != null) el.setAttribute(k, v);
    });
    (kids || []).forEach(function (c) {
      if (c) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return el;
  }

  function select(options, current, onChange, label) {
    var sel = h('select', { 'aria-label': label || '' });
    options.forEach(function (o) {
      var opt = h('option', { value: o.value, text: o.label });
      sel.appendChild(opt);
    });
    sel.value = current == null ? '' : String(current);
    sel.addEventListener('change', function () { onChange(sel.value); });
    return sel;
  }

  // ---- audience -----------------------------------------------------------
  function loadParishes(orgId, cb) {
    if (parishCache[orgId]) { cb(parishCache[orgId]); return; }
    fetch('/admin/polls/parishes?org_id=' + encodeURIComponent(orgId), { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : { parishes: [] }; })
      .then(function (j) { parishCache[orgId] = j.parishes || []; cb(parishCache[orgId]); })
      .catch(function () { cb([]); });
  }

  function renderAudience() {
    audienceBox.textContent = '';
    selectors.forEach(function (s, i) {
      if (!init.orgs.some(function (o) { return o.id === s.org_id; }) && init.orgs.length) {
        s.org_id = init.orgs[0].id;
      }
      var row = h('div', { 'class': 'oa-row' });
      row.appendChild(select(
        [{ value: 'entity_role', label: 'Entity role' }, { value: 'parish_role', label: 'Parish role' }],
        s.source,
        function (v) { s.source = v; s.role_key = ''; s.scope = 'org'; s.parish_id = null; renderAudience(); changed(); },
        'Kind of role'));
      var roles = (s.source === 'parish_role' ? init.parish_roles : init.entity_roles)
        .map(function (r) { return { value: r.key, label: r.label }; });
      row.appendChild(select([{ value: '', label: 'Choose a role…' }].concat(roles), s.role_key,
        function (v) { s.role_key = v; changed(); }, 'Role'));
      row.appendChild(h('span', { 'class': 'oa-at', text: 'at' }));
      row.appendChild(select(
        init.orgs.map(function (o) { return { value: String(o.id), label: o.code + ' — ' + o.name }; }),
        s.org_id,
        function (v) { s.org_id = parseInt(v, 10); s.parish_id = null; renderAudience(); changed(); },
        'Entity'));
      if (s.source === 'parish_role') {
        row.appendChild(select(
          [{ value: 'org', label: 'every parish in this diocese' }, { value: 'parish', label: 'one parish' }],
          s.scope,
          function (v) { s.scope = v; s.parish_id = null; renderAudience(); changed(); },
          'Which parishes'));
        if (s.scope === 'parish') {
          var holder = h('span', { 'class': 'oa-parish-slot', text: 'Loading parishes…' });
          row.appendChild(holder);
          loadParishes(s.org_id, function (list) {
            holder.textContent = '';
            var opts = [{ value: '', label: 'Choose a parish…' }].concat(
              list.map(function (p) { return { value: String(p.id), label: p.name }; }));
            holder.appendChild(select(opts, s.parish_id, function (v) {
              s.parish_id = v ? parseInt(v, 10) : null; changed();
            }, 'Parish'));
          });
        }
      }
      row.appendChild(h('button', {
        type: 'button', 'class': 'btn btn-sm btn-secondary', text: 'Remove', disabled: selectors.length < 2,
        'aria-label': 'Remove this group',
        onclick: function () { selectors.splice(i, 1); renderAudience(); changed(); }
      }));
      audienceBox.appendChild(row);
    });
  }

  function changed() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, 350);
  }

  function preview() {
    var seq = ++previewSeq;   // bumped FIRST, so even the "nothing chosen" path supersedes an in-flight request
    var ready = selectors.filter(function (s) { return s.role_key && (s.scope !== 'parish' || s.parish_id); });
    previewBox.textContent = '';
    if (!ready.length) {
      previewBox.appendChild(h('span', { 'class': 'hint', text: 'Choose a role to see who would be asked.' }));
      return;
    }
    previewBox.appendChild(h('span', { 'class': 'hint', text: 'Checking who that is…' }));
    fetch('/admin/polls/audience-preview', {
      method: 'POST', credentials: 'same-origin',
      headers: Object.assign({ 'Content-Type': 'application/json' }, window.csrfHeader ? window.csrfHeader() : {}),
      body: JSON.stringify({ selectors: ready })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (seq !== previewSeq) return;               // a newer request superseded this one
      previewBox.textContent = '';
      if (j.errors && j.errors.length) {
        previewBox.appendChild(h('ul', { 'class': 'oa-errors' }, j.errors.map(function (e) { return h('li', { text: e }); })));
        return;
      }
      var n = j.count;
      previewBox.appendChild(h('strong', { text: n + (n === 1 ? ' person' : ' people') + ' would be asked.' }));
      if (n) {
        var rows = j.people.map(function (p) {
          return h('tr', {}, [h('td', { text: p.name }), h('td', { text: p.email }), h('td', { text: p.via.join('; ') })]);
        });
        var table = h('table', { 'class': 'oa-table' }, [
          h('thead', {}, [h('tr', {}, [h('th', { text: 'Name' }), h('th', { text: 'Email' }), h('th', { text: 'Why' })])]),
          h('tbody', {}, rows)]);
        var det = h('details', {}, [h('summary', { text: 'Show the list' + (n > j.people.length ? ' (first ' + j.people.length + ')' : '') }), table]);
        previewBox.appendChild(det);
      } else {
        previewBox.appendChild(h('p', { 'class': 'hint', text: 'No one holds that role right now.' }));
      }
    }).catch(function () {
      if (seq === previewSeq) previewBox.textContent = 'The preview could not be loaded. You can still save.';
    });
  }

  document.getElementById('oa-add-audience').addEventListener('click', function () {
    selectors.push({ source: 'entity_role', role_key: '', org_id: init.current_org_id, scope: 'org', parish_id: null });
    renderAudience();
  });

  // ---- questions ----------------------------------------------------------
  function normalizeQuestion(q) {
    var cfg = q.config || {};
    return {
      qtype: q.qtype || 'yes_no', prompt: q.prompt || '', required: q.required !== false,
      yes_label: cfg.yes_label || '', no_label: cfg.no_label || '',
      options_text: (cfg.options || []).map(function (o) { return typeof o === 'string' ? o : o.label; }).join('\n'),
      max_select: cfg.max_select || '', max_length: cfg.max_length || 1000, multiline: cfg.multiline !== false
    };
  }

  function toSpec(q) {
    var config = {};
    if (q.qtype === 'yes_no') {
      if (String(q.yes_label).trim()) config.yes_label = String(q.yes_label).trim();
      if (String(q.no_label).trim()) config.no_label = String(q.no_label).trim();
    } else if (q.qtype === 'single_choice' || q.qtype === 'multi_choice') {
      config.options = String(q.options_text).split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
      if (q.qtype === 'multi_choice' && String(q.max_select).trim()) config.max_select = parseInt(q.max_select, 10);
    } else if (q.qtype === 'text') {
      config.max_length = parseInt(q.max_length, 10) || 1000;
      config.multiline = !!q.multiline;
    }
    return { qtype: q.qtype, prompt: q.prompt, required: q.required, config: config };
  }

  function field(label, input) {
    return h('div', { 'class': 'field' }, [h('label', {}, [label, input])]);
  }

  function configFields(q) {
    var box = h('div', { 'class': 'oa-config' });
    function bind(el, key, isCheck) {
      el.addEventListener('input', function () { q[key] = isCheck ? el.checked : el.value; });
      return el;
    }
    if (q.qtype === 'yes_no') {
      box.appendChild(field('Label for "Yes" ', bind(h('input', { type: 'text', maxlength: 40, placeholder: 'Yes', value: q.yes_label }), 'yes_label')));
      box.appendChild(field('Label for "No" ', bind(h('input', { type: 'text', maxlength: 40, placeholder: 'No', value: q.no_label }), 'no_label')));
    } else if (q.qtype === 'single_choice' || q.qtype === 'multi_choice') {
      var ta = h('textarea', { rows: 4, placeholder: 'One option per line' });
      ta.value = q.options_text;
      box.appendChild(field('Options (one per line, 2 to 12) ', bind(ta, 'options_text')));
      if (q.qtype === 'multi_choice') {
        box.appendChild(field('Most a person may pick (blank = any number) ',
          bind(h('input', { type: 'number', min: 1, max: 12, value: q.max_select }), 'max_select')));
      }
    } else if (q.qtype === 'text') {
      box.appendChild(field('Longest answer (characters) ',
        bind(h('input', { type: 'number', min: 1, max: 5000, value: q.max_length }), 'max_length')));
      var ml = h('input', { type: 'checkbox' });
      ml.checked = !!q.multiline;
      box.appendChild(h('div', { 'class': 'field oa-check' }, [h('label', {}, [bind(ml, 'multiline', true), ' Allow several lines'])]));
    }
    return box;
  }

  function renderQuestions() {
    questionsBox.textContent = '';
    questions.forEach(function (q, i) {
      var card = h('div', { 'class': 'oa-qcard' });
      var typeSel = select(init.question_types.map(function (t) { return { value: t.key, label: t.label }; }), q.qtype,
        function (v) { q.qtype = v; renderQuestions(); }, 'Question type');
      card.appendChild(h('div', { 'class': 'oa-qhead' }, [
        h('strong', { text: 'Question ' + (i + 1) }), typeSel,
        h('span', { 'class': 'oa-spacer' }),
        h('button', { type: 'button', 'class': 'btn btn-sm btn-secondary', text: 'Up', disabled: i === 0,
          'aria-label': 'Move question up', onclick: function () { move(i, -1); } }),
        h('button', { type: 'button', 'class': 'btn btn-sm btn-secondary', text: 'Down', disabled: i === questions.length - 1,
          'aria-label': 'Move question down', onclick: function () { move(i, 1); } }),
        h('button', { type: 'button', 'class': 'btn btn-sm btn-secondary', text: 'Remove', disabled: questions.length < 2,
          'aria-label': 'Remove question', onclick: function () { questions.splice(i, 1); renderQuestions(); } })
      ]));
      var prompt = h('textarea', { rows: 2, maxlength: 500, placeholder: 'The question people will see' });
      prompt.value = q.prompt;
      prompt.addEventListener('input', function () { q.prompt = prompt.value; });
      card.appendChild(field('Question ', prompt));
      var req = h('input', { type: 'checkbox' });
      req.checked = q.required;
      req.addEventListener('change', function () { q.required = req.checked; });
      card.appendChild(h('div', { 'class': 'field oa-check' }, [h('label', {}, [req, ' An answer is required'])]));
      card.appendChild(configFields(q));
      questionsBox.appendChild(card);
    });
  }

  function move(i, d) {
    var j = i + d;
    if (j < 0 || j >= questions.length) return;
    var t = questions[i]; questions[i] = questions[j]; questions[j] = t;
    renderQuestions();
  }

  document.getElementById('oa-add-question').addEventListener('click', function () {
    questions.push(normalizeQuestion({ qtype: 'yes_no', prompt: '', required: true, config: {} }));
    renderQuestions();
  });

  // ---- test-run toggle ----------------------------------------------------
  var testBox = document.getElementById('oa-test-box');
  var testChk = document.getElementById('oa-test-mode');
  var testAddr = document.getElementById('test_address');
  function syncTestBox() {
    testBox.hidden = !testChk.checked;
    // A hidden control still takes part in browser validation: a malformed address typed and then
    // un-ticked would silently block Save. A disabled one is neither validated nor submitted.
    testAddr.disabled = !testChk.checked;
  }
  testChk.addEventListener('change', syncTestBox);
  syncTestBox();

  // ---- submit -------------------------------------------------------------
  form.addEventListener('submit', function (e) {
    document.getElementById('oa-selectors-json').value = JSON.stringify(selectors);
    document.getElementById('oa-questions-json').value = JSON.stringify(questions.map(toSpec));
    if (typeof showButtonLoading === 'function') {
      showButtonLoading(e.submitter || form.querySelector('button[type="submit"]'));
    }
  });

  renderAudience();
  renderQuestions();
  changed();
})();
