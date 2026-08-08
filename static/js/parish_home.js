// parish_home.js -- Parish Portal S4 (2026-08-08) home page announcements
// feed. Fetches GET /api/announcements/mine on load (the server derives the
// viewer's own parish itself -- nothing here passes a parish_id) and
// renders the results into #announcementsList, same escapeHtml/fetch
// pattern as notifications.js.

document.addEventListener('DOMContentLoaded', () => {
  const section = document.getElementById('announcementsSection');
  const list = document.getElementById('announcementsList');
  if (!section || !list) return;

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleDateString();
  }

  async function load() {
    try {
      const resp = await fetch('/api/announcements/mine', { credentials: 'same-origin' });
      if (!resp.ok) return;
      const data = await resp.json();
      const items = data.announcements || [];
      if (!items.length) return;
      section.hidden = false;
      list.innerHTML = items.map((a) => (
        '<div class="announcement-item">' +
        '<div class="announcement-title">' + escapeHtml(a.title) + '</div>' +
        '<div class="announcement-date">' + escapeHtml(fmtDate(a.publish_at)) + '</div>' +
        '<div class="announcement-body">' + escapeHtml(a.body) + '</div>' +
        '</div>'
      )).join('');
    } catch (err) {
      // A feed hiccup should never break the rest of the home page.
    }
  }

  load();
});
