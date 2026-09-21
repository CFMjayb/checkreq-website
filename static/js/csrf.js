// csrf.js -- shared helper, M8 (Security Assessment 2026-09-19).
//
// Reads the per-session CSRF token base.html stamps into a <meta> tag (the
// same token csrf_guard.py's middleware checks on every POST/PUT/DELETE/
// PATCH) and exposes it for the fetch() calls in this app that POST with no
// <form> of their own to carry a hidden csrf_token field -- a JSON body
// (admin_setup.js's save/add routes, feedback_chat.js), or an
// intentionally field-less POST (notifications.js's mark-as-read,
// admin_setup.js's "check all" button). A plain <form method="post"> never
// needs this file -- its own hidden csrf_token input already carries the
// token in the request body, which is what the middleware checks first for
// any submission that doesn't send this header.
window.csrfToken = function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  return meta ? meta.getAttribute('content') : '';
};

// Spread into a fetch() call's own headers object, e.g.:
//   fetch(url, { method: 'POST', headers: Object.assign({'Content-Type':'application/json'}, csrfHeader()), body })
window.csrfHeader = function () {
  return { 'X-CSRF-Token': window.csrfToken() };
};
