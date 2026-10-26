# LumaMarket

A Flask + SQLite marketplace for a university project, covering authentication (Stage 1), e-commerce (Stage 2) and weather plus administration (Stage 3). No frameworks beyond Flask; the code uses short helper functions and direct SQLite so it is easy to follow.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then fill in the values (see below)
ADMIN_EMAIL='you@example.com' ADMIN_NAME='Your Name' ADMIN_PASSWORD='A-strong-pass1' python3 setup_admin.py
flask --app app run --debug   # http://127.0.0.1:5000
```

The database (`EcommerceDB`) is created or upgraded automatically on start. The upgrade only **adds** tables and one column; existing users are kept. Never commit `.env` or the database.

## Environment variables

| Variable | Needed for | Notes |
|---|---|---|
| `SECRET_KEY` | everything | Without it a random key is used and sessions end on every restart. |
| `ECOMMERCE_DATABASE` | optional | Path of the SQLite file. |
| `SESSION_COOKIE_SECURE` | production | Set to `1` only over HTTPS. |
| `APP_BASE_URL` | reset emails | e.g. `http://127.0.0.1:5000`; used to build the reset link. |
| `RESEND_API_KEY`, `RESEND_FROM_EMAIL` | reset emails | See Resend below. |
| `OPENWEATHER_API_KEY` | weather | See OpenWeather below. |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Google sign-in | Button is hidden when empty. |
| `CURRENCY`, `UPLOAD_DIR` | optional | Currency symbol (default `$`) and image folder. |
| `GEOIP_ENABLED` | optional | `0` switches off the IP-based location on the Weather page (default on). |
| `TRUST_PROXY` | hosting | `1` when a proxy/host sits in front of the app so the real visitor IP is used. Turned on automatically on Render. |

### Weather for your current city (IP location)
Opening `/weather` for the first time estimates the visitor's city from their IP address using [ipwho.is](https://ipwho.is) (HTTPS, no key) and shows that city's weather, with a **Change location** button (search by city name) and a **Use my current location** button to go back. The IP is sent to ipwho.is only for the lookup and is never stored; only the city and coordinates are kept in the visitor's session. The estimate is approximate (a VPN or mobile network may show another city). On your own computer the address is `127.0.0.1`, so the lookup uses your machine's public address instead. Behind a host such as Render the real visitor IP comes from `X-Forwarded-For`; `TRUST_PROXY` (automatic on Render) enables that.

### Resend (password reset emails)
1. Create an account at resend.com, then **API Keys → Create API Key**; put it in `RESEND_API_KEY`.
2. For testing set `RESEND_FROM_EMAIL=LumaMarket <onboarding@resend.dev>`. In this sandbox Resend only delivers to the email address of your own Resend account. To email anyone, verify a domain under **Domains** and use an address on it.
3. With `FLASK_DEBUG=1`, if sending fails the reset link is also printed in the terminal (development only).

### OpenWeather
Create an account at openweathermap.org, copy the key from **API keys** into `OPENWEATHER_API_KEY`. New keys can take up to a couple of hours to activate (until then the page shows a "service could not answer" message). The app uses the free *Current weather data* endpoint.

### Google OAuth
1. [Google Cloud Console](https://console.cloud.google.com/) → **APIs & Services → OAuth consent screen** (External; add yourself as a test user).
2. **Credentials → Create credentials → OAuth client ID → Web application**.
3. Authorized redirect URI (exactly): `http://127.0.0.1:5000/auth/google/callback`.
4. Put the client id and secret in `.env` and open the app at `http://127.0.0.1:5000`.

New Google users choose Buyer or Seller (never Admin). They have no password (`has_password = 0`); they can set one on **Profile → Set a password** or through **Forgot password**.

## Features and routes

| Area | Routes | Who |
|---|---|---|
| Auth | `/register`, `/confirm-email`, `POST /confirm-email/resend`, `/login`, `POST /logout`, `/forgot-password`, `/reset-password/<token>`, `/auth/google…` | public |
| Profile | `/profile`, `POST /profile/password` | signed in |
| Catalogue | `/products`, `/products/<id>` (image gallery), `/uploads/products/<file>` | public |
| Seller | `/seller/dashboard`, `/seller/products/new`, `/seller/products/<id>/edit`, `POST …/status` | Seller (own products only) |
| Buyer | `/cart`, `/cart/drawer`, `POST /cart/add`, `/cart/update/<id>`, `/cart/remove/<id>`, `/checkout`, `/orders`, `/orders/<id>` | Buyer |
| Weather | `/weather`, `POST /weather/detect` | public |
| Admin | `/admin/dashboard`, `POST /admin/users/<id>`, `POST /admin/admins`, `/admin/weather`, `/admin/products`, `POST /admin/products/<id>/choice` | Admin |

### Email confirmation at sign-up
Creating an account sends a **6-digit code** (Resend) to the address; the account cannot sign in until the code is entered at `/confirm-email`. Codes work for 15 minutes, once, allow 5 wrong tries, and only an HMAC of the code is stored. A new code can be requested after 60 seconds (5 per hour) and cancels the older one. Signing in with the right password on an unconfirmed account sends a fresh code instead of letting the user in. Accounts that existed before this feature, admin-created accounts and Google accounts count as confirmed. Changing the email on the profile page keeps the old address until the new one's code is entered. Registering again with an address that was never confirmed replaces that unconfirmed sign-up, and Google sign-in on such an address discards the unproven password. Resetting a password through the emailed link also confirms the address.

### Security measures
Hashed passwords (Werkzeug scrypt); CSRF token on every POST; server-side role checks on every protected route; reset tokens are random, stored only as SHA-256 hashes, expire after 30 minutes, are single use (claimed atomically) and a new request cancels older links; the forgot-password answer is identical whether or not the email exists; reset requests are limited to 3 per email and 10 per IP per hour, sign-in to 10 failures per 15 minutes; password rules (8–128 chars, a letter and a number); image uploads are checked by their real bytes (PNG/JPG/GIF/WebP, ≤ 2 MB) and saved under random names; SQL uses parameters only; foreign keys and CHECK constraints are on; no-store cache headers so Back never shows a signed-out dashboard.

### Images, badges and the side cart
- **Several images per product** (up to 6, 2 MB each, PNG/JPG/GIF/WebP checked by real file content). The first is the cover; sellers can pick another cover or remove pictures when editing. One bad file rejects the whole upload and nothing is kept. Products that had one image are migrated automatically.
- **Badges** are never invented: *-N% off*, *New* (14 days), *Best seller* (5+ units sold) and *Only N left* (5 or fewer) are computed from real data; *Free shipping* is declared by the seller (the shop does not charge shipping fees, so the badge states the seller's promise); *LumaMarket's Choice* can only be awarded by an Admin at `/admin/products`.
- **Add to cart** buttons (with a cart icon) are on every product card and product page. For Buyers the item is added in the background and a **side cart** slides in with *Continue to checkout* and *Continue shopping*, so the page never jumps away. Quantity and Remove work inside the side cart. It is keyboard accessible (focus is moved in and kept inside, Esc closes, focus returns to the button) and without JavaScript the same forms fall back to the full cart page.
- Screenshots of these screens (taken from a throw-away demo database) are in `docs/screenshots/`.

### Pricing and promotions
Prices are whole cents. A product has a regular price and an optional sale price (must be lower) with optional start/end dates (inclusive). The sale applies only while valid and inside its dates; the discount percentage is `round((regular − sale) / regular × 100)` and is hidden if it would be under 1%. The cart, checkout and order totals are computed on the server from the database; orders store the price actually paid.

### Checkout and stock
Checkout is simulated (no card fields; a tick-box confirms that). It runs in one `BEGIN IMMEDIATE` transaction: each product's stock is reduced with `UPDATE … WHERE stock >= quantity`, so the last item cannot be sold twice; if any line fails the whole order is rolled back.

## Database (ER diagram)

```mermaid
erDiagram
    users ||--o{ products : "sells (seller_id)"
    users ||--o{ cart_items : has
    products ||--o{ cart_items : "in"
    products ||--o{ product_images : "has pictures"
    users ||--o{ orders : "places (buyer_id)"
    orders ||--|{ order_items : contains
    products ||--o{ order_items : "sold as"
    users ||--o{ password_resets : requests
    users ||--o{ email_codes : "confirms with"
    users |o--o{ weather_searches : "performs (optional)"
    users { int id PK  text email UK  text password_hash  int has_password  int email_verified  text role  int is_active }
    products { int id PK  int seller_id FK  text name  int price_cents  int sale_price_cents  text sale_start  text sale_end  int stock  text image  int free_shipping  int is_choice  int is_active }
    product_images { int id PK  int product_id FK  text filename  int position }
    cart_items { int user_id PK,FK  int product_id PK,FK  int quantity }
    orders { int id PK  int buyer_id FK  int total_cents  text status  text shipping_name  text shipping_address }
    order_items { int id PK  int order_id FK  int product_id FK  int seller_id FK  text product_name  int unit_price_cents  int quantity }
    email_codes { int id PK  int user_id FK  text purpose  text email  text code_hash  int attempts  text expires_at  text used_at }
    password_resets { int id PK  int user_id FK  text token_hash UK  text expires_at  text used_at }
    weather_searches { int id PK  int user_id FK  text search_type  text query  text place  int success  text error }
    throttle { int id PK  text kind  text key  text created_at }
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```
The tests run on a temporary database and a temporary upload folder (set in `tests/__init__.py`; `wipe_database` refuses to run on anything else), and call neither Resend nor OpenWeather (both are mocked). All tests currently pass (run the command to see the count). They cover: CSRF rejection; role redirects and unauthorized dashboard/admin/seller/cart/order access; login throttling; password rules; reset flow, expiry, reuse, hashing, throttling, no email enumeration, inactive accounts, Google-only accounts; Resend request format; product validation (bad prices/stock/dates/images), database constraints, cross-seller edit attempts; promotions and date windows; cart limits and persistence; checkout totals, stock, rollback, concurrent last-item purchase, no card fields; multiple images (limits, cover, removal, cleanup, migration), badge rules, Admin-only Choice badge, side-cart JSON endpoints and button visibility by role; weather success/validation/not-found/network/API errors, history persistence, admin-only statistics, rate limit.

## Manual test plan / demonstration sequence
1. **Register** a Seller and a Buyer (`/register`); sign in, note the different dashboards; sign out and press Back (login page, not a dashboard).
2. **Unauthorized access**: signed out, open `/admin/dashboard` (redirect); as Buyer open `/seller/products/new` and `/admin/dashboard` (403).
3. **Forgot password**: `/forgot-password` with your Resend-account email → open the email link → set a new password → reuse the same link (rejected) → wait 30 minutes or edit `expires_at` to see expiry. Try an unknown email: same message.
4. **Seller**: add a product with an image, a sale price and dates; try price `0`, a `.txt` renamed to `.png`, and a sale price above the price (all rejected). Sign in as another Seller and open `/seller/products/<id>/edit` (403).
5. **Buyer**: browse and search, add to cart, try a quantity above stock (limited), change quantities, check out (tick the simulated-payment box), see the confirmation; confirm the stock fell and the Seller dashboard lists the sale.
6. **Weather** (no login): search `Kigali`; try an empty field, `Xyzzyq` (not found), and disconnect the network (error with retry).
7. **Admin**: open **Weather search history** for the searches and usage statistics; manage users on the dashboard.

## HCI design decisions
1. **Visible feedback for every action** – flash messages (errors announced with `role="alert"`), a loading message while weather loads, and a confirmation page after ordering (visibility of system status).
2. **Error prevention before correction** – number inputs with min/max, price patterns, quantity capped at stock, and a confirm dialog before an Admin changes a role or deactivates an account.
3. **Honest, plain error messages** – "We could not find that place" vs "We could not reach the weather service", each with a next step (recognise, diagnose, recover).
4. **Consistency and familiarity** – one header/nav on every page, the same card, table and form styles, a struck-through old price with a "-25%" badge as on common shops.
5. **Accessibility** – labels on all inputs, skip link, visible focus ring, colour never the only signal (stock and sale states also use text), semantic tables that stack on phones, `aria-live` regions.
6. **Safe simulation** – the checkout states clearly that no real payment is taken and asks for no card data, so users are not misled.
7. **Privacy-preserving wording** – "If an account exists, we sent a link" protects users from account probing.

## Known limitations
- Email codes are 6 digits, so security rests on the 5-try limit and 15-minute expiry; an attacker who copied the database *and* `SECRET_KEY` could work out an unexpired code.
- Sessions store only the user id, so changing a password does not sign out other devices.
- The Resend sandbox sender only delivers to your own Resend account address until a domain is verified.
- Accounts created with Google before this update are not flagged `has_password = 0`; they can still use **Forgot password** to set a password.
- Orders have a single status ("Confirmed"); no shipping workflow, refunds or seller order management beyond the sales list.
- Rate limits are keyed on the client IP as the server sees it; behind a proxy configure it to pass the real IP.
- IP-based location is only approximate and depends on a third-party service (ipwho.is); if it is unavailable the page falls back to the search form.
