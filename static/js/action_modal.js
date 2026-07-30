// action_modal.js -- shared in-app dialog replacing window.prompt()/confirm()
// (Jay, 2026-07-29: "not a Chrome dialogue box"). Builds a single overlay +
// textarea dialog on demand, reused across pages via showActionModal().

function showActionModal({ title, hint = '', placeholder = '', required = false, confirmLabel = 'Confirm' }) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div class="modal-box" role="dialog" aria-modal="true">
        <div class="modal-title">${title}</div>
        ${hint ? `<div class="modal-hint">${hint}</div>` : ''}
        <textarea rows="3" placeholder="${placeholder}"></textarea>
        <div class="modal-error"></div>
        <div class="modal-actions">
          <button type="button" class="btn btn-secondary btn-sm" data-action="cancel">Cancel</button>
          <button type="button" class="btn btn-primary btn-sm" data-action="confirm">${confirmLabel}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    const textarea = overlay.querySelector('textarea');
    const errorEl = overlay.querySelector('.modal-error');
    textarea.focus();

    function close(result) {
      overlay.remove();
      document.removeEventListener('keydown', onKeydown);
      resolve(result);
    }
    function onKeydown(e) {
      if (e.key === 'Escape') close(null);
    }
    document.addEventListener('keydown', onKeydown);

    overlay.querySelector('[data-action="cancel"]').addEventListener('click', () => close(null));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(null); });
    overlay.querySelector('[data-action="confirm"]').addEventListener('click', () => {
      const value = textarea.value.trim();
      if (required && !value) {
        errorEl.textContent = 'This field is required.';
        return;
      }
      close(value);
    });
  });
}
