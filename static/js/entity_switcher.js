document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('entitySwitcher');
  if (!sel) return;
  sel.addEventListener('change', () => {
    const next = encodeURIComponent(window.location.pathname);
    window.location.href = `/select-entity/${sel.value}?next=${next}`;
  });
});
