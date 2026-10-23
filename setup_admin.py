"""Create the first Admin account, or make sure an existing account is an active Admin.

Safe to run more than once. Nothing is hardcoded: the details come from
environment variables (or you are prompted for anything that is missing).

    ADMIN_EMAIL='you@example.com' ADMIN_NAME='Your Name' ADMIN_PASSWORD='a-strong-secret' python3 setup_admin.py

If the email already exists, the account is promoted to Admin and re-activated.
Its password is only replaced when you also pass RESET_ADMIN_PASSWORD=1.
"""
import getpass
import os
import sqlite3

from werkzeug.security import generate_password_hash

from config import DATABASE
from functions import init_db, password_error, valid_email


def main():
    init_db()
    email = (os.environ.get("ADMIN_EMAIL") or input("Admin email: ")).strip().lower()
    if not valid_email(email):
        raise SystemExit("Please provide a valid ADMIN_EMAIL.")

    connection = sqlite3.connect(DATABASE)
    try:
        existing = connection.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()

        if existing:
            connection.execute(
                "UPDATE users SET role = 'Admin', is_active = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (existing[0],),
            )
            if os.environ.get("RESET_ADMIN_PASSWORD") == "1":
                password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("New admin password: ")
                if password_error(password):
                    raise SystemExit(password_error(password))
                connection.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(password), existing[0]),
                )
            connection.commit()
            print(f"{email} already existed and is now an active Admin.")
            return

        name = (os.environ.get("ADMIN_NAME") or input("Admin name: ")).strip()
        password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
        if len(name) < 2:
            raise SystemExit("Please provide an ADMIN_NAME of at least 2 characters.")
        if password_error(password):
            raise SystemExit(password_error(password))
        connection.execute(
            "INSERT INTO users (name, email, password_hash, role, has_password) VALUES (?, ?, ?, 'Admin', 1)",
            (name, email, generate_password_hash(password)),
        )
        connection.commit()
        print(f"Admin account created for {email}.")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
