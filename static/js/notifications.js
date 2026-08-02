// notifications.js -- header notification bell (In-App Notifications,
// 2026-08-02). Same click-to-reveal popup mechanism my_requests.js /
// admin_users.js already established (.status-pill-btn/.status-popup:
// toggle-open, single document-level click listener that closes an open
// popup unless the click landed inside it) -- the one difference here is
// the list content isn't server-rendered at page load (this bell is on
// EVERY page; querying 20 notifications on every single render for data
// that's usually never opened isn't worth it). It's fetched fresh via
// GET /api/notifications the moment the bell is actually clicked.
//
// Clicking a notification fires POST /api/notifications/{id}/read, then
// navigates to its link_url -- same "mark as read on click" pattern the
// plan specified, no separate dismiss action.

document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('notifBellBtn');
  const popup = document.getElementById('notifPopup');
  const list = document.getElementById('notifList');
  if (!btn || !popup || !list) return;

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString();
  }

  function renderList(items) {
    if (!items.length) {
      list.innerHTML = '<div class="notif-empty">No notifications yet.</div>';
      return;
    }
    list.innerHTML = items.map((n) => (
      '<button type="button" class="notif-item' + (n.read ? '' : ' unread') + '" ' +
      'data-id="' + n.id + '" data-link="' + escapeHtml(n.link_url || '') + '">' +
      '<div class="notif-message">' + escapeHtml(n.message) + '</div>' +
      '<div class="notif-time">' + escapeHtml(fmtTime(n.created_at)) + '</div>' +
      '</button>'
    )).join('');
  }

  async function loadNotifications() {
    list.innerHTML = '<div class="notif-empty">Loading…</div>';
    try {
      const resp = await fetch('/api/notifications', { credentials: 'same-origin' });
      if (!resp.ok) {
        list.innerHTML = '<div class="notif-empty">Couldn’t load notifications.</div>';
        return;
      }
      const data = await resp.json();
      renderList(data.notifications || []);
    } catch (err) {
      list.innerHTML = '<div class="notif-empty">Couldn’t load notifications.</div>';
    }
  }

  btn.addEventListener('click', (evt) => {
    evt.stopPropagation();
    const willOpen = popup.hidden;
    popup.hidden = !willOpen;
    if (willOpen) loadNotifications();
  });

  // Outside-click dismissal -- same convention as every other popup in
  // this codebase (my_requests.js/admin_users.js).
  document.addEventListener('click', (evt) => {
    if (popup.hidden) return;
    if (popup.contains(evt.target) || evt.target === btn) return;
    popup.hidden = true;
  });

  list.addEventListener('click', async (evt) => {
    const item = evt.target.closest('.notif-item');
    if (!item) return;
    const id = item.dataset.id;
    const link = item.dataset.link;
    try {
      await fetch('/api/notifications/' + id + '/read', { method: 'POST', credentials: 'same-origin' });
    } catch (err) {
      // A mark-read hiccup must never block navigation -- proceed anyway.
    }
    if (link) {
      window.location.href = link;
    } else {
      item.classList.remove('unread');
    }
  });
});
