"""
scripts/init_db.py

Run this once before starting QCGate for the first time.
Creates the database, all tables, and default config values.
Also creates the initial admin account.

Usage:
    python scripts/init_db.py
"""

import sys
import os

# Allow imports from the project root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from qcgate.database import initialise_database, get_connection
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_admin_account() -> None:
    """
    Prompt for an admin username and password, then create the account.
    Skipped if an admin account already exists.
    """
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM users WHERE role = 'admin'"
    ).fetchone()
    conn.close()

    if existing:
        print("Admin account already exists — skipping.")
        return

    print("\n--- Create Admin Account ---")
    username = input("Admin username: ").strip()
    if not username:
        print("Username cannot be empty. Skipping admin creation.")
        return

    password = input("Admin password: ").strip()
    if not password:
        print("Password cannot be empty. Skipping admin creation.")
        return

    password_hash = pwd_context.hash(password)

    conn = get_connection()
    conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')",
        (username, password_hash)
    )
    conn.commit()
    conn.close()
    print(f"Admin account '{username}' created.")


if __name__ == "__main__":
    print("Initialising QCGate database...")
    initialise_database()
    create_admin_account()
    print("\nDone. You can now start QCGate.")
