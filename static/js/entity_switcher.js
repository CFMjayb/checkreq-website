// M8 (Security Assessment 2026-09-19): /select-entity/{org_id} is now POST
// (was GET, reachable cross-site under SameSite=Lax -- a top-level GET
// navigation still carries the cookie). This used to just set
// window.location.href directly; now it points the surrounding form
// (base.html's #entitySwitcherForm, which already carries a hidden
// csrf_token field) at the chosen org's own POST URL and submits it -- the
// exact same full-page navigation to the same target, just via POST.
document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('entitySwitcher');
  const form = document.getElementById('entitySwitcherForm');
  if (!sel || !form) return;
  sel.addEventListener('change', () => {
    form.action = `/select-entity/${sel.value}`;
    form.submit();
  });
});
