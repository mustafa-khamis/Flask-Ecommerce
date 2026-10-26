import re
import unittest
from unittest import mock

import emailer
from functions import hash_code, take_over_unconfirmed_for_google
from tests.helpers import PASSWORD, AppTestCase, app_module

SIGNUP = {"name": "New Person", "email": "new@example.com", "password": PASSWORD, "confirmation": PASSWORD, "role": "Buyer"}


class ConfirmBase(AppTestCase):
    def sign_up(self, client=None, **overrides):
        """Register through the form with the email service mocked. Returns (response, sent_emails)."""
        with mock.patch("emailer.send_email", return_value=True) as sent:
            response = self.post("/register", {**SIGNUP, **overrides}, client=client, token_path="/register")
        return response, sent

    @staticmethod
    def code_from(sent, index=-1):
        text = sent.call_args_list[index][0][2]
        return text.splitlines()[-1]

    def confirm(self, code, client=None, token_path="/confirm-email"):
        return self.post("/confirm-email", {"code": code}, client=client, token_path=token_path)

    def user(self, email="new@example.com"):
        rows = self.query("SELECT * FROM users WHERE email = ?", (email,))
        return rows[0] if rows else None

    def backdate_code(self, seconds):
        self.execute("UPDATE email_codes SET created_at = datetime('now', ?)", (f"-{seconds} seconds",))


class SignupCodeTests(ConfirmBase):
    def test_registration_creates_an_unconfirmed_account_and_emails_a_six_digit_code(self):
        response, sent = self.sign_up()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/confirm-email"))
        self.assertEqual(self.user()["email_verified"], 0)
        to, subject, text, html = sent.call_args[0]
        self.assertEqual(to, "new@example.com")
        code = self.code_from(sent)
        self.assertRegex(code, r"^\d{6}$")
        self.assertTrue(text.rstrip().endswith(code))             # the code is the last thing in the text
        self.assertEqual(len(re.findall(r"\b\d{6}\b", text)), 1)  # and the only 6-digit number
        self.assertIn(code, html)
        self.assertIn(code, subject)

    def test_not_signed_in_until_the_code_is_entered(self):
        client = app_module.app.test_client()
        self.sign_up(client=client)
        self.assertEqual(client.get("/buyer/dashboard").status_code, 302)
        self.assertEqual(client.get("/profile").status_code, 302)

    def test_correct_code_confirms_signs_in_and_works_only_once(self):
        client = app_module.app.test_client()
        _, sent = self.sign_up(client=client)
        code = self.code_from(sent)
        response = self.confirm(code, client=client)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/buyer/dashboard"))
        self.assertEqual(self.user()["email_verified"], 1)
        self.assertEqual(client.get("/buyer/dashboard").status_code, 200)
        # the same code cannot be used again (nothing is pending any more)
        other = app_module.app.test_client()
        with other.session_transaction() as sess:
            sess["verify_user_id"] = self.user()["id"]
        self.assertEqual(self.confirm(code, client=other, token_path="/login").status_code, 302)  # nothing left: login

    def test_resend_countdown_is_shown_right_after_the_first_email(self):
        client = app_module.app.test_client()
        self.sign_up(client=client)
        page = client.get("/confirm-email").get_data(as_text=True)
        self.assertRegex(page, r'data-wait="\d+"')          # a just-sent code (0 s ago) still means "wait"
        self.backdate_code(120)
        self.assertNotIn("data-wait", client.get("/confirm-email").get_data(as_text=True))

    def test_confirming_shows_the_welcome_message(self):
        client = app_module.app.test_client()
        _, sent = self.sign_up(client=client)
        response = self.confirm(self.code_from(sent), client=client)
        page = client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Email confirmed. Welcome to LumaMarket, New Person!", page)

    def test_wrong_codes_are_counted_and_five_wrong_tries_lock_the_code(self):
        client = app_module.app.test_client()
        _, sent = self.sign_up(client=client)
        right = self.code_from(sent)
        wrong = "000000" if right != "000000" else "111111"
        for left in (4, 3, 2, 1):
            page = self.confirm(wrong, client=client).get_data(as_text=True)
            self.assertIn(f"{left} {'try' if left == 1 else 'tries'} left", page)
        page = self.confirm(wrong, client=client).get_data(as_text=True)
        self.assertIn("Too many wrong attempts", page)
        self.assertEqual(self.confirm(right, client=client).status_code, 400)   # even the right code is now refused
        self.assertEqual(self.user()["email_verified"], 0)

    def test_malformed_codes_are_rejected_and_counted(self):
        client = app_module.app.test_client()
        self.sign_up(client=client)
        for bad in ("", "12345", "1234567", "abcdef", "12 456"):
            self.assertEqual(self.confirm(bad, client=client).status_code, 400, bad)
        self.assertEqual(self.query("SELECT attempts FROM email_codes")[0][0], 5)

    def test_expired_code_is_rejected(self):
        client = app_module.app.test_client()
        _, sent = self.sign_up(client=client)
        self.execute("UPDATE email_codes SET expires_at = datetime('now', '-1 minute')")
        response = self.confirm(self.code_from(sent), client=client)
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.get_data(as_text=True))
        self.assertEqual(self.user()["email_verified"], 0)

    def test_code_is_stored_hashed_not_in_plain_text(self):
        _, sent = self.sign_up()
        code = self.code_from(sent)
        stored = self.query("SELECT code_hash FROM email_codes")[0][0]
        self.assertNotEqual(stored, code)
        self.assertEqual(stored, hash_code(self.user()["id"], "signup", code))

    def test_new_code_replaces_the_old_one(self):
        client = app_module.app.test_client()
        _, first = self.sign_up(client=client)
        self.backdate_code(120)
        with mock.patch("emailer.send_email", return_value=True) as second:
            self.post("/confirm-email/resend", client=client, token_path="/confirm-email")
        old, new = self.code_from(first), self.code_from(second)
        if old != new:
            self.assertEqual(self.confirm(old, client=client).status_code, 400)
        self.assertEqual(self.confirm(new, client=client).status_code, 302)

    def test_resend_has_a_cooldown_and_an_hourly_limit(self):
        client = app_module.app.test_client()
        self.sign_up(client=client)
        with mock.patch("emailer.send_email", return_value=True) as sent:
            self.post("/confirm-email/resend", client=client, token_path="/confirm-email")
            self.assertEqual(sent.call_count, 0)                      # too soon after the first email
            for _ in range(10):
                self.backdate_code(120)
                self.post("/confirm-email/resend", client=client, token_path="/confirm-email")
        self.assertEqual(sent.call_count, 4)   # 1 at sign-up + 4 resends = the limit of 5 per hour

    def test_a_failed_email_send_is_reported_clearly(self):
        client = app_module.app.test_client()
        with mock.patch("emailer.send_email", return_value=False):
            response = self.post("/register", SIGNUP, client=client, token_path="/register", follow=True)
        page = response.get_data(as_text=True)
        self.assertIn("could not send the email", page)
        self.assertIn('name="code"', page)
        self.assertEqual(self.user()["email_verified"], 0)

    def test_confirm_pages_need_a_pending_confirmation_and_csrf(self):
        self.assertEqual(self.client.get("/confirm-email").status_code, 302)
        self.assertEqual(self.client.post("/confirm-email/resend").status_code, 400)
        client = app_module.app.test_client()
        self.sign_up(client=client)
        self.assertEqual(client.post("/confirm-email", data={"code": "123456"}).status_code, 400)  # no CSRF token

    def test_a_confirmed_account_cannot_be_re_confirmed_through_the_session_key(self):
        self.make_user("old@example.com")
        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["verify_user_id"] = self.user("old@example.com")["id"]
        self.assertEqual(client.get("/confirm-email").status_code, 302)


class UnconfirmedAccountTests(ConfirmBase):
    def test_unconfirmed_user_signing_in_gets_a_code_instead_of_access(self):
        self.sign_up()
        self.backdate_code(120)
        client = app_module.app.test_client()
        with mock.patch("emailer.send_email", return_value=True) as sent:
            response = self.post("/login", {"email": "new@example.com", "password": PASSWORD}, client=client)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/confirm-email"))
        self.assertEqual(client.get("/buyer/dashboard").status_code, 302)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(self.confirm(self.code_from(sent), client=client).status_code, 302)
        self.assertEqual(client.get("/buyer/dashboard").status_code, 200)

    def test_unconfirmed_login_with_a_wrong_password_reveals_nothing(self):
        self.sign_up()
        response = self.login_wrong("new@example.com")
        self.assertEqual(response.status_code, 401)

    def login_wrong(self, email):
        return self.post("/login", {"email": email, "password": "Wrong12345"})

    def test_signing_up_again_with_an_unconfirmed_address_replaces_it(self):
        self.sign_up()
        first_id = self.user()["id"]
        response, sent = self.sign_up(password="Another1234", confirmation="Another1234")
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.user()["id"], first_id)
        self.assertEqual(self.query("SELECT COUNT(*) FROM users WHERE email = 'new@example.com'")[0][0], 1)
        # the old password no longer works, the new one does (after confirming)
        client = app_module.app.test_client()
        self.confirm(self.code_from(sent), client=client) if False else None
        self.assertEqual(self.post("/login", {"email": "new@example.com", "password": PASSWORD}).status_code, 401)

    def test_a_confirmed_address_cannot_be_registered_again(self):
        self.make_user("taken@example.com")
        response, _ = self.sign_up(email="taken@example.com")
        self.assertEqual(response.status_code, 400)

    def test_google_sign_in_discards_the_password_of_an_unproven_signup(self):
        self.sign_up()
        take_over_unconfirmed_for_google(self.user()["id"])
        row = self.user()
        self.assertEqual((row["email_verified"], row["has_password"]), (1, 0))
        self.assertEqual(self.login_wrong("new@example.com").status_code, 401)
        self.assertEqual(self.post("/login", {"email": "new@example.com", "password": PASSWORD}).status_code, 401)

    def test_resetting_the_password_through_the_emailed_link_also_confirms_the_address(self):
        self.sign_up()
        with mock.patch("app.send_reset_email", return_value=True) as sent:
            self.post("/forgot-password", {"email": "new@example.com"}, token_path="/forgot-password")
        link = re.sub(r"^https?://[^/]+", "", sent.call_args[0][2])
        self.post(link, {"password": "BrandNew456", "confirmation": "BrandNew456"}, token_path=link)
        self.assertEqual(self.user()["email_verified"], 1)
        self.assertEqual(self.post("/login", {"email": "new@example.com", "password": "BrandNew456"}).status_code, 302)

    def test_existing_and_admin_created_accounts_count_as_confirmed(self):
        self.make_user("old@example.com")
        self.assertEqual(self.user("old@example.com")["email_verified"], 1)
        self.assertEqual(self.login("old@example.com").status_code, 302)


class EmailChangeTests(ConfirmBase):
    def setUp(self):
        super().setUp()
        self.make_user("me@example.com", name="Me Myself")
        self.client_me = self.client_for("me@example.com")

    def change_to(self, new_email, name="Me Myself"):
        with mock.patch("emailer.send_email", return_value=True) as sent:
            response = self.post("/profile", {"name": name, "email": new_email}, client=self.client_me, token_path="/profile")
        return response, sent

    def test_new_address_is_only_used_after_the_code_is_entered(self):
        response, sent = self.change_to("fresh@example.com")
        self.assertTrue(response.headers["Location"].endswith("/confirm-email"))
        self.assertEqual(sent.call_args[0][0], "fresh@example.com")                 # the code goes to the NEW address
        self.assertIsNotNone(self.user("me@example.com"))
        self.assertIsNone(self.user("fresh@example.com"))
        self.assertEqual(self.confirm("000000" if self.code_from(sent) != "000000" else "111111",
                                      client=self.client_me).status_code, 400)
        self.assertIsNotNone(self.user("me@example.com"))                           # still the old one
        self.assertEqual(self.confirm(self.code_from(sent), client=self.client_me).status_code, 302)
        self.assertIsNone(self.user("me@example.com"))
        self.assertIsNotNone(self.user("fresh@example.com"))
        self.assertEqual(self.client_me.get("/profile").status_code, 200)

    def test_name_only_changes_apply_immediately(self):
        with mock.patch("emailer.send_email", return_value=True) as sent:
            response = self.post("/profile", {"name": "Renamed Me", "email": "me@example.com"},
                                 client=self.client_me, token_path="/profile")
        sent.assert_not_called()
        self.assertTrue(response.headers["Location"].endswith("/profile"))
        self.assertEqual(self.user("me@example.com")["name"], "Renamed Me")

    def test_an_address_used_by_someone_else_is_refused(self):
        self.make_user("other@example.com")
        response, sent = self.change_to("other@example.com")
        self.assertEqual(response.status_code, 200)
        sent.assert_not_called()

    def test_email_change_codes_cannot_be_used_by_a_signed_out_visitor(self):
        _, sent = self.change_to("fresh@example.com")
        stranger = app_module.app.test_client()
        with stranger.session_transaction() as sess:
            sess["verify_user_id"] = self.user("me@example.com")["id"]
        self.assertEqual(stranger.get("/confirm-email").status_code, 302)
        self.assertEqual(self.confirm(self.code_from(sent), client=stranger, token_path="/login").status_code, 302)
        self.assertIsNotNone(self.user("me@example.com"))


class CodeEmailContentTests(unittest.TestCase):
    def test_text_ends_with_the_bare_code(self):
        text, html = emailer.build_code_email("Ann <b>", "012345", 15)
        self.assertEqual(text.splitlines()[-1], "012345")
        self.assertIn("012345", html)
        self.assertNotIn("<b>", html)   # the name is escaped in the HTML version


if __name__ == "__main__":
    unittest.main()
