/* The Beacon Overview reader (overview_reader.html). The document sits in a sandboxed iframe that has NO script rights, so this
   page does everything: it sizes the iframe to the document (on load, on window resize, and when the document's own size changes)
   and switches the document between light and dark. The choice is remembered on this device only (localStorage, wrapped in
   try/catch because it can be blocked). No inline handlers, no network calls. */
(function () {
  'use strict';
  var frame = document.getElementById('ovr-frame');
  if (!frame) { return; }
  var button = document.querySelector('[data-ovr-theme]');
  var KEY = 'beacon.overview.theme';
  var theme = 'light';
  var observer = null;
  var pending = false;
  var wiredDoc = null;

  function doc() {
    try { return frame.contentDocument; } catch (e) { return null; }
  }

  function stored() {
    try { return window.localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function save(value) {
    try { window.localStorage.setItem(KEY, value); } catch (e) { /* blocked: the choice just is not remembered */ }
  }

  function fit() {
    var d = doc();
    if (!d || !d.documentElement) { return; }
    var h = Math.ceil(d.documentElement.getBoundingClientRect().height);
    if (h > 0) { frame.style.height = (h + 2) + 'px'; }
  }

  function schedule() {
    if (pending) { return; }
    pending = true;
    window.setTimeout(function () { pending = false; fit(); }, 30);      // a timer, not requestAnimationFrame: paused while the tab is in the background
  }

  function paint() {
    var d = doc();
    if (d && d.documentElement) { d.documentElement.setAttribute('data-theme', theme); }
    frame.setAttribute('data-theme', theme);
    if (button) { button.textContent = theme === 'dark' ? 'Light mode' : 'Dark mode'; }
    schedule();
  }

  /* A link like href="#s-money" inside a srcdoc iframe is resolved against the PARENT page's address, so left alone it would load
     this whole page inside the iframe. The iframe is as tall as its document, so "go to the section" means scrolling THIS page. */
  function scrollToSection(id) {
    var d = doc();
    var target = d && id ? d.getElementById(id) : null;
    if (!target) { return false; }
    var header = document.querySelector('.site-header');
    var offset = (header ? header.offsetHeight : 0) + 16;
    var top = target.getBoundingClientRect().top + frame.getBoundingClientRect().top + window.pageYOffset - offset;
    window.scrollTo(0, Math.max(0, top));
    return true;
  }

  function wireAnchors(d) {
    d.addEventListener('click', function (event) {
      var link = event.target && event.target.closest ? event.target.closest('a[href^="#"]') : null;
      if (!link) { return; }
      event.preventDefault();
      scrollToSection(link.getAttribute('href').slice(1));
    });
  }

  function onLoaded() {
    paint();
    var d = doc();
    if (d && d.documentElement && window.ResizeObserver) {
      if (observer) { observer.disconnect(); }
      observer = new window.ResizeObserver(schedule);
      observer.observe(d.documentElement);
    }
    if (d && wiredDoc !== d) {
      wiredDoc = d;
      wireAnchors(d);
      if (window.location.hash.length > 1) {      // an overview:<slug>#section link from another document
        window.setTimeout(function () {
          var id = window.location.hash.slice(1);
          try { id = decodeURIComponent(id); } catch (e) { /* use it as typed */ }
          fit();
          scrollToSection(id);
        }, 60);
      }
    }
  }

  var saved = stored();
  if (saved === 'dark' || saved === 'light') {
    theme = saved;
  } else if (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) {
    theme = 'dark';
  }

  if (button) {
    button.addEventListener('click', function () {
      theme = theme === 'dark' ? 'light' : 'dark';
      save(theme);
      paint();
    });
  }
  frame.addEventListener('load', onLoaded);
  window.addEventListener('resize', schedule);
  var d0 = doc();
  if (d0 && d0.readyState === 'complete') { onLoaded(); }
})();
