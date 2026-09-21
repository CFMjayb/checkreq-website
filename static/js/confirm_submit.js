// confirm_submit.js -- shared helper, M9 (Security Assessment 2026-09-19).
//
// Replaces every plain onsubmit="return confirm('...{{ value }}...')"
// inline handler in this app. The bug those had: Jinja autoescape converts
// a literal quote in an interpolated value to &#39; for the HTML ATTRIBUTE
// context, but the BROWSER decodes that entity back to a literal quote
// BEFORE the JS engine parses the onsubmit attribute's own source -- so a
// self-editable value (a person's own display name, a vendor name typed
// during new-vendor onboarding) containing a quote can break out of the
// JS string literal the value was spliced into.
//
// The fix: never splice a template value into JS source at all. A form
// opts in with data-confirm="Remove {{ value }}?" -- Jinja escapes `value`
// for the ATTRIBUTE context exactly as before, but this script reads it
// back via getAttribute() (plain text, never re-parsed as JS/HTML) and
// hands it straight to confirm(). One delegated listener covers every
// current and future data-confirm form with no per-page wiring.
document.addEventListener('submit', function (e) {
  var form = e.target;
  if (form && form.hasAttribute && form.hasAttribute('data-confirm')) {
    if (!confirm(form.getAttribute('data-confirm'))) {
      e.preventDefault();
    }
  }
});
