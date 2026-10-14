import csv
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from backend.app import serialize_document
from backend.import_demo_dataset import (
    EXPECTED_HEADERS,
    InvalidCsvRow,
    import_csv,
    parse_csv_row,
    read_csv_rows,
)
from backend.setup_mysql import SCHEMA


def valid_row():
    return {
        "Name": "Demo Person",
        "Age": "34",
        "Gender": "Female",
        "Blood Type": "A+",
        "Medical Condition": "Asthma",
        "Date of Admission": "12-02-2024",
        "Doctor": "Demo Doctor",
        "Hospital": "Demo Hospital",
        "Insurance Provider": "Demo Insurer",
        "Billing Amount": "12345.67890",
        "Room Number": "312",
        "Admission Type": "Urgent",
        "Discharge Date": "16-02-2024",
        "Medication": "Demo medication",
        "Test Results": "Normal",
    }


class DemoDatasetTests(unittest.TestCase):
    def test_csv_row_maps_all_fields_and_marks_source_row(self):
        parsed = parse_csv_row(valid_row(), 2, "fixture.csv")
        self.assertEqual(len(parsed), 17)
        self.assertEqual(parsed[0], "Demo Person")
        self.assertEqual(parsed[5].isoformat(), "2024-02-12")
        self.assertEqual(parsed[9], Decimal("12345.67890"))
        self.assertEqual(parsed[-2:], ("fixture.csv", 2))

    def test_invalid_date_and_numeric_values_are_rejected(self):
        row = valid_row()
        row["Date of Admission"] = "2024-02-12"
        with self.assertRaisesRegex(InvalidCsvRow, "DD-MM-YYYY"):
            parse_csv_row(row, 2)
        row = valid_row()
        row["Age"] = "not-a-number"
        with self.assertRaisesRegex(InvalidCsvRow, "Age must be an integer"):
            parse_csv_row(row, 2)

    def test_billing_precision_up_to_nine_decimal_places_is_preserved(self):
        row = valid_row()
        row["Billing Amount"] = "123.123456789"
        parsed = parse_csv_row(row, 2)
        self.assertEqual(parsed[9], Decimal("123.123456789"))
        row["Billing Amount"] = "123.1234567890"
        with self.assertRaisesRegex(InvalidCsvRow, "9 decimal places"):
            parse_csv_row(row, 2)

    def test_reader_counts_invalid_rows_and_requires_exact_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "fixture.csv"
            invalid = valid_row()
            invalid["Room Number"] = "room 4"
            with csv_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=EXPECTED_HEADERS)
                writer.writeheader()
                writer.writerow(valid_row())
                writer.writerow(invalid)
            valid, invalid_rows, _ = read_csv_rows(csv_path)
            self.assertEqual(len(valid), 1)
            self.assertEqual(len(invalid_rows), 1)
            self.assertEqual(invalid_rows[0][0], 3)

            csv_path.write_text("wrong,header\nvalue,another\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "headers"):
                read_csv_rows(csv_path)

    def test_import_reruns_skip_existing_source_rows(self):
        class FakeCursor:
            def __init__(self):
                self.seen_rows = set()
                self.rowcount = 0

            def execute(self, statement):
                self.statement = statement

            def fetchall(self):
                if self.statement == "SHOW COLUMNS FROM demo_ehr_records":
                    return [{"Field": column} for column in (
                        "full_name", "age", "gender", "blood_type", "medical_condition",
                        "admission_date", "doctor", "hospital", "insurance_provider",
                        "billing_amount", "room_number", "admission_type", "discharge_date",
                        "medication", "test_results", "is_demo", "source_file", "source_row",
                        "dataset_sha256", "imported_at",
                    )]
                return [
                    {"Key_name": "uq_demo_source_row", "Non_unique": 0,
                     "Seq_in_index": 1, "Column_name": "source_file"},
                    {"Key_name": "uq_demo_source_row", "Non_unique": 0,
                     "Seq_in_index": 2, "Column_name": "source_row"},
                ]

            def executemany(self, statement, batch):
                self.statement = statement
                inserted = 0
                for values in batch:
                    source_key = (values[15], values[16])
                    if source_key not in self.seen_rows:
                        self.seen_rows.add(source_key)
                        inserted += 1
                self.rowcount = inserted

        class CursorContext:
            def __init__(self, cursor):
                self.cursor = cursor

            def __enter__(self):
                return self.cursor

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "fixture.csv"
            with csv_path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=EXPECTED_HEADERS)
                writer.writeheader()
                writer.writerow(valid_row())
                writer.writerow(valid_row())

            cursor = FakeCursor()
            with patch(
                "backend.import_demo_dataset.app_module.database_cursor",
                side_effect=lambda **kwargs: CursorContext(cursor),
            ):
                first = import_csv(csv_path)
                second = import_csv(csv_path)

            self.assertEqual((first["imported"], first["skipped"]), (2, 0))
            self.assertEqual((second["imported"], second["skipped"]), (0, 2))
            self.assertIn("INSERT IGNORE INTO demo_ehr_records", cursor.statement)

    def test_decimal_values_are_json_serializable_without_precision_loss(self):
        self.assertEqual(serialize_document({"billing_amount": Decimal("123.45678")}),
                         {"billing_amount": "123.45678"})

    def test_demo_table_is_independent_of_accounts_and_real_records(self):
        demo_table_sql = SCHEMA[2]
        self.assertIn("UNIQUE KEY uq_demo_source_row (source_file, source_row)", demo_table_sql)
        self.assertNotIn("FOREIGN KEY", demo_table_sql)
        self.assertNotIn("password", demo_table_sql.lower())
        self.assertNotIn("CREATE TABLE IF NOT EXISTS users", demo_table_sql)
        self.assertNotIn("CREATE TABLE IF NOT EXISTS ehr_records", demo_table_sql)


if __name__ == "__main__":
    unittest.main()
