import hashlib
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mysql.connector.errors import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from backend.app import (
    consume_password_reset_token,
    create_access_token,
    create_app,
    fetch_demo_records,
    fetch_doctor_dashboard_records,
    fetch_records,
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
        with patch("backend.app.insert_user", return_value=10) as insert_user:
            response = self.client.post("/api/register", json=payload)
        self.assertEqual(response.status_code, 201)
        user = insert_user.call_args.args[0]
        self.assertEqual(user["role"], "patient")
        self.assertEqual(user["status"], "active")
        self.assertTrue(check_password_hash(user["password_hash"], "correct-pass"))

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

        with patch("backend.app.insert_user", side_effect=save_user):
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
        with patch("backend.app.insert_user", side_effect=duplicate_error):
            response = self.client.post("/api/register", json=payload)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.content_type, "application/json")

    def test_invalid_registration_returns_json(self):
        response = self.client.post("/api/register", json={"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content_type, "application/json")
        self.assertIn("fields", response.json)

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
                headers={"Authorization": f"Bearer {token}"},
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
                headers={"Authorization": f"Bearer {token}"},
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

    def test_fetch_records_includes_patient_name_and_filters_by_patient_id(self):
        with patch("backend.app.fetch_all", return_value=[]) as fetch_all:
            fetch_records(3)
        query, parameters = fetch_all.call_args.args
        self.assertIn("users.name AS patient_name", query)
        self.assertIn("JOIN users ON users.id = ehr_records.patient_id", query)
        self.assertIn("WHERE ehr_records.patient_id = %s", query)
        self.assertEqual(parameters, (3,))

    def test_fetch_records_can_include_patient_accounts_without_ehr(self):
        with patch("backend.app.fetch_all", return_value=[]) as fetch_all:
            fetch_records(include_patient_accounts=True)
        query = fetch_all.call_args.args[0]
        self.assertIn("FROM users LEFT JOIN ehr_records", query)
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
                "source_type": "Dataset",
                "source_id": "1",
                "full_name": "Dataset Patient",
                "medical_condition": "Asthma",
            },
            {
                "source_type": "Manually Added EHR",
                "source_id": "2",
                "full_name": "Manual Patient",
                "medical_condition": "Diabetes",
                "diagnosis": "Type 2",
                "treatment": "Insulin",
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
        self.assertTrue(response.json["combined_data"])
        self.assertEqual(response.json["pages"], 2)
        self.assertEqual(
            [record["source_type"] for record in response.json["records"]],
            ["Dataset", "Manually Added EHR"],
        )
        patient_token = create_access_token(self.app, {"_id": 3, "role": "patient"})
        forbidden = self.client.get(
            "/api/doctor-dashboard/records",
            headers={"Authorization": f"Bearer {patient_token}"},
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_fetch_doctor_dashboard_records_normalizes_both_sources_and_filters_names(self):
        with (
            patch("backend.app.fetch_one", return_value={"total": 4}) as fetch_one,
            patch("backend.app.fetch_all", return_value=[]) as fetch_all,
        ):
            fetch_doctor_dashboard_records(2, 2, "  MiXeD  ")
        count_query, count_parameters = fetch_one.call_args.args
        records_query, records_parameters = fetch_all.call_args.args
        for query in (count_query, records_query):
            self.assertIn("demo_ehr_records", query)
            self.assertIn("ehr_records", query)
            self.assertIn("LOWER(TRIM(", query)
        self.assertIn("JOIN users AS patients", records_query)
        self.assertIn("JOIN users AS doctors", records_query)
        self.assertIn("UNION ALL", records_query)
        self.assertEqual(count_parameters, ("mixed", "mixed"))
        self.assertEqual(records_parameters, ("mixed", "mixed", 2, 2))

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
            "treatment": "Treatment",
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

    def test_api_errors_are_json(self):
        response = self.client.get("/api/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.content_type, "application/json")


if __name__ == "__main__":
    unittest.main()
