// my_requests.js -- My Requests page interactions (2026-07-26).
//
// 1. Status pill -> popup with status history (Task 4). Jay was explicit:
//    a REAL popup, not an inline expansion, that dismisses on any click
//    outside it. Vanilla JS, no library -- one toggled-visible <div> per
//    row (already rendered server-side by my_requests.html, hidden by
//    default via the `hidden` attribute), plus a single document-level
//    click listener that closes whichever popup is open unless the click
//    landed inside that popup or on the pill button that opened it.
// 2. Cancel confirmation (Task 2) -- a plain confirm() dialog before the
//    Cancel form submits, consistent with how this codebase handles other
//    destructive-ish actions elsewhere (no custom modal needed for this).

document.addEventListener('DOMContentLoaded', () => {
  let openPopup = null;

  function closeOpenPopup() {
    if (openPopup) {
      openPopup.hidden = true;
      openPopup = null;
    }
  }

  document.querySelectorAll('.status-pill-btn').forEach((btn) => {
    btn.addEventListener('click', (evt) => {
      evt.stopPropagation();
      const popup = document.getElementById(btn.dataset.popup);
      if (!popup) return;
      if (popup === openPopup) {
        // Clicking the same pill again toggles it closed.
        closeOpenPopup();
        return;
      }
      closeOpenPopup();
      popup.hidden = false;
      openPopup = popup;
    });
  });

  // Outside-click dismissal -- closes the open popup unless the click was
  // inside the popup itself or on the button that opened it (that case is
  // already handled by the toggle logic above, via stopPropagation).
  document.addEventListener('click', (evt) => {
    if (!openPopup) return;
    if (openPopup.contains(evt.target)) return;
    closeOpenPopup();
  });

  document.querySelectorAll('.cancel-form').forEach((form) => {
    form.addEventListener('submit', (evt) => {
      if (!window.confirm('Cancel this check request? This cannot be undone from here -- ' +
                           'the request will be marked Cancelled and can no longer be edited.')) {
        evt.preventDefault();
      }
    });
  });
});
