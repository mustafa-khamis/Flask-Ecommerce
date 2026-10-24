"""Send email through Resend's HTTP API (https://resend.com/docs/api-reference/emails/send-email)."""
import json
import os
import ssl
import urllib.error
import urllib.request

from markupsafe import escape

from config import RESEND_API_KEY, RESEND_FROM_EMAIL

RESEND_URL = "https://api.resend.com/emails"


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def send_email(to, subject, text, html):
    """Return True when Resend accepted the message. Never raises, so callers can stay generic."""
    if not RESEND_API_KEY or not RESEND_FROM_EMAIL:
        print("Email not sent: RESEND_API_KEY / RESEND_FROM_EMAIL are not set.")
        return False
    payload = json.dumps({
        "from": RESEND_FROM_EMAIL, "to": [to], "subject": subject, "text": text, "html": html,
    }).encode()
    req = urllib.request.Request(RESEND_URL, data=payload, method="POST", headers={
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "lumamarket-student-project/1.0",  # Resend rejects requests without one
    })
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ssl_context()):
            return True
    except urllib.error.HTTPError as error:
        print(f"Resend returned HTTP {error.code}: {error.read()[:300]!r}")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print(f"Resend request failed: {error}")
    return False


def build_reset_email(name, link, minutes):
    """Return (text, html) for the reset email. Both contain exactly the same link.

    In the plain-text version the link is the very last thing, so no sentence can ever run on
    after it, even if a mail system joins the lines together. It is a bare URL, not Markdown.
    """
    text = "\n".join([
        f"Hello {name},",
        "",
        "We received a request to reset your LumaMarket password.",
        "If you did not request this, you can ignore this email. Your password will not change.",
        "",
        f"To choose a new password, open this link within {minutes} minutes (it works once):",
        link,
    ])
    html = (f"<p>Hello {escape(name)},</p>"
            f"<p>We received a request to reset your LumaMarket password. "
            f"The link below works for {minutes} minutes and can be used once.</p>"
            f'<p><a href="{escape(link)}">Choose a new password</a></p>'
            "<p>If you did not request this, you can ignore this email. Your password will not change.</p>")
    return text, html


def send_reset_email(to, name, link, minutes):
    text, html = build_reset_email(name, link, minutes)
    sent = send_email(to, "Reset your LumaMarket password", text, html)
    if not sent and os.environ.get("FLASK_DEBUG") == "1":
        print(f"[debug only] Password reset link for {to}: {link}")
    return sent
