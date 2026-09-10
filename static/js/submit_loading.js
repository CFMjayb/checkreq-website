// submit_loading.js -- shared helper, 2026-09-10.
//
// Jay: "when you post to QBO, there is no indication that it is working...
// we also need the same type of visual on the Check Request screen when you
// press 'Submit Request'." Both actions are plain form submits (no AJAX) --
// a real network round trip (Post to QBO's own qbo-mcp-server -> QuickBooks
// call in particular can take several real seconds) with nothing visually
// changing until the resulting page finishes loading. showButtonLoading()
// disables the clicked button and adds a spinning red-ring CSS treatment
// (.btn-loading, base.css) right as the real submission is about to
// proceed -- the eventual page navigation/reload is what naturally clears
// it, so there's no corresponding "hide" function to call.
//
// Deliberately a small function each page's own submit handler calls
// explicitly, not a blanket document-wide submit listener -- several forms
// in this app (Cancel, etc.) have their own onsubmit confirm()/validation
// logic this must never interfere with.
function showButtonLoading(btn) {
  if (!btn) return;
  btn.disabled = true;
  btn.classList.add('btn-loading');
}
