"""Products, promotions, the cart, checkout and orders.

Every price is stored as whole cents. Prices, stock and totals are always worked out on the
server from the database; the browser only ever sends product ids and quantities.
"""
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from config import CURRENCY, MAX_UPLOAD_BYTES, UPLOAD_DIR
from functions import connect_db

MAX_PRICE_CENTS = 100_000_000      # 1,000,000.00
MAX_STOCK = 100_000
MAX_CART_QUANTITY = 99
PAGE_SIZE = 12

IMAGE_TYPES = (  # (first bytes, extension)
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


class CheckoutError(Exception):
    """Raised with a message that is safe to show to the buyer."""


# --- Formatting and pricing --------------------------------------------------
def money(cents):
    return f"{CURRENCY}{int(cents) / 100:,.2f}"


def parse_price(text):
    """'12.50' -> 1250. Returns None when the text is not a positive price with at most 2 decimals."""
    text = str(text).strip()
    if not re.fullmatch(r"\d{1,9}(\.\d{1,2})?", text):  # plain digits only: no signs, commas or 1e3
        return None
    cents = int(Decimal(text) * 100)
    return cents if 0 < cents <= MAX_PRICE_CENTS else None


def parse_whole_number(text, low, high):
    text = str(text).strip()
    if not text.isdigit():
        return None
    number = int(text)
    return number if low <= number <= high else None


def parse_date(text):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def price_info(product, today=None):
    """What the buyer pays right now for one unit, and whether a promotion applies.

    A promotion applies only when sale_price is below the regular price and today falls
    inside the optional start/end dates (both ends included).
    """
    today = (today or date.today()).isoformat()
    regular = product["price_cents"]
    sale = product["sale_price_cents"]
    starts, ends = product["sale_start"], product["sale_end"]
    valid_sale = sale is not None and 0 < sale < regular
    on_sale = bool(valid_sale and (not starts or starts <= today) and (not ends or today <= ends))
    current = sale if on_sale else regular
    percent = round((regular - sale) * 100 / regular) if on_sale else 0
    return {
        "current_cents": current,
        "regular_cents": regular,
        "on_sale": on_sale,
        "percent_off": percent if percent >= 1 else 0,
        "upcoming": bool(valid_sale and starts and starts > today),
    }


# --- Validating a product form -----------------------------------------------
def validate_product_form(form):
    """Return (clean_values, errors). Nothing from the browser is trusted."""
    errors = []
    name = form.get("name", "").strip()
    description = form.get("description", "").strip()
    price = parse_price(form.get("price", ""))
    stock = parse_whole_number(form.get("stock", ""), 0, MAX_STOCK)
    sale_text = form.get("sale_price", "").strip()
    sale = parse_price(sale_text) if sale_text else None
    start_text, end_text = form.get("sale_start", "").strip(), form.get("sale_end", "").strip()
    start = parse_date(start_text) if start_text else None
    end = parse_date(end_text) if end_text else None

    if not 2 <= len(name) <= 120:
        errors.append("Product name must be between 2 and 120 characters.")
    if len(description) > 2000:
        errors.append("Description must be 2000 characters or fewer.")
    if price is None:
        errors.append(f"Enter a valid price greater than 0 (at most 2 decimals, up to {MAX_PRICE_CENTS // 100:,}).")
    if stock is None:
        errors.append(f"Stock must be a whole number from 0 to {MAX_STOCK:,}.")
    if sale_text and sale is None:
        errors.append("Sale price must be a valid price greater than 0.")
    if sale is not None and price is not None and sale >= price:
        errors.append("Sale price must be lower than the regular price.")
    if start_text and start is None:
        errors.append("Sale start date is not a valid date.")
    if end_text and end is None:
        errors.append("Sale end date is not a valid date.")
    if start and end and end < start:
        errors.append("Sale end date cannot be before the start date.")
    if (start_text or end_text) and not sale_text:
        errors.append("Enter a sale price, or clear the sale dates.")

    clean = {
        "name": name, "description": description, "price_cents": price, "stock": stock,
        "sale_price_cents": sale if sale_text else None,
        "sale_start": start.isoformat() if start else None,
        "sale_end": end.isoformat() if end else None,
    }
    return clean, errors


# --- Image uploads -----------------------------------------------------------
def save_image(file_storage):
    """Check the real file contents (not just the name), store it under a random name, return that name."""
    data = file_storage.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ValueError("The image file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"The image is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    extension = next((ext for magic, ext in IMAGE_TYPES if data.startswith(magic)), None)
    if extension is None and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        extension = ".webp"
    if extension is None:
        raise ValueError("Please upload a PNG, JPG, GIF or WebP image.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = uuid.uuid4().hex + extension
    (UPLOAD_DIR / filename).write_bytes(data)
    return filename


def delete_image(filename):
    if filename and "/" not in filename and "\\" not in filename:
        try:
            (UPLOAD_DIR / filename).unlink()
        except OSError:
            pass


# --- Products ----------------------------------------------------------------
def _one(sql, params=()):
    connection = connect_db()
    try:
        return connection.execute(sql, params).fetchone()
    finally:
        connection.close()


def _all(sql, params=()):
    connection = connect_db()
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def get_product(product_id):
    return _one(
        """SELECT p.*, u.name AS seller_name FROM products p
           JOIN users u ON u.id = p.seller_id WHERE p.id = ?""", (product_id,))


def search_catalogue(search="", page=1):
    """Active products from active sellers, newest first. Returns (rows, total_count)."""
    where = "p.is_active = 1 AND u.is_active = 1"
    params = []
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND (p.name LIKE ? ESCAPE '\\' OR p.description LIKE ? ESCAPE '\\')"
        params += [f"%{escaped}%"] * 2
    total = _one(f"SELECT COUNT(*) FROM products p JOIN users u ON u.id = p.seller_id WHERE {where}", params)[0]
    rows = _all(
        f"""SELECT p.*, u.name AS seller_name FROM products p JOIN users u ON u.id = p.seller_id
            WHERE {where} ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?""",
        params + [PAGE_SIZE, (max(page, 1) - 1) * PAGE_SIZE])
    return rows, total


def latest_products(limit=4):
    return search_catalogue()[0][:limit]


def seller_products(seller_id):
    return _all("SELECT * FROM products WHERE seller_id = ? ORDER BY created_at DESC, id DESC", (seller_id,))


def create_product(seller_id, values, image):
    connection = connect_db()
    try:
        cursor = connection.execute(
            """INSERT INTO products (seller_id, name, description, price_cents, sale_price_cents,
                                     sale_start, sale_end, stock, image)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (seller_id, values["name"], values["description"], values["price_cents"],
             values["sale_price_cents"], values["sale_start"], values["sale_end"], values["stock"], image))
        connection.commit()
        return cursor.lastrowid
    finally:
        connection.close()


def update_product(product_id, seller_id, values, image):
    """The seller_id in the WHERE clause means nobody can edit another seller's product, even by guessing ids."""
    connection = connect_db()
    try:
        cursor = connection.execute(
            """UPDATE products SET name = ?, description = ?, price_cents = ?, sale_price_cents = ?,
                      sale_start = ?, sale_end = ?, stock = ?, image = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND seller_id = ?""",
            (values["name"], values["description"], values["price_cents"], values["sale_price_cents"],
             values["sale_start"], values["sale_end"], values["stock"], image, product_id, seller_id))
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def set_product_active(product_id, seller_id, active):
    connection = connect_db()
    try:
        cursor = connection.execute(
            "UPDATE products SET is_active = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND seller_id = ?",
            (1 if active else 0, product_id, seller_id))
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


# --- Cart ----------------------------------------------------------------------
def get_cart(user_id):
    """Cart lines with current product data, prices worked out on the server."""
    rows = _all(
        """SELECT c.quantity, p.*, u.is_active AS seller_active FROM cart_items c
           JOIN products p ON p.id = c.product_id JOIN users u ON u.id = p.seller_id
           WHERE c.user_id = ? ORDER BY p.name""", (user_id,))
    lines, total, problems = [], 0, 0
    for row in rows:
        info = price_info(row)
        available = bool(row["is_active"] and row["seller_active"])
        issue = None
        if not available:
            issue = "No longer available"
        elif row["stock"] == 0:
            issue = "Out of stock"
        elif row["quantity"] > row["stock"]:
            issue = f"Only {row['stock']} left in stock"
        line_total = info["current_cents"] * row["quantity"]
        if not issue:
            total += line_total
        else:
            problems += 1
        lines.append({"product": row, "quantity": row["quantity"], "price": info,
                      "line_total": line_total, "issue": issue})
    return {"lines": lines, "total_cents": total, "has_problems": problems > 0,
            "count": sum(line["quantity"] for line in lines)}


def cart_count(user_id):
    return _one("SELECT COALESCE(SUM(quantity), 0) FROM cart_items WHERE user_id = ?", (user_id,))[0]


def add_to_cart(user_id, product_id, quantity):
    """Returns (ok, message). The quantity in the cart can never exceed the stock on hand."""
    connection = connect_db()
    try:
        product = connection.execute(
            """SELECT p.* FROM products p JOIN users u ON u.id = p.seller_id
               WHERE p.id = ? AND p.is_active = 1 AND u.is_active = 1""", (product_id,)).fetchone()
        if product is None:
            return False, "That product is not available."
        if product["stock"] <= 0:
            return False, f"{product['name']} is out of stock."
        existing = connection.execute(
            "SELECT quantity FROM cart_items WHERE user_id = ? AND product_id = ?", (user_id, product_id)).fetchone()
        wanted = (existing["quantity"] if existing else 0) + quantity
        final = min(wanted, product["stock"], MAX_CART_QUANTITY)
        connection.execute(
            """INSERT INTO cart_items (user_id, product_id, quantity) VALUES (?, ?, ?)
               ON CONFLICT(user_id, product_id) DO UPDATE SET quantity = excluded.quantity""",
            (user_id, product_id, final))
        connection.commit()
        if final < wanted:
            return True, f"Only {final} of {product['name']} can be added (stock limit). Your cart now has {final}."
        return True, f"Added {product['name']} to your cart."
    finally:
        connection.close()


def set_cart_quantity(user_id, product_id, quantity):
    """quantity 0 removes the line. Returns a message describing what happened."""
    connection = connect_db()
    try:
        if quantity <= 0:
            connection.execute("DELETE FROM cart_items WHERE user_id = ? AND product_id = ?", (user_id, product_id))
            connection.commit()
            return "Item removed from your cart."
        product = connection.execute("SELECT name, stock FROM products WHERE id = ?", (product_id,)).fetchone()
        if product is None:
            return "That product is not available."
        final = min(quantity, product["stock"], MAX_CART_QUANTITY)
        if final <= 0:
            connection.execute("DELETE FROM cart_items WHERE user_id = ? AND product_id = ?", (user_id, product_id))
            connection.commit()
            return f"{product['name']} is out of stock and was removed from your cart."
        connection.execute(
            "UPDATE cart_items SET quantity = ? WHERE user_id = ? AND product_id = ?", (final, user_id, product_id))
        connection.commit()
        if final < quantity:
            return f"Only {final} of {product['name']} in stock. Quantity set to {final}."
        return "Cart updated."
    finally:
        connection.close()


# --- Checkout and orders ---------------------------------------------------------
def checkout(user_id, shipping_name, shipping_address):
    """Turn the cart into an order. All or nothing: if any line fails, nothing is saved and no stock changes.

    BEGIN IMMEDIATE takes the write lock first, and each stock update only succeeds while enough
    stock remains, so two buyers can never both buy the last item.
    """
    connection = connect_db()
    connection.isolation_level = None  # manage the transaction by hand
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """SELECT c.quantity, p.*, u.is_active AS seller_active FROM cart_items c
               JOIN products p ON p.id = c.product_id JOIN users u ON u.id = p.seller_id
               WHERE c.user_id = ?""", (user_id,)).fetchall()
        if not rows:
            raise CheckoutError("Your cart is empty.")

        total, lines = 0, []
        for row in rows:
            if not row["is_active"] or not row["seller_active"]:
                raise CheckoutError(f"{row['name']} is no longer available. Remove it from your cart to continue.")
            unit = price_info(row)["current_cents"]
            updated = connection.execute(
                "UPDATE products SET stock = stock - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND stock >= ?",
                (row["quantity"], row["id"], row["quantity"]))
            if updated.rowcount != 1:
                raise CheckoutError(f"Not enough stock for {row['name']}. Please lower the quantity and try again.")
            total += unit * row["quantity"]
            lines.append((row, unit))

        order_id = connection.execute(
            "INSERT INTO orders (buyer_id, total_cents, shipping_name, shipping_address) VALUES (?, ?, ?, ?)",
            (user_id, total, shipping_name, shipping_address)).lastrowid
        for row, unit in lines:
            connection.execute(
                """INSERT INTO order_items (order_id, product_id, seller_id, product_name, unit_price_cents, quantity)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_id, row["id"], row["seller_id"], row["name"], unit, row["quantity"]))
        connection.execute("DELETE FROM cart_items WHERE user_id = ?", (user_id,))
        connection.execute("COMMIT")
        return order_id
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def buyer_orders(user_id):
    return _all(
        """SELECT o.*, (SELECT COALESCE(SUM(quantity), 0) FROM order_items WHERE order_id = o.id) AS item_count
           FROM orders o WHERE buyer_id = ? ORDER BY o.created_at DESC, o.id DESC""", (user_id,))


def get_order(order_id, buyer_id):
    order = _one("SELECT * FROM orders WHERE id = ? AND buyer_id = ?", (order_id, buyer_id))
    if order is None:
        return None, []
    return order, _all("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))


def seller_sales(seller_id, limit=20):
    return _all(
        """SELECT oi.*, o.created_at, o.status, u.name AS buyer_name FROM order_items oi
           JOIN orders o ON o.id = oi.order_id JOIN users u ON u.id = o.buyer_id
           WHERE oi.seller_id = ? ORDER BY o.created_at DESC, oi.id DESC LIMIT ?""", (seller_id, limit))
