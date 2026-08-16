// Easy View -- alternate New Check Request entry screen (Phase D,
// Cornerstone Served Parishes Plan.md). CURRENT_ORG_ID is emitted inline by
// new_request_easy.html before this file loads, same convention as the
// classic page's new_request.js.
//
// Deliberately a SEPARATE file, not a shared import -- this codebase's own
// established pattern is one .js per page (my_requests.js, new_request.js,
// etc.), and the two pages' interaction models differ enough (no live
// voucher mirror here) that most of new_request.js's own logic doesn't
// apply. What IS the same is duplicated here on purpose rather than
// factored into a shared module, to avoid touching new_request.js (a live,
// heavily-used production file) at all for this build.
//
// Submission itself still POSTs to the same /new-request route
// (new_request_submit in main.py, unchanged) -- the only difference is the
// hidden ui_variant=easy field in the form, which that route reads to
// decide where to redirect on success (the existing /requests/{number}/view
// page, which shows the completed voucher, instead of straight to My
// Requests). See new_request_easy.html's own comment block for the full
// design writeup.

let vendorDisplayText = '—';
let vendorTomSelect = null;
let uploadedFile = null;
let showingDocument = false;

function escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

function fmtMoney(n) {
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

async function loadProgramAreas() {
  const sel = document.getElementById('programAreaSelect');
  const r = await fetch(`/api/program-areas/${CURRENT_ORG_ID}`);
  const areas = await r.json();
  sel.innerHTML = '<option value="">Select...</option>' +
    areas.map(a => `<option value="${a.id}">${a.title}</option>`).join('');
}

// ---- GL Account picker -- identical behavior to the classic page's own
// initGlAccountSelect (see new_request.js for the fuller history of why
// each piece of this is here: dropdownParent:'body' escapes .gl-lines-
// scroll's clipping, depth-based indentation matches the real dot-count
// hierarchy in sort_order, etc.). Copied rather than shared, per this
// file's own top-of-file note. ----

function glAccountLabel(a) {
  return `${a.account_name} (${a.account_number})`;
}

function glAccountDepth(sortOrder) {
  const s = String(sortOrder || '').trim();
  if (!/^[0-9]+(\.[0-9]+)*$/.test(s)) return 0;
  return s.split('.').length - 1;
}

async function fetchGlAccountOptions(programAreaId, q) {
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
    preload: 'focus',
    dropdownParent: 'body',
    load: function (query, callback) {
      const programAreaId = document.getElementById('programAreaSelect').value;
      fetchGlAccountOptions(programAreaId, query).then(callback).catch(() => callback());
    },
    render: {
      option: function (data, escape) {
        const pad = (data.depth || 0) * 10;
        return `<div style="padding-left:${pad}px">${escape(data.label)}</div>`;
      },
    },
    onItemAdd: function () { scheduleBudgetChecks(); },
    onItemRemove: function () { scheduleBudgetChecks(); },
  });
  return ts;
}

function refreshAllGlAccountOptions() {
  document.querySelectorAll('.glAccount').forEach(sel => {
    if (sel.tomselect) sel.tomselect.destroy();
    sel.innerHTML = '<option value="">Account...</option>';
    initGlAccountSelect(sel);
  });
}

function removeGlLine(btn) {
  const container = document.getElementById('glLines');
  if (container.children.length > 1) {
    const line = btn.closest('.gl-line');
    const sel = line.querySelector('.glAccount');
    if (sel && sel.tomselect) sel.tomselect.destroy();
    line.remove();
    scheduleBudgetChecks();
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
  scheduleBudgetChecks();
}

// ---- New Vendor Onboarding panel -- same mutually-exclusive-with-Tom-
// Select-vendor-picker behavior as the classic page. ----

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

function showNewVendorPanel(show) {
  document.getElementById('usingNewVendor').value = show ? '1' : '0';
  document.getElementById('newVendorPanel').style.display = show ? '' : 'none';
  setStatus('vendorValidation', null);
  setStatus('vendorConfirmed', false);
  if (vendorTomSelect) {
    vendorTomSelect.clear();
    vendorTomSelect.control_input.value = '';
    if (!show) vendorTomSelect.load('');
    const wrapper = document.getElementById('vendorSelect').closest('.ts-wrapper') ||
      document.getElementById('vendorSelect').parentElement.querySelector('.ts-wrapper');
    if (wrapper) wrapper.style.display = show ? 'none' : '';
  }
}

function initVendorSelect() {
  vendorTomSelect = new TomSelect('#vendorSelect', {
    valueField: 'id',
    labelField: 'display_name',
    searchField: 'display_name',
    placeholder: 'Search vendors...',
    preload: 'focus',
    load: function (query, callback) {
      fetch(`/api/vendors/${CURRENT_ORG_ID}?q=${encodeURIComponent(query)}`)
        .then(r => r.json())
        .then(data => callback(data))
        .catch(() => callback());
    },
    onItemAdd: function (value, item) {
      vendorDisplayText = item.textContent.trim();
      setStatus('vendorValidation', null);
      setStatus('vendorConfirmed', true);
    },
    onItemRemove: function () {
      vendorDisplayText = '—';
      setStatus('vendorConfirmed', false);
    },
  });
}

// ---- Ask My Accountant: swaps GL Coding entry for a single Amount field.
// Toggling `required` explicitly, not just `hidden` -- a required field
// inside a hidden section still blocks native form submission. ----

function toggleAskMyAccountant() {
  const checked = document.getElementById('askMyAccountantCheckbox').checked;
  document.getElementById('glCodingSection').hidden = checked;
  document.getElementById('askMyAccountantAmountSection').hidden = !checked;
  document.querySelectorAll('#glLines .glAccount, #glLines .glAmount').forEach(el => {
    el.required = !checked;
  });
  document.getElementById('askMyAccountantAmount').required = checked;
}

// ---- Budget/Overspend live preview -- identical mechanics to the classic
// page (same debounce pattern, same /api/budget-status endpoint), and
// deliberately still rendered INLINE under each GL line (.gl-budget-status)
// rather than moved into the Status & Messages panel -- see
// new_request_easy.css's comment on this judgment call. ----

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

// ---- Status & Messages panel (right pane) ----
// Consolidates the messages the classic page shows inline (upload/
// extraction status, vendor validation, the pre-approved-attachment
// warning) into one place. `setStatus(key, value)` upserts one slot and
// re-renders the whole panel from state -- simpler than patching
// individual DOM nodes in place, and the panel is small enough that a
// full re-render on every change is not worth optimizing away.

const statusState = {
  upload: null,            // { text, kind, caveats } | null
  vendorValidation: null,  // string | null
  vendorConfirmed: false,
  preApprovedWarning: false,
};

function setStatus(key, value) {
  statusState[key] = value;
  renderStatusPanel();
}

function renderStatusPanel() {
  const body = document.getElementById('statusPanelBody');
  const parts = [];

  if (statusState.upload) {
    const kindClass = statusState.upload.kind ? ' ' + statusState.upload.kind : '';
    let html = `<div class="status-msg${kindClass}"><strong>Document upload</strong>${escapeHtml(statusState.upload.text)}`;
    (statusState.upload.caveats || []).forEach(c => {
      html += `<span class="caveat">${escapeHtml(c)}</span>`;
    });
    html += '</div>';
    parts.push(html);
  }

  if (statusState.vendorConfirmed) {
    parts.push('<div class="status-msg success"><strong>Vendor</strong>&#10003; Vendor confirmed and active</div>');
  }
  if (statusState.vendorValidation) {
    parts.push(`<div class="status-msg error"><strong>Vendor</strong>${escapeHtml(statusState.vendorValidation)}</div>`);
  }
  if (statusState.preApprovedWarning) {
    parts.push('<div class="status-msg error"><strong>Pre-Approved Submission</strong>Attach at least one file showing the approval before submitting this way.</div>');
  }

  body.innerHTML = parts.length
    ? parts.join('')
    : '<p class="status-panel-empty">Nothing to report yet — upload a document or fill in the form on the left.</p>';
}

// ---- Upload-to-prefill extraction -- same "here's what I read, please
// verify" pattern as the classic page (each touched field gets
// .auto-filled, removed the moment the user edits it), just reporting its
// result into the Status & Messages panel instead of an inline banner. ----

function markAutoFilled(el) {
  el.classList.add('auto-filled');
  const clear = () => { el.classList.remove('auto-filled'); el.removeEventListener('input', clear); el.removeEventListener('change', clear); };
  el.addEventListener('input', clear);
  el.addEventListener('change', clear);
}

function applyExtractedFields(data, filename) {
  if (data.error) {
    setStatus('upload', { text: data.error, kind: 'error' });
    return;
  }

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

  scheduleBudgetChecks();

  const vendorNote = data.matched_vendor_id ? '' : (data.vendor_name ? ' (no matching vendor found -- click "Add a new vendor" below, already prefilled from this document -- please review)' : '');
  const confidenceNote = data.confidence && data.confidence !== 'high' ? ` [${data.confidence} confidence]` : '';
  setStatus('upload', {
    text: `Filled from "${filename}" -- please review before submitting.${confidenceNote}${vendorNote}`,
    kind: 'success',
    caveats: data.caveats,
  });
}

async function handleAttachmentUpload(fileInput) {
  const files = fileInput.files;
  if (!files || !files.length) return;
  const first = files[0]; // used only for prefill/preview -- ALL selected files still submit as attachments
  uploadedFile = first;
  setDocToggleEnabled(true);
  showDocumentView(); // matches the classic page's own default: show the document right after a fresh upload
  setStatus('upload', { text: 'Reading document...' });
  const body = new FormData();
  body.append('file', first);
  try {
    const r = await fetch('/api/extract-document', { method: 'POST', body });
    const data = await r.json();
    applyExtractedFields(data, first.name);
  } catch {
    setStatus('upload', { text: "Couldn't read this document -- please fill in the form manually.", kind: 'error' });
  }
}

// ---- Document-view toggle -- positioned near Submit (form-header-row),
// not at the top of the right pane like the classic page's own
// #previewToggle. The right pane has no live voucher mirror to toggle
// AWAY from here -- just an empty state vs. the uploaded document. ----

function setDocToggleEnabled(enabled) {
  document.getElementById('viewDocToggleBtn').disabled = !enabled;
}

function showDocumentView() {
  if (!uploadedFile) return;
  const docWrap = document.getElementById('documentPreviewWrap');
  docWrap.innerHTML = '';
  const url = URL.createObjectURL(uploadedFile);
  if (uploadedFile.type === 'application/pdf') {
    const iframe = document.createElement('iframe');
    iframe.src = url;
    iframe.title = 'Uploaded document';
    iframe.className = 'document-preview-frame';
    docWrap.appendChild(iframe);
  } else if (uploadedFile.type.startsWith('image/')) {
    const img = document.createElement('img');
    img.src = url;
    img.alt = 'Uploaded document';
    img.className = 'document-preview-image';
    docWrap.appendChild(img);
  } else {
    docWrap.textContent = "This file type can't be previewed inline.";
  }
  document.getElementById('previewEmptyState').style.display = 'none';
  docWrap.style.display = 'block';
  document.getElementById('viewDocToggleBtn').textContent = 'Hide Document';
  showingDocument = true;
}

function hideDocumentView() {
  document.getElementById('documentPreviewWrap').style.display = 'none';
  document.getElementById('previewEmptyState').style.display = '';
  document.getElementById('viewDocToggleBtn').textContent = 'View Uploaded Document';
  showingDocument = false;
}

function toggleDocumentView() {
  if (!uploadedFile) return;
  if (showingDocument) hideDocumentView(); else showDocumentView();
}

// ---- Vendor selection required at submit time -- same real bug guard as
// the classic page (see new_request.js's own comment): Tom Select's
// backing <select> can be left valueless by the extraction flow above
// without HTML5 `required` ever catching it, since Tom Select keeps that
// element display:none. Server-side re-validates regardless. ----

function vendorSelectionIsValid() {
  const usingNewVendor = document.getElementById('usingNewVendor').value === '1';
  if (usingNewVendor) return true;
  const vendorId = document.getElementById('vendorSelect').value;
  return !!vendorId;
}

document.addEventListener('DOMContentLoaded', () => {
  initVendorSelect();
  loadProgramAreas();
  document.querySelectorAll('.glAccount').forEach(sel => initGlAccountSelect(sel));

  document.getElementById('viewDocToggleBtn').addEventListener('click', toggleDocumentView);

  document.getElementById('reqForm').addEventListener('submit', async (e) => {
    if (!vendorSelectionIsValid()) {
      e.preventDefault();
      setStatus('vendorValidation', 'Please select a vendor from the list, or click "Add a new one" below.');
      document.getElementById('vendorSelect').closest('.field').scrollIntoView({ behavior: 'smooth', block: 'center' });
      return;
    }

    const preApprovedBox = document.getElementById('preApprovedCheckbox');
    if (preApprovedBox && preApprovedBox.checked) {
      const newlyAttached = document.getElementById('attachmentsInput').files.length;
      if (newlyAttached === 0) {
        e.preventDefault();
        setStatus('preApprovedWarning', true);
        document.getElementById('preApprovedRow').scrollIntoView({ behavior: 'smooth', block: 'center' });
        return;
      }
      setStatus('preApprovedWarning', false);
    }

    // Three-tier budget design: pre-flight check for tier-3 (over budget
    // beyond the account's buffer) BEFORE the real submission -- same
    // mechanism as the classic page (main.py's new_request_submit re-runs
    // the authoritative check server-side regardless of this pre-flight).
    const form = e.target;
    const already = form.querySelector('input[name="confirmed_overbudget"]');
    if (already && already.value === '1') return;

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
        hint: cfoRequired.join(' '),
        confirmLabel: 'Submit Anyway',
      });
      if (result === null) return;
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

  document.getElementById('programAreaSelect').addEventListener('change', () => {
    refreshAllGlAccountOptions();
    scheduleBudgetChecks();
  });
  document.getElementById('glLines').addEventListener('input', scheduleBudgetChecks);
  document.getElementById('glLines').addEventListener('change', scheduleBudgetChecks);
  document.getElementById('attachmentsInput').addEventListener('change', (e) => handleAttachmentUpload(e.target));

  document.getElementById('addNewVendorLink').addEventListener('click', (e) => { e.preventDefault(); showNewVendorPanel(true); });
  document.getElementById('cancelNewVendorLink').addEventListener('click', (e) => { e.preventDefault(); showNewVendorPanel(false); });
  document.querySelectorAll('input[name="new_vendor_entity_type"]').forEach(r => r.addEventListener('change', updateNewVendorEntityFieldVisibility));
});
