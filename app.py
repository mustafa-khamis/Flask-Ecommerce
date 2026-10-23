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
from werkzeug.security import check_password_hash

from config import (
    APP_BASE_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_ENABLED, GOOGLE_REDIRECT_URI,
    MAX_UPLOAD_BYTES, PASSWORD_RESET_MINUTES, SECRET_KEY, SESSION_COOKIE_SECURE, UPLOAD_DIR,
)
import shop
import weather as weather_service
from emailer import send_reset_email
from functions import (
    ROLES, add_google_user, add_user, count_users, create_reset_token, find_valid_reset,
    get_user_by_email, get_user_by_id, get_users, init_db, login_required, password_error,
    role_required, throttle_clear, throttle_count, throttle_record, update_password,
    update_profile, update_user, use_reset_token, valid_email,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = SESSION_COOKIE_SECURE
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES + 512 * 1024  # image plus the other form fields
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
    g.user = user if user and user["is_active"] else None


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
        if get_user_by_email(email):
            errors.append("An account with that email already exists.")

        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("auth/register.html"), 400

        try:
            add_user(name, email, password, role)
        except sqlite3.IntegrityError:
            flash("An account with that email already exists.", "error")
            return render_template("auth/register.html"), 400
        flash("Account created. You can now sign in.", "success")
        return redirect(url_for("login"))
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
            session.clear()
            session["user_id"] = user["id"]
            flash(f"Welcome back, {user['name']}.", "success")
            return redirect(url_for(dashboard_url(user["role"])))
        throttle_record("login", throttle_key)
        flash("Invalid email or password.", "error")
        return render_template("auth/login.html"), 401
    return render_template("auth/login.html")


# --- Forgot / reset password -------------------------------------------------
RESET_REQUEST_MESSAGE = ("If an account exists for that email address, we have sent a link to reset the "
                         f"password. It works for {PASSWORD_RESET_MINUTES} minutes.")


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
        if within_limits and user and user["is_active"]:
            token = create_reset_token(user["id"], PASSWORD_RESET_MINUTES)
            base = APP_BASE_URL or request.host_url.rstrip("/")
            link = f"{base}{url_for('reset_password', token=token)}"
            send_reset_email(user["email"], user["name"], link, PASSWORD_RESET_MINUTES)
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


def finish_login(user):
    session.clear()
    session["user_id"] = user["id"]
    flash(f"Welcome back, {user['name']}.", "success")
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
@app.route("/weather")
def weather():
    mode = "coordinates" if request.args.get("mode") == "coordinates" else "city"
    searched = any(key in request.args for key in ("city", "lat", "lon"))
    context = {"mode": mode, "form": request.args, "report": None, "error": None, "searched": searched}
    if not searched:
        return render_template("weather.html", **context)

    user_id = g.user["id"] if g.user else None
    query = (request.args.get("city", "") if mode == "city"
             else f"{request.args.get('lat', '')}, {request.args.get('lon', '')}")
    status = 200
    try:
        ip = request.remote_addr or "unknown"
        if throttle_count("weather", ip, 10) >= 30:  # protects the OpenWeather quota
            raise weather_service.WeatherError("service", "Too many searches in a short time. Please wait a few minutes.")
        throttle_record("weather", ip)
        report, query = weather_service.lookup(mode, request.args)
        context["report"] = report
        weather_service.record_search(user_id, mode, query, report=report)
    except weather_service.WeatherError as problem:
        context["error"] = problem
        status = {"validation": 400, "not_found": 404, "network": 502, "service": 503}[problem.kind]
        weather_service.record_search(user_id, mode, query, error=f"{problem.kind}: {problem}")
    return render_template("weather.html", **context), status


@app.route("/admin/weather")
@role_required("Admin")
def admin_weather():
    return render_template("dashboards/admin_weather.html", stats=weather_service.usage_stats(),
                           searches=weather_service.recent_searches())

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
    return render_template("shop/product.html", product=product, is_owner=is_owner)


# --- Seller: product management ------------------------------------------------
def product_form_data():
    """Read the product form; returns (values, errors, new_image_filename)."""
    values, errors = shop.validate_product_form(request.form)
    image = None
    upload = request.files.get("image")
    if upload and upload.filename:
        try:
            image = shop.save_image(upload)
        except ValueError as problem:
            errors.append(str(problem))
    return values, errors, image


def form_defaults():
    return {key: request.form.get(key, "") for key in
            ("name", "description", "price", "stock", "sale_price", "sale_start", "sale_end")}


@app.route("/seller/products/new", methods=["GET", "POST"])
@role_required("Seller")
def product_new():
    if request.method == "POST":
        values, errors, image = product_form_data()
        if errors:
            shop.delete_image(image)
            for error in errors:
                flash(error, "error")
            return render_template("seller/product_form.html", product=None, form=form_defaults()), 400
        product_id = shop.create_product(g.user["id"], values, image)
        flash("Product created.", "success")
        return redirect(url_for("product_detail", product_id=product_id))
    return render_template("seller/product_form.html", product=None, form={})


def own_product_or_404(product_id):
    product = shop.get_product(product_id)
    if product is None:
        abort(404)
    if product["seller_id"] != g.user["id"]:
        abort(403)  # sellers can only touch their own products
    return product


@app.route("/seller/products/<int:product_id>/edit", methods=["GET", "POST"])
@role_required("Seller")
def product_edit(product_id):
    product = own_product_or_404(product_id)
    if request.method == "POST":
        values, errors, image = product_form_data()
        if errors:
            shop.delete_image(image)
            for error in errors:
                flash(error, "error")
            return render_template("seller/product_form.html", product=product, form=form_defaults()), 400
        if shop.update_product(product_id, g.user["id"], values, image or product["image"]):
            if image:
                shop.delete_image(product["image"])
            flash("Product updated.", "success")
        return redirect(url_for("product_detail", product_id=product_id))
    form = {
        "name": product["name"], "description": product["description"],
        "price": f"{product['price_cents'] / 100:.2f}", "stock": product["stock"],
        "sale_price": f"{product['sale_price_cents'] / 100:.2f}" if product["sale_price_cents"] else "",
        "sale_start": product["sale_start"] or "", "sale_end": product["sale_end"] or "",
    }
    return render_template("seller/product_form.html", product=product, form=form)


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


@app.post("/cart/add")
@role_required("Buyer")
def cart_add():
    quantity = quantity_from_form()
    product_id = shop.parse_whole_number(request.form.get("product_id", ""), 1, 2**31)
    if not quantity or not product_id:
        flash(f"Choose a quantity from 1 to {shop.MAX_CART_QUANTITY}.", "error")
        return redirect(request.referrer or url_for("catalog"))
    ok, message = shop.add_to_cart(g.user["id"], product_id, quantity)
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
        flash(f"Enter a quantity from 0 to {shop.MAX_CART_QUANTITY}.", "error")
    else:
        flash(shop.set_cart_quantity(g.user["id"], product_id, quantity), "success")
    return redirect(url_for("cart"))


@app.post("/cart/remove/<int:product_id>")
@role_required("Buyer")
def cart_remove(product_id):
    flash(shop.set_cart_quantity(g.user["id"], product_id, 0), "success")
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
