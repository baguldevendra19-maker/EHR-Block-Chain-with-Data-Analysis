"""Isolated unit tests verifying the complete EHR record lifecycle and audit blocks."""

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from backend.blockchain import Block, Blockchain, calculate_record_hash
from backend.app import append_record_audit, create_app, verify_blockchain


class BlockchainRecordLifecycleUnitTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})

    def test_append_record_audit_creates_block_linked_to_genesis(self):
        genesis = Blockchain.create_genesis_block()
        fake_state = {"last_block_index": 0, "last_block_hash": genesis.hash}
        fake_record = {
            "id": 101,
            "patient_id": 10,
            "patient_profile_id": 1,
            "diagnosis": "Hypertension Stage 1",
            "treatment": "Lifestyle change",
            "medical_condition": "Hypertension",
            "updated_at": datetime(2026, 10, 28, 12, 0, 0),
        }
        fake_medicines = [
            {
                "record_id": 101,
                "line_number": 1,
                "medicine_name": "Amlodipine",
                "dose": "5mg",
                "frequency": "Once daily",
                "duration": "30 days",
            }
        ]

        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            fake_record,  # SELECT * FROM ehr_records
            fake_state,   # SELECT last_block_index, last_block_hash FROM blockchain_state FOR UPDATE
            {"block_index": 0, "block_hash": genesis.hash},  # SELECT tip FROM blockchain_blocks FOR UPDATE
        ]
        cursor.fetchall.return_value = fake_medicines

        append_record_audit(cursor, record_id=101, action="create", actor_role="doctor")

        # Verify insert into blockchain_blocks
        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO blockchain_blocks" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        inserted_params = insert_calls[0][0][1]
        block_index, _, record_id, record_hash, action, actor_role, previous_hash, block_hash = inserted_params

        self.assertEqual(block_index, 1)
        self.assertEqual(record_id, 101)
        expected_hash = calculate_record_hash(fake_record, fake_medicines)
        self.assertEqual(record_hash, expected_hash)
        self.assertEqual(action, "create")
        self.assertEqual(actor_role, "doctor")
        self.assertEqual(previous_hash, genesis.hash)
        self.assertEqual(len(block_hash), 64)

        # Verify state update
        update_calls = [c for c in cursor.execute.call_args_list if "UPDATE blockchain_state" in c[0][0]]
        self.assertEqual(len(update_calls), 1)
        state_params = update_calls[0][0][1]
        self.assertEqual(state_params, (1, block_hash))

    def test_append_record_audit_updates_block_linked_to_previous_block(self):
        genesis = Blockchain.create_genesis_block()
        chain = Blockchain()
        block_1 = chain.add_block(
            record_id=101,
            record_hash="1" * 64,
            action="create",
            actor_role="doctor",
        )

        fake_state = {"last_block_index": 1, "last_block_hash": block_1.hash}
        updated_record = {
            "id": 101,
            "patient_id": 10,
            "patient_profile_id": 1,
            "diagnosis": "Hypertension Stage 2",
            "treatment": "Medication intensified",
            "medical_condition": "Hypertension",
            "updated_at": datetime(2026, 10, 28, 14, 0, 0),
        }
        updated_medicines = [
            {
                "record_id": 101,
                "line_number": 1,
                "medicine_name": "Amlodipine",
                "dose": "10mg",
                "frequency": "Once daily",
                "duration": "30 days",
            }
        ]

        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            updated_record,
            fake_state,
            {"block_index": 1, "block_hash": block_1.hash},
        ]
        cursor.fetchall.return_value = updated_medicines

        append_record_audit(cursor, record_id=101, action="update", actor_role="doctor")

        insert_calls = [c for c in cursor.execute.call_args_list if "INSERT INTO blockchain_blocks" in c[0][0]]
        self.assertEqual(len(insert_calls), 1)
        inserted_params = insert_calls[0][0][1]
        block_index, _, record_id, record_hash, action, actor_role, previous_hash, block_hash = inserted_params

        self.assertEqual(block_index, 2)
        self.assertEqual(record_id, 101)
        expected_hash = calculate_record_hash(updated_record, updated_medicines)
        self.assertEqual(record_hash, expected_hash)
        self.assertEqual(action, "update")
        self.assertEqual(actor_role, "doctor")
        self.assertEqual(previous_hash, block_1.hash)

        update_calls = [c for c in cursor.execute.call_args_list if "UPDATE blockchain_state" in c[0][0]]
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0][1], (2, block_hash))

    def test_verify_blockchain_validates_full_chain_after_create_and_update(self):
        chain = Blockchain()
        record_created = {"id": 1, "diagnosis": "Initial Cold", "treatment": "Rest"}
        meds_created = [{"record_id": 1, "line_number": 1, "medicine_name": "Vitamin C"}]
        hash_1 = calculate_record_hash(record_created, meds_created)
        block_1 = chain.add_block(1, hash_1, "create", "doctor")

        record_updated = {"id": 1, "diagnosis": "Resolved", "treatment": "None"}
        meds_updated = []
        hash_2 = calculate_record_hash(record_updated, meds_updated)
        block_2 = chain.add_block(1, hash_2, "update", "doctor")

        db_blocks = [
            {
                "block_index": b.index,
                "timestamp": datetime.fromisoformat(b.timestamp).replace(tzinfo=None),
                "record_id": b.record_id,
                "record_hash": b.record_hash,
                "action": b.action,
                "actor_role": b.actor_role,
                "previous_hash": b.previous_hash,
                "block_hash": b.hash,
            }
            for b in chain.chain
        ]
        db_state = {"last_block_index": block_2.index, "last_block_hash": block_2.hash}
        db_records = [record_updated]

        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            db_blocks,      # SELECT ... FROM blockchain_blocks
            db_records,     # SELECT * FROM ehr_records
            [],             # SELECT ... FROM prescription_medicines
        ]
        cursor.fetchone.return_value = db_state

        with self.app.app_context(), patch("backend.app.database_cursor") as mock_cursor_ctx:
            mock_cursor_ctx.return_value.__enter__.return_value = cursor
            result = verify_blockchain()

        self.assertTrue(result["chain_valid"])
        self.assertTrue(result["integrity_valid"])
        self.assertTrue(result["record_hashes_valid"])
        self.assertEqual(result["records_checked"], 1)
        self.assertEqual(result["records_valid"], 1)
        self.assertEqual(result["mismatched_records"], 0)
        self.assertEqual(result["untracked_records"], 0)
        self.assertEqual(result["tip_hash"], block_2.hash)

    def test_verify_blockchain_detects_tampered_record_value_as_mismatch(self):
        chain = Blockchain()
        record_created = {"id": 1, "diagnosis": "Initial Cold", "treatment": "Rest"}
        meds_created = []
        hash_1 = calculate_record_hash(record_created, meds_created)
        block_1 = chain.add_block(1, hash_1, "create", "doctor")

        db_blocks = [
            {
                "block_index": b.index,
                "timestamp": datetime.fromisoformat(b.timestamp).replace(tzinfo=None),
                "record_id": b.record_id,
                "record_hash": b.record_hash,
                "action": b.action,
                "actor_role": b.actor_role,
                "previous_hash": b.previous_hash,
                "block_hash": b.hash,
            }
            for b in chain.chain
        ]
        db_state = {"last_block_index": block_1.index, "last_block_hash": block_1.hash}
        
        # Tampered record: someone altered the diagnosis directly in the database
        tampered_record = {"id": 1, "diagnosis": "Forged diagnosis", "treatment": "Rest"}

        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            db_blocks,
            [tampered_record],
            [],
        ]
        cursor.fetchone.return_value = db_state

        with self.app.app_context(), patch("backend.app.database_cursor") as mock_cursor_ctx:
            mock_cursor_ctx.return_value.__enter__.return_value = cursor
            result = verify_blockchain(record_id=1)

        self.assertTrue(result["chain_valid"])
        self.assertFalse(result["integrity_valid"])
        self.assertFalse(result["record_hashes_valid"])
        self.assertEqual(result["records_checked"], 1)
        self.assertEqual(result["records_valid"], 0)
        self.assertEqual(result["mismatched_records"], 1)
        self.assertEqual(result["untracked_records"], 0)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["status"], "mismatch")
        self.assertNotEqual(result["records"][0]["record_hash"], result["records"][0]["audited_hash"])


if __name__ == "__main__":
    unittest.main()
