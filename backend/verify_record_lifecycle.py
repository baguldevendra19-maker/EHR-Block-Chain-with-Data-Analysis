"""Safe, isolated end-to-end verification of the EHR record lifecycle.

This script executes against a completely isolated test database
('ehr_blockchain_lifecycle_test') to verify:
1. Genesis initialization
2. EHR record creation with linked blockchain audit block (action='create')
3. Blockchain integrity validation after creation
4. EHR record update with linked blockchain audit block (action='update')
5. Blockchain integrity validation after update
6. Tamper detection: out-of-band record alteration detected as mismatch
7. Complete cleanup of the test database
8. Invariance check on the production/demo database ('ehr_blockchain')
"""

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
import mysql.connector

# Ensure backend package is importable
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))
load_dotenv(BASE_DIR / ".env")

from backend.blockchain import Block, Blockchain, calculate_record_hash
from backend.setup_mysql import (
    DiagnosticCursor,
    SCHEMA,
    ensure_blockchain_action_enum,
    initialize_blockchain_genesis,
)
from backend.app import (
    add_patient_visit,
    create_app,
    update_record_fields,
    verify_blockchain,
)

ISOLATED_DB = "ehr_blockchain_lifecycle_test"
PROD_DB = os.getenv("MYSQL_DATABASE", "ehr_blockchain")


def get_mysql_connection(database=None):
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=database,
    )


def snapshot_database(database_name):
    conn = get_mysql_connection(database_name)
    cur = conn.cursor(dictionary=True)

    cur.execute("SELECT id, medical_condition, diagnosis, treatment, updated_at FROM ehr_records ORDER BY id")
    records = cur.fetchall()

    cur.execute("SELECT block_index, record_id, action, actor_role, previous_hash, block_hash FROM blockchain_blocks ORDER BY block_index")
    blocks = cur.fetchall()

    cur.execute("SELECT id, last_block_index, last_block_hash FROM blockchain_state")
    state = cur.fetchall()

    cur.close()
    conn.close()
    return {"records": records, "blocks": blocks, "state": state}


def run_lifecycle_verification():
    print("=" * 70)
    print("EHR RECORD LIFECYCLE & AUDIT BLOCK VERIFICATION")
    print("=" * 70)

    # ---------------------------------------------------------
    # STEP 0: Safety Pre-check on Production/Demo Database
    # ---------------------------------------------------------
    print(f"\n[Step 0] Capturing pre-test snapshot of production database '{PROD_DB}'...")
    prod_pre = snapshot_database(PROD_DB)
    print(f"  - Demo EHR records count: {len(prod_pre['records'])} (IDs: {[r['id'] for r in prod_pre['records']]})")
    print(f"  - Blockchain blocks count: {len(prod_pre['blocks'])} (indices 0..{len(prod_pre['blocks']) - 1})")
    print(f"  - Blockchain tip hash: {prod_pre['state'][0]['last_block_hash']}")

    # Verify baseline in prod app
    prod_app = create_app({"TESTING": True, "MYSQL_DATABASE": PROD_DB})
    with prod_app.app_context():
        prod_pre_verify = verify_blockchain()
    assert prod_pre_verify["chain_valid"] is True, "Production chain is invalid before tests!"
    assert prod_pre_verify["integrity_valid"] is True, "Production integrity invalid before tests!"
    assert prod_pre_verify["records_checked"] == 5, f"Expected 5 records, got {prod_pre_verify['records_checked']}"
    assert prod_pre_verify["records_valid"] == 5, f"Expected 5 valid records, got {prod_pre_verify['records_valid']}"
    assert prod_pre_verify["untracked_records"] == 0, "Untracked records detected in production!"
    assert prod_pre_verify["mismatched_records"] == 0, "Mismatched records detected in production!"
    print("  [OK] Production database pre-check passed: 5 records valid, 0 untracked, 0 mismatches.")

    # ---------------------------------------------------------
    # STEP 1: Setup Isolated Test Database
    # ---------------------------------------------------------
    print(f"\n[Step 1] Initializing isolated test database '{ISOLATED_DB}'...")
    server_conn = get_mysql_connection()
    server_cur = server_conn.cursor()
    server_cur.execute(f"DROP DATABASE IF EXISTS `{ISOLATED_DB}`")
    server_cur.execute(f"CREATE DATABASE `{ISOLATED_DB}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    server_conn.commit()
    server_cur.close()
    server_conn.close()

    test_conn = get_mysql_connection(ISOLATED_DB)
    test_cur = DiagnosticCursor(test_conn.cursor())
    for statement in SCHEMA:
        test_cur.execute(statement)
    ensure_blockchain_action_enum(test_conn, test_cur)
    initialize_blockchain_genesis(test_conn, test_cur)

    # Insert prerequisite doctor and patient
    now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
    test_cur.execute(
        "INSERT INTO users (id, name, email, password_hash, `role`, status, created_at) "
        "VALUES (1, 'Dr. Lifecycle Auditor', 'auditor@test.local', 'dummyhash', 'doctor', 'active', %s)",
        (now_dt,),
    )
    test_cur.execute(
        "INSERT INTO patient_profiles (id, patient_code, linked_user_id, full_name, age, gender, blood_group, created_at) "
        "VALUES (1, 'EHR-TEST-0000000001', NULL, 'Jane Test Patient', 32, 'female', 'O+', %s)",
        (now_dt,),
    )
    test_conn.commit()
    test_cur.close()
    test_conn.close()
    print("  [OK] Isolated test database schema and genesis block created.")

    test_app = create_app({"TESTING": True, "MYSQL_DATABASE": ISOLATED_DB})

    # Verify genesis state in isolated DB
    with test_app.app_context():
        genesis_verify = verify_blockchain()
        assert genesis_verify["chain_valid"] is True
        assert genesis_verify["records_checked"] == 0
        assert genesis_verify["integrity_valid"] is True
    print("  [OK] Genesis block verified: chain valid, 0 records.")

    # ---------------------------------------------------------
    # STEP 2: Create a New EHR Record and Verify Audit Block
    # ---------------------------------------------------------
    print("\n[Step 2] Testing EHR record creation and linked audit block...")
    visit_data = {
        "treatment_date": date(2026, 10, 28),
        "diagnosis": "Acute Bronchitis",
        "treatment": "Bronchodilator and fluids",
        "hospital_name": "Metro General Hospital",
        "additional_instructions": "Bed rest for 3 days",
        "follow_up_date": date(2026, 11, 4),
        "medicines": [
            {
                "medicine_name": "Albuterol Inhaler",
                "dose": "90mcg",
                "frequency": "Every 4-6 hours",
                "duration": "7 days",
            },
            {
                "medicine_name": "Guaifenesin",
                "dose": "400mg",
                "frequency": "Twice daily",
                "duration": "5 days",
            },
        ],
    }

    with test_app.app_context():
        new_record_id = add_patient_visit("EHR-TEST-0000000001", visit_data, doctor_id=1)
        assert new_record_id is not None, "Failed to create patient visit!"

        # Query isolated database to confirm block addition
        snap_after_create = snapshot_database(ISOLATED_DB)
        records = snap_after_create["records"]
        blocks = snap_after_create["blocks"]
        state = snap_after_create["state"][0]

        assert len(records) == 1, f"Expected 1 record, found {len(records)}"
        assert len(blocks) == 2, f"Expected 2 blocks (genesis + create), found {len(blocks)}"
        
        create_block = blocks[1]
        assert create_block["block_index"] == 1
        assert create_block["record_id"] == new_record_id
        assert create_block["action"] == "create"
        assert create_block["actor_role"] == "doctor"
        assert create_block["previous_hash"] == blocks[0]["block_hash"], "Block 1 not linked to genesis!"
        assert state["last_block_index"] == 1
        assert state["last_block_hash"] == create_block["block_hash"]

        print(f"  - Record #{new_record_id} created successfully.")
        print(f"  - Block #1 added: action={create_block['action']}, actor={create_block['actor_role']}")
        print(f"  - Previous hash: {create_block['previous_hash'][:16]}... (matches Genesis hash)")
        print(f"  - Block hash:    {create_block['block_hash'][:16]}...")
        print(f"  - State tip:     index={state['last_block_index']}, hash={state['last_block_hash'][:16]}...")

        # Step 4: Verify full chain after create operation
        verify_after_create = verify_blockchain(record_id=new_record_id)
        assert verify_after_create["chain_valid"] is True, "Chain invalid after create!"
        assert verify_after_create["integrity_valid"] is True, "Integrity invalid after create!"
        assert verify_after_create["records_checked"] == 1
        assert verify_after_create["records_valid"] == 1
        assert verify_after_create["mismatched_records"] == 0
        assert verify_after_create["untracked_records"] == 0
        assert verify_after_create["records"][0]["status"] == "valid"
        print("  [OK] Full chain and record verified after create: chain_valid=True, integrity_valid=True.")

    # ---------------------------------------------------------
    # STEP 3: Update the EHR Record and Verify 2nd Audit Block
    # ---------------------------------------------------------
    print("\n[Step 3] Testing EHR record update and linked audit block...")
    updates = {
        "diagnosis": "Acute Bronchitis - Improving",
        "treatment": "Continue hydration and rest",
        "medical_condition": "Bronchitis",
    }

    with test_app.app_context():
        updated = update_record_fields(new_record_id, updates)
        assert updated is True, "Failed to update record!"

        # Query isolated database to confirm second block addition
        snap_after_update = snapshot_database(ISOLATED_DB)
        records = snap_after_update["records"]
        blocks = snap_after_update["blocks"]
        state = snap_after_update["state"][0]

        assert len(records) == 1
        assert len(blocks) == 3, f"Expected 3 blocks (genesis + create + update), found {len(blocks)}"

        update_block = blocks[2]
        assert update_block["block_index"] == 2
        assert update_block["record_id"] == new_record_id
        assert update_block["action"] == "update"
        assert update_block["actor_role"] == "doctor"
        assert update_block["previous_hash"] == blocks[1]["block_hash"], "Block 2 not linked to Block 1!"
        assert state["last_block_index"] == 2
        assert state["last_block_hash"] == update_block["block_hash"]

        print(f"  - Record #{new_record_id} updated successfully.")
        print(f"  - Block #2 added: action={update_block['action']}, actor={update_block['actor_role']}")
        print(f"  - Previous hash: {update_block['previous_hash'][:16]}... (matches Block #1 hash)")
        print(f"  - Block hash:    {update_block['block_hash'][:16]}...")
        print(f"  - State tip:     index={state['last_block_index']}, hash={state['last_block_hash'][:16]}...")

        # Step 4: Verify full chain after update operation
        verify_after_update = verify_blockchain(record_id=new_record_id)
        assert verify_after_update["chain_valid"] is True, "Chain invalid after update!"
        assert verify_after_update["integrity_valid"] is True, "Integrity invalid after update!"
        assert verify_after_update["records_checked"] == 1
        assert verify_after_update["records_valid"] == 1
        assert verify_after_update["mismatched_records"] == 0
        assert verify_after_update["untracked_records"] == 0
        assert verify_after_update["records"][0]["status"] == "valid"
        print("  [OK] Full chain and record verified after update: chain_valid=True, integrity_valid=True.")

    # ---------------------------------------------------------
    # STEP 5: Tamper Simulation in Isolated Test DB
    # ---------------------------------------------------------
    print("\n[Step 5] Tampering simulation: direct out-of-band database alteration...")
    test_conn = get_mysql_connection(ISOLATED_DB)
    test_cur = test_conn.cursor()
    test_cur.execute(
        "UPDATE ehr_records SET diagnosis = 'FORGED / TAMPERED DIAGNOSIS' WHERE id = %s",
        (new_record_id,),
    )
    test_conn.commit()
    test_cur.close()
    test_conn.close()
    print("  - Direct SQL update executed on ehr_records without audit block.")

    with test_app.app_context():
        verify_tampered = verify_blockchain(record_id=new_record_id)
        print(f"  - chain_valid:        {verify_tampered['chain_valid']}")
        print(f"  - integrity_valid:    {verify_tampered['integrity_valid']}")
        print(f"  - record_hashes_valid:{verify_tampered['record_hashes_valid']}")
        print(f"  - mismatched_records: {verify_tampered['mismatched_records']}")
        print(f"  - records_valid:      {verify_tampered['records_valid']}")
        print(f"  - record status:      {verify_tampered['records'][0]['status']}")
        print(f"  - computed hash:      {verify_tampered['records'][0]['record_hash'][:16]}...")
        print(f"  - audited block hash: {verify_tampered['records'][0]['audited_hash'][:16]}...")

        assert verify_tampered["chain_valid"] is True, "Cryptographic chain itself should remain valid!"
        assert verify_tampered["integrity_valid"] is False, "Integrity check must fail on tampered record!"
        assert verify_tampered["record_hashes_valid"] is False, "Record hashes check must fail!"
        assert verify_tampered["mismatched_records"] == 1, "Expected exactly 1 mismatched record!"
        assert verify_tampered["records_valid"] == 0, "No records should be valid!"
        assert verify_tampered["records"][0]["status"] == "mismatch"
        print("  [OK] Tampering successfully detected! Verification caught the unauthorized record alteration.")

    # ---------------------------------------------------------
    # STEP 6: Clean Up Isolated Database
    # ---------------------------------------------------------
    print(f"\n[Step 6] Dropping isolated test database '{ISOLATED_DB}'...")
    server_conn = get_mysql_connection()
    server_cur = server_conn.cursor()
    server_cur.execute(f"DROP DATABASE `{ISOLATED_DB}`")
    server_conn.commit()
    server_cur.execute(f"SHOW DATABASES LIKE '{ISOLATED_DB}'")
    remaining = server_cur.fetchall()
    assert len(remaining) == 0, "Isolated test database was not dropped!"
    server_cur.close()
    server_conn.close()
    print(f"  [OK] Isolated test database '{ISOLATED_DB}' dropped cleanly.")

    # ---------------------------------------------------------
    # STEP 7: Confirm Production/Demo Database Unchanged
    # ---------------------------------------------------------
    print(f"\n[Step 7] Confirming production/demo database '{PROD_DB}' remains untouched...")
    prod_post = snapshot_database(PROD_DB)

    # Check records
    assert len(prod_post["records"]) == len(prod_pre["records"]) == 5, "Production record count changed!"
    for pre_rec, post_rec in zip(prod_pre["records"], prod_post["records"]):
        assert pre_rec["id"] == post_rec["id"], f"Record ID mismatch: {pre_rec['id']} vs {post_rec['id']}"
        assert pre_rec["diagnosis"] == post_rec["diagnosis"], f"Diagnosis changed for record #{pre_rec['id']}!"
        assert pre_rec["updated_at"] == post_rec["updated_at"], f"Timestamp changed for record #{pre_rec['id']}!"

    # Check blocks
    assert len(prod_post["blocks"]) == len(prod_pre["blocks"]) == 6, "Production block count changed!"
    for pre_block, post_block in zip(prod_pre["blocks"], prod_post["blocks"]):
        assert pre_block["block_index"] == post_block["block_index"]
        assert pre_block["block_hash"] == post_block["block_hash"], f"Block hash changed for block #{pre_block['block_index']}!"

    # Check state
    assert prod_post["state"] == prod_pre["state"], "Production blockchain state changed!"

    # Run verify_blockchain on production app
    with prod_app.app_context():
        prod_post_verify = verify_blockchain()
    assert prod_post_verify["chain_valid"] is True
    assert prod_post_verify["integrity_valid"] is True
    assert prod_post_verify["records_checked"] == 5
    assert prod_post_verify["records_valid"] == 5
    assert prod_post_verify["untracked_records"] == 0
    assert prod_post_verify["mismatched_records"] == 0
    assert prod_post_verify["tip_hash"] == prod_pre_verify["tip_hash"]

    print("  - Records: exactly 5 demo records (IDs 4, 5, 6, 7, 8) unchanged.")
    print("  - Blocks:  exactly 6 blocks (indices 0..5) unchanged.")
    print(f"  - Tip hash:{prod_post_verify['tip_hash']}")
    print("  - Full verification: chain_valid=True, integrity_valid=True, mismatches=0, untracked=0.")
    print("  [OK] Production/demo database is 100% UNCHANGED and intact.")

    print("\n" + "=" * 70)
    print("ALL EHR RECORD LIFECYCLE VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_lifecycle_verification()
    sys.exit(0 if success else 1)
