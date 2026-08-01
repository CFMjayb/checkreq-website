// New Check Request form behavior. CURRENT_ORG_ID is emitted inline by
// new_request.html before this file loads (session-fixed for the page --
// see main.py's session-authoritative org_id design).
//
// Also drives the live check-voucher preview on the right: every field
// listener below writes straight into #voucherPreview's [data-field=...]
// nodes -- no server round-trip except the debounced approval-chain-preview
// fetch, which just exposes what /new-request's POST handler already
// computes via approval_engine.py.

let vendorDisplayText = '—';
let chainDebounceTimer = null;
let vendorTomSelect = null;

async function loadProgramAreas() {
  const sel = document.getElementById('programAreaSelect');
  const r = await fetch(`/api/program-areas/${CURRENT_ORG_ID}`);
  const areas = await r.json();
  sel.innerHTML = '<option value="">Select...</option>' +
    areas.map(a => `<option value="${a.id}">${a.title}</option>`).join('');
}

// ---- GL Account picker (Task 5/6, 2026-07-26 batch) ----
// Was a plain native <select>, repopulated by writing raw <option> HTML
// directly. Jay's request (Task 5): "Same behavior for the Account Number
// [as Vendor]" -- searchable, full list shown on click. Converted to a Tom
// Select per GL line, matching the Vendor field's exact preload: 'focus'
// pattern (see initVendorSelect's comment for why that option actually
// shows the full list on click, not just page load).
//
// Task 6 (indentation): originally depth was "digits after the first dot"
// to match real EDOM data that used one-dot values like "1.11" for what was
// clearly meant to be a THIRD nesting level. Jay has now clarified the real
// intended rule explicitly: "'1' is the furthest to the left, '1.1' is
// indented one level to the right, '1.1.1' is two levels in, and so forth"
// -- genuine dot-count hierarchy. This directly conflicted with the old
// digits-after-decimal rule (under true dot-counting, "1.11" and "1.1" are
// the SAME depth), so the underlying DATA needed correcting too, not just
// this formula -- see migrations/009_fix_ambiguous_sort_order.py, applied
// live before this change shipped. glAccountDepth() below is now a simple,
// literal dot count, matching main.py's/qbo-mcp-server's own ORDER BY logic
// (string_to_array(sort_order,'.') -- always was depth-agnostic and needed
// no change of its own).

function glAccountLabel(a) {
  // "Display Name (Account Number)" per Jay's preference -- was
  // "Account Number - Display Name". Deliberately plain text with no
  // indentation baked in -- indentation is rendered separately (via Tom
  // Select's render.option, see initGlAccountSelect) so the live voucher
  // preview / underlying <select>'s real <option> text (which the printed
  // check-voucher table reads via .selectedIndex/.options[...].text) never
  // shows leading indentation characters.
  return `${a.account_name} (${a.account_number})`;
}

function glAccountDepth(sortOrder) {
  const s = String(sortOrder || '').trim();
  if (!/^[0-9]+(\.[0-9]+)*$/.test(s)) return 0; // malformed -- same regex the server uses; renders unindented
  return s.split('.').length - 1;
}

async function fetchGlAccountOptions(programAreaId, q) {
  // Filtered/ordered by whichever Program Area is currently selected, via
  // checkreq.program_area_gl_accounts -- NOT the raw, unfiltered chart of
  // accounts (found missing entirely, live, 2026-07-25). Until a program
  // area is chosen, there's nothing to filter by -- return no options
  // rather than hitting the API with a blank program_area_id (that query
  // param is required server-side, main.py's api_gl_accounts route would
  // 422 on an empty value).
  if (!programAreaId) return [];
  const r = await fetch(`/api/gl-accounts/${CURRENT_ORG_ID}?program_area_id=${programAreaId}&q=${encodeURIComponent(q || '')}`);
  const accts = await r.json();
  return accts.map(a => ({ id: a.id, label: glAccountLabel(a), depth: glAccountDepth(a.sort_order) }));
}

function initGlAccountSelect(selectEl) {
  const ts = new TomSelect(selectEl, {
    valueField: 'id',
    labelField: 'label',
    searchField: ['label'],
    placeholder: 'Search GL accounts...',
    preload: 'focus', // same "full list on click" behavior as the Vendor field
    // Jay, 2026-07-29: "only one item on the drop down list shows up... it's
    // underneath of the scroll box." .gl-lines-scroll clips any descendant
    // that overflows it, including this dropdown's own popup -- no box
    // height would ever be tall enough to show a real ~14-option list.
    // dropdownParent:'body' reparents the popup to <body> at construction,
    // escaping that clipping ancestor entirely (confirmed live: without
    // this, the dropdown stayed nested under .gl-lines-scroll and was cut
    // off after ~1 row no matter how much taller the container was made).
    dropdownParent: 'body',
    load: function (query, callback) {
      const programAreaId = document.getElementById('programAreaSelect').value;
      fetchGlAccountOptions(programAreaId, query).then(callback).catch(() => callback());
    },
    render: {
      // Indentation is applied ONLY here (a 16px-per-depth-level left pad on
      // the dropdown row) -- the item chip / underlying <select>'s real
      // <option> text (labelField) stays plain, unindented text.
      option: function (data, escape) {
        // 10px/level, not 16 -- the account column is narrow (see new_request.css's
        // .gl-line grid comment), and indentation was eating into already-tight
        // width for long real GL labels.
        const pad = (data.depth || 0) * 10;
        return `<div style="padding-left:${pad}px">${escape(data.label)}</div>`;
      },
    },
    onItemAdd: function () { refreshPreview(); },
    onItemRemove: function () { refreshPreview(); },
  });
  return ts;
}

function refreshAllGlAccountOptions() {
  // Re-populate every existing GL line's account dropdown whenever the
  // Program Area changes -- which accounts are even allowed differs per
  // program area, so a stale selection from a different area must not
  // silently survive the switch. Destroy + reinit each Tom Select instance
  // (rather than trying to clear/reload options in place) -- simplest way
  // to guarantee no stale cached search results/selected value survive the
  // switch; Tom Select's own destroy() restores the underlying <select> to
  // its construction-time markup automatically.
  document.querySelectorAll('.glAccount').forEach(sel => {
    if (sel.tomselect) sel.tomselect.destroy();
    sel.innerHTML = '<option value="">Account...</option>';
    initGlAccountSelect(sel);
  });
}

// ---- New Vendor Onboarding: "Add a new vendor" inline panel ----
// Exactly one of "pick existing vendor" / "add new vendor" is active at a
// time (New Vendor Onboarding Plan.md, Section 2). #usingNewVendor (a
// hidden field) is what new_request_submit's server-side branch actually
// reads -- this JS only toggles visibility and keeps the live voucher
// preview's vendor line in sync with whichever mode is active.

function updateNewVendorEntityFieldVisibility() {
  const checked = document.querySelector('input[name="new_vendor_entity_type"]:checked');
  const entityType = checked ? checked.value : 'individual';
  document.getElementById('newVendorIndividualFields').style.display = entityType === 'individual' ? '' : 'none';
  document.getElementById('newVendorEntityFields').style.display = entityType === 'entity' ? '' : 'none';
}

function computeNewVendorDisplayName() {
  const checked = document.querySelector('input[name="new_vendor_entity_type"]:checked');
  const entityType = checked ? checked.value : 'individual';
  if (entityType === 'entity') {
    return document.getElementById('nvCompanyName').value.trim() || null;
  }
  const first = document.getElementById('nvFirstName').value.trim();
  const last = document.getElementById('nvLastName').value.trim();
  return (first + ' ' + last).trim() || null;
}

function setVendorConfirmedMessage(show) {
  document.getElementById('vendorConfirmedMsg').style.display = show ? '' : 'none';
  document.getElementById('addNewVendorLink').style.display = show ? 'none' : '';
}

function showNewVendorPanel(show) {
  document.getElementById('usingNewVendor').value = show ? '1' : '0';
  document.getElementById('newVendorPanel').style.display = show ? '' : 'none';
  setVendorValidationMessage(''); // whichever mode is now active, the prior error no longer applies
  setVendorConfirmedMessage(false); // no existing vendor is selected once the new-vendor panel is active
  if (vendorTomSelect) {
    // Real bug (Jay, 2026-07-29): clicking "Use an existing vendor instead"
    // after an unmatched-vendor extraction left the dropdown completely
    // unusable -- applyExtractedFields() calls setTextboxValue() to show
    // the extracted name, which sets the visible search text WITHOUT ever
    // triggering Tom Select's own load(), so reopening the dropdown showed
    // ZERO options for that stale text (confirmed live). Always clear()
    // AND force a fresh load('') here, not just on the show=true branch,
    // so switching back to "existing vendor" mode always starts from a
    // real, populated list -- never leftover/empty search state.
    vendorTomSelect.clear();
    vendorTomSelect.control_input.value = '';
    if (!show) vendorTomSelect.load('');
    // Tom Select renders its own wrapper next to the original <select> --
    // hide/show that wrapper so exactly one vendor-picking UI is visible.
    const wrapper = document.getElementById('vendorSelect').closest('.ts-wrapper') ||
      document.getElementById('vendorSelect').parentElement.querySelector('.ts-wrapper');
    if (wrapper) wrapper.style.display = show ? 'none' : '';
  }
  refreshPreview();
}

function initVendorSelect() {
  vendorTomSelect = new TomSelect('#vendorSelect', {
    valueField: 'id',
    labelField: 'display_name',
    searchField: 'display_name',
    placeholder: 'Search vendors...',
    // Task 5 (2026-07-26 batch), Jay's exact request: "The Vendor needs to
    // do the first pull of vendors when you click in the box." preload:
    // 'focus' calls Tom Select's own preload() on first focus, which invokes
    // this load() with an empty query directly (bypassing the normal
    // shouldLoad gate that only fires load() once you've typed a
    // character) -- confirmed against the vendored tom-select.complete.min.js
    // source (v2.6.2) before relying on it: preload()'s implementation is
    // exactly `this.load("")`, and the /api/vendors/{org_id} route already
    // returns a real default list (first 25 by display_name) for an empty
    // `q`, so this "just works" with zero server-side change.
    preload: 'focus',
    load: function (query, callback) {
      fetch(`/api/vendors/${CURRENT_ORG_ID}?q=${encodeURIComponent(query)}`)
        .then(r => r.json())
        .then(data => callback(data))
        .catch(() => callback());
    },
    onItemAdd: function (value, item) {
      vendorDisplayText = item.textContent.trim();
      setVendorValidationMessage('');
      setVendorConfirmedMessage(true);
      refreshPreview();
    },
    onItemRemove: function () {
      vendorDisplayText = '—';
      setVendorConfirmedMessage(false);
      refreshPreview();
    },
  });
}

function removeGlLine(btn) {
  const container = document.getElementById('glLines');
  if (container.children.length > 1) {
    const line = btn.closest('.gl-line');
    const sel = line.querySelector('.glAccount');
    if (sel && sel.tomselect) sel.tomselect.destroy();
    line.remove();
    refreshPreview();
  }
}

function addGlLine() {
  const container = document.getElementById('glLines');
  const div = document.createElement('div');
  div.className = 'gl-line';
  div.innerHTML = `
    <div class="field"><select class="glAccount" name="gl_account_id" required><option value="">Account...</option></select></div>
    <div class="field"><input type="number" step="0.01" class="glAmount" name="gl_amount" placeholder="0.00" required></div>
    <div class="field"><input type="text" class="glMemo" name="gl_memo" placeholder="Optional memo"></div>
    <button type="button" class="remove-line" onclick="removeGlLine(this)">&times;</button>
    <div class="gl-budget-status"></div>`;
  container.appendChild(div);
  initGlAccountSelect(div.querySelector('.glAccount'));
  refreshPreview();
  scheduleBudgetChecks();
}

// ---- Live voucher preview ----

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

function fmtMoney(n) {
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatDisplayDate(isoStr) {
  if (!isoStr) return '—';
  const [y, m, d] = isoStr.split('-').map(Number);
  const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
    'August', 'September', 'October', 'November', 'December'];
  if (!y || !m || !d) return '—';
  return `${months[m - 1]} ${d}, ${y}`;
}

const ONES = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten',
  'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen'];
const TENS = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety'];

function threeDigitsToWords(n) {
  let s = '';
  if (n >= 100) { s += ONES[Math.floor(n / 100)] + ' Hundred '; n %= 100; }
  if (n >= 20) { s += TENS[Math.floor(n / 10)] + ' '; n %= 10; }
  else if (n >= 10) { s += ONES[n] + ' '; n = 0; }
  if (n > 0) { s += ONES[n] + ' '; }
  return s.trim();
}

function amountInWords(amount) {
  amount = Math.round(amount * 100) / 100;
  const dollars = Math.floor(amount);
  const cents = Math.round((amount - dollars) * 100);
  const scales = [['', 1], ['Thousand', 1000], ['Million', 1000000], ['Billion', 1000000000]];
  let remaining = dollars;
  const parts = [];
  for (let i = scales.length - 1; i >= 0; i--) {
    const [name, size] = scales[i];
    const chunk = Math.floor(remaining / size);
    if (chunk > 0) {
      parts.push(threeDigitsToWords(chunk) + (name ? ' ' + name : ''));
      remaining %= size;
    }
  }
  const dollarsWords = parts.join(' ').trim() || 'Zero';
  return `${dollarsWords} and ${String(cents).padStart(2, '0')}/100 Dollars`;
}

function setField(name, value) {
  const el = document.querySelector(`#voucherPreview [data-field="${name}"]`);
  if (el) el.textContent = value;
}

function updateVoucherGlTable() {
  const rows = [...document.querySelectorAll('#glLines .gl-line')];
  const tbody = document.querySelector('#voucherPreview [data-field="gl_lines"]');
  let total = 0;
  tbody.innerHTML = rows.map(row => {
    const acctSel = row.querySelector('.glAccount');
    const acctText = acctSel.selectedIndex > 0 ? acctSel.options[acctSel.selectedIndex].text : '—';
    const amt = parseFloat(row.querySelector('.glAmount').value) || 0;
    const memo = row.querySelector('.glMemo').value;
    total += amt;
    return `<tr><td>${escapeHtml(acctText)}</td><td>${fmtMoney(amt)}</td><td>${escapeHtml(memo)}</td></tr>`;
  }).join('');
  setField('total', fmtMoney(total));
  setField('amount', fmtMoney(total));
  setField('amount_words', amountInWords(total));
  return total;
}

function scheduleChainPreview(programAreaId, total) {
  clearTimeout(chainDebounceTimer);
  chainDebounceTimer = setTimeout(() => updateChainPreview(programAreaId, total), 300);
}

async function updateChainPreview(programAreaId, total) {
  if (!programAreaId || total <= 0) { setField('chain_summary', '—'); return; }
  try {
    const r = await fetch(`/api/approval-chain-preview?program_area_id=${programAreaId}&amount=${total}`);
    const data = await r.json();
    setField('chain_summary', data.summary || '—');
  } catch {
    setField('chain_summary', '—');
  }
}

function refreshPreview() {
  const usingNewVendorEl = document.getElementById('usingNewVendor');
  const usingNewVendor = usingNewVendorEl && usingNewVendorEl.value === '1';
  setField('vendor', usingNewVendor ? (computeNewVendorDisplayName() || '—') : vendorDisplayText);
  setField('date', formatDisplayDate(document.getElementById('payDateInput').value));
  setField('description', document.getElementById('descriptionInput').value || '—');

  const paSel = document.getElementById('programAreaSelect');
  setField('program_area', paSel.selectedIndex > 0 ? paSel.options[paSel.selectedIndex].text : '—');

  const total = updateVoucherGlTable();
  scheduleChainPreview(paSel.value, total);
  scheduleBudgetChecks();
}

// ---- Budget/Overspend live preview (Budget Overspend Tracking Plan.md,
// 2026-07-26, Section 4) ----
// Per GL line, shows "Budget: $X * Spent: $Y" (Y already includes this
// line's own typed amount, per the plan's wording), switching red when
// over budget. Reuses the exact debounce pattern the approval-chain
// preview already uses (scheduleChainPreview/updateChainPreview above) --
// one shared timer, all currently-visible lines re-checked together (no
// per-line timers -- simpler, and a single 350ms debounce already collapses
// rapid typing across multiple fields/lines just fine).

let budgetDebounceTimer = null;

function scheduleBudgetChecks() {
  clearTimeout(budgetDebounceTimer);
  budgetDebounceTimer = setTimeout(updateAllBudgetChecks, 350);
}

async function updateAllBudgetChecks() {
  const programAreaId = document.getElementById('programAreaSelect').value;
  const rows = [...document.querySelectorAll('#glLines .gl-line')];
  await Promise.all(rows.map(async (row) => {
    const statusEl = row.querySelector('.gl-budget-status');
    if (!statusEl) return;
    const glAccountId = row.querySelector('.glAccount').value;
    const amount = parseFloat(row.querySelector('.glAmount').value) || 0;
    if (!programAreaId || !glAccountId || amount <= 0) {
      statusEl.textContent = '';
      statusEl.className = 'gl-budget-status';
      statusEl.title = '';
      return;
    }
    try {
      const r = await fetch(`/api/budget-status?program_area_id=${programAreaId}&gl_account_id=${glAccountId}&amount=${amount}`);
      const d = await r.json();
      if (!d.budget_found) {
        statusEl.textContent = '';
        statusEl.className = 'gl-budget-status';
        statusEl.title = '';
        return;
      }
      // Three-tier design (Approval Workflow Corrections, 2026-07-31): a
      // green check for "checked, within budget" is Jay's own direct
      // request -- distinct from the amber/red warning treatment for the
      // two over-budget tiers, since those genuinely aren't "OK" the same
      // way tier 1 is.
      statusEl.className = `gl-budget-status tier-${d.tier}`;
      if (d.tier === 'ok') {
        statusEl.textContent = `✓ Budget: ${fmtMoney(d.annual_budget)} · Spent: ${fmtMoney(d.projected)}`;
        statusEl.title = 'Budget checked -- within budget.';
      } else if (d.tier === 'buffer_notice') {
        statusEl.textContent = `⚠ Budget: ${fmtMoney(d.annual_budget)} · Spent: ${fmtMoney(d.projected)}`;
        statusEl.title = 'Over budget, but within this account\'s allowed buffer -- will proceed, CFO notified (FYI only).';
      } else {
        statusEl.textContent = `⚠ Budget: ${fmtMoney(d.annual_budget)} · Spent: ${fmtMoney(d.projected)}`;
        statusEl.title = 'Over budget beyond this account\'s allowed buffer -- submitting will ask you to confirm and will require CFO approval.';
      }
    } catch {
      statusEl.textContent = '';
      statusEl.className = 'gl-budget-status';
    }
  }));
}

// ---- Upload-to-prefill extraction ----
// "Here's what I read, please verify" -- never a silent overwrite. Each
// touched field gets .auto-filled (removed the moment the user edits that
// field) so "AI-suggested, not yet reviewed" stays visually distinct from
// "human-reviewed." Extraction failure never blocks manual entry -- the
// form is exactly as usable as it always was if this fails or is skipped.

function markAutoFilled(el) {
  el.classList.add('auto-filled');
  const clear = () => { el.classList.remove('auto-filled'); el.removeEventListener('input', clear); el.removeEventListener('change', clear); };
  el.addEventListener('input', clear);
  el.addEventListener('change', clear);
}

function setUploadStatus(message, kind, caveats) {
  const el = document.getElementById('uploadStatus');
  el.className = 'upload-status' + (kind ? ' ' + kind : '');
  el.innerHTML = escapeHtml(message);
  (caveats || []).forEach(c => {
    const span = document.createElement('span');
    span.className = 'caveat';
    span.textContent = c;
    el.appendChild(span);
  });
}

function applyExtractedFields(data, filename) {
  if (data.error) {
    setUploadStatus(data.error, 'error');
    return;
  }

  // data.date is the INVOICE's own printed date, not the requested pay date
  // of this check request -- those are different things and must not be
  // conflated. payDateInput already defaults to today and is required;
  // silently overwriting it with an old invoice date caused a real, live
  // bug (found by Jay 2026-07-25: a MileIQ invoice from 2025 replaced the
  // correct 2026 request date). Deliberately not auto-filled here.
  if (data.description) {
    const descInput = document.getElementById('descriptionInput');
    descInput.value = data.description;
    markAutoFilled(descInput);
  }
  if (data.amount) {
    const firstAmt = document.querySelector('#glLines .gl-line .glAmount');
    if (firstAmt && !firstAmt.value) {
      firstAmt.value = data.amount.toFixed(2);
      markAutoFilled(firstAmt);
    }
  }
  if (data.vendor_name) {
    if (data.matched_vendor_id && vendorTomSelect) {
      vendorTomSelect.addOption({ id: String(data.matched_vendor_id), display_name: data.vendor_name });
      vendorTomSelect.addItem(String(data.matched_vendor_id));
    } else if (vendorTomSelect) {
      vendorTomSelect.setTextboxValue(data.vendor_name);
      vendorDisplayText = data.vendor_name;
      // Jay, 2026-07-29: "if I decide to add a new vendor... you should
      // already bring over the name... you should be able to read [the
      // Sold By block] from the upload." No existing-vendor match --
      // prefill the "Add a new vendor" panel's own fields now (it isn't
      // open yet; whenever the user clicks "Add a new vendor," these
      // values are already sitting in the form). Defaults to Entity mode
      // (Company Name), not Individual -- an invoice's vendor is almost
      // always a business, and the extraction only ever returns one
      // combined name string, never separate first/last.
      const entityRadio = document.querySelector('input[name="new_vendor_entity_type"][value="entity"]');
      if (entityRadio) { entityRadio.checked = true; updateNewVendorEntityFieldVisibility(); }
      const setIfEmpty = (id, val) => {
        const el = document.getElementById(id);
        if (el && !el.value && val) { el.value = val; markAutoFilled(el); }
      };
      setIfEmpty('nvCompanyName', data.vendor_name);
      setIfEmpty('nvAddr1', data.vendor_address_line1);
      setIfEmpty('nvAddr2', data.vendor_address_line2);
      setIfEmpty('nvCity', data.vendor_city);
      setIfEmpty('nvState', data.vendor_state);
      setIfEmpty('nvZip', data.vendor_zip);
      setIfEmpty('nvPhone', data.vendor_phone);
      setIfEmpty('nvContactEmail', data.vendor_contact_email);
    }
  }

  refreshPreview();

  const vendorNote = data.matched_vendor_id ? '' : (data.vendor_name ? ' (no matching vendor found -- click "Add a new vendor" below, already prefilled from this document -- please review)' : '');
  const confidenceNote = data.confidence && data.confidence !== 'high' ? ` [${data.confidence} confidence]` : '';
  setUploadStatus(`Filled from "${filename}" -- please review before submitting.${confidenceNote}${vendorNote}`, 'success', data.caveats);
}

// ---- Vendor selection required at submit time ----
// Real bug found live 2026-07-25 (Jay): the upload-to-prefill feature can
// leave the underlying vendor <select> with no value at all (see
// applyExtractedFields()'s setTextboxValue() branch above) while
// usingNewVendor is still "0" -- so the form would silently POST with no
// vendor identified whatsoever. HTML5 `required` on vendorSelect doesn't
// catch this: Tom Select keeps the real <select> at display:none, and the
// HTML5 constraint-validation spec explicitly excludes display:none
// elements, regardless of required/value state. This client-side check is
// just a fast, friendly pre-submit guard -- the definitive fix is the
// server-side check in new_request_submit (main.py); this only saves a
// round-trip and gives a clearer inline message than a generic 400 would.

function setVendorValidationMessage(msg) {
  const el = document.getElementById('vendorValidationMsg');
  el.textContent = msg;
  el.style.display = msg ? '' : 'none';
}

function vendorSelectionIsValid() {
  const usingNewVendor = document.getElementById('usingNewVendor').value === '1';
  if (usingNewVendor) return true; // the "Add a new vendor" panel's own required fields cover this case
  const vendorId = document.getElementById('vendorSelect').value;
  return !!vendorId;
}

// ---- Edit prefill ----
// EDIT_DATA is emitted by new_request.html only when this page was reached
// via GET /requests/{request_number}/edit (main.py's edit_request_form) --
// null on a brand-new /new-request. Reconstructs however many GL lines the
// original request had (the form always starts with exactly one blank line
// otherwise) and pre-selects the vendor, either an existing vendor (Tom
// Select addItem, same pattern applyExtractedFields() already uses for a
// matched vendor) or the "Add a new vendor" panel's fields (when the
// original request used a not-yet-onboarded vendor_request).

async function applyEditPrefill() {
  const d = EDIT_DATA;
  if (!d) return;

  const paSel = document.getElementById('programAreaSelect');
  paSel.value = String(d.program_area_id);

  const container = document.getElementById('glLines');
  container.querySelectorAll('.glAccount').forEach(sel => { if (sel.tomselect) sel.tomselect.destroy(); });
  container.innerHTML = '';
  const lines = (d.gl_lines && d.gl_lines.length) ? d.gl_lines : [{ gl_account_id: '', amount: 0, memo: '' }];

  // Fetch the allowed GL accounts for this program area ONCE (not once per
  // line) -- every line under the same program area shares the identical
  // option list, so this avoids N redundant fetches for an N-line request.
  const glOptions = await fetchGlAccountOptions(paSel.value);

  for (const line of lines) {
    const div = document.createElement('div');
    div.className = 'gl-line';
    div.innerHTML = `
      <div class="field"><select class="glAccount" name="gl_account_id" required><option value="">Account...</option></select></div>
      <div class="field"><input type="number" step="0.01" class="glAmount" name="gl_amount" placeholder="0.00" required></div>
      <div class="field"><input type="text" class="glMemo" name="gl_memo" placeholder="Optional memo"></div>
      <button type="button" class="remove-line" onclick="removeGlLine(this)">&times;</button>
      <div class="gl-budget-status"></div>`;
    container.appendChild(div);
    const acctSel = div.querySelector('.glAccount');
    const ts = initGlAccountSelect(acctSel);
    if (glOptions.length) ts.addOption(glOptions);
    if (line.gl_account_id) ts.addItem(String(line.gl_account_id));
    if (line.amount) div.querySelector('.glAmount').value = Number(line.amount).toFixed(2);
    if (line.memo) div.querySelector('.glMemo').value = line.memo;
  }
  scheduleBudgetChecks();

  if (d.vendor) {
    vendorTomSelect.addOption({ id: String(d.vendor.id), display_name: d.vendor.display_name });
    vendorTomSelect.addItem(String(d.vendor.id));
  } else if (d.new_vendor) {
    showNewVendorPanel(true);
    const nv = d.new_vendor;
    const radio = document.querySelector(`input[name="new_vendor_entity_type"][value="${nv.entity_type}"]`);
    if (radio) radio.checked = true;
    updateNewVendorEntityFieldVisibility();
    document.getElementById('nvFirstName').value = nv.first_name || '';
    document.getElementById('nvLastName').value = nv.last_name || '';
    document.getElementById('nvCompanyName').value = nv.company_name || '';
    document.getElementById('nvDbaName').value = nv.dba_name || '';
    document.getElementById('nvAddr1').value = nv.address_line1 || '';
    document.getElementById('nvAddr2').value = nv.address_line2 || '';
    document.getElementById('nvCity').value = nv.city || '';
    document.getElementById('nvState').value = nv.state || '';
    document.getElementById('nvZip').value = nv.zip || '';
    document.getElementById('nvPhone').value = nv.phone || '';
    document.getElementById('nvContactName').value = nv.contact_name || '';
    document.getElementById('nvContactEmail').value = nv.contact_email || '';
  }

  refreshPreview();
}

async function handleAttachmentUpload(fileInput) {
  const files = fileInput.files;
  if (!files || !files.length) return;
  const first = files[0]; // used only for prefill -- ALL selected files still submit as attachments
  showDocumentPreview(first);
  setUploadStatus('Reading document...', '');
  const body = new FormData();
  body.append('file', first);
  try {
    const r = await fetch('/api/extract-document', { method: 'POST', body });
    const data = await r.json();
    applyExtractedFields(data, first.name);
  } catch {
    setUploadStatus("Couldn't read this document -- please fill in the form manually.", 'error');
  }
}

// Jay, 2026-07-29: "you never get the opportunity to look at the [uploaded]
// document... it might be nice to be able to toggle between an uploaded
// document versus the check request itself." Renders client-side via
// URL.createObjectURL -- the file is already sitting in the <input>, no
// server round-trip needed just to look at it.
function showDocumentPreview(file) {
  const toggleBar = document.getElementById('previewToggle');
  const voucherWrap = document.getElementById('voucherPreviewWrap');
  const docWrap = document.getElementById('documentPreviewWrap');
  const url = URL.createObjectURL(file);
  docWrap.innerHTML = '';
  if (file.type === 'application/pdf') {
    const iframe = document.createElement('iframe');
    iframe.src = url;
    iframe.title = 'Uploaded document';
    iframe.className = 'document-preview-frame';
    docWrap.appendChild(iframe);
  } else if (file.type.startsWith('image/')) {
    const img = document.createElement('img');
    img.src = url;
    img.alt = 'Uploaded document';
    img.className = 'document-preview-image';
    docWrap.appendChild(img);
  } else {
    docWrap.textContent = "This file type can't be previewed inline.";
  }
  toggleBar.style.display = 'flex';
  // Default to showing the document itself right after a fresh upload --
  // that's the whole point of the toggle existing at all.
  toggleBar.querySelectorAll('.preview-toggle-btn').forEach(b => b.classList.remove('active'));
  toggleBar.querySelector('[data-view="document"]').classList.add('active');
  voucherWrap.style.display = 'none';
  docWrap.style.display = 'block';
}

function initPreviewToggle() {
  const toggleBar = document.getElementById('previewToggle');
  const voucherWrap = document.getElementById('voucherPreviewWrap');
  const docWrap = document.getElementById('documentPreviewWrap');
  toggleBar.querySelectorAll('.preview-toggle-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      toggleBar.querySelectorAll('.preview-toggle-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const showDoc = btn.dataset.view === 'document';
      voucherWrap.style.display = showDoc ? 'none' : '';
      docWrap.style.display = showDoc ? 'block' : 'none';
    });
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initPreviewToggle();
  initVendorSelect();
  loadProgramAreas().then(() => {
    if (window.EDIT_DATA) {
      applyEditPrefill();
    } else {
      refreshPreview();
    }
  });
  document.querySelectorAll('.glAccount').forEach(sel => initGlAccountSelect(sel));

  document.getElementById('reqForm').addEventListener('submit', async (e) => {
    if (!vendorSelectionIsValid()) {
      e.preventDefault();
      setVendorValidationMessage('Please select a vendor from the list, or click "Add a new one" below.');
      document.getElementById('vendorSelect').closest('.field').scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }

    // Pre-Approved Submission Designation (2026-08-01): client-side check
    // only -- new_request_submit re-validates this server-side regardless
    // (never trust a checkbox alone), same posture as every other gate in
    // this app. Attachments already on the request (edit mode) count too,
    // not just a file freshly picked in this exact submit.
    const preApprovedBox = document.getElementById('preApprovedCheckbox');
    if (preApprovedBox && preApprovedBox.checked) {
      const newlyAttached = document.getElementById('attachmentsInput').files.length;
      const alreadyAttached = window.EXISTING_ATTACHMENT_COUNT || 0;
      if (newlyAttached === 0 && alreadyAttached === 0) {
        e.preventDefault();
        document.getElementById('preApprovedWarning').style.display = 'block';
        document.getElementById('preApprovedRow').scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      document.getElementById('preApprovedWarning').style.display = 'none';
    }

    // Three-tier budget design (Approval Workflow Corrections, 2026-07-31):
    // pre-flight check for tier-3 (over budget beyond the account's
    // buffer) BEFORE the real submission -- Jay's direct request: "the
    // user can be asked if they want to submit this." The real,
    // authoritative check still happens server-side in
    // new_request_submit regardless of what this pre-flight call finds --
    // this is purely so the confirmation is a real dialog, not a raw
    // error page on the actual submit attempt.
    const form = e.target;
    const already = form.querySelector('input[name="confirmed_overbudget"]');
    if (already && already.value === '1') return; // already confirmed -- let this one through

    e.preventDefault();
    let cfoRequired = [];
    try {
      const resp = await fetch('/api/budget-check-submission', { method: 'POST', body: new FormData(form) });
      const data = await resp.json();
      cfoRequired = data.cfo_required || [];
    } catch {
      // A failed pre-flight check isn't fatal -- fall through to the real
      // submission below, which re-runs the identical check server-side.
    }

    if (cfoRequired.length) {
      const result = await showActionModal({
        title: 'Over Budget — Confirm Submission',
        // cfoRequired's own detail strings (main.py's _evaluate_gl_line_budgets)
        // already end with "Submitting will require CFO sign-off..." -- do
        // not append a second, near-duplicate sentence here.
        hint: cfoRequired.join(' '),
        confirmLabel: 'Submit Anyway',
      });
      if (result === null) return; // cancelled -- back to editing
    }

    let hidden = form.querySelector('input[name="confirmed_overbudget"]');
    if (!hidden) {
      hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'confirmed_overbudget';
      form.appendChild(hidden);
    }
    hidden.value = '1';
    form.submit();
  });

  document.getElementById('payDateInput').addEventListener('input', refreshPreview);
  document.getElementById('descriptionInput').addEventListener('input', refreshPreview);
  document.getElementById('programAreaSelect').addEventListener('change', () => {
    refreshAllGlAccountOptions();
    refreshPreview();
  });
  document.getElementById('glLines').addEventListener('input', refreshPreview);
  document.getElementById('glLines').addEventListener('change', refreshPreview);
  document.getElementById('attachmentsInput').addEventListener('change', (e) => handleAttachmentUpload(e.target));

  document.getElementById('addNewVendorLink').addEventListener('click', (e) => { e.preventDefault(); showNewVendorPanel(true); });
  document.getElementById('cancelNewVendorLink').addEventListener('click', (e) => { e.preventDefault(); showNewVendorPanel(false); });
  document.querySelectorAll('input[name="new_vendor_entity_type"]').forEach(r => r.addEventListener('change', () => {
    updateNewVendorEntityFieldVisibility();
    refreshPreview();
  }));
  document.getElementById('newVendorPanel').addEventListener('input', refreshPreview);

  refreshPreview();
});
