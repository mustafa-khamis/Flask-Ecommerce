import os
from pathlib import Path

try:  # Load variables from a local .env file when python-dotenv is installed.
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DATABASE = Path(os.environ.get("ECOMMERCE_DATABASE", BASE_DIR / "EcommerceDB"))
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    # No hardcoded fallback: a random key keeps the app safe, but sessions end on every restart.
    import secrets
    SECRET_KEY = secrets.token_hex(32)
    print("WARNING: SECRET_KEY is not set. Using a temporary random key; set it in .env.")
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "0") == "1"

# Google sign-in is switched on only when both values are set.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
# Optional. Defaults to http://<host>/auth/google/callback.
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

# Password reset emails are sent with Resend (https://resend.com).
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "")
# Public address of the site, used to build the link inside reset emails.
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").rstrip("/")
PASSWORD_RESET_MINUTES = 30

# Weather comes from OpenWeather (https://openweathermap.org/api).
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")

# Product images are saved here (inside static/ so Flask can serve them).
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", BASE_DIR / "static" / "uploads" / "products"))
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
CURRENCY = os.environ.get("CURRENCY", "$")
