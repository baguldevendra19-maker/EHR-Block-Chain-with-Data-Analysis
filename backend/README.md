# EHR Secure backend

## MySQL setup

1. Start your local MySQL Server and open MySQL Workbench if you want to inspect the database there.
2. Set `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, and `MYSQL_PASSWORD` in `backend/.env`. Keep the password only in `.env`; do not commit or print that file. The example values are in `.env.example`.
   `MYSQL_BACKUP_FILE` can point to a non-empty SQL export; if unset, setup
   requires `ehr_blockchain_backup.sql` beside the project folder.
3. Create and install dependencies into the backend virtual environment from the repository root:

```powershell
py -m venv backend\venv
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

4. Create the database and required tables/indexes without dropping or replacing existing data:

```powershell
backend\venv\Scripts\python.exe backend/setup_mysql.py
```

Before applying a schema upgrade, setup requires the SQL export, creates
versioned snapshots of existing tables, and verifies that each snapshot matches
the live table exactly before recording its digest. On retries it verifies the
snapshots have not changed without requiring live table counts to remain frozen.
Review the snapshots and validate the migration against a restored backup
before production use. The setup command is not run automatically by Flask.

5. Import the bundled synthetic/demo dataset into the separate `demo_ehr_records` table:

```powershell
backend\venv\Scripts\python.exe -m backend.import_demo_dataset
```

The importer validates the exact CSV header, date and numeric formats, and row shape before writing. It reports imported, duplicate/skipped, and invalid rows. Rows are keyed by CSV source filename and source line, and inserts use `INSERT IGNORE`; rerunning the command does not replace records or create duplicates. Invalid rows are reported (up to 20 details) and do not prevent valid rows from importing.

6. Test the database connection. The historical `testmongo.py` filename is retained, but it now checks MySQL:

```powershell
backend\venv\Scripts\python.exe backend/testmongo.py
```

A successful check prints `{'ok': 1}`.

7. Start Flask:

```powershell
backend\venv\Scripts\python.exe backend/app.py
```

8. Create the first administrator from an authorized local shell. The password is prompted and hashed; there is no public Admin or Doctor signup:

```powershell
backend\venv\Scripts\python.exe backend/create_user.py --name "Project Admin" --email admin@example.test --role admin
```

The same utility can create doctor accounts for an authorized administrator/operator. Public `/api/register` always creates patients.

The health endpoint `GET /api/health` pings MySQL and reports `database: connected` or a JSON `503` response. The API runs at `http://127.0.0.1:5000`. Serve `frontend` with a local static server such as VS Code Live Server.

## Password reset

The existing Forgot Password link works for active Doctor and Patient accounts. Reset requests use the same response for existing and unknown email addresses. The API stores only a SHA-256 hash of a cryptographically random, single-use token; links expire after `PASSWORD_RESET_TTL_MINUTES` (30 minutes by default). Run the schema setup command above once to add the `password_reset_tokens` table. This is additive and does not change existing user or record rows.

For email delivery, configure `MAIL_SERVER`, `MAIL_PORT` (STARTTLS, normally `587`), `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`, and `FRONTEND_BASE_URL` in `backend/.env`. These settings are required to link an account signup to an existing doctor-created profile; the account remains unlinked until the email verification link is confirmed. Keep mail credentials out of source control.

For local development only, a reset link can instead be returned to the browser when both `FLASK_DEBUG=true` and `PASSWORD_RESET_DEV_MODE=true` are set. The request must come from the loopback host. Never enable debug or development-link mode in production; use authenticated TLS email delivery there. Set `PASSWORD_RESET_DEV_MODE=false` after local testing.

To complete a reset, open the emailed/development link, enter a new password and confirmation, and submit. The password is stored with Werkzeug hashing; successful use consumes all outstanding reset links for that account.

## API

- `POST /api/register` creates a patient account only; password hashes are stored in `users`.
- `POST /api/register` returns `202` when the submitted name, phone, and email match one unlinked clinic-created patient profile. It sends a one-time email verification link; `POST /api/email-verification/confirm` consumes the token and links the account only after email ownership is verified. Ambiguous matches are rejected rather than linked by email alone.
- `POST /api/patients` requires a doctor token and creates a patient profile and first visit in one transaction. Patient IDs are generated on the server as `EHR-` followed by the zero-padded profile ID.
- `GET /api/patients/search?q=<patient-id-or-name>` and `GET /api/patients/<patient_code>` require a doctor token. Search supports Patient ID or case-insensitive partial name matching; patient history includes all visits newest first by visit date and record ID, with each visit's diagnosis, treatment plan, prescription, doctor, hospital, reports, and additional instructions wherever available.
- `POST /api/patients/<patient_code>/visits` requires a doctor token and creates a separate visit while retaining the patient's permanent ID.
- `POST /api/patients/<patient_code>/visits/<visit_id>/reports` requires a doctor token and accepts up to five PDF/image files per request. Files are signature-checked, limited to 10 MB total and per file, and stored outside MySQL. `GET /api/reports/<report_id>/view` and `/download` require an authenticated doctor or the linked patient account; report metadata and patient/visit links remain in MySQL.
- `GET /api/patient/profile` requires a patient token and returns only that account's linked profile, visits, medicines, follow-up dates, and report metadata.
- `POST /api/patient/profile/link/request` requires the existing patient account's token. It matches a single clinic profile by email, full name, and phone, then sends an email verification link. The profile is linked only when `/api/email-verification/confirm` consumes that one-time token.
- `POST /api/login` accepts email, password, and selected role (`admin`, `doctor`, or `patient`) and returns a JWT.
- `POST /api/password-reset/request` requests reset instructions without confirming whether an email is registered.
- `POST /api/password-reset/confirm` accepts the one-time reset token and new password.
- `GET /api/records` allows doctors to view records and patients to view only records linked to their own patient profile.
- `GET /api/demo-records?page=1&per_page=25` requires a doctor token and returns a paginated, explicitly demo-marked view from `demo_ehr_records`. An optional `search` parameter performs a case-insensitive partial match against the demo record name (or record ID). Demo records are not linked to user accounts and are never exposed through the patient records endpoint.
- `GET /api/doctor-dashboard/records?page=1&per_page=25&search=<name-or-id>` requires a doctor token and returns a paginated, read-only list from `patient_profiles` and `ehr_records` only. Each manually added Patient ID appears once, with the latest visit, its globally unique Record ID, and total visit count; rows are ordered newest visit first. Search is a case-insensitive partial match against patient names, visit IDs, and Patient IDs. The response marks this dashboard feed with `real_ehr_only: true`. Demo dataset rows are not queried or displayed here; clicking a patient opens the complete history for that Patient ID, newest visit first.
- `PUT /api/patients/<patient_code>` requires a doctor token and updates only the patient profile's full name, age, gender, blood group, mobile number, and email. Patient ID and visit, prescription, and report records are not changed.
- `POST /api/records` remains available for legacy doctor-created records tied to an existing patient account. The Add EHR workflow uses `POST /api/patients` or `POST /api/patients/<patient_code>/visits` to keep patient identity separate from visits.
- `PUT /api/records/<record_id>` requires a doctor token.
- `GET /api/admin/doctors` and `POST /api/admin/doctors` require an Admin token. `PATCH /api/admin/doctors/<doctor_id>/status` activates or deactivates a Doctor account.
- `GET /api/health` verifies the database connection.

`setup_mysql.py` creates or upgrades `users`, then `patient_profiles`, then `ehr_records` and their dependent tables, alongside independent `demo_ehr_records` and password-token tables. It does not drop or recreate existing tables. Existing EHR rows are linked by direct user/profile IDs or by a unique match on email, full name, and phone; ambiguous or conflicting rows stop the migration for manual resolution. Existing patient accounts are not automatically linked to unlinked clinic profiles by matching text fields; the authenticated user must complete email verification through the profile-link flow. EHR uploads are stored on disk in Flask's instance `report_uploads` directory by default; set `REPORT_UPLOAD_FOLDER` to a private persistent directory outside the public frontend/web root for deployment. The API never returns a filesystem path. Back up this directory together with MySQL so report references and files remain available after restore. If upgrading the demo table from the earlier `DECIMAL(15,5)` version, it only widens that column to `DECIMAL(19,9)` so every CSV billing value is preserved exactly. Demo rows contain the 15 CSV fields and import metadata, with a unique `(source_file, source_row)` key; the demo table has no account or EHR foreign keys. Existing Atlas data is not copied by this setup; Atlas remains unchanged.
Blockchain verification is implemented with SHA-256 cryptographic chaining in `blockchain_blocks` and `blockchain_state`. The ledger records the Genesis block, new visit creation (`create`), field updates (`update`), and historical baseline registration (`historical_baseline`). The historical baseline audits pre-existing demo records from baseline registration onward without claiming proof of earlier untracked historical integrity. `GET /api/blockchain/verify` (optionally with `?record_id=<id>`) verifies full chain consistency and matches record state hashes.

## Healthcare sample analytics

The public `frontend/healthcare-analytics.html` page reads aggregate analytics from `GET /api/analytics/healthcare`. The endpoint uses Pandas to read `dataset/healthcare_dataset.csv` directly and does not query MySQL, the demo-records table, or actual patient records. It returns aggregates only, not row-level names or clinical records. Install the added Pandas requirement with the backend requirements, start Flask, serve `frontend` with the static server above, and open the **Healthcare Analytics** button on the home page. Date presets use the selected reference date (defaulting to the latest valid sample admission); custom ranges are inclusive.
