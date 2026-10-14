"""Import the healthcare CSV into the isolated demo-records table."""

import csv
import hashlib
from io import StringIO
import sys
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from mysql.connector.errors import Error as MySQLError

from . import app as app_module
from .setup_mysql import SCHEMA

CSV_PATH = Path(__file__).resolve().parent.parent / "dataset" / "healthcare_dataset.csv"
EXPECTED_HEADERS = (
    "Name",
    "Age",
    "Gender",
    "Blood Type",
    "Medical Condition",
    "Date of Admission",
    "Doctor",
    "Hospital",
    "Insurance Provider",
    "Billing Amount",
    "Room Number",
    "Admission Type",
    "Discharge Date",
    "Medication",
    "Test Results",
)
DEMO_TABLE_SCHEMA = SCHEMA[2]
INSERT_SQL = """
    INSERT IGNORE INTO demo_ehr_records (
        full_name, age, gender, blood_type, medical_condition, admission_date, doctor,
        hospital, insurance_provider, billing_amount, room_number, admission_type,
        discharge_date, medication, test_results, is_demo, source_file, source_row,
        dataset_sha256, imported_at
    ) VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s, %s, %s, %s
    )
"""
TEXT_LIMITS = {
    "Name": 255,
    "Gender": 32,
    "Blood Type": 3,
    "Medical Condition": 120,
    "Doctor": 255,
    "Hospital": 255,
    "Insurance Provider": 120,
    "Admission Type": 32,
    "Medication": 120,
    "Test Results": 32,
}
DATE_COLUMNS = ("Date of Admission", "Discharge Date")


class InvalidCsvRow(ValueError):
    pass


def parse_csv_row(row, source_row, source_file=CSV_PATH.name):
    if None in row or any(row.get(header) is None for header in EXPECTED_HEADERS):
        raise InvalidCsvRow("row has missing or extra CSV fields")

    values = {}
    for header, limit in TEXT_LIMITS.items():
        value = row[header].strip()
        if not value:
            raise InvalidCsvRow(f"{header} is empty")
        if len(value) > limit:
            raise InvalidCsvRow(f"{header} exceeds {limit} characters")
        values[header] = value

    try:
        age = int(row["Age"].strip())
    except (TypeError, ValueError):
        raise InvalidCsvRow("Age must be an integer") from None
    if not 0 <= age <= 130:
        raise InvalidCsvRow("Age must be between 0 and 130")

    try:
        billing_amount = Decimal(row["Billing Amount"].strip())
    except (InvalidOperation, AttributeError):
        raise InvalidCsvRow("Billing Amount must be a decimal number") from None
    if not billing_amount.is_finite() or abs(billing_amount) >= Decimal("10000000000"):
        raise InvalidCsvRow("Billing Amount is outside the supported DECIMAL range")
    if max(0, -billing_amount.as_tuple().exponent) > 9:
        raise InvalidCsvRow("Billing Amount may have at most 9 decimal places")

    try:
        room_number = int(row["Room Number"].strip())
    except (TypeError, ValueError):
        raise InvalidCsvRow("Room Number must be an integer") from None
    if not 0 <= room_number <= 4_294_967_295:
        raise InvalidCsvRow("Room Number is outside the supported range")

    dates = []
    for column in DATE_COLUMNS:
        try:
            parsed_date = datetime.strptime(row[column].strip(), "%d-%m-%Y").date()
        except (TypeError, ValueError):
            raise InvalidCsvRow(f"{column} must use DD-MM-YYYY format") from None
        dates.append(parsed_date)
    if dates[1] < dates[0]:
        raise InvalidCsvRow("Discharge Date cannot be before Date of Admission")

    return (
        values["Name"],
        age,
        values["Gender"],
        values["Blood Type"],
        values["Medical Condition"],
        dates[0],
        values["Doctor"],
        values["Hospital"],
        values["Insurance Provider"],
        billing_amount,
        room_number,
        values["Admission Type"],
        dates[1],
        values["Medication"],
        values["Test Results"],
        source_file,
        source_row,
    )


def read_csv_rows(path):
    contents = path.read_bytes()
    dataset_hash = hashlib.sha256(contents).hexdigest()
    try:
        text = contents.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"CSV is not valid UTF-8 (byte {error.start}).") from None

    valid_rows = []
    invalid_rows = []
    try:
        reader = csv.DictReader(StringIO(text, newline=""), strict=True)
        if tuple(reader.fieldnames or ()) != EXPECTED_HEADERS:
            raise ValueError("CSV headers do not match the expected 15 columns exactly.")
        for source_row, row in enumerate(reader, start=2):
            try:
                valid_rows.append(parse_csv_row(row, source_row, path.name))
            except InvalidCsvRow as error:
                invalid_rows.append((source_row, str(error)))
    except csv.Error as error:
        invalid_rows.append((reader.line_num + 1, f"malformed CSV syntax: {error}"))

    return valid_rows, invalid_rows, dataset_hash


def import_csv(path=CSV_PATH):
    valid_rows, invalid_rows, dataset_hash = read_csv_rows(Path(path))
    imported = 0
    skipped = 0
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)

    with app_module.app.app_context():
        with app_module.database_cursor(commit=True) as cursor:
            cursor.execute(DEMO_TABLE_SCHEMA)
            cursor.execute("SHOW COLUMNS FROM demo_ehr_records")
            actual_columns = {column["Field"] for column in cursor.fetchall()}
            expected_columns = {
                "full_name", "age", "gender", "blood_type", "medical_condition",
                "admission_date", "doctor", "hospital", "insurance_provider",
                "billing_amount", "room_number", "admission_type", "discharge_date",
                "medication", "test_results", "is_demo", "source_file", "source_row",
                "dataset_sha256", "imported_at",
            }
            missing = expected_columns - actual_columns
            if missing:
                raise RuntimeError(
                    "The existing demo_ehr_records table is missing required columns: "
                    + ", ".join(sorted(missing))
                )
            cursor.execute("SHOW INDEX FROM demo_ehr_records")
            indexes = {}
            for index in cursor.fetchall():
                indexes.setdefault(index["Key_name"], []).append(index)
            has_unique_source_key = any(
                len(index) == 2
                and all(column["Non_unique"] == 0 for column in index)
                and [
                    (column["Seq_in_index"], column["Column_name"])
                    for column in sorted(index, key=lambda item: item["Seq_in_index"])
                ] == [(1, "source_file"), (2, "source_row")]
                for index in indexes.values()
            )
            if not has_unique_source_key:
                raise RuntimeError(
                    "The demo_ehr_records table lacks its unique source-row key; "
                    "refusing to import rows that could duplicate."
                )

            for start in range(0, len(valid_rows), 1000):
                batch = [
                    (*row, dataset_hash, imported_at)
                    for row in valid_rows[start:start + 1000]
                ]
                cursor.executemany(INSERT_SQL, batch)
                batch_imported = cursor.rowcount
                imported += batch_imported
                skipped += len(batch) - batch_imported

    return {
        "imported": imported,
        "skipped": skipped,
        "invalid": len(invalid_rows),
        "invalid_rows": invalid_rows,
        "source_rows": len(valid_rows) + len(invalid_rows),
        "dataset_sha256": dataset_hash,
    }


def main():
    try:
        result = import_csv()
    except (MySQLError, OSError, RuntimeError, ValueError) as error:
        print(f"Demo dataset import failed ({type(error).__name__}): {error}", file=sys.stderr)
        return 1

    print(
        f"Demo import complete: imported={result['imported']}, "
        f"skipped={result['skipped']}, invalid={result['invalid']}, "
        f"source_rows={result['source_rows']}"
    )
    for source_row, reason in result["invalid_rows"][:20]:
        print(f"Invalid CSV row {source_row}: {reason}", file=sys.stderr)
    if len(result["invalid_rows"]) > 20:
        print(f"... {len(result['invalid_rows']) - 20} more invalid rows.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
