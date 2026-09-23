// Invoice Intake queue (2026-09-22): a single "Upload File" button opens the
// picker and auto-submits on selection (Jay's feedback item 7 -- no separate
// "Choose file" + "Upload" pair). Rows created as a fast Draft stub show
// "Processing..." immediately (main.py's invoice_intake_upload()); this polls
// /invoice-intake/status every few seconds and patches each row in place once
// its background extraction (_finish_invoice_processing()) completes, per
// Jay's "update the row once it is ready for review."
(function () {
  const uploadBtn = document.getElementById('invoiceUploadBtn');
  const uploadInput = document.getElementById('invoiceUploadInput');
  const uploadForm = document.getElementById('invoiceUploadForm');
  if (uploadBtn && uploadInput && uploadForm) {
    uploadBtn.addEventListener('click', () => uploadInput.click());
    uploadInput.addEventListener('change', () => {
      if (uploadInput.files && uploadInput.files.length) uploadForm.submit();
    });
  }

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtMoney(n) {
    return '$' + Number(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function anyProcessingRows() {
    return !!document.querySelector('#invoiceDraftsTable tr[data-processing="true"]');
  }

  function applyStatus(rows) {
    let stillProcessing = false;
    rows.forEach((r) => {
      const tr = document.getElementById('row-' + r.request_number);
      if (!tr) return;
      if (r.processing) {
        stillProcessing = true;
        return;
      }
      if (tr.dataset.processing !== 'true') return; // already finished, nothing to patch
      tr.dataset.processing = 'false';
      const vendorTd = tr.querySelector('.col-vendor');
      if (vendorTd) {
        vendorTd.removeAttribute('colspan');
        vendorTd.innerHTML = escapeHtml(r.vendor_name);
        const amountTd = document.createElement('td');
        amountTd.className = 'col-amount';
        amountTd.textContent = fmtMoney(r.amount);
        vendorTd.after(amountTd);
      }
      const actionsTd = tr.querySelector('.col-actions');
      if (actionsTd) {
        actionsTd.innerHTML = '<a href="/requests/' + encodeURIComponent(r.request_number) + '/edit">Code &amp; Submit</a>';
      }
    });
    return stillProcessing;
  }

  // Jay, 2026-09-23: "the 1 invoice(s) added to the queue -- processing now
  // did not go away when the processing was completed" -- this banner is a
  // one-time, server-rendered "your upload succeeded" message from the
  // redirect that landed you here; nothing was clearing it once its own
  // "processing now" claim went stale. Cleared here, once, the moment
  // polling first confirms every row is done -- never touched again after
  // that (so a real error banner, or one with no "processing" wording at
  // all, is left alone).
  function clearProcessingBanner() {
    const banner = document.getElementById('uploadBanner');
    if (banner && /processing now/i.test(banner.textContent)) banner.remove();
  }

  let pollTimer = null;
  function poll() {
    fetch('/invoice-intake/status', { credentials: 'same-origin' })
      .then((resp) => (resp.ok ? resp.json() : null))
      .then((data) => {
        if (!data || !data.rows) return;
        const stillProcessing = applyStatus(data.rows);
        if (!stillProcessing) {
          clearProcessingBanner();
          if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
        }
      })
      .catch(() => {});
  }

  if (anyProcessingRows()) {
    poll();
    pollTimer = setInterval(poll, 4000);
  }
})();
