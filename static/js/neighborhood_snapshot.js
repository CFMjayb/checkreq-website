/* Neighborhood View: switch between Story, Report and All details, pick the comparison (U.S., state, county), print.
   No inline handlers (the page's security rules). The choices are remembered in this browser only. */
(function () {
  var root = document.getElementById('nbRoot');
  if (!root) return;
  document.documentElement.classList.add('nb-js');

  function load(key, fallback) {
    try { return window.localStorage.getItem(key) || fallback; } catch (e) { return fallback; }
  }
  function save(key, value) {
    try { window.localStorage.setItem(key, value); } catch (e) { /* private window: just do not remember */ }
  }

  var tab = load('nb.tab', 'story');
  var cmp = load('nb.cmp', 'us');
  if (tab === 'snapshot') tab = 'report';   /* the old Snapshot tab became Report */
  if (tab !== 'story' && tab !== 'report' && tab !== 'details') tab = 'story';
  if (['us', 'state', 'county'].indexOf(cmp) < 0) cmp = 'us';

  function paint() {
    root.setAttribute('data-tab', tab);
    root.setAttribute('data-cmp', cmp);
    Array.prototype.forEach.call(document.querySelectorAll('.nb-tab'), function (b) {
      var on = b.getAttribute('data-tab') === tab;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    Array.prototype.forEach.call(document.querySelectorAll('.nb-cmp-btn'), function (b) {
      var on = b.getAttribute('data-cmp') === cmp;
      b.classList.toggle('is-active', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  document.addEventListener('click', function (ev) {
    var t = ev.target.closest ? ev.target.closest('.nb-tab, .nb-cmp-btn, #nbPrint') : null;
    if (!t) return;
    if (t.classList.contains('nb-tab')) {
      tab = t.getAttribute('data-tab');
      save('nb.tab', tab);
      paint();
    } else if (t.classList.contains('nb-cmp-btn')) {
      cmp = t.getAttribute('data-cmp');
      save('nb.cmp', cmp);
      paint();
    } else if (t.id === 'nbPrint') {
      window.print();
    }
  });
  paint();
})();
