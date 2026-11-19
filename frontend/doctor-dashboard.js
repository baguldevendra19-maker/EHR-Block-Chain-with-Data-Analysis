const API_BASE_URL = 'http://127.0.0.1:5000';
const pageSize = 25;
let currentPage = 1;
let totalPages = 0;
let activePatient = null;
let editingPatient = null;
let historyLoadSequence = 0;
let historyReturnFocus = null;

const statusElement = document.querySelector('#patient-summary-status');
const countElement = document.querySelector('#patient-summary-count');
const tableWrapper = document.querySelector('#patient-summary-table-scroll');
const tableBody = document.querySelector('#patient-summary-body');
const pagination = document.querySelector('#patient-summary-pagination');
const pageStatus = document.querySelector('#patient-summary-page-status');
const previousButton = document.querySelector('#patient-summary-previous');
const nextButton = document.querySelector('#patient-summary-next');
const patientSearchInput = document.querySelector('#patient-search-input');
const patientSearchSection = document.querySelector('#patient-search-section');
const patientSearchAction = document.querySelector('#patient-search-action');
const patientSearchStatus = document.querySelector('#patient-search-status');
const patientSearchResults = document.querySelector('#patient-search-results');
const patientHistory = document.querySelector('#patient-history');
const patientDetails = document.querySelector('#patient-details');
const patientVisits = document.querySelector('#patient-visits');
const historyDialog = document.querySelector('#patient-history-dialog');
const historyDialogPatient = document.querySelector('#history-dialog-patient');
const historyDialogStatus = document.querySelector('#history-dialog-status');
const historyDialogVisits = document.querySelector('#history-dialog-visits');
const patientEditDialog = document.querySelector('#patient-edit-dialog');
const patientEditForm = document.querySelector('#patient-edit-form');
const patientEditStatus = document.querySelector('#patient-edit-status');
const patientEditSubmit = document.querySelector('#patient-edit-submit');
const addEhrAction = document.querySelector('#add-ehr-action');
const addEhrSection = document.querySelector('#add-ehr-section');
const addEhrForm = document.querySelector('#add-ehr-form');
const addEhrStatus = document.querySelector('#add-ehr-status');
const addEhrSubmit = document.querySelector('#add-ehr-submit');
const newPatientFields = document.querySelector('#new-patient-fields');
const existingPatientSummary = document.querySelector('#existing-patient-summary');
const medicineRows = document.querySelector('#medicine-rows');
const blockchainVerifyAction = document.querySelector('#blockchain-verify-action');
const blockchainVerifySection = document.querySelector('#blockchain-verify-section');
const blockchainVerifySubmit = document.querySelector('#blockchain-verify-submit');
const blockchainVerifyClear = document.querySelector('#blockchain-verify-clear');
const blockchainVerifyClose = document.querySelector('#blockchain-verify-close');
const blockchainRecordIdInput = document.querySelector('#blockchain-record-id');
const blockchainVerifyStatus = document.querySelector('#blockchain-verify-status');
const blockchainVerifyResults = document.querySelector('#blockchain-verify-results');

function getDoctorSession() {
  const token = sessionStorage.getItem('ehrAuthToken');
  try {
    const user = JSON.parse(sessionStorage.getItem('ehrUser') || 'null');
    return token && user?.role === 'doctor' ? { token, user } : null;
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

function displayRecord(record, token) {
  const row = document.createElement('tr');
  addCell(row, record.source_type);
  addCell(row, record.source_id);
  const patientIdCell = createPatientIdCell(
    document,
    row,
    record,
    (patientCode, patientRow) => openPatientHistoryModal(token, patientCode, patientRow),
  );
  row.append(patientIdCell);
  addCell(row, record.full_name);
  addCell(row, record.visit_count);
  addCell(row, [record.age, record.gender].filter(Boolean).join(' / '));
  addCell(row, record.blood_type);
  addCell(row, record.medical_condition);
  addCell(row, record.diagnosis || record.test_results);
  addCell(row, record.treatment);
  addCell(row, displayDate(record.treatment_date));
  addCell(row, record.hospital_name);
  addCell(row, record.doctor_name);
  const actionsCell = document.createElement('td');
  const editButton = document.createElement('button');
  editButton.type = 'button';
  editButton.className = 'patient-edit-button';
  editButton.textContent = 'Edit';
  editButton.setAttribute('aria-label', `Edit details for ${record.full_name}`);
  editButton.addEventListener('click', (event) => {
    event.stopPropagation();
    openPatientEditDialog(record);
  });
  editButton.addEventListener('keydown', (event) => event.stopPropagation());
  actionsCell.append(editButton);
  row.append(actionsCell);
  tableBody.append(row);
}

function openPatientEditDialog(patient) {
  patientEditForm.elements.full_name.value = patient.full_name || '';
  patientEditForm.elements.age.value = patient.age || '';
  patientEditForm.elements.gender.value = patient.gender || '';
  patientEditForm.elements.blood_group.value = patient.blood_type || '';
  patientEditForm.elements.mobile_number.value = patient.mobile_number || '';
  patientEditForm.elements.email.value = patient.email || '';
  patientEditForm.elements.email.value = patient.email || '';
  document.querySelector('#patient-edit-code').textContent = patient.patient_code;
  patientEditForm.dataset.patientCode = patient.patient_code;
  patientEditStatus.textContent = '';
  patientEditSubmit.disabled = false;
  patientEditDialog.showModal();
  patientEditForm.elements.full_name.focus({ preventScroll: true });
}

async function submitPatientEdit(token) {
  if (!patientEditForm.reportValidity()) {
    return;
  }
  const patientCode = patientEditForm.dataset.patientCode;
  const payload = Object.fromEntries(
    ['full_name', 'age', 'gender', 'blood_group', 'mobile_number', 'email'].map((field) => [
      field,
      patientEditForm.elements[field].value.trim(),
    ]),
  );
  patientEditStatus.textContent = 'Saving patient details...';
  patientEditSubmit.disabled = true;
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/patients/${encodeURIComponent(patientCode)}`,
      {
        method: 'PUT',
        headers: {
          Authorization: 'Bearer ' + token,
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
      },
    );
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const result = await response.json();
    if (!response.ok) {
      const fieldHint = Array.isArray(result.fields) && result.fields.length
        ? ` Correct these fields: ${result.fields.join(', ')}.`
        : '';
      throw new Error(
        `${result.error || `Unable to save patient details (HTTP ${response.status}).`}${fieldHint}`,
      );
    }
    patientEditStatus.textContent = 'Patient details updated successfully.';
    await loadDashboardRecords(token);
  } catch (error) {
    patientEditStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
  } finally {
    patientEditSubmit.disabled = false;
  }
}

async function fetchDashboardRecordsPage(token, page) {
  const response = await fetch(
    `${API_BASE_URL}/api/doctor-dashboard/records?page=${page}&per_page=${pageSize}`,
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
  return validateDashboardRecordsPage(payload, page, pageSize);
}

async function loadDashboardRecords(token) {
  statusElement.textContent = 'Loading records...';
  statusElement.hidden = false;
  tableWrapper.hidden = true;
  pagination.hidden = true;
  countElement.textContent = '';
  tableBody.replaceChildren();

  try {
    const payload = await fetchDashboardRecordsPage(token, currentPage);
    totalPages = payload.pages;
    countElement.textContent = `${payload.total} patients`;
    if (payload.records.length === 0) {
      statusElement.textContent = 'No clinical patient records found.';
      return;
    }
    payload.records.forEach((record) => displayRecord(record, token));
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

function clearPatientHistory() {
  activePatient = null;
  patientHistory.hidden = true;
  patientDetails.replaceChildren();
  patientVisits.replaceChildren();
  patientSearchResults.replaceChildren();
}

function addDetail(details, label, value) {
  const term = document.createElement('dt');
  term.textContent = label;
  const description = document.createElement('dd');
  description.textContent = value == null || value === '' ? '—' : String(value);
  details.append(term, description);
}

function addReportActions(list, reports, token) {
  reports.forEach((report) => {
    const item = document.createElement('li');
    const label = document.createElement('span');
    label.textContent = `${report.original_name} (${displayDate(report.uploaded_at)}) `;
    item.append(label);
    [['View', 'view'], ['Download', 'download']].forEach(([text, action]) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'report-action';
      button.textContent = text;
      button.addEventListener('click', () =>
        openMedicalReport(report._id, action, token, report.original_name));
      item.append(button);
    });
    list.append(item);
  });
}

function renderHistoryDialog(patient, token) {
  historyDialogPatient.replaceChildren();
  addDetail(historyDialogPatient, 'Patient ID', patient.patient_code);
  addDetail(historyDialogPatient, 'Patient Name', patient.full_name);
  addDetail(historyDialogPatient, 'Visit Count', patient.visits.length);
  historyDialogVisits.replaceChildren();
  if (patient.visits.length === 0) {
    historyDialogStatus.textContent = 'No visits have been recorded for this patient.';
    historyDialogStatus.hidden = false;
    return;
  }

  historyDialogStatus.hidden = true;
  patient.visits.forEach((visit, index) => {
    const article = document.createElement('article');
    article.className = 'patient-visit history-dialog-visit';
    const heading = document.createElement('h3');
    heading.textContent = `Visit ${patient.visits.length - index} — ${displayDate(visit.treatment_date)}`;
    article.append(heading);

    const diagnosis = document.createElement('p');
    diagnosis.className = 'history-visit-diagnosis';
    diagnosis.textContent = `Diagnosis: ${visit.diagnosis || '—'}`;
    article.append(diagnosis);

    const prescriptionHeading = document.createElement('h4');
    prescriptionHeading.textContent = 'Prescription';
    article.append(prescriptionHeading);
    if (visit.medicines.length === 0) {
      const emptyPrescription = document.createElement('p');
      emptyPrescription.textContent = 'No prescription recorded.';
      article.append(emptyPrescription);
    } else {
      const medicines = document.createElement('ul');
      medicines.className = 'history-visit-list';
      visit.medicines.forEach((medicine) => {
        const item = document.createElement('li');
        item.textContent = [
          medicine.medicine_name,
          medicine.dose,
          medicine.frequency,
          medicine.duration,
        ].filter(Boolean).join(' — ');
        medicines.append(item);
      });
      article.append(medicines);
    }

    const reportsHeading = document.createElement('h4');
    reportsHeading.textContent = 'Reports';
    article.append(reportsHeading);
    if (visit.reports.length) {
      const reports = document.createElement('ul');
      reports.className = 'history-visit-list';
      addReportActions(reports, visit.reports, token);
      article.append(reports);
    } else {
      const emptyReports = document.createElement('p');
      emptyReports.textContent = 'No reports attached to this visit.';
      article.append(emptyReports);
    }

    const fullDetails = document.createElement('details');
    fullDetails.className = 'history-visit-full-details';
    const summary = document.createElement('summary');
    summary.textContent = 'Open full visit details';
    fullDetails.append(summary);
    const details = document.createElement('dl');
    details.className = 'visit-details';
    addDetail(details, 'Doctor Name', visit.doctor_name);
    addDetail(details, 'Hospital Name', visit.hospital_name);
    addDetail(details, 'Treatment Plan', visit.treatment);
    addDetail(details, 'Follow-up Date', displayDate(visit.follow_up_date));
    addDetail(details, 'Additional Instructions', visit.additional_instructions);
    fullDetails.append(details);
    article.append(fullDetails);
    historyDialogVisits.append(article);
  });
}

function renderPatientHistory(patient, token) {
  activePatient = patient;
  patientDetails.replaceChildren();
  addDetail(patientDetails, 'Patient ID', patient.patient_code);
  addDetail(patientDetails, 'Patient Full Name', patient.full_name);
  addDetail(patientDetails, 'Age', patient.age);
  addDetail(patientDetails, 'Gender', patient.gender);
  addDetail(patientDetails, 'Blood Group', patient.blood_group);
  addDetail(patientDetails, 'Mobile Number', patient.mobile_number);
  addDetail(patientDetails, 'Email Address', patient.email);
  patientVisits.replaceChildren();

  if (patient.visits.length === 0) {
    const emptyMessage = document.createElement('p');
    emptyMessage.textContent = 'No visits have been recorded for this patient.';
    patientVisits.append(emptyMessage);
  }
  patient.visits.forEach((visit) => {
    const article = document.createElement('article');
    article.className = 'patient-visit';
    const heading = document.createElement('h4');
    heading.textContent = `Visit — ${displayDate(visit.treatment_date)}`;
    article.append(heading);

    const visitDetails = document.createElement('dl');
    visitDetails.className = 'visit-details';
    addDetail(visitDetails, 'Doctor Name', visit.doctor_name);
    addDetail(visitDetails, 'Hospital Name', visit.hospital_name);
    addDetail(visitDetails, 'Diagnosis / Problem', visit.diagnosis);
    addDetail(visitDetails, 'Treatment Plan', visit.treatment);
    addDetail(visitDetails, 'Follow-up Date', displayDate(visit.follow_up_date));
    article.append(visitDetails);

    const medicineHeading = document.createElement('h5');
    medicineHeading.textContent = 'Prescription';
    article.append(medicineHeading);
    const medicineTable = document.createElement('table');
    medicineTable.className = 'medicine-table';
    const head = medicineTable.createTHead().insertRow();
    ['Medicine Name', 'Dose', 'Frequency', 'Duration'].forEach((label) => {
      const cell = document.createElement('th');
      cell.scope = 'col';
      cell.textContent = label;
      head.append(cell);
    });
    const body = medicineTable.createTBody();
    visit.medicines.forEach((medicine) => {
      const row = body.insertRow();
      ['medicine_name', 'dose', 'frequency', 'duration'].forEach((field) => {
        addCell(row, medicine[field]);
      });
    });
    article.append(medicineTable);
    if (visit.additional_instructions) {
      const instructions = document.createElement('p');
      instructions.className = 'visit-instructions';
      instructions.textContent = `Additional Instructions: ${visit.additional_instructions}`;
      article.append(instructions);
    }
    if (visit.reports?.length) {
      const reportHeading = document.createElement('h5');
      reportHeading.textContent = 'Medical Reports';
      article.append(reportHeading);
      const reportList = document.createElement('ul');
      reportList.className = 'visit-report-list';
      addReportActions(reportList, visit.reports, token);
      article.append(reportList);
    }
    const printButton = document.createElement('button');
    printButton.type = 'button';
    printButton.className = 'print-prescription-button';
    printButton.textContent = 'Print Prescription';
    printButton.addEventListener('click', () => printPrescription(patient, visit));
    article.append(printButton);
    patientVisits.append(article);
  });
  patientHistory.hidden = false;
}

async function loadPatientHistory(token, patientCode) {
  clearPatientHistory();
  patientSearchStatus.textContent = 'Searching patient records...';
  patientSearchStatus.hidden = false;
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/patients/${encodeURIComponent(patientCode)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Unable to search patient records (HTTP ${response.status}).`);
    }
    if (
      !payload.patient
      || !Array.isArray(payload.patient.visits)
      || payload.patient.visits.some((visit) => !Array.isArray(visit.medicines))
    ) {
      throw new Error('The server returned an invalid patient-history response.');
    }
    renderPatientHistory(payload.patient, token);
    patientSearchStatus.hidden = true;
  } catch (error) {
    patientSearchStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
  }
}

async function openPatientHistoryModal(token, patientCode, row) {
  historyReturnFocus = row;
  historyLoadSequence += 1;
  const requestSequence = historyLoadSequence;
  if (!historyDialog.open) {
    historyDialog.showModal();
  }
  document.querySelector('#history-dialog-title').textContent = 'Patient history';
  historyDialogPatient.replaceChildren();
  historyDialogVisits.replaceChildren();
  historyDialogStatus.textContent = 'Loading visit history...';
  historyDialogStatus.hidden = false;
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/patients/${encodeURIComponent(patientCode)}`,
      { headers: { Authorization: 'Bearer ' + token } },
    );
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Unable to retrieve patient history (HTTP ${response.status}).`);
    }
    if (
      !payload.patient
      || !Array.isArray(payload.patient.visits)
      || payload.patient.visits.some(
        (visit) => !Array.isArray(visit.medicines) || !Array.isArray(visit.reports),
      )
    ) {
      throw new Error('The server returned an invalid patient-history response.');
    }
    if (requestSequence !== historyLoadSequence || !historyDialog.open) {
      return;
    }
    document.querySelector('#history-dialog-title').textContent =
      `${payload.patient.full_name} — Visit History`;
    renderHistoryDialog(payload.patient, token);
  } catch (error) {
    if (requestSequence !== historyLoadSequence || !historyDialog.open) {
      return;
    }
    historyDialogStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
    historyDialogStatus.hidden = false;
  }
}

async function searchPatient(token) {
  const query = patientSearchInput.value.trim();
  clearPatientHistory();
  if (!query) {
    patientSearchStatus.textContent = 'Enter a Patient ID or patient name to search.';
    patientSearchStatus.hidden = false;
    return;
  }
  if (/^EHR-\d{10,20}$/i.test(query)) {
    await loadPatientHistory(token, query.toUpperCase());
    return;
  }
  if (query.length < 2) {
    patientSearchStatus.textContent = 'Enter at least two characters to search by name.';
    patientSearchStatus.hidden = false;
    return;
  }
  patientSearchStatus.textContent = 'Searching patient profiles...';
  patientSearchStatus.hidden = false;
  try {
    const response = await fetch(
      `${API_BASE_URL}/api/patients/search?q=${encodeURIComponent(query)}`,
      { headers: { Authorization: 'Bearer ' + token } },
    );
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || `Unable to search patients (HTTP ${response.status}).`);
    }
    if (!Array.isArray(payload.patients)) {
      throw new Error('The server returned an invalid patient-search response.');
    }
    if (payload.patients.length === 0) {
      patientSearchStatus.textContent = 'No matching patient profiles were found.';
      return;
    }
    patientSearchResults.replaceChildren();
    payload.patients.forEach((patient) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'patient-search-result';
      button.textContent = `${patient.full_name} — ${patient.patient_code}`;
      button.addEventListener('click', () => {
        patientSearchInput.value = patient.patient_code;
        loadPatientHistory(token, patient.patient_code);
      });
      patientSearchResults.append(button);
    });
    patientSearchStatus.textContent =
      `${payload.patients.length} matching profile${payload.patients.length === 1 ? '' : 's'} found.`;
    patientSearchStatus.hidden = false;
  } catch (error) {
    patientSearchStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
  }
}

function makeMedicineRow() {
  const row = document.createElement('fieldset');
  row.className = 'medicine-row';
  const fields = [
    ['medicine_name', 'Medicine Name'],
    ['dose', 'Dose'],
    ['frequency', 'Frequency'],
    ['duration', 'Duration'],
  ];
  fields.forEach(([name, labelText]) => {
    const label = document.createElement('label');
    label.textContent = labelText;
    const input = document.createElement('input');
    input.name = name;
    input.required = true;
    input.maxLength = name === 'medicine_name' ? 255 : 120;
    label.append(input);
    row.append(label);
  });
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'remove-medicine';
  remove.textContent = 'Remove';
  remove.setAttribute('aria-label', 'Remove medicine row');
  remove.addEventListener('click', () => {
    row.remove();
    updateMedicineRemoveButtons();
  });
  row.append(remove);
  medicineRows.append(row);
  updateMedicineRemoveButtons();
}

function updateMedicineRemoveButtons() {
  const buttons = medicineRows.querySelectorAll('.remove-medicine');
  buttons.forEach((button) => {
    button.disabled = buttons.length === 1;
  });
}

function localDateString() {
  const today = new Date();
  const month = String(today.getMonth() + 1).padStart(2, '0');
  const day = String(today.getDate()).padStart(2, '0');
  return `${today.getFullYear()}-${month}-${day}`;
}

function resetEhrForm() {
  addEhrForm.reset();
  editingPatient = null;
  newPatientFields.hidden = false;
  newPatientFields.disabled = false;
  existingPatientSummary.hidden = true;
  document.querySelector('#add-ehr-title').textContent = 'Add a patient and first visit';
  document.querySelector('#add-ehr-description').textContent =
    'Patient ID is generated by the database when the record is saved.';
  addEhrSubmit.textContent = 'Save patient and prescription';
  addEhrStatus.textContent = '';
  medicineRows.replaceChildren();
  makeMedicineRow();
  document.querySelector('#ehr-doctor-name').value = getDoctorSession()?.user?.name || '';
  document.querySelector('#ehr-patient-id').value = 'Generated when saved';
  document.querySelector('#ehr-treatment-date').value = localDateString();
}

function openNewPatientForm() {
  resetEhrForm();
  addEhrSection.hidden = false;
  addEhrAction.setAttribute('aria-expanded', 'true');
  document.querySelector('#ehr-full-name').focus({ preventScroll: true });
}

function openVisitForm(patient) {
  resetEhrForm();
  editingPatient = patient;
  newPatientFields.hidden = true;
  newPatientFields.disabled = true;
  document.querySelector('#existing-patient-name').textContent = patient.full_name;
  document.querySelector('#existing-patient-code').textContent = patient.patient_code;
  existingPatientSummary.hidden = false;
  document.querySelector('#add-ehr-title').textContent = `Add visit for ${patient.full_name}`;
  document.querySelector('#add-ehr-description').textContent =
    `Patient ID ${patient.patient_code} stays the same for every visit.`;
  addEhrSubmit.textContent = 'Save visit';
  addEhrSection.hidden = false;
  addEhrAction.setAttribute('aria-expanded', 'true');
  document.querySelector('#ehr-diagnosis').focus({ preventScroll: true });
}

function collectVisitPayload() {
  return {
    treatment_date: document.querySelector('#ehr-treatment-date').value,
    hospital_name: document.querySelector('#ehr-hospital-name').value.trim(),
    diagnosis: document.querySelector('#ehr-diagnosis').value.trim(),
    treatment: document.querySelector('#ehr-treatment').value.trim(),
    medicines: [...medicineRows.querySelectorAll('.medicine-row')].map((row) => ({
      medicine_name: row.querySelector('[name="medicine_name"]').value.trim(),
      dose: row.querySelector('[name="dose"]').value.trim(),
      frequency: row.querySelector('[name="frequency"]').value.trim(),
      duration: row.querySelector('[name="duration"]').value.trim(),
    })),
    additional_instructions: document.querySelector('#ehr-additional-instructions').value.trim(),
    follow_up_date: document.querySelector('#ehr-follow-up-date').value,
  };
}

async function uploadVisitReports(token, patientCode, visitId, files) {
  if (files.length === 0) {
    return;
  }
  const formData = new FormData();
  files.forEach((file) => formData.append('reports', file));
  const response = await fetch(
    `${API_BASE_URL}/api/patients/${encodeURIComponent(patientCode)}/visits/${visitId}/reports`,
    { method: 'POST', headers: { Authorization: 'Bearer ' + token }, body: formData },
  );
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error || `Unable to upload medical reports (HTTP ${response.status}).`);
  }
}

async function submitEhr(token) {
  if (!addEhrForm.reportValidity()) {
    return;
  }
  const visit = collectVisitPayload();
  const isNewPatient = editingPatient === null;
  const payload = isNewPatient
    ? {
      full_name: document.querySelector('#ehr-full-name').value.trim(),
      age: document.querySelector('#ehr-age').value,
      gender: document.querySelector('#ehr-gender').value,
      blood_group: document.querySelector('#ehr-blood-group').value,
      mobile_number: document.querySelector('#ehr-mobile-number').value.trim(),
      email: document.querySelector('#ehr-email').value.trim(),
      ...visit,
    }
    : visit;
  const reportFiles = [...document.querySelector('#ehr-medical-reports').files];

  addEhrStatus.textContent = isNewPatient ? 'Saving patient and first visit...' : 'Saving visit...';
  addEhrSubmit.disabled = true;
  try {
    const url = isNewPatient
      ? `${API_BASE_URL}/api/patients`
      : `${API_BASE_URL}/api/patients/${encodeURIComponent(editingPatient.patient_code)}/visits`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });
    const contentType = response.headers.get('content-type') || '';
    if (!contentType.includes('application/json')) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    const result = await response.json();
    if (!response.ok) {
      const fieldHint = Array.isArray(result.fields) && result.fields.length
        ? ` Correct these fields: ${result.fields.join(', ')}.`
        : '';
      throw new Error(
        `${result.error || `Unable to save the visit (HTTP ${response.status}).`}${fieldHint}`,
      );
    }

    const patientCode = isNewPatient ? result.patient_code : editingPatient.patient_code;
    if (isNewPatient) {
      document.querySelector('#ehr-patient-id').value = patientCode;
    }
    let reportUploadMessage = '';
    try {
      await uploadVisitReports(token, patientCode, result.visit_id, reportFiles);
    } catch (error) {
      reportUploadMessage = ` Medical report upload failed: ${error.message}`;
    }
    addEhrStatus.textContent = isNewPatient
      ? `Patient ${patientCode} and visit ${result.visit_id} saved successfully.${reportUploadMessage}`
      : `Visit ${result.visit_id} saved successfully.${reportUploadMessage}`;
    currentPage = 1;
    await loadDashboardRecords(token);
    patientSearchInput.value = patientCode;
    patientSearchSection.hidden = false;
    patientSearchAction.setAttribute('aria-expanded', 'true');
    await searchPatient(token);
  } catch (error) {
    addEhrStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
  } finally {
    addEhrSubmit.disabled = false;
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
    const response = await fetch(
      `${API_BASE_URL}/api/reports/${encodeURIComponent(reportId)}/${action}`,
      { headers: { Authorization: 'Bearer ' + token } },
    );
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
    patientSearchStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
    patientSearchStatus.hidden = false;
  }
}

function appendPrintLine(parent, label, value) {
  const line = document.createElement('p');
  const strong = document.createElement('strong');
  strong.textContent = `${label}: `;
  line.append(strong, document.createTextNode(value == null || value === '' ? '—' : String(value)));
  parent.append(line);
}

function printPrescription(patient, visit) {
  const printout = document.querySelector('#prescription-printout');
  printout.replaceChildren();
  const heading = document.createElement('h1');
  heading.textContent = 'Prescription';
  printout.append(heading);
  appendPrintLine(printout, 'Patient ID', patient.patient_code);
  appendPrintLine(printout, 'Patient Full Name', patient.full_name);
  appendPrintLine(printout, 'Age / Gender', `${patient.age} / ${patient.gender}`);
  appendPrintLine(printout, 'Blood Group', patient.blood_group);
  appendPrintLine(printout, 'Mobile Number', patient.mobile_number);
  appendPrintLine(printout, 'Doctor Name', visit.doctor_name);
  appendPrintLine(printout, 'Treatment Date', displayDate(visit.treatment_date));
  appendPrintLine(printout, 'Hospital Name', visit.hospital_name);
  appendPrintLine(printout, 'Diagnosis / Problem', visit.diagnosis);

  const table = document.createElement('table');
  table.className = 'medicine-table';
  const header = table.createTHead().insertRow();
  ['Medicine Name', 'Dose', 'Frequency', 'Duration'].forEach((value) => {
    const cell = document.createElement('th');
    cell.scope = 'col';
    cell.textContent = value;
    header.append(cell);
  });
  const body = table.createTBody();
  visit.medicines.forEach((medicine) => {
    const row = body.insertRow();
    ['medicine_name', 'dose', 'frequency', 'duration'].forEach((field) => {
      addCell(row, medicine[field]);
    });
  });
  printout.append(table);
  appendPrintLine(printout, 'Additional Instructions', visit.additional_instructions);
  appendPrintLine(printout, 'Follow-up Date', displayDate(visit.follow_up_date));
  document.body.classList.add('printing-prescription');
  printout.hidden = false;
  window.addEventListener('afterprint', () => {
    document.body.classList.remove('printing-prescription');
    printout.hidden = true;
  }, { once: true });
  window.print();
}

async function verifyBlockchainApi(token, recordId = null) {
  const query = recordId ? `?record_id=${encodeURIComponent(recordId)}` : '';
  const response = await fetch(`${API_BASE_URL}/api/blockchain/verify${query}`, {
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
    throw new Error('Your session has expired. Please sign in again.');
  }
  if (response.status === 403) {
    throw new Error('Access forbidden: Doctor or Admin role is required to verify blockchain integrity.');
  }
  if (response.status === 404) {
    throw new Error(
      recordId
        ? `EHR Record #${recordId} was not found in the database. Note: Record ID is the visit Record ID (e.g. 4), not the Patient ID (e.g. EHR-0000000002).`
        : (payload.error || 'EHR record not found.')
    );
  }
  if (response.status === 503) {
    throw new Error(
      payload.error || 'Unable to verify blockchain integrity. The blockchain service or database is unavailable.'
    );
  }
  if (!response.ok) {
    throw new Error(payload.error || `Unable to verify blockchain integrity (HTTP ${response.status}).`);
  }
  return validateBlockchainVerifyResponse(payload);
}

function clearBlockchainResults() {
  if (blockchainVerifyResults) {
    blockchainVerifyResults.hidden = true;
    blockchainVerifyResults.style.display = 'none';
  }
  const banner = document.querySelector('#blockchain-banner');
  if (banner) {
    banner.className = 'blockchain-banner';
  }
  const bannerIcon = document.querySelector('#blockchain-banner-icon');
  if (bannerIcon) bannerIcon.textContent = '';
  const bannerTitle = document.querySelector('#blockchain-banner-title');
  if (bannerTitle) bannerTitle.textContent = '';
  const bannerMessage = document.querySelector('#blockchain-banner-message');
  if (bannerMessage) bannerMessage.textContent = '';

  const metricChain = document.querySelector('#metric-chain-status');
  if (metricChain) {
    metricChain.textContent = '—';
    metricChain.className = 'metric-value';
  }
  const metricChecked = document.querySelector('#metric-records-checked');
  if (metricChecked) metricChecked.textContent = '—';
  const metricValid = document.querySelector('#metric-records-valid');
  if (metricValid) metricValid.textContent = '—';
  const metricMismatched = document.querySelector('#metric-records-mismatched');
  if (metricMismatched) {
    metricMismatched.textContent = '—';
    metricMismatched.className = 'metric-value';
  }
  const metricUntracked = document.querySelector('#metric-records-untracked');
  if (metricUntracked) {
    metricUntracked.textContent = '—';
    metricUntracked.className = 'metric-value';
  }

  const tipBox = document.querySelector('#blockchain-tip-box');
  if (tipBox) tipBox.hidden = true;
  const tipHash = document.querySelector('#blockchain-tip-hash');
  if (tipHash) {
    tipHash.textContent = '—';
    tipHash.title = '';
  }

  const recordsContainer = document.querySelector('#blockchain-records-container');
  if (recordsContainer) recordsContainer.hidden = true;
  const recordsBody = document.querySelector('#blockchain-records-body');
  if (recordsBody) recordsBody.replaceChildren();
}

function renderBlockchainResults(result) {
  clearBlockchainResults();
  const evaluation = evaluateBlockchainState(result);
  const banner = document.querySelector('#blockchain-banner');
  const bannerIcon = document.querySelector('#blockchain-banner-icon');
  const bannerTitle = document.querySelector('#blockchain-banner-title');
  const bannerMessage = document.querySelector('#blockchain-banner-message');

  banner.className = `blockchain-banner banner-${evaluation.severity}`;
  bannerIcon.textContent = evaluation.severity === 'success' ? '✓' : evaluation.severity === 'warning' ? '!' : '⚠';
  bannerTitle.textContent = evaluation.title;
  bannerMessage.textContent = evaluation.message;

  const metricChain = document.querySelector('#metric-chain-status');
  metricChain.textContent = result.chain_valid ? 'Valid & Intact' : 'Invalid / Broken';
  metricChain.className = `metric-value ${result.chain_valid ? 'status-valid' : 'status-invalid'}`;

  document.querySelector('#metric-records-checked').textContent = String(result.records_checked);
  document.querySelector('#metric-records-valid').textContent = String(result.records_valid);

  const metricMismatched = document.querySelector('#metric-records-mismatched');
  metricMismatched.textContent = String(result.mismatched_records);
  metricMismatched.className = `metric-value ${result.mismatched_records > 0 ? 'status-invalid' : ''}`;

  const metricUntracked = document.querySelector('#metric-records-untracked');
  metricUntracked.textContent = String(result.untracked_records);
  metricUntracked.className = `metric-value ${result.untracked_records > 0 ? 'status-warn' : ''}`;

  const tipBox = document.querySelector('#blockchain-tip-box');
  const tipHashElement = document.querySelector('#blockchain-tip-hash');
  if (result.tip_hash) {
    tipHashElement.textContent = result.tip_hash;
    tipHashElement.title = result.tip_hash;
    if (tipBox) tipBox.hidden = false;
  } else {
    tipHashElement.textContent = 'None';
    tipHashElement.title = '';
    if (tipBox) tipBox.hidden = true;
  }

  const recordsContainer = document.querySelector('#blockchain-records-container');
  const recordsBody = document.querySelector('#blockchain-records-body');
  recordsBody.replaceChildren();

  if (Array.isArray(result.records) && result.records.length > 0) {
    recordsContainer.hidden = false;
    result.records.forEach((rec) => {
      const row = document.createElement('tr');

      const idCell = document.createElement('td');
      idCell.textContent = `#${rec.record_id}`;
      row.append(idCell);

      const statusCell = document.createElement('td');
      const badge = document.createElement('span');
      badge.className = `blockchain-badge badge-${rec.status}`;
      badge.textContent = rec.status;
      statusCell.append(badge);
      row.append(statusCell);

      const hashCell = document.createElement('td');
      const currentCode = document.createElement('code');
      currentCode.className = 'hash-code';
      currentCode.textContent = formatHashPreview(rec.record_hash);
      currentCode.title = rec.record_hash || '';
      hashCell.append(currentCode);
      row.append(hashCell);

      const auditCell = document.createElement('td');
      const auditCode = document.createElement('code');
      auditCode.className = 'hash-code';
      auditCode.textContent = formatHashPreview(rec.audited_hash);
      auditCode.title = rec.audited_hash || 'Not audited in blockchain';
      auditCell.append(auditCode);
      row.append(auditCell);

      recordsBody.append(row);
    });
  } else {
    recordsContainer.hidden = true;
  }

  blockchainVerifyResults.hidden = false;
  blockchainVerifyResults.style.display = 'grid';
}

async function runBlockchainVerification(token) {
  const { recordId, error: parseError } = parseRecordIdInput(blockchainRecordIdInput.value);
  if (parseError) {
    clearBlockchainResults();
    blockchainVerifyStatus.textContent = parseError;
    blockchainVerifyStatus.className = 'blockchain-status-error';
    blockchainRecordIdInput.focus();
    return;
  }

  blockchainVerifySubmit.disabled = true;
  clearBlockchainResults();
  blockchainVerifyStatus.className = 'blockchain-status-loading';
  blockchainVerifyStatus.textContent = recordId
    ? `Verifying blockchain audit for Record #${recordId}...`
    : 'Auditing blockchain integrity and validating record hashes...';

  try {
    const result = await verifyBlockchainApi(token, recordId);
    blockchainVerifyStatus.textContent = 'Verification completed.';
    blockchainVerifyStatus.className = 'blockchain-status-success';
    renderBlockchainResults(result);
  } catch (error) {
    clearBlockchainResults();
    blockchainVerifyStatus.textContent = error instanceof TypeError
      ? 'The backend is unavailable. Start Flask and try again.'
      : error.message;
    blockchainVerifyStatus.className = 'blockchain-status-error';
  } finally {
    blockchainVerifySubmit.disabled = false;
  }
}

const session = getDoctorSession();
if (!session) {
  sessionStorage.removeItem('ehrAuthToken');
  sessionStorage.removeItem('ehrUser');
  window.location.replace('login.HTML');
} else {
  const { token } = session;
  document.querySelector('#sign-out').addEventListener('click', () => {
    sessionStorage.removeItem('ehrAuthToken');
    sessionStorage.removeItem('ehrUser');
  });
  document.querySelector('#history-dialog-close').addEventListener('click', () => {
    historyDialog.close();
  });
  historyDialog.addEventListener('click', (event) => {
    if (event.target === historyDialog) {
      historyDialog.close();
    }
  });
  historyDialog.addEventListener('close', () => {
    historyLoadSequence += 1;
    historyReturnFocus?.focus();
    historyReturnFocus = null;
  });
  document.querySelector('#patient-edit-close').addEventListener('click', () => {
    patientEditDialog.close();
  });
  document.querySelector('#patient-edit-cancel').addEventListener('click', () => {
    patientEditDialog.close();
  });
  patientEditForm.addEventListener('submit', (event) => {
    event.preventDefault();
    submitPatientEdit(token);
  });
  patientSearchAction.addEventListener('click', () => {
    const isOpening = patientSearchSection.hidden;
    patientSearchSection.hidden = !isOpening;
    patientSearchAction.setAttribute('aria-expanded', String(isOpening));
    if (isOpening) {
      patientSearchInput.focus({ preventScroll: true });
    }
  });
  document.querySelector('#patient-search-close').addEventListener('click', () => {
    patientSearchSection.hidden = true;
    patientSearchAction.setAttribute('aria-expanded', 'false');
    patientSearchAction.focus({ preventScroll: true });
  });
  document.querySelector('#patient-search-submit').addEventListener('click', () => searchPatient(token));
  patientSearchInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      searchPatient(token);
    }
  });
  document.querySelector('#patient-search-clear').addEventListener('click', () => {
    patientSearchInput.value = '';
    clearPatientHistory();
    patientSearchStatus.textContent = 'Enter a Patient ID or patient name to search.';
    patientSearchStatus.hidden = false;
    patientSearchInput.focus();
  });
  addEhrAction.addEventListener('click', openNewPatientForm);
  document.querySelector('#add-ehr-close').addEventListener('click', () => {
    addEhrSection.hidden = true;
    addEhrAction.setAttribute('aria-expanded', 'false');
    addEhrAction.focus({ preventScroll: true });
  });
  document.querySelector('#add-ehr-cancel').addEventListener('click', () => {
    resetEhrForm();
    addEhrSection.hidden = true;
    addEhrAction.setAttribute('aria-expanded', 'false');
    addEhrAction.focus({ preventScroll: true });
  });
  document.querySelector('#add-patient-visit').addEventListener('click', () => {
    if (activePatient) {
      openVisitForm(activePatient);
    }
  });
  document.querySelector('#add-medicine').addEventListener('click', makeMedicineRow);
  addEhrForm.addEventListener('submit', (event) => {
    event.preventDefault();
    submitEhr(token);
  });
  previousButton.addEventListener('click', () => {
    if (currentPage > 1) {
      currentPage -= 1;
      loadDashboardRecords(token);
    }
  });
  nextButton.addEventListener('click', () => {
    if (currentPage < totalPages) {
      currentPage += 1;
      loadDashboardRecords(token);
    }
  });
  blockchainVerifyAction.addEventListener('click', () => {
    const isOpening = blockchainVerifySection.hidden;
    blockchainVerifySection.hidden = !isOpening;
    blockchainVerifyAction.setAttribute('aria-expanded', String(isOpening));
    if (isOpening) {
      blockchainVerifySubmit.focus({ preventScroll: true });
    }
  });
  blockchainVerifyClose.addEventListener('click', () => {
    blockchainVerifySection.hidden = true;
    blockchainVerifyAction.setAttribute('aria-expanded', 'false');
    blockchainVerifyAction.focus({ preventScroll: true });
  });
  blockchainVerifySubmit.addEventListener('click', () => runBlockchainVerification(token));
  blockchainRecordIdInput.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      runBlockchainVerification(token);
    }
  });
  blockchainVerifyClear.addEventListener('click', () => {
    blockchainRecordIdInput.value = '';
    blockchainVerifyStatus.textContent = 'Click "Verify Blockchain" to start the cryptographic audit.';
    blockchainVerifyStatus.className = '';
    clearBlockchainResults();
    blockchainRecordIdInput.focus();
  });
  resetEhrForm();
  loadDashboardRecords(token);
}
