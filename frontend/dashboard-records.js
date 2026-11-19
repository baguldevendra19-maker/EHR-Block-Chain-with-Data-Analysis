function validateDashboardRecordsPage(payload, page, pageSize) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('The server returned an invalid dashboard-record response.');
  }
  if (payload.real_ehr_only !== true) {
    throw new Error(
      'The dashboard API is outdated or returning mixed data. Restart the backend server to load the real-EHR-only endpoint.',
    );
  }

  const expectedPages = Number.isInteger(payload.total) && payload.total >= 0
    ? Math.ceil(payload.total / pageSize)
    : -1;
  if (
    !Array.isArray(payload.records)
    || !Number.isInteger(payload.total)
    || payload.total < 0
    || payload.page !== page
    || payload.pages !== expectedPages
    || payload.per_page !== pageSize
  ) {
    throw new Error('The server returned an invalid dashboard-record response.');
  }

  const patientCodes = new Set();
  const invalidPatientSummaries = payload.records.some((record) => {
    if (
      !record
      || record.source_type !== 'Manually Added EHR'
      || typeof record.source_id !== 'string'
      || typeof record.patient_code !== 'string'
      || !record.patient_code
      || !Number.isInteger(record.visit_count)
      || record.visit_count < 1
      || patientCodes.has(record.patient_code)
    ) {
      return true;
    }
    patientCodes.add(record.patient_code);
    return false;
  });
  const expectedRecordCount = Math.max(
    0,
    Math.min(pageSize, payload.total - (page - 1) * pageSize),
  );
  if (invalidPatientSummaries || payload.records.length !== expectedRecordCount) {
    throw new Error('The server returned an invalid dashboard-record response.');
  }
  return payload;
}

function createPatientIdCell(document, row, record, onOpenHistory) {
  const cell = document.createElement('td');
  cell.textContent = record.patient_code == null ? '' : String(record.patient_code);
  if (!record.patient_code) {
    return cell;
  }

  row.className = 'patient-record-row';
  row.tabIndex = 0;
  row.setAttribute('role', 'button');
  row.setAttribute(
    'aria-label',
    `Open visit history for ${record.full_name}, Patient ID ${record.patient_code}, ${record.visit_count} visits`,
  );
  const openHistory = () => onOpenHistory(record.patient_code, row);
  row.addEventListener('click', openHistory);
  row.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      openHistory();
    }
  });
  return cell;
}

function validateBlockchainVerifyResponse(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('The server returned an invalid blockchain verification response.');
  }
  if (
    typeof payload.chain_valid !== 'boolean'
    || typeof payload.integrity_valid !== 'boolean'
    || typeof payload.record_hashes_valid !== 'boolean'
    || !Number.isInteger(payload.records_checked)
    || payload.records_checked < 0
    || !Number.isInteger(payload.records_valid)
    || payload.records_valid < 0
    || !Number.isInteger(payload.untracked_records)
    || payload.untracked_records < 0
    || !Number.isInteger(payload.mismatched_records)
    || payload.mismatched_records < 0
    || !Array.isArray(payload.records)
  ) {
    throw new Error('The server returned an invalid blockchain verification response.');
  }
  return payload;
}

function evaluateBlockchainState(result) {
  if (!result || typeof result !== 'object') {
    return {
      state: 'error',
      severity: 'error',
      title: 'Verification Failed',
      message: 'Invalid verification result received.',
    };
  }
  if (!result.chain_valid) {
    return {
      state: 'invalid-chain',
      severity: 'error',
      title: 'Blockchain Integrity Compromised',
      message: 'The cryptographic chain sequence or persisted state is invalid. Blocks do not link correctly.',
    };
  }
  if (result.mismatched_records > 0) {
    return {
      state: 'record-mismatch',
      severity: 'error',
      title: 'Record Content Mismatch Detected',
      message: `${result.mismatched_records} record(s) do not match their audited blockchain hashes. Data tampering detected.`,
    };
  }
  if (result.untracked_records > 0) {
    return {
      state: 'untracked-records',
      severity: 'warning',
      title: 'Untracked Records Detected',
      message: `${result.untracked_records} record(s) exist in the database without blockchain audit blocks.`,
    };
  }
  if (result.integrity_valid) {
    return {
      state: 'success',
      severity: 'success',
      title: 'Blockchain Verified & Secure',
      message: `All ${result.records_checked} record(s) cryptographically match their immutable blockchain audit hashes.`,
    };
  }
  return {
    state: 'inconclusive',
    severity: 'error',
    title: 'Verification Inconclusive',
    message: 'Blockchain verification could not confirm record integrity.',
  };
}

function formatHashPreview(hash, length = 16) {
  if (typeof hash !== 'string' || !hash.trim()) {
    return '—';
  }
  const clean = hash.trim();
  if (clean.length <= length) {
    return clean;
  }
  return `${clean.slice(0, 8)}...${clean.slice(-6)}`;
}

function parseRecordIdInput(rawInput) {
  if (rawInput == null) {
    return { recordId: null, error: null };
  }
  const trimmed = String(rawInput).trim();
  if (trimmed === '') {
    return { recordId: null, error: null };
  }
  if (!/^\d+$/.test(trimmed)) {
    return {
      recordId: null,
      error: 'Enter a valid positive integer Record ID (e.g. 1). Note: Record ID is the visit Record ID, not the Patient ID.',
    };
  }
  const parsed = parseInt(trimmed, 10);
  if (!Number.isInteger(parsed) || parsed <= 0) {
    return {
      recordId: null,
      error: 'Record ID must be greater than 0.',
    };
  }
  return { recordId: parsed, error: null };
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    createPatientIdCell,
    validateDashboardRecordsPage,
    validateBlockchainVerifyResponse,
    evaluateBlockchainState,
    formatHashPreview,
    parseRecordIdInput,
  };
}
