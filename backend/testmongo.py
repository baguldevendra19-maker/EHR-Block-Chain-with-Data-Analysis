
"""Test MySQL connectivity; the original filename is retained for compatibility."""

import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

from app import app, fetch_one
from mysql.connector.errors import Error as MySQLError


if __name__ == "__main__":
    try:
        with app.app_context():
            result = fetch_one("SELECT 1 AS ok")
            print({"ok": result["ok"]})
            print("MySQL connection successful!")
    except RuntimeError as error:
        print(error)
        sys.exit(1)
    except MySQLError as error:
        print(type(error).__name__)
        sys.exit(1)
