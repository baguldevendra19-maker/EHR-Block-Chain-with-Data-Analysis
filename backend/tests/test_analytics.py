import csv
import tempfile
import unittest
from pathlib import Path

from backend.app import create_app


HEADERS = (
    "Name", "Age", "Gender", "Blood Type", "Medical Condition", "Date of Admission",
    "Doctor", "Hospital", "Insurance Provider", "Billing Amount", "Room Number",
    "Admission Type", "Discharge Date", "Medication", "Test Results",
)


def sample_row(name, age, gender, condition, admission_date, hospital, billing):
    return {
        "Name": name,
        "Age": age,
        "Gender": gender,
        "Blood Type": "A+",
        "Medical Condition": condition,
        "Date of Admission": admission_date,
        "Doctor": "Sample Doctor",
        "Hospital": hospital,
        "Insurance Provider": "Sample Insurer",
        "Billing Amount": billing,
        "Room Number": "101",
        "Admission Type": "Urgent",
        "Discharge Date": "15-02-2024",
        "Medication": "Sample Medicine",
        "Test Results": "Normal",
    }


class HealthcareAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dataset_path = Path(self.temp_dir.name) / "healthcare_dataset.csv"
        duplicate = sample_row("Sample One", "34", "Female", "Asthma", "12-02-2024", "Hospital A", "100")
        rows = [
            duplicate,
            duplicate.copy(),
            sample_row("Sample Two", "50", "Male", "Diabetes", "13-02-2024", "Hospital B", "200"),
            sample_row("Invalid Date", "40", "Female", "Asthma", "invalid", "Hospital A", "150"),
        ]
        with self.dataset_path.open("w", encoding="utf-8", newline="") as dataset_file:
            writer = csv.DictWriter(dataset_file, fieldnames=HEADERS)
            writer.writeheader()
            writer.writerows(rows)
        app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "ANALYTICS_DATASET_PATH": str(self.dataset_path),
        })
        self.client = app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_weekly_filter_returns_sample_csv_aggregates(self):
        response = self.client.get("/api/analytics/healthcare?period=weekly&anchor_date=2024-02-14")

        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data["title"], "Healthcare Sample Dataset Analysis")
        self.assertEqual(data["period"]["start_date"], "2024-02-12")
        self.assertEqual(data["period"]["end_date"], "2024-02-18")
        self.assertEqual(data["kpis"]["total_records"], 3)
        self.assertEqual(data["kpis"]["average_age"], 39.3)
        self.assertEqual(data["kpis"]["average_billing"], 133.33)
        self.assertEqual(data["charts"]["medical_conditions"][0], {"label": "Asthma", "value": 2})
        self.assertEqual(data["charts"]["gender"], [
            {"label": "Female", "value": 2},
            {"label": "Male", "value": 1},
        ])
        self.assertEqual(data["charts"]["monthly_admissions"], [
            {"label": "2024-02", "value": 3},
        ])
        self.assertEqual(data["charts"]["admission_types"], [{"label": "Urgent", "value": 3}])
        self.assertEqual(sum(item["value"] for item in data["charts"]["billing_amounts"]), 3)
        self.assertNotIn("records", data)
        self.assertNotIn("Sample One", str(data))

    def test_daily_filter_uses_reference_date(self):
        response = self.client.get("/api/analytics/healthcare?period=daily&anchor_date=2024-02-12")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["kpis"]["total_records"], 2)
        self.assertEqual(response.json["period"]["start_date"], "2024-02-12")
        self.assertEqual(response.json["period"]["end_date"], "2024-02-12")

    def test_monthly_filter_uses_calendar_month(self):
        response = self.client.get("/api/analytics/healthcare?period=monthly&anchor_date=2024-02-14")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["kpis"]["total_records"], 3)
        self.assertEqual(response.json["period"]["start_date"], "2024-02-01")
        self.assertEqual(response.json["period"]["end_date"], "2024-02-29")

    def test_custom_range_is_inclusive_and_empty_range_is_explicit(self):
        response = self.client.get(
            "/api/analytics/healthcare?period=custom&start_date=2024-02-13&end_date=2024-02-13"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["kpis"]["total_records"], 1)

        empty = self.client.get(
            "/api/analytics/healthcare?period=custom&start_date=2024-03-01&end_date=2024-03-02"
        )
        self.assertEqual(empty.status_code, 200)
        self.assertTrue(empty.json["empty"])
        self.assertEqual(empty.json["kpis"]["total_records"], 0)
        self.assertIsNone(empty.json["kpis"]["average_age"])

    def test_invalid_ranges_and_periods_are_rejected(self):
        reversed_range = self.client.get(
            "/api/analytics/healthcare?period=custom&start_date=2024-02-14&end_date=2024-02-12"
        )
        invalid_date = self.client.get(
            "/api/analytics/healthcare?period=custom&start_date=not-a-date&end_date=2024-02-14"
        )
        invalid_period = self.client.get("/api/analytics/healthcare?period=yearly")

        self.assertEqual(reversed_range.status_code, 400)
        self.assertEqual(invalid_date.status_code, 400)
        self.assertEqual(invalid_period.status_code, 400)
