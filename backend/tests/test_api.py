import hashlib
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from werkzeug.datastructures import FileStorage
from mysql.connector.errors import IntegrityError, ProgrammingError
from werkzeug.security import check_password_hash, generate_password_hash

from backend.app import (
    consume_password_reset_token,
    create_access_token,
    create_app,
    add_patient_visit,
    fetch_demo_records,
    fetch_doctor_dashboard_records,
    fetch_patient_history,
    fetch_records,
    patient_email_lock_name,
    update_patient_profile_in_database,
)


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        self.client = self.app.test_client()

    def test_password_reset_request_returns_local_link_for_doctors_and_patients(self):
        self.app.config.update(DEBUG=True, PASSWORD_RESET_DEV_MODE=True)
        users = [
            {"id": 21, "email": "doctor@example.test", "role": "doctor"},
            {"id": 22, "email": "patient@example.test", "role": "patient"},
        ]
        for user in users:
            with (
                patch("backend.app.fetch_one", return_value=user),
                patch("backend.app.issue_password_reset_token") as issue_token,
            ):
                response = self.client.post(
                    "/api/password-reset/request",
                    json={"email": user["email"]},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"},
                )
            self.assertEqual(response.status_code, 200)
            reset_url = response.json["reset_url"]
            raw_token = parse_qs(urlsplit(reset_url).fragment)["token"][0]
            self.assertGreaterEqual(len(raw_token), 32)
            self.assertEqual(issue_token.call_args.args[0], user["id"])
            self.assertEqual(
                issue_token.call_args.args[1],
                hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            )
            self.assertNotIn(raw_token, str(issue_token.call_args.args))

    def test_password_reset_request_does_not_return_links_in_production(self):
        user = {"id": 21, "email": "doctor@example.test", "role": "doctor"}
        with (
            patch("backend.app.fetch_one", return_value=user),
            patch("backend.app.issue_password_reset_token") as issue_token,
        ):
            response = self.client.post(
                "/api/password-reset/request",
                json={"email": user["email"]},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reset_url", response.json)
        issue_token.assert_not_called()
        with patch("backend.app.fetch_one", return_value=None):
            unknown_email = self.client.post(
                "/api/password-reset/request",
                json={"email": "unknown@example.test"},
                environ_base={"REMOTE_ADDR": "127.0.0.1"},
            )
        self.assertEqual(unknown_email.status_code, response.status_code)
        self.assertEqual(unknown_email.json["message"], response.json["message"])

    def test_password_reset_uses_email_delivery_without_returning_token(self):
        self.app.config.update(MAIL_SERVER="smtp.example.test", MAIL_FROM="ehr@example.test")
        user = {"id": 22, "email": "patient@example.test", "role": "patient"}
        with (
            patch("backend.app.fetch_one", return_value=user),
            patch("backend.app.issue_password_reset_token"),
            patch("backend.app.send_password_reset_email") as send_email,
        ):
            response = self.client.post(
                "/api/password-reset/request",
                json={"email": user["email"]},
                environ_base={"REMOTE_ADDR": "192.0.2.20"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("reset_url", response.json)
        send_email.assert_called_once()

    def test_password_reset_confirm_hashes_password_for_both_roles(self):
        token = "t" * 43
        new_password = "new-secure-password"
        for role in ("doctor", "patient"):
            with self.subTest(role=role):
                saved_hash = {}

                def consume(token_hash, password_hash):
                    saved_hash["hash"] = password_hash
                    saved_hash["token_hash"] = token_hash
                    return "success"

                with patch("backend.app.consume_password_reset_token", side_effect=consume):
                    response = self.client.post(
                        "/api/password-reset/confirm",
                        json={
                            "token": token,
                            "password": new_password,
                            "confirm_password": new_password,
                        },
                    )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(check_password_hash(saved_hash["hash"], new_password))
                self.assertNotEqual(saved_hash["hash"], new_password)
                self.assertEqual(
                    saved_hash["token_hash"],
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                )

                account = {
                    "_id": 21, "name": "Account", "email": f"{role}@example.test",
                    "role": role, "status": "active", "password_hash": saved_hash["hash"],
                }
                with patch("backend.app.fetch_one", return_value=account):
                    correct_login = self.client.post(
                        "/api/login",
                        json={"email": account["email"], "password": new_password, "role": role},
                    )
                    old_password_login = self.client.post(
                        "/api/login",
                        json={"email": account["email"], "password": "old-password", "role": role},
                    )
                self.assertEqual(correct_login.status_code, 200)
                self.assertEqual(old_password_login.status_code, 401)

    def test_password_reset_rejects_invalid_expired_and_used_tokens(self):
        messages = {
            "invalid": "This reset link is invalid.",
            "expired": "This reset link has expired. Request a new one.",
            "used": "This reset link has already been used. Request a new one.",
        }
        for result, expected_message in messages.items():
            with self.subTest(result=result), patch(
                "backend.app.consume_password_reset_token", return_value=result
            ):
                response = self.client.post(
                    "/api/password-reset/confirm",
                    json={
                        "token": "t" * 43,
                        "password": "new-secure-password",
                        "confirm_password": "new-secure-password",
                    },
                )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json["error"], expected_message)

    def test_password_reset_token_consumer_enforces_expiry_and_single_use(self):
        class ResetCursor:
            def __init__(self, reset):
                self.reset = reset
                self.rowcount = 0
                self.result = None
                self.saved_password_hash = None

            def execute(self, statement, parameters):
                if statement.startswith("SELECT t.user_id"):
                    self.result = self.reset if parameters[0] == "a" * 64 else None
                elif statement.startswith("UPDATE users SET password_hash"):
                    self.saved_password_hash = parameters[0]
                    self.rowcount = 1
                elif statement.startswith("UPDATE password_reset_tokens SET used_at"):
                    self.reset["used_at"] = parameters[0]
                    self.rowcount = 1
                else:
                    raise AssertionError(f"Unexpected SQL: {statement}")

            def fetchone(self):
                return self.result

        def consume_with(reset):
            cursor = ResetCursor(reset)
            with patch("backend.app.database_cursor", return_value=nullcontext(cursor)):
                result = consume_password_reset_token("a" * 64, "hashed-new-password")
            return result, cursor

        active_reset = {
            "user_id": 21,
            "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=5),
            "used_at": None,
            "role": "doctor",
            "status": "active",
        }
        result, cursor = consume_with(active_reset)
        self.assertEqual(result, "success")
        self.assertEqual(cursor.saved_password_hash, "hashed-new-password")
        self.assertIsNotNone(active_reset["used_at"])
        self.assertEqual(consume_with(active_reset)[0], "used")

        expired_reset = {
            **active_reset,
            "expires_at": datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
            "used_at": None,
        }
        self.assertEqual(consume_with(expired_reset)[0], "expired")
        self.assertEqual(consume_with(None)[0], "invalid")

    def test_patient_registration_hashes_password_and_sets_patient_role(self):
        payload = {
            "name": "New Patient", "email": "new@example.test", "password": "correct-pass",
            "confirm_password": "correct-pass", "date_of_birth": "1995-04-12",
            "gender": "female", "phone": "555-0100", "role": "doctor",
        }
        with patch(
            "backend.app.register_patient_account", return_value={"status": "created"}
        ) as register_patient:
            response = self.client.post("/api/register", json=payload)
        self.assertEqual(response.status_code, 201)
        user = register_patient.call_args.args[0]
        self.assertEqual(user["role"], "patient")
        self.assertEqual(user["status"], "active")
        self.assertTrue(check_password_hash(user["password_hash"], "correct-pass"))

    def test_patient_email_lock_name_fits_mysql_limit(self):
        email = f"{'a' * 243}@example.test"
        lock_name = patient_email_lock_name(email)
        self.assertEqual(len(lock_name), 64)
        self.assertEqual(lock_name, patient_email_lock_name(email))
        self.assertNotEqual(lock_name, patient_email_lock_name("other@example.test"))

    def test_registration_of_existing_patient_requires_email_verification(self):
        self.app.config.update(MAIL_SERVER="smtp.example.test", MAIL_FROM="ehr@example.test")
        payload = {
            "name": "Existing Patient", "email": "patient@example.test",
            "password": "correct-pass", "confirm_password": "correct-pass",
            "date_of_birth": "1995-04-12", "gender": "female", "phone": "555-0100",
        }
        with (
            patch(
                "backend.app.register_patient_account",
                return_value={"status": "verification_required"},
            ) as register_patient,
            patch("backend.app.send_patient_verification_email") as send_email,
        ):
            response = self.client.post("/api/register", json=payload)
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("token", response.json)
        self.assertEqual(register_patient.call_args.args[0]["email"], payload["email"])
        self.assertIn("verify-email.HTML#token=", send_email.call_args.args[2])

    def test_patient_email_verification_confirmation_consumes_token(self):
        with patch("backend.app.consume_patient_link_verification", return_value="success") as consume:
            response = self.client.post(
                "/api/email-verification/confirm",
                json={"token": "v" * 43},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("verified", response.json["message"].lower())
        self.assertEqual(len(consume.call_args.args[0]), 64)

    def test_existing_patient_account_can_request_profile_link_verification(self):
        self.app.config.update(MAIL_SERVER="smtp.example.test", MAIL_FROM="ehr@example.test")
        token = create_access_token(self.app, {"_id": 17, "role": "patient"})
        with (
            patch(
                "backend.app.request_patient_profile_link",
                return_value={
                    "status": "verification_required",
                    "email": "patient@example.test",
                },
            ) as request_link,
            patch("backend.app.send_patient_verification_email") as send_email,
        ):
            response = self.client.post(
                "/api/patient/profile/link/request",
                headers={"Authorization": "Bearer " + token},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(request_link.call_args.args[0], 17)
        self.assertEqual(len(request_link.call_args.args[1]), 64)
        self.assertIn("verify-email.HTML#token=", send_email.call_args.args[2])

    def test_patient_can_log_in_with_credentials_from_registered_account(self):
        payload = {
            "name": "Registered Patient", "email": "registered@example.test",
            "password": "correct-pass", "confirm_password": "correct-pass",
            "date_of_birth": "1995-04-12", "gender": "female", "phone": "555-0100",
        }
        saved_users = []

        def save_user(user):
            saved_users.append({**user, "_id": 10})
            return 10

        with patch(
            "backend.app.register_patient_account",
            side_effect=lambda user, *_args: (
                save_user(user) and {"status": "created"}
            ),
        ):
            registration = self.client.post("/api/register", json=payload)
        self.assertEqual(registration.status_code, 201)

        with patch("backend.app.fetch_one", return_value=saved_users[0]):
            login = self.client.post(
                "/api/login",
                json={"email": payload["email"], "password": payload["password"], "role": "patient"},
            )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json["user"]["id"], "10")
        self.assertEqual(login.json["user"]["role"], "patient")
        self.assertNotIn("password_hash", login.json["user"])

    def test_duplicate_registration_is_rejected(self):
        payload = {
            "name": "Existing", "email": "existing@example.test", "password": "correct-pass",
            "confirm_password": "correct-pass", "date_of_birth": "1995-04-12",
            "gender": "male", "phone": "555-0100",
        }
        duplicate_error = IntegrityError(msg="duplicate email", errno=1062)
        with patch("backend.app.register_patient_account", side_effect=duplicate_error):
            response = self.client.post("/api/register", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.content_type, "application/json")

    def test_missing_registration_table_is_logged_without_exposing_database_error(self):
        missing_table_error = ProgrammingError(
            msg="Table 'ehr_blockchain.patient_profiles' doesn't exist",
            errno=1146,
        )
        with (
            patch(
                "backend.app.register_patient_account",
                side_effect=missing_table_error,
            ),
            self.assertLogs("backend.app", level="ERROR") as logged,
        ):
            response = self.client.post(
                "/api/register",
                json={
                    "name": "New Patient",
                    "email": "new@example.test",
                    "password": "correct-pass",
                    "confirm_password": "correct-pass",
                    "date_of_birth": "1995-04-12",
                    "gender": "female",
                    "phone": "555-0100",
                },
            )
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("patient_profiles", response.get_data(as_text=True))
        self.assertIn("Table 'ehr_blockchain.patient_profiles' doesn't exist", logged.output[0])

    def test_invalid_registration_returns_json(self):
        response = self.client.post("/api/register", json={"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/json")
        self.assertIn("fields", response.json)

    def test_registration_rejects_non_object_json(self):
        response = self.client.post("/api/register", json=["not", "an", "object"])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "A JSON object is required.")

    def test_patient_login_returns_token(self):
        patient = {
            "_id": 1, "name": "Patient", "email": "patient@test", "role": "patient",
            "status": "active", "password_hash": generate_password_hash("correct"),
        }
        with patch("backend.app.fetch_one", return_value=patient):
            response = self.client.post(
                "/api/login",
                json={"email": "patient@test", "password": "correct", "role": "patient"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user"]["role"], "patient")
        self.assertEqual(response.json["user"]["id"], "1")
        self.assertIn("token", response.json)

    def test_invalid_password_and_role_are_rejected(self):
        patient = {
            "_id": 1, "name": "Patient", "email": "patient@test", "role": "patient",
            "status": "active", "password_hash": generate_password_hash("correct"),
        }
        with patch("backend.app.fetch_one", return_value=patient):
            bad_password = self.client.post(
                "/api/login",
                json={"email": "patient@test", "password": "wrong", "role": "patient"},
            )
            bad_role = self.client.post(
                "/api/login",
                json={"email": "patient@test", "password": "correct", "role": "doctor"},
            )
        self.assertEqual(bad_password.status_code, 401)
        self.assertEqual(bad_role.status_code, 401)

    def test_admin_only_doctor_creation(self):
        token = create_access_token(self.app, {"_id": 1, "role": "admin"})
        with patch("backend.app.insert_user", return_value=12) as insert_user:
            response = self.client.post(
                "/api/admin/doctors",
                headers={"Authorization": "Bearer {}".format(token)},
                json={"name": "Doctor", "email": "doctor@example.test", "password": "secure-pass"},
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["id"], "12")
        self.assertEqual(insert_user.call_args.args[0]["role"], "doctor")

    def test_patient_cannot_create_doctor_and_admin_created_doctor_can_log_in(self):
        patient_token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        denied = self.client.post(
            "/api/admin/doctors",
            headers={"Authorization": f"Bearer {patient_token}"},
            json={"name": "Doctor", "email": "doctor@example.test", "password": "secure-pass"},
        )
        self.assertEqual(denied.status_code, 403)

        admin_token = create_access_token(self.app, {"_id": 1, "role": "admin"})
        created_users = []

        def store_doctor(user):
            created_users.append({**user, "_id": 12})
            return 12

        with patch("backend.app.insert_user", side_effect=store_doctor):
            created = self.client.post(
                "/api/admin/doctors",
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"name": "Doctor", "email": "doctor@example.test", "password": "secure-pass"},
            )
        self.assertEqual(created.status_code, 201)
        doctor = created_users[0]
        self.assertEqual(doctor["role"], "doctor")
        self.assertTrue(check_password_hash(doctor["password_hash"], "secure-pass"))
        with patch("backend.app.fetch_one", return_value=doctor):
            login = self.client.post(
                "/api/login",
                json={"email": "doctor@example.test", "password": "secure-pass", "role": "doctor"},
            )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json["user"]["role"], "doctor")
        self.assertIn("token", login.json)
        with patch(
            "backend.app.fetch_demo_records",
            return_value={"records": [{"_id": 1, "full_name": "Demo Patient", "is_demo": True}], "total": 1},
        ) as fetch_demo_records:
            demo_records = self.client.get(
                "/api/demo-records?page=1&per_page=25",
                headers={"Authorization": f"Bearer {login.json['token']}"},
            )
        self.assertEqual(demo_records.status_code, 200)
        self.assertEqual(demo_records.json["total"], 1)
        fetch_demo_records.assert_called_once_with(1, 25)

    def test_patient_records_are_scoped_to_patient_id(self):
        record = {"_id": 20, "patient_id": 3, "patient_name": "Example Patient", "diagnosis": "test"}
        token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        with patch("backend.app.fetch_records", return_value=[record]) as fetch_records:
            response = self.client.get(
                "/api/records",
                headers={"Authorization": "Bearer {}".format(token)},
            )
        self.assertEqual(response.status_code, 200)
        fetch_records.assert_called_once_with(3)
        self.assertEqual(response.json["records"][0]["patient_id"], "3")
        self.assertEqual(response.json["records"][0]["patient_name"], "Example Patient")
        self.assertEqual(response.json["records"][0]["_id"], "20")

    def test_doctor_records_include_registered_patients_without_ehr(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        patient = {
            "patient_id": 31,
            "patient_name": "Registered Patient",
            "_id": None,
            "medical_condition": None,
            "diagnosis": None,
            "treatment": None,
            "record_date": None,
        }
        with patch("backend.app.fetch_records", return_value=[patient]) as fetch_records:
            response = self.client.get(
                "/api/records",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200)
        fetch_records.assert_called_once_with(include_patient_accounts=True)
        self.assertEqual(response.json["records"][0]["patient_name"], "Registered Patient")
        self.assertIsNone(response.json["records"][0]["_id"])
        self.assertNotIn("password_hash", response.json["records"][0])

    def test_fetch_records_joins_patient_profile_and_filters_by_account_id(self):
        with patch("backend.app.fetch_all", return_value=[]) as fetch_all:
            fetch_records(3)
        query, parameters = fetch_all.call_args.args
        self.assertIn("patient_profiles.full_name AS patient_name", query)
        self.assertIn("JOIN patient_profiles", query)
        self.assertIn("WHERE patient_profiles.linked_user_id = %s", query)
        self.assertEqual(parameters, (3,))

    def test_fetch_records_can_include_patient_accounts_without_ehr(self):
        with patch("backend.app.fetch_all", return_value=[]) as fetch_all:
            fetch_records(include_patient_accounts=True)
        query = fetch_all.call_args.args[0]
        self.assertIn("FROM users LEFT JOIN patient_profiles", query)
        self.assertIn("LEFT JOIN ehr_records", query)
        self.assertIn("users.`role` = 'patient'", query)
        self.assertIn("users.status = 'active'", query)
        self.assertNotIn("password_hash", query)
        self.assertEqual(fetch_all.call_args.kwargs, {})

    def test_patient_cannot_create_record(self):
        token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        response = self.client.post(
            "/api/records",
            headers={"Authorization": f"Bearer {token}"},
            json={},
        )
        self.assertEqual(response.status_code, 403)

    def test_doctor_dashboard_api_returns_valid_empty_page(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.fetch_doctor_dashboard_records",
            return_value={"records": [], "total": 0},
        ) as fetch_dashboard_records:
            response = self.client.get(
                "/api/doctor-dashboard/records?page=1&per_page=25",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 200)
        fetch_dashboard_records.assert_called_once_with(1, 25, "")
        self.assertEqual(
            response.json,
            {
                "real_ehr_only": True,
                "records": [],
                "page": 1,
                "per_page": 25,
                "total": 0,
                "pages": 0,
            },
        )

    def test_doctor_can_update_only_patient_profile_fields(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        payload = {
            "full_name": " Updated Patient ",
            "age": "42",
            "gender": "FEMALE",
            "blood_group": "o+",
            "mobile_number": " +1 555-123-4567 ",
            "email": " Patient@example.test ",
        }
        with patch("backend.app.update_patient_profile_in_database", return_value=True) as update_profile:
            response = self.client.put(
                "/api/patients/EHR-0000000042",
                headers={"Authorization": "Bearer " + token},
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["message"], "Patient details updated.")
        self.assertEqual(response.json["patient"]["patient_code"], "EHR-0000000042")
        update_profile.assert_called_once_with(
            "EHR-0000000042",
            {
                "full_name": "Updated Patient",
                "age": 42,
                "gender": "female",
                "blood_group": "O+",
                "mobile_number": "+1 555-123-4567",
                "email": "patient@example.test",
            },
        )

    def test_patient_profile_update_requires_doctor_authorization(self):
        response = self.client.put(
            "/api/patients/EHR-0000000042",
            json={},
        )
        self.assertEqual(response.status_code, 401)

        token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        response = self.client.put(
            "/api/patients/EHR-0000000042",
            headers={"Authorization": "Bearer " + token},
            json={},
        )
        self.assertEqual(response.status_code, 403)

    def test_patient_profile_update_rejects_visit_and_unrecognized_fields(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        payload = {
            "full_name": "Patient",
            "age": "42",
            "gender": "female",
            "blood_group": "O+",
            "mobile_number": "+1 555-123-4567",
            "email": "patient@example.test",
            "diagnosis": "Must not be editable here",
        }
        with patch("backend.app.update_patient_profile_in_database") as update_profile:
            response = self.client.put(
                "/api/patients/EHR-0000000042",
                headers={"Authorization": "Bearer " + token},
                json=payload,
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Only patient name", response.json["error"])
        update_profile.assert_not_called()

    def test_patient_profile_update_validates_editable_values(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        payload = {
            "full_name": "",
            "age": "131",
            "gender": "unknown",
            "blood_group": "X",
            "mobile_number": "invalid",
            "email": "not-an-email",
        }
        with patch("backend.app.update_patient_profile_in_database") as update_profile:
            response = self.client.put(
                "/api/patients/EHR-0000000042",
                headers={"Authorization": "Bearer " + token},
                json=payload,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            set(response.json["fields"]),
            {"full_name", "age", "gender", "blood_group", "mobile_number", "email"},
        )
        update_profile.assert_not_called()

    def test_patient_profile_update_does_not_update_visit_records(self):
        profile = {
            "full_name": "Patient",
            "age": 42,
            "gender": "female",
            "blood_group": "O+",
            "mobile_number": "+1 555-123-4567",
            "email": "patient@example.test",
        }
        with patch("backend.app.execute_statement", return_value=(None, 1)) as execute:
            self.assertTrue(update_patient_profile_in_database("EHR-0000000042", profile))

        statement, parameters = execute.call_args.args
        self.assertIn("UPDATE patient_profiles SET", statement)
        self.assertNotIn("patient_code =", statement)
        self.assertNotIn("ehr_records", statement)
        self.assertNotIn("prescription", statement)
        self.assertNotIn("medical_reports", statement)
        self.assertEqual(
            parameters,
            (
                "Patient", 42, "female", "O+", "+1 555-123-4567",
                "patient@example.test", "EHR-0000000042",
            ),
        )

    def test_demo_records_reject_unauthenticated_users(self):
        response = self.client.get("/api/demo-records")
        self.assertEqual(response.status_code, 401)

    def test_demo_records_reject_patient_role(self):
        token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        response = self.client.get(
            "/api/demo-records",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 403)

    def test_demo_records_are_doctor_only_and_paginated_separately(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        demo_record = {
            "_id": 1, "full_name": "Demo Patient", "is_demo": True,
            "medical_condition": "Asthma",
        }
        with patch(
            "backend.app.fetch_demo_records",
            return_value={"records": [demo_record], "total": 26},
        ) as fetch_demo_records:
            response = self.client.get(
                "/api/demo-records?page=2&per_page=25",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200)
        fetch_demo_records.assert_called_once_with(2, 25)
        self.assertTrue(response.json["demo_data"])
        self.assertEqual(response.json["records"][0]["full_name"], "Demo Patient")
        self.assertEqual(response.json["pages"], 2)

    def test_demo_records_search_uses_the_filtered_dataset_and_pagination(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.fetch_demo_records",
            return_value={"records": [{"_id": "1", "full_name": "Bobby JacksOn"}], "total": 1},
        ) as fetch_demo_records:
            response = self.client.get(
                "/api/demo-records?page=1&per_page=100&search=%20bObBy%20",
                headers={"Authorization": "Be" + "arer " + token},
            )
        self.assertEqual(response.status_code, 200, response.get_json())
        fetch_demo_records.assert_called_once_with(1, 100, "bObBy")
        self.assertEqual(response.json["records"][0]["full_name"], "Bobby JacksOn")
        self.assertEqual(response.json["pages"], 1)

    def test_fetch_demo_records_search_is_case_insensitive_and_scoped_to_demo_rows(self):
        with (
            patch("backend.app.fetch_one", return_value={"total": 101}) as fetch_one,
            patch("backend.app.fetch_all", return_value=[]) as fetch_all,
        ):
            fetch_demo_records(2, 100, "  bObBy  ")
        count_query, count_parameters = fetch_one.call_args.args
        records_query, records_parameters = fetch_all.call_args.args
        for query in (count_query, records_query):
            self.assertIn("WHERE is_demo = TRUE", query)
            self.assertIn("LOWER(TRIM(full_name))", query)
            self.assertIn("CAST(id AS CHAR)", query)
        self.assertEqual(count_parameters, ("bobby", "bobby"))
        self.assertEqual(records_parameters, ("bobby", "bobby", 100, 100))

    def test_doctor_dashboard_records_are_doctor_only_and_paginated(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        records = [
            {
                "source_type": "Manually Added EHR",
                "source_id": "2",
                "patient_code": "EHR-0000000002",
                "full_name": "Manual Patient",
                "visit_count": 2,
                "medical_condition": "Diabetes",
                "diagnosis": "Type 2",
                "treatment": "Insulin",
            },
            {
                "source_type": "Manually Added EHR",
                "source_id": "3",
                "patient_code": "EHR-0000000003",
                "full_name": "Second Manual Patient",
                "visit_count": 1,
                "medical_condition": "Asthma",
                "diagnosis": "Mild",
                "treatment": "Inhaler",
            },
        ]
        with patch(
            "backend.app.fetch_doctor_dashboard_records",
            return_value={"records": records, "total": 27},
        ) as fetch_dashboard_records:
            response = self.client.get(
                "/api/doctor-dashboard/records?page=2&per_page=25&search=patient",
                headers={"Authorization": f"Bearer {token}"},
            )
        self.assertEqual(response.status_code, 200)
        fetch_dashboard_records.assert_called_once_with(2, 25, "patient")
        self.assertEqual(
            response.json,
            {
                "real_ehr_only": True,
                "records": records,
                "page": 2,
                "per_page": 25,
                "total": 27,
                "pages": 2,
            },
        )
        self.assertEqual(response.json["pages"], 2)
        self.assertEqual(response.json["records"], records)
        patient_token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        forbidden = self.client.get(
            "/api/doctor-dashboard/records",
            headers={"Authorization": f"Bearer {patient_token}"},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_fetch_doctor_dashboard_records_returns_unique_real_patients_only(self):
        rows = [
            {
                "source_type": "Manually Added EHR",
                "source_id": "103",
                "patient_code": "EHR-0000000042",
                "full_name": "Patient One",
                "visit_count": 3,
                "record_sort_id": 103,
            },
            {
                "source_type": "Manually Added EHR",
                "source_id": "104",
                "patient_code": "EHR-0000000043",
                "full_name": "Patient Two",
                "visit_count": 1,
                "record_sort_id": 104,
            },
        ]
        with (
            patch("backend.app.fetch_one", return_value={"total": 2}) as fetch_one,
            patch("backend.app.fetch_all", return_value=rows) as fetch_all,
        ):
            result = fetch_doctor_dashboard_records(2, 2, "  MiXeD  ")
        count_query, count_parameters = fetch_one.call_args.args
        records_query, records_parameters = fetch_all.call_args.args
        for query in (count_query, records_query):
            self.assertIn("ehr_records", query)
            self.assertNotIn("demo_ehr_records", query)
        self.assertIn("LOWER(TRIM(patient_profiles.full_name))", records_query)
        self.assertIn(
            "JOIN patient_profiles ON patient_profiles.id = ehr_records.patient_profile_id",
            count_query,
        )
        self.assertIn("CAST(search_record.id AS CHAR)", records_query)
        self.assertIn("LOWER(patient_profiles.patient_code)", records_query)
        self.assertIn("COUNT(DISTINCT patient_profiles.id)", count_query)
        self.assertIn("COUNT(*) AS visit_count", records_query)
        self.assertIn("GROUP BY patient_profile_id", records_query)
        self.assertIn("ORDER BY latest_visit.record_date DESC, latest_visit.id DESC", records_query)
        self.assertIn("summary_visit.id AS record_sort_id", records_query)
        self.assertIn(
            "ORDER BY treatment_date DESC, record_sort_id DESC",
            records_query,
        )
        self.assertNotIn("record_sort_id", result["records"][0])
        self.assertEqual(result["total"], 2)
        self.assertEqual(
            [(row["patient_code"], row["visit_count"]) for row in result["records"]],
            [("EHR-0000000042", 3), ("EHR-0000000043", 1)],
        )
        self.assertEqual(
            count_parameters,
            ("mixed", "mixed", "mixed", "mixed"),
        )
        self.assertEqual(
            records_parameters,
            ("mixed", "mixed", "mixed", "mixed", 2, 2),
        )

    def test_dashboard_api_returns_only_real_patient_summaries(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        rows = [
            {
                "source_type": "Manually Added EHR",
                "source_id": "103",
                "patient_code": "EHR-0000000042",
                "full_name": "Patient One",
                "email": "patient.one@example.test",
                "visit_count": 3,
            },
            {
                "source_type": "Manually Added EHR",
                "source_id": "104",
                "patient_code": "EHR-0000000043",
                "full_name": "Patient Two",
                "visit_count": 1,
            },
        ]
        with patch(
            "backend.app.fetch_doctor_dashboard_records",
            return_value={"records": rows, "total": 2},
        ):
            response = self.client.get(
                "/api/doctor-dashboard/records",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["real_ehr_only"])
        records = response.json["records"]
        self.assertEqual(len(records), 2)
        self.assertEqual(response.json["total"], 2)
        self.assertEqual(
            [record["source_type"] for record in records],
            ["Manually Added EHR", "Manually Added EHR"],
        )
        self.assertEqual(
            {record["patient_code"]: record["visit_count"] for record in records},
            {"EHR-0000000042": 3, "EHR-0000000043": 1},
        )
        self.assertEqual(records[0]["email"], "patient.one@example.test")

    def test_demo_records_reject_invalid_pagination(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        response = self.client.get(
            "/api/demo-records?page=0",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 400)

    def test_doctor_can_create_patient_record(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        payload = {
            "patient_id": "3", "medical_condition": "Condition", "diagnosis": "Diagnosis",
            "treatment": "Treatment", "blood_pressure": " 120/80 ", "age": "42",
            "gender": "FEMALE", "record_date": "2025-10-10",
        }
        with (
            patch("backend.app.fetch_one", return_value={"id": 3}),
            patch("backend.app.insert_record", return_value=24) as insert_record,
        ):
            response = self.client.post(
                "/api/records",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["id"], "24")
        record = insert_record.call_args.args[0]
        self.assertEqual(record["patient_id"], 3)
        self.assertEqual(record["created_by"], 8)
        self.assertEqual(record["blood_pressure"], "120/80")
        self.assertEqual(record["gender"], "female")
        self.assertEqual(record["record_date"], datetime(2025, 10, 10))

    def test_doctor_can_create_patient_and_first_visit(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        payload = {
            "full_name": "New Patient", "age": "42", "gender": "female",
            "blood_group": "O+", "mobile_number": "555-0100",
            "email": "new.patient@example.test", "treatment_date": "2025-10-10",
            "hospital_name": "General Hospital", "diagnosis": "Condition",
            "treatment": "Rest and monitor symptoms.",
            "medicines": [{
                "medicine_name": "Medicine", "dose": "10 mg",
                "frequency": "Daily", "duration": "7 days",
            }],
        }
        with patch(
            "backend.app.create_patient_with_visit",
            return_value={"patient_code": "EHR-0000000042", "visit_id": 24},
        ) as create_patient:
            response = self.client.post(
                "/api/patients",
                headers={"Authorization": "Bearer " + token},
                json=payload,
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["patient_code"], "EHR-0000000042")
        self.assertEqual(response.json["visit_id"], "24")
        self.assertEqual(create_patient.call_args.args[0]["email"], payload["email"])
        self.assertEqual(create_patient.call_args.args[1]["medicines"][0]["medicine_name"], "Medicine")
        self.assertEqual(
            create_patient.call_args.args[1]["treatment"],
            "Rest and monitor symptoms.",
        )

    def test_doctor_can_add_repeat_visit_to_existing_patient(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        payload = {
            "treatment_date": "2025-10-10", "hospital_name": "General Hospital",
            "diagnosis": "Follow-up", "treatment": "Continue current care.",
            "medicines": [{
                "medicine_name": "Medicine", "dose": "10 mg",
                "frequency": "Daily", "duration": "7 days",
            }],
        }
        with patch(
            "backend.app.add_patient_visit",
            side_effect=[25, 26, 27],
        ) as add_visit:
            responses = [
                self.client.post(
                    "/api/patients/EHR-0000000042/visits",
                    headers={"Authorization": "Bearer " + token},
                    json=payload,
                ),
            ]
            second_payload = {**payload, "treatment_date": "2025-10-11"}
            responses.append(
                self.client.post(
                    "/api/patients/EHR-0000000042/visits",
                    headers={"Authorization": "Bearer " + token},
                    json=second_payload,
                )
            )
            third_payload = {**payload, "treatment_date": "2025-10-12"}
            responses.append(
                self.client.post(
                    "/api/patients/EHR-0000000042/visits",
                    headers={"Authorization": "Bearer " + token},
                    json=third_payload,
                )
            )
        self.assertEqual([response.status_code for response in responses], [201, 201, 201])
        self.assertEqual(
            [response.json["visit_id"] for response in responses],
            ["25", "26", "27"],
        )
        self.assertEqual(add_visit.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in add_visit.call_args_list],
            ["EHR-0000000042"] * 3,
        )
        self.assertEqual(
            [call.args[1]["treatment"] for call in add_visit.call_args_list],
            ["Continue current care."] * 3,
        )

    def test_doctor_visit_requires_a_treatment_plan(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        response = self.client.post(
            "/api/patients/EHR-0000000042/visits",
            headers={"Authorization": "Bearer " + token},
            json={
                "treatment_date": "2025-10-10",
                "hospital_name": "General Hospital",
                "diagnosis": "Follow-up",
                "medicines": [{
                    "medicine_name": "Medicine",
                    "dose": "10 mg",
                    "frequency": "Daily",
                    "duration": "7 days",
                }],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("treatment", response.json["fields"])

    def test_doctor_can_search_patient_name_or_code(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.search_patient_profiles",
            return_value=[{"patient_code": "EHR-0000000042", "full_name": "Patient"}],
        ) as search:
            response = self.client.get(
                "/api/patients/search?q=Pat",
                headers={"Authorization": "Bearer " + token},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["patients"][0]["patient_code"], "EHR-0000000042")
        search.assert_called_once_with("Pat")

    def test_doctor_can_upload_visit_reports(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        report = FileStorage(filename="scan.pdf", content_type="application/pdf")
        with patch(
            "backend.app.save_visit_reports",
            return_value=[{"_id": 10, "original_name": "scan.pdf"}],
        ) as save_reports:
            response = self.client.post(
                "/api/patients/EHR-0000000042/visits/24/reports",
                headers={"Authorization": "Bearer " + token},
                data={"reports": report},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json["reports"][0]["original_name"], "scan.pdf")
        save_reports.assert_called_once()

    def test_report_file_validation_checks_real_content_not_only_extension(self):
        from backend.app import report_mime_type

        self.assertEqual(report_mime_type("scan.pdf", b"%PDF-1.7"), "application/pdf")
        self.assertIsNone(report_mime_type("scan.pdf", b"<html>not a report</html>"))

    def test_doctor_record_creation_rejects_blank_required_values(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch("backend.app.fetch_one") as fetch_patient, patch(
            "backend.app.insert_record"
        ) as insert_record:
            response = self.client.post(
                "/api/records",
                headers={"Authorization": "Be" + "arer " + token},
                json={
                    "patient_id": "3",
                    "medical_condition": "  ",
                    "diagnosis": "Diagnosis",
                    "treatment": "Treatment",
                },
            )
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertIn("medical_condition", response.json["fields"])
        fetch_patient.assert_not_called()
        insert_record.assert_not_called()

    def test_doctor_record_creation_rejects_invalid_optional_fields(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        response = self.client.post(
            "/api/records",
            headers={"Authorization": "Be" + "arer " + token},
            json={
                "patient_id": "3",
                "medical_condition": "Condition",
                "diagnosis": "Diagnosis",
                "treatment": "Treatment",
                "gender": "unknown",
            },
        )
        self.assertEqual(response.status_code, 400, response.get_json())
        self.assertEqual(response.json["error"], "Select a valid gender.")

    def test_doctor_can_update_record(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch("backend.app.update_record_fields", return_value=True) as update_record:
            response = self.client.put(
                "/api/records/24",
                headers={"Authorization": f"Bearer {token}"},
                json={"diagnosis": "Updated diagnosis"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(update_record.call_args.args[0], 24)
        self.assertEqual(update_record.call_args.args[1]["diagnosis"], "Updated diagnosis")

    def test_admin_can_update_doctor_status(self):
        token = create_access_token(self.app, {"_id": 1, "role": "admin"})
        with patch("backend.app.set_doctor_status", return_value=True) as set_status:
            response = self.client.patch(
                "/api/admin/doctors/12/status",
                headers={"Authorization": f"Bearer {token}"},
                json={"status": "inactive"},
            )
        self.assertEqual(response.status_code, 200)
        set_status.assert_called_once_with(12, "inactive")

    def test_health_check_pings_mysql(self):
        with patch("backend.app.fetch_one", return_value={"ok": 1}):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["database"], "connected")

    def test_health_check_reports_missing_mysql_configuration(self):
        with patch("backend.app.fetch_one", side_effect=RuntimeError("missing MySQL configuration")):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.content_type, "application/json")
        self.assertEqual(response.json["database"], "unavailable")

    def test_blockchain_verification_requires_doctor_or_admin(self):
        response = self.client.get("/api/blockchain/verify")
        self.assertEqual(response.status_code, 401)

        patient_token = create_access_token(
            self.app, {"_id": 9, "role": "patient"}
        )
        response = self.client.get(
            "/api/blockchain/verify",
            headers={"Authorization": "Bearer " + patient_token},
        )
        self.assertEqual(response.status_code, 403)

    def test_authorized_blockchain_verification_and_record_id_validation(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.verify_blockchain",
            return_value={
                "chain_valid": True,
                "records_checked": 1,
                "records_valid": 1,
                "untracked_records": 0,
                "mismatched_records": 0,
                "record_hashes_valid": True,
                "integrity_valid": True,
                "tip_hash": "a" * 64,
                "records": [{
                    "record_id": "24", "record_hash": "b" * 64,
                    "audited_hash": "b" * 64, "status": "valid",
                }],
            },
        ) as verify:
            response = self.client.get(
                "/api/blockchain/verify?record_id=24",
                headers={"Authorization": "Bearer " + token},
            )
            invalid = self.client.get(
                "/api/blockchain/verify?record_id=0",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["integrity_valid"])
        self.assertEqual(response.json["records"][0]["record_id"], "24")
        verify.assert_called_once_with(24)
        self.assertEqual(invalid.status_code, 400)

    def test_blockchain_verification_returns_404_when_record_not_found(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.verify_blockchain",
            return_value={
                "chain_valid": True,
                "records_checked": 0,
                "records_valid": 0,
                "untracked_records": 0,
                "mismatched_records": 0,
                "record_hashes_valid": True,
                "integrity_valid": True,
                "tip_hash": "a" * 64,
                "records": [],
            },
        ):
            response = self.client.get(
                "/api/blockchain/verify?record_id=9999",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"], "EHR record not found.")

    def test_blockchain_verification_returns_503_on_database_error(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.verify_blockchain",
            side_effect=RuntimeError("Database failure during verification"),
        ):
            response = self.client.get(
                "/api/blockchain/verify",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json["error"], "Unable to verify blockchain integrity.")

    def test_blockchain_verification_returns_503_when_schema_uninitialized(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.verify_blockchain",
            side_effect=RuntimeError("Blockchain schema is not initialized; run setup_mysql.py."),
        ):
            response = self.client.get(
                "/api/blockchain/verify",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json["error"],
            "Blockchain ledger is not initialized in the database. Run setup_mysql.py.",
        )

    def test_blockchain_verification_rejects_leading_zero_record_id(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        response = self.client.get(
            "/api/blockchain/verify?record_id=002",
            headers={"Authorization": "Bearer " + token},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json["error"], "record_id must be a valid positive integer.")

    def test_blockchain_verification_reports_tamper_mismatch(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.verify_blockchain",
            return_value={
                "chain_valid": True,
                "records_checked": 3,
                "records_valid": 2,
                "untracked_records": 0,
                "mismatched_records": 1,
                "record_hashes_valid": False,
                "integrity_valid": False,
                "tip_hash": "c" * 64,
                "records": [],
            },
        ):
            response = self.client.get(
                "/api/blockchain/verify",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["integrity_valid"])
        self.assertEqual(response.json["mismatched_records"], 1)

    def test_blockchain_verification_with_baseline_and_subsequent_update(self):
        token = create_access_token(self.app, {"_id": 8, "role": "doctor"})
        with patch(
            "backend.app.verify_blockchain",
            return_value={
                "chain_valid": True,
                "records_checked": 5,
                "records_valid": 5,
                "untracked_records": 0,
                "mismatched_records": 0,
                "record_hashes_valid": True,
                "integrity_valid": True,
                "tip_hash": "a" * 64,
                "records": [],
            },
        ):
            response = self.client.get(
                "/api/blockchain/verify",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["chain_valid"])
        self.assertTrue(response.json["integrity_valid"])
        self.assertEqual(response.json["records_valid"], 5)
        self.assertEqual(response.json["untracked_records"], 0)

    def test_api_errors_are_json(self):
        response = self.client.get("/api/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content_type, "application/json")


class VisitPersistenceTests(unittest.TestCase):
    def test_repeat_visits_insert_distinct_rows_for_the_same_patient_profile(self):
        class FakeCursor:
            def __init__(self):
                self.patient = {
                    "id": 42,
                    "linked_user_id": 17,
                    "full_name": "Patient",
                    "age": 41,
                    "gender": "female",
                }
                self.doctor = {"name": "Doctor"}
                self.result = None
                self.lastrowid = 100
                self.visit_inserts = []
                self.prescription_inserts = []

            def execute(self, statement, parameters=()):
                if statement.startswith("SELECT id, linked_user_id"):
                    self.result = self.patient
                elif statement.startswith("SELECT name FROM users"):
                    self.result = self.doctor
                elif statement.startswith("INSERT INTO ehr_records"):
                    self.lastrowid += 1
                    self.visit_inserts.append((statement, parameters, self.lastrowid))

            def fetchone(self):
                return self.result

            def executemany(self, _statement, parameters):
                if "INSERT INTO prescription_medicines" not in _statement:
                    raise AssertionError("Expected prescriptions to be inserted with each visit.")
                self.prescription_inserts.extend(parameters)

        cursor = FakeCursor()

        def committed_transaction(commit=False):
            self.assertTrue(commit)
            return nullcontext(cursor)

        first_visit = {
            "treatment_date": datetime(2025, 10, 10).date(),
            "diagnosis": "Initial diagnosis",
            "treatment": "Initial plan",
            "hospital_name": "General Hospital",
            "additional_instructions": "Rest",
            "follow_up_date": None,
            "medicines": [{
                "medicine_name": "Medicine",
                "dose": "10 mg",
                "frequency": "Daily",
                "duration": "7 days",
            }],
        }
        second_visit = {
            **first_visit,
            "treatment_date": datetime(2025, 10, 11).date(),
            "diagnosis": "Follow-up diagnosis",
            "treatment": "Follow-up plan",
        }
        third_visit = {
            **first_visit,
            "treatment_date": datetime(2025, 10, 12).date(),
            "diagnosis": "Third visit diagnosis",
            "treatment": "Third visit plan",
        }
        with (
            patch("backend.app.database_cursor", side_effect=committed_transaction) as transaction,
            patch("backend.app.append_record_audit") as audit_record,
        ):
            first_id = add_patient_visit("EHR-0000000042", first_visit, 8)
            second_id = add_patient_visit("EHR-0000000042", second_visit, 8)
            third_id = add_patient_visit("EHR-0000000042", third_visit, 8)

        self.assertEqual(
            [call.kwargs for call in transaction.call_args_list],
            [{"commit": True}] * 3,
        )
        self.assertEqual((first_id, second_id, third_id), (101, 102, 103))
        self.assertEqual(
            [call.args[1:] for call in audit_record.call_args_list],
            [(101, "create", "doctor"), (102, "create", "doctor"), (103, "create", "doctor")],
        )
        self.assertEqual(
            [insert[1][1] for insert in cursor.visit_inserts],
            [42, 42, 42],
        )
        self.assertEqual(
            [insert[1][2] for insert in cursor.visit_inserts],
            ["Initial diagnosis", "Follow-up diagnosis", "Third visit diagnosis"],
        )
        self.assertEqual(len({insert[2] for insert in cursor.visit_inserts}), 3)
        self.assertEqual(
            [prescription[0] for prescription in cursor.prescription_inserts],
            [101, 102, 103],
        )
        self.assertEqual(
            [prescription[2] for prescription in cursor.prescription_inserts],
            ["Medicine", "Medicine", "Medicine"],
        )

    def test_patient_history_loads_visits_by_profile_and_includes_saved_details(self):
        profile = {
            "_id": 42,
            "patient_code": "EHR-0000000042",
            "full_name": "Patient",
        }
        visits = [
            {
                "_id": 102,
                "diagnosis": "Latest diagnosis",
                "treatment": "Latest treatment plan",
                "treatment_date": datetime(2025, 10, 11),
                "doctor_name": "Doctor",
                "hospital_name": "General Hospital",
                "additional_instructions": "Continue rest",
                "follow_up_date": None,
            },
            {
                "_id": 101,
                "diagnosis": "Previous diagnosis",
                "treatment": "Previous treatment plan",
                "treatment_date": datetime(2025, 10, 10),
                "doctor_name": "Doctor",
                "hospital_name": "General Hospital",
                "additional_instructions": None,
                "follow_up_date": None,
            },
        ]
        medicine = {
            "record_id": 102,
            "medicine_name": "Medicine",
            "dose": "10 mg",
            "frequency": "Daily",
            "duration": "7 days",
        }
        report = {
            "_id": 6,
            "record_id": 102,
            "original_name": "scan.pdf",
            "mime_type": "application/pdf",
            "size_bytes": 128,
            "uploaded_at": datetime(2025, 10, 11),
        }
        with (
            patch("backend.app.fetch_one", return_value=profile) as fetch_profile,
            patch(
                "backend.app.fetch_all",
                side_effect=[visits, [medicine], [report]],
            ) as fetch_all,
        ):
            patient = fetch_patient_history("EHR-0000000042")

        history_query, history_parameters = fetch_all.call_args_list[0].args
        self.assertIn("WHERE patient_profile_id = %s", history_query)
        self.assertIn("ORDER BY record_date DESC, id DESC", history_query)
        self.assertEqual(history_parameters, (42,))
        fetch_profile.assert_called_once()
        self.assertEqual([visit["_id"] for visit in patient["visits"]], [102, 101])
        self.assertEqual(patient["visits"][0]["diagnosis"], "Latest diagnosis")
        self.assertEqual(patient["visits"][0]["treatment"], "Latest treatment plan")
        self.assertEqual(patient["visits"][0]["doctor_name"], "Doctor")
        self.assertEqual(patient["visits"][0]["hospital_name"], "General Hospital")
        self.assertEqual(patient["visits"][0]["additional_instructions"], "Continue rest")
        self.assertEqual(patient["visits"][0]["medicines"][0]["medicine_name"], "Medicine")
        self.assertEqual(patient["visits"][0]["reports"][0]["original_name"], "scan.pdf")

    def test_patient_history_api_returns_history_for_requested_patient_id(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-secret"})
        client = app.test_client()
        token = create_access_token(app, {"_id": 8, "role": "doctor"})
        patient = {
            "_id": 42,
            "patient_code": "EHR-0000000042",
            "full_name": "Patient",
            "visits": [
                {"_id": 102, "diagnosis": "Latest", "medicines": [], "reports": []},
                {"_id": 101, "diagnosis": "Previous", "medicines": [], "reports": []},
            ],
        }
        with patch("backend.app.fetch_patient_history", return_value=patient) as fetch_history:
            response = client.get(
                "/api/patients/EHR-0000000042",
                headers={"Authorization": "Bearer " + token},
            )

        self.assertEqual(response.status_code, 200)
        fetch_history.assert_called_once_with("EHR-0000000042")
        self.assertEqual(response.json["patient"]["patient_code"], "EHR-0000000042")
        self.assertEqual(
            [visit["diagnosis"] for visit in response.json["patient"]["visits"]],
            ["Latest", "Previous"],
        )


if __name__ == "__main__":
    unittest.main()
