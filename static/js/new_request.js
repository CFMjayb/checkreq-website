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

async function fillGlAccountOptions(selectEl) {
  // Filtered/ordered by whichever Program Area is currently selected, via
  // checkreq.program_area_gl_accounts -- NOT the raw, unfiltered chart of
  // accounts. Found live 2026-07-25 (Jay): this was never actually wired
  // up despite the mapping table/display_text/allow_post/sort_order all
  // already existing server-side. Until a program area is chosen, there's
  // nothing to filter by, so just show the placeholder.
  const programAreaId = document.getElementById('programAreaSelect').value;
  if (!programAreaId) {
    selectEl.innerHTML = '<option value="">Account...</option>';
    return;
  }
  const r = await fetch(`/api/gl-accounts/${CURRENT_ORG_ID}?program_area_id=${programAreaId}`);
  const accts = await r.json();
  selectEl.innerHTML = '<option value="">Account...</option>' +
    accts.map(a => `<option value="${a.id}">${a.account_number} - ${a.account_name}</option>`).join('');
}

function refreshAllGlAccountOptions() {
  // Re-populate every existing GL line's account dropdown whenever the
  // Program Area changes -- which accounts are even allowed differs per
  // program area, so a stale selection from a different area must not
  // silently survive the switch.
  document.querySelectorAll('.glAccount').forEach(sel => {
    sel.value = '';
    fillGlAccountOptions(sel);
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

function showNewVendorPanel(show) {
  document.getElementById('usingNewVendor').value = show ? '1' : '0';
  document.getElementById('newVendorPanel').style.display = show ? '' : 'none';
  if (vendorTomSelect) {
    if (show) vendorTomSelect.clear();
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
    load: function (query, callback) {
      fetch(`/api/vendors/${CURRENT_ORG_ID}?q=${encodeURIComponent(query)}`)
        .then(r => r.json())
        .then(data => callback(data))
        .catch(() => callback());
    },
    onItemAdd: function (value, item) {
      vendorDisplayText = item.textContent.trim();
      refreshPreview();
    },
    onItemRemove: function () {
      vendorDisplayText = '—';
      refreshPreview();
    },
  });
}

function removeGlLine(btn) {
  const container = document.getElementById('glLines');
  if (container.children.length > 1) {
    btn.closest('.gl-line').remove();
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
    <button type="button" class="remove-line" onclick="removeGlLine(this)">&times;</button>`;
  container.appendChild(div);
  fillGlAccountOptions(div.querySelector('.glAccount'));
  refreshPreview();
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
    return `<tr><td>${escapeHtml(acctText)}</td><td>${amt.toFixed(2)}</td><td>${escapeHtml(memo)}</td></tr>`;
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
    }
  }

  refreshPreview();

  const vendorNote = data.matched_vendor_id ? '' : (data.vendor_name ? ' (no matching vendor found -- please select one)' : '');
  const confidenceNote = data.confidence && data.confidence !== 'high' ? ` [${data.confidence} confidence]` : '';
  setUploadStatus(`Filled from "${filename}" -- please review before submitting.${confidenceNote}${vendorNote}`, 'success', data.caveats);
}

async function handleAttachmentUpload(fileInput) {
  const files = fileInput.files;
  if (!files || !files.length) return;
  const first = files[0]; // used only for prefill -- ALL selected files still submit as attachments
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

document.addEventListener('DOMContentLoaded', () => {
  loadProgramAreas().then(refreshPreview);
  document.querySelectorAll('.glAccount').forEach(sel => fillGlAccountOptions(sel));
  initVendorSelect();

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
