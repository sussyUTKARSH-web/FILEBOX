document.addEventListener('click', (event) => {
  const toggle = event.target.closest('[data-toggle-password]');
  if (toggle) {
    const input = document.getElementById(toggle.dataset.togglePassword);
    if (input) {
      input.type = input.type === 'password' ? 'text' : 'password';
      toggle.setAttribute('aria-label', input.type === 'password' ? 'Show password' : 'Hide password');
    }
  }
});

document.addEventListener('submit', (event) => {
  const form = event.target.closest('form[data-confirm]');
  if (form && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    return;
  }
  const editForm = event.target.closest('form[data-edit-lock]');
  if (editForm) editForm.dataset.submitting = 'true';
});

const editForm = document.querySelector('form[data-edit-lock]');
if (editForm) {
  const token = editForm.querySelector('[name="edit_token"]')?.value;
  const csrf = editForm.querySelector('[name="csrf_token"]')?.value;
  const status = editForm.querySelector('.edit-lock-status');
  const editor = editForm.querySelector('textarea');
  const saveButton = editForm.querySelector('button[type="submit"]');

  const heartbeat = async () => {
    try {
      const response = await fetch(editForm.dataset.heartbeatUrl, {
        method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: new URLSearchParams({csrf_token: csrf, edit_token: token}),
        credentials: 'same-origin',
      });
      if (response.status === 409 || response.status === 403) {
        window.clearInterval(heartbeatTimer);
        if (editor) editor.readOnly = true;
        if (saveButton) saveButton.disabled = true;
        if (status) status.textContent = 'Your edit lock expired. Reload this page before making changes.';
      }
    } catch (_) {
      // A brief connection interruption is covered by the lease timeout.
    }
  };
  const heartbeatTimer = window.setInterval(heartbeat, 15000);

  window.addEventListener('pagehide', () => {
    window.clearInterval(heartbeatTimer);
    if (editForm.dataset.submitting === 'true') return;
    const payload = new Blob([
      new URLSearchParams({csrf_token: csrf, edit_token: token}).toString(),
    ], {type: 'application/x-www-form-urlencoded;charset=UTF-8'});
    navigator.sendBeacon(editForm.dataset.releaseUrl, payload);
  }, {once: true});
}
