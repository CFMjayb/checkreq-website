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
//
// 3. Phase E (2026-08-16): My Requests and Admin -> All Requests both gained
//    a `.scroll-panel` sticky-header/scrollable-body wrapper this session.
//    .status-popup is `position:absolute` (base.css), and an
//    `overflow-y:auto` ancestor clips ANY absolutely-positioned descendant
//    that renders outside its box, regardless of the descendant's own
//    `overflow:visible` -- the exact same failure mode Tom Select's GL
//    Account dropdown hit inside a scrolling container (2026-07-29, fixed
//    there via `dropdownParent:'body'`). Only reposition a popup this way
//    when it's actually inside a `.scroll-panel` -- my_approvals.html also
//    loads this file and has no such wrapper, so its popups keep their
//    original CSS-only behavior untouched.

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
      const scrollPanel = btn.closest('.scroll-panel');
      if (scrollPanel) {
        if (popup.dataset.reparented !== '1') {
          document.body.appendChild(popup);
          popup.dataset.reparented = '1';
          popup.style.position = 'fixed';
          popup.style.right = 'auto';
        }
        const rect = btn.getBoundingClientRect();
        const popupWidth = popup.offsetWidth || 300;
        let left = rect.right - popupWidth;
        if (left < 8) left = 8;
        const maxLeft = window.innerWidth - popupWidth - 8;
        if (left > maxLeft) left = Math.max(8, maxLeft);
        popup.style.left = left + 'px';
        popup.style.top = (rect.bottom + 4) + 'px';
      }
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
