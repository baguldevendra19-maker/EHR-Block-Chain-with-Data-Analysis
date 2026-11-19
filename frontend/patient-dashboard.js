const API_BASE_URL = 'http://127.0.0.1:5000';

const statusElement = document.querySelector('#patient-record-status');
const countElement = document.querySelector('#patient-record-count');
const tableWrapper = document.querySelector('#patient-records-scroll');
const tableBody = document.querySelector('#patient-records-body');
const profileStatus = document.querySelector('#patient-profile-status');
const profileDetails = document.querySelector('#patient-profile-details');
const linkProfileButton = document.createElement('button');
linkProfileButton.type = 'button';
linkProfileButton.textContent = 'Verify email to link existing clinical records';
linkProfileButton.hidden = true;
profileStatus.after(linkProfileButton);

function patientToken() {
  const token = sessionStorage.getItem('ehrAuthToken');
  try {
    const user = JSON.parse(sessionStorage.getItem('ehrUser') || 'null');
    return token && user?.role === 'patient' ? token : null;
  } catch {
    return null;
  }
}

function addCell(row, value) {
  const cell = document.createElement('td');
  cell.textContent = value == null ? '' : String(value);
  row.append(cell);
}

function displayRecord(record, token) {
  const row = document.createElement('tr');
  addCell(row, typeof record.record_date === 'string' ? record.record_date.slice(0, 10) : '');
  addCell(row, record.diagnosis);
  addCell(row, record.hospital_name);
  addCell(row, record.doctor_name);
  addCell(row, (record.medicines || []).map((medicine) =>
    `${medicine.medicine_name} — ${medicine.dose}, ${medicine.frequency}, ${medicine.duration}`
  ).join('; ') || record.treatment);
  addCell(row, record.additional_instructions);
  addCell(row, typeof record.follow_up_date === 'string' ? record.follow_up_date.slice(0, 10) : '');
  const reportsCell = document.createElement('td');
  (record.reports || []).forEach((report) => {
    [['View', 'view'], ['Download', 'download']].forEach(([label, action]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'report-action';
      button.textContent = `${label}: ${report.original_name}`;
      button.addEventListener('click', () =>
        openMedicalReport(report._id, action, token, report.original_name));
      reportsCell.append(button, document.createTextNode(' '));
    });
  });
  row.append(reportsCell);
  tableBody.append(row);
}

function addProfileDetail(label, value) {
  const term = document.createElement('dt');
  term.textContent = label;
  const description = document.createElement('dd');
  description.textContent = value == null || value === '' ? '—' : String(value);
  profileDetails.append(term, description);
}

async function loadPatientProfile(token) {
  try {
    const response = await fetch(`${API_BASE_URL}/api/patient/profile`, {
      headers: { Authorization: 'Bearer ' + token },
    });
    const payload = await response.json();
    if (response.status === 404) {
      profileStatus.textContent =
        'No clinical profile is linked yet. Verify your email to link matching medical records.';
      linkProfileButton.hidden = false;
      return;
    }
    if (!response.ok) {
      throw new Error(payload.error || `Unable to load your profile (HTTP ${response.status}).`);
    }
    const patient = payload.patient;
    if (!patient || !Array.isArray(patient.visits)) {
      throw new Error('The server returned an invalid patient-profile response.');
    }
    profileDetails.replaceChildren();
    [
      ['Patient ID', patient.patient_code],
      ['Patient Full Name', patient.full_name],
      ['Age', patient.age],
      ['Gender', patient.gender],
      ['Blood Group', patient.blood_group],
      ['Mobile Number', patient.mobile_number],
      ['Email Address', patient.email],
    ].forEach(([label, value]) => addProfileDetail(label, value));
    profileDetails.hidden = false;
    profileStatus.hidden = true;
    linkProfileButton.hidden = true;
  } catch (error) {
    profileStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and reload this page.'
      : error.message;
  }
}

async function requestPatientProfileLink(token) {
  linkProfileButton.disabled = true;
  profileStatus.hidden = false;
  profileStatus.textContent = 'Requesting a secure email verification link...';
  try {
    const response = await fetch(`${API_BASE_URL}/api/patient/profile/link/request`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token },
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Unable to request profile linking (HTTP ${response.status}).`);
    }
    profileStatus.textContent = payload.message;
    linkProfileButton.hidden = true;
  } catch (error) {
    profileStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
  } finally {
    linkProfileButton.disabled = false;
  }
}

async function openMedicalReport(reportId, action, token, filename) {
  let reportWindow = null;
  try {
    if (action === 'view') {
      reportWindow = window.open('about:blank', '_blank');
      if (!reportWindow) {
        throw new Error('Allow pop-ups to view medical reports.');
      }
      reportWindow.opener = null;
    }
    const response = await fetch(`${API_BASE_URL}/api/reports/${encodeURIComponent(reportId)}/${action}`, {
      headers: { Authorization: 'Bearer ' + token },
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `Unable to retrieve report (HTTP ${response.status}).`);
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    if (action === 'view') {
      reportWindow.location.href = objectUrl;
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
      return;
    }
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = filename || 'medical-report';
    link.click();
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
  } catch (error) {
    if (reportWindow && !reportWindow.closed) {
      reportWindow.close();
    }
    statusElement.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
    statusElement.hidden = false;
  }
}

async function loadPatientRecords(token) {
  try {
    const response = await fetch(`${API_BASE_URL}/api/records`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const payload = await response.json();
    if (response.status === 401) {
      sessionStorage.removeItem('ehrAuthToken');
      sessionStorage.removeItem('ehrUser');
      window.location.replace('login.HTML?role=patient');
      return;
    }
    if (!response.ok) {
      throw new Error(payload.error || `Unable to load your records (HTTP ${response.status}).`);
    }
    if (!Array.isArray(payload.records)) {
      throw new Error('The server returned an invalid health-record response.');
    }

    countElement.textContent = `${payload.records.length} records`;
    tableBody.replaceChildren();
    if (payload.records.length === 0) {
      statusElement.textContent = 'No clinical encounter records found for this account.';
      return;
    }
    payload.records.forEach((record) => displayRecord(record, token));
    statusElement.hidden = true;
    tableWrapper.hidden = false;
  } catch (error) {
    statusElement.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and reload this page.'
      : error.message;
  }
}

const token = patientToken();
if (!token) {
  sessionStorage.removeItem('ehrAuthToken');
  sessionStorage.removeItem('ehrUser');
  window.location.replace('login.HTML?role=patient');
} else {
  linkProfileButton.addEventListener('click', () => requestPatientProfileLink(token));
  document.querySelector('#patient-sign-out').addEventListener('click', () => {
    sessionStorage.removeItem('ehrAuthToken');
    sessionStorage.removeItem('ehrUser');
  });
  loadPatientProfile(token);
  loadPatientRecords(token);
}
