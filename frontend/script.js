const menuToggle = document.querySelector('.menu-toggle');
const navLinks = document.querySelector('.nav-links');

if (menuToggle && navLinks) {
  menuToggle.addEventListener('click', () => {
    const isOpen = navLinks.classList.toggle('open');
    menuToggle.setAttribute('aria-expanded', String(isOpen));
    menuToggle.setAttribute('aria-label', isOpen ? 'Close navigation menu' : 'Open navigation menu');
  });

  document.querySelectorAll('.nav-links a').forEach((link) => {
    link.addEventListener('click', () => {
      navLinks.classList.remove('open');
      menuToggle.setAttribute('aria-expanded', 'false');
      menuToggle.setAttribute('aria-label', 'Open navigation menu');
    });
  });
}

const revealObserver = new IntersectionObserver((entries) => {
  entries.forEach((entry) => {
    if (entry.isIntersecting) {
      entry.target.classList.add('visible');
      revealObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.12 });

document.querySelectorAll('.reveal').forEach((element) => revealObserver.observe(element));

const loginForm = document.querySelector('.login-form');
const signupForm = document.querySelector('.signup-form');
const API_BASE_URL = 'http://127.0.0.1:5000';
const requestedRole = new URLSearchParams(window.location.search).get('role');
const selectedRole = ['doctor', 'patient'].includes(requestedRole)
  ? requestedRole
  : loginForm?.querySelector('input[name="role"]:checked')?.value || 'doctor';

if (loginForm) {
  const roleInput = loginForm.querySelector(`input[name="role"][value="${selectedRole}"]`);
  if (roleInput) {
    roleInput.checked = true;
    roleInput.closest('.role-option')?.classList.add('selected');
    document.querySelectorAll('.role-option').forEach((option) => {
      if (option !== roleInput.closest('.role-option')) option.classList.remove('selected');
    });
  }

  const signupLink = document.querySelector('#signup-link');
  if (signupLink) {
    signupLink.href = `signup-selection.HTML?role=${selectedRole}`;
  }
}

if (signupForm && requestedRole === 'doctor') {
  window.location.replace('doctor-signup.HTML?role=doctor');
}

const signupSelectionBack = document.querySelector('#signup-selection-back');
if (signupSelectionBack) {
  signupSelectionBack.href = `login.HTML?role=${selectedRole}`;
}

async function readApiResponse(response) {
  const contentType = response.headers.get('content-type') || '';
  const responseText = await response.text();
  let payload;
  if (contentType.includes('application/json')) {
    try {
      payload = JSON.parse(responseText);
    } catch {
      throw new Error('The server returned invalid JSON.');
    }
  } else {
    throw new Error(response.ok
      ? 'The server returned an unexpected response.'
      : `The API returned HTTP ${response.status}. Check that the Flask backend is running at ${API_BASE_URL}.`);
  }
  if (!response.ok) throw new Error(payload.error || 'The request could not be completed.');
  return payload;
}
const passwordToggle = document.querySelector('.password-toggle');
const passwordInput = document.querySelector('#password');

if (passwordToggle && passwordInput) {
  passwordToggle.addEventListener('click', () => {
    const isVisible = passwordInput.type === 'text';
    passwordInput.type = isVisible ? 'password' : 'text';
    passwordToggle.textContent = isVisible ? 'Show' : 'Hide';
    passwordToggle.setAttribute('aria-label', isVisible ? 'Show password' : 'Hide password');
  });
}

document.querySelectorAll('.role-option').forEach((option) => {
  option.addEventListener('click', () => {
    document.querySelectorAll('.role-option').forEach((item) => item.classList.remove('selected'));
    option.classList.add('selected');
    const role = option.querySelector('input[name="role"]')?.value;
    const signupLink = document.querySelector('#signup-link');
    if (signupLink && (role === 'doctor' || role === 'patient')) {
      signupLink.href = `signup-selection.HTML?role=${role}`;
    }
  });
});

if (loginForm) {
  loginForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const loginButton = loginForm.querySelector('.login-button');
    const originalButtonText = loginButton.innerHTML;
    const errorMessage = document.querySelector('.login-error');
    errorMessage?.remove();
    loginButton.disabled = true;
    loginButton.textContent = 'Signing in...';
    loginButton.classList.add('is-loading');

    const formData = new FormData(loginForm);
    try {
      const response = await fetch(`${API_BASE_URL}/api/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: formData.get('email'),
          password: formData.get('password'),
          role: formData.get('role'),
        }),
      });
      const result = await readApiResponse(response);

      sessionStorage.setItem('ehrAuthToken', result.token);
      sessionStorage.setItem('ehrUser', JSON.stringify(result.user));
      window.location.href = result.user.role === 'doctor' ? 'doctor-dashboard.HTML' : 'patient-dashboard.HTML';
    } catch (error) {
      const message = document.createElement('p');
      message.className = 'login-error';
      message.setAttribute('role', 'alert');
      message.textContent = error instanceof TypeError
        ? 'The authentication service is unavailable. Please start the backend and try again.'
        : error.message;
      loginForm.prepend(message);
    } finally {
      loginButton.disabled = false;
      loginButton.innerHTML = originalButtonText;
      loginButton.classList.remove('is-loading');
    }
  });
}

const forgotPasswordLink = document.querySelector('#forgot-password-link');
const forgotPasswordDialog = document.querySelector('#forgot-password-dialog');
const passwordResetRequestForm = document.querySelector('#password-reset-request-form');
const passwordResetRequestStatus = document.querySelector('#password-reset-request-status');
const passwordResetDevLink = document.querySelector('#password-reset-dev-link');

if (forgotPasswordLink && forgotPasswordDialog && passwordResetRequestForm) {
  forgotPasswordLink.addEventListener('click', (event) => {
    event.preventDefault();
    const loginEmail = loginForm?.querySelector('input[name="email"]');
    const resetEmail = passwordResetRequestForm.querySelector('input[name="email"]');
    if (loginEmail && resetEmail) resetEmail.value = loginEmail.value;
    forgotPasswordDialog.showModal();
    resetEmail?.focus();
  });

  document.querySelector('#close-password-reset').addEventListener('click', () => {
    forgotPasswordDialog.close();
  });

  passwordResetRequestForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    passwordResetRequestStatus.textContent = '';
    passwordResetRequestStatus.classList.remove('is-error');
    passwordResetDevLink.hidden = true;
    const submitButton = passwordResetRequestForm.querySelector('button[type="submit"]');
    submitButton.disabled = true;
    try {
      const response = await fetch(`${API_BASE_URL}/api/password-reset/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: new FormData(passwordResetRequestForm).get('email'),
        }),
      });
      const result = await readApiResponse(response);
      passwordResetRequestStatus.textContent = result.message;
      if (result.reset_url) {
        passwordResetDevLink.href = result.reset_url;
        passwordResetDevLink.hidden = false;
      }
    } catch (error) {
      passwordResetRequestStatus.textContent = error instanceof TypeError
        ? 'The authentication service is unavailable. Please try again.'
        : error.message;
      passwordResetRequestStatus.classList.add('is-error');
    } finally {
      submitButton.disabled = false;
    }
  });
}

const resetPasswordForm = document.querySelector('#reset-password-form');
if (resetPasswordForm) {
  const resetPasswordStatus = document.querySelector('#reset-password-status');
  const resetToken = new URLSearchParams(window.location.hash.slice(1)).get('token');
  const submitButton = resetPasswordForm.querySelector('button[type="submit"]');
  if (!resetToken) {
    resetPasswordStatus.textContent = 'This reset link is invalid or incomplete.';
    resetPasswordStatus.classList.add('is-error');
    submitButton.disabled = true;
  }

  resetPasswordForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (!resetToken) return;
    resetPasswordStatus.textContent = '';
    resetPasswordStatus.classList.remove('is-error');
    const formData = new FormData(resetPasswordForm);
    const password = String(formData.get('password') || '');
    const confirmPassword = String(formData.get('confirm_password') || '');
    if (password !== confirmPassword) {
      resetPasswordStatus.textContent = 'Passwords do not match.';
      resetPasswordStatus.classList.add('is-error');
      return;
    }
    submitButton.disabled = true;
    try {
      const response = await fetch(`${API_BASE_URL}/api/password-reset/confirm`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token: resetToken,
          password,
          confirm_password: confirmPassword,
        }),
      });
      const result = await readApiResponse(response);
      resetPasswordStatus.textContent = result.message;
      resetPasswordForm.querySelectorAll('input').forEach((input) => { input.disabled = true; });
      submitButton.textContent = 'Password Reset';
      window.history.replaceState(null, '', window.location.pathname);
    } catch (error) {
      resetPasswordStatus.textContent = error instanceof TypeError
        ? 'The authentication service is unavailable. Please try again.'
        : error.message;
      resetPasswordStatus.classList.add('is-error');
      submitButton.disabled = false;
    }
  });
}

if (signupForm) {
  console.info('[EHR Secure] Registration form handler attached.');
  signupForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    console.info('[EHR Secure] Registration submit triggered.');
    const submitButton = signupForm.querySelector('.signup-button');
    const errorMessage = document.querySelector('.signup-error');
    errorMessage?.remove();
    const formData = new FormData(signupForm);
    const requiredFields = ['name', 'email', 'password', 'confirm_password', 'date_of_birth', 'gender', 'phone'];
    const missingField = requiredFields.find((field) => !String(formData.get(field) || '').trim());
    const email = String(formData.get('email') || '').trim();
    const password = String(formData.get('password') || '');
    const confirmPassword = String(formData.get('confirm_password') || '');
    if (missingField) {
      console.warn('[EHR Secure] Registration validation failed: missing field', missingField);
      showSignupMessage('Please complete all required fields.', 'signup-error');
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      console.warn('[EHR Secure] Registration validation failed: invalid email.');
      showSignupMessage('Please enter a valid email address.', 'signup-error');
      return;
    }
    if (password.length < 8) {
      console.warn('[EHR Secure] Registration validation failed: password too short.');
      showSignupMessage('Password must be at least 8 characters.', 'signup-error');
      return;
    }
    if (password !== confirmPassword) {
      console.warn('[EHR Secure] Registration validation failed: passwords do not match.');
      showSignupMessage('Passwords do not match.', 'signup-error');
      return;
    }
    submitButton.disabled = true;
    submitButton.textContent = 'Creating account...';
    submitButton.classList.add('is-loading');
    try {
      console.info('[EHR Secure] Sending registration request:', `${API_BASE_URL}/api/register`);
      const response = await fetch(`${API_BASE_URL}/api/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(Object.fromEntries(formData.entries())),
      });
      const result = await readApiResponse(response);
      console.info('[EHR Secure] Registration response:', response.status, result);
      sessionStorage.setItem('signupMessage', result.message);
      window.location.href = 'login.HTML?role=patient';
    } catch (error) {
      console.error('[EHR Secure] Registration request failed:', error);
      showSignupMessage(error instanceof TypeError
        ? 'The authentication service is unavailable. Please try again.'
        : error.message, 'signup-error');
    } finally {
      submitButton.disabled = false;
      submitButton.textContent = 'Create Account';
      submitButton.classList.remove('is-loading');
    }
  });
}

const signupMessage = sessionStorage.getItem('signupMessage');
if (signupMessage && document.querySelector('.login-form')) {
  const message = document.createElement('p');
  message.className = 'signup-success';
  message.setAttribute('role', 'status');
  message.textContent = signupMessage;
  document.querySelector('.login-form').prepend(message);
  sessionStorage.removeItem('signupMessage');
}

function showSignupMessage(text, className) {
  const message = document.createElement('p');
  message.className = className;
  message.setAttribute('role', 'alert');
  message.textContent = text;
  signupForm.prepend(message);
}