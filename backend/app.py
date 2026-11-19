"""Flask API for EHR authentication and MySQL-backed record access."""
import hashlib
import os
import re
import secrets
import smtplib
import ssl
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from functools import wraps
from pathlib import Path
from urllib.parse import quote

import jwt
from dotenv import load_dotenv
from flask import Flask, current_app, jsonify, request, send_file
from flask_cors import CORS
import mysql.connector
from mysql.connector.errors import Error as MySQLError, IntegrityError
from werkzeug.datastructures import FileStorage
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

if __package__:
    from .analytics import DATASET_PATH, analytics_blueprint
    from .blockchain import Blockchain, Block, calculate_record_hash
else:
    from analytics import DATASET_PATH, analytics_blueprint
    from blockchain import Blockchain, Block, calculate_record_hash

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

ALLOWED_ROLES = {"admin", "doctor", "patient"}
ALLOWED_GENDERS = {"female", "male", "non-binary", "prefer-not-to-say"}
ALLOWED_BLOOD_GROUPS = {"A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PATIENT_CODE_PATTERN = re.compile(r"^EHR-\d{10,20}$", re.IGNORECASE)
REPORT_MAX_BYTES = 10 * 1024 * 1024
REPORT_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"}
RECORD_FIELDS = ("medical_condition", "blood_pressure", "age", "gender", "diagnosis", "treatment")


class PatientIdentityConflict(Exception):
    pass


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
        REPORT_UPLOAD_FOLDER=os.getenv(
            "REPORT_UPLOAD_FOLDER", str(Path(app.instance_path) / "report_uploads")
        ),
        ANALYTICS_DATASET_PATH=str(DATASET_PATH),
        MAX_CONTENT_LENGTH=11 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    app.register_blueprint(analytics_blueprint)

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

    @app.errorhandler(413)
    def handle_payload_too_large(error):
        if request.path.startswith("/api/"):
            return jsonify({"error": "The upload exceeds the 10 MB per-request limit."}), 413
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

    @app.get("/api/blockchain/verify")
    @require_auth(app, roles={"admin", "doctor"})
    def verify_blockchain_route(current_user):
        record_id = request.args.get("record_id")
        if record_id is not None:
            try:
                record_id = parse_database_id(record_id)
            except (TypeError, ValueError):
                return jsonify({"error": "record_id must be a valid positive integer."}), 400
        try:
            result = verify_blockchain(record_id)
        except (MySQLError, RuntimeError) as error:
            app.logger.exception("Blockchain verification failed")
            message = "Unable to verify blockchain integrity."
            if (isinstance(error, MySQLError) and getattr(error, "errno", None) == 1146) or (
                isinstance(error, RuntimeError) and "Blockchain schema is not initialized" in str(error)
            ):
                message = "Blockchain ledger is not initialized in the database. Run setup_mysql.py."
            return jsonify({"error": message}), 503
        if record_id is not None and not result["records"]:
            return jsonify({"error": "EHR record not found."}), 404
        return jsonify(result)

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

    @app.post("/api/email-verification/confirm")
    def confirm_patient_email():
        payload = request.get_json(silent=True) or {}
        token = payload.get("token")
        if not isinstance(token, str) or not 32 <= len(token) <= 256:
            return jsonify({"error": "This verification link is invalid or incomplete."}), 400
        try:
            result = consume_patient_link_verification(
                hashlib.sha256(token.encode("utf-8")).hexdigest()
            )
        except IntegrityError as error:
            if error.errno == 1062:
                return jsonify({"error": "An account with this email already exists."}), 409
            app.logger.exception("Patient email verification integrity error")
            return jsonify({"error": "Email verification is temporarily unavailable."}), 503
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient email verification failed")
            return jsonify({"error": "Email verification is temporarily unavailable."}), 503
        messages = {
            "invalid": "This verification link is invalid.",
            "expired": "This verification link has expired. Register again to request a new link.",
            "used": "This verification link has already been used.",
            "linked": "This Patient ID is already linked to an account.",
            "success": "Email verified. You can now sign in.",
        }
        if result != "success":
            return jsonify({"error": messages[result]}), 400
        return jsonify({"message": messages[result]})

    @app.post("/api/register")
    def register():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "A JSON object is required."}), 400
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
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            raw_token = secrets.token_urlsafe(32)
            registration = register_patient_account(
                user,
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                now,
                now + timedelta(minutes=app.config["PASSWORD_RESET_TTL_MINUTES"]),
                bool(app.config["MAIL_SERVER"] and app.config["MAIL_FROM"]),
            )
            if registration["status"] == "identity_conflict":
                return jsonify({
                    "error": "The patient identity could not be matched uniquely. Contact the clinic."
                }), 409
            if registration["status"] == "email_unavailable":
                return jsonify({
                    "error": "Email delivery is not configured; patient account linking is unavailable."
                }), 503
            if registration["status"] == "verification_required":
                verification_url = (
                    f"{app.config['FRONTEND_BASE_URL']}/verify-email.HTML"
                    f"#token={quote(raw_token, safe='')}"
                )
                try:
                    send_patient_verification_email(
                        app.config, email, verification_url
                    )
                except (OSError, smtplib.SMTPException):
                    app.logger.exception("Patient account verification email delivery failed")
                    revoke_patient_link_verification(
                        hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), now
                    )
                    return jsonify({
                        "error": "Unable to send verification email. Please try again later."
                    }), 503
                return jsonify({
                    "message": "Check your email to verify ownership and link this Patient ID."
                }), 202
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
            if current_user["role"] == "patient":
                records = fetch_records(parse_database_id(current_user["id"]))
            else:
                records = fetch_records(include_patient_accounts=True)
        except ValueError:
            return jsonify({"error": "Your session is invalid. Please sign in again."}), 401
        except MySQLError:
            app.logger.exception("Record query failed")
            return jsonify({"error": "Unable to retrieve records."}), 503
        return jsonify({"records": [serialize_document(record) for record in records]})

    @app.get("/api/patient/profile")
    @require_auth(app, roles={"patient"})
    def get_own_patient_profile(current_user):
        try:
            patient_id = parse_database_id(current_user["id"])
            patient = fetch_patient_profile_for_user(patient_id)
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient profile query failed")
            return jsonify({"error": "Unable to retrieve your patient profile."}), 503
        if patient is None:
            return jsonify({"error": "No patient profile is linked to this account."}), 404
        return jsonify({"patient": serialize_document(patient)})

    @app.post("/api/patient/profile/link/request")
    @require_auth(app, roles={"patient"})
    def request_own_patient_profile_link(current_user):
        if not app.config["MAIL_SERVER"] or not app.config["MAIL_FROM"]:
            return jsonify({
                "error": "Email delivery is not configured; patient profile linking is unavailable."
            }), 503
        try:
            user_id = parse_database_id(current_user["id"])
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            raw_token = secrets.token_urlsafe(32)
            result = request_patient_profile_link(
                user_id,
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
                now,
                now + timedelta(minutes=app.config["PASSWORD_RESET_TTL_MINUTES"]),
            )
            if result["status"] == "not_found":
                return jsonify({"error": "No matching clinic patient profile was found."}), 404
            if result["status"] == "identity_conflict":
                return jsonify({
                    "error": "The patient identity could not be matched uniquely. Contact the clinic."
                }), 409
            if result["status"] == "already_linked":
                return jsonify({"message": "This patient profile is already linked to your account."})

            verification_url = (
                f"{app.config['FRONTEND_BASE_URL']}/verify-email.HTML"
                f"#token={quote(raw_token, safe='')}"
            )
            try:
                send_patient_verification_email(
                    app.config, result["email"], verification_url
                )
            except (OSError, smtplib.SMTPException):
                app.logger.exception("Patient profile-link verification email delivery failed")
                revoke_patient_profile_link_verification(
                    hashlib.sha256(raw_token.encode("utf-8")).hexdigest(), now
                )
                return jsonify({
                    "error": "Unable to send verification email. Please try again later."
                }), 503
            return jsonify({
                "message": "Check your email to verify ownership and link your existing Patient ID."
            }), 202
        except ValueError:
            return jsonify({"error": "Your session is invalid. Please sign in again."}), 401
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient profile-link request failed")
            return jsonify({"error": "Patient profile linking is temporarily unavailable."}), 503

    @app.get("/api/patients/search")
    @require_auth(app, roles={"doctor"})
    def search_patients(current_user):
        query = request.args.get("q", "").strip()
        if len(query) < 2:
            return jsonify({"error": "Enter at least two characters to search."}), 400
        try:
            patients = search_patient_profiles(query)
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient search failed")
            return jsonify({"error": "Unable to search patient profiles."}), 503
        return jsonify({"patients": [serialize_document(patient) for patient in patients]})

    @app.post("/api/patients")
    @require_auth(app, roles={"doctor"})
    def create_patient(current_user):
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "A JSON object is required."}), 400
        patient, patient_error = validate_patient_payload(payload)
        visit, visit_error = validate_visit_payload(payload)
        if patient_error or visit_error:
            return jsonify({
                "error": "Correct the patient and first-visit details before saving.",
                "fields": patient_error + visit_error,
            }), 400
        try:
            doctor_id = parse_database_id(current_user["id"])
            result = create_patient_with_visit(patient, visit, doctor_id)
        except PatientIdentityConflict:
            return jsonify({
                "error": "A patient with this email already exists. Search for that Patient ID and add a visit."
            }), 409
        except ValueError:
            return jsonify({"error": "Your session is invalid. Please sign in again."}), 401
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient and first visit creation failed")
            return jsonify({"error": "Unable to create the patient record."}), 503
        return jsonify({
            "message": "Patient and first visit created.",
            "patient_code": result["patient_code"],
            "visit_id": str(result["visit_id"]),
        }), 201

    @app.get("/api/patients/<patient_code>")
    @require_auth(app, roles={"doctor"})
    def get_patient_history(current_user, patient_code):
        if not PATIENT_CODE_PATTERN.fullmatch(patient_code):
            return jsonify({"error": "Enter a valid Patient ID such as EHR-0000000001."}), 400
        try:
            patient = fetch_patient_history(patient_code)
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient history query failed")
            return jsonify({"error": "Unable to retrieve patient history."}), 503
        if patient is None:
            return jsonify({"error": "Patient not found."}), 404
        return jsonify({"patient": serialize_document(patient)})

    @app.put("/api/patients/<patient_code>")
    @require_auth(app, roles={"doctor"})
    def update_patient_profile(current_user, patient_code):
        if not PATIENT_CODE_PATTERN.fullmatch(patient_code):
            return jsonify({"error": "Enter a valid Patient ID such as EHR-0000000001."}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "A JSON object is required."}), 400
        editable_fields = {
            "full_name", "age", "gender", "blood_group", "mobile_number", "email",
        }
        if set(payload) != editable_fields:
            return jsonify({
                "error": "Only patient name, age, gender, blood group, mobile number, and email may be updated.",
                "fields": sorted(editable_fields - set(payload)),
            }), 400
        patient, errors = validate_patient_payload(payload)
        if errors:
            return jsonify({
                "error": "Correct the patient details before saving.",
                "fields": errors,
            }), 400
        try:
            updated = update_patient_profile_in_database(patient_code, patient)
        except IntegrityError as error:
            if error.errno == 1062:
                return jsonify({"error": "Another patient profile already uses this email address."}), 409
            app.logger.exception("Patient profile update integrity error")
            return jsonify({"error": "Unable to update patient details."}), 503
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient profile update failed")
            return jsonify({"error": "Unable to update patient details."}), 503
        if not updated:
            return jsonify({"error": "Patient not found."}), 404
        return jsonify({
            "message": "Patient details updated.",
            "patient": {
                "patient_code": patient_code,
                **patient,
            },
        })

    @app.post("/api/patients/<patient_code>/visits")
    @require_auth(app, roles={"doctor"})
    def add_patient_visit_route(current_user, patient_code):
        if not PATIENT_CODE_PATTERN.fullmatch(patient_code):
            return jsonify({"error": "Enter a valid Patient ID such as EHR-0000000001."}), 400
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "A JSON object is required."}), 400
        visit, visit_error = validate_visit_payload(payload)
        if visit_error:
            return jsonify({
                "error": "Correct the visit and prescription details before saving.",
                "fields": visit_error,
            }), 400
        try:
            doctor_id = parse_database_id(current_user["id"])
            visit_id = add_patient_visit(patient_code, visit, doctor_id)
        except ValueError:
            return jsonify({"error": "Your session is invalid. Please sign in again."}), 401
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient visit creation failed")
            return jsonify({"error": "Unable to create the patient visit."}), 503
        if visit_id is None:
            return jsonify({"error": "Patient not found."}), 404
        return jsonify({"message": "Visit created.", "visit_id": str(visit_id)}), 201

    @app.post("/api/patients/<patient_code>/visits/<int:visit_id>/reports")
    @require_auth(app, roles={"doctor"})
    def upload_visit_reports(current_user, patient_code, visit_id):
        if not PATIENT_CODE_PATTERN.fullmatch(patient_code):
            return jsonify({"error": "Enter a valid Patient ID."}), 400
        files = request.files.getlist("reports")
        if not files:
            return jsonify({"error": "Choose at least one PDF or image report."}), 400
        try:
            doctor_id = parse_database_id(current_user["id"])
            reports = save_visit_reports(patient_code, visit_id, files, doctor_id)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except FileNotFoundError:
            return jsonify({"error": "The patient visit was not found."}), 404
        except (OSError, MySQLError, RuntimeError):
            app.logger.exception("Medical report upload failed")
            return jsonify({"error": "Unable to save medical reports."}), 503
        return jsonify({"reports": [serialize_document(report) for report in reports]}), 201

    @app.get("/api/reports/<int:report_id>/<action>")
    @require_auth(app, roles={"doctor", "patient"})
    def get_medical_report(current_user, report_id, action):
        if action not in {"view", "download"}:
            return jsonify({"error": "Report action must be view or download."}), 400
        try:
            report = fetch_authorized_report(report_id, current_user)
        except (MySQLError, RuntimeError, ValueError):
            app.logger.exception("Medical report authorization or lookup failed")
            return jsonify({"error": "Unable to retrieve the medical report."}), 503
        if report is None:
            return jsonify({"error": "Medical report not found."}), 404
        path = Path(app.config["REPORT_UPLOAD_FOLDER"]) / report["storage_key"]
        if not path.is_file():
            app.logger.error("Medical report file is missing for report id %s", report_id)
            return jsonify({"error": "The medical report file is unavailable."}), 404
        return send_file(
            path,
            mimetype=report["mime_type"],
            as_attachment=action == "download",
            download_name=report["original_name"],
            conditional=True,
        )

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
            "real_ehr_only": True,
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
        if not isinstance(payload, dict):
            return jsonify({"error": "A JSON object is required."}), 400
        required = ("medical_condition", "diagnosis", "treatment")
        missing = [
            field for field in required
            if not isinstance(payload.get(field), str) or not payload[field].strip()
        ]
        if payload.get("patient_id") is None or (
            isinstance(payload.get("patient_id"), str) and not payload["patient_id"].strip()
        ):
            missing.insert(0, "patient_id")
        if missing:
            return jsonify({
                "error": "Enter a patient ID, medical condition, diagnosis, and treatment.",
                "fields": missing,
            }), 400
        try:
            patient_id = parse_database_id(payload["patient_id"])
        except (TypeError, ValueError):
            return jsonify({"error": "patient_id must be a valid patient ID."}), 400
        optional_fields = {"blood_pressure": 64, "age": 16, "gender": 32}
        optional_values = {}
        for field, max_length in optional_fields.items():
            value = payload.get(field)
            if value is not None and not isinstance(value, str):
                return jsonify({"error": f"{field} must be text."}), 400
            value = value.strip() if isinstance(value, str) else None
            if value and len(value) > max_length:
                return jsonify({"error": f"{field} must be {max_length} characters or fewer."}), 400
            optional_values[field] = value or None
        if optional_values["gender"] and optional_values["gender"].lower() not in ALLOWED_GENDERS:
            return jsonify({"error": "Select a valid gender."}), 400
        try:
            patient = fetch_one(
                "SELECT id FROM users WHERE id = %s AND role = %s",
                (patient_id, "patient"),
            )
        except (MySQLError, RuntimeError):
            app.logger.exception("Patient lookup failed")
            return jsonify({"error": "Unable to create the record."}), 503
        if patient is None:
            return jsonify({"error": "Patient account not found."}), 404

        record_date = payload.get("record_date")
        if record_date:
            if not isinstance(record_date, str):
                return jsonify({"error": "record_date must use YYYY-MM-DD format."}), 400
            try:
                record_date = datetime.strptime(record_date, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "record_date must use YYYY-MM-DD format."}), 400
        else:
            record_date = date.today()
        record = {
            "patient_id": patient_id,
            "medical_condition": str(payload["medical_condition"]).strip(),
            "blood_pressure": optional_values["blood_pressure"],
            "age": optional_values["age"],
            "gender": optional_values["gender"].lower() if optional_values["gender"] else None,
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
        except (MySQLError, RuntimeError):
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


def send_patient_verification_email(config, recipient, verification_url):
    message = EmailMessage()
    message["Subject"] = "Verify your EHR Secure patient account"
    message["From"] = config["MAIL_FROM"]
    message["To"] = recipient
    message.set_content(
        "A patient account was requested to link to an existing EHR Secure Patient ID.\n\n"
        "Verify ownership of this email address and link the patient account using this "
        "one-time link:\n"
        f"{verification_url}\n\n"
        "If you did not request this, you can ignore this message."
    )
    context = ssl.create_default_context()
    with smtplib.SMTP(config["MAIL_SERVER"], config["MAIL_PORT"], timeout=10) as server:
        server.starttls(context=context)
        if config["MAIL_USERNAME"]:
            server.login(config["MAIL_USERNAME"], config["MAIL_PASSWORD"])
        server.send_message(message)


def register_patient_account(user, token_hash, created_at, expires_at, email_delivery):
    email = user["email"]
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT GET_LOCK(%s, 10) AS lock_acquired",
            (patient_email_lock_name(email),),
        )
        lock_result = cursor.fetchone()
        if not lock_result or lock_result["lock_acquired"] != 1:
            raise RuntimeError("Unable to lock patient email registration.")
        cursor.execute(
            "SELECT id, patient_code, full_name, mobile_number, linked_user_id "
            "FROM patient_profiles WHERE LOWER(TRIM(email)) = %s FOR UPDATE",
            (email,),
        )
        profiles = cursor.fetchall()
        if profiles:
            matches = [
                profile for profile in profiles
                if normalize_identity_text(profile["full_name"])
                == normalize_identity_text(user["name"])
                and normalize_phone(profile["mobile_number"])
                == normalize_phone(user["phone"])
            ]
            if len(matches) != 1 or matches[0]["linked_user_id"] is not None:
                return {"status": "identity_conflict"}
            if not email_delivery:
                return {"status": "email_unavailable"}
            cursor.execute(
                "INSERT INTO patient_email_verifications "
                "(patient_profile_id, name, email, password_hash, date_of_birth, gender, "
                "phone, token_hash, created_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    matches[0]["id"], user["name"], user["email"], user["password_hash"],
                    mysql_datetime(user["date_of_birth"]), user["gender"], user["phone"],
                    token_hash, created_at, expires_at,
                ),
            )
            return {"status": "verification_required"}

        user_id = insert_user_with_cursor(cursor, user)
        insert_patient_profile_for_user(cursor, user, user_id)
        return {"status": "created"}


def request_patient_profile_link(user_id, token_hash, created_at, expires_at):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT id, name, email, phone FROM users "
            "WHERE id = %s AND `role` = 'patient' AND status = 'active' FOR UPDATE",
            (user_id,),
        )
        user = cursor.fetchone()
        if user is None:
            return {"status": "identity_conflict"}
        cursor.execute(
            "SELECT id FROM patient_profiles WHERE linked_user_id = %s FOR UPDATE",
            (user_id,),
        )
        if cursor.fetchone() is not None:
            return {"status": "already_linked"}
        cursor.execute(
            "SELECT GET_LOCK(%s, 10) AS lock_acquired",
            (patient_email_lock_name(user["email"]),),
        )
        lock_result = cursor.fetchone()
        if not lock_result or lock_result["lock_acquired"] != 1:
            raise RuntimeError("Unable to lock patient email registration.")
        cursor.execute(
            "SELECT id, full_name, mobile_number, linked_user_id FROM patient_profiles "
            "WHERE LOWER(TRIM(email)) = %s FOR UPDATE",
            (user["email"].strip().lower(),),
        )
        profiles = cursor.fetchall()
        matches = [
            profile for profile in profiles
            if normalize_identity_text(profile["full_name"])
            == normalize_identity_text(user["name"])
            and normalize_phone(profile["mobile_number"]) == normalize_phone(user["phone"])
        ]
        if len(profiles) != 1 or len(matches) != 1:
            return {"status": "identity_conflict" if profiles else "not_found"}
        profile = matches[0]
        if profile["linked_user_id"] is not None:
            return {
                "status": "already_linked"
                if profile["linked_user_id"] == user_id else "identity_conflict"
            }
        cursor.execute(
            "UPDATE patient_profile_link_verifications SET used_at = %s "
            "WHERE user_id = %s AND used_at IS NULL",
            (created_at, user_id),
        )
        cursor.execute(
            "INSERT INTO patient_profile_link_verifications "
            "(patient_profile_id, user_id, token_hash, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            (profile["id"], user_id, token_hash, created_at, expires_at),
        )
        return {"status": "verification_required", "email": user["email"]}


def consume_patient_link_verification(token_hash):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT id, patient_profile_id, name, email, password_hash, date_of_birth, "
            "gender, phone, expires_at, used_at "
            "FROM patient_email_verifications WHERE token_hash = %s FOR UPDATE",
            (token_hash,),
        )
        verification = cursor.fetchone()
        if verification is None:
            cursor.execute(
                "SELECT id, patient_profile_id, user_id, expires_at, used_at "
                "FROM patient_profile_link_verifications WHERE token_hash = %s FOR UPDATE",
                (token_hash,),
            )
            link_verification = cursor.fetchone()
            if link_verification is None:
                return "invalid"
            if link_verification["used_at"] is not None:
                return "used"
            expires_at = link_verification["expires_at"]
            if expires_at.tzinfo is not None:
                expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
            if expires_at <= now:
                return "expired"
            cursor.execute(
                "SELECT id, name, email, phone FROM users "
                "WHERE id = %s AND `role` = 'patient' AND status = 'active' FOR UPDATE",
                (link_verification["user_id"],),
            )
            user = cursor.fetchone()
            cursor.execute(
                "SELECT id, full_name, email, mobile_number, linked_user_id "
                "FROM patient_profiles WHERE id = %s FOR UPDATE",
                (link_verification["patient_profile_id"],),
            )
            profile = cursor.fetchone()
            if user is None or profile is None or profile["linked_user_id"] is not None:
                return "linked"
            if (
                user["email"].strip().lower() != (profile["email"] or "").strip().lower()
                or normalize_identity_text(user["name"])
                != normalize_identity_text(profile["full_name"])
                or normalize_phone(user["phone"]) != normalize_phone(profile["mobile_number"])
            ):
                return "invalid"
            cursor.execute(
                "SELECT id FROM patient_profiles WHERE linked_user_id = %s FOR UPDATE",
                (user["id"],),
            )
            if cursor.fetchone() is not None:
                return "linked"
            cursor.execute(
                "UPDATE patient_profiles SET linked_user_id = %s "
                "WHERE id = %s AND linked_user_id IS NULL",
                (user["id"], profile["id"]),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("The patient profile was linked concurrently.")
            cursor.execute(
                "UPDATE patient_profile_link_verifications SET used_at = %s WHERE id = %s",
                (now, link_verification["id"]),
            )
            return "success"
        if verification["used_at"] is not None:
            return "used"
        expires_at = verification["expires_at"]
        if expires_at.tzinfo is not None:
            expires_at = expires_at.astimezone(timezone.utc).replace(tzinfo=None)
        if expires_at <= now:
            return "expired"
        cursor.execute(
            "SELECT linked_user_id FROM patient_profiles WHERE id = %s FOR UPDATE",
            (verification["patient_profile_id"],),
        )
        profile = cursor.fetchone()
        if profile is None or profile["linked_user_id"] is not None:
            return "linked"
        user = {
            "name": verification["name"],
            "email": verification["email"],
            "password_hash": verification["password_hash"],
            "role": "patient",
            "status": "active",
            "date_of_birth": verification["date_of_birth"],
            "gender": verification["gender"],
            "phone": verification["phone"],
            "created_at": now,
        }
        user_id = insert_user_with_cursor(cursor, user)
        cursor.execute(
            "UPDATE patient_profiles SET linked_user_id = %s "
            "WHERE id = %s AND linked_user_id IS NULL",
            (user_id, verification["patient_profile_id"]),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("The patient profile was linked concurrently.")
        cursor.execute(
            "UPDATE patient_email_verifications SET used_at = %s WHERE id = %s",
            (now, verification["id"]),
        )
    return "success"


def revoke_patient_link_verification(token_hash, revoked_at):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "UPDATE patient_email_verifications SET used_at = %s "
            "WHERE token_hash = %s AND used_at IS NULL",
            (revoked_at, token_hash),
        )


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


def validate_patient_payload(payload):
    errors = []
    full_name = payload.get("full_name")
    if not isinstance(full_name, str) or not full_name.strip() or len(full_name.strip()) > 120:
        errors.append("full_name")

    age = payload.get("age")
    if (
        isinstance(age, bool)
        or not isinstance(age, (str, int))
        or not str(age).isdigit()
        or int(age) > 130
    ):
        errors.append("age")
        age = None
    else:
        age = int(age)

    gender = payload.get("gender")
    gender = gender.strip().lower() if isinstance(gender, str) else ""
    if gender not in ALLOWED_GENDERS:
        errors.append("gender")

    blood_group = payload.get("blood_group")
    blood_group = blood_group.strip().upper() if isinstance(blood_group, str) else ""
    if blood_group not in ALLOWED_BLOOD_GROUPS:
        errors.append("blood_group")

    mobile_number = payload.get("mobile_number")
    mobile_number = mobile_number.strip() if isinstance(mobile_number, str) else ""
    if not re.fullmatch(r"\+?[0-9][0-9\s().-]{6,28}", mobile_number):
        errors.append("mobile_number")

    email = payload.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if len(email) > 255 or not EMAIL_PATTERN.fullmatch(email):
        errors.append("email")

    return {
        "full_name": full_name.strip() if isinstance(full_name, str) else "",
        "age": age,
        "gender": gender,
        "blood_group": blood_group,
        "mobile_number": mobile_number,
        "email": email,
    }, errors


def validate_visit_payload(payload):
    errors = []
    treatment_date = parse_form_date(payload.get("treatment_date"), "treatment_date", errors)
    follow_up_date = parse_form_date(
        payload.get("follow_up_date"), "follow_up_date", errors, required=False
    )

    hospital_name = payload.get("hospital_name")
    if not isinstance(hospital_name, str) or not hospital_name.strip() or len(hospital_name.strip()) > 255:
        errors.append("hospital_name")

    diagnosis = payload.get("diagnosis")
    if not isinstance(diagnosis, str) or not diagnosis.strip():
        errors.append("diagnosis")

    treatment = payload.get("treatment")
    if (
        not isinstance(treatment, str)
        or not treatment.strip()
        or len(treatment.strip()) > 10000
    ):
        errors.append("treatment")

    additional_instructions = payload.get("additional_instructions", "")
    if not isinstance(additional_instructions, str) or len(additional_instructions) > 10000:
        errors.append("additional_instructions")

    medicines = payload.get("medicines")
    if not isinstance(medicines, list) or not medicines or len(medicines) > 100:
        errors.append("medicines")
        medicines = []
    clean_medicines = []
    for index, medicine in enumerate(medicines):
        if not isinstance(medicine, dict):
            errors.append(f"medicines[{index}]")
            continue
        clean = {}
        for field, max_length in (
            ("medicine_name", 255), ("dose", 120), ("frequency", 120), ("duration", 120)
        ):
            value = medicine.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
                errors.append(f"medicines[{index}].{field}")
                clean[field] = ""
            else:
                clean[field] = value.strip()
        clean_medicines.append(clean)

    return {
        "treatment_date": treatment_date,
        "hospital_name": hospital_name.strip() if isinstance(hospital_name, str) else "",
        "diagnosis": diagnosis.strip() if isinstance(diagnosis, str) else "",
        "treatment": treatment.strip() if isinstance(treatment, str) else "",
        "additional_instructions": (
            additional_instructions.strip()
            if isinstance(additional_instructions, str) else ""
        ),
        "follow_up_date": follow_up_date,
        "medicines": clean_medicines,
    }, errors


def parse_form_date(value, field, errors, required=True):
    if not value:
        if required:
            errors.append(field)
        return None
    if not isinstance(value, str):
        errors.append(field)
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        errors.append(field)
        return None


def format_patient_code(patient_id):
    return f"EHR-{patient_id:010d}"


def normalize_identity_text(value):
    return " ".join(str(value or "").split()).casefold()


def normalize_phone(value):
    return re.sub(r"\D", "", str(value or ""))


def patient_email_lock_name(email):
    return f"ehr-p-{hashlib.sha256(email.encode('utf-8')).hexdigest()[:58]}"


def patient_age_from_birth_date(value):
    if value is None:
        return None
    birth_date = value.date() if isinstance(value, datetime) else value
    today = date.today()
    return today.year - birth_date.year - (
        (today.month, today.day) < (birth_date.month, birth_date.day)
    )


def insert_visit(cursor, patient, visit, doctor_id, doctor_name):
    treatment_date = datetime.combine(visit["treatment_date"], datetime.min.time())
    cursor.execute(
        "INSERT INTO ehr_records "
        "(patient_id, patient_profile_id, medical_condition, blood_pressure, age, gender, "
        "diagnosis, treatment, record_date, created_by, doctor_name, hospital_name, "
        "additional_instructions, follow_up_date, updated_at) "
        "VALUES (%s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            patient.get("linked_user_id"), patient["id"], visit["diagnosis"],
            str(patient.get("age")) if patient.get("age") is not None else None,
            patient.get("gender"), visit["diagnosis"], visit["treatment"], treatment_date,
            doctor_id, doctor_name, visit["hospital_name"],
            visit["additional_instructions"] or None, visit["follow_up_date"],
            datetime.now(timezone.utc).replace(tzinfo=None),
        ),
    )
    record_id = cursor.lastrowid
    cursor.executemany(
        "INSERT INTO prescription_medicines "
        "(record_id, line_number, medicine_name, dose, frequency, duration) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        [
            (
                record_id, line_number, medicine["medicine_name"], medicine["dose"],
                medicine["frequency"], medicine["duration"],
            )
            for line_number, medicine in enumerate(visit["medicines"], start=1)
        ],
    )
    append_record_audit(cursor, record_id, "create", "doctor")
    return record_id


def create_patient_with_visit(patient_data, visit, doctor_id):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT GET_LOCK(%s, 10) AS lock_acquired",
            (patient_email_lock_name(patient_data["email"]),),
        )
        lock_result = cursor.fetchone()
        if not lock_result or lock_result["lock_acquired"] != 1:
            raise RuntimeError("Unable to lock patient email registration.")
        cursor.execute(
            "SELECT id FROM patient_profiles WHERE LOWER(TRIM(email)) = %s FOR UPDATE",
            (patient_data["email"],),
        )
        if cursor.fetchone() is not None:
            raise PatientIdentityConflict()
        cursor.execute(
            "SELECT id FROM users WHERE LOWER(TRIM(email)) = %s",
            (patient_data["email"],),
        )
        if cursor.fetchone() is not None:
            raise PatientIdentityConflict()
        cursor.execute(
            "SELECT name FROM users WHERE id = %s AND `role` = 'doctor' AND status = 'active'",
            (doctor_id,),
        )
        doctor = cursor.fetchone()
        if doctor is None:
            raise RuntimeError("The authenticated doctor account is no longer active.")
        cursor.execute(
            "INSERT INTO patient_profiles "
            "(patient_code, full_name, age, gender, blood_group, mobile_number, email, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                f"PENDING-{uuid.uuid4().hex[:16]}", patient_data["full_name"],
                patient_data["age"], patient_data["gender"], patient_data["blood_group"],
                patient_data["mobile_number"], patient_data["email"],
                datetime.now(timezone.utc).replace(tzinfo=None),
            ),
        )
        patient_id = cursor.lastrowid
        patient_code = format_patient_code(patient_id)
        cursor.execute(
            "UPDATE patient_profiles SET patient_code = %s WHERE id = %s",
            (patient_code, patient_id),
        )
        patient = {**patient_data, "id": patient_id, "linked_user_id": None}
        visit_id = insert_visit(cursor, patient, visit, doctor_id, doctor["name"])
    return {"patient_code": patient_code, "visit_id": visit_id}


def add_patient_visit(patient_code, visit, doctor_id):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT id, linked_user_id, full_name, age, gender FROM patient_profiles "
            "WHERE UPPER(patient_code) = UPPER(%s) FOR UPDATE",
            (patient_code,),
        )
        patient = cursor.fetchone()
        if patient is None:
            return None
        cursor.execute(
            "SELECT name FROM users WHERE id = %s AND `role` = 'doctor' AND status = 'active'",
            (doctor_id,),
        )
        doctor = cursor.fetchone()
        if doctor is None:
            raise RuntimeError("The authenticated doctor account is no longer active.")
        return insert_visit(cursor, patient, visit, doctor_id, doctor["name"])


def fetch_patient_history(patient_code):
    patient = fetch_one(
        "SELECT id AS _id, patient_code, full_name, age, gender, blood_group, mobile_number, email "
        "FROM patient_profiles WHERE UPPER(patient_code) = UPPER(%s)",
        (patient_code,),
    )
    if patient is None:
        return None
    visits = fetch_all(
        "SELECT id AS _id, diagnosis, treatment, record_date AS treatment_date, doctor_name, "
        "hospital_name, additional_instructions, follow_up_date "
        "FROM ehr_records WHERE patient_profile_id = %s "
        "ORDER BY record_date DESC, id DESC",
        (patient["_id"],),
    )
    if visits:
        placeholders = ", ".join(["%s"] * len(visits))
        medicines = fetch_all(
            "SELECT record_id, line_number, medicine_name, dose, frequency, duration "
            f"FROM prescription_medicines WHERE record_id IN ({placeholders}) "
            "ORDER BY record_id, line_number",
            tuple(visit["_id"] for visit in visits),
        )
        medicines_by_visit = {}
        for medicine in medicines:
            medicines_by_visit.setdefault(medicine["record_id"], []).append({
                key: medicine[key]
                for key in ("medicine_name", "dose", "frequency", "duration")
            })
        for visit in visits:
            visit["medicines"] = medicines_by_visit.get(visit["_id"], [])
        add_reports(visits)
    patient["visits"] = visits
    return patient


def update_patient_profile_in_database(patient_code, patient):
    _, rowcount = execute_statement(
        "UPDATE patient_profiles SET full_name = %s, age = %s, gender = %s, "
        "blood_group = %s, mobile_number = %s, email = %s "
        "WHERE UPPER(patient_code) = UPPER(%s)",
        (
            patient["full_name"],
            patient["age"],
            patient["gender"],
            patient["blood_group"],
            patient["mobile_number"],
            patient["email"],
            patient_code,
        ),
    )
    if rowcount:
        return True
    existing = fetch_one(
        "SELECT 1 FROM patient_profiles WHERE UPPER(patient_code) = UPPER(%s)",
        (patient_code,),
    )
    return existing is not None


def fetch_patient_profile_for_user(user_id):
    patient = fetch_one(
        "SELECT patient_code FROM patient_profiles WHERE linked_user_id = %s",
        (user_id,),
    )
    return fetch_patient_history(patient["patient_code"]) if patient else None


def search_patient_profiles(query):
    normalized_query = query.strip().casefold()
    return fetch_all(
        "SELECT id AS _id, patient_code, full_name, age, gender, blood_group, "
        "mobile_number, email FROM patient_profiles "
        "WHERE LOCATE(%s, LOWER(TRIM(full_name))) > 0 "
        "OR LOCATE(%s, LOWER(patient_code)) > 0 "
        "ORDER BY full_name, patient_code LIMIT 50",
        (normalized_query, normalized_query),
    )


def insert_user(user):
    with database_cursor(commit=True) as cursor:
        user_id = insert_user_with_cursor(cursor, user)
        if user["role"] == "patient":
            insert_patient_profile_for_user(cursor, user, user_id)
    return user_id


def insert_user_with_cursor(cursor, user):
    cursor.execute(
        "INSERT INTO users "
        "(name, email, password_hash, role, status, date_of_birth, gender, phone, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            user["name"], user["email"], user["password_hash"], user["role"], user["status"],
            mysql_datetime(user.get("date_of_birth")), user.get("gender"), user.get("phone"),
            mysql_datetime(user["created_at"]),
        ),
    )
    return cursor.lastrowid


def insert_patient_profile_for_user(cursor, user, user_id):
    cursor.execute(
        "INSERT INTO patient_profiles "
        "(patient_code, linked_user_id, full_name, age, gender, mobile_number, email, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            f"PENDING-U-{uuid.uuid4().hex[:12]}", user_id, user["name"],
            patient_age_from_birth_date(user.get("date_of_birth")), user.get("gender"),
            user.get("phone"), user["email"], mysql_datetime(user["created_at"]),
        ),
    )
    profile_id = cursor.lastrowid
    cursor.execute(
        "UPDATE patient_profiles SET patient_code = %s WHERE id = %s",
        (format_patient_code(profile_id), profile_id),
    )
    return profile_id


def insert_record(record):
    with database_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT id FROM patient_profiles WHERE linked_user_id = %s",
            (record["patient_id"],),
        )
        patient_profile = cursor.fetchone()
        if patient_profile is None:
            raise RuntimeError("The patient account has no patient profile.")
        cursor.execute(
            "SELECT name FROM users WHERE id = %s AND `role` = 'doctor'",
            (record["created_by"],),
        )
        doctor = cursor.fetchone()
        if doctor is None:
            raise RuntimeError("The authenticated doctor account was not found.")
        cursor.execute(
            "INSERT INTO ehr_records "
            "(patient_id, patient_profile_id, medical_condition, blood_pressure, age, gender, "
            "diagnosis, treatment, record_date, created_by, doctor_name, hospital_name, "
            "additional_instructions, follow_up_date, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, %s)",
            (
                record["patient_id"], patient_profile["id"], record["medical_condition"],
                record["blood_pressure"], record["age"], record["gender"], record["diagnosis"],
                record["treatment"], mysql_datetime(record["record_date"]), record["created_by"],
                doctor["name"], mysql_datetime(record["updated_at"]),
            ),
        )
        record_id = cursor.lastrowid
        append_record_audit(cursor, record_id, "create", "doctor")
        return record_id


def fetch_records(patient_id=None, include_patient_accounts=False):
    if include_patient_accounts and patient_id is not None:
        raise ValueError("Patient account searches cannot be scoped to a patient ID.")
    if include_patient_accounts:
        statement = (
            "SELECT ehr_records.id AS _id, users.id AS patient_id, "
            "patient_profiles.patient_code, patient_profiles.full_name AS patient_name, "
            "patient_profiles.age AS patient_age, patient_profiles.gender AS patient_gender, "
            "patient_profiles.blood_group, patient_profiles.mobile_number, patient_profiles.email, "
            "ehr_records.patient_profile_id, "
            "ehr_records.medical_condition, ehr_records.blood_pressure, ehr_records.age, "
            "ehr_records.gender, ehr_records.diagnosis, ehr_records.treatment, "
            "ehr_records.record_date, ehr_records.doctor_name, ehr_records.hospital_name, "
            "ehr_records.additional_instructions, ehr_records.follow_up_date, "
            "ehr_records.created_by, ehr_records.updated_at "
            "FROM users LEFT JOIN patient_profiles "
            "ON patient_profiles.linked_user_id = users.id "
            "LEFT JOIN ehr_records ON ehr_records.patient_profile_id = patient_profiles.id "
            "WHERE users.`role` = 'patient' AND users.status = 'active' "
            "ORDER BY patient_profiles.full_name, ehr_records.record_date DESC"
        )
        records = fetch_all(statement)
        return add_prescriptions(records)

    statement = (
        "SELECT ehr_records.id AS _id, patient_profiles.linked_user_id AS patient_id, "
        "patient_profiles.patient_code, patient_profiles.full_name AS patient_name, "
        "patient_profiles.age AS patient_age, patient_profiles.gender AS patient_gender, "
        "patient_profiles.blood_group, patient_profiles.mobile_number, patient_profiles.email, "
        "ehr_records.patient_profile_id, "
        "ehr_records.medical_condition, ehr_records.blood_pressure, ehr_records.age, "
        "ehr_records.gender, ehr_records.diagnosis, ehr_records.treatment, "
        "ehr_records.record_date, ehr_records.doctor_name, ehr_records.hospital_name, "
        "ehr_records.additional_instructions, ehr_records.follow_up_date, "
        "ehr_records.created_by, ehr_records.updated_at "
        "FROM ehr_records JOIN patient_profiles "
        "ON patient_profiles.id = ehr_records.patient_profile_id "
        "LEFT JOIN users ON users.id = patient_profiles.linked_user_id"
    )
    parameters = ()
    if patient_id is not None:
        statement += " WHERE patient_profiles.linked_user_id = %s"
        parameters = (patient_id,)
    statement += " ORDER BY ehr_records.record_date DESC"
    return add_prescriptions(fetch_all(statement, parameters))


def add_prescriptions(records):
    records_with_ids = [record for record in records if record.get("_id") is not None]
    if not records_with_ids:
        return records
    placeholders = ", ".join(["%s"] * len(records_with_ids))
    medicines = fetch_all(
        "SELECT record_id, line_number, medicine_name, dose, frequency, duration "
        f"FROM prescription_medicines WHERE record_id IN ({placeholders}) "
        "ORDER BY record_id, line_number",
        tuple(record["_id"] for record in records_with_ids),
    )
    medicines_by_record = {}
    for medicine in medicines:
        medicines_by_record.setdefault(medicine["record_id"], []).append({
            key: medicine[key] for key in ("medicine_name", "dose", "frequency", "duration")
        })
    for record in records:
        record["medicines"] = medicines_by_record.get(record.get("_id"), [])
    add_reports(records)
    return records


def add_reports(records):
    records_with_ids = [record for record in records if record.get("_id") is not None]
    if not records_with_ids:
        return records
    placeholders = ", ".join(["%s"] * len(records_with_ids))
    reports = fetch_all(
        "SELECT id AS _id, record_id, original_name, mime_type, size_bytes, uploaded_at "
        "FROM medical_reports WHERE record_id IN (" + placeholders + ") "
        "ORDER BY uploaded_at, id",
        tuple(record["_id"] for record in records_with_ids),
    )
    reports_by_record = {}
    for report in reports:
        reports_by_record.setdefault(report["record_id"], []).append({
            key: report[key]
            for key in ("_id", "original_name", "mime_type", "size_bytes", "uploaded_at")
        })
    for record in records:
        record["reports"] = reports_by_record.get(record.get("_id"), [])
    return records


def report_mime_type(filename, content):
    extension = Path(filename).suffix.lower()
    signatures = {
        ".pdf": (b"%PDF-", "application/pdf"),
        ".jpg": (b"\xff\xd8\xff", "image/jpeg"),
        ".jpeg": (b"\xff\xd8\xff", "image/jpeg"),
        ".png": (b"\x89PNG\r\n\x1a\n", "image/png"),
        ".gif": (b"GIF87a", "image/gif"),
    }
    if extension == ".gif" and content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if extension == ".webp" and len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    signature = signatures.get(extension)
    if signature and content.startswith(signature[0]):
        return signature[1]
    return None


def save_visit_reports(patient_code, visit_id, files, doctor_id):
    if len(files) > 5:
        raise ValueError("Upload no more than five reports at a time.")
    prepared = []
    total_size = 0
    for uploaded_file in files:
        filename = secure_filename(uploaded_file.filename or "")
        if not filename or Path(filename).suffix.lower() not in REPORT_EXTENSIONS:
            raise ValueError("Reports must be PDF, JPEG, PNG, GIF, or WebP files.")
        content = uploaded_file.stream.read(REPORT_MAX_BYTES + 1)
        if not content or len(content) > REPORT_MAX_BYTES:
            raise ValueError("Each report must be non-empty and no larger than 10 MB.")
        total_size += len(content)
        mime_type = report_mime_type(filename, content)
        if mime_type is None:
            raise ValueError("A report's file contents do not match its supported file type.")
        prepared.append({
            "original_name": filename,
            "content": content,
            "mime_type": mime_type,
            "storage_key": f"{uuid.uuid4().hex}{Path(filename).suffix.lower()}",
        })
    if total_size > REPORT_MAX_BYTES:
        raise ValueError("The combined report upload must not exceed 10 MB.")

    saved_paths = []
    upload_folder = Path(current_app.config["REPORT_UPLOAD_FOLDER"])
    try:
        with database_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT patient_profiles.id FROM patient_profiles "
                "JOIN ehr_records ON ehr_records.patient_profile_id = patient_profiles.id "
                "WHERE UPPER(patient_profiles.patient_code) = UPPER(%s) "
                "AND ehr_records.id = %s FOR UPDATE",
                (patient_code, visit_id),
            )
            patient = cursor.fetchone()
            if patient is None:
                raise FileNotFoundError("Patient visit not found.")
            cursor.execute(
                "SELECT id FROM users WHERE id = %s AND `role` = 'doctor' AND status = 'active'",
                (doctor_id,),
            )
            if cursor.fetchone() is None:
                raise RuntimeError("The authenticated doctor account is no longer active.")

            upload_folder.mkdir(parents=True, exist_ok=True)
            for report in prepared:
                path = upload_folder / report["storage_key"]
                with path.open("xb") as report_file:
                    report_file.write(report["content"])
                saved_paths.append(path)
                cursor.execute(
                    "INSERT INTO medical_reports "
                    "(patient_profile_id, record_id, original_name, storage_key, mime_type, "
                    "size_bytes, sha256, uploaded_by, uploaded_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        patient["id"], visit_id, report["original_name"], report["storage_key"],
                        report["mime_type"], len(report["content"]),
                        hashlib.sha256(report["content"]).hexdigest(), doctor_id,
                        datetime.now(timezone.utc).replace(tzinfo=None),
                    ),
                )
                report["_id"] = cursor.lastrowid
        return [{
            "_id": report["_id"],
            "original_name": report["original_name"],
            "mime_type": report["mime_type"],
            "size_bytes": len(report["content"]),
        } for report in prepared]
    except Exception:
        for path in saved_paths:
            path.unlink(missing_ok=True)
        raise


def fetch_authorized_report(report_id, current_user):
    parameters = [report_id]
    authorization = ""
    if current_user["role"] == "patient":
        authorization = " AND patient_profiles.linked_user_id = %s"
        parameters.append(parse_database_id(current_user["id"]))
    return fetch_one(
        "SELECT medical_reports.storage_key, medical_reports.original_name, "
        "medical_reports.mime_type FROM medical_reports "
        "JOIN patient_profiles ON patient_profiles.id = medical_reports.patient_profile_id "
        "WHERE medical_reports.id = %s" + authorization,
        tuple(parameters),
    )


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
    ehr_filter = ""
    search_parameters = ()
    if normalized_search:
        ehr_filter = (
            " AND (LOCATE(%s, LOWER(TRIM(patient_profiles.full_name))) > 0 "
            "OR LOCATE(%s, LOWER(patient_profiles.patient_code)) > 0 "
            "OR LOCATE(%s, CAST(patient_profiles.linked_user_id AS CHAR)) > 0 "
            "OR EXISTS (SELECT 1 FROM ehr_records AS search_record "
            "WHERE search_record.patient_profile_id = patient_profiles.id "
            "AND LOCATE(%s, CAST(search_record.id AS CHAR)) > 0))"
        )
        search_parameters = (
            normalized_search, normalized_search, normalized_search, normalized_search,
        )

    total_row = fetch_one(
        "SELECT COUNT(DISTINCT patient_profiles.id) AS total FROM ehr_records "
        "JOIN patient_profiles ON patient_profiles.id = ehr_records.patient_profile_id "
        f"WHERE TRUE{ehr_filter}",
        search_parameters,
    )

    records = fetch_all(
        "SELECT 'Manually Added EHR' AS source_type, CAST(summary_visit.id AS CHAR) AS source_id, "
        "patient_profiles.patient_code, patient_profiles.full_name, "
        "CAST(patient_profiles.age AS CHAR) AS age, patient_profiles.gender, "
        "patient_profiles.blood_group AS blood_type, patient_profiles.mobile_number, "
        "patient_profiles.email, "
        "summary_visit.medical_condition, summary_visit.diagnosis, summary_visit.treatment, "
        "summary_visit.doctor_name, summary_visit.record_date AS treatment_date, "
        "summary_visit.hospital_name, visit_counts.visit_count, summary_visit.id AS record_sort_id "
        "FROM patient_profiles "
        "JOIN ehr_records AS summary_visit ON summary_visit.id = ("
        "SELECT latest_visit.id FROM ehr_records AS latest_visit "
        "WHERE latest_visit.patient_profile_id = patient_profiles.id "
        "ORDER BY latest_visit.record_date DESC, latest_visit.id DESC LIMIT 1) "
        "JOIN (SELECT patient_profile_id, COUNT(*) AS visit_count FROM ehr_records "
        "GROUP BY patient_profile_id) AS visit_counts "
        "ON visit_counts.patient_profile_id = patient_profiles.id "
        f"WHERE TRUE{ehr_filter} "
        "ORDER BY treatment_date DESC, record_sort_id DESC "
        "LIMIT %s OFFSET %s",
        search_parameters + (per_page, offset),
    )
    for record in records:
        record.pop("record_sort_id", None)
    return {"records": records, "total": total_row["total"]}


def update_record_fields(record_id, updates):
    columns = ", ".join(f"`{field}` = %s" for field in updates)
    values = tuple(updates.values()) + (record_id,)
    with database_cursor(commit=True) as cursor:
        cursor.execute("SELECT id FROM ehr_records WHERE id = %s FOR UPDATE", (record_id,))
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            f"UPDATE ehr_records SET {columns} WHERE id = %s",
            values,
        )
        if cursor.rowcount == 0:
            return False
        append_record_audit(cursor, record_id, "update", "doctor")
        return True


def append_record_audit(cursor, record_id, action, actor_role):
    cursor.execute("SELECT * FROM ehr_records WHERE id = %s", (record_id,))
    record = cursor.fetchone()
    if record is None:
        raise RuntimeError("Cannot audit a missing EHR record.")
    cursor.execute(
        "SELECT line_number, medicine_name, dose, frequency, duration "
        "FROM prescription_medicines WHERE record_id = %s ORDER BY line_number",
        (record_id,),
    )
    medicines = cursor.fetchall()
    record_hash = calculate_record_hash(record, medicines)

    cursor.execute(
        "SELECT last_block_index, last_block_hash FROM blockchain_state "
        "WHERE id = 1 FOR UPDATE"
    )
    state = cursor.fetchone()
    if state is None:
        raise RuntimeError("Blockchain schema is not initialized; run setup_mysql.py.")
    cursor.execute(
        "SELECT block_index, block_hash FROM blockchain_blocks "
        "ORDER BY block_index DESC LIMIT 1 FOR UPDATE"
    )
    tip = cursor.fetchone()
    if (
        tip is None
        or tip["block_index"] != state["last_block_index"]
        or tip["block_hash"] != state["last_block_hash"]
    ):
        raise RuntimeError("Blockchain tip does not match its persisted state.")
    previous = Block(
        index=state["last_block_index"],
        timestamp=datetime(1970, 1, 1, tzinfo=timezone.utc),
        record_id=None,
        record_hash="0" * 64,
        action="genesis",
        actor_role="system",
        previous_hash="0" * 64,
        hash=state["last_block_hash"],
    )
    block = Block(
        index=state["last_block_index"] + 1,
        timestamp=datetime.now(timezone.utc),
        record_id=record_id,
        record_hash=record_hash,
        action=action,
        actor_role=actor_role,
        previous_hash=previous.hash,
    )
    cursor.execute(
        "INSERT INTO blockchain_blocks "
        "(block_index, created_at, record_id, record_hash, action, actor_role, "
        "previous_hash, block_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (
            block.index,
            datetime.fromisoformat(block.timestamp).replace(tzinfo=None),
            record_id,
            record_hash,
            action,
            actor_role,
            block.previous_hash,
            block.hash,
        ),
    )
    cursor.execute(
        "UPDATE blockchain_state SET last_block_index = %s, last_block_hash = %s "
        "WHERE id = 1",
        (block.index, block.hash),
    )


def verify_blockchain(record_id=None):
    with database_cursor() as cursor:
        cursor.execute(
            "SELECT block_index, created_at AS timestamp, record_id, record_hash, "
            "action, actor_role, previous_hash, block_hash FROM blockchain_blocks "
            "ORDER BY block_index"
        )
        block_rows = cursor.fetchall()
        chain = Blockchain.from_blocks(block_rows)
        chain_valid = chain.is_chain_valid()

        cursor.execute(
            "SELECT last_block_index, last_block_hash FROM blockchain_state "
            "WHERE id = 1"
        )
        state = cursor.fetchone()
        state_valid = bool(
            state
            and chain.chain
            and state["last_block_index"] == chain.chain[-1].index
            and state["last_block_hash"] == chain.chain[-1].hash
        )

        query = "SELECT * FROM ehr_records"
        parameters = ()
        if record_id is not None:
            query += " WHERE id = %s"
            parameters = (record_id,)
        cursor.execute(query, parameters)
        records = cursor.fetchall()
        record_ids = [record["id"] for record in records]
        medicines_by_record = {current_id: [] for current_id in record_ids}
        if record_ids:
            placeholders = ", ".join(["%s"] * len(record_ids))
            cursor.execute(
                "SELECT record_id, line_number, medicine_name, dose, frequency, duration "
                f"FROM prescription_medicines WHERE record_id IN ({placeholders}) "
                "ORDER BY record_id, line_number",
                tuple(record_ids),
            )
            for medicine in cursor.fetchall():
                medicines_by_record[medicine["record_id"]].append(medicine)

    latest_hashes = {}
    for block in chain.chain[1:]:
        latest_hashes[block.record_id] = block.record_hash
    statuses = []
    for record in records:
        current_hash = calculate_record_hash(
            record, medicines_by_record[record["id"]]
        )
        audited_hash = latest_hashes.get(record["id"])
        statuses.append({
            "record_id": str(record["id"]),
            "record_hash": current_hash,
            "audited_hash": audited_hash,
            "status": (
                "untracked" if audited_hash is None
                else "valid" if audited_hash == current_hash
                else "mismatch"
            ),
        })
    valid_count = sum(item["status"] == "valid" for item in statuses)
    return {
        "chain_valid": chain_valid and state_valid,
        "records_checked": len(statuses),
        "records_valid": valid_count,
        "untracked_records": sum(item["status"] == "untracked" for item in statuses),
        "mismatched_records": sum(item["status"] == "mismatch" for item in statuses),
        "record_hashes_valid": valid_count == len(statuses),
        "integrity_valid": chain_valid and state_valid and valid_count == len(statuses),
        "tip_hash": chain.chain[-1].hash if chain.chain else None,
        "records": statuses if record_id is not None else [],
    }


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
