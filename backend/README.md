# EHR Secure backend

## MySQL setup

1. Start your local MySQL Server and open MySQL Workbench if you want to inspect the database there.
2. Set `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, and `MYSQL_PASSWORD` in `backend/.env`. Keep the password only in `.env`; do not commit or print that file. The example values are in `.env.example`.
3. Create and install dependencies into the backend virtual environment from the repository root:

```powershell
py -m venv backend\venv
backend\venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

4. Create the database and required tables/indexes without dropping or replacing existing data:

```powershell
backend\venv\Scripts\python.exe backend/setup_mysql.py
```

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

For email delivery, configure `MAIL_SERVER`, `MAIL_PORT` (STARTTLS, normally `587`), `MAIL_USERNAME`, `MAIL_PASSWORD`, `MAIL_FROM`, and `FRONTEND_BASE_URL` in `backend/.env`. Keep mail credentials out of source control.

For local development only, a reset link can instead be returned to the browser when both `FLASK_DEBUG=true` and `PASSWORD_RESET_DEV_MODE=true` are set. The request must come from the loopback host. Never enable debug or development-link mode in production; use authenticated TLS email delivery there. Set `PASSWORD_RESET_DEV_MODE=false` after local testing.

To complete a reset, open the emailed/development link, enter a new password and confirmation, and submit. The password is stored with Werkzeug hashing; successful use consumes all outstanding reset links for that account.

## API

- `POST /api/register` creates a patient account only; password hashes are stored in `users`.
- `POST /api/login` accepts email, password, and selected role (`admin`, `doctor`, or `patient`) and returns a JWT.
- `POST /api/password-reset/request` requests reset instructions without confirming whether an email is registered.
- `POST /api/password-reset/confirm` accepts the one-time reset token and new password.
- `GET /api/records` allows doctors to view all records and patients to view only records matching their own user ID.
- `GET /api/demo-records?page=1&per_page=25` requires a doctor token and returns a paginated, explicitly demo-marked view from `demo_ehr_records`. An optional `search` parameter performs a case-insensitive partial match against the demo record name (or record ID). Demo records are not linked to user accounts and are never exposed through the patient records endpoint.
- `GET /api/doctor-dashboard/records?page=1&per_page=25&search=<name>` requires a doctor token and returns one paginated, read-only view of both demo dataset rows and manually added EHR rows. Search is a case-insensitive partial patient-name match and the `source_type` field identifies each row. The sources remain separate in the database.
- `POST /api/records` and `PUT /api/records/<record_id>` require a doctor token.
- `GET /api/admin/doctors` and `POST /api/admin/doctors` require an Admin token. `PATCH /api/admin/doctors/<doctor_id>/status` activates or deactivates a Doctor account.
- `GET /api/health` verifies the database connection.

`setup_mysql.py` creates the `users`, `ehr_records`, independent `demo_ehr_records`, and `password_reset_tokens` tables. It uses `CREATE DATABASE/TABLE IF NOT EXISTS` and does not delete records. If upgrading the demo table from the earlier `DECIMAL(15,5)` version, it only widens that column to `DECIMAL(19,9)` so every CSV billing value is preserved exactly. Demo rows contain the 15 CSV fields and import metadata, with a unique `(source_file, source_row)` key; the demo table has no account or EHR foreign keys. Existing Atlas data is not copied by this setup; Atlas remains unchanged.

Blockchain verification and Data Analysis are not implemented in this backend phase.
