const API_BASE_URL = 'http://127.0.0.1:5000';
const pageSize = 25;
const searchPageSize = 100;
let currentPage = 1;
let totalPages = 0;

const statusElement = document.querySelector('#demo-record-status');
const countElement = document.querySelector('#demo-record-count');
const tableWrapper = document.querySelector('#demo-table-scroll');
const tableBody = document.querySelector('#demo-records-body');
const pagination = document.querySelector('#demo-pagination');
const pageStatus = document.querySelector('#demo-page-status');
const previousButton = document.querySelector('#demo-previous');
const nextButton = document.querySelector('#demo-next');
const patientSearchInput = document.querySelector('#patient-search-input');
const patientSearchSection = document.querySelector('#patient-search-section');
const patientSearchAction = document.querySelector('#patient-search-action');
const patientSearchClose = document.querySelector('#patient-search-close');
const patientSearchClear = document.querySelector('#patient-search-clear');
const patientSearchStatus = document.querySelector('#patient-search-status');
const patientSearchCount = document.querySelector('#patient-search-count');
const patientSearchResults = document.querySelector('#patient-search-results');
const patientSearchBody = document.querySelector('#patient-search-body');
let patientSearchSequence = 0;

function getDoctorSession() {
  const token = sessionStorage.getItem('ehrAuthToken');
  try {
    const user = JSON.parse(sessionStorage.getItem('ehrUser') || 'null');
    return token && user?.role === 'doctor' ? token : null;
  } catch {
    return null;
  }
}

function addCell(row, value) {
  const cell = document.createElement('td');
  cell.textContent = value == null ? '' : String(value);
  row.append(cell);
}

function displayDate(value) {
  return typeof value === 'string' ? value.slice(0, 10) : '';
}

function displayRecord(record, target = tableBody) {
  const row = document.createElement('tr');
  addCell(row, record.source_type);
  addCell(row, record.full_name);
  addCell(row, `${record.age} / ${record.gender}`);
  addCell(row, record.blood_type);
  addCell(row, record.medical_condition);
  addCell(row, record.diagnosis || record.test_results);
  addCell(row, record.treatment || record.medication);
  addCell(row, displayDate(record.admission_date || record.record_date));
  addCell(row, displayDate(record.discharge_date));
  addCell(row, record.doctor);
  addCell(row, record.hospital);
  addCell(row, record.billing_amount);
  target.append(row);
}

function normalizePatientSearch(value) {
  return String(value ?? '').normalize('NFKC').trim().replace(/\s+/g, ' ').toLowerCase();
}

async function fetchDashboardRecordsPage(token, page, perPage, query = '') {
  const response = await fetch(
    `${API_BASE_URL}/api/doctor-dashboard/records?page=${page}&per_page=${perPage}&search=${encodeURIComponent(query)}`,
    { headers: { Authorization: `Bearer ${token}` } },
  );
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
  }
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Unable to retrieve dashboard records (HTTP ${response.status}).`);
  }
  const expectedPages = Number.isInteger(payload.total) && payload.total >= 0 && perPage > 0
    ? Math.ceil(payload.total / perPage)
    : -1;
  if (
    payload.combined_data !== true
    || !Array.isArray(payload.records)
    || !Number.isInteger(payload.total)
    || payload.total < 0
    || payload.page !== page
    || !Number.isInteger(payload.pages)
    || payload.pages !== expectedPages
    || payload.per_page !== perPage
  ) {
    throw new Error('The server returned an invalid dashboard-record response.');
  }
  return payload;
}

async function fetchMatchingDashboardRecords(token, query, searchSequence) {
  const firstPage = await fetchDashboardRecordsPage(token, 1, searchPageSize, query);
  const records = [...firstPage.records];
  for (let page = 2; page <= firstPage.pages; page += 1) {
    if (searchSequence !== patientSearchSequence) {
      return records;
    }
    const payload = await fetchDashboardRecordsPage(token, page, searchPageSize, query);
    if (payload.total !== firstPage.total || payload.pages !== firstPage.pages) {
      throw new Error('The dashboard records changed during search. Please try again.');
    }
    records.push(...payload.records);
  }
  if (records.length !== firstPage.total) {
    throw new Error('The server returned an incomplete dashboard-record search result.');
  }
  return records;
}

async function searchPatients(token) {
  const searchSequence = ++patientSearchSequence;
  const query = normalizePatientSearch(patientSearchInput.value);
  patientSearchBody.replaceChildren();
  patientSearchResults.hidden = true;
  patientSearchCount.textContent = '';
  if (!query) {
    patientSearchStatus.textContent = 'Enter a patient name or ID to search.';
    patientSearchStatus.hidden = false;
    return;
  }

  patientSearchStatus.textContent = 'Searching patient records...';
  patientSearchStatus.hidden = false;
  try {
    const records = await fetchMatchingDashboardRecords(token, query, searchSequence);
    if (searchSequence !== patientSearchSequence) {
      return;
    }
    const currentQuery = normalizePatientSearch(patientSearchInput.value);
    const matches = currentQuery
      ? records.filter((record) =>
        normalizePatientSearch(record.full_name).includes(currentQuery)
      )
      : records;
    if (matches.length === 0) {
      patientSearchStatus.textContent = 'No matching records found';
      return;
    }
    matches.forEach((record) => displayRecord(record, patientSearchBody));
    patientSearchStatus.hidden = true;
    patientSearchResults.hidden = false;
    patientSearchCount.textContent = `${matches.length} matching records`;
  } catch (error) {
    if (searchSequence !== patientSearchSequence) {
      return;
    }
    patientSearchStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
  }
}

async function loadDemoRecords(token) {
  statusElement.textContent = 'Loading demo records...';
  statusElement.hidden = false;
  tableWrapper.hidden = true;
  pagination.hidden = true;
  countElement.textContent = '';
  tableBody.replaceChildren();

  try {
    const response = await fetch(
      `${API_BASE_URL}/api/doctor-dashboard/records?page=${currentPage}&per_page=${pageSize}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Unable to load dashboard records (HTTP ${response.status}).`);
    }
    if (
      payload.combined_data !== true
      || !Array.isArray(payload.records)
      || !Number.isInteger(payload.total)
      || !Number.isInteger(payload.pages)
      || payload.page !== currentPage
      || payload.per_page !== pageSize
    ) {
      throw new Error('The server returned an invalid dashboard-record response.');
    }

    totalPages = payload.pages;
    countElement.textContent = `${payload.total} healthcare records`;
    if (payload.records.length === 0) {
      statusElement.textContent = 'No healthcare records are available.';
      return;
    }

    payload.records.forEach((record) => displayRecord(record));
    statusElement.hidden = true;
    tableWrapper.hidden = false;
    pagination.hidden = false;
    pageStatus.textContent = `Page ${payload.page} of ${payload.pages}`;
    previousButton.disabled = payload.page <= 1;
    nextButton.disabled = payload.page >= payload.pages;
  } catch (error) {
    statusElement.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and reload this page.'
      : error.message;
    countElement.textContent = '';
  }
}

const token = getDoctorSession();
if (!token) {
  sessionStorage.removeItem('ehrAuthToken');
  sessionStorage.removeItem('ehrUser');
  window.location.replace('login.HTML');
} else {
  document.querySelector('#sign-out').addEventListener('click', () => {
    sessionStorage.removeItem('ehrAuthToken');
    sessionStorage.removeItem('ehrUser');
  });
  patientSearchAction.addEventListener('click', () => {
    const isOpening = patientSearchSection.hidden;
    patientSearchSection.hidden = !isOpening;
    patientSearchAction.setAttribute('aria-expanded', String(isOpening));
    if (isOpening) {
      patientSearchInput.focus({ preventScroll: true });
    }
  });
  patientSearchClose.addEventListener('click', () => {
    patientSearchSection.hidden = true;
    patientSearchAction.setAttribute('aria-expanded', 'false');
    patientSearchAction.focus({ preventScroll: true });
  });
  patientSearchInput.addEventListener('input', () => searchPatients(token));
  patientSearchClear.addEventListener('click', () => {
    patientSearchInput.value = '';
    searchPatients(token);
    patientSearchInput.focus();
  });
  previousButton.addEventListener('click', () => {
    if (currentPage > 1) {
      currentPage -= 1;
      loadDemoRecords(token);
    }
  });
  nextButton.addEventListener('click', () => {
    if (currentPage < totalPages) {
      currentPage += 1;
      loadDemoRecords(token);
    }
  });
  loadDemoRecords(token);
}
