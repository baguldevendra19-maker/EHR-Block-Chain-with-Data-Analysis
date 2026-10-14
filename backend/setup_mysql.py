"""Create the EHR MySQL database and tables without replacing existing data."""

import os
import re
import sys
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from mysql.connector.errors import Error as MySQLError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS users (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        name VARCHAR(120) NOT NULL,
        email VARCHAR(255) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        `role` ENUM('admin', 'doctor', 'patient') NOT NULL,
        status ENUM('active', 'inactive') NOT NULL DEFAULT 'active',
        date_of_birth DATETIME(6) NULL,
        gender VARCHAR(32) NULL,
        phone VARCHAR(30) NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        CONSTRAINT uq_users_email UNIQUE (email)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS ehr_records (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        patient_id BIGINT UNSIGNED NOT NULL,
        medical_condition TEXT NOT NULL,
        blood_pressure VARCHAR(64) NULL,
        age VARCHAR(16) NULL,
        gender VARCHAR(32) NULL,
        diagnosis TEXT NOT NULL,
        treatment TEXT NOT NULL,
        record_date DATETIME(6) NOT NULL,
        created_by BIGINT UNSIGNED NOT NULL,
        updated_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        KEY ix_ehr_patient_record_date (patient_id, record_date),
        CONSTRAINT fk_ehr_patient FOREIGN KEY (patient_id) REFERENCES users (id),
        CONSTRAINT fk_ehr_creator FOREIGN KEY (created_by) REFERENCES users (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS demo_ehr_records (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        full_name VARCHAR(255) NOT NULL,
        age TINYINT UNSIGNED NOT NULL,
        gender VARCHAR(32) NOT NULL,
        blood_type VARCHAR(3) NOT NULL,
        medical_condition VARCHAR(120) NOT NULL,
        admission_date DATE NOT NULL,
        doctor VARCHAR(255) NOT NULL,
        hospital VARCHAR(255) NOT NULL,
        insurance_provider VARCHAR(120) NOT NULL,
        billing_amount DECIMAL(19, 9) NOT NULL,
        room_number INT UNSIGNED NOT NULL,
        admission_type VARCHAR(32) NOT NULL,
        discharge_date DATE NOT NULL,
        medication VARCHAR(120) NOT NULL,
        test_results VARCHAR(32) NOT NULL,
        is_demo BOOLEAN NOT NULL DEFAULT TRUE,
        source_file VARCHAR(255) NOT NULL,
        source_row BIGINT UNSIGNED NOT NULL,
        dataset_sha256 CHAR(64) NOT NULL,
        imported_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_demo_source_row (source_file, source_row),
        KEY ix_demo_admission_date (admission_date),
        KEY ix_demo_medical_condition (medical_condition)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        user_id BIGINT UNSIGNED NOT NULL,
        token_hash CHAR(64) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        expires_at DATETIME(6) NOT NULL,
        used_at DATETIME(6) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_password_reset_token_hash (token_hash),
        KEY ix_password_reset_user (user_id, used_at),
        KEY ix_password_reset_expiry (expires_at),
        CONSTRAINT fk_password_reset_user FOREIGN KEY (user_id) REFERENCES users (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

REQUIRED_COLUMNS = {
    "users": {
        "id", "name", "email", "password_hash", "role", "status",
        "date_of_birth", "gender", "phone", "created_at",
    },
    "password_reset_tokens": {
        "id", "user_id", "token_hash", "created_at", "expires_at", "used_at",
    },
    "ehr_records": {
        "id", "patient_id", "medical_condition", "blood_pressure", "age", "gender",
        "diagnosis", "treatment", "record_date", "created_by", "updated_at",
    },
    "demo_ehr_records": {
        "id", "full_name", "age", "gender", "blood_type", "medical_condition",
        "admission_date", "doctor", "hospital", "insurance_provider", "billing_amount",
        "room_number", "admission_type", "discharge_date", "medication", "test_results",
        "is_demo", "source_file", "source_row", "dataset_sha256", "imported_at",
    },
}


def main():
    database = os.getenv("MYSQL_DATABASE", "ehr_blockchain")
    user = os.getenv("MYSQL_USER", "")
    if not user:
        raise SystemExit("Set MYSQL_USER in backend/.env before initializing the database.")
    if not re.fullmatch(r"[A-Za-z0-9_]+", database):
        raise SystemExit("MYSQL_DATABASE may contain only letters, numbers, and underscores.")

    connection = None
    try:
        connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "localhost"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=user,
            password=os.getenv("MYSQL_PASSWORD", ""),
        )
        cursor = connection.cursor()
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        connection.commit()
        cursor.close()
        connection.close()
        connection = mysql.connector.connect(
            host=os.getenv("MYSQL_HOST", "localhost"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            database=database,
            user=user,
            password=os.getenv("MYSQL_PASSWORD", ""),
        )
        cursor = connection.cursor()
        for statement in SCHEMA:
            cursor.execute(statement)
        for table, expected_columns in REQUIRED_COLUMNS.items():
            cursor.execute(f"SHOW COLUMNS FROM `{table}`")
            column_rows = cursor.fetchall()
            actual_columns = {row[0] for row in column_rows}
            missing = expected_columns - actual_columns
            if missing:
                raise RuntimeError(
                    f"Existing table '{table}' is missing required columns: "
                    f"{', '.join(sorted(missing))}. No existing table was altered."
                )
            id_column = next(row for row in column_rows if row[0] == "id")
            if id_column[3] != "PRI" or "auto_increment" not in id_column[5].lower():
                raise RuntimeError(
                    f"Existing table '{table}' does not have an auto-increment primary key. "
                    "No existing table was altered."
                )
        cursor.execute("SHOW INDEX FROM users")
        index_rows = cursor.fetchall()
        index_columns = {}
        for row in index_rows:
            index_columns.setdefault(row[2], []).append(row)
        unique_email = any(
            len(rows) == 1 and rows[0][1] == 0 and rows[0][4] == "email"
            for rows in index_columns.values()
        )
        if not unique_email:
            raise RuntimeError(
                "The existing users table has no unique email index. "
                "No existing table was altered."
            )
        cursor.execute("SHOW INDEX FROM demo_ehr_records")
        demo_index_rows = cursor.fetchall()
        demo_indexes = {}
        for row in demo_index_rows:
            demo_indexes.setdefault(row[2], []).append(row)
        unique_source_row = any(
            len(rows) == 2
            and all(row[1] == 0 for row in rows)
            and [(row[3], row[4]) for row in sorted(rows, key=lambda item: item[3])]
            == [(1, "source_file"), (2, "source_row")]
            for rows in demo_indexes.values()
        )
        if not unique_source_row:
            raise RuntimeError(
                "The demo_ehr_records table has no unique (source_file, source_row) key. "
                "No existing table was altered."
            )
        cursor.execute("SHOW COLUMNS FROM demo_ehr_records LIKE 'billing_amount'")
        billing_column = cursor.fetchone()
        billing_type = billing_column[1].lower()
        if billing_type == "decimal(15,5)":
            cursor.execute(
                "ALTER TABLE demo_ehr_records "
                "MODIFY COLUMN billing_amount DECIMAL(19, 9) NOT NULL"
            )
        elif billing_type != "decimal(19,9)":
            raise RuntimeError(
                "The demo_ehr_records.billing_amount column must be DECIMAL(19,9); "
                "no incompatible existing type was changed."
            )
        connection.commit()
        print(f"MySQL schema is ready in database '{database}'. Existing data was not deleted.")
    except (MySQLError, ValueError, RuntimeError) as error:
        print(f"MySQL setup failed ({type(error).__name__}): {error}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if connection is not None and connection.is_connected():
            connection.close()


if __name__ == "__main__":
    main()
