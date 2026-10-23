# E-Commerce Stage 1

This Flask application implements user management and authentication only. Product inventory, catalog, cart, checkout, and payment features are intentionally reserved for Stage 2.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python setup_admin.py
flask --app app run --debug
```

The database is created at `EcommerceDB` by default. Set `ECOMMERCE_DATABASE` to use a separate path. Set `SESSION_COOKIE_SECURE=1` only when serving over HTTPS. Never commit `.env`, passwords, or the database.

To create an admin non-interactively for a controlled deployment, provide `ADMIN_PASSWORD` for the setup command:

```bash
ADMIN_PASSWORD='use-a-secret-value' python setup_admin.py
```

## Stage 1 routes

- `GET /` public welcome page
- `GET, POST /register` buyer/seller registration (public registration cannot create admins)
- `GET, POST /login` authentication
- `POST /logout` sign out
- `GET, POST /profile` profile updates
- `POST /profile/password` password changes
- `GET /admin/dashboard`, `POST /admin/users/<id>` admin user management
- `GET /seller/dashboard` and `GET /buyer/dashboard` role-specific dashboards

The code deliberately uses direct SQLite connections and short helper functions so it is easy to follow in a university assignment. Passwords are Werkzeug hashes, sessions contain only the user ID, and protected routes check the current active role from SQLite.

## Google sign-in

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project, then **APIs & Services → OAuth consent screen** (External, add your email as a test user).
2. **Credentials → Create credentials → OAuth client ID → Web application**.
3. Add this under **Authorized redirect URIs** (exactly): `http://127.0.0.1:5000/auth/google/callback`
4. Copy `.env.example` to `.env` and fill in `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, then `pip install -r requirements.txt`.
5. Open the app at `http://127.0.0.1:5000` (use the same host as the redirect URI) and use **Continue with Google**.

If the two variables are empty, the Google button is simply hidden. Existing accounts are matched by verified email; new Google users choose Buyer or Seller (never Admin).
