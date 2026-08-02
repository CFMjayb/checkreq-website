/* admin_setup.js — Beacon Admin > Setup Tables (prototype, 2026-08-01).
   Shared by admin_setup_gl_mapping.html and admin_setup_organizations.html;
   each page only wires up the tables it actually contains.

   Editing model, and why it's this and not something simpler:
   the workbook's model is "type into cells, click Save, the whole sheet is
   sent row by row, then read column K on each failing row." That has one
   genuinely good property (per-row error detail — added 2026-07-25 after a
   single status cell hid 17 real failures) and one bad one (every present
   row is PUT back whether or not it changed, so a concurrent nightly-job
   write can be clobbered by a stale sheet). This keeps the good property
   and drops the bad one: only rows the user actually touched are sent, and
   each row's result lands on that row. */

(function () {
  'use strict';

  // ---- dirty tracking -------------------------------------------------

  function fieldValue(el) {
    if (el.type === 'checkbox') return el.checked;
    return el.value;
  }

  function captureBaseline(table) {
    // The value each field held at page load. A field edited and then put
    // back to its original value stops counting as dirty — matching what
    // the user would reasonably expect from "No changes".
    table.querySelectorAll('tr[data-row-id] [data-field]').forEach(function (el) {
      el.dataset.baseline = String(fieldValue(el));
    });
  }

  function rowIsDirty(tr) {
    // Marked-for-removal (2026-08-02 feedback batch, Item 8.4) is dirty
    // regardless of what any individual field says -- the pending change
    // IS the row, not a field on it.
    if (tr.dataset.pendingDelete === '1') return true;
    if (tr.dataset.isNew === '1') return true;
    return Array.prototype.some.call(tr.querySelectorAll('[data-field]'), function (el) {
      return String(fieldValue(el)) !== el.dataset.baseline;
    });
  }

  function rowPayload(tr) {
    var out = { id: tr.dataset.rowId ? Number(tr.dataset.rowId) : null };
    if (tr.dataset.pendingDelete === '1') {
      out._delete = true;
      return out;
    }
    tr.querySelectorAll('[data-field]').forEach(function (el) {
      out[el.dataset.field] = fieldValue(el);
    });
    return out;
  }

  /** Toggle (or un-toggle) a row's pending-delete mark. Nothing hits the
   *  server here -- the actual DELETE only happens inside the batched Save
   *  (see gl_mapping_save's `_delete: true` handling), the direct fix for
   *  Jay hitting the old immediate-delete button by mistake ("I just
   *  deleted one that I didn't wanna delete"). */
  function toggleRowDelete(tr, btn, refreshState) {
    var marking = tr.dataset.pendingDelete !== '1';
    if (marking) {
      if (!confirm('Mark this mapping for removal?\n\nIt only withdraws it from this program area’s picker -- requests already coded to it are untouched. Nothing is deleted until you click Save Changes.')) {
        return;
      }
      tr.dataset.pendingDelete = '1';
      tr.classList.add('row-pending-delete');
      tr.querySelectorAll('input, select').forEach(function (el) { el.disabled = true; });
      btn.textContent = '↩';
      btn.title = 'Undo -- keep this mapping';
      btn.classList.add('is-marked');
    } else {
      delete tr.dataset.pendingDelete;
      tr.classList.remove('row-pending-delete');
      tr.querySelectorAll('input, select').forEach(function (el) { el.disabled = false; });
      btn.textContent = '×';
      btn.title = 'Mark this mapping for removal';
      btn.classList.remove('is-marked');
    }
    refreshState();
  }

  function msgRowFor(tr) {
    var id = tr.dataset.rowId;
    if (!id) return null;
    return tr.parentElement.querySelector('.msg-row[data-msg-for="' + id + '"]');
  }

  function setRowMessage(tr, text, kind) {
    var mr = msgRowFor(tr);
    if (!mr) return;
    var div = mr.querySelector('.row-msg');
    div.textContent = text || '';
    div.className = 'row-msg' + (kind ? ' ' + kind : '');
    mr.hidden = !text;
  }

  function clearRowState(tr) {
    tr.classList.remove('row-dirty', 'row-saved', 'row-failed');
  }

  /**
   * Wires one editable table to one Save button.
   * saveUrl receives {rows:[...]} and must answer
   * {saved, failed, results:[{id, ok, error?, new_id?}]}.
   */
  function wireTable(opts) {
    var table = document.getElementById(opts.tableId);
    var btn = document.getElementById(opts.saveBtnId);
    var state = document.getElementById(opts.stateId);
    if (!table || !btn || !state) return null;

    captureBaseline(table);

    function refreshState() {
      var dirty = 0;
      table.querySelectorAll('tr[data-row-id], tr[data-is-new="1"]').forEach(function (tr) {
        if (!tr.querySelector('[data-field]')) return;
        if (rowIsDirty(tr)) {
          dirty++;
          tr.classList.add('row-dirty');
          tr.classList.remove('row-saved', 'row-failed');
        } else if (tr.classList.contains('row-dirty')) {
          clearRowState(tr);
        }
      });
      btn.disabled = dirty === 0;
      state.className = 'save-state' + (dirty ? ' dirty' : '');
      state.textContent = dirty
        ? dirty + ' unsaved change' + (dirty === 1 ? '' : 's')
        : 'No changes';
      return dirty;
    }

    table.addEventListener('input', function (e) {
      if (e.target.dataset && e.target.dataset.field) refreshState();
    });
    table.addEventListener('change', function (e) {
      if (e.target.dataset && e.target.dataset.field) refreshState();
    });

    // Mark-for-removal toggle (2026-08-02 feedback batch, Item 8.4) -- a
    // delegated listener so it works for both the GL Mapping table's rows
    // and any future table wired through this same function.
    table.addEventListener('click', function (e) {
      var delBtn = e.target.closest('.delete-toggle');
      if (!delBtn) return;
      toggleRowDelete(delBtn.closest('tr'), delBtn, refreshState);
    });

    // Leaving with unsaved edits is the single easiest way to lose work on a
    // screen like this — the workbook at least keeps them in the file.
    window.addEventListener('beforeunload', function (e) {
      if (!btn.disabled) { e.preventDefault(); e.returnValue = ''; }
    });

    btn.addEventListener('click', function () {
      var rows = [];
      var trs = [];
      table.querySelectorAll('tr').forEach(function (tr) {
        if (!tr.querySelector('[data-field]')) return;
        if (!rowIsDirty(tr)) return;
        rows.push(rowPayload(tr));
        trs.push(tr);
      });
      if (!rows.length) return;

      btn.disabled = true;
      state.className = 'save-state';
      state.textContent = 'Saving ' + rows.length + '...';

      fetch(opts.saveUrl, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rows: rows }),
      })
        .then(function (r) { return r.json(); })
        .then(function (data) {
          if (data.error) throw new Error(data.error);
          var byId = {};
          (data.results || []).forEach(function (res, i) {
            // Newly-created rows come back keyed by position (id was null).
            byId[res.id === null || res.id === undefined ? 'new:' + i : res.id] = res;
          });
          var newIdx = 0;
          trs.forEach(function (tr) {
            var key = tr.dataset.rowId ? tr.dataset.rowId : 'new:' + (newIdx++);
            var res = byId[key] || byId[Number(key)];
            clearRowState(tr);
            if (res && res.ok) {
              tr.classList.add('row-saved');
              setRowMessage(tr, '', null);
              if (res.new_id) {
                tr.dataset.rowId = String(res.new_id);
                delete tr.dataset.isNew;
                tr.removeAttribute('data-is-new');
              }
              tr.querySelectorAll('[data-field]').forEach(function (el) {
                el.dataset.baseline = String(fieldValue(el));
              });
            } else {
              tr.classList.add('row-failed');
              setRowMessage(tr, (res && res.error) || 'Save failed.', 'err');
            }
          });
          var failed = data.failed || 0;
          state.className = 'save-state ' + (failed ? 'err' : 'ok');
          state.textContent = failed
            ? data.saved + ' saved, ' + failed + ' failed'
            : 'Saved ' + data.saved;
          refreshState();

          // 2026-08-02 feedback batch, Item 10: once every dirty row saved
          // cleanly, reload so the table reflects the just-saved state (a
          // deleted row actually disappears, a new row lands in its real
          // sorted position, group counts update) and the top banner shows
          // "Changes have been saved." A partial failure deliberately does
          // NOT reload -- the per-row error messages need to stay visible
          // so the user can fix and retry.
          if (!failed && opts.reloadOnSave !== false) {
            var url = new URL(window.location.href);
            url.searchParams.set('saved', '1');
            window.location.href = url.toString();
          }
        })
        .catch(function (err) {
          state.className = 'save-state err';
          state.textContent = 'Save failed: ' + err.message;
          btn.disabled = false;
        });
    });

    return { table: table, refreshState: refreshState };
  }

  // ---- GL mapping page ------------------------------------------------

  function initGlMappingPage() {
    var table = document.getElementById('mapTable');
    if (!table) return;

    wireTable({
      tableId: 'mapTable',
      saveBtnId: 'saveBtn',
      stateId: 'saveState',
      saveUrl: '/admin/setup/gl-mapping/save',
    });

    // --- expand/collapse group headers (2026-08-02 feedback batch, Item 8.1) ---
    // Each group's own rows carry data-group-of="group-N" pointing back at
    // its header's data-group="group-N" -- toggled by attribute match, not
    // DOM position, so it stays correct even once row order/backdrop
    // changes (e.g. a future in-place row removal).
    table.addEventListener('click', function (e) {
      var header = e.target.closest('tr.area-header');
      if (!header) return;
      var groupId = header.dataset.group;
      var collapsed = header.classList.toggle('is-collapsed');
      table.querySelectorAll('tr[data-group-of="' + groupId + '"]').forEach(function (tr) {
        // A msg-row stays hidden unless it actually has an error to show --
        // never force it visible just because the group expanded.
        if (tr.classList.contains('msg-row')) {
          if (collapsed) tr.hidden = true;
          else if (!tr.querySelector('.row-msg').textContent) tr.hidden = true;
          else tr.hidden = false;
        } else {
          tr.style.display = collapsed ? 'none' : '';
        }
      });
    });

    // --- on-demand budget lookup ---
    // Reuses main.py's existing /api/budget-status verbatim (the same route
    // the check-request GL Coding panel already calls). Deliberately one
    // account at a time on click, never eagerly for the whole table: each
    // call reaches live QBO through qbo-mcp-server, so 47 of them on page
    // load would be slow and pointless. amount=0 asks the plain question
    // "where does this account stand right now", with no request added.
    table.addEventListener('click', function (e) {
      var btn = e.target.closest('.budget-btn');
      if (!btn) return;
      var tr = btn.closest('tr');
      var cell = tr.querySelector('.budget-cell');
      cell.className = 'budget-cell';
      cell.textContent = 'Checking...';
      var url = '/api/budget-status?program_area_id=' + tr.dataset.areaId +
                '&gl_account_id=' + tr.dataset.accountId + '&amount=0';
      fetch(url)
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d.budget_found) { cell.textContent = 'Not budgeted'; return; }
          cell.textContent = fmtMoney(d.annual_budget) + ' budget · ' +
                             fmtMoney(d.actual_spend) + ' spent';
          if (d.actual_spend > d.annual_budget) cell.classList.add('over');
        })
        .catch(function () { cell.textContent = 'Lookup failed'; });
    });

    // --- add-a-mapping panel ---
    var areaSel = document.getElementById('addArea');
    var acctSel = document.getElementById('addAccount');
    var addBtn = document.getElementById('addBtn');
    var addMsg = document.getElementById('addMsg');
    var acctTs = null;

    function acctLabel(a) { return a.account_name + ' (' + a.account_number + ')'; }

    function rebuildAccountPicker() {
      if (acctTs) { acctTs.destroy(); acctTs = null; }
      acctSel.innerHTML = '<option value=""></option>';
      var areaId = areaSel.value;
      if (!areaId) {
        acctSel.innerHTML = '<option value="">Pick a program area first...</option>';
        return;
      }
      // Same Tom Select shape as the check-request form's own pickers
      // (preload:'focus' so a click shows the full list without typing).
      // The feed is dependent on the chosen program area and already
      // excludes accounts mapped to it, so a duplicate can't be picked.
      acctTs = new TomSelect(acctSel, {
        valueField: 'id',
        labelField: 'label',
        searchField: ['label'],
        placeholder: 'Search this entity\'s chart of accounts...',
        preload: 'focus',
        load: function (query, callback) {
          fetch('/admin/setup/api/unmapped-gl-accounts?program_area_id=' + areaId +
                '&q=' + encodeURIComponent(query || ''))
            .then(function (r) { return r.json(); })
            .then(function (rows) {
              callback(rows.map(function (a) { return { id: a.id, label: acctLabel(a) }; }));
            })
            .catch(function () { callback(); });
        },
      });
    }

    areaSel.addEventListener('change', function () {
      rebuildAccountPicker();
      addMsg.textContent = '';
    });

    addBtn.addEventListener('click', function () {
      addMsg.className = 'row-msg';
      var body = {
        program_area_id: areaSel.value,
        gl_account_id: acctTs ? acctTs.getValue() : '',
        display_text: document.getElementById('addDisplay').value,
        sort_order: document.getElementById('addSort').value,
        overspend_buffer_amount: document.getElementById('addBuffer').value,
        allow_post: document.getElementById('addAllowPost').checked,
      };
      if (!body.program_area_id || !body.gl_account_id) {
        addMsg.className = 'row-msg err';
        addMsg.textContent = 'Pick a program area and a GL account.';
        return;
      }
      addBtn.disabled = true;
      addMsg.textContent = 'Adding...';
      fetch('/admin/setup/gl-mapping/add', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
        .then(function (res) {
          addBtn.disabled = false;
          if (!res.ok || res.d.error) {
            addMsg.className = 'row-msg err';
            addMsg.textContent = res.d.error || 'Could not add that mapping.';
            return;
          }
          // Reload so the new row lands in correct hierarchical position —
          // inserting it client-side would mean reimplementing the server's
          // dot-segment sort in JS, which is exactly the kind of duplicated
          // ordering rule this codebase has been bitten by before.
          window.location.reload();
        })
        .catch(function (err) {
          addBtn.disabled = false;
          addMsg.className = 'row-msg err';
          addMsg.textContent = 'Could not add that mapping: ' + err.message;
        });
    });
  }

  // ---- Organizations / global approvers page --------------------------

  function initOrganizationsPage() {
    if (!document.getElementById('orgTable')) return;

    wireTable({
      tableId: 'orgTable',
      saveBtnId: 'orgSaveBtn',
      stateId: 'orgSaveState',
      saveUrl: '/admin/setup/organizations/save-orgs',
    });

    var appr = wireTable({
      tableId: 'apprTable',
      saveBtnId: 'apprSaveBtn',
      stateId: 'apprSaveState',
      saveUrl: '/admin/setup/organizations/save-approvers',
    });

    var addBtn = document.getElementById('apprAddBtn');
    var tpl = document.getElementById('apprRowTemplate');
    var body = document.getElementById('apprBody');
    if (addBtn && tpl && body && appr) {
      addBtn.addEventListener('click', function () {
        var empty = document.getElementById('apprEmpty');
        if (empty) empty.remove();
        var tr = tpl.content.firstElementChild.cloneNode(true);
        tr.dataset.isNew = '1';
        tr.setAttribute('data-is-new', '1');
        tr.querySelectorAll('[data-field]').forEach(function (el) {
          el.dataset.baseline = ' never';  // always counts as dirty
        });
        body.appendChild(tr);
        appr.refreshState();
        var first = tr.querySelector('input');
        if (first) first.focus();
      });

      body.addEventListener('click', function (e) {
        var drop = e.target.closest('[data-drop-new]');
        if (!drop) return;
        drop.closest('tr').remove();
        appr.refreshState();
      });
    }
  }

  function fmtMoney(n) {
    return '$' + Number(n || 0).toLocaleString('en-US', {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initGlMappingPage();
    initOrganizationsPage();
  });
})();
