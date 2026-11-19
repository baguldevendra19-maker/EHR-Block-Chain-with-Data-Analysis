const assert = require('node:assert/strict');
const test = require('node:test');
const {
  createPatientIdCell,
  validateDashboardRecordsPage,
} = require('../dashboard-records.js');

function record(patientCode, sourceId, visitCount) {
  return {
    source_type: 'Manually Added EHR',
    source_id: sourceId,
    patient_code: patientCode,
    visit_count: visitCount,
  };
}

test('accepts unique real-patient summaries with latest Record IDs and visit counts', () => {
  const payload = {
    real_ehr_only: true,
    records: [
      record('EHR-0000000042', '103', 3),
      record('EHR-0000000043', '104', 1),
    ],
    page: 1,
    per_page: 25,
    total: 2,
    pages: 1,
  };

  assert.equal(validateDashboardRecordsPage(payload, 1, 25), payload);
});

test('accepts a well-formed empty dashboard response', () => {
  const payload = {
    real_ehr_only: true,
    records: [],
    page: 1,
    per_page: 25,
    total: 0,
    pages: 0,
  };

  assert.equal(validateDashboardRecordsPage(payload, 1, 25), payload);
});

test('identifies the old combined dashboard response as an outdated API', () => {
  assert.throws(
    () => validateDashboardRecordsPage({
      combined_data: true,
      records: [],
      page: 1,
      per_page: 25,
      total: 55500,
      pages: 2220,
    }, 1, 25),
    /outdated or returning mixed data/,
  );
});

test('rejects duplicate Patient IDs and incomplete page results', () => {
  const duplicatePatients = {
    real_ehr_only: true,
    records: [
      record('EHR-0000000042', '103', 3),
      record('EHR-0000000042', '104', 4),
    ],
    page: 1,
    per_page: 25,
    total: 2,
    pages: 1,
  };
  assert.throws(
    () => validateDashboardRecordsPage(duplicatePatients, 1, 25),
    /invalid dashboard-record response/,
  );

  const incompletePage = {
    ...duplicatePatients,
    records: [record('EHR-0000000042', '103', 3)],
  };
  assert.throws(
    () => validateDashboardRecordsPage(incompletePage, 1, 25),
    /invalid dashboard-record response/,
  );
});

test('renders the Patient ID and opens the same patient history on click or keyboard', () => {
  const elements = [];
  const document = {
    createElement(tagName) {
      const element = {
        tagName,
        attributes: {},
        handlers: {},
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        addEventListener(name, handler) {
          this.handlers[name] = handler;
        },
      };
      elements.push(element);
      return element;
    },
  };
  const row = document.createElement('tr');
  const recordSummary = record('EHR-0000000042', '103', 3);
  recordSummary.full_name = 'Patient One';
  const openedPatients = [];
  const cell = createPatientIdCell(
    document,
    row,
    recordSummary,
    (patientCode, patientRow) => openedPatients.push([patientCode, patientRow]),
  );

  assert.equal(cell.textContent, 'EHR-0000000042');
  assert.equal(row.attributes.role, 'button');
  row.handlers.click();
  row.handlers.keydown({ key: 'Enter', preventDefault() {} });
  assert.deepEqual(openedPatients, [
    ['EHR-0000000042', row],
    ['EHR-0000000042', row],
  ]);
});
