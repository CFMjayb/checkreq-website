// user_menu.js -- header user-name dropdown (2026-08-08 feedback batch).
// Content is server-rendered (a static list of links/one form), so this is
// only the open/closed toggle -- same click-to-reveal + click-outside-to-
// close mechanism notifications.js already established, minus the fetch.

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('userMenuBtn');
  const popup = document.getElementById('userMenuPopup');
  if (!btn || !popup) return;

  btn.addEventListener('click', (evt) => {
    evt.stopPropagation();
    popup.hidden = !popup.hidden;
  });

  document.addEventListener('click', (evt) => {
    if (popup.hidden) return;
    if (popup.contains(evt.target) || evt.target === btn) return;
    popup.hidden = true;
  });
});
