import unittest
from datetime import datetime, timezone

from backend.blockchain import Blockchain, Block, ZERO_HASH, calculate_record_hash


class BlockchainCoreTests(unittest.TestCase):
    def test_genesis_block_is_created_with_a_sha256_hash(self):
        blockchain = Blockchain()

        self.assertEqual(len(blockchain.chain), 1)
        self.assertEqual(blockchain.chain[0].index, 0)
        self.assertEqual(blockchain.chain[0].previous_hash, ZERO_HASH)
        self.assertEqual(len(blockchain.chain[0].hash), 64)
        self.assertTrue(blockchain.is_chain_valid())

    def test_new_blocks_link_to_the_previous_block(self):
        blockchain = Blockchain()
        first = blockchain.add_block(42, "a" * 64, "create", "doctor")
        second = blockchain.add_block(
            42, "b" * 64, "update", "doctor",
            timestamp=datetime(2026, 10, 26, tzinfo=timezone.utc),
        )

        self.assertEqual(first.index, 1)
        self.assertEqual(first.previous_hash, blockchain.chain[0].hash)
        self.assertEqual(second.index, 2)
        self.assertEqual(second.previous_hash, first.hash)
        self.assertTrue(blockchain.is_chain_valid())

    def test_verification_detects_changed_block_data_and_links(self):
        blockchain = Blockchain()
        block = blockchain.add_block(42, "a" * 64, "create", "doctor")
        blockchain.chain[1] = Block(
            index=block.index,
            timestamp=block.timestamp,
            record_id=block.record_id,
            record_hash="c" * 64,
            action=block.action,
            actor_role=block.actor_role,
            previous_hash=block.previous_hash,
            hash=block.hash,
        )
        self.assertFalse(blockchain.is_chain_valid())

        blockchain.chain[1] = block
        blockchain.chain.append(
            Block(
                index=2,
                timestamp=block.timestamp,
                record_id=42,
                record_hash="b" * 64,
                action="update",
                actor_role="doctor",
                previous_hash="d" * 64,
            )
        )
        self.assertFalse(blockchain.is_chain_valid())

    def test_blocks_round_trip_through_database_shaped_rows(self):
        blockchain = Blockchain()
        blockchain.add_block(42, "a" * 64, "create", "doctor")
        rows = [
            {
                "block_index": block.index,
                "timestamp": datetime.fromisoformat(block.timestamp).replace(tzinfo=None),
                "record_id": block.record_id,
                "record_hash": block.record_hash,
                "action": block.action,
                "actor_role": block.actor_role,
                "previous_hash": block.previous_hash,
                "block_hash": block.hash,
            }
            for block in blockchain.chain
        ]

        restored = Blockchain.from_blocks(rows)

        self.assertEqual(restored.chain, blockchain.chain)
        self.assertTrue(restored.is_chain_valid())

    def test_record_hash_is_deterministic_and_changes_with_record_data(self):
        record = {"diagnosis": "private diagnosis", "updated_at": datetime(2026, 1, 1)}
        prescription = [{"line_number": 1, "medicine_name": "private medicine"}]

        digest = calculate_record_hash(record, prescription)

        self.assertEqual(digest, calculate_record_hash(dict(reversed(list(record.items()))), prescription))
        self.assertNotEqual(
            digest,
            calculate_record_hash({**record, "diagnosis": "changed"}, prescription),
        )
        self.assertEqual(len(digest), 64)

    def test_block_creation_rejects_invalid_audit_metadata(self):
        blockchain = Blockchain()
        with self.assertRaises(ValueError):
            blockchain.add_block(0, "a" * 64, "create", "doctor")
        with self.assertRaises(ValueError):
            blockchain.add_block(42, "not-a-hash", "create", "doctor")
        with self.assertRaises(ValueError) as context:
            blockchain.add_block(42, "a" * 64, "delete", "doctor")
        self.assertIn("historical_baseline", str(context.exception))

    def test_historical_baseline_action_is_supported_and_links_correctly(self):
        blockchain = Blockchain()
        baseline_block = blockchain.add_block(
            record_id=101,
            record_hash="e" * 64,
            action="historical_baseline",
            actor_role="admin",
        )
        self.assertEqual(baseline_block.index, 1)
        self.assertEqual(baseline_block.action, "historical_baseline")
        self.assertEqual(baseline_block.actor_role, "admin")
        self.assertEqual(baseline_block.previous_hash, blockchain.chain[0].hash)
        self.assertTrue(blockchain.is_chain_valid())

        # Subsequent update from doctor links properly to historical baseline
        update_block = blockchain.add_block(
            record_id=101,
            record_hash="f" * 64,
            action="update",
            actor_role="doctor",
        )
        self.assertEqual(update_block.index, 2)
        self.assertEqual(update_block.previous_hash, baseline_block.hash)
        self.assertTrue(blockchain.is_chain_valid())

    def test_tampering_with_baseline_or_updated_block_is_detected(self):
        blockchain = Blockchain()
        baseline_block = blockchain.add_block(
            record_id=202,
            record_hash="1" * 64,
            action="historical_baseline",
            actor_role="admin",
        )
        update_block = blockchain.add_block(
            record_id=202,
            record_hash="2" * 64,
            action="update",
            actor_role="doctor",
        )
        self.assertTrue(blockchain.is_chain_valid())

        # Tamper with the baseline block's payload without recalculating hash
        tampered_baseline = Block(
            index=baseline_block.index,
            timestamp=baseline_block.timestamp,
            record_id=baseline_block.record_id,
            record_hash="9" * 64,
            action=baseline_block.action,
            actor_role=baseline_block.actor_role,
            previous_hash=baseline_block.previous_hash,
            hash=baseline_block.hash,
        )
        blockchain.chain[1] = tampered_baseline
        self.assertFalse(blockchain.is_chain_valid())

        # Restore baseline block, then tamper with the update block's link
        blockchain.chain[1] = baseline_block
        tampered_update = Block(
            index=update_block.index,
            timestamp=update_block.timestamp,
            record_id=update_block.record_id,
            record_hash=update_block.record_hash,
            action=update_block.action,
            actor_role=update_block.actor_role,
            previous_hash="0" * 64,  # broken link
            hash=update_block.hash,
        )
        blockchain.chain[2] = tampered_update
        self.assertFalse(blockchain.is_chain_valid())

    def test_record_lifecycle_baseline_subsequent_update_and_tamper_detection(self):
        # 1. Initial EHR record data and prescriptions
        record_data = {"id": 300, "diagnosis": "Initial fever", "treatment": "Rest"}
        meds = [{"line_number": 1, "medicine_name": "Paracetamol", "dose": "500mg"}]
        baseline_hash = calculate_record_hash(record_data, meds)

        # 2. Blockchain ledger initialized with genesis and baseline block
        blockchain = Blockchain()
        baseline_block = blockchain.add_block(
            record_id=300,
            record_hash=baseline_hash,
            action="historical_baseline",
            actor_role="admin",
        )
        self.assertEqual(baseline_block.index, 1)
        self.assertEqual(blockchain.chain[-1].record_hash, baseline_hash)
        self.assertTrue(blockchain.is_chain_valid())

        # 3. Doctor updates the record with a new diagnosis and medicine
        updated_record_data = {**record_data, "diagnosis": "Resolved"}
        updated_meds = []
        updated_hash = calculate_record_hash(updated_record_data, updated_meds)
        self.assertNotEqual(baseline_hash, updated_hash)

        # 4. Subsequent update appends a new linked block
        update_block = blockchain.add_block(
            record_id=300,
            record_hash=updated_hash,
            action="update",
            actor_role="doctor",
        )
        self.assertEqual(update_block.index, 2)
        self.assertEqual(update_block.previous_hash, baseline_block.hash)
        self.assertTrue(blockchain.is_chain_valid())

        # 5. Tampering simulation: someone modifies the database record behind the scenes
        tampered_record_data = {**updated_record_data, "treatment": "Unauthorized alteration"}
        tampered_hash = calculate_record_hash(tampered_record_data, updated_meds)
        latest_audited_hash = blockchain.chain[-1].record_hash
        self.assertNotEqual(tampered_hash, latest_audited_hash)


if __name__ == "__main__":
    unittest.main()


