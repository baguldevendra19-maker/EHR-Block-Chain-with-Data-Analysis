const assert = require('node:assert/strict');
const test = require('node:test');
const {
  validateBlockchainVerifyResponse,
  evaluateBlockchainState,
  formatHashPreview,
  parseRecordIdInput,
} = require('../dashboard-records.js');

test('parseRecordIdInput parses canonical integers and normalizes inputs with leading zeros', () => {
  assert.deepEqual(parseRecordIdInput('2'), { recordId: 2, error: null });
  assert.deepEqual(parseRecordIdInput('002'), { recordId: 2, error: null });
  assert.deepEqual(parseRecordIdInput('  042  '), { recordId: 42, error: null });
  assert.deepEqual(parseRecordIdInput(''), { recordId: null, error: null });
  assert.deepEqual(parseRecordIdInput('   '), { recordId: null, error: null });
  assert.deepEqual(parseRecordIdInput(null), { recordId: null, error: null });
});

test('parseRecordIdInput rejects invalid, non-digit, zero, and negative inputs', () => {
  assert.match(parseRecordIdInput('0').error, /greater than 0/);
  assert.match(parseRecordIdInput('-5').error, /valid positive integer/);
  assert.match(parseRecordIdInput('abc').error, /valid positive integer/);
  assert.match(parseRecordIdInput('1.5').error, /valid positive integer/);
  assert.match(parseRecordIdInput('EHR-0000000002').error, /not the Patient ID/);
});

test('accepts valid full blockchain verification response', () => {
  const payload = {
    chain_valid: true,
    records_checked: 5,
    records_valid: 5,
    untracked_records: 0,
    mismatched_records: 0,
    record_hashes_valid: true,
    integrity_valid: true,
    tip_hash: 'a'.repeat(64),
    records: [],
  };

  assert.equal(validateBlockchainVerifyResponse(payload), payload);
});

test('accepts valid single-record verification response with audited record detail', () => {
  const payload = {
    chain_valid: true,
    records_checked: 1,
    records_valid: 1,
    untracked_records: 0,
    mismatched_records: 0,
    record_hashes_valid: true,
    integrity_valid: true,
    tip_hash: 'a'.repeat(64),
    records: [{
      record_id: '42',
      record_hash: 'b'.repeat(64),
      audited_hash: 'b'.repeat(64),
      status: 'valid',
    }],
  };

  assert.equal(validateBlockchainVerifyResponse(payload), payload);
});

test('rejects malformed or incomplete blockchain verification payloads', () => {
  assert.throws(
    () => validateBlockchainVerifyResponse(null),
    /invalid blockchain verification response/,
  );
  assert.throws(
    () => validateBlockchainVerifyResponse({ chain_valid: true }),
    /invalid blockchain verification response/,
  );
  assert.throws(
    () => validateBlockchainVerifyResponse({
      chain_valid: 'true',
      integrity_valid: true,
      record_hashes_valid: true,
      records_checked: 1,
      records_valid: 1,
      untracked_records: 0,
      mismatched_records: 0,
      records: [],
    }),
    /invalid blockchain verification response/,
  );
});

test('evaluates success state when all records and chain are intact', () => {
  const evaluation = evaluateBlockchainState({
    chain_valid: true,
    integrity_valid: true,
    mismatched_records: 0,
    untracked_records: 0,
    records_checked: 10,
    records_valid: 10,
  });

  assert.equal(evaluation.state, 'success');
  assert.equal(evaluation.severity, 'success');
  assert.match(evaluation.title, /Verified & Secure/i);
});

test('evaluates invalid-chain state when cryptographic chain is broken', () => {
  const evaluation = evaluateBlockchainState({
    chain_valid: false,
    integrity_valid: false,
    mismatched_records: 0,
    untracked_records: 0,
    records_checked: 10,
    records_valid: 10,
  });

  assert.equal(evaluation.state, 'invalid-chain');
  assert.equal(evaluation.severity, 'error');
  assert.match(evaluation.title, /Compromised/i);
});

test('evaluates record-mismatch state when record contents do not match audited hash', () => {
  const evaluation = evaluateBlockchainState({
    chain_valid: true,
    integrity_valid: false,
    mismatched_records: 2,
    untracked_records: 0,
    records_checked: 10,
    records_valid: 8,
  });

  assert.equal(evaluation.state, 'record-mismatch');
  assert.equal(evaluation.severity, 'error');
  assert.match(evaluation.message, /2 record\(s\) do not match/);
});

test('evaluates untracked-records state when database contains unaudited records', () => {
  const evaluation = evaluateBlockchainState({
    chain_valid: true,
    integrity_valid: false,
    mismatched_records: 0,
    untracked_records: 3,
    records_checked: 10,
    records_valid: 7,
  });

  assert.equal(evaluation.state, 'untracked-records');
  assert.equal(evaluation.severity, 'warning');
  assert.match(evaluation.message, /3 record\(s\) exist in the database without blockchain audit/);
});

test('formats hash previews with ellipsis for display readability', () => {
  const fullHash = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
  const preview = formatHashPreview(fullHash);

  assert.equal(preview, 'e3b0c442...52b855');
  assert.equal(formatHashPreview('short'), 'short');
  assert.equal(formatHashPreview(''), '—');
  assert.equal(formatHashPreview(null), '—');
});
