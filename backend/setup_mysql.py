"""Create the EHR MySQL database and tables without replacing existing data."""

import os
import re
import sys
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import mysql.connector
from dotenv import load_dotenv
from mysql.connector.errors import Error as MySQLError

if __package__:
    from .blockchain import Block, Blockchain, calculate_record_hash
else:
    from blockchain import Block, Blockchain, calculate_record_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS blockchain_blocks (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        block_index INT UNSIGNED NOT NULL,
        created_at DATETIME(6) NOT NULL,
        record_id BIGINT UNSIGNED NULL,
        record_hash CHAR(64) NOT NULL,
        action ENUM('genesis', 'create', 'update', 'historical_baseline') NOT NULL,
        actor_role ENUM('system', 'admin', 'doctor') NOT NULL,
        previous_hash CHAR(64) NOT NULL,
        block_hash CHAR(64) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_blockchain_block_index (block_index),
        UNIQUE KEY uq_blockchain_block_hash (block_hash),
        KEY ix_blockchain_record_index (record_id, block_index)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS blockchain_state (
        id TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
        last_block_index INT UNSIGNED NOT NULL,
        last_block_hash CHAR(64) NOT NULL,
        PRIMARY KEY (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
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
    CREATE TABLE IF NOT EXISTS patient_profiles (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        patient_code VARCHAR(32) NOT NULL,
        linked_user_id BIGINT UNSIGNED NULL,
        full_name VARCHAR(120) NOT NULL,
        age SMALLINT UNSIGNED NULL,
        gender VARCHAR(32) NULL,
        blood_group VARCHAR(3) NULL,
        mobile_number VARCHAR(30) NULL,
        email VARCHAR(255) NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_patient_profile_code (patient_code),
        UNIQUE KEY uq_patient_profile_user (linked_user_id),
        KEY ix_patient_profile_email (email),
        CONSTRAINT fk_patient_profile_user FOREIGN KEY (linked_user_id) REFERENCES users (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS ehr_records (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        patient_id BIGINT UNSIGNED NULL,
        patient_profile_id BIGINT UNSIGNED NULL,
        medical_condition TEXT NOT NULL,
        blood_pressure VARCHAR(64) NULL,
        age VARCHAR(16) NULL,
        gender VARCHAR(32) NULL,
        diagnosis TEXT NOT NULL,
        treatment TEXT NOT NULL,
        record_date DATETIME(6) NOT NULL,
        created_by BIGINT UNSIGNED NOT NULL,
        doctor_name VARCHAR(120) NULL,
        hospital_name VARCHAR(255) NULL,
        additional_instructions TEXT NULL,
        follow_up_date DATE NULL,
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
    """
    CREATE TABLE IF NOT EXISTS prescription_medicines (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        record_id BIGINT UNSIGNED NOT NULL,
        line_number SMALLINT UNSIGNED NOT NULL,
        medicine_name VARCHAR(255) NOT NULL,
        dose VARCHAR(120) NOT NULL,
        frequency VARCHAR(120) NOT NULL,
        duration VARCHAR(120) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_prescription_medicine_line (record_id, line_number),
        CONSTRAINT fk_prescription_medicine_record FOREIGN KEY (record_id)
            REFERENCES ehr_records (id) ON DELETE CASCADE
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS medical_reports (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        patient_profile_id BIGINT UNSIGNED NOT NULL,
        record_id BIGINT UNSIGNED NOT NULL,
        original_name VARCHAR(255) NOT NULL,
        storage_key CHAR(40) NOT NULL,
        mime_type VARCHAR(64) NOT NULL,
        size_bytes INT UNSIGNED NOT NULL,
        sha256 CHAR(64) NOT NULL,
        uploaded_by BIGINT UNSIGNED NOT NULL,
        uploaded_at DATETIME(6) NOT NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_medical_report_storage_key (storage_key),
        KEY ix_medical_report_patient_visit (patient_profile_id, record_id),
        CONSTRAINT fk_medical_report_patient FOREIGN KEY (patient_profile_id)
            REFERENCES patient_profiles (id),
        CONSTRAINT fk_medical_report_visit FOREIGN KEY (record_id)
            REFERENCES ehr_records (id),
        CONSTRAINT fk_medical_report_uploader FOREIGN KEY (uploaded_by)
            REFERENCES users (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS patient_email_verifications (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        patient_profile_id BIGINT UNSIGNED NOT NULL,
        name VARCHAR(120) NOT NULL,
        email VARCHAR(255) NOT NULL,
        password_hash VARCHAR(255) NOT NULL,
        date_of_birth DATETIME(6) NOT NULL,
        gender VARCHAR(32) NOT NULL,
        phone VARCHAR(30) NOT NULL,
        token_hash CHAR(64) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        expires_at DATETIME(6) NOT NULL,
        used_at DATETIME(6) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_patient_email_verification_token (token_hash),
        KEY ix_patient_email_verification_profile (patient_profile_id, used_at),
        CONSTRAINT fk_patient_email_verification_profile FOREIGN KEY (patient_profile_id)
            REFERENCES patient_profiles (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS patient_profile_link_verifications (
        id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
        patient_profile_id BIGINT UNSIGNED NOT NULL,
        user_id BIGINT UNSIGNED NOT NULL,
        token_hash CHAR(64) NOT NULL,
        created_at DATETIME(6) NOT NULL,
        expires_at DATETIME(6) NOT NULL,
        used_at DATETIME(6) NULL,
        PRIMARY KEY (id),
        UNIQUE KEY uq_patient_profile_link_token (token_hash),
        KEY ix_patient_profile_link_user (user_id, used_at),
        CONSTRAINT fk_patient_profile_link_profile FOREIGN KEY (patient_profile_id)
            REFERENCES patient_profiles (id),
        CONSTRAINT fk_patient_profile_link_user FOREIGN KEY (user_id)
            REFERENCES users (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)

REQUIRED_COLUMNS = {
    "blockchain_blocks": {
        "id", "block_index", "created_at", "record_id", "record_hash",
        "action", "actor_role", "previous_hash", "block_hash",
    },
    "blockchain_state": {"id", "last_block_index", "last_block_hash"},
    "users": {
        "id", "name", "email", "password_hash", "role", "status",
        "date_of_birth", "gender", "phone", "created_at",
    },
    "password_reset_tokens": {
        "id", "user_id", "token_hash", "created_at", "expires_at", "used_at",
    },
    "ehr_records": {
        "id", "patient_id", "medical_condition", "blood_pressure", "age", "gender",
        "patient_profile_id", "diagnosis", "treatment", "record_date", "created_by",
        "doctor_name", "hospital_name", "additional_instructions", "follow_up_date",
        "updated_at",
    },
    "demo_ehr_records": {
        "id", "full_name", "age", "gender", "blood_type", "medical_condition",
        "admission_date", "doctor", "hospital", "insurance_provider", "billing_amount",
        "room_number", "admission_type", "discharge_date", "medication", "test_results",
        "is_demo", "source_file", "source_row", "dataset_sha256", "imported_at",
    },
    "patient_profiles": {
        "id", "patient_code", "linked_user_id", "full_name", "age", "gender",
        "blood_group", "mobile_number", "email", "created_at",
    },
    "prescription_medicines": {
        "id", "record_id", "line_number", "medicine_name", "dose", "frequency", "duration",
    },
    "medical_reports": {
        "id", "patient_profile_id", "record_id", "original_name", "storage_key",
        "mime_type", "size_bytes", "sha256", "uploaded_by", "uploaded_at",
    },
    "patient_email_verifications": {
        "id", "patient_profile_id", "name", "email", "password_hash", "date_of_birth",
        "gender", "phone", "token_hash", "created_at", "expires_at", "used_at",
    },
    "patient_profile_link_verifications": {
        "id", "patient_profile_id", "user_id", "token_hash", "created_at", "expires_at",
        "used_at",
    },
}

MIGRATION_BACKUPS = {
    "users": "ehr_migration_backup_v2_users",
    "ehr_records": "ehr_migration_backup_v2_ehr_records",
    "patient_profiles": "ehr_migration_backup_v2_patient_profiles",
    "demo_ehr_records": "ehr_migration_backup_v2_demo_ehr_records",
    "prescription_medicines": "ehr_migration_backup_v2_prescription_medicines",
    "medical_reports": "ehr_migration_backup_v2_medical_reports",
    "patient_email_verifications": "ehr_migration_backup_v2_patient_email_verifications",
    "patient_profile_link_verifications": (
        "ehr_migration_backup_v2_patient_profile_link_verifications"
    ),
}


def table_exists(cursor, table):
    cursor.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return cursor.fetchone()[0] == 1


def initialize_blockchain_genesis(connection, cursor):
    cursor.execute("SELECT COUNT(*) FROM blockchain_blocks")
    block_count = cursor.fetchone()[0]
    cursor.execute(
        "SELECT last_block_index, last_block_hash FROM blockchain_state "
        "WHERE id = 1"
    )
    state = cursor.fetchone()
    if block_count == 0 and state is None:
        genesis = Blockchain.create_genesis_block()
        cursor.execute(
            "INSERT INTO blockchain_blocks "
            "(block_index, created_at, record_id, record_hash, action, actor_role, "
            "previous_hash, block_hash) VALUES (%s, %s, NULL, %s, 'genesis', "
            "'system', %s, %s)",
            (
                genesis.index,
                "1970-01-01 00:00:00.000000",
                genesis.record_hash,
                genesis.previous_hash,
                genesis.hash,
            ),
        )
        cursor.execute(
            "INSERT INTO blockchain_state "
            "(id, last_block_index, last_block_hash) VALUES (1, %s, %s)",
            (genesis.index, genesis.hash),
        )
        connection.commit()
        return
    if block_count == 0 or state is None:
        raise RuntimeError(
            "Blockchain initialization is incomplete. No existing chain data was changed."
        )


def ensure_blockchain_action_enum(connection, cursor):
    """Safely extend blockchain_blocks.action ENUM to support historical_baseline."""
    if not table_exists(cursor, "blockchain_blocks"):
        return
    cursor.execute("SHOW COLUMNS FROM `blockchain_blocks` LIKE 'action'")
    row = cursor.fetchone()
    if row and "historical_baseline" not in row[1].lower():
        cursor.execute(
            "ALTER TABLE `blockchain_blocks` MODIFY COLUMN `action` "
            "ENUM('genesis', 'create', 'update', 'historical_baseline') NOT NULL"
        )
        connection.commit()


def initialize_historical_baseline(connection, cursor):
    """Safely append baseline audit blocks for existing un-audited EHR demo records.

    This baseline establishes a cryptographic audit anchor for existing demo records
    and verifies their state from the time of baseline registration onward.
    It does not claim proof of earlier untracked historical integrity.
    The operation is strictly idempotent: already-audited records are skipped.
    """
    if not table_exists(cursor, "ehr_records") or not table_exists(cursor, "blockchain_blocks"):
        return []

    cursor.execute("SELECT id FROM `ehr_records` ORDER BY id ASC")
    all_record_rows = cursor.fetchall()
    if not all_record_rows:
        return []

    cursor.execute(
        "SELECT DISTINCT record_id FROM `blockchain_blocks` WHERE record_id IS NOT NULL"
    )
    audited_record_ids = {
        row[0] if isinstance(row, (tuple, list)) else row["record_id"]
        for row in cursor.fetchall()
    }

    record_ids_to_baseline = [
        row[0] if isinstance(row, (tuple, list)) else row["id"]
        for row in all_record_rows
        if (row[0] if isinstance(row, (tuple, list)) else row["id"]) not in audited_record_ids
    ]
    if not record_ids_to_baseline:
        return []

    cursor.execute(
        "SELECT last_block_index, last_block_hash FROM `blockchain_state` "
        "WHERE id = 1 FOR UPDATE"
    )
    state = cursor.fetchone()
    if state is None:
        raise RuntimeError("Blockchain state is not initialized; genesis block required.")

    last_index = state[0] if isinstance(state, (tuple, list)) else state["last_block_index"]
    last_hash = state[1] if isinstance(state, (tuple, list)) else state["last_block_hash"]

    cursor.execute(
        "SELECT block_index, block_hash FROM `blockchain_blocks` "
        "ORDER BY block_index DESC LIMIT 1 FOR UPDATE"
    )
    tip = cursor.fetchone()
    tip_index = tip[0] if isinstance(tip, (tuple, list)) else tip["block_index"]
    tip_hash = tip[1] if isinstance(tip, (tuple, list)) else tip["block_hash"]
    if tip is None or tip_index != last_index or tip_hash != last_hash:
        raise RuntimeError("Blockchain tip does not match its persisted state.")

    cursor.execute("SHOW COLUMNS FROM `ehr_records`")
    col_names = [col[0] for col in cursor.fetchall()]

    created_blocks = []
    current_index = last_index
    current_previous_hash = last_hash

    for record_id in record_ids_to_baseline:
        cursor.execute("SELECT * FROM `ehr_records` WHERE id = %s", (record_id,))
        rec_row = cursor.fetchone()
        if isinstance(rec_row, dict):
            record_dict = rec_row
        else:
            record_dict = dict(zip(col_names, rec_row))

        cursor.execute(
            "SELECT line_number, medicine_name, dose, frequency, duration "
            "FROM `prescription_medicines` WHERE record_id = %s ORDER BY line_number",
            (record_id,),
        )
        med_rows = cursor.fetchall()
        if med_rows and isinstance(med_rows[0], dict):
            med_dicts = med_rows
        else:
            med_keys = ["line_number", "medicine_name", "dose", "frequency", "duration"]
            med_dicts = [dict(zip(med_keys, m)) for m in med_rows]

        record_hash = calculate_record_hash(record_dict, med_dicts)
        current_index += 1
        now_utc = datetime.now(timezone.utc)
        block = Block(
            index=current_index,
            timestamp=now_utc,
            record_id=record_id,
            record_hash=record_hash,
            action="historical_baseline",
            actor_role="admin",
            previous_hash=current_previous_hash,
        )
        cursor.execute(
            "INSERT INTO `blockchain_blocks` "
            "(block_index, created_at, record_id, record_hash, action, actor_role, "
            "previous_hash, block_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                block.index,
                datetime.fromisoformat(block.timestamp).replace(tzinfo=None),
                record_id,
                record_hash,
                "historical_baseline",
                "admin",
                block.previous_hash,
                block.hash,
            ),
        )
        current_previous_hash = block.hash
        created_blocks.append(block)

    cursor.execute(
        "UPDATE `blockchain_state` SET last_block_index = %s, last_block_hash = %s "
        "WHERE id = 1",
        (current_index, current_previous_hash),
    )
    connection.commit()
    return created_blocks


class MigrationOperationError(RuntimeError):
    """A database error annotated with the SQL operation that failed."""

    def __init__(self, statement, error):
        operation = " ".join(statement.split())
        super().__init__(
            f"SQL operation failed: {operation} "
            f"(MySQL error {error.errno}: {error.msg})"
        )


class DiagnosticCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, statement, parameters=None):
        try:
            if parameters is None:
                return self._cursor.execute(statement)
            return self._cursor.execute(statement, parameters)
        except MySQLError as error:
            raise MigrationOperationError(statement, error) from error

    def executemany(self, statement, parameters):
        try:
            return self._cursor.executemany(statement, parameters)
        except MySQLError as error:
            raise MigrationOperationError(statement, error) from error

    def __getattr__(self, name):
        return getattr(self._cursor, name)


def schema_changes_needed(cursor):
    for table, expected_columns in REQUIRED_COLUMNS.items():
        if not table_exists(cursor, table):
            return True
        cursor.execute(f"SHOW COLUMNS FROM `{table}`")
        rows = cursor.fetchall()
        by_name = {row[0]: row for row in rows}
        if expected_columns - by_name.keys():
            return True
        if table == "ehr_records" and (
            by_name["patient_id"][2] != "YES"
            or by_name["patient_profile_id"][2] != "NO"
        ):
            return True
        if table == "demo_ehr_records" and by_name["billing_amount"][1].lower() != "decimal(19,9)":
            return True
    return False


def table_digest(cursor, table):
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise ValueError(f"Unsafe table name for snapshot verification: {table!r}")
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    columns = cursor.fetchall()
    primary_key = primary_key_columns(cursor, table)
    if not primary_key:
        raise RuntimeError(f"Cannot verify backup table '{table}' without a primary key.")
    column_names = [row[0] for row in columns]
    selected_columns = ", ".join(f"`{column}`" for column in column_names)
    ordering = ", ".join(f"`{column}`" for column in primary_key)
    cursor.execute(
        f"SELECT {selected_columns} FROM `{table}` ORDER BY {ordering}"
    )
    digest = hashlib.sha256()
    while True:
        rows = cursor.fetchmany(1000)
        if not rows:
            break
        for row in rows:
            encoded = json.dumps(
                row, default=str, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def primary_key_columns(cursor, table):
    cursor.execute(f"SHOW INDEX FROM `{table}`")
    return [
        row[4] for row in sorted(
            (row for row in cursor.fetchall() if row[2] == "PRIMARY"),
            key=lambda row: int(row[3]),
        )
    ]


def verify_backup_matches_source(cursor, table, backup_table):
    cursor.execute(f"SHOW COLUMNS FROM `{table}`")
    source_columns = cursor.fetchall()
    cursor.execute(f"SHOW COLUMNS FROM `{backup_table}`")
    backup_columns = cursor.fetchall()
    source_signature = [
        (row[0], row[1].lower(), row[2], row[3], (row[5] or "").lower())
        for row in source_columns
    ]
    backup_signature = [
        (row[0], row[1].lower(), row[2], row[3], (row[5] or "").lower())
        for row in backup_columns
    ]
    if source_signature != backup_signature:
        raise RuntimeError(
            f"Existing backup '{backup_table}' has a different schema from '{table}'. "
            "It was not overwritten."
        )
    primary_key = primary_key_columns(cursor, table)
    if not primary_key:
        raise RuntimeError(f"Cannot verify backup table '{backup_table}' without a primary key.")
    cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
    source_count = cursor.fetchone()[0]
    cursor.execute(f"SELECT COUNT(*) FROM `{backup_table}`")
    backup_count = cursor.fetchone()[0]
    if source_count != backup_count:
        raise RuntimeError(
            f"Existing backup '{backup_table}' contains {backup_count} rows, "
            f"but '{table}' contains {source_count}; it was not overwritten."
        )

    join = " AND ".join(
        f"source.`{column}` = snapshot.`{column}`" for column in primary_key
    )
    same_values = " AND ".join(
        f"source.`{column}` <=> snapshot.`{column}`"
        for column in (row[0] for row in source_columns)
    )
    for left_table, right_table in ((table, backup_table), (backup_table, table)):
        cursor.execute(
            f"SELECT COUNT(*) FROM `{left_table}` AS source "
            f"LEFT JOIN `{right_table}` AS snapshot ON {join} "
            f"WHERE snapshot.`{primary_key[0]}` IS NULL OR NOT ({same_values})"
        )
        if cursor.fetchone()[0]:
            raise RuntimeError(
                f"Existing backup '{backup_table}' does not contain the exact data "
                f"from '{table}'; it was not overwritten."
            )
    return source_count


def backup_existing_data(connection, cursor):
    manifest_table = "ehr_migration_backup_manifest"
    manifest_exists = table_exists(cursor, manifest_table)
    verified_snapshots = []
    for table, backup_table in MIGRATION_BACKUPS.items():
        source_exists = table_exists(cursor, table)
        backup_exists = table_exists(cursor, backup_table)
        manifest = None
        if manifest_exists:
            cursor.execute(
                "SELECT backup_table, source_count, snapshot_sha256 "
                f"FROM `{manifest_table}` WHERE table_name = %s",
                (table,),
            )
            manifest = cursor.fetchone()
        if not source_exists:
            if manifest or backup_exists:
                raise RuntimeError(
                    f"Migration source table '{table}' is missing while its backup "
                    f"'{backup_table}' exists. Resolve this without deleting either table."
                )
            continue
        if manifest:
            if manifest[0] != backup_table or not backup_exists:
                raise RuntimeError(
                    f"Backup manifest for '{table}' does not match the existing "
                    f"'{backup_table}' table. No backup was replaced."
                )
            cursor.execute(f"SELECT COUNT(*) FROM `{backup_table}`")
            if cursor.fetchone()[0] != manifest[1] or table_digest(cursor, backup_table) != manifest[2]:
                raise RuntimeError(
                    f"Verified backup '{backup_table}' has changed since its snapshot "
                    "was recorded. No migration was started."
                )
            continue
        if not backup_exists:
            cursor.execute(f"CREATE TABLE `{backup_table}` LIKE `{table}`")
            cursor.execute(f"INSERT INTO `{backup_table}` SELECT * FROM `{table}`")
        source_count = verify_backup_matches_source(cursor, table, backup_table)
        snapshot_digest = table_digest(cursor, backup_table)
        verified_snapshots.append(
            (table, backup_table, source_count, snapshot_digest)
        )

    if not manifest_exists:
        cursor.execute(
            f"CREATE TABLE `{manifest_table}` ("
            "table_name VARCHAR(64) NOT NULL PRIMARY KEY, "
            "backup_table VARCHAR(128) NOT NULL, "
            "source_count BIGINT UNSIGNED NOT NULL, "
            "snapshot_sha256 CHAR(64) NOT NULL, "
            "verified_at DATETIME(6) NOT NULL)"
        )
    for snapshot in verified_snapshots:
        cursor.execute(
            f"INSERT INTO `{manifest_table}` "
            "(table_name, backup_table, source_count, snapshot_sha256, verified_at) "
            "VALUES (%s, %s, %s, %s, NOW(6))",
            snapshot,
        )
    connection.commit()


def ensure_patient_visit_migration(connection, cursor):
    cursor.execute("SHOW COLUMNS FROM ehr_records")
    columns = {row[0]: row for row in cursor.fetchall()}
    if "patient_id" not in columns or "created_by" not in columns:
        raise RuntimeError(
            "Existing ehr_records must contain patient_id and created_by before "
            "patient-profile migration can proceed."
        )
    if columns["patient_id"][1].lower() != "bigint unsigned":
        raise RuntimeError(
            "Existing ehr_records.patient_id must be BIGINT UNSIGNED; "
            "no incompatible column type was changed."
        )
    if (
        "patient_profile_id" in columns
        and columns["patient_profile_id"][1].lower() != "bigint unsigned"
    ):
        raise RuntimeError(
            "Existing ehr_records.patient_profile_id must be BIGINT UNSIGNED; "
            "no incompatible column type was changed."
        )
    cursor.execute("SHOW COLUMNS FROM patient_profiles")
    profile_columns = {row[0]: row for row in cursor.fetchall()}
    if "email" not in profile_columns:
        cursor.execute("ALTER TABLE patient_profiles ADD COLUMN email VARCHAR(255) NULL")
    cursor.execute("SHOW INDEX FROM patient_profiles")
    profile_indexes = {row[2] for row in cursor.fetchall()}
    if "ix_patient_profile_email" not in profile_indexes:
        cursor.execute("CREATE INDEX ix_patient_profile_email ON patient_profiles (email)")
    cursor.execute(
        "UPDATE patient_profiles AS p JOIN users AS u ON u.id = p.linked_user_id "
        "SET p.email = u.email WHERE p.email IS NULL"
    )
    cursor.execute(
        "INSERT INTO patient_profiles "
        "(patient_code, linked_user_id, full_name, age, gender, mobile_number, email, created_at) "
        "SELECT CONCAT('PENDING-U-', u.id), u.id, u.name, "
        "TIMESTAMPDIFF(YEAR, DATE(u.date_of_birth), CURDATE()), u.gender, u.phone, "
        "u.email, u.created_at FROM users AS u "
        "LEFT JOIN patient_profiles AS linked ON linked.linked_user_id = u.id "
        "WHERE u.`role` = 'patient' AND linked.id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM patient_profiles AS p "
        "WHERE LOWER(TRIM(p.email)) = LOWER(TRIM(u.email)))"
    )
    cursor.execute(
        "UPDATE patient_profiles SET patient_code = CONCAT('EHR-', LPAD(id, 10, '0')) "
        "WHERE patient_code LIKE 'PENDING-U-%'"
    )
    additions = {
        "patient_profile_id": "BIGINT UNSIGNED NULL",
        "doctor_name": "VARCHAR(120) NULL",
        "hospital_name": "VARCHAR(255) NULL",
        "additional_instructions": "TEXT NULL",
        "follow_up_date": "DATE NULL",
    }
    for column, column_type in additions.items():
        if column not in columns:
            cursor.execute(
                f"ALTER TABLE ehr_records ADD COLUMN `{column}` {column_type}"
            )

    cursor.execute(
        "SELECT COUNT(*) FROM ehr_records AS r "
        "JOIN patient_profiles AS p ON p.id = r.patient_profile_id "
        "JOIN users AS u ON u.id = r.patient_id "
        "WHERE r.patient_id IS NOT NULL AND ("
        "(p.linked_user_id IS NOT NULL AND p.linked_user_id <> u.id) OR "
        "(p.linked_user_id IS NULL "
        "AND (LOWER(TRIM(COALESCE(p.email, ''))) <> LOWER(TRIM(u.email)) "
        "OR LOWER(TRIM(p.full_name)) <> LOWER(TRIM(u.name)) "
        "OR TRIM(COALESCE(p.mobile_number, '')) <> TRIM(COALESCE(u.phone, '')))))"
    )
    if cursor.fetchone()[0]:
        raise RuntimeError(
            "Existing EHR rows conflict with their patient profiles. "
            "The backup snapshots were retained; resolve the mismatch before retrying setup."
        )

    cursor.execute(
        "UPDATE ehr_records AS r "
        "JOIN users AS u ON u.id = r.patient_id "
        "JOIN patient_profiles AS p ON p.linked_user_id = u.id "
        "SET r.patient_profile_id = p.id WHERE r.patient_profile_id IS NULL"
    )
    cursor.execute(
        "UPDATE ehr_records AS r "
        "JOIN users AS u ON u.id = r.patient_id "
        "JOIN patient_profiles AS p ON p.linked_user_id IS NULL "
        "AND LOWER(TRIM(p.email)) = LOWER(TRIM(u.email)) "
        "AND LOWER(TRIM(p.full_name)) = LOWER(TRIM(u.name)) "
        "AND TRIM(p.mobile_number) = TRIM(u.phone) "
        "SET r.patient_profile_id = p.id "
        "WHERE r.patient_profile_id IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM patient_profiles AS linked "
        "WHERE linked.linked_user_id = u.id) "
        "AND (SELECT COUNT(*) FROM patient_profiles AS candidate "
        "WHERE candidate.linked_user_id IS NULL "
        "AND LOWER(TRIM(candidate.email)) = LOWER(TRIM(u.email)) "
        "AND LOWER(TRIM(candidate.full_name)) = LOWER(TRIM(u.name)) "
        "AND TRIM(candidate.mobile_number) = TRIM(u.phone)) = 1"
    )
    cursor.execute(
        "UPDATE ehr_records AS r JOIN users AS u ON u.id = r.created_by "
        "SET r.doctor_name = u.name WHERE r.doctor_name IS NULL"
    )
    cursor.execute(
        "SELECT COUNT(*) FROM ehr_records WHERE patient_profile_id IS NULL"
    )
    if cursor.fetchone()[0]:
        raise RuntimeError(
            "Some existing EHR rows have no patient profile. The backup snapshots were retained; "
            "resolve these rows before retrying setup."
        )

    if columns["patient_id"][2] == "NO":
        cursor.execute(
            "ALTER TABLE ehr_records MODIFY COLUMN patient_id BIGINT UNSIGNED NULL"
        )
    cursor.execute(
        "ALTER TABLE ehr_records MODIFY COLUMN patient_profile_id BIGINT UNSIGNED NOT NULL"
    )

    cursor.execute("SHOW INDEX FROM ehr_records")
    indexes = {row[2] for row in cursor.fetchall()}
    if "ix_ehr_patient_profile_date" not in indexes:
        cursor.execute(
            "CREATE INDEX ix_ehr_patient_profile_date "
            "ON ehr_records (patient_profile_id, record_date)"
        )

    cursor.execute(
        "SELECT CONSTRAINT_NAME FROM information_schema.KEY_COLUMN_USAGE "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'ehr_records' "
        "AND COLUMN_NAME = 'patient_profile_id' AND REFERENCED_TABLE_NAME = 'patient_profiles'",
        (os.getenv("MYSQL_DATABASE", "ehr_blockchain"),),
    )
    if cursor.fetchone() is None:
        cursor.execute(
            "ALTER TABLE ehr_records ADD CONSTRAINT fk_ehr_patient_profile "
            "FOREIGN KEY (patient_profile_id) REFERENCES patient_profiles (id)"
        )
    connection.commit()


def validate_foreign_key_column_types(cursor):
    references = (
        ("patient_profiles", "linked_user_id", "users", "id"),
        ("ehr_records", "patient_id", "users", "id"),
        ("ehr_records", "patient_profile_id", "patient_profiles", "id"),
        ("ehr_records", "created_by", "users", "id"),
        ("password_reset_tokens", "user_id", "users", "id"),
        ("patient_profile_link_verifications", "patient_profile_id", "patient_profiles", "id"),
        ("patient_profile_link_verifications", "user_id", "users", "id"),
        ("prescription_medicines", "record_id", "ehr_records", "id"),
        ("medical_reports", "patient_profile_id", "patient_profiles", "id"),
        ("medical_reports", "record_id", "ehr_records", "id"),
        ("medical_reports", "uploaded_by", "users", "id"),
        ("patient_email_verifications", "patient_profile_id", "patient_profiles", "id"),
    )
    identifier_columns = {
        (table, "id")
        for table in (
            "users", "patient_profiles", "ehr_records",
            "password_reset_tokens", "patient_profile_link_verifications",
            "prescription_medicines", "medical_reports",
            "patient_email_verifications",
        )
    }
    identifier_columns.update(
        (table, column) for table, column, _, _ in references
    )
    columns_by_table = {}
    for table, column in identifier_columns:
        if table not in columns_by_table:
            cursor.execute(f"SHOW COLUMNS FROM `{table}`")
            columns_by_table[table] = {
                row[0]: row for row in cursor.fetchall()
            }
        columns = columns_by_table[table]
        if column not in columns:
            raise RuntimeError(
                f"Table '{table}' is missing foreign-key column '{column}'."
            )
        if columns[column][1].lower() != "bigint unsigned":
            raise RuntimeError(
                f"Column '{table}.{column}' must be BIGINT UNSIGNED to match "
                "its referenced identifier. No incompatible type was changed."
            )

    database = os.getenv("MYSQL_DATABASE", "ehr_blockchain")
    for table, column, referenced_table, referenced_column in references:
        cursor.execute(
            "SELECT REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
            "FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s "
            "AND REFERENCED_TABLE_NAME IS NOT NULL",
            (database, table, column),
        )
        targets = cursor.fetchall()
        if targets != [(referenced_table, referenced_column)]:
            raise RuntimeError(
                f"Foreign key '{table}.{column}' must reference "
                f"'{referenced_table}.{referenced_column}'."
            )


def main():
    database = os.getenv("MYSQL_DATABASE", "ehr_blockchain")
    user = os.getenv("MYSQL_USER", "")
    backup_file = Path(
        os.getenv("MYSQL_BACKUP_FILE")
        or BASE_DIR.parent.parent / "ehr_blockchain_backup.sql"
    ).expanduser()
    if not backup_file.is_file() or backup_file.stat().st_size == 0:
        raise SystemExit(
            "A non-empty SQL export is required before database setup. "
            "Set MYSQL_BACKUP_FILE or place ehr_blockchain_backup.sql beside the project folder."
        )
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
        cursor = DiagnosticCursor(connection.cursor())
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
        cursor = DiagnosticCursor(connection.cursor())
        if schema_changes_needed(cursor):
            backup_existing_data(connection, cursor)
        for statement in SCHEMA:
            cursor.execute(statement)
        ensure_blockchain_action_enum(connection, cursor)
        initialize_blockchain_genesis(connection, cursor)
        baseline_blocks = initialize_historical_baseline(connection, cursor)
        if baseline_blocks:
            print(
                f"Historical baseline audit created for {len(baseline_blocks)} demo records "
                f"(Blocks {baseline_blocks[0].index}..{baseline_blocks[-1].index})."
            )
        ensure_patient_visit_migration(connection, cursor)
        validate_foreign_key_column_types(cursor)
        for table, expected_columns in REQUIRED_COLUMNS.items():
            cursor.execute(f"SHOW COLUMNS FROM `{table}`")
            column_rows = cursor.fetchall()
            actual_columns = {row[0] for row in column_rows}
            missing = expected_columns - actual_columns
            if missing:
                raise RuntimeError(
                    f"Existing table '{table}' is missing required columns: "
                    f"{', '.join(sorted(missing))}. The migration may be partially applied; "
                    "use the verified backups and resolve the schema before retrying."
                )
            id_column = next(row for row in column_rows if row[0] == "id")
            if id_column[3] != "PRI" or "auto_increment" not in id_column[5].lower():
                raise RuntimeError(
                    f"Existing table '{table}' does not have an auto-increment primary key. "
                    "The migration may be partially applied; resolve the schema before retrying."
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
                "The migration may be partially applied; resolve the schema before retrying."
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
                "The migration may be partially applied; resolve the schema before retrying."
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