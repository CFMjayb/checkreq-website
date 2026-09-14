// impersonate.js -- lazy-loads the "Parish-Only Users" section on the
// Impersonate a User admin page (2026-09-14). Entity users are already
// server-rendered on page load (the common case); parish-only users are
// fetched from GET /api/impersonate/parish-users only the first time this
// <details> section is actually expanded, same fetch-on-open deferral
// notifications.js already established for the header bell.

document.addEventListener('DOMContentLoaded', () => {
  const section = document.getElementById('parishUsersSection');
  const hint = document.getElementById('parishUsersHint');
  const body = document.getElementById('parishUsersBody');
  if (!section || !body) return;

  let loaded = false;

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function renderRows(users) {
    if (!users.length) {
      body.innerHTML = '<tr><td colspan="4">No parish-only users found.</td></tr>';
      return;
    }
    body.innerHTML = users.map((u) => (
      '<tr>' +
      '<td>' + escapeHtml(u.display_name || '—') + '</td>' +
      '<td>' + escapeHtml(u.email) + '</td>' +
      '<td>' + u.roles.map((r) => (
        '<span class="badge badge-approved">' + escapeHtml(r.role_label) + ' (' + escapeHtml(r.parish_name) + ')</span>'
      )).join('') + '</td>' +
      '<td class="col-action">' +
      '<form method="post" action="/admin/impersonate/' + u.id + '">' +
      '<button type="submit" class="btn btn-secondary btn-sm">Impersonate</button>' +
      '</form></td>' +
      '</tr>'
    )).join('');
  }

  async function loadParishUsers() {
    if (loaded) return;
    loaded = true;
    if (hint) hint.textContent = 'Loading…';
    try {
      const resp = await fetch('/api/impersonate/parish-users', { credentials: 'same-origin' });
      if (!resp.ok) {
        if (hint) hint.textContent = 'Couldn’t load parish-only users.';
        loaded = false;
        return;
      }
      const data = await resp.json();
      if (hint) hint.hidden = true;
      renderRows(data.users || []);
    } catch (err) {
      if (hint) hint.textContent = 'Couldn’t load parish-only users.';
      loaded = false;
    }
  }

  section.addEventListener('toggle', () => {
    if (section.open) loadParishUsers();
  });
});
