"""Create an admin, doctor, or patient account from an interactive shell."""

import argparse
import getpass
from datetime import datetime, timezone

from mysql.connector.errors import Error as MySQLError, IntegrityError
from werkzeug.security import generate_password_hash

from app import app, insert_user


def main():
    parser = argparse.ArgumentParser(description="Create an EHR Secure account")
    parser.add_argument("--name", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--role", choices=("admin", "doctor", "patient"), required=True)
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    if len(password) < 8:
        parser.error("Password must be at least 8 characters.")

    with app.app_context():
        try:
            insert_user({
                "name": args.name.strip(),
                "email": args.email.strip().lower(),
                "password_hash": generate_password_hash(password),
                "role": args.role,
                "status": "active",
                "created_at": datetime.now(timezone.utc),
            })
        except IntegrityError as error:
            if error.errno == 1062:
                parser.error("An account with this email already exists.")
            parser.error(f"Unable to create account in MySQL ({type(error).__name__}).")
        except MySQLError as error:
            parser.error(f"Unable to create account in MySQL ({type(error).__name__}).")
    print(f"Created {args.role} account: {args.email.strip().lower()}")


if __name__ == "__main__":
    main()