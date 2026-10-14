"""Flask API for EHR authentication and MySQL-backed record access."""

import hashlib
import os
import re
import secrets
import smtplib
import ssl
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import quote

import jwt
from dotenv import load_dotenv
from flask import Flask, current_app, jsonify, request
from flask_cors import CORS
import mysql.connector
from mysql.connector.errors import Error as MySQLError, IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ALLOWED_ROLES = {"admin", "doctor", "patient"}
ALLOWED_GENDERS = {"female", "male", "non-binary", "prefer-not-to-say"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
RECORD_FIELDS = ("medical_condition", "blood_pressure", "age", "gender", "diagnosis", "treatment")


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(
        SECRET_KEY=os.getenv("JWT_SECRET", "change-this-secret"),
        JWT_EXPIRY_HOURS=int(os.getenv("JWT_EXPIRY_HOURS", "8")),
        MYSQL_HOST=os.getenv("MYSQL_HOST", "localhost"),
        MYSQL_PORT=int(os.getenv("MYSQL_PORT", "3306")),
        MYSQL_DATABASE=os.getenv("MYSQL_DATABASE", "ehr_blockchain"),
        MYSQL_USER=os.getenv("MYSQL_USER", ""),
        MYSQL_PASSWORD=os.getenv("MYSQL_PASSWORD", ""),
        DEBUG=os.getenv("FLASK_DEBUG", "false").lower() == "true",
        PASSWORD_RESET_DEV_MODE=os.getenv("PASSWORD_RESET_DEV_MODE", "false").lower() == "true",
        PASSWORD_RESET_TTL_MINUTES=int(os.getenv("PASSWORD_RESET_TTL_MINUTES", "30")),
        FRONTEND_BASE_URL=os.getenv("FRONTEND_BASE_URL", "http://127.0.0.1:5500").rstrip("/"),
        MAIL_SERVER=os.getenv("MAIL_SERVER", ""),
        MAIL_PORT=int(os.getenv("MAIL_PORT", "587")),
        MAIL_USERNAME=os.getenv("MAIL_USERNAME", ""),
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD", ""),
        MAIL_FROM=os.getenv("MAIL_FROM", ""),
    )
    if test_config:
        app.config.update(test_config)

    configured_origins = os.getenv("CORS_ORIGINS", "*")
    origins = "*" if configured_origins.strip() == "*" else [
        origin.strip() for origin in configured_origins.split(",") if origin.strip()
    ]
    CORS(app, resources={r"/api/*": {"origins": origins, "allow_headers": ["Content-Type", "Authorization"]}})

    @app.errorhandler(404)
    def handle_not_found(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "API endpoint not found."}), 404
        return error

    @app.errorhandler(405)
    def handle_method_not_allowed(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "HTTP method is not allowed for this API endpoint."}), 405
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        app.logger.exception("Unhandled API error", exc_info=error)
        if request.path.startswith("/api/"):
            return jsonify({"error": "An unexpected server error occurred."}), 500
        raise error

    @app.get("/api/health")
    def health_check():
        try:
            fetch_one("SELECT 1 AS ok")
            return jsonify({"status": "ok", "service": "ehr-secure-api", "database": "connected"})
        except (MySQLError, RuntimeError):
            app.logger.exception("MySQL health check failed")
            return jsonify({"status": "error", "service": "ehr-secure-api", "database": "unavailable"}), 503

    @app.post("/api/login")
    def login():
        payload = request.get_json(silent=True) or {}
        email = str(payload.get("email", "")).strip().lower()
        password = payload.get("password", "")
        role = str(payload.get("role", "")).strip().lower()
        if not email or not isinstance(password, str) or not password or role not in ALLOWED_ROLES:
            return jsonify({"error": "Email, password, and a valid role are required."}), 400

        try:
            user = fetch_one(
                "SELECT id AS _id, name, email, password_hash, role, status "
                "FROM users WHERE email = %s",
                (email,),
            )
        except (MySQLError, RuntimeError):
            app.logger.exception("Login database error")
            return jsonify({"error": "The authentication service is temporarily unavailable."}), 503

        if (
            not user
            or user.get("role") != role
            or user.get("status", "active") != "active"
            or not check_password_hash(user.get("password_hash", ""), password)
        ):
            return jsonify({"error": "Invalid email, password, or role."}), 401

        token = create_access_token(app, user)
        return jsonify({
            "message": "Login successful",
            "token": token,
            "user": {
                "id": str(user["_id"]), "name": user["name"],
                "email": user["email"], "role": user["role"],
            },
        })

    @app.post("/api/password-reset/request")
    def request_password_reset():
        payload = request.get_json(silent=True) or {}
        email = str(payload.get("email", "")).strip().lower()
        generic_message = (
            "If an eligible account exists and reset delivery is configured, "
            "password reset instructions will be sent."
        )
        if not EMAIL_PATTERN.fullmatch(email):
            return jsonify({"message": generic_message})

        try:
            user = fetch_one(
                "SELECT id, email, role FROM users "
                "WHERE email = %s AND status = 'active' AND `role` IN ('doctor', 'patient')",
                (email,),
            )
            if not user:
                return jsonify({"message": generic_message})

            development_preview = (
                app.config["DEBUG"]
                and app.config["PASSWORD_RESET_DEV_MODE"]
                and request.remote_addr in {"127.0.0.1", "::1"}
            )
            email_delivery = bool(app.config["MAIL_SERVER"] and app.config["MAIL_FROM"])
            if not development_preview and not email_delivery:
                return jsonify({"message": generic_message})

            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            expires_at = now + timedelta(minutes=app.config["PASSWORD_RESET_TTL_MINUTES"])
            issue_password_reset_token(user["id"], token_hash, now, expires_at)
            reset_url = (
                f"{app.config['FRONTEND_BASE_URL']}/reset-password.HTML"
                f"#token={quote(token, safe='')}"
            )

            if email_delivery:
                try:
                    send_password_reset_email(app.config, user["email"], reset_url)
                except (OSError, smtplib.SMTPException):
                    app.logger.exception("Password-reset email delivery failed")
                    revoke_password_reset_token(token_hash, now)
                    return jsonify({"message": generic_message})

            response = {"message": generic_message}
            if development_preview:
                response["reset_url"] = reset_url
            return jsonify(response)
        except (MySQLError, RuntimeError):
            app.logger.exception("Password-reset request failed")
            return jsonify({"error": "Password reset is temporarily unavailable."}), 503

    @app.post("/api/password-reset/confirm")
    def confirm_password_reset():
        payload = request.get_json(silent=True) or {}
        token = payload.get("token")
        password = payload.get("password")
        confirm_password = payload.get("confirm_password")
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return jsonify({"error": "This reset link is invalid or incomplete."}), 400
        if not isinstance(password, str) or not 8 <= len(password) <= 256:
            return jsonify({"error": "Password must be between 8 and 256 characters."}), 400
        if password != confirm_password:
            return jsonify({"error": "Passwords do not match."}), 400

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        password_hash = generate_password_hash(password)
        try:
            result = consume_password_reset_token(token_hash, password_hash)
        except (MySQLError, RuntimeError):
            app.logger.exception("Password-reset confirmation failed")
            return jsonify({"error": "Password reset is temporarily unavailable."}), 503

        messages = {
            "invalid": "This reset link is invalid.",
            "expired": "This reset link has expired. Request a new one.",
            "used": "This reset link has already been used. Request a new one.",
            "success": "Your password has been reset. You can now sign in.",
        }
        if result != "success":
            return jsonify({"error": messages[result]}), 400
        return jsonify({"message": messages[result]})

    @app.post("/api/register")
    def register():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        email = str(payload.get("email", "")).strip().lower()
        password = payload.get("password", "")
        confirm_password = payload.get("confirm_password", "")
        date_of_birth = str(payload.get("date_of_birth", "")).strip()
        gender = str(payload.get("gender", "")).strip().lower()
        phone = str(payload.get("phone", "")).strip()

        errors = {}
        if len(name) < 2 or len(name) > 120:
            errors["name"] = "Full name must be between 2 and 120 characters."
        if len(email) > 255 or not EMAIL_PATTERN.fullmatch(email):
            errors["email"] = "Enter a valid email address."
        if not isinstance(password, str) or len(password) < 8 or len(password) > 256:
            errors["password"] = "Password must be between 8 and 256 characters."
        if password != confirm_password:
            errors["confirm_password"] = "Passwords do not match."
        try:
            birth_date = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
            if birth_date >= date.today():
                errors["date_of_birth"] = "Date of birth must be in the past."
        except ValueError:
            errors["date_of_birth"] = "Enter a valid date of birth."
        if gender not in ALLOWED_GENDERS:
            errors["gender"] = "Select a valid gender."
        if not phone or len(phone) > 30:
            errors["phone"] = "Phone number is required and must be 30 characters or fewer."
        if errors:
            return jsonify({"error": "Please correct the highlighted fields.", "fields": errors}), 400

        user = {
            "name": name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "patient",
            "status": "active",
            "date_of_birth": datetime.combine(birth_date, datetime.min.time(), tzinfo=timezone.utc),
            "gender": gender,
            "phone": phone,
            "created_at": datetime.now(timezone.utc),
        }
        try:
            insert_user(user)
        except IntegrityError as error:
            if error.errno == 1062:
                return jsonify({"error": "An account with this email already exists."}), 409
            app.logger.exception("Registration integrity error")
            return jsonify({"error": "The registration service is temporarily unavailable."}), 503
        except (MySQLError, RuntimeError):
            app.logger.exception("Registration database error")
            return jsonify({"error": "The registration service is temporarily unavailable."}), 503
        return jsonify({"message": "Account created successfully. Please login."}), 201

    @app.get("/api/records")
    @require_auth(app, roles={"doctor", "patient"})
    def get_records(current_user):
        try:
            query = {}
            if current_user["role"] == "patient":
                query["patient_id"] = parse_database_id(current_user["id"])
            if current_user["role"] == "patient":
                records = fetch_records(query["patient_id"])
            else:
                records = fetch_records(include_patient_accounts=True)
        except ValueError:
            return jsonify({"error": "Your session is invalid. Please sign in again."}), 401
        except MySQLError:
            app.logger.exception("Record query failed")
            return jsonify({"error": "Unable to retrieve records."}), 503
        return jsonify({"records": [serialize_document(record) for record in records]})

    @app.get("/api/demo-records")
    @require_auth(app, roles={"doctor"})
    def get_demo_records(current_user):
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "25"))
        except ValueError:
            return jsonify({"error": "page and per_page must be integers."}), 400
        if page < 1 or per_page < 1 or per_page > 100:
            return jsonify({"error": "page must be positive and per_page must be between 1 and 100."}), 400
        search = request.args.get("search", "").strip()
        try:
            if search:
                result = fetch_demo_records(page, per_page, search)
            else:
                result = fetch_demo_records(page, per_page)
        except (MySQLError, RuntimeError):
            app.logger.exception("Demo record query failed")
            return jsonify({"error": "Unable to retrieve demo records."}), 503
        return jsonify({
            "demo_data": True,
            "records": [serialize_document(record) for record in result["records"]],
            "page": page,
            "per_page": per_page,
            "total": result["total"],
            "pages": (result["total"] + per_page - 1) // per_page,
        })

    @app.get("/api/doctor-dashboard/records")
    @require_auth(app, roles={"doctor"})
    def get_doctor_dashboard_records(current_user):
        try:
            page = int(request.args.get("page", "1"))
            per_page = int(request.args.get("per_page", "25"))
        except ValueError:
            return jsonify({"error": "page and per_page must be integers."}), 400
        if page < 1 or per_page < 1 or per_page > 100:
            return jsonify({"error": "page must be positive and per_page must be between 1 and 100."}), 400

        search = request.args.get("search", "").strip()
        try:
            result = fetch_doctor_dashboard_records(page, per_page, search)
        except (MySQLError, RuntimeError):
            app.logger.exception("Doctor dashboard record query failed")
            return jsonify({"error": "Unable to retrieve dashboard records."}), 503
        return jsonify({
            "combined_data": True,
            "records": [serialize_document(record) for record in result["records"]],
            "page": page,
            "per_page": per_page,
            "total": result["total"],
            "pages": (result["total"] + per_page - 1) // per_page,
        })

    @app.post("/api/records")
    @require_auth(app, roles={"doctor"})
    def add_record(current_user):
        payload = request.get_json(silent=True) or {}
        required = ("patient_id", "medical_condition", "diagnosis", "treatment")
        missing = [field for field in required if not payload.get(field)]
        if missing:
            return jsonify({"error": "Missing required fields", "fields": missing}), 400
        try:
            patient_id = parse_database_id(payload["patient_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "patient_id must be a valid patient ID."}), 400
        try:
            patient = fetch_one(
                "SELECT id FROM users WHERE id = %s AND role = %s",
                (patient_id, "patient"),
            )
        except MySQLError:
            app.logger.exception("Patient lookup failed")
            return jsonify({"error": "Unable to create the record."}), 503
        if patient is None:
            return jsonify({"error": "Patient account not found."}), 404

        record_date = payload.get("record_date")
        if record_date:
            try:
                record_date = datetime.strptime(record_date, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "record_date must use YYYY-MM-DD format."}), 400
        else:
            record_date = date.today()
        record = {
            "patient_id": patient_id,
            "medical_condition": str(payload["medical_condition"]).strip(),
            "blood_pressure": payload.get("blood_pressure"),
            "age": payload.get("age"),
            "gender": payload.get("gender"),
            "diagnosis": str(payload["diagnosis"]).strip(),
            "treatment": str(payload["treatment"]).strip(),
            "record_date": datetime.combine(record_date, datetime.min.time()),
            "created_by": None,
            "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        try:
            record["created_by"] = parse_database_id(current_user["id"])
            record_id = insert_record(record)
        except ValueError:
            return jsonify({"error": "Your session is invalid. Please sign in again."}), 401
        except MySQLError:
            app.logger.exception("Record insert failed")
            return jsonify({"error": "Unable to create the record."}), 503
        return jsonify({"message": "Record created", "id": str(record_id)}), 201

    @app.put("/api/records/<record_id>")
    @require_auth(app, roles={"doctor"})
    def update_record(current_user, record_id):
        payload = request.get_json(silent=True) or {}
        updates = {field: payload[field] for field in RECORD_FIELDS if field in payload}
        if not updates:
            return jsonify({"error": "At least one editable field is required."}), 400
        if "medical_condition" in updates:
            updates["medical_condition"] = str(updates["medical_condition"]).strip()
        if "diagnosis" in updates:
            updates["diagnosis"] = str(updates["diagnosis"]).strip()
        if "treatment" in updates:
            updates["treatment"] = str(updates["treatment"]).strip()
        updates["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            matched = update_record_fields(parse_database_id(record_id), updates)
        except ValueError:
            return jsonify({"error": "Record not found."}), 404
        except MySQLError:
            app.logger.exception("Record update failed")
            return jsonify({"error": "Unable to update the record."}), 503
        if not matched:
            return jsonify({"error": "Record not found."}), 404
        return jsonify({"message": "Record updated"})

    @app.get("/api/admin/doctors")
    @require_auth(app, roles={"admin"})
    def list_doctors(current_user):
        doctors = fetch_doctors()
        return jsonify({"doctors": [serialize_document(doctor) for doctor in doctors]})

    @app.post("/api/admin/doctors")
    @require_auth(app, roles={"admin"})
    def create_doctor(current_user):
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name", "")).strip()
        email = str(payload.get("email", "")).strip().lower()
        password = payload.get("password", "")
        if len(name) < 2 or len(name) > 120 or not EMAIL_PATTERN.fullmatch(email) or len(password) < 8:
            return jsonify({"error": "Valid name, email, and a password of at least 8 characters are required."}), 400
        doctor = {
            "name": name, "email": email, "password_hash": generate_password_hash(password),
            "role": "doctor", "status": "active", "created_at": datetime.now(timezone.utc),
        }
        try:
            doctor_id = insert_user(doctor)
        except IntegrityError as error:
            if error.errno == 1062:
                return jsonify({"error": "An account with this email already exists."}), 409
            app.logger.exception("Doctor creation integrity error")
            return jsonify({"error": "Unable to create doctor account."}), 503
        except MySQLError:
            app.logger.exception("Doctor creation failed")
            return jsonify({"error": "Unable to create doctor account."}), 503
        return jsonify({"message": "Doctor account created", "id": str(doctor_id)}), 201

    @app.patch("/api/admin/doctors/<doctor_id>/status")
    @require_auth(app, roles={"admin"})
    def update_doctor_status(current_user, doctor_id):
        status = (request.get_json(silent=True) or {}).get("status")
        if status not in {"active", "inactive"}:
            return jsonify({"error": "Status must be active or inactive."}), 400
        try:
            matched = set_doctor_status(parse_database_id(doctor_id), status)
        except ValueError:
            return jsonify({"error": "Doctor not found."}), 404
        except MySQLError:
            app.logger.exception("Doctor status update failed")
            return jsonify({"error": "Unable to update doctor account."}), 503
        if not matched:
            return jsonify({"error": "Doctor not found."}), 404
        return jsonify({"message": "Doctor status updated"})

    return app


def issue_password_reset_token(user_id, token_hash, created_at, expires_at):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "UPDATE password_reset_tokens SET used_at = %s "
            "WHERE user_id = %s AND used_at IS NULL",
            (created_at, user_id),
        )
        cursor.execute(
            "INSERT INTO password_reset_tokens "
            "(user_id, token_hash, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (user_id, token_hash, created_at, expires_at),
        )


def revoke_password_reset_token(token_hash, used_at):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "UPDATE password_reset_tokens SET used_at = %s "
            "WHERE token_hash = %s AND used_at IS NULL",
            (used_at, token_hash),
        )


def consume_password_reset_token(token_hash, password_hash):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT t.user_id, t.expires_at, t.used_at, u.`role`, u.status "
            "FROM password_reset_tokens AS t "
            "JOIN users AS u ON u.id = t.user_id "
            "WHERE t.token_hash = %s FOR UPDATE",
            (token_hash,),
        )
        reset = cursor.fetchone()
        if not reset:
            return "invalid"
        if reset["used_at"] is not None:
            return "used"
        expires_at = reset["expires_at"]
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if expires_at <= now:
            return "expired"
        if reset["role"] not in {"doctor", "patient"} or reset["status"] != "active":
            return "invalid"

        cursor.execute(
            "UPDATE users SET password_hash = %s "
            "WHERE id = %s AND status = 'active' AND `role` IN ('doctor', 'patient')",
            (password_hash, reset["user_id"]),
        )
        if cursor.rowcount != 1:
            return "invalid"
        cursor.execute(
            "UPDATE password_reset_tokens SET used_at = %s "
            "WHERE user_id = %s AND used_at IS NULL",
            (now, reset["user_id"]),
        )
    return "success"


def send_password_reset_email(config, recipient, reset_url):
    message = EmailMessage()
    message["Subject"] = "Reset your EHR Secure password"
    message["From"] = config["MAIL_FROM"]
    message["To"] = recipient
    message.set_content(
        "A password reset was requested for your EHR Secure account.\n\n"
        f"Use this one-time link within {config['PASSWORD_RESET_TTL_MINUTES']} minutes:\n"
        f"{reset_url}\n\n"
        "If you did not request this reset, you can ignore this message."
    )
    context = ssl.create_default_context()
    with smtplib.SMTP(config["MAIL_SERVER"], config["MAIL_PORT"], timeout=10) as server:
        server.starttls(context=context)
        if config["MAIL_USERNAME"]:
            server.login(config["MAIL_USERNAME"], config["MAIL_PASSWORD"])
        server.send_message(message)


@contextmanager
def database_cursor(commit=False):
    config = current_app.config
    missing = [key for key in ("MYSQL_DATABASE", "MYSQL_USER") if not config.get(key)]
    if missing:
        raise RuntimeError(f"Missing MySQL configuration: {', '.join(missing)}.")

    connection = mysql.connector.connect(
        host=config["MYSQL_HOST"],
        port=config["MYSQL_PORT"],
        database=config["MYSQL_DATABASE"],
        user=config["MYSQL_USER"],
        password=config["MYSQL_PASSWORD"],
    )
    cursor = None
    try:
        cursor = connection.cursor(dictionary=True)
        yield cursor
        if commit:
            connection.commit()
    except Exception:
        if commit:
            connection.rollback()
        raise
    finally:
        try:
            if cursor is not None:
                cursor.close()
        finally:
            connection.close()


def fetch_one(statement, parameters=()):
    with database_cursor() as cursor:
        cursor.execute(statement, parameters)
        return cursor.fetchone()


def fetch_all(statement, parameters=()):
    with database_cursor() as cursor:
        cursor.execute(statement, parameters)
        return cursor.fetchall()


def execute_statement(statement, parameters=()):
    with database_cursor(commit=True) as cursor:
        cursor.execute(statement, parameters)
        return cursor.lastrowid, cursor.rowcount


def insert_user(user):
    statement = (
        "INSERT INTO users "
        "(name, email, password_hash, role, status, date_of_birth, gender, phone, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = (
        user["name"], user["email"], user["password_hash"], user["role"], user["status"],
        mysql_datetime(user.get("date_of_birth")), user.get("gender"), user.get("phone"),
        mysql_datetime(user["created_at"]),
    )
    inserted_id, _ = execute_statement(statement, values)
    return inserted_id


def insert_record(record):
    statement = (
        "INSERT INTO ehr_records "
        "(patient_id, medical_condition, blood_pressure, age, gender, diagnosis, treatment, "
        "record_date, created_by, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    values = tuple(record[field] for field in (
        "patient_id", "medical_condition", "blood_pressure", "age", "gender", "diagnosis",
        "treatment", "record_date", "created_by", "updated_at",
    ))
    values = values[:7] + (
        mysql_datetime(values[7]), values[8], mysql_datetime(values[9]),
    )
    inserted_id, _ = execute_statement(statement, values)
    return inserted_id


def fetch_records(patient_id=None, include_patient_accounts=False):
    if include_patient_accounts and patient_id is not None:
        raise ValueError("Patient account searches cannot be scoped to a patient ID.")
    if include_patient_accounts:
        statement = (
            "SELECT ehr_records.id AS _id, users.id AS patient_id, users.name AS patient_name, "
            "ehr_records.medical_condition, ehr_records.blood_pressure, ehr_records.age, "
            "ehr_records.gender, ehr_records.diagnosis, ehr_records.treatment, "
            "ehr_records.record_date, ehr_records.created_by, ehr_records.updated_at "
            "FROM users LEFT JOIN ehr_records ON ehr_records.patient_id = users.id "
            "WHERE users.`role` = 'patient' AND users.status = 'active' "
            "ORDER BY users.name, ehr_records.record_date DESC"
        )
        return fetch_all(statement)

    statement = (
        "SELECT ehr_records.id AS _id, ehr_records.patient_id, users.name AS patient_name, "
        "ehr_records.medical_condition, ehr_records.blood_pressure, ehr_records.age, "
        "ehr_records.gender, ehr_records.diagnosis, ehr_records.treatment, "
        "ehr_records.record_date, ehr_records.created_by, ehr_records.updated_at "
        "FROM ehr_records JOIN users ON users.id = ehr_records.patient_id"
    )
    parameters = ()
    if patient_id is not None:
        statement += " WHERE ehr_records.patient_id = %s"
        parameters = (patient_id,)
    statement += " ORDER BY ehr_records.record_date DESC"
    return fetch_all(statement, parameters)


def fetch_demo_records(page, per_page, search=""):
    offset = (page - 1) * per_page
    where_clause = "WHERE is_demo = TRUE"
    search_parameters = ()
    search = search.strip()
    if search:
        where_clause += (
            " AND (LOCATE(%s, LOWER(TRIM(full_name))) > 0 "
            "OR LOCATE(%s, CAST(id AS CHAR)) > 0)"
        )
        normalized_search = search.lower()
        search_parameters = (normalized_search, normalized_search)

    count_statement = f"SELECT COUNT(*) AS total FROM demo_ehr_records {where_clause}"
    if search_parameters:
        total_row = fetch_one(count_statement, search_parameters)
    else:
        total_row = fetch_one(count_statement)

    records_statement = (
        "SELECT id AS _id, full_name, age, gender, blood_type, medical_condition, "
        "admission_date, doctor, hospital, insurance_provider, billing_amount, room_number, "
        "admission_type, discharge_date, medication, test_results, is_demo, imported_at "
        f"FROM demo_ehr_records {where_clause} "
        "ORDER BY source_row ASC LIMIT %s OFFSET %s"
    )
    records_parameters = search_parameters + (per_page, offset)
    records = fetch_all(records_statement, records_parameters)
    return {"records": records, "total": total_row["total"]}


def fetch_doctor_dashboard_records(page, per_page, search=""):
    offset = (page - 1) * per_page
    normalized_search = search.strip().lower()
    demo_filter = ""
    ehr_filter = ""
    search_parameters = ()
    if normalized_search:
        demo_filter = " AND LOCATE(%s, LOWER(TRIM(full_name))) > 0"
        ehr_filter = " AND LOCATE(%s, LOWER(TRIM(patients.name))) > 0"
        search_parameters = (normalized_search, normalized_search)

    total_row = fetch_one(
        "SELECT "
        "(SELECT COUNT(*) FROM demo_ehr_records "
        f"WHERE is_demo = TRUE{demo_filter}) + "
        "(SELECT COUNT(*) FROM ehr_records "
        "JOIN users AS patients ON patients.id = ehr_records.patient_id "
        f"WHERE TRUE{ehr_filter}) AS total",
        search_parameters,
    )

    records = fetch_all(
        "SELECT 'Dataset' AS source_type, CAST(dataset.id AS CHAR) AS source_id, "
        "dataset.full_name, CAST(dataset.age AS CHAR) AS age, dataset.gender, "
        "dataset.blood_type, dataset.medical_condition, NULL AS diagnosis, NULL AS treatment, "
        "dataset.admission_date AS record_date, dataset.discharge_date, dataset.doctor, "
        "dataset.hospital, dataset.medication, dataset.test_results, "
        "CAST(dataset.billing_amount AS CHAR) AS billing_amount "
        "FROM demo_ehr_records AS dataset "
        f"WHERE dataset.is_demo = TRUE{demo_filter} "
        "UNION ALL "
        "SELECT 'Manually Added EHR' AS source_type, CAST(ehr_records.id AS CHAR) AS source_id, "
        "patients.name AS full_name, ehr_records.age, ehr_records.gender, "
        "NULL AS blood_type, ehr_records.medical_condition, ehr_records.diagnosis, "
        "ehr_records.treatment, ehr_records.record_date, NULL AS discharge_date, "
        "doctors.name AS doctor, NULL AS hospital, NULL AS medication, NULL AS test_results, "
        "NULL AS billing_amount "
        "FROM ehr_records "
        "JOIN users AS patients ON patients.id = ehr_records.patient_id "
        "JOIN users AS doctors ON doctors.id = ehr_records.created_by "
        f"WHERE TRUE{ehr_filter} "
        "ORDER BY record_date DESC, source_type, source_id "
        "LIMIT %s OFFSET %s",
        search_parameters + (per_page, offset),
    )
    return {"records": records, "total": total_row["total"]}


def update_record_fields(record_id, updates):
    columns = ", ".join(f"`{field}` = %s" for field in updates)
    values = tuple(updates.values()) + (record_id,)
    _, rowcount = execute_statement(
        f"UPDATE ehr_records SET {columns} WHERE id = %s",
        values,
    )
    return rowcount > 0


def fetch_doctors():
    return fetch_all(
        "SELECT id AS _id, name, email, role, status, created_at "
        "FROM users WHERE role = %s",
        ("doctor",),
    )


def set_doctor_status(doctor_id, status):
    _, rowcount = execute_statement(
        "UPDATE users SET status = %s WHERE id = %s AND role = %s",
        (status, doctor_id, "doctor"),
    )
    return rowcount > 0


def parse_database_id(value):
    if isinstance(value, bool):
        raise ValueError("ID must be a positive integer.")
    parsed = int(value)
    if parsed <= 0 or str(parsed) != str(value).strip():
        raise ValueError("ID must be a positive integer.")
    return parsed


def mysql_datetime(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def create_access_token(app, user):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user["_id"]), "role": user["role"],
            "iat": now, "exp": now + timedelta(hours=app.config["JWT_EXPIRY_HOURS"]),
        },
        app.config["SECRET_KEY"], algorithm="HS256",
    )


def require_auth(app, roles=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return jsonify({"error": "A bearer token is required."}), 401
            try:
                claims = jwt.decode(header[7:], app.config["SECRET_KEY"], algorithms=["HS256"])
                current_user = {"id": str(claims["sub"]), "role": claims["role"]}
            except (jwt.InvalidTokenError, KeyError, ValueError):
                return jsonify({"error": "Invalid or expired token."}), 401
            if current_user["role"] not in ALLOWED_ROLES or (roles and current_user["role"] not in roles):
                return jsonify({"error": "This role is not permitted to perform this action."}), 403
            return view(current_user, *args, **kwargs)
        return wrapped
    return decorator


def serialize_document(document):
    if isinstance(document, dict):
        return {
            key: (
                str(value) if key in {"_id", "patient_id", "created_by"} and isinstance(value, int)
                else serialize_document(value)
            )
            for key, value in document.items()
        }
    if isinstance(document, list):
        return [serialize_document(value) for value in document]
    if isinstance(document, int):
        return document
    if isinstance(document, Decimal):
        return str(document)
    if isinstance(document, (datetime, date)):
        if isinstance(document, datetime) and document.tzinfo is None:
            document = document.replace(tzinfo=timezone.utc)
        return document.isoformat()
    return document


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "false").lower() == "true")
