import json
import os
import secrets
import sqlite3
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

from flask import (
    Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for,
)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash

from config import (
    APP_BASE_URL, CODE_MINUTES, CODE_RESEND_SECONDS, CODE_RESENDS_PER_HOUR, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_ENABLED, GOOGLE_REDIRECT_URI,
    MAX_IMAGES_PER_PRODUCT, MAX_UPLOAD_BYTES, PASSWORD_RESET_MINUTES, SECRET_KEY, SESSION_COOKIE_SECURE,
    TRUST_PROXY, UPLOAD_DIR,
)
import geolocation
import shop
import weather as weather_service
from emailer import send_code_email, send_reset_email
from functions import (
    ROLES, add_google_user, add_user, change_email, check_email_code, count_users, create_email_code,
    create_reset_token, find_valid_reset, get_user_by_email, get_user_by_id, get_users, init_db,
    login_required, mark_email_verified, password_error, pending_code, remove_unconfirmed_account,
    role_required, seconds_since_last_code, take_over_unconfirmed_for_google, throttle_clear,
    throttle_count, throttle_record, update_password, update_profile, update_user, use_reset_token,
    valid_email,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
if TRUST_PROXY:
    # Use the visitor address that the hosting proxy puts in X-Forwarded-For (one proxy deep),
    # otherwise every visitor would look like the proxy: same location and shared rate limits.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES * MAX_IMAGES_PER_PRODUCT + 512 * 1024  # all images plus other fields
init_db()


def dashboard_url(role):
    if role == "Admin":
        return "admin_dashboard"
    if role == "Seller":
        return "seller_dashboard"
    return "buyer_dashboard"


def format_date(value):
    """Turn an SQLite timestamp such as '2026-10-22 09:30:00' into '22 Oct 2026'."""
    try:
        day = datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except ValueError:
        return value
    return f"{day.day} {day:%b %Y}"


# Helpers available in every template.
app.jinja_env.globals["dashboard_url"] = dashboard_url
app.jinja_env.globals["current_year"] = date.today().year
app.jinja_env.globals["google_enabled"] = GOOGLE_ENABLED
app.jinja_env.filters["date"] = format_date
app.jinja_env.filters["money"] = shop.money
app.jinja_env.globals["price_info"] = shop.price_info
app.jinja_env.globals["product_badges"] = shop.product_badges
app.jinja_env.globals["MAX_IMAGES"] = MAX_IMAGES_PER_PRODUCT


@app.context_processor
def inject_cart_count():
    if g.get("user") and g.user["role"] == "Buyer":
        return {"cart_count": shop.cart_count(g.user["id"])}
    return {"cart_count": 0}


@app.before_request
def load_current_user():
    # Lets public pages (home, sign in, errors) know who is signed in for the header.
    # Protected routes still check access with login_required and role_required.
    user = get_user_by_id(session["user_id"]) if "user_id" in session else None
    g.user = user if user and user["is_active"] and user["email_verified"] else None


CSRF_MESSAGE = "Your session expired or the form was not valid. Please reload the page and try again."


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_input"] = lambda: Markup(
    f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


@app.before_request
def check_csrf_token():
    # Every state-changing request must carry the token that was put in the user's session.
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(session.get("csrf_token", ""), sent):
            abort(400, CSRF_MESSAGE)


@app.after_request
def disable_page_caching(response):
    response.headers["Referrer-Policy"] = "no-referrer"  # keeps reset tokens out of Referer headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Stops the browser showing a cached dashboard when going Back after signing out.
    if request.endpoint != "static":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.route("/")
def welcome():
    return render_template("index.html", featured=shop.latest_products(4))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        role = request.form.get("role", "")
        errors = []

        if len(name) < 2:
            errors.append("Please enter your full name.")
        if not valid_email(email):
            errors.append("Please enter a valid email address.")
        if password_error(password):
            errors.append(password_error(password))
        if password != confirmation:
            errors.append("Passwords do not match.")
        if role not in ("Buyer", "Seller"):
            errors.append("Please select Buyer or Seller.")
        existing = get_user_by_email(email)
        if existing and existing["email_verified"]:
            errors.append("An account with that email already exists.")

        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("auth/register.html"), 400

        # An earlier sign-up with this address that was never confirmed cannot sign in, so it is replaced.
        remove_unconfirmed_account(email)
        try:
            user_id = add_user(name, email, password, role, email_verified=0)
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "error")
            return render_template("auth/register.html"), 400
        session.clear()
        session["verify_user_id"] = user_id
        sent = send_confirmation_code(get_user_by_id(user_id), "signup", email)
        flash(f"We sent a 6-digit code to {email}. Enter it to finish creating your account." if sent else
              "Your account was created, but we could not send the email right now. Press \u201cSend a new code\u201d to try again.",
              "success" if sent else "error")
        return redirect(url_for("confirm_email"))
    return render_template("auth/register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        throttle_key = f"{email}|{request.remote_addr}"
        if throttle_count("login", throttle_key, 15) >= 10:
            flash("Too many sign-in attempts. Please wait 15 minutes or reset your password.", "error")
            return render_template("auth/login.html"), 429
        user = get_user_by_email(email)
        if user and user["is_active"] and check_password_hash(
            user["password_hash"], request.form.get("password", "")
        ):
            throttle_clear("login", throttle_key)
            if not user["email_verified"]:
                # Right password, but the address was never confirmed: send a code instead of signing in.
                session.clear()
                session["verify_user_id"] = user["id"]
                if may_send_code(user["id"]):
                    send_confirmation_code(user, "signup", user["email"])
                flash("Please confirm your email address first. Enter the 6-digit code we sent you.", "info")
                return redirect(url_for("confirm_email"))
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['name']}.", "success")
            return redirect(url_for(dashboard_url(user["role"])))
        throttle_record("login", throttle_key)
        flash("Invalid email or password.", "error")
        return render_template("auth/login.html"), 401
    return render_template("auth/login.html")


# --- Confirming an email address with a 6-digit code ------------------------------
def send_confirmation_code(user, purpose, email):
    """Make a code and email it. Returns True when Resend accepted the message."""
    throttle_record("code-send", str(user["id"]))
    code = create_email_code(user["id"], purpose, email, CODE_MINUTES)
    sent = send_code_email(email, user["name"], code, CODE_MINUTES)
    if sent:
        app.logger.info("Confirmation code sent to %s (%s).", email, purpose)
    else:
        app.logger.error("Confirmation code to %s FAILED to send (see the Resend message above).", email)
    return sent


def may_send_code(user_id):
    """False while the resend wait is running or the hourly limit is used up."""
    since = seconds_since_last_code(user_id)
    return ((since is None or since >= CODE_RESEND_SECONDS)
            and throttle_count("code-send", str(user_id), 60) < CODE_RESENDS_PER_HOUR)


def mask_email(email):
    name, _, domain = email.partition("@")
    return f"{name[:1]}{'*' * max(len(name) - 1, 2)}@{domain}"


def code_subject():
    """Who is confirming right now, as (user, newest unused code or None), or None if nobody is.

    The user id comes from the signed session: it is only put there by a new sign-up, by a correct
    password on an unconfirmed account, or by a signed-in user changing their email.
    """
    user = get_user_by_id(session.get("verify_user_id"))
    if user is None or not user["is_active"]:
        return None
    row = pending_code(user["id"])
    if not user["email_verified"]:
        return user, row  # sign-up confirmation (no or expired code: the page offers a new one)
    if row is not None and row["purpose"] == "email_change" and g.user and g.user["id"] == user["id"]:
        return user, row  # a signed-in owner confirming a new address
    return None


def render_confirm(user, row, status=200):
    since = seconds_since_last_code(user["id"])
    wait = 0 if since is None else max(CODE_RESEND_SECONDS - since, 0)  # 0 seconds ago is NOT "never sent"
    target = row["email"] if row else user["email"]
    return render_template("auth/confirm_email.html", masked=mask_email(target), wait=wait, has_code=row is not None
                           and not row["expired"], purpose=row["purpose"] if row else "signup",
                           minutes=CODE_MINUTES), status


@app.route("/confirm-email", methods=["GET", "POST"])
def confirm_email():
    subject = code_subject()
    if subject is None:
        flash("There is nothing to confirm. Sign in, or create an account.", "info")
        return redirect(url_for("login"))
    user, row = subject
    if request.method == "POST":
        status, info = check_email_code(user["id"], request.form.get("code", ""))
        if status == "ok":
            if info["purpose"] == "email_change":
                if not change_email(user["id"], info["email"]):
                    flash("That email address is already in use by another account.", "error")
                    return redirect(url_for("profile"))
                session.pop("verify_user_id", None)
                flash("Your new email address is confirmed.", "success")
                return redirect(url_for("profile"))
            mark_email_verified(user["id"])
            return finish_login(get_user_by_id(user["id"]), f"Email confirmed. Welcome to LumaMarket, {user['name']}!")
        message = {
            "wrong": f"That code is not right. You have {info} {'try' if info == 1 else 'tries'} left.",
            "expired": f"That code has expired (codes work for {CODE_MINUTES} minutes). Send a new code.",
            "locked": "Too many wrong attempts. Send a new code to try again.",
            "none": "There is no active code. Send a new code.",
        }[status]
        flash(message, "error")
        return render_confirm(user, pending_code(user["id"]), 400)
    return render_confirm(user, row)


@app.post("/confirm-email/resend")
def confirm_email_resend():
    subject = code_subject()
    if subject is None:
        return redirect(url_for("login"))
    user, row = subject
    since = seconds_since_last_code(user["id"])
    if since is not None and since < CODE_RESEND_SECONDS:
        flash(f"Please wait {CODE_RESEND_SECONDS - since} seconds before asking for another code.", "error")
    elif not may_send_code(user["id"]):
        flash("Too many codes requested. Please try again in an hour.", "error")
    else:
        purpose = row["purpose"] if row else "signup"
        target = row["email"] if row else user["email"]
        if send_confirmation_code(user, purpose, target):
            flash(f"A new code was sent to {target}. Older codes no longer work.", "success")
        else:
            flash("We could not send the email right now. Please try again in a moment.", "error")
    return redirect(url_for("confirm_email"))


# --- Forgot / reset password -------------------------------------------------
RESET_REQUEST_MESSAGE = ("If an account exists for that email address, we have sent a link to reset the "
                         f"password. It works for {PASSWORD_RESET_MINUTES} minutes.")


def reset_link(token):
    """Absolute reset URL. APP_BASE_URL (if set) is the public address; otherwise Flask's own external URL."""
    if APP_BASE_URL:
        return APP_BASE_URL + url_for("reset_password", token=token)
    return url_for("reset_password", token=token, _external=True)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        if not valid_email(email):
            flash("Please enter a valid email address.", "error")
            return render_template("auth/forgot_password.html"), 400

        # Limits per email and per IP. Going over a limit looks exactly like success,
        # so the response never reveals whether an account exists.
        ip = request.remote_addr or "unknown"
        within_limits = (throttle_count("reset", email, 60) < 3
                         and throttle_count("reset-ip", ip, 60) < 10)
        throttle_record("reset", email)
        throttle_record("reset-ip", ip)

        user = get_user_by_email(email)
        # The page always answers the same way; the server log says what really happened.
        if not within_limits:
            app.logger.warning("Password reset NOT sent to %s: rate limit reached (3 per email, 10 per IP per hour).", email)
        elif not user or not user["is_active"]:
            app.logger.info("Password reset requested for %s: no active account, nothing sent.", email)
        else:
            token = create_reset_token(user["id"], PASSWORD_RESET_MINUTES)
            if send_reset_email(user["email"], user["name"], reset_link(token), PASSWORD_RESET_MINUTES):
                app.logger.info("Password reset email sent to %s.", email)
            else:
                app.logger.error("Password reset email to %s FAILED to send (see the Resend message above).", email)
        flash(RESET_REQUEST_MESSAGE, "success")
        return redirect(url_for("forgot_password"))
    return render_template("auth/forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    if find_valid_reset(token) is None:
        return render_template("auth/reset_invalid.html"), 400
    if request.method == "POST":
        password = request.form.get("password", "")
        problem = password_error(password)
        if problem:
            flash(problem, "error")
        elif password != request.form.get("confirmation", ""):
            flash("Passwords do not match.", "error")
        elif use_reset_token(token, password):
            flash("Your password has been changed. You can now sign in.", "success")
            return redirect(url_for("login"))
        else:
            return render_template("auth/reset_invalid.html"), 400
        return render_template("auth/reset_password.html", token=token), 400
    return render_template("auth/reset_password.html", token=token)


# --- Google sign-in (OAuth 2.0 authorization code flow) ---------------------
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def google_redirect_uri():
    return GOOGLE_REDIRECT_URI or url_for("google_callback", _external=True)


def ssl_context():
    """Use certifi's trusted certificates when available (python.org builds on macOS ship none)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def google_request(url, data=None, token=None):
    """Call a Google endpoint and return its JSON, or None if the call fails."""
    body = urllib.parse.urlencode(data).encode() if data else None
    request_ = urllib.request.Request(url, data=body)
    if token:
        request_.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request_, timeout=10, context=ssl_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        print(f"Google sign-in: {url} returned HTTP {error.code}: {error.read()[:300]!r}")
    except (urllib.error.URLError, ValueError) as error:
        print(f"Google sign-in: request to {url} failed: {error}")
    return None


def finish_login(user, message=None):
    session.clear()
    session["user_id"] = user["id"]
    flash(message or f"Welcome back, {user['name']}.", "success")
    return redirect(url_for(dashboard_url(user["role"])))


@app.route("/auth/google")
def google_start():
    if not GOOGLE_ENABLED:
        abort(404)
    state = secrets.token_urlsafe(32)
    session["google_state"] = state
    # A role chosen on the registration page (Buyer or Seller only) is remembered for new accounts.
    role = request.args.get("role")
    session["google_role"] = role if role in ("Buyer", "Seller") else ""
    query = urllib.parse.urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": google_redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{query}")


@app.route("/auth/google/callback")
def google_callback():
    if not GOOGLE_ENABLED:
        abort(404)
    expected_state = session.pop("google_state", None)
    wanted_role = session.pop("google_role", "")
    if request.args.get("error"):
        flash("Google sign-in was cancelled. You can try again or use your email and password.", "info")
        return redirect(url_for("login"))
    if not expected_state or not secrets.compare_digest(expected_state, request.args.get("state", "")):
        flash("Google sign-in could not be verified. Please try again.", "error")
        return redirect(url_for("login"))

    tokens = google_request(GOOGLE_TOKEN_URL, data={
        "code": request.args.get("code", ""),
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": google_redirect_uri(),
        "grant_type": "authorization_code",
    })
    profile_info = google_request(GOOGLE_USERINFO_URL, token=tokens.get("access_token")) if tokens and tokens.get("access_token") else None
    email = (profile_info or {}).get("email", "").strip().lower()
    if not profile_info or not email or not profile_info.get("email_verified"):
        flash("We could not get a verified email address from Google. Please try again.", "error")
        return redirect(url_for("login"))

    user = get_user_by_email(email)
    if user:
        if not user["is_active"]:
            flash("This account has been deactivated. Please contact an administrator.", "error")
            return redirect(url_for("login"))
        if not user["email_verified"]:
            # Google proved the address; the unproven sign-up's password is thrown away.
            take_over_unconfirmed_for_google(user["id"])
            user = get_user_by_id(user["id"])
        return finish_login(user)

    # New person: they still choose Buyer or Seller (Admin is never available).
    session.clear()
    session["google_pending"] = {
        "name": (profile_info.get("name") or email.split("@")[0]).strip(),
        "email": email,
    }
    if wanted_role in ("Buyer", "Seller"):
        return create_google_account(wanted_role)
    return redirect(url_for("google_role"))


def create_google_account(role):
    pending = session.get("google_pending")
    try:
        add_google_user(pending["name"], pending["email"], role)
    except sqlite3.IntegrityError:
        pass  # Account appeared in the meantime; just sign in below.
    user = get_user_by_email(pending["email"])
    flash("Your account has been created with Google.", "success")
    session.clear()
    session["user_id"] = user["id"]
    return redirect(url_for(dashboard_url(user["role"])))


@app.route("/auth/google/role", methods=["GET", "POST"])
def google_role():
    pending = session.get("google_pending")
    if not pending:
        return redirect(url_for("login"))
    if request.method == "POST":
        role = request.form.get("role", "")
        if role not in ("Buyer", "Seller"):
            flash("Please choose Buyer or Seller.", "error")
            return render_template("auth/google_role.html", pending=pending), 400
        return create_google_account(role)
    return render_template("auth/google_role.html", pending=pending)


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        other_user = get_user_by_email(email)
        if len(name) < 2 or not valid_email(email):
            flash("Please enter a valid name and email.", "error")
        elif other_user and other_user["id"] != g.user["id"]:
            flash("That email address is already in use.", "error")
        elif email != g.user["email"].lower():
            # A new address is only used after its owner enters the code we send there.
            update_profile(g.user["id"], name, g.user["email"])
            session["verify_user_id"] = g.user["id"]
            sent = send_confirmation_code(g.user, "email_change", email)
            flash(f"We sent a 6-digit code to {email}. Enter it to confirm the new address." if sent else
                  "We could not send the email right now. Press \u201cSend a new code\u201d to try again.",
                  "success" if sent else "error")
            return redirect(url_for("confirm_email"))
        else:
            update_profile(g.user["id"], name, email)
            flash("Profile updated.", "success")
            return redirect(url_for("profile"))
    return render_template("profile.html")


@app.post("/profile/password")
@login_required
def change_password():
    new_password = request.form.get("new_password", "")
    # Accounts created with Google have no password yet, so there is no current one to check.
    if g.user["has_password"] and not check_password_hash(
            g.user["password_hash"], request.form.get("current_password", "")):
        flash("Current password is incorrect.", "error")
    elif password_error(new_password):
        flash(password_error(new_password), "error")
    elif request.form.get("new_password") != request.form.get("confirmation"):
        flash("Passwords do not match.", "error")
    else:
        update_password(g.user["id"], new_password)
        flash("Password changed successfully." if g.user["has_password"] else "Password set. You can now sign in with your email and password too.", "success")
    return redirect(url_for("profile"))


@app.route("/admin/dashboard")
@role_required("Admin")
def admin_dashboard():
    stats = {
        "total": count_users(),
        "active": count_users(is_active=True),
        "inactive": count_users(is_active=False),
        "admins": count_users("Admin"),
        "sellers": count_users("Seller"),
        "buyers": count_users("Buyer"),
        "products": len(shop.all_products_for_admin()),
    }
    search = request.args.get("search", "").strip()
    role_filter = request.args.get("role", "")
    if role_filter not in ROLES:
        role_filter = ""
    return render_template("dashboards/admin.html", users=get_users(search, role_filter),
                           stats=stats, search=search, role_filter=role_filter)


@app.post("/admin/users/<int:user_id>")
@role_required("Admin")
def admin_update_user(user_id):
    user = get_user_by_id(user_id)
    role = request.form.get("role")
    is_active = 1 if request.form.get("is_active") == "1" else 0
    if user is None:
        abort(404)
    if role not in ROLES:
        abort(400)
    if role == user["role"] and is_active == user["is_active"]:
        flash(f"No changes were made to {user['name']}.", "info")
    elif user_id == g.user["id"] and (role != "Admin" or not is_active):
        flash("You cannot deactivate or demote yourself.", "error")
    elif (user["role"] == "Admin" and user["is_active"] and (role != "Admin" or not is_active)
          and count_users("Admin", is_active=True) == 1):
        flash("The last active administrator cannot be deactivated or demoted.", "error")
    else:
        update_user(user_id, role, is_active)
        status = "active" if is_active else "inactive"
        flash(f"{user['name']} is now an {status} {role}.", "success")
    # Return to the same search results the admin was looking at.
    return redirect(url_for("admin_dashboard",
                            search=request.form.get("return_search") or None,
                            role=request.form.get("return_role") or None))


@app.post("/admin/admins")
@role_required("Admin")
def admin_create_admin():
    """Only a signed-in Admin can reach this route, so only Admins can create other Admins."""
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    errors = []
    if len(name) < 2:
        errors.append("Please enter the new administrator's full name.")
    if not valid_email(email):
        errors.append("Please enter a valid email address.")
    if password_error(password):
        errors.append(password_error(password))
    if password != request.form.get("confirmation", ""):
        errors.append("Passwords do not match.")
    if valid_email(email) and get_user_by_email(email):
        errors.append("An account with that email already exists. Use the Role menu in the table to make them an Admin.")

    if errors:
        for error in errors:
            flash(error, "error")
    else:
        try:
            add_user(name, email, password, "Admin")
            flash(f"Administrator account created for {name}.", "success")
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "error")
    return redirect(url_for("admin_dashboard"))



# --- Weather (public) ------------------------------------------------------------
def saved_location():
    """The place the weather page is currently showing for this visitor, or None."""
    place = session.get("weather_location")
    if isinstance(place, dict) and {"city", "lat", "lon"} <= place.keys():
        return place
    return None


def remember_location(city, country, lat, lon, source):
    session["weather_location"] = {"city": city, "country": country, "lat": lat, "lon": lon, "source": source}


def visitor_ip():
    """The visitor's address. Behind a trusted proxy chain it is the first public address in X-Forwarded-For."""
    if TRUST_PROXY:
        return geolocation.first_public_ip(request.headers.get("X-Forwarded-For"), request.remote_addr or "")
    return request.remote_addr or ""


def detect_location():
    """Estimate the visitor's city from their IP address and remember it for this session.
    Returns the place or None; when None, g.location_error says why in plain words."""
    place, g.location_error = geolocation.locate_with_reason(visitor_ip())
    session["location_tried"] = True
    if place:
        remember_location(place["city"], place["country"], place["lat"], place["lon"], "ip")
        session.pop("location_error", None)
    else:
        session.pop("weather_location", None)
        session["location_error"] = g.location_error
    return saved_location()


def run_weather(mode, params, user_id, query, context, label=None):
    """Look the weather up, save it to the search history and fill in the template context."""
    try:
        ip = request.remote_addr or "unknown"
        if throttle_count("weather", ip, 10) >= 30:  # protects the OpenWeather quota
            raise weather_service.WeatherError("service", "Too many searches in a short time. Please wait a few minutes.")
        throttle_record("weather", ip)
        report, query = weather_service.lookup(mode, params)
        context["report"] = report
        weather_service.record_search(user_id, mode, label or query, report=report)
        return 200
    except weather_service.WeatherError as problem:
        context["error"] = problem
        weather_service.record_search(user_id, mode, query, error=f"{problem.kind}: {problem}")
        return {"validation": 400, "not_found": 404, "network": 502, "service": 503}[problem.kind]


@app.route("/weather")
def weather():
    mode = "coordinates" if request.args.get("mode") == "coordinates" else "city"
    searched = any(key in request.args for key in ("city", "lat", "lon"))
    user_id = g.user["id"] if g.user else None
    context = {"mode": mode, "form": request.args, "report": None, "error": None, "searched": searched,
               "location": None, "show_form": True, "location_failed": False}

    if searched:  # the visitor typed a place: show it and make it their current location
        query = (request.args.get("city", "") if mode == "city"
                 else f"{request.args.get('lat', '')}, {request.args.get('lon', '')}")
        status = run_weather(mode, request.args, user_id, query, context)
        report = context["report"]
        if report and report["lat"] is not None:
            remember_location(report["place"], report["country"], report["lat"], report["lon"], "search")
            context["location"] = saved_location()  # the page now says "Weather in <that place>"
        return render_template("weather.html", **context), status

    # No search: use the remembered location, or work one out from the IP address the first time.
    place = saved_location()
    if place is None and not session.get("location_tried"):
        place = detect_location()
    context["location_failed"] = place is None and geolocation.GEOIP_ENABLED
    context["location_error"] = session.get("location_error", "")
    if place:
        context["location"] = place
        context["show_form"] = request.args.get("change") == "1"
        label = f"{place['city']} (detected from IP)" if place["source"] == "ip" else place["city"]
        status = run_weather("coordinates", {"lat": str(place["lat"]), "lon": str(place["lon"])}, user_id,
                             label, context, label=label)
        return render_template("weather.html", **context), status
    return render_template("weather.html", **context)


@app.post("/weather/browser-location")
def weather_browser_location():
    """Fallback: the visitor's browser looked up its own location (same free services) and sends it here.

    Used when the server could not do it, for example behind a hosting proxy. The values are checked
    like any other input; a visitor can only change what their own weather page shows.
    """
    place = geolocation.clean_place(request.form)
    if place is None:
        session["location_error"] = (session.get("location_error", "") +
                                     " Your browser's answer could not be used either.").strip()
        return redirect(url_for("weather", browser_failed=1))
    remember_location(place["city"], place["country"], place["lat"], place["lon"], "ip")
    session.pop("location_error", None)
    return redirect(url_for("weather"))


@app.post("/weather/detect")
def weather_detect():
    """"Use my current location": forget the chosen place and estimate it from the IP address again."""
    detect_location()  # on failure the weather page explains why and tries the browser instead
    return redirect(url_for("weather"))


@app.route("/admin/weather")
@role_required("Admin")
def admin_weather():
    return render_template("dashboards/admin_weather.html", stats=weather_service.usage_stats(),
                           searches=weather_service.recent_searches())


@app.route("/admin/products")
@role_required("Admin")
def admin_products():
    return render_template("dashboards/admin_products.html", products=shop.all_products_for_admin())


@app.post("/admin/products/<int:product_id>/choice")
@role_required("Admin")
def admin_product_choice(product_id):
    product = shop.get_product(product_id)
    if product is None:
        abort(404)
    award = request.form.get("choice") == "1"
    shop.set_product_choice(product_id, award)
    flash(f"{product['name']} {'now has' if award else 'no longer has'} the LumaMarket's Choice badge.", "success")
    return redirect(url_for("admin_products"))

@app.route("/seller/dashboard")
@role_required("Seller")
def seller_dashboard():
    products = shop.seller_products(g.user["id"])
    return render_template("dashboards/seller.html", products=products,
                           sales=shop.seller_sales(g.user["id"]),
                           low_stock=sum(1 for p in products if p["is_active"] and p["stock"] <= 5))


@app.route("/buyer/dashboard")
@role_required("Buyer")
def buyer_dashboard():
    return render_template("dashboards/buyer.html", recent_orders=shop.buyer_orders(g.user["id"])[:5])

# --- Catalogue -----------------------------------------------------------------
@app.route("/uploads/products/<filename>")
def product_image(filename):
    return send_from_directory(UPLOAD_DIR, filename)


@app.route("/products")
def catalog():
    search = request.args.get("q", "").strip()[:100]
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    products, total = shop.search_catalogue(search, page)
    pages = max((total + shop.PAGE_SIZE - 1) // shop.PAGE_SIZE, 1)
    return render_template("shop/catalog.html", products=products, total=total, search=search,
                           page=page, pages=pages)


@app.route("/products/<int:product_id>")
def product_detail(product_id):
    product = shop.get_product(product_id)
    if product is None:
        abort(404)
    is_owner = bool(g.user and (g.user["id"] == product["seller_id"] or g.user["role"] == "Admin"))
    if not product["is_active"] and not is_owner:
        abort(404)
    return render_template("shop/product.html", product=product, is_owner=is_owner,
                           images=shop.product_images(product_id))


# --- Seller: product management ------------------------------------------------
def product_form_data(already_have=0):
    """Read the product form; returns (values, errors, new_image_filenames)."""
    values, errors = shop.validate_product_form(request.form)
    images, image_errors = shop.save_images(request.files.getlist("images"), already_have)
    return values, errors + image_errors, images


def form_defaults():
    form = {key: request.form.get(key, "") for key in
            ("name", "description", "price", "stock", "sale_price", "sale_start", "sale_end")}
    form["free_shipping"] = request.form.get("free_shipping", "")
    return form


def discard(filenames):
    for filename in filenames:
        shop.delete_image(filename)


@app.route("/seller/products/new", methods=["GET", "POST"])
@role_required("Seller")
def product_new():
    if request.method == "POST":
        values, errors, images = product_form_data()
        if errors:
            discard(images)
            for error in errors:
                flash(error, "error")
            return render_template("seller/product_form.html", product=None, images=[], form=form_defaults()), 400
        product_id = shop.create_product(g.user["id"], values, images)
        flash("Product created.", "success")
        return redirect(url_for("product_detail", product_id=product_id))
    return render_template("seller/product_form.html", product=None, images=[], form={})


def own_product_or_404(product_id):
    product = shop.get_product(product_id)
    if product is None:
        abort(404)
    if product["seller_id"] != g.user["id"]:
        abort(403)  # sellers can only touch their own products
    return product


def id_list(name):
    return [int(value) for value in request.form.getlist(name) if value.isdigit()]


@app.route("/seller/products/<int:product_id>/edit", methods=["GET", "POST"])
@role_required("Seller")
def product_edit(product_id):
    product = own_product_or_404(product_id)
    current_images = shop.product_images(product_id)
    if request.method == "POST":
        remove_ids = [i for i in id_list("remove_image") if i in {img["id"] for img in current_images}]
        keeping = len(current_images) - len(remove_ids)
        values, errors, new_images = product_form_data(already_have=keeping)
        if errors:
            discard(new_images)
            for error in errors:
                flash(error, "error")
            return render_template("seller/product_form.html", product=product, images=current_images,
                                   form=form_defaults()), 400
        cover = request.form.get("cover_image", "")
        cover_id = int(cover) if cover.isdigit() and int(cover) not in remove_ids else None
        updated, removed = shop.update_product(product_id, g.user["id"], values, new_images, remove_ids, cover_id)
        discard(removed)
        if updated:
            flash("Product updated.", "success")
        return redirect(url_for("product_detail", product_id=product_id))
    form = {
        "name": product["name"], "description": product["description"],
        "price": f"{product['price_cents'] / 100:.2f}", "stock": product["stock"],
        "sale_price": f"{product['sale_price_cents'] / 100:.2f}" if product["sale_price_cents"] else "",
        "sale_start": product["sale_start"] or "", "sale_end": product["sale_end"] or "",
        "free_shipping": "1" if product["free_shipping"] else "",
    }
    return render_template("seller/product_form.html", product=product, images=current_images, form=form)


@app.post("/seller/products/<int:product_id>/status")
@role_required("Seller")
def product_status(product_id):
    product = own_product_or_404(product_id)
    make_active = request.form.get("active") == "1"
    shop.set_product_active(product_id, g.user["id"], make_active)
    flash(f"{product['name']} is now {'visible in' if make_active else 'hidden from'} the catalogue.", "success")
    return redirect(url_for("seller_dashboard"))


# --- Buyer: cart, checkout, orders ---------------------------------------------
def quantity_from_form(default=1):
    return shop.parse_whole_number(request.form.get("quantity", default), 0, shop.MAX_CART_QUANTITY)


def wants_json():
    """The cart drawer sends this header; plain form posts (no JavaScript) get a normal redirect."""
    return request.headers.get("X-Requested-With") == "fetch"


def drawer_response(message=None, ok=True, status=200):
    current = shop.get_cart(g.user["id"])
    html = render_template("shop/_cart_drawer.html", cart=current, message=message, ok=ok)
    return {"ok": ok, "message": message, "count": current["count"], "html": html}, status


@app.get("/cart/drawer")
@role_required("Buyer")
def cart_drawer():
    return drawer_response()


@app.post("/cart/add")
@role_required("Buyer")
def cart_add():
    quantity = quantity_from_form()
    product_id = shop.parse_whole_number(request.form.get("product_id", ""), 1, 2**31)
    if not quantity or not product_id:
        message, ok = f"Choose a quantity from 1 to {shop.MAX_CART_QUANTITY}.", False
    else:
        ok, message = shop.add_to_cart(g.user["id"], product_id, quantity)
    if wants_json():
        return drawer_response(message, ok, 200 if ok else 400)
    flash(message, "success" if ok else "error")
    return redirect(url_for("cart") if ok else (request.referrer or url_for("catalog")))


@app.route("/cart")
@role_required("Buyer")
def cart():
    return render_template("shop/cart.html", cart=shop.get_cart(g.user["id"]))


@app.post("/cart/update/<int:product_id>")
@role_required("Buyer")
def cart_update(product_id):
    quantity = quantity_from_form(default=-1)
    if quantity is None:
        message, ok = f"Enter a quantity from 0 to {shop.MAX_CART_QUANTITY}.", False
    else:
        message, ok = shop.set_cart_quantity(g.user["id"], product_id, quantity), True
    if wants_json():
        return drawer_response(message, ok, 200 if ok else 400)
    flash(message, "success" if ok else "error")
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:product_id>")
@role_required("Buyer")
def cart_remove(product_id):
    message = shop.set_cart_quantity(g.user["id"], product_id, 0)
    if wants_json():
        return drawer_response(message)
    flash(message, "success")
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
@role_required("Buyer")
def checkout():
    current = shop.get_cart(g.user["id"])
    if not current["lines"]:
        flash("Your cart is empty.", "info")
        return redirect(url_for("catalog"))
    if request.method == "POST":
        name = request.form.get("shipping_name", "").strip()
        address = request.form.get("shipping_address", "").strip()
        errors = []
        if len(name) < 2 or len(name) > 100:
            errors.append("Enter the name for delivery (2 to 100 characters).")
        if len(address) < 5 or len(address) > 300:
            errors.append("Enter a delivery address (5 to 300 characters).")
        if request.form.get("confirm_simulated") != "1":
            errors.append("Please tick the box to confirm this is a simulated payment.")
        if not errors:
            try:
                order_id = shop.checkout(g.user["id"], name, address)
            except shop.CheckoutError as problem:
                errors.append(str(problem))
            else:
                flash("Order placed. Thank you!", "success")
                return redirect(url_for("order_detail", order_id=order_id))
        for error in errors:
            flash(error, "error")
        return render_template("shop/checkout.html", cart=shop.get_cart(g.user["id"]),
                               form=request.form), 400
    return render_template("shop/checkout.html", cart=current, form={"shipping_name": g.user["name"]})


@app.route("/orders")
@role_required("Buyer")
def orders():
    return render_template("shop/orders.html", orders=shop.buyer_orders(g.user["id"]))


@app.route("/orders/<int:order_id>")
@role_required("Buyer")
def order_detail(order_id):
    order, items = shop.get_order(order_id, g.user["id"])
    if order is None:
        abort(404)  # also what other buyers' orders look like: nothing to see
    return render_template("shop/order.html", order=order, items=items)



@app.errorhandler(400)
def bad_request(error):
    message = (error.description if error.description == CSRF_MESSAGE else
               "The information sent was incomplete or invalid. Please go back and try again.")
    return render_template("error.html", code=400, title="Something went wrong with that request",
                           message=message), 400


@app.errorhandler(413)
def too_large(error):
    return render_template("error.html", code=413, title="That upload is too large",
                           message="Images must be 2 MB or smaller. Please choose a smaller file."), 413


@app.errorhandler(403)
def forbidden(error):
    return render_template("access_denied.html"), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(error):
    return render_template("error.html", code=500, title="Something went wrong on our side",
                           message="We could not complete your request. Please try again in a moment."), 500


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
