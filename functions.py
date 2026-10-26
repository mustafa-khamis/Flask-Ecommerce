import hashlib
import hmac
import re
import sqlite3
from functools import wraps
import secrets
from pathlib import Path

from flask import abort, g, redirect, request, session, url_for
from werkzeug.security import generate_password_hash

from config import CODE_MAX_ATTEMPTS, DATABASE, SECRET_KEY

ROLES = ("Admin", "Seller", "Buyer")


def connect_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db():
    Path(DATABASE).parent.mkdir(parents=True, exist_ok=True)
    connection = connect_db()
    try:
        old_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if old_table:
            columns = [row["name"] for row in connection.execute("PRAGMA table_info(users)")]
            if "password_hash" not in columns:
                # Convert the original student schema once, without deleting its users.
                connection.execute("ALTER TABLE users RENAME TO old_users")
                create_users_table(connection)
                rows = connection.execute(
                    "SELECT id, name, email, password, role FROM old_users"
                ).fetchall()
                for row in rows:
                    role = row["role"] if row["role"] in ROLES else "Buyer"
                    connection.execute(
                        """INSERT INTO users
                           (id, name, email, password_hash, role)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            row["id"], row["name"], row["email"].lower(),
                            generate_password_hash(row["password"]), role,
                        ),
                    )
                connection.execute("DROP TABLE old_users")
        else:
            create_users_table(connection)
        create_other_tables(connection)
        upgrade_products(connection)
        connection.commit()
    finally:
        connection.close()


def create_users_table(connection):
    connection.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            has_password INTEGER NOT NULL DEFAULT 1,
            role TEXT NOT NULL CHECK (role IN ('Admin', 'Seller', 'Buyer')),
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def create_other_tables(connection):
    """Add everything after Stage 1. Safe to run on every start: nothing is dropped or rewritten."""
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(users)")]
    if "email_verified" not in columns:
        # 1 for everyone who already exists, so nobody is locked out by this feature.
        connection.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
    if "has_password" not in columns:
        # 0 means the account was created with Google and has no password of its own yet.
        connection.execute("ALTER TABLE users ADD COLUMN has_password INTEGER NOT NULL DEFAULT 1")

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- 6-digit confirmation codes. purpose is 'signup' or 'email_change' (email = the address to confirm).
        CREATE TABLE IF NOT EXISTS email_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose TEXT NOT NULL CHECK (purpose IN ('signup', 'email_change')),
            email TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_email_codes_user ON email_codes(user_id, created_at);

        -- One row per attempt (kind = 'reset' or 'login'); used to rate-limit.
        CREATE TABLE IF NOT EXISTS throttle (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            key TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_throttle ON throttle(kind, key, created_at);

        -- Prices are whole cents. sale_price_cents is optional and must be below price_cents.
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL REFERENCES users(id),
            name TEXT NOT NULL CHECK (length(name) BETWEEN 2 AND 120),
            description TEXT NOT NULL DEFAULT '',
            price_cents INTEGER NOT NULL CHECK (price_cents > 0),
            sale_price_cents INTEGER CHECK (sale_price_cents IS NULL OR
                                            (sale_price_cents > 0 AND sale_price_cents < price_cents)),
            sale_start TEXT,
            sale_end TEXT CHECK (sale_end IS NULL OR sale_start IS NULL OR sale_end >= sale_start),
            stock INTEGER NOT NULL DEFAULT 0 CHECK (stock >= 0),
            image TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_products_seller ON products(seller_id);

        -- Every picture of a product; products.image always mirrors the first one (the cover).
        CREATE TABLE IF NOT EXISTS product_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            position INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_product_images ON product_images(product_id, position);

        CREATE TABLE IF NOT EXISTS cart_items (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            PRIMARY KEY (user_id, product_id)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id INTEGER NOT NULL REFERENCES users(id),
            total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
            status TEXT NOT NULL DEFAULT 'Confirmed',
            shipping_name TEXT NOT NULL,
            shipping_address TEXT NOT NULL,
            payment_method TEXT NOT NULL DEFAULT 'Simulated payment',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        -- Name and price are copied at purchase time so old orders never change.
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_id INTEGER NOT NULL REFERENCES products(id),
            seller_id INTEGER NOT NULL REFERENCES users(id),
            product_name TEXT NOT NULL,
            unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents > 0),
            quantity INTEGER NOT NULL CHECK (quantity > 0)
        );

        CREATE TABLE IF NOT EXISTS weather_searches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            search_type TEXT NOT NULL CHECK (search_type IN ('city', 'coordinates')),
            query TEXT NOT NULL,
            place TEXT,
            country TEXT,
            temperature REAL,
            condition TEXT,
            success INTEGER NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def upgrade_products(connection):
    """Columns and data added after the first shop release (safe to repeat)."""
    columns = [row["name"] for row in connection.execute("PRAGMA table_info(products)")]
    if "free_shipping" not in columns:  # set by the seller
        connection.execute("ALTER TABLE products ADD COLUMN free_shipping INTEGER NOT NULL DEFAULT 0")
    if "is_choice" not in columns:  # set by an Admin only
        connection.execute("ALTER TABLE products ADD COLUMN is_choice INTEGER NOT NULL DEFAULT 0")
    # Products that had a single image keep it as their first gallery picture.
    connection.execute(
        """INSERT INTO product_images (product_id, filename, position)
           SELECT id, image, 0 FROM products
           WHERE image IS NOT NULL AND id NOT IN (SELECT product_id FROM product_images)""")


def password_error(password):
    """Return a message when the password is not acceptable, otherwise None."""
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if len(password) > 128:
        return "Password must be 128 characters or fewer."
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        return "Password must contain at least one letter and one number."
    return None


def valid_email(email):
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def get_user_by_email(email):
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT * FROM users WHERE email = ?", (email.lower(),)
        ).fetchone()
    finally:
        connection.close()


def get_user_by_id(user_id):
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    finally:
        connection.close()


def add_user(name, email, password, role, has_password=1, email_verified=1):
    """Create an account and return its id. Public sign-ups pass email_verified=0 until the code is entered."""
    connection = connect_db()
    try:
        cursor = connection.execute(
            """INSERT INTO users (name, email, password_hash, role, has_password, email_verified)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, email.lower(), generate_password_hash(password), role, has_password, email_verified),
        )
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def get_users(search="", role=""):
    query = "SELECT id, name, email, role, is_active, created_at FROM users WHERE 1 = 1"
    params = []
    if search:
        query += " AND (name LIKE ? OR email LIKE ?)"
        params += [f"%{search}%", f"%{search}%"]
    if role in ROLES:
        query += " AND role = ?"
        params.append(role)
    query += " ORDER BY created_at DESC"
    connection = connect_db()
    try:
        return connection.execute(query, params).fetchall()
    finally:
        connection.close()


def update_profile(user_id, name, email):
    connection = connect_db()
    try:
        connection.execute(
            """UPDATE users SET name = ?, email = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (name, email.lower(), user_id),
        )
        connection.commit()
    finally:
        connection.close()


def update_password(user_id, password):
    connection = connect_db()
    try:
        connection.execute(
            """UPDATE users SET password_hash = ?, has_password = 1, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (generate_password_hash(password), user_id),
        )
        connection.commit()
    finally:
        connection.close()


def update_user(user_id, role, is_active):
    connection = connect_db()
    try:
        connection.execute(
            "UPDATE users SET role = ?, is_active = ? WHERE id = ?",
            (role, is_active, user_id),
        )
        connection.commit()
    finally:
        connection.close()


def count_users(role=None, is_active=None):
    query = "SELECT COUNT(*) FROM users WHERE 1 = 1"
    params = []
    if role:
        query += " AND role = ?"
        params.append(role)
    if is_active is not None:
        query += " AND is_active = ?"
        params.append(1 if is_active else 0)
    connection = connect_db()
    try:
        return connection.execute(query, params).fetchone()[0]
    finally:
        connection.close()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_user_by_id(session.get("user_id"))
        if user is None or not user["is_active"] or not user["email_verified"]:
            session.pop("user_id", None)  # keep verify_user_id: someone mid-confirmation stays mid-confirmation
            return redirect(url_for("login"))
        g.user = user
        return view(*args, **kwargs)
    return wrapped


def role_required(role):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] != role:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def add_google_user(name, email, role):
    """Create an account for someone who signed up with Google.
    They have no password, so a random unusable one is stored (has_password = 0);
    they can set a real one from their profile or through "Forgot password"."""
    add_user(name, email, secrets.token_urlsafe(32), role, has_password=0)


# --- Rate limiting -----------------------------------------------------------
def throttle_count(kind, key, minutes):
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM throttle WHERE kind = ? AND key = ? AND created_at > datetime('now', ?)",
            (kind, key, f"-{minutes} minutes"),
        ).fetchone()[0]
    finally:
        connection.close()


def throttle_record(kind, key):
    connection = connect_db()
    try:
        connection.execute("INSERT INTO throttle (kind, key) VALUES (?, ?)", (kind, key))
        connection.execute("DELETE FROM throttle WHERE created_at < datetime('now', '-1 day')")
        connection.commit()
    finally:
        connection.close()


def throttle_clear(kind, key):
    connection = connect_db()
    try:
        connection.execute("DELETE FROM throttle WHERE kind = ? AND key = ?", (kind, key))
        connection.commit()
    finally:
        connection.close()


# --- Password reset tokens ---------------------------------------------------
# Only a SHA-256 hash of the token is stored, so a copy of the database cannot be used to reset accounts.
def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_reset_token(user_id, minutes):
    token = secrets.token_urlsafe(32)
    connection = connect_db()
    try:
        # A new request cancels any older link that has not been used.
        connection.execute(
            "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE user_id = ? AND used_at IS NULL",
            (user_id,),
        )
        connection.execute(
            """INSERT INTO password_resets (user_id, token_hash, expires_at)
               VALUES (?, ?, datetime('now', ?))""",
            (user_id, hash_token(token), f"+{minutes} minutes"),
        )
        connection.commit()
    finally:
        connection.close()
    return token


def find_valid_reset(token):
    """Return the reset row if the token exists, is unused and has not expired."""
    connection = connect_db()
    try:
        return connection.execute(
            """SELECT pr.*, u.is_active FROM password_resets pr
               JOIN users u ON u.id = pr.user_id
               WHERE pr.token_hash = ? AND pr.used_at IS NULL AND pr.expires_at > datetime('now')""",
            (hash_token(token),),
        ).fetchone()
    finally:
        connection.close()


def use_reset_token(token, new_password):
    """Set the new password and burn the token in one transaction. Returns False if the token is no longer valid."""
    connection = connect_db()
    try:
        row = connection.execute(
            """SELECT id, user_id FROM password_resets
               WHERE token_hash = ? AND used_at IS NULL AND expires_at > datetime('now')""",
            (hash_token(token),),
        ).fetchone()
        if row is None:
            return False
        # The "used_at IS NULL" condition makes sure two simultaneous requests cannot both succeed.
        claimed = connection.execute(
            "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE id = ? AND used_at IS NULL",
            (row["id"],),
        )
        if claimed.rowcount != 1:
            connection.rollback()
            return False
        connection.execute(
            """UPDATE users SET password_hash = ?, has_password = 1, email_verified = 1,
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",  # opening the emailed link proves the address
            (generate_password_hash(new_password), row["user_id"]),
        )
        connection.execute(
            "UPDATE password_resets SET used_at = CURRENT_TIMESTAMP WHERE user_id = ? AND used_at IS NULL",
            (row["user_id"],),
        )
        connection.commit()
        return True
    finally:
        connection.close()


# --- Email confirmation codes ------------------------------------------------------
# Only an HMAC of the code is stored (keyed with SECRET_KEY), never the code itself.
def hash_code(user_id, purpose, code):
    return hmac.new(SECRET_KEY.encode(), f"{purpose}:{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


def create_email_code(user_id, purpose, email, minutes):
    """Make a fresh 6-digit code for this person; any older unused code stops working."""
    code = f"{secrets.randbelow(10**6):06d}"
    connection = connect_db()
    try:
        connection.execute("UPDATE email_codes SET used_at = CURRENT_TIMESTAMP WHERE user_id = ? AND used_at IS NULL",
                           (user_id,))
        connection.execute(
            """INSERT INTO email_codes (user_id, purpose, email, code_hash, expires_at)
               VALUES (?, ?, ?, ?, datetime('now', ?))""",
            (user_id, purpose, email.lower(), hash_code(user_id, purpose, code), f"+{minutes} minutes"))
        connection.commit()
    finally:
        connection.close()
    return code


def pending_code(user_id):
    """The newest unused code row (even if expired), or None."""
    connection = connect_db()
    try:
        return connection.execute(
            """SELECT *, expires_at <= datetime('now') AS expired,
                      CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_seconds
               FROM email_codes WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1""",
            (user_id,)).fetchone()
    finally:
        connection.close()


def seconds_since_last_code(user_id):
    connection = connect_db()
    try:
        row = connection.execute(
            """SELECT CAST((julianday('now') - julianday(MAX(created_at))) * 86400 AS INTEGER)
               FROM email_codes WHERE user_id = ?""", (user_id,)).fetchone()
        return row[0]  # None when no code was ever sent
    finally:
        connection.close()


def check_email_code(user_id, submitted):
    """Check a typed code. Returns (status, info) with status one of:
    ok (info = the code row), wrong (info = tries left), expired, locked, none.
    The attempt counter is updated in the same transaction, so guesses cannot be raced."""
    submitted = submitted.strip()
    connection = connect_db()
    connection.isolation_level = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT *, expires_at <= datetime('now') AS expired FROM email_codes
               WHERE user_id = ? AND used_at IS NULL ORDER BY id DESC LIMIT 1""", (user_id,)).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return "none", None
        if row["expired"]:
            connection.execute("COMMIT")
            return "expired", None
        if row["attempts"] >= CODE_MAX_ATTEMPTS:
            connection.execute("COMMIT")
            return "locked", None
        expected = row["code_hash"]
        if re.fullmatch(r"\d{6}", submitted) and hmac.compare_digest(
                expected, hash_code(user_id, row["purpose"], submitted)):
            connection.execute("UPDATE email_codes SET used_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
            connection.execute("COMMIT")
            return "ok", row
        connection.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE id = ?", (row["id"],))
        connection.execute("COMMIT")
        left = CODE_MAX_ATTEMPTS - row["attempts"] - 1
        return ("wrong", left) if left > 0 else ("locked", None)
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def mark_email_verified(user_id):
    connection = connect_db()
    try:
        connection.execute("UPDATE users SET email_verified = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (user_id,))
        connection.commit()
    finally:
        connection.close()


def change_email(user_id, new_email):
    """Switch the account to a confirmed new address. Returns False if somebody else already uses it."""
    connection = connect_db()
    try:
        connection.execute("UPDATE users SET email = ?, email_verified = 1, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                           (new_email.lower(), user_id))
        connection.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        connection.close()


def remove_unconfirmed_account(email):
    """Free an address that someone signed up with but never confirmed (such accounts cannot sign in)."""
    connection = connect_db()
    try:
        connection.execute("DELETE FROM users WHERE email = ? AND email_verified = 0", (email.lower(),))
        connection.commit()
    finally:
        connection.close()


def take_over_unconfirmed_for_google(user_id):
    """Google just proved this address, so an unconfirmed password account on it is not trusted:
    confirm it, but throw away whatever password the (unproven) sign-up chose."""
    connection = connect_db()
    try:
        connection.execute(
            """UPDATE users SET email_verified = 1, has_password = 0, password_hash = ?,
                      updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (generate_password_hash(secrets.token_urlsafe(32)), user_id))
        connection.commit()
    finally:
        connection.close()
