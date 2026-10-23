"""Shared test setup: every test run gets its own empty database and upload folder."""
import os
import re
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="lumamarket-tests-")
os.environ["ECOMMERCE_DATABASE"] = os.path.join(_tmp, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["RESEND_API_KEY"] = "re_test"
os.environ["RESEND_FROM_EMAIL"] = "Test <test@example.com>"
os.environ["OPENWEATHER_API_KEY"] = "owm_test"
os.environ.pop("GOOGLE_CLIENT_ID", None)

import app as app_module  # noqa: E402
from config import DATABASE  # noqa: E402
from functions import add_user, connect_db, init_db  # noqa: E402

PASSWORD = "Passw0rd123"


def wipe_database():
    connection = connect_db()
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        for (name,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
            connection.execute(f"DROP TABLE {name}")
        connection.commit()
    finally:
        connection.close()
    init_db()


class AppTestCase(unittest.TestCase):
    def setUp(self):
        wipe_database()
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()

    # -- helpers
    def token(self, client=None, path="/login"):
        page = (client or self.client).get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def post(self, path, data=None, client=None, follow=False, token_path="/login", **kw):
        client = client or self.client
        data = dict(data or {})
        data.setdefault("csrf_token", self.token(client, token_path))
        return client.post(path, data=data, follow_redirects=follow, **kw)

    def make_user(self, email, role="Buyer", name="Test User"):
        add_user(name, email, PASSWORD, role)
        return email

    def login(self, email, client=None):
        return self.post("/login", {"email": email, "password": PASSWORD}, client=client)

    def client_for(self, email):
        client = app_module.app.test_client()
        response = self.login(email, client=client)
        assert response.status_code == 302, response.status_code
        return client

    def query(self, sql, params=()):
        connection = connect_db()
        try:
            return connection.execute(sql, params).fetchall()
        finally:
            connection.close()

    def execute(self, sql, params=()):
        connection = connect_db()
        try:
            connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()
