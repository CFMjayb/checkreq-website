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
// 2026-09-22 (Jay): "checking property location on a property-related
// invoice" -- Research Coding needs whatever specific property/service
// address the document itself named, set either by a live extraction on
// this page (applyExtractedFields) or, for a bulk-ingested Invoice Intake
// Draft, by whatever was found and persisted at intake time
// (seedIntakeStatus). Null when the vendor/document doesn't have one.
let lastExtractedServiceAddress = null;

// Program Area default (2026-09-22, Jay's feedback batch, restated a 4th
// time same day: "you are supposed to select the Program Area that has
// Master in it if they have access to it or the first item in their
// program list"). ALWAYS applies now -- both a brand-new submission
// (loadProgramAreas, below) AND an existing Draft with no program_area_id
// yet (applyEditPrefill -- Invoice Intake's old "leave blank, defaults to
// All" design is retired; every request gets a real, always-editable
// Program Area from the start now, never a blank one). Found via live
// testing against real EDOM data: the real "master" area is titled
// "EDOM Master", not literally "Master" -- an exact-string match missed it
// entirely. Matches the whole word "master" anywhere in the title (word-
// boundaried, so it won't false-positive on something like "Grantmaster")
// to handle each org's own per-diocese naming.
function defaultProgramAreaId(areas) {
  if (!areas || !areas.length) return null;
  const master = areas.find(a => /\bmaster\b/i.test(a.title || ''));
  return String((master || areas[0]).id);
}

let _lastLoadedProgramAreas = [];

async function loadProgramAreas() {
  const sel = document.getElementById('programAreaSelect');
  const r = await fetch(`/api/program-areas/${CURRENT_ORG_ID}`);
  const areas = await r.json();
  _lastLoadedProgramAreas = areas;
  sel.innerHTML = '<option value="">Select...</option>' +
    areas.map(a => `<option value="${a.id}">${a.title}</option>`).join('');
  if (!window.EDIT_DATA) {
    const d = defaultProgramAreaId(areas);
    if (d) sel.value = d;
  }
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
  // accounts (found missing entirely, live, 2026-07-25). Check Requests
  // always have a real programAreaId by the time this is called (the
  // field is `required`); Invoice Intake (2026-08-02) can genuinely have
  // none ("Program Area defaults to All") -- main.py's /api/gl-accounts
  // route now falls back to the plain active chart of accounts in that
  // case, so this always fetches something rather than showing nothing.
  const params = new URLSearchParams({ q: q || '' });
  if (programAreaId) params.set('program_area_id', programAreaId);
  const r = await fetch(`/api/gl-accounts/${CURRENT_ORG_ID}?${params.toString()}`);
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
  // 2026-09-22 (Jay): "used to show to the right of the Vendor header...
  // needs to return there" -- restored as its original inline confirmation
  // next to the field's own label (was folded into the Status & Messages
  // panel during the same-day unification -- see the panel's own comment).
  const inline = document.getElementById('vendorConfirmedInline');
  if (inline) inline.style.display = show ? 'inline' : 'none';
  document.getElementById('addNewVendorLink').style.display = show ? 'none' : '';
}

function showNewVendorPanel(show) {
  document.getElementById('usingNewVendor').value = show ? '1' : '0';
  document.getElementById('newVendorPanel').style.display = show ? '' : 'none';
  setVendorValidationMessage(''); // whichever mode is now active, the prior error no longer applies
  setVendorConfirmedMessage(false); // no existing vendor is selected once the new-vendor panel is active
  setStatus('vendorMatches', null); // any pending "possible matches" suggestion no longer applies either
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
      refreshArtBanner(value);
      refreshPreview();
    },
    onItemRemove: function () {
      vendorDisplayText = '—';
      setVendorConfirmedMessage(false);
      refreshArtBanner(null);
      refreshPreview();
    },
  });
}

// ART/Monkey-See-Monkey-Do status (Invoice Intake, Tier 3, 2026-08-02) --
// used to be its own standalone banner, only rendered on the Invoice
// Intake coding screen. Folded into the shared Status & Messages panel
// (2026-09-22 unification) -- the underlying data is keyed on
// (vendor_id, org_id), not request_type, so there's no real reason a plain
// Check Request submitter shouldn't also see "this vendor has a
// pre-approved workflow" if it applies. Just exposes what
// new_request_submit already computes at submission time, same
// "live-preview" pattern as the approval-chain-preview/budget-status
// calls.
async function refreshArtBanner(vendorId) {
  if (!vendorId) { setStatus('art', null); return; }
  try {
    const r = await fetch(`/api/vendor-preapproval-status?vendor_id=${encodeURIComponent(vendorId)}`);
    const data = await r.json();
    if (!data.has_art) { setStatus('art', null); return; }
    // L5 (Security Assessment 2026-09-19): both values below are
    // admin-controlled (an ART entry's own vendor link + free-text special
    // handling notes, set on the Setup Tables ART screen), not user-typed
    // on this page -- but escapeHtml() every other innerHTML sink in this
    // file already uses is cheap insurance regardless of who can currently
    // set the value, so applied here too rather than left as the one
    // sink that assumed its input was safe.
    let html = `<strong>ART Preapproved</strong> (${escapeHtml(data.vendor_display_name)}) -- skips the approval chain, goes straight to AP Review.`;
    if (data.is_monkey_see_monkey_do) html += ' <em>Monkey-See-Monkey-Do: GL coding below was auto-filled from last month’s invoice -- please review.</em>';
    if (data.special_handling_notes) html += `<br>${escapeHtml(data.special_handling_notes)}`;
    setStatus('art', html);
  } catch (e) {
    setStatus('art', null);
  }
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

// Real bug, live 2026-09-23: this used to also render a GL-lines table into
// #voucherPreview (the live CR-form mirror) -- that mirror was removed from
// the right pane earlier the same session (Standing UI-UX Rules #2), but
// this function's own DOM-write into it was never updated to match, so
// `tbody` was always null and every write into it threw. setField() already
// no-ops safely when its own target is missing (see its own guard) -- this
// function had no equivalent guard. Now it only computes and returns the
// total; nothing here writes to the DOM at all, since there's no longer a
// live mirror to write into.
function updateVoucherGlTable() {
  const askMyAccountantEl = document.getElementById('askMyAccountantCheckbox');
  const askMyAccountant = askMyAccountantEl && askMyAccountantEl.checked;
  let total = 0;
  if (askMyAccountant) {
    total = parseFloat(document.getElementById('askMyAccountantAmount').value) || 0;
  } else {
    document.querySelectorAll('#glLines .gl-line').forEach(row => {
      total += parseFloat(row.querySelector('.glAmount').value) || 0;
    });
  }
  return total;
}

// Ask My Accountant (2026-08-16): swaps GL Coding entry for a single Amount
// field. Toggling `required` explicitly, not just `hidden` -- a required
// field inside a hidden section still blocks native form submission.
function toggleAskMyAccountant() {
  const checked = document.getElementById('askMyAccountantCheckbox').checked;
  document.getElementById('glCodingSection').hidden = checked;
  document.getElementById('askMyAccountantAmountSection').hidden = !checked;
  document.querySelectorAll('#glLines .glAccount, #glLines .glAmount').forEach(el => {
    el.required = !checked;
  });
  document.getElementById('askMyAccountantAmount').required = checked;
  refreshPreview();
}

function scheduleChainPreview(programAreaId, total) {
  clearTimeout(chainDebounceTimer);
  chainDebounceTimer = setTimeout(() => updateChainPreview(programAreaId, total), 300);
}

function setChainSummary(text) {
  // 2026-09-22 (Jay): Approval Chain Preview moved to the left pane -- was
  // setField('chain_summary', ...), targeting a [data-field] node under
  // the now-retired #voucherPreview mirror. Direct id lookup now that it's
  // its own standalone element (see new_request.html).
  const el = document.getElementById('chainSummaryDisplay');
  if (el) el.textContent = text;
}

async function updateChainPreview(programAreaId, total) {
  if (!programAreaId || total <= 0) { setChainSummary('—'); return; }
  try {
    const r = await fetch(`/api/approval-chain-preview?program_area_id=${programAreaId}&amount=${total}`);
    const data = await r.json();
    setChainSummary(data.summary || '—');
  } catch {
    setChainSummary('—');
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

// ---- Status & Messages panel (2026-09-22, Jay's feedback batch) ----
// Consolidates every status message this page shows -- was scattered
// inline (#uploadStatus under the upload row, #vendorConfirmedMsg/
// #vendorValidationMsg under Vendor, the pre-approved-attachment warning,
// the invoice-intake-only ART banner) -- into one panel under GL Coding.
// Ported from Easy View's own already-built version (new_request_easy.js,
// retired the same session -- see new_request_easy_pre_easy_view_
// unification.js) rather than re-derived. setStatus(key, value) upserts
// one slot and re-renders the whole panel from state; the panel is small
// enough that a full re-render on every change isn't worth optimizing away.

const statusState = {
  upload: null,             // { text, kind, caveats } | null
  vendorValidation: null,   // string | null
  vendorMatches: null,      // { candidates, vendorName } | null -- near-miss suggestions from extraction
  art: null,                // pre-built safe HTML string | null -- ART/MSMD vendor note
  preApprovedWarning: false,
  research: null,           // { text, kind } | { source, suggestions, ... } | null -- Research Coding result
};

function setStatus(key, value) {
  statusState[key] = value;
  renderStatusPanel();
}

function setUploadStatus(message, kind, caveats) {
  setStatus('upload', { text: message, kind, caveats });
}

function renderStatusPanel() {
  const body = document.getElementById('statusPanelBody');
  if (!body) return;
  const parts = [];

  if (statusState.upload) {
    const kindClass = statusState.upload.kind ? ' ' + statusState.upload.kind : '';
    let html = `<div class="status-msg${kindClass}"><strong>Document upload</strong>${escapeHtml(statusState.upload.text)}`;
    (statusState.upload.caveats || []).forEach(c => { html += `<span class="caveat">${escapeHtml(c)}</span>`; });
    html += '</div>';
    parts.push(html);
  }
  if (statusState.vendorValidation) {
    parts.push(`<div class="status-msg error"><strong>Vendor</strong>${escapeHtml(statusState.vendorValidation)}</div>`);
  }
  if (statusState.vendorMatches) {
    // 2026-09-22 (Jay): a real invoice failed to match an existing vendor
    // it should have -- widened matching (main.py's _vendor_match_candidates,
    // name-similarity + extracted city/zip corroboration) now surfaces
    // plausible near-misses here instead of silently opening "Add a new
    // vendor" underneath a real existing match. Never auto-applies one --
    // always a one-click human confirm.
    const { candidates, vendorName } = statusState.vendorMatches;
    let html = `<div class="status-msg warning"><strong>Possible vendor matches</strong>No confident match for "${escapeHtml(vendorName)}" -- did you mean:<ul class="status-action-list">`;
    candidates.forEach(c => {
      html += `<li><button type="button" class="btn btn-secondary btn-sm vendor-match-btn" data-vendor-id="${c.id}" data-vendor-name="${escapeHtml(c.display_name)}">${escapeHtml(c.display_name)}</button></li>`;
    });
    html += '</ul>Or use "Add a new vendor" above.</div>';
    parts.push(html);
  }
  if (statusState.art) {
    parts.push(`<div class="status-msg"><strong>Vendor Note</strong>${statusState.art}</div>`);
  }
  if (statusState.preApprovedWarning) {
    parts.push('<div class="status-msg error"><strong>Pre-Approved Submission</strong>Attach at least one file showing the approval before submitting this way.</div>');
  }
  if (statusState.research) {
    const r = statusState.research;
    if (r.suggestions && r.suggestions.length) {
      let html = '<div class="status-msg"><strong>Research Coding</strong>';
      html += r.source === 'qbo_last_bill'
        ? `From this vendor's most recent QBO bill${r.bill_date ? ' (' + escapeHtml(r.bill_date) + ')' : ''}:`
        : `Based on this vendor's prior submissions in Beacon:`;
      html += '<ul class="status-action-list">';
      r.suggestions.forEach(s => {
        html += `<li><button type="button" class="btn btn-secondary btn-sm coding-suggestion-btn" data-gl-account-id="${s.gl_account_id}" data-gl-label="${escapeHtml(s.label)}">${escapeHtml(s.label)}</button>${s.times_used ? ` <span class="sub">(used ${s.times_used}x)</span>` : ''}</li>`;
      });
      html += '</ul></div>';
      parts.push(html);
    } else if (r.text) {
      parts.push(`<div class="status-msg${r.kind ? ' ' + r.kind : ''}"><strong>Research Coding</strong>${escapeHtml(r.text)}</div>`);
    }
  }

  body.innerHTML = parts.length
    ? parts.join('')
    : '<p class="status-panel-empty">Nothing to report yet — upload a document or fill in the form.</p>';
}

function applyVendorMatch(id, name) {
  vendorTomSelect.addOption({ id: String(id), display_name: name });
  vendorTomSelect.addItem(String(id)); // triggers onItemAdd -> setVendorConfirmedMessage/refreshPreview
  setStatus('vendorMatches', null);
}

function applyCodingSuggestion(glAccountId, label) {
  // Fills the first empty GL line, or adds a new one if every line already
  // has an account -- same "please review, fully editable" treatment
  // Monkey-See-Monkey-Do's own existing prefill already uses, never
  // silently final.
  const rows = [...document.querySelectorAll('#glLines .gl-line')];
  let target = rows.find(row => !row.querySelector('.glAccount').value);
  if (!target) {
    addGlLine();
    const updated = [...document.querySelectorAll('#glLines .gl-line')];
    target = updated[updated.length - 1];
  }
  const sel = target.querySelector('.glAccount');
  const ts = sel && sel.tomselect;
  if (ts) {
    if (!ts.options[String(glAccountId)]) ts.addOption({ id: String(glAccountId), label, depth: 0 });
    ts.addItem(String(glAccountId));
  }
  const memo = target.querySelector('.glMemo');
  if (memo && !memo.value) memo.value = 'Suggested by Research Coding -- please review';
  refreshPreview();
}

async function researchCoding() {
  const usingNewVendor = document.getElementById('usingNewVendor').value === '1';
  const vendorId = usingNewVendor ? '' : document.getElementById('vendorSelect').value;
  if (!vendorId) {
    setStatus('research', { text: 'Select an existing vendor first -- Research Coding looks up prior coding for a vendor already on file.', kind: 'error' });
    return;
  }
  setStatus('research', { text: 'Looking up prior coding...' });
  try {
    let url = `/api/vendor-coding-history?vendor_id=${encodeURIComponent(vendorId)}`;
    if (lastExtractedServiceAddress) url += `&service_address=${encodeURIComponent(lastExtractedServiceAddress)}`;
    const r = await fetch(url);
    const data = await r.json();
    if (!data.suggestions || !data.suggestions.length) {
      setStatus('research', { text: `No prior coding found for ${data.vendor_display_name || 'this vendor'}.`, kind: 'warning' });
      return;
    }
    setStatus('research', data);
  } catch {
    setStatus('research', { text: "Couldn't look up prior coding.", kind: 'error' });
  }
}

// ---- Resizable divider between the two panes (2026-09-22, Jay's feedback
// batch): "have a vertical scroll bar between the data entry section on
// the left and the ... check request form on the right... in case the
// space is needed." No such mechanism existed anywhere in this codebase --
// built from scratch. Drags .split-form's flex-basis directly (in px);
// .split-preview keeps flex:1 and absorbs whatever's left automatically.
// "When the window is resized... the left side should not [shrink]... the
// right side of the screen should decrease in size" -- the min-width on
// .split-form (new_request.css) is what actually enforces that; this drag
// handler just clamps the SAME floor when the user drags, so the two
// behaviors can never disagree. Persisted in localStorage -- a plain
// per-browser convenience, not shared state. ----

const SPLIT_MIN_PX = 420;
const SPLIT_MAX_PX = 900;
const SPLIT_RIGHT_PANE_COLLAPSED_PX = 160; // how much the right side keeps visible with no document yet
const SPLIT_WIDTH_KEY = 'beacon_new_request_split_width';

// 2026-09-22 (Jay): "at the start of the screen, I would have the vertical
// scroll bar to the far right, until a file is uploaded. Then I would show
// the uploaded document moving the scroll bar over to the mid screen."
// The right pane has nothing to show until a document exists (its own
// empty-state, see new_request.html) -- called from showDocumentFrame()
// the instant one appears (a fresh upload, or an existing Draft's
// already-archived attachment rendered on load).
function widenPreviewPaneForDocument() {
  const formPane = document.querySelector('.split-form');
  if (!formPane) return;
  const saved = parseInt(localStorage.getItem(SPLIT_WIDTH_KEY) || '', 10);
  // A real saved drag preference wins; otherwise clear the inline override
  // entirely so the CSS default (flex: 0 0 44%, a genuine mid-screen split)
  // takes over, rather than picking another hardcoded number here.
  formPane.style.flexBasis = (saved && saved >= SPLIT_MIN_PX) ? saved + 'px' : '';
}

function initSplitDivider() {
  const divider = document.getElementById('splitDivider');
  const formPane = document.querySelector('.split-form');
  if (!divider || !formPane) return;

  if (window.EXISTING_DOCUMENT_ATTACHMENT) {
    // A document already exists (an existing Draft's own attachment) --
    // renderExistingAttachment() calls widenPreviewPaneForDocument() itself
    // once it actually renders, but seed the same saved-or-default width
    // here too so there's no visible "wide, then snap back" flash before
    // that async render completes.
    widenPreviewPaneForDocument();
  } else {
    // No document yet -- push the divider toward the far right, leaving
    // just enough of the right pane's empty-state visible to be legible.
    const shellWidth = formPane.parentElement.getBoundingClientRect().width;
    formPane.style.flexBasis = Math.max(SPLIT_MIN_PX, shellWidth - SPLIT_RIGHT_PANE_COLLAPSED_PX) + 'px';
  }

  let dragging = false;
  divider.addEventListener('mousedown', (e) => {
    dragging = true;
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const shellRect = formPane.parentElement.getBoundingClientRect();
    const width = Math.max(SPLIT_MIN_PX, Math.min(SPLIT_MAX_PX, e.clientX - shellRect.left));
    formPane.style.flexBasis = width + 'px';
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.userSelect = '';
    try {
      localStorage.setItem(SPLIT_WIDTH_KEY, String(Math.round(formPane.getBoundingClientRect().width)));
    } catch {
      // localStorage can throw in a private/locked-down browser context --
      // the divider still works for this session, it just won't persist.
    }
  });
}

function applyExtractedFields(data, filename) {
  if (data.error) {
    setUploadStatus(data.error, 'error');
    return;
  }
  lastExtractedServiceAddress = data.service_address || null;

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
      setStatus('vendorMatches', null);
    } else if (data.possible_vendor_matches && data.possible_vendor_matches.length && vendorTomSelect) {
      // 2026-09-22 (Jay): a real invoice failed to match an existing vendor
      // it should have -- widened matching now surfaces plausible
      // near-misses instead of silently giving up. Deliberately does NOT
      // open the "Add a new vendor" panel here (unlike the truly-unmatched
      // branch below) -- there's a real, existing vendor that's probably
      // the right one, so defaulting to "add a new vendor" would be worse.
      setStatus('vendorMatches', { candidates: data.possible_vendor_matches, vendorName: data.vendor_name });
    } else if (vendorTomSelect) {
      // Real bug found live 2026-09-10 (Jay): an unmatched vendor name
      // (e.g. "Jane Ford") sat in the Tom Select search box looking
      // exactly like a confirmed selection -- setTextboxValue() only
      // sets the visible text, it never calls addItem(), so the
      // underlying <select> stayed empty. The banner below already said
      // "no matching vendor found -- click Add a new vendor," but that's
      // easy to miss when the vendor box itself looks filled in, and it
      // led directly to a blocked submission with no clear reason why.
      // Fixed: open the "Add a new vendor" panel immediately instead of
      // leaving it collapsed behind a small link -- showNewVendorPanel()
      // also clears the deceptive vendorSelect text as a side effect, so
      // there's no longer a fake-looking selection sitting in the form.
      showNewVendorPanel(true);
      vendorDisplayText = data.vendor_name;
      // Jay, 2026-07-29: "if I decide to add a new vendor... you should
      // already bring over the name... you should be able to read [the
      // Sold By block] from the upload." Prefill the now-open panel's
      // own fields. Defaults to Entity mode (Company Name), not
      // Individual -- an invoice's vendor is almost always a business,
      // and the extraction only ever returns one combined name string,
      // never separate first/last.
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

  // 2026-09-10 (Jay): "if you see account coding on the check request, you
  // should prefill in the gl coding and then backfill the program so you
  // can fully complete the transaction." matched_gl_account_id/
  // matched_program_area_id are only ever set server-side on an exact,
  // unambiguous match (main.py's api_extract_document) -- never guessed.
  // Program Area is set FIRST since the GL Account picker's own option
  // list is scoped to whichever Program Area is selected (see
  // fetchGlAccountOptions) -- but the actual selection below is made via
  // an explicit addOption+addItem on the still-live Tom Select instance
  // (same fallback pattern applyEditPrefill() already uses for an account
  // outside the default 50-row fetch), so it doesn't depend on that
  // picker's own async load ever completing. Never overwrites a value the
  // submitter already picked themselves.
  if (data.matched_gl_account_id) {
    const paSel = document.getElementById('programAreaSelect');
    if (data.matched_program_area_id && !paSel.value) {
      paSel.value = String(data.matched_program_area_id);
      markAutoFilled(paSel);
    }
    const firstAcctSel = document.querySelector('#glLines .gl-line .glAccount');
    const ts = firstAcctSel && firstAcctSel.tomselect;
    if (ts && !ts.items.length) {
      if (!ts.options[String(data.matched_gl_account_id)]) {
        ts.addOption({ id: String(data.matched_gl_account_id), label: data.matched_gl_account_name || String(data.matched_gl_account_id), depth: 0 });
      }
      ts.addItem(String(data.matched_gl_account_id));
    }
  }

  refreshPreview();

  // Bug found live 2026-09-22, testing the new possible-vendor-matches path:
  // this note used to only check matched_vendor_id, so it kept claiming
  // "the Add a new vendor panel below has been opened" even when a
  // possible-matches suggestion was shown instead (that branch deliberately
  // does NOT open the new-vendor panel -- see the vendor_name handling
  // above). Now reflects all three real outcomes.
  let vendorNote = '';
  if (!data.matched_vendor_id && data.vendor_name) {
    vendorNote = (data.possible_vendor_matches && data.possible_vendor_matches.length)
      ? ' (see "Possible vendor matches" below)'
      : ' (no matching vendor found -- the "Add a new vendor" panel below has been opened and prefilled from this document -- please review)';
  }
  const glNote = (data.coded_gl_account && !data.matched_gl_account_id)
    ? ` (this document appears to be coded to GL account "${data.coded_gl_account}", but no matching account was found for this entity -- please code it manually)`
    : '';
  const confidenceNote = data.confidence && data.confidence !== 'high' ? ` [${data.confidence} confidence]` : '';
  setUploadStatus(`Filled from "${filename}" -- please review before submitting.${confidenceNote}${vendorNote}${glNote}`, 'success', data.caveats);
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
  setStatus('vendorValidation', msg || null);
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
  // 2026-09-22 (Jay, restated a 4th time): a Draft with no Program Area yet
  // (e.g. a bulk-ingested Invoice Intake row nothing on the document itself
  // resolved one for) gets the SAME Master-or-first default a brand-new
  // submission already gets, via defaultProgramAreaId() -- never left blank.
  if (d.program_area_id) {
    paSel.value = String(d.program_area_id);
  } else {
    const def = defaultProgramAreaId(_lastLoadedProgramAreas);
    if (def) paSel.value = def;
  }

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
    if (line.gl_account_id) {
      // Real bug found via live testing (2026-08-02): the default fetch
      // above is capped at 50 accounts (see fetchGlAccountOptions/the
      // server route's own LIMIT 50) -- for a program area (or, for
      // Invoice Intake, the full unfiltered chart) with more accounts than
      // that, a previously-saved account outside that window has no
      // matching option registered, so addItem() below silently no-ops
      // and the field renders blank. Guarantee the saved account is always
      // addable using the account_number/account_name edit_data already
      // carries for this exact line, regardless of whether the default
      // fetch happened to include it.
      if (!ts.options[String(line.gl_account_id)] && line.account_name) {
        ts.addOption({ id: String(line.gl_account_id), label: glAccountLabel(line), depth: 0 });
      }
      ts.addItem(String(line.gl_account_id));
    }
    if (line.amount) div.querySelector('.glAmount').value = Number(line.amount).toFixed(2);
    if (line.memo) div.querySelector('.glMemo').value = line.memo;
  }
  scheduleBudgetChecks();

  if (d.vendor) {
    vendorTomSelect.addOption({ id: String(d.vendor.id), display_name: d.vendor.display_name });
    vendorTomSelect.addItem(String(d.vendor.id));
    refreshArtBanner(d.vendor.id);
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

  seedIntakeStatus();
  refreshPreview();
}

// Bulk Invoice Intake (2026-09-22): renders whatever _ingest_invoice_file()
// found and persisted on this Draft at intake time -- the extraction
// outcome and, if no vendor was confidently matched, the same "did you
// mean" candidate list Classic's live upload already knows how to render.
// A confidently-matched vendor needs no separate seeding here -- the
// d.vendor branch above already calls vendorTomSelect.addItem(), which
// triggers onItemAdd -> setVendorConfirmedMessage(true)/refreshArtBanner(),
// the exact same path a live confirm takes. This only fills the gap that
// path doesn't cover: an extraction note, or an unresolved near-miss list,
// for a Draft nobody has looked at since it was ingested.
function seedIntakeStatus() {
  const s = EDIT_DATA && EDIT_DATA.intake_status;
  if (!s) return;
  if (s.upload) setStatus('upload', s.upload);
  if (s.vendor_matches) {
    setStatus('vendorMatches', { candidates: s.vendor_matches.candidates, vendorName: s.vendor_matches.vendor_name });
  }
  if (s.service_address) lastExtractedServiceAddress = s.service_address;
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
    const r = await fetch('/api/extract-document', { method: 'POST', body, headers: window.csrfHeader() });
    const data = await r.json();
    applyExtractedFields(data, first.name);
  } catch {
    setUploadStatus("Couldn't read this document -- please fill in the form manually.", 'error');
  }
}

// Jay, 2026-07-29: "you never get the opportunity to look at the [uploaded]
// document." 2026-09-22 correction: the right pane no longer mirrors the
// CR form at all (see new_request.html's own comment), so there's nothing
// to toggle between anymore -- the document IS the right pane, full stop,
// the moment one exists. Renders client-side via URL.createObjectURL --
// the file is already sitting in the <input>, no server round-trip needed
// just to look at it.
function showDocumentFrame(iframeOrImgEl) {
  const emptyState = document.getElementById('documentPreviewEmpty');
  const docWrap = document.getElementById('documentPreviewWrap');
  docWrap.innerHTML = '';
  docWrap.appendChild(iframeOrImgEl);
  if (emptyState) emptyState.style.display = 'none';
  docWrap.style.display = 'block';
  widenPreviewPaneForDocument();
}

function showDocumentPreview(file) {
  const url = URL.createObjectURL(file);
  let el;
  if (file.type === 'application/pdf') {
    el = document.createElement('iframe');
    el.src = url;
    el.title = 'Uploaded document';
    el.className = 'document-preview-frame';
  } else if (file.type.startsWith('image/')) {
    el = document.createElement('img');
    el.src = url;
    el.alt = 'Uploaded document';
    el.className = 'document-preview-image';
  } else {
    el = document.createElement('p');
    el.textContent = "This file type can't be previewed inline.";
  }
  showDocumentFrame(el);
}

// 2026-09-22 (Jay): "I can't see the invoice... you must have a image
// viewer on the right." Reviewing an EXISTING Draft (bulk Invoice Intake's
// real case -- uploaded by someone else, possibly days ago) has no live
// file input holding the bytes anymore; the only copy is whatever's
// already archived. Fetches it through the same authenticated view route
// the Attachments list already links to (same-origin, so the browser's
// existing session cookie covers it -- no separate token needed).
function renderExistingAttachment() {
  const att = window.EXISTING_DOCUMENT_ATTACHMENT;
  if (!att || !window.EDIT_DATA) return;
  const url = `/requests/${window.EDIT_DATA.editing_request_number}/attachments/${att.id}/view`;
  let el;
  if ((att.content_type || '').startsWith('image/')) {
    el = document.createElement('img');
    el.src = url;
    el.alt = att.original_filename || 'Uploaded document';
    el.className = 'document-preview-image';
  } else {
    // PDF, or anything else the browser's own plugin/viewer can attempt --
    // matches showDocumentPreview()'s own PDF branch.
    el = document.createElement('iframe');
    el.src = url;
    el.title = att.original_filename || 'Uploaded document';
    el.className = 'document-preview-frame';
  }
  showDocumentFrame(el);
}

document.addEventListener('DOMContentLoaded', () => {
  initVendorSelect();
  initSplitDivider();
  renderExistingAttachment();

  // 2026-09-22 (Jay): "just have Upload File, then go to the picker, select
  // the file and begin the upload process" -- one custom-styled button
  // triggers the real (hidden) file input; the input's own change event
  // (wired below/already wired for attachmentsInput) does the rest with no
  // second click. Applies to both upload surfaces on this page.
  const attachmentsUploadBtn = document.getElementById('attachmentsUploadBtn');
  if (attachmentsUploadBtn) {
    attachmentsUploadBtn.addEventListener('click', () => document.getElementById('attachmentsInput').click());
  }
  const attachmentAddBtn = document.getElementById('attachmentAddBtn');
  if (attachmentAddBtn) {
    attachmentAddBtn.addEventListener('click', () => document.getElementById('attachmentAddInput').click());
  }
  const attachmentAddInput = document.getElementById('attachmentAddInput');
  if (attachmentAddInput) {
    attachmentAddInput.addEventListener('change', () => {
      if (attachmentAddInput.files.length) document.getElementById('attachmentAddForm').submit();
    });
  }

  // Event delegation for buttons the Status & Messages panel injects
  // dynamically (possible-vendor-match confirms, Research Coding
  // suggestions) -- the panel is fully re-rendered on every state change,
  // so a direct listener on any one button would be destroyed the next
  // render; delegating to the panel's own stable container avoids that.
  const statusPanelBody = document.getElementById('statusPanelBody');
  if (statusPanelBody) {
    statusPanelBody.addEventListener('click', (e) => {
      const vendorBtn = e.target.closest('.vendor-match-btn');
      if (vendorBtn) { applyVendorMatch(vendorBtn.dataset.vendorId, vendorBtn.dataset.vendorName); return; }
      const codingBtn = e.target.closest('.coding-suggestion-btn');
      if (codingBtn) { applyCodingSuggestion(codingBtn.dataset.glAccountId, codingBtn.dataset.glLabel); return; }
    });
  }
  const researchBtn = document.getElementById('researchCodingBtn');
  if (researchBtn) researchBtn.addEventListener('click', researchCoding);

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
      const attachmentsInputEl = document.getElementById('attachmentsInput');
      const newlyAttached = attachmentsInputEl ? attachmentsInputEl.files.length : 0;
      const alreadyAttached = window.EXISTING_ATTACHMENT_COUNT || 0;
      if (newlyAttached === 0 && alreadyAttached === 0) {
        e.preventDefault();
        setStatus('preApprovedWarning', true);
        document.getElementById('preApprovedRow').scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      setStatus('preApprovedWarning', false);
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
    if (already && already.value === '1') {
      showButtonLoading(e.submitter); // 2026-09-10: about to really submit
      return; // already confirmed -- let this one through
    }

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
    showButtonLoading(e.submitter); // 2026-09-10: about to really submit
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
  // Real bug, live 2026-09-23: unguarded on an Invoice Intake edit page,
  // which deliberately never renders the "Upload File to Prefill Form" box
  // (new_request.html line ~49 -- the file was already uploaded at intake
  // time) -- the resulting null.addEventListener() threw and silently
  // aborted every listener registration AFTER this line in the same
  // DOMContentLoaded callback (addNewVendorLink/cancelNewVendorLink/
  // newVendorPanel's listeners, and the initial refreshPreview() call).
  const attachmentsInputForChange = document.getElementById('attachmentsInput');
  if (attachmentsInputForChange) {
    attachmentsInputForChange.addEventListener('change', (e) => handleAttachmentUpload(e.target));
  }

  document.getElementById('addNewVendorLink').addEventListener('click', (e) => { e.preventDefault(); showNewVendorPanel(true); });
  document.getElementById('cancelNewVendorLink').addEventListener('click', (e) => { e.preventDefault(); showNewVendorPanel(false); });
  document.querySelectorAll('input[name="new_vendor_entity_type"]').forEach(r => r.addEventListener('change', () => {
    updateNewVendorEntityFieldVisibility();
    refreshPreview();
  }));
  document.getElementById('newVendorPanel').addEventListener('input', refreshPreview);

  refreshPreview();
});
