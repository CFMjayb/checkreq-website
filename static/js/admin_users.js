// admin_users.js -- Users & Roles list + detail pages (2026-08-02 feedback
// batch, Item 13/13-continued/14).
//
// 1. Popup toggle for Roles/Program Areas/Entities "click to reveal" cells
//    on the list page -- same generic pattern my_requests.js already
//    established (.status-pill-btn / .status-popup / outside-click
//    dismissal), reimplemented here rather than shared via <script src>
//    since this codebase has no bundler and each page's JS is its own file.
// 2. Client-side column sort on the list table (Item 13: "there'll need to
//    be some sort here") -- all rows are already server-rendered, so this
//    is a plain re-order of <tr> elements, no round-trip needed.
// 3. Detail page's entity-filter dropdown (Item 14.2) -- Roles/Program
//    Areas rows carry data-org-id; the dropdown shows only the matching
//    rows (or everything, for "All entities").

(function () {
  'use strict';

  function wirePopups() {
    let openPopup = null;
    function closeOpenPopup() {
      if (openPopup) { openPopup.hidden = true; openPopup = null; }
    }
    document.querySelectorAll('.status-pill-btn').forEach(function (btn) {
      btn.addEventListener('click', function (evt) {
        evt.stopPropagation();
        const popup = document.getElementById(btn.dataset.popup);
        if (!popup) return;
        if (popup === openPopup) { closeOpenPopup(); return; }
        closeOpenPopup();
        popup.hidden = false;
        openPopup = popup;
      });
    });
    document.addEventListener('click', function (evt) {
      if (!openPopup) return;
      if (openPopup.contains(evt.target)) return;
      closeOpenPopup();
    });
  }

  // ---- sortable list table ---------------------------------------------
  function wireSort() {
    const table = document.getElementById('usersTable');
    if (!table) return;
    const tbody = table.tBodies[0];
    const headers = table.querySelectorAll('th[data-sort]');
    let currentSort = null;
    let ascending = true;

    function cellSortValue(tr, key) {
      const cell = tr.querySelector('[data-sort-value="' + key + '"]');
      if (!cell) return '';
      return (cell.dataset.value != null ? cell.dataset.value : cell.textContent).trim().toLowerCase();
    }

    headers.forEach(function (th) {
      th.addEventListener('click', function () {
        const key = th.dataset.sort;
        ascending = (currentSort === key) ? !ascending : true;
        currentSort = key;
        headers.forEach(function (h) { h.classList.remove('sort-asc', 'sort-desc'); });
        th.classList.add(ascending ? 'sort-asc' : 'sort-desc');

        const rows = Array.prototype.slice.call(tbody.querySelectorAll('tr[data-row]'));
        rows.sort(function (a, b) {
          const av = cellSortValue(a, key), bv = cellSortValue(b, key);
          if (av < bv) return ascending ? -1 : 1;
          if (av > bv) return ascending ? 1 : -1;
          return 0;
        });
        rows.forEach(function (tr) { tbody.appendChild(tr); });
      });
    });
  }

  // ---- detail page: entity filter ---------------------------------------
  function wireEntityFilter() {
    const sel = document.getElementById('entityFilter');
    if (!sel) return;
    function apply() {
      const val = sel.value; // '' = all entities
      document.querySelectorAll('[data-org-row]').forEach(function (tr) {
        tr.hidden = !!val && tr.dataset.orgId !== val;
      });
      document.querySelectorAll('[data-org-empty]').forEach(function (el) {
        const scope = el.dataset.orgEmpty;
        const anyVisible = Array.prototype.some.call(
          document.querySelectorAll('[data-org-row][data-scope="' + scope + '"]'),
          function (tr) { return !tr.hidden; }
        );
        el.hidden = anyVisible;
      });
    }
    sel.addEventListener('change', apply);
    apply();
  }

  document.addEventListener('DOMContentLoaded', function () {
    wirePopups();
    wireSort();
    wireEntityFilter();
  });
})();
