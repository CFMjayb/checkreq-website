/* The checklist editor (26-162; templates/hr_template_edit.html). No inline handlers (CSP); text is only ever set with
 * textContent, never as HTML.
 *
 * The page holds the whole checklist in memory. Add / Edit / Remove / move change that list on the page only; nothing is
 * written until "Save as new version", which posts the whole list (the server checks every item again and publishes the next
 * version, or says "unchanged"). Removing a SAVED item only marks it (struck through, Undo) until that save.
 */
(function () {
  'use strict';

  var dataEl = document.getElementById('teData');
  if (!dataEl) return;
  var D = JSON.parse(dataEl.textContent);

  var body = document.getElementById('teBody');
  var nameInput = document.getElementById('teName');
  var searchInput = document.getElementById('teSearch');
  var saveBtn = document.getElementById('teSave');
  var addBtn = document.getElementById('teAdd');
  var countEl = document.getElementById('teCount');
  var msgEl = document.getElementById('teMsg');
  var form = document.getElementById('teForm');
  var payloadEl = document.getElementById('tePayload');

  var dlg = document.getElementById('itemDialog');
  var confirmDlg = document.getElementById('confirmDialog');
  var f = {
    title: document.getElementById('itemDialogTitle'), error: document.getElementById('itemError'),
    label: document.getElementById('fLabel'), help: document.getElementById('fHelp'),
    section: document.getElementById('fSection'), owner: document.getElementById('fOwner'),
    basis: document.getElementById('fBasis'), offset: document.getElementById('fOffset'), hard: document.getElementById('fHard'),
    biz: document.getElementById('fBizDays'), required: document.getElementById('fRequired'),
    doc: document.getElementById('fDoc'), cond: document.getElementById('fCondition'),
    customWrap: document.getElementById('fCustomWrap'), custom: document.getElementById('fCustom')
  };

  var sectionLabel = {}, basisLabel = {}, presetLabel = {};
  D.sections.forEach(function (s) { sectionLabel[s.key] = s.label; });
  D.bases.forEach(function (b) { basisLabel[b.key] = b.label; });
  D.presets.forEach(function (p) { presetLabel[p.value] = p.label; });

  var nextId = 1;
  var items = [];
  var editing = null;        // the item open in the dialog; null while adding
  var submitting = false;

  // ---- the model --------------------------------------------------------------------------------------------------------
  function txt(v) { return v === null || v === undefined ? '' : String(v); }
  function make(r) {
    return {
      id: nextId++, key: txt(r.item_key), label: txt(r.label), help: txt(r.help_text), section: txt(r.section),
      owner: txt(r.owner_group), basis: txt(r.due_basis), offset: txt(r.due_offset_days === null ? 0 : r.due_offset_days),
      hard: txt(r.hard_deadline_days), biz: !!r.business_days, required: !!r.required, doc: txt(r.doc_type_key),
      cond: txt(r.condition), removed: false
    };
  }
  function whole(v) { var s = txt(v).trim(); return s === '' ? null : Number(s); }
  function signature(it) {
    return JSON.stringify([it.label.trim(), it.help.trim(), it.section, it.owner, it.basis, whole(it.offset) || 0, whole(it.hard),
      !!it.biz, !!it.required, it.doc, it.cond.split(/\s+/).filter(Boolean).join(' ')]);
  }
  var original = {};          // item_key -> signature, as last saved
  var originalOrder = [];
  (D.original || []).forEach(function (r) { var m = make(r); original[m.key] = signature(m); originalOrder.push(m.key); });
  var originalName = D.originalName || '';
  D.items.forEach(function (r) { items.push(make(r)); });

  function active() { return items.filter(function (it) { return !it.removed; }); }
  function isNewItem(it) { return !it.key || !Object.prototype.hasOwnProperty.call(original, it.key); }
  function changes() {
    var act = active();
    var keys = act.filter(function (it) { return !isNewItem(it); }).map(function (it) { return it.key; });
    var added = act.filter(isNewItem).length;
    var edited = act.filter(function (it) { return !isNewItem(it) && original[it.key] !== signature(it); }).length;
    var removed = originalOrder.filter(function (k) { return keys.indexOf(k) === -1; }).length;
    var kept = originalOrder.filter(function (k) { return keys.indexOf(k) !== -1; });
    var moved = kept.join('|') !== keys.join('|');
    var renamed = nameInput.value.trim() !== originalName.trim();
    return { added: added, edited: edited, removed: removed, moved: moved, renamed: renamed,
             any: !!(added || edited || removed || moved || renamed) };
  }

  // ---- words ------------------------------------------------------------------------------------------------------------
  var WORDS = { clergy: 'clergy', lay: 'lay staff', health_eligible: 'health eligible', pension_eligible: 'pension eligible',
                housing_provided: 'housing provided' };
  function tokens(s) { return s.replace(/\(/g, ' ( ').replace(/\)/g, ' ) ').split(/\s+/).filter(Boolean); }
  function condText(c) {
    c = c.split(/\s+/).filter(Boolean).join(' ');
    if (!c) return 'Everyone';
    if (presetLabel[c]) return presetLabel[c];
    return tokens(c).map(function (t) {
      var lt = t.toLowerCase();
      if (WORDS[lt]) return WORDS[lt];
      if (lt.indexOf('org=') === 0) return t.slice(4);
      if (lt.indexOf('enrolled:') === 0) return 'enrolled in ' + lt.slice(9).replace(/_/g, ' ');
      return lt;
    }).join(' ');
  }
  function validateCondition(text) {
    var toks = tokens(text);
    if (!toks.length) return '';
    if (toks.indexOf('(') !== -1 || toks.indexOf(')') !== -1) return 'Brackets are not supported in a rule yet.';
    var expectAtom = true;
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i], lt = t.toLowerCase();
      if (expectAtom) {
        if (lt === 'not') continue;
        if (WORDS[lt]) { expectAtom = false; continue; }
        if (lt.indexOf('org=') === 0) {
          if (!t.slice(4)) return 'org= needs an entity code.';
          expectAtom = false; continue;
        }
        if (lt.indexOf('enrolled:') === 0) {
          if (D.coverages.indexOf(lt.slice(9)) === -1) return "Unknown coverage in '" + t + "'. Use one of: " + D.coverages.join(', ') + '.';
          expectAtom = false; continue;
        }
        return "Unknown word '" + t + "' in the rule.";
      }
      if (lt !== 'and' && lt !== 'or') return "Expected 'and' or 'or' before '" + t + "'.";
      expectAtom = true;
    }
    return expectAtom ? 'A rule cannot end with and / or / not.' : '';
  }
  function dueText(it) {
    var base = basisLabel[it.basis] || it.basis;
    var n = whole(it.offset) || 0, s;
    if (n === 0) s = 'On ' + base;
    else s = Math.abs(n) + (it.biz ? ' business' : '') + (Math.abs(n) === 1 ? ' day ' : ' days ') + (n > 0 ? 'after ' : 'before ') + base;
    var h = whole(it.hard);
    if (h !== null) s += ' · firm +' + h;
    return s;
  }

  // ---- drawing the list -------------------------------------------------------------------------------------------------
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function btn(label, tip, act, disabled) {
    var b = el('button', 'btn btn-sm btn-secondary ff-rowbtn', label);
    b.type = 'button';
    b.dataset.act = act;
    if (tip) b.setAttribute('data-tip', tip);
    if (disabled) b.disabled = true;
    return b;
  }
  function render() {
    var searching = !!searchInput.value.trim();
    body.textContent = '';
    var n = 0;
    items.forEach(function (it, idx) {
      var isNew = isNewItem(it);
      var changed = !isNew && !it.removed && original[it.key] !== signature(it);
      var tr = el('tr', 'ff-row te-row' + (it.removed ? ' is-removed' : (isNew ? ' is-new' : (changed ? ' is-changed' : ''))));
      tr.dataset.id = String(it.id);
      if (!it.removed) n++;
      tr.appendChild(el('td', 'c-num c-muted', it.removed ? '—' : String(n)));
      var tdItem = el('td', 'te-item');
      var line1 = el('div', 'te-label');
      line1.appendChild(document.createTextNode(it.label));
      if (it.removed) line1.appendChild(el('span', 'te-tag te-tag-del', 'will be removed'));
      else if (isNew) line1.appendChild(el('span', 'te-tag te-tag-new', 'new'));
      else if (changed) line1.appendChild(el('span', 'te-tag te-tag-chg', 'changed'));
      tdItem.appendChild(line1);
      tdItem.appendChild(el('div', 'te-help', it.help));
      if (it.label || it.help) tdItem.setAttribute('data-tip', it.help ? it.label + ' — ' + it.help : it.label);
      tr.appendChild(tdItem);
      tr.appendChild(el('td', '', sectionLabel[it.section] || it.section));
      tr.appendChild(el('td', '', it.owner));
      var tdDue = el('td', '', dueText(it));
      tdDue.setAttribute('data-tip', tdDue.textContent);
      tr.appendChild(tdDue);
      var tdCond = el('td', '', condText(it.cond));
      tdCond.setAttribute('data-tip', tdCond.textContent);
      tr.appendChild(tdCond);
      tr.appendChild(el('td', 'c-center', it.required ? '✓' : 'optional'));
      var tdAct = el('td', 'te-actions');
      if (it.removed) {
        tdAct.appendChild(btn('Undo', 'Keep this item', 'undo'));
      } else {
        tdAct.appendChild(btn('▲', searching ? 'Clear the search to move items' : 'Move up', 'up', searching || idx === 0));
        tdAct.appendChild(btn('▼', searching ? 'Clear the search to move items' : 'Move down', 'down', searching || idx === items.length - 1));
        tdAct.appendChild(btn('Edit', '', 'edit'));
        tdAct.appendChild(btn('Remove', isNew ? 'Take this new item out' : 'Mark for removal (nothing is removed until you save)', 'remove'));
      }
      tr.appendChild(tdAct);
      tr.dataset.text = (it.label + ' ' + it.help + ' ' + (sectionLabel[it.section] || '') + ' ' + it.owner + ' ' + condText(it.cond)).toLowerCase();
      body.appendChild(tr);
    });
    applySearch();
    paintFoot();
  }
  function applySearch() {
    var q = searchInput.value.trim().toLowerCase();
    Array.prototype.forEach.call(body.querySelectorAll('tr.te-row'), function (tr) { tr.hidden = !!q && tr.dataset.text.indexOf(q) === -1; });
  }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
  function paintFoot() {
    var c = changes(), act = active().length;
    var shown = body.querySelectorAll('tr.te-row:not([hidden])').length;
    countEl.textContent = plural(act, 'item', 'items') + (searchInput.value.trim() ? ' · ' + shown + ' showing' : '');
    var bits = [];
    if (c.added) bits.push(c.added + ' added');
    if (c.edited) bits.push(c.edited + ' changed');
    if (c.removed) bits.push(c.removed + ' removed');
    if (c.moved) bits.push('order changed');
    if (c.renamed) bits.push('name changed');
    msgEl.textContent = c.any ? 'Not saved yet: ' + bits.join(', ') : 'No changes yet';
    msgEl.className = 'ff-msg' + (c.any ? ' attn' : '');
    saveBtn.disabled = !c.any;
  }
  window.addEventListener('beforeunload', function (e) {
    if (!submitting && changes().any) { e.preventDefault(); e.returnValue = ''; }
  });

  // ---- the row buttons --------------------------------------------------------------------------------------------------
  function byId(id) { for (var i = 0; i < items.length; i++) if (items[i].id === id) return i; return -1; }
  body.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-act]') : null;
    if (!b || b.disabled) return;
    var tr = b.closest('tr');
    var i = byId(parseInt(tr.dataset.id, 10));
    if (i < 0) return;
    var it = items[i], act = b.dataset.act;
    if (act === 'up' && i > 0) { items[i] = items[i - 1]; items[i - 1] = it; render(); keepVisible(it); }
    else if (act === 'down' && i < items.length - 1) { items[i] = items[i + 1]; items[i + 1] = it; render(); keepVisible(it); }
    else if (act === 'edit') openDialog(it);
    else if (act === 'remove') { if (isNewItem(it)) items.splice(i, 1); else it.removed = true; render(); }
    else if (act === 'undo') { it.removed = false; render(); }
  });
  function keepVisible(it) {
    var tr = body.querySelector('tr[data-id="' + it.id + '"]');
    if (tr && tr.scrollIntoView) tr.scrollIntoView({ block: 'nearest' });
  }
  searchInput.addEventListener('input', function () { render(); });
  nameInput.addEventListener('input', paintFoot);

  // ---- the edit pop-up --------------------------------------------------------------------------------------------------
  function fill(select, opts) {
    select.textContent = '';
    opts.forEach(function (o) { var op = document.createElement('option'); op.value = o[0]; op.textContent = o[1]; select.appendChild(op); });
  }
  fill(f.section, D.sections.map(function (s) { return [s.key, s.label]; }));
  fill(f.owner, D.owners.map(function (o) { return [o, o]; }));
  fill(f.basis, D.bases.map(function (b) { return [b.key, b.label.charAt(0).toUpperCase() + b.label.slice(1)]; }));
  fill(f.doc, [['', 'None']].concat(D.docTypes.map(function (d) { return [d.key, d.label + (d.sensitivity && d.sensitivity !== 'standard' ? ' (restricted)' : '')]; })));
  fill(f.cond, D.presets.map(function (p) { return [p.value, p.label]; }).concat([['__custom', 'Custom rule…']]));

  f.cond.addEventListener('change', function () {
    f.customWrap.hidden = f.cond.value !== '__custom';
    if (!f.customWrap.hidden) f.custom.focus();
  });
  function showError(msg) { f.error.textContent = msg; f.error.hidden = !msg; }

  function openDialog(it) {
    editing = it;
    var v = it || { label: '', help: '', section: D.sections[0].key, owner: D.owners[0], basis: D.defaultBasis, offset: '0', hard: '',
                    biz: false, required: true, doc: '', cond: '' };
    f.title.textContent = it ? 'Edit item' : 'Add item';
    showError('');
    f.label.value = v.label; f.help.value = v.help; f.section.value = v.section; f.owner.value = v.owner; f.basis.value = v.basis;
    f.offset.value = v.offset; f.hard.value = v.hard; f.biz.checked = !!v.biz; f.required.checked = !!v.required; f.doc.value = v.doc;
    var c = v.cond.split(/\s+/).filter(Boolean).join(' ');
    if (!c || presetLabel[c] !== undefined) { f.cond.value = c; f.custom.value = ''; }
    else { f.cond.value = '__custom'; f.custom.value = c; }
    f.customWrap.hidden = f.cond.value !== '__custom';
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    f.label.focus();
  }
  function closeDialog() { if (dlg.close) dlg.close(); else dlg.removeAttribute('open'); }
  function intOrBlank(input, what, lo, hi) {
    var s = input.value.trim();
    if (s === '') return { ok: true, v: '' };
    if (!/^-?\d+$/.test(s)) return { ok: false, msg: what + ' must be a whole number.' };
    var n = parseInt(s, 10);
    if (n < lo || n > hi) return { ok: false, msg: what + ' must be between ' + lo + ' and ' + hi + '.' };
    return { ok: true, v: String(n) };
  }
  document.getElementById('itemOk').addEventListener('click', function () {
    var label = f.label.value.trim();
    if (!label) { showError('The wording is required.'); f.label.focus(); return; }
    if (label.length > D.limits.label) { showError('The wording is longer than ' + D.limits.label + ' characters.'); return; }
    if (f.help.value.trim().length > D.limits.help) { showError('The help text is longer than ' + D.limits.help + ' characters.'); return; }
    var off = intOrBlank(f.offset, 'The days', -D.limits.days, D.limits.days);
    if (!off.ok) { showError(off.msg); f.offset.focus(); return; }
    var hard = intOrBlank(f.hard, 'The hard deadline', 0, D.limits.days);
    if (!hard.ok) { showError(hard.msg); f.hard.focus(); return; }
    var cond = f.cond.value === '__custom' ? f.custom.value.split(/\s+/).filter(Boolean).join(' ') : f.cond.value;
    var bad = validateCondition(cond);
    if (bad) { showError(bad); if (f.cond.value === '__custom') f.custom.focus(); return; }
    var vals = { label: label, help: f.help.value.trim(), section: f.section.value, owner: f.owner.value, basis: f.basis.value,
                 offset: off.v === '' ? '0' : off.v, hard: hard.v, biz: f.biz.checked, required: f.required.checked, doc: f.doc.value, cond: cond };
    var target = editing;
    if (target) { Object.keys(vals).forEach(function (k) { target[k] = vals[k]; }); }
    else {
      target = make({ item_key: '', label: '', section: '', owner_group: '', due_basis: '' });
      Object.keys(vals).forEach(function (k) { target[k] = vals[k]; });
      items.push(target);
      searchInput.value = '';
    }
    closeDialog();
    render();
    keepVisible(target);
  });
  document.getElementById('itemCancel').addEventListener('click', closeDialog);
  addBtn.addEventListener('click', function () { openDialog(null); });

  // ---- saving -----------------------------------------------------------------------------------------------------------
  saveBtn.addEventListener('click', function () {
    var c = changes(), act = active();
    if (!nameInput.value.trim()) { nameInput.focus(); msgEl.textContent = 'The checklist needs a name.'; msgEl.className = 'ff-msg attn'; return; }
    if (!act.length) { msgEl.textContent = 'A checklist needs at least one item.'; msgEl.className = 'ff-msg attn'; return; }
    var bits = [];
    if (c.added) bits.push(plural(c.added, 'item', 'items') + ' added');
    if (c.edited) bits.push(plural(c.edited, 'item', 'items') + ' changed');
    if (c.removed) bits.push(plural(c.removed, 'item', 'items') + ' removed');
    if (c.moved) bits.push('the order changed');
    if (c.renamed) bits.push('the name changed');
    document.getElementById('confirmText').textContent = 'This checklist will have ' + plural(act.length, 'item', 'items') + ': ' + bits.join(', ') + '.';
    if (confirmDlg.showModal) confirmDlg.showModal(); else confirmDlg.setAttribute('open', '');
  });
  document.getElementById('confirmCancel').addEventListener('click', function () { if (confirmDlg.close) confirmDlg.close(); else confirmDlg.removeAttribute('open'); });
  document.getElementById('confirmOk').addEventListener('click', function () {
    var rows = active().map(function (it) {
      return { item_key: it.key, label: it.label, help_text: it.help, section: it.section, owner_group: it.owner, due_basis: it.basis,
               due_offset_days: it.offset, business_days: it.biz, hard_deadline_days: it.hard, required: it.required,
               doc_type_key: it.doc, condition: it.cond };
    });
    payloadEl.value = JSON.stringify(rows);
    submitting = true;
    var okBtn = document.getElementById('confirmOk');
    if (window.showButtonLoading) window.showButtonLoading(okBtn);
    form.submit();
  });

  render();
})();
