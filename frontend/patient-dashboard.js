const API_BASE_URL = 'http://127.0.0.1:5000';

const statusElement = document.querySelector('#patient-record-status');
const countElement = document.querySelector('#patient-record-count');
const tableWrapper = document.querySelector('#patient-records-scroll');
const tableBody = document.querySelector('#patient-records-body');

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

function displayRecord(record) {
  const row = document.createElement('tr');
  addCell(row, typeof record.record_date === 'string' ? record.record_date.slice(0, 10) : '');
  addCell(row, record.medical_condition);
  addCell(row, record.diagnosis);
  addCell(row, record.treatment);
  tableBody.append(row);
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
      statusElement.textContent = 'No health records are currently associated with your account.';
      return;
    }
    payload.records.forEach(displayRecord);
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
  document.querySelector('#patient-sign-out').addEventListener('click', () => {
    sessionStorage.removeItem('ehrAuthToken');
    sessionStorage.removeItem('ehrUser');
  });
  loadPatientRecords(token);
}
