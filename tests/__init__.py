"""Importing this package points the app at a throw-away database BEFORE any app module is imported."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="lumamarket-tests-")
os.environ["ECOMMERCE_DATABASE"] = os.path.join(_tmp, "test.db")
os.environ["UPLOAD_DIR"] = os.path.join(_tmp, "uploads")
os.environ["SECRET_KEY"] = "test-secret"
os.environ["RESEND_API_KEY"] = "re_test"
os.environ["RESEND_FROM_EMAIL"] = "Test <test@example.com>"
os.environ["OPENWEATHER_API_KEY"] = "owm_test"
os.environ["GEOIP_ENABLED"] = "0"   # tests that need the IP lookup switch it on and mock the network
os.environ["APP_BASE_URL"] = ""
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GOOGLE_CLIENT_SECRET"] = ""
TEMP_DIR = _tmp
