// feedback_chat.js -- conversational Feedback intake (2026-08-02).
// Simple request/response per turn (no streaming needed -- this is a
// slow-paced reflective conversation, not a live chat product, per the
// approved design). Renders the message list, sends new messages to
// POST /feedback/message, and submits the whole conversation for
// summarization via POST /feedback/{id}/close.

document.addEventListener('DOMContentLoaded', () => {
  const messagesEl = document.getElementById('chatMessages');
  const errorEl = document.getElementById('chatError');
  const form = document.getElementById('chatForm');
  const input = document.getElementById('chatInput');
  const sendBtn = document.getElementById('chatSendBtn');
  const submitBtn = document.getElementById('chatSubmitBtn');
  const submitHint = document.getElementById('chatSubmitHint');
  const conversationIdField = document.getElementById('chatConversationId');
  if (!messagesEl || !form) return;

  let conversationId = conversationIdField.value ? parseInt(conversationIdField.value, 10) : null;
  let sentAnyMessage = !!conversationId;
  let limitReached = false;

  function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s == null ? '' : String(s);
    return d.innerHTML;
  }

  function appendMessage(role, content) {
    const wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-' + role;
    const bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.innerHTML = escapeHtml(content).replace(/\n/g, '<br>');
    wrap.appendChild(bubble);
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setError(msg) {
    if (!msg) {
      errorEl.hidden = true;
      errorEl.textContent = '';
      return;
    }
    errorEl.hidden = false;
    errorEl.textContent = msg;
  }

  function updateSubmitState() {
    if (limitReached) {
      submitBtn.disabled = false;
      submitHint.textContent = 'Ready when you are.';
      return;
    }
    submitBtn.disabled = !sentAnyMessage;
    submitHint.textContent = sentAnyMessage
      ? 'Ready whenever you feel this is complete.'
      : 'Send at least one message first.';
  }

  updateSubmitState();

  form.addEventListener('submit', async (evt) => {
    evt.preventDefault();
    const text = input.value.trim();
    if (!text || limitReached) return;

    setError(null);
    appendMessage('user', text);
    input.value = '';
    sendBtn.disabled = true;
    input.disabled = true;

    try {
      const resp = await fetch('/feedback/message', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, conversation_id: conversationId }),
      });
      const data = await resp.json();
      if (!resp.ok) {
        setError(data.error || 'Something went wrong sending that -- please try again.');
        return;
      }
      conversationId = data.conversation_id;
      appendMessage('assistant', data.reply);
      sentAnyMessage = true;
      limitReached = !!data.limit_reached;
      updateSubmitState();
    } catch (err) {
      setError('Couldn’t reach the server -- check your connection and try again.');
    } finally {
      sendBtn.disabled = false;
      input.disabled = false;
      input.focus();
    }
  });

  submitBtn.addEventListener('click', async () => {
    if (!conversationId) return;
    setError(null);
    submitBtn.disabled = true;
    submitBtn.textContent = 'Submitting…';

    try {
      const resp = await fetch('/feedback/' + conversationId + '/close', {
        method: 'POST',
        credentials: 'same-origin',
      });
      const data = await resp.json();
      if (!resp.ok) {
        setError(data.error || 'Couldn’t submit just now -- please try again.');
        submitBtn.disabled = false;
        submitBtn.textContent = 'Submit my feedback';
        return;
      }
      window.location.href = '/feedback?submitted=1';
    } catch (err) {
      setError('Couldn’t reach the server -- check your connection and try again.');
      submitBtn.disabled = false;
      submitBtn.textContent = 'Submit my feedback';
    }
  });

  input.addEventListener('keydown', (evt) => {
    if (evt.key === 'Enter' && !evt.shiftKey) {
      evt.preventDefault();
      form.requestSubmit();
    }
  });
});
