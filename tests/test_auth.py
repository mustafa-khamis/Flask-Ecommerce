import re
import unittest
from unittest import mock

from tests.helpers import PASSWORD, AppTestCase, app_module
from functions import add_google_user


def reset_link_from(send_mock):
    link = send_mock.call_args[0][2]
    return re.sub(r"^https?://[^/]+", "", link)


class AuthTests(AppTestCase):
    def test_post_without_csrf_token_is_rejected(self):
        self.make_user("a@example.com")
        response = self.client.post("/login", data={"email": "a@example.com", "password": PASSWORD})
        self.assertEqual(response.status_code, 400)

    def test_login_logout_and_role_redirects(self):
        for email, role, target in [("b@example.com", "Buyer", "/buyer/dashboard"),
                                    ("s@example.com", "Seller", "/seller/dashboard"),
                                    ("ad@example.com", "Admin", "/admin/dashboard")]:
            self.make_user(email, role)
            client = app_module.app.test_client()
            response = self.login(email, client)
            self.assertEqual(response.headers["Location"], target)
            self.assertEqual(client.get(target).status_code, 200)
            self.post("/logout", client=client, token_path=target)
            self.assertEqual(client.get(target).status_code, 302)

    def test_roles_cannot_open_other_dashboards(self):
        self.make_user("b@example.com", "Buyer")
        client = self.client_for("b@example.com")
        self.assertEqual(client.get("/admin/dashboard").status_code, 403)
        self.assertEqual(client.get("/seller/dashboard").status_code, 403)

    def test_anonymous_cannot_open_dashboards(self):
        for path in ("/admin/dashboard", "/seller/dashboard", "/buyer/dashboard", "/profile"):
            self.assertEqual(self.client.get(path).status_code, 302, path)

    def test_login_is_throttled_after_repeated_failures(self):
        self.make_user("a@example.com")
        for _ in range(10):
            self.assertEqual(self.post("/login", {"email": "a@example.com", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.login("a@example.com").status_code, 429)

    def test_weak_passwords_rejected_at_registration(self):
        for weak in ("short1", "alllettersonly", "12345678"):
            response = self.post("/register", {"name": "New User", "email": "n@example.com", "password": weak,
                                               "confirmation": weak, "role": "Buyer"}, token_path="/register")
            self.assertEqual(response.status_code, 400, weak)


class PasswordResetTests(AppTestCase):
    def request_reset(self, email, client=None):
        return self.post("/forgot-password", {"email": email}, client=client, token_path="/forgot-password")

    def test_full_reset_flow_and_token_cannot_be_reused(self):
        self.make_user("a@example.com")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.request_reset("a@example.com")
        link = reset_link_from(sent)
        self.assertEqual(self.client.get(link).status_code, 200)

        new_password = "BrandNew456"
        response = self.post(link, {"password": new_password, "confirmation": new_password}, token_path=link)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.post("/login", {"email": "a@example.com", "password": new_password}).status_code, 302)
        self.assertEqual(self.login("a@example.com").status_code, 401)  # old password no longer works

        self.assertEqual(self.client.get(link).status_code, 400)  # single use
        again = self.post(link, {"password": "Another789x", "confirmation": "Another789x"}, token_path="/login")
        self.assertEqual(again.status_code, 400)

    def test_expired_token_is_rejected(self):
        self.make_user("a@example.com")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.request_reset("a@example.com")
        link = reset_link_from(sent)
        self.execute("UPDATE password_resets SET expires_at = datetime('now', '-1 minute')")
        self.assertEqual(self.client.get(link).status_code, 400)
        response = self.post(link, {"password": "BrandNew456", "confirmation": "BrandNew456"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.login("a@example.com").status_code, 302)  # password unchanged

    def test_new_request_invalidates_older_link(self):
        self.make_user("a@example.com")
        links = []
        for _ in range(2):
            with mock.patch("app.send_reset_email", return_value=True) as sent:
                self.request_reset("a@example.com")
            links.append(reset_link_from(sent))
        self.assertEqual(self.client.get(links[0]).status_code, 400)
        self.assertEqual(self.client.get(links[1]).status_code, 200)

    def test_token_is_stored_hashed(self):
        self.make_user("a@example.com")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.request_reset("a@example.com")
        token = reset_link_from(sent).rsplit("/", 1)[1]
        stored = self.query("SELECT token_hash FROM password_resets")[0][0]
        self.assertNotEqual(stored, token)

    def test_response_is_identical_for_unknown_email(self):
        self.make_user("a@example.com")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            known = self.request_reset("a@example.com", client=app_module.app.test_client())
            unknown = self.request_reset("nobody@example.com", client=app_module.app.test_client())
        self.assertEqual(sent.call_count, 1)
        self.assertEqual((known.status_code, known.headers["Location"]), (unknown.status_code, unknown.headers["Location"]))
        page_known = self.client.get("/forgot-password").status_code
        self.assertEqual(page_known, 200)

    def test_requests_are_throttled_but_look_the_same(self):
        self.make_user("a@example.com")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            for _ in range(6):
                response = self.request_reset("a@example.com")
                self.assertEqual(response.status_code, 302)
        self.assertEqual(sent.call_count, 3)

    def test_inactive_account_gets_no_email(self):
        self.make_user("a@example.com")
        self.execute("UPDATE users SET is_active = 0")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.request_reset("a@example.com")
        sent.assert_not_called()

    def test_password_rules_apply_on_reset(self):
        self.make_user("a@example.com")
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.request_reset("a@example.com")
        link = reset_link_from(sent)
        self.assertEqual(self.post(link, {"password": "short", "confirmation": "short"}, token_path=link).status_code, 400)
        self.assertEqual(self.post(link, {"password": "Passw0rd123", "confirmation": "Different123"}, token_path=link).status_code, 400)
        self.assertEqual(self.client.get(link).status_code, 200)  # still valid after failed attempts

    def test_google_only_account_can_set_password_via_reset(self):
        add_google_user("Gina", "g@example.com", "Buyer")
        self.assertEqual(self.query("SELECT has_password FROM users")[0][0], 0)
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.request_reset("g@example.com")
        link = reset_link_from(sent)
        self.post(link, {"password": "BrandNew456", "confirmation": "BrandNew456"}, token_path=link)
        self.assertEqual(self.query("SELECT has_password FROM users")[0][0], 1)
        self.assertEqual(self.post("/login", {"email": "g@example.com", "password": "BrandNew456"}).status_code, 302)

    def test_google_only_account_can_set_password_from_profile_without_current_one(self):
        add_google_user("Gina", "g@example.com", "Buyer")
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["user_id"] = self.query("SELECT id FROM users")[0][0]
        page = client.get("/profile").get_data(as_text=True)
        self.assertIn("Set a password", page)
        self.assertNotIn('name="current_password"', page)
        self.post("/profile/password", {"new_password": "BrandNew456", "confirmation": "BrandNew456"},
                  client=client, token_path="/profile")
        self.assertEqual(self.query("SELECT has_password FROM users")[0][0], 1)


class ResendTests(AppTestCase):
    def test_email_payload_sent_to_resend(self):
        import emailer
        with mock.patch("emailer.urllib.request.urlopen") as opened:
            self.assertTrue(emailer.send_email("a@example.com", "Hi", "text", "<p>html</p>"))
        request = opened.call_args[0][0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.get_header("Authorization"), "Bearer re_test")

    def test_failure_is_reported_not_raised(self):
        import emailer, urllib.error
        with mock.patch("emailer.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            self.assertFalse(emailer.send_email("a@example.com", "Hi", "t", "h"))


if __name__ == "__main__":
    unittest.main()
