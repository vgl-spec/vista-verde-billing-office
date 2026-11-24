// "Show password" toggle for fields rendered by ui.field(type='password').
// Kept out of app.js so screens can adopt it without touching the shared bundle.
document.querySelectorAll('[data-reveal]').forEach((button) => {
  const input = document.getElementById(button.dataset.reveal);
  if (!input) return;
  button.addEventListener('click', () => {
    const showing = input.type === 'text';
    input.type = showing ? 'password' : 'text';
    button.textContent = showing ? 'Show password' : 'Hide password';
    button.setAttribute('aria-pressed', String(!showing));
    input.focus();
  });
  // Never submit or leave a password visible after the form is sent or the page is restored from cache.
  const hide = () => { input.type = 'password'; button.textContent = 'Show password'; button.setAttribute('aria-pressed', 'false'); };
  input.form?.addEventListener('submit', hide);
  window.addEventListener('pageshow', hide);
});
