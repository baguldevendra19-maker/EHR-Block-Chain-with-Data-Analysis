const API_BASE_URL = 'http://127.0.0.1:5000';
const statusElement = document.querySelector('#verification-status');
const token = new URLSearchParams(window.location.hash.slice(1)).get('token');
window.history.replaceState(null, '', window.location.pathname);

async function verifyPatientEmail() {
  if (!token) {
    statusElement.textContent = 'This verification link is invalid or incomplete.';
    return;
  }
  try {
    const response = await fetch(`${API_BASE_URL}/api/email-verification/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    });
    const payload = await response.json();
    statusElement.textContent = response.ok
      ? payload.message
      : payload.error || `Email verification failed (HTTP ${response.status}).`;
  } catch (error) {
    statusElement.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and reopen this link.'
      : error.message;
  }
}

verifyPatientEmail();
