/* The email PDF button (26-158, 2026-10-11). No inline handlers (CSP).
 *
 * Any button with data-pdf-pop and data-pdf-src="<viewer address>":
 *   - hold the pointer over it a moment and a small preview of the email's PDF appears beside it;
 *     the preview stays while the pointer is on it or on the button;
 *   - click it for a large view in a dialog (with "Open in a new tab").
 * The address is Beacon's own viewer (HR Admin only, same origin), so the file is shown in a plain iframe.
 */
(function () {
  'use strict';
  var pop = null, showTimer = null, hideTimer = null, owner = null;

  function frameSrc(url) { return url + '#toolbar=0&navpanes=0&view=FitH'; }

  function hidePop() {
    clearTimeout(showTimer); clearTimeout(hideTimer);
    if (pop) { pop.remove(); pop = null; }
    owner = null;
  }
  function scheduleHide() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hidePop, 220);
  }
  function showPop(btn) {
    var url = btn.getAttribute('data-pdf-src');
    if (!url) return;
    pop = document.createElement('div');
    pop.className = 'te-pdf-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', 'Email PDF preview');
    var cap = document.createElement('div');
    cap.className = 'te-pdf-cap';
    cap.textContent = 'Email copy (click the PDF button for a larger view)';
    var fr = document.createElement('iframe');
    fr.setAttribute('title', 'Email PDF preview');
    fr.src = frameSrc(url);
    pop.appendChild(cap);
    pop.appendChild(fr);
    document.body.appendChild(pop);
    pop.addEventListener('mouseenter', function () { clearTimeout(hideTimer); });
    pop.addEventListener('mouseleave', scheduleHide);
    var r = btn.getBoundingClientRect();
    var w = pop.offsetWidth, h = pop.offsetHeight;
    var left = r.right + 10;
    if (left + w > window.innerWidth - 8) left = Math.max(8, r.left - w - 10);
    var top = Math.min(Math.max(8, r.top - 40), window.innerHeight - h - 8);
    pop.style.left = left + 'px';
    pop.style.top = Math.max(8, top) + 'px';
  }

  function openDialog(url) {
    hidePop();
    var d = document.createElement('dialog');
    d.className = 'te-pdf-dialog';
    var bar = document.createElement('div');
    bar.className = 'te-pdf-bar';
    var t = document.createElement('strong');
    t.textContent = 'Email copy (a masked PDF made for review)';
    var right = document.createElement('span');
    right.className = 'te-pdf-right';
    var a = document.createElement('a');
    a.href = url; a.target = '_blank'; a.rel = 'noopener'; a.textContent = 'Open in a new tab';
    var close = document.createElement('button');
    close.type = 'button'; close.className = 'btn btn-sm'; close.textContent = 'Close';
    right.appendChild(a); right.appendChild(close);
    bar.appendChild(t); bar.appendChild(right);
    var fr = document.createElement('iframe');
    fr.setAttribute('title', 'Email PDF');
    fr.src = url;
    d.appendChild(bar); d.appendChild(fr);
    document.body.appendChild(d);
    function done() { if (d.parentNode) d.remove(); }
    close.addEventListener('click', function () { if (d.close) d.close(); else done(); });
    d.addEventListener('close', done);
    d.addEventListener('click', function (e) { if (e.target === d) { if (d.close) d.close(); else done(); } });
    if (d.showModal) d.showModal(); else d.setAttribute('open', '');
  }

  document.addEventListener('mouseover', function (e) {
    var b = e.target.closest ? e.target.closest('[data-pdf-pop]') : null;
    if (!b) return;
    clearTimeout(hideTimer);
    if (b === owner) return;
    hidePop();
    owner = b;
    showTimer = setTimeout(function () { showPop(b); }, 350);
  });
  document.addEventListener('mouseout', function (e) {
    var b = e.target.closest ? e.target.closest('[data-pdf-pop]') : null;
    if (!b || b !== owner) return;
    if (e.relatedTarget && b.contains(e.relatedTarget)) return;
    clearTimeout(showTimer);
    if (pop) scheduleHide(); else owner = null;
  });
  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-pdf-pop]') : null;
    if (!b) return;
    e.preventDefault();
    var url = b.getAttribute('data-pdf-src');
    if (url) openDialog(url);
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hidePop(); });
  document.addEventListener('scroll', hidePop, true);
})();
