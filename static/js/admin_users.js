// admin_users.js -- Users & Roles list + detail pages (2026-08-02 feedback
// batch, Item 13/13-continued/14; entity-first redesign 2026-08-16,
// Cornerstone Served Parishes Plan.md Phase F).
//
// 1. Popup toggle for Roles/Program Areas/Entities "click to reveal" cells
//    on the list page -- same generic pattern my_requests.js already
//    established (.status-pill-btn / .status-popup / outside-click
//    dismissal), reimplemented here rather than shared via <script src>
//    since this codebase has no bundler and each page's JS is its own file.
// 2. Client-side column sort on the list table (Item 13: "there'll need to
//    be some sort here") -- all rows are already server-rendered, so this
//    is a plain re-order of <tr> elements, no round-trip needed.
// 3. Detail page's entity-filter dropdown (Item 14.2, extended 2026-08-16
//    Phase F) -- Roles/Program Areas/Approval Rules rows carry
//    data-org-id; the dropdown shows only the matching rows (or
//    everything, for "All entities"). Picking one specific entity also
//    now (a) shows a diocese-level/parish-level badge for that entity
//    (each <option> carries data-served/data-parish/data-diocese-code from
//    the server) and (b) swaps the Program Areas/Approval Rules cards'
//    content for a plain "doesn't apply here" note whenever that entity
//    isn't Cornerstone-served -- those two features only exist for
//    Cornerstone-served entities. "All entities" keeps the original
//    behavior (every panel's content shown, no single-entity badge).

(function () {
  'use strict';

  // Phase E (2026-08-16): the list table gained a `.scroll-panel` sticky-
  // header/scrollable-body wrapper this session. `.status-popup` is
  // `position:absolute` (base.css), and an `overflow-y:auto` ancestor clips
  // ANY absolutely-positioned descendant that would render outside its box
  // -- the same failure mode Tom Select's GL Account dropdown hit inside a
  // scrolling container (2026-07-29, fixed via `dropdownParent:'body'`).
  // Reparent to <body> + switch to `position:fixed`, computed from the
  // opening button's own rect, only once per popup (idempotent -- clicking
  // the same pill again just repositions, doesn't re-append).
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

    const header = document.getElementById('entityLevelHeader');
    const badge = document.getElementById('entityLevelBadge');
    const paCard = document.getElementById('programAreasCard');
    const arCard = document.getElementById('approvalRulesCard');
    // 2026-08-16, Jay: the Grant-a-Role form's own Entity dropdown should
    // follow whichever entity is picked at the top of the page, instead of
    // requiring the same choice twice. "All entities" up top maps to this
    // form's own 'all' option (its literal value for a cross-entity grant,
    // distinct from the filter's '' meaning "show everything").
    const grantOrgSelect = document.getElementById('org_id');

    function apply() {
      const val = sel.value; // '' = all entities
      if (grantOrgSelect) {
        grantOrgSelect.value = val || 'all';
      }
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

      if (!val) {
        // "All entities" -- unchanged historical behavior: every panel's
        // real content shown, no single-entity diocese/parish-level badge
        // (a mixed set of entities can't honestly be called one or the
        // other).
        if (header) header.hidden = true;
        if (paCard) paCard.hidden = false;
        if (arCard) arCard.hidden = false;
        return;
      }

      const opt = sel.options[sel.selectedIndex];
      const isParish = opt.dataset.parish === '1';
      const isServed = opt.dataset.served === '1';

      if (header && badge) {
        header.hidden = false;
        if (isParish) {
          badge.className = 'badge badge-draft';
          badge.textContent = 'Parish-level entity — served by ' +
            (opt.dataset.dioceseCode || opt.dataset.dioceseName || 'its diocese');
        } else {
          badge.className = 'badge badge-approved';
          badge.textContent = 'Diocese-level entity';
        }
      }

      // Program Areas / Approval Rules only exist for Cornerstone-served
      // entities (Cornerstone Served Parishes Plan.md Phase F). 2026-08-16,
      // Jay: hide each card outright for a non-served entity, rather than
      // swapping its content for an explanatory note.
      if (paCard) paCard.hidden = !isServed;
      if (arCard) arCard.hidden = !isServed;
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
