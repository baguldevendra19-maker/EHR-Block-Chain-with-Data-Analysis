import re
import unittest

from mysql.connector.errors import ProgrammingError

from backend.setup_mysql import DiagnosticCursor, SCHEMA


class SchemaSetupTests(unittest.TestCase):
    def test_schema_creates_every_referenced_table_before_its_foreign_keys(self):
        table_names = []
        for statement in SCHEMA:
            match = re.search(
                r"CREATE TABLE IF NOT EXISTS\s+([A-Za-z0-9_]+)",
                statement,
                re.IGNORECASE,
            )
            self.assertIsNotNone(match, statement)
            table_names.append(match.group(1))

        positions = {table: index for index, table in enumerate(table_names)}
        for index, statement in enumerate(SCHEMA):
            for referenced_table in re.findall(
                r"REFERENCES\s+([A-Za-z0-9_]+)\s*\(",
                statement,
                re.IGNORECASE,
            ):
                self.assertIn(referenced_table, positions)
                self.assertLess(
                    positions[referenced_table],
                    index,
                    f"{table_names[index]} references {referenced_table} before its creation",
                )

        self.assertLess(positions["users"], positions["patient_profiles"])
        self.assertLess(positions["patient_profiles"], positions["ehr_records"])
        self.assertLess(positions["ehr_records"], positions["prescription_medicines"])
        self.assertNotIn("(CREATE TABLE", "\n".join(SCHEMA))

    def test_diagnostic_cursor_identifies_failing_sql_without_logging_parameters(self):
        class FailingCursor:
            def execute(self, statement, parameters=None):
                raise ProgrammingError(
                    msg="Failed to open the referenced table 'patient_profiles'",
                    errno=1824,
                )

        cursor = DiagnosticCursor(FailingCursor())
        statement = (
            "CREATE TABLE patient_profile_link_verifications "
            "FOREIGN KEY (patient_profile_id) REFERENCES patient_profiles (id)"
        )
        with self.assertRaisesRegex(RuntimeError, "patient_profiles") as raised:
            cursor.execute(statement, ("sensitive-value",))

        self.assertIn(statement, str(raised.exception))
        self.assertNotIn("sensitive-value", str(raised.exception))

    def test_ensure_blockchain_action_enum_alters_table_when_enum_outdated(self):
        from unittest.mock import MagicMock
        from backend.setup_mysql import ensure_blockchain_action_enum

        conn = MagicMock()
        cursor = MagicMock()
        # table_exists check returns True
        cursor.fetchone.side_effect = [
            (1,),  # table_exists count
            ("action", "enum('genesis','create','update')", "NO", "", None, ""),  # show columns
        ]
        ensure_blockchain_action_enum(conn, cursor)
        # Check that ALTER TABLE was executed
        executed_sqls = [call[0][0] for call in cursor.execute.call_args_list]
        self.assertTrue(any("ALTER TABLE `blockchain_blocks` MODIFY COLUMN `action`" in sql for sql in executed_sqls))
        conn.commit.assert_called_once()

    def test_initialize_historical_baseline_skips_when_all_records_audited(self):
        from unittest.mock import MagicMock
        from backend.setup_mysql import initialize_historical_baseline

        conn = MagicMock()
        cursor = MagicMock()
        # table_exists check:
        cursor.fetchone.side_effect = [
            (1,),  # table_exists ehr_records
            (1,),  # table_exists blockchain_blocks
        ]
        # SELECT id FROM ehr_records
        # SELECT DISTINCT record_id FROM blockchain_blocks
        cursor.fetchall.side_effect = [
            [(4,), (5,)],  # ehr_records
            [(4,), (5,)],  # already audited
        ]
        result = initialize_historical_baseline(conn, cursor)
        self.assertEqual(result, [])
        conn.commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()

