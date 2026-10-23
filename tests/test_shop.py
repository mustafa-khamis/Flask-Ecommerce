import io
import threading
import unittest
from datetime import date, timedelta

import shop
from tests.helpers import AppTestCase, app_module

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def product_form(**overrides):
    data = {"name": "Blue Mug", "description": "A mug", "price": "10.00", "stock": "5",
            "sale_price": "", "sale_start": "", "sale_end": ""}
    data.update(overrides)
    return data


class ShopBase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_user("seller@example.com", "Seller", "Sally Seller")
        self.make_user("seller2@example.com", "Seller", "Other Seller")
        self.make_user("buyer@example.com", "Buyer", "Bob Buyer")
        self.make_user("buyer2@example.com", "Buyer", "Bea Buyer")
        self.seller = self.client_for("seller@example.com")
        self.seller2 = self.client_for("seller2@example.com")
        self.buyer = self.client_for("buyer@example.com")
        self.buyer2 = self.client_for("buyer2@example.com")

    def add_product(self, client=None, **overrides):
        response = self.post("/seller/products/new", product_form(**overrides), client=client or self.seller,
                             token_path="/seller/products/new")
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True)[:500])
        return int(response.headers["Location"].rsplit("/", 1)[1])

    def add_to_cart(self, product_id, quantity=1, client=None):
        return self.post("/cart/add", {"product_id": product_id, "quantity": quantity},
                         client=client or self.buyer, token_path="/cart")

    def stock(self, product_id):
        return self.query("SELECT stock FROM products WHERE id = ?", (product_id,))[0][0]


class ProductTests(ShopBase):
    def test_seller_creates_product_and_it_appears_in_catalogue(self):
        pid = self.add_product()
        page = self.client.get("/products").get_data(as_text=True)
        self.assertIn("Blue Mug", page)
        self.assertIn("$10.00", page)
        self.assertEqual(self.client.get(f"/products/{pid}").status_code, 200)

    def test_invalid_products_are_rejected(self):
        bad = [dict(price="0"), dict(price="-5"), dict(price="abc"), dict(price="1.999"), dict(price="1e3"),
               dict(stock="-1"), dict(stock="1.5"), dict(stock="abc"), dict(stock="999999999"),
               dict(name="x"), dict(price="10000000"),
               dict(sale_price="10.00"), dict(sale_price="12.00"), dict(sale_price="0"),
               dict(sale_start="2026-01-01"),  # dates without a sale price
               dict(sale_price="5", sale_start="2026-05-02", sale_end="2026-05-01"),
               dict(sale_price="5", sale_start="not-a-date")]
        for overrides in bad:
            response = self.post("/seller/products/new", product_form(**overrides), client=self.seller,
                                 token_path="/seller/products/new")
            self.assertEqual(response.status_code, 400, overrides)
        self.assertEqual(self.query("SELECT COUNT(*) FROM products")[0][0], 0)

    def test_database_constraints_reject_bad_rows_even_without_the_form(self):
        sid = self.query("SELECT id FROM users WHERE email='seller@example.com'")[0][0]
        import sqlite3
        for price, sale, stock in [(0, None, 1), (1000, 1000, 1), (1000, 2000, 1), (1000, None, -1)]:
            with self.assertRaises(sqlite3.IntegrityError):
                self.execute("INSERT INTO products (seller_id, name, price_cents, sale_price_cents, stock) "
                             "VALUES (?, 'Thing', ?, ?, ?)", (sid, price, sale, stock))

    def test_buyers_and_anonymous_cannot_create_products(self):
        self.assertEqual(self.client.get("/seller/products/new").status_code, 302)
        self.assertEqual(self.buyer.get("/seller/products/new").status_code, 403)
        response = self.post("/seller/products/new", product_form(), client=self.buyer, token_path="/cart")
        self.assertEqual(response.status_code, 403)

    def test_seller_cannot_modify_another_sellers_product(self):
        pid = self.add_product()
        self.assertEqual(self.seller2.get(f"/seller/products/{pid}/edit").status_code, 403)
        response = self.post(f"/seller/products/{pid}/edit", product_form(name="Hacked", price="0.01"),
                             client=self.seller2, token_path="/seller/dashboard")
        self.assertEqual(response.status_code, 403)
        response = self.post(f"/seller/products/{pid}/status", {"active": "0"}, client=self.seller2,
                             token_path="/seller/dashboard")
        self.assertEqual(response.status_code, 403)
        row = self.query("SELECT name, price_cents, is_active FROM products")[0]
        self.assertEqual(tuple(row), ("Blue Mug", 1000, 1))
        # the SQL itself also refuses
        self.assertFalse(shop.update_product(pid, 999, shop.validate_product_form(product_form())[0], None))

    def test_seller_can_edit_and_hide_own_product(self):
        pid = self.add_product()
        self.post(f"/seller/products/{pid}/edit", product_form(name="Red Mug", stock="9"),
                  client=self.seller, token_path="/seller/dashboard")
        self.assertEqual(tuple(self.query("SELECT name, stock FROM products")[0]), ("Red Mug", 9))
        self.post(f"/seller/products/{pid}/status", {"active": "0"}, client=self.seller, token_path="/seller/dashboard")
        self.assertNotIn("Red Mug", self.client.get("/products").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/products/{pid}").status_code, 404)
        self.assertEqual(self.seller.get(f"/products/{pid}").status_code, 200)

    def test_image_upload_validates_real_content(self):
        good = self.post("/seller/products/new",
                         {**product_form(), "image": (io.BytesIO(PNG), "mug.png")},
                         client=self.seller, token_path="/seller/products/new", content_type="multipart/form-data")
        self.assertEqual(good.status_code, 302)
        name = self.query("SELECT image FROM products")[0][0]
        self.assertTrue(name.endswith(".png"))
        self.assertEqual(self.client.get(f"/uploads/products/{name}").status_code, 200)

        for filename, content in [("evil.png", b"<script>alert(1)</script>"), ("x.svg", b"<svg onload=alert(1)>"),
                                  ("big.png", PNG + b"0" * (3 * 1024 * 1024))]:
            response = self.post("/seller/products/new",
                                 {**product_form(name="Other"), "image": (io.BytesIO(content), filename)},
                                 client=self.seller, token_path="/seller/products/new",
                                 content_type="multipart/form-data")
            self.assertIn(response.status_code, (400, 413), filename)
        self.assertEqual(self.query("SELECT COUNT(*) FROM products")[0][0], 1)

    def test_path_traversal_in_image_route_is_blocked(self):
        self.assertEqual(self.client.get("/uploads/products/..%2Fapp.py").status_code, 404)

    def test_search_treats_wildcards_literally(self):
        self.add_product(name="Plain Mug")
        self.add_product(name="100% Cotton Shirt")
        page = self.client.get("/products?q=%25").get_data(as_text=True)
        self.assertIn("100% Cotton Shirt", page)
        self.assertNotIn("Plain Mug", page)


class PromotionTests(ShopBase):
    def row(self, **kw):
        base = {"price_cents": 10000, "sale_price_cents": None, "sale_start": None, "sale_end": None}
        base.update(kw)
        return base

    def test_discount_calculation(self):
        info = shop.price_info(self.row(sale_price_cents=7500))
        self.assertEqual((info["current_cents"], info["on_sale"], info["percent_off"]), (7500, True, 25))
        self.assertEqual(shop.price_info(self.row(sale_price_cents=3333))["percent_off"], 67)

    def test_no_discount_without_valid_sale(self):
        for kw in [dict(), dict(sale_price_cents=10000), dict(sale_price_cents=12000), dict(sale_price_cents=0)]:
            info = shop.price_info(self.row(**kw))
            self.assertFalse(info["on_sale"], kw)
            self.assertEqual(info["current_cents"], 10000)

    def test_date_window_is_inclusive_and_enforced(self):
        today = date(2026, 6, 15)
        in_window = self.row(sale_price_cents=5000, sale_start="2026-06-15", sale_end="2026-06-15")
        self.assertTrue(shop.price_info(in_window, today)["on_sale"])
        self.assertFalse(shop.price_info(in_window, date(2026, 6, 14))["on_sale"])
        self.assertTrue(shop.price_info(in_window, date(2026, 6, 14))["upcoming"])
        self.assertFalse(shop.price_info(in_window, date(2026, 6, 16))["on_sale"])

    def test_checkout_charges_server_side_sale_price_only_inside_window(self):
        today = date.today()
        on = self.add_product(name="On Sale", sale_price="7.50",
                              sale_start=(today - timedelta(days=1)).isoformat(),
                              sale_end=(today + timedelta(days=1)).isoformat())
        expired = self.add_product(name="Expired Sale", sale_price="1.00",
                                   sale_start=(today - timedelta(days=9)).isoformat(),
                                   sale_end=(today - timedelta(days=2)).isoformat())
        self.add_to_cart(on)
        self.add_to_cart(expired)
        page = self.buyer.get("/cart").get_data(as_text=True)
        self.assertIn("$17.50", page)  # 7.50 + 10.00
        self.assertIn("-25%", self.client.get(f"/products/{on}").get_data(as_text=True))
        self.assertNotIn("-90%", self.client.get(f"/products/{expired}").get_data(as_text=True))


class CartTests(ShopBase):
    def test_cart_requires_buyer_role(self):
        pid = self.add_product()
        self.assertEqual(self.client.get("/cart").status_code, 302)
        self.assertEqual(self.seller.get("/cart").status_code, 403)
        response = self.post("/cart/add", {"product_id": pid, "quantity": 1}, client=self.seller, token_path="/seller/dashboard")
        self.assertEqual(response.status_code, 403)

    def test_cart_persists_across_sessions(self):
        pid = self.add_product()
        self.add_to_cart(pid, 2)
        fresh = self.client_for("buyer@example.com")
        self.assertIn("Blue Mug", fresh.get("/cart").get_data(as_text=True))
        self.assertEqual(self.query("SELECT quantity FROM cart_items")[0][0], 2)

    def test_cannot_exceed_stock_when_adding(self):
        pid = self.add_product(stock="3")
        self.add_to_cart(pid, 2)
        self.add_to_cart(pid, 5)
        self.assertEqual(self.query("SELECT quantity FROM cart_items")[0][0], 3)

    def test_invalid_quantities_rejected(self):
        pid = self.add_product()
        for bad in ("0", "-1", "abc", "1.5", "1000", ""):
            self.add_to_cart(pid, bad)
        self.assertEqual(self.query("SELECT COUNT(*) FROM cart_items")[0][0], 0)

    def test_out_of_stock_cannot_be_added(self):
        pid = self.add_product(stock="0")
        self.add_to_cart(pid, 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM cart_items")[0][0], 0)
        self.assertIn("Out of stock", self.client.get(f"/products/{pid}").get_data(as_text=True))

    def test_update_clamps_to_stock_and_zero_removes(self):
        pid = self.add_product(stock="4")
        self.add_to_cart(pid, 1)
        self.post(f"/cart/update/{pid}", {"quantity": "50"}, client=self.buyer, token_path="/cart")
        self.assertEqual(self.query("SELECT quantity FROM cart_items")[0][0], 4)
        self.post(f"/cart/update/{pid}", {"quantity": "2"}, client=self.buyer, token_path="/cart")
        self.assertEqual(self.query("SELECT quantity FROM cart_items")[0][0], 2)
        self.post(f"/cart/update/{pid}", {"quantity": "0"}, client=self.buyer, token_path="/cart")
        self.assertEqual(self.query("SELECT COUNT(*) FROM cart_items")[0][0], 0)

    def test_totals_are_calculated_on_the_server_and_ignore_posted_prices(self):
        pid = self.add_product(price="12.34")
        self.post("/cart/add", {"product_id": pid, "quantity": 3, "price": "0.01", "total": "0.03"},
                  client=self.buyer, token_path="/cart")
        self.assertIn("$37.02", self.buyer.get("/cart").get_data(as_text=True))

    def test_buyers_carts_are_separate(self):
        pid = self.add_product()
        self.add_to_cart(pid, 1)
        self.assertNotIn("Blue Mug", self.buyer2.get("/cart").get_data(as_text=True))


class CheckoutTests(ShopBase):
    DELIVERY = {"shipping_name": "Bob", "shipping_address": "1 Main Street, Kigali", "confirm_simulated": "1"}

    def checkout(self, client=None, **overrides):
        return self.post("/checkout", {**self.DELIVERY, **overrides}, client=client or self.buyer, token_path="/checkout")

    def test_successful_checkout_saves_order_reduces_stock_and_empties_cart(self):
        a = self.add_product(name="Item A", price="10.00", stock="5")
        b = self.add_product(name="Item B", price="2.50", stock="2")
        self.add_to_cart(a, 2)
        self.add_to_cart(b, 1)
        response = self.checkout()
        self.assertEqual(response.status_code, 302)
        order_id = int(response.headers["Location"].rsplit("/", 1)[1])
        order = self.query("SELECT * FROM orders")[0]
        self.assertEqual(order["total_cents"], 2250)
        self.assertEqual(self.query("SELECT COUNT(*) FROM order_items")[0][0], 2)
        self.assertEqual((self.stock(a), self.stock(b)), (3, 1))
        self.assertEqual(self.query("SELECT COUNT(*) FROM cart_items")[0][0], 0)
        page = self.buyer.get(f"/orders/{order_id}").get_data(as_text=True)
        self.assertIn("confirmed", page)
        self.assertIn("$22.50", page)
        self.assertIn("Item A", self.seller.get("/seller/dashboard").get_data(as_text=True))

    def test_order_keeps_price_paid_after_product_changes(self):
        pid = self.add_product(price="10.00")
        self.add_to_cart(pid, 1)
        order_id = int(self.checkout().headers["Location"].rsplit("/", 1)[1])
        self.post(f"/seller/products/{pid}/edit", product_form(price="99.00", name="Renamed"),
                  client=self.seller, token_path="/seller/dashboard")
        page = self.buyer.get(f"/orders/{order_id}").get_data(as_text=True)
        self.assertIn("$10.00", page)
        self.assertIn("Blue Mug", page)

    def test_cannot_buy_more_than_stock_even_if_stock_dropped_after_adding(self):
        pid = self.add_product(stock="5")
        self.add_to_cart(pid, 5)
        self.execute("UPDATE products SET stock = 2")  # someone else bought 3
        response = self.checkout()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.query("SELECT COUNT(*) FROM orders")[0][0], 0)
        self.assertEqual(self.stock(pid), 2)
        self.assertEqual(self.query("SELECT quantity FROM cart_items")[0][0], 5)

    def test_failed_checkout_is_all_or_nothing(self):
        a = self.add_product(name="Item A", stock="5")
        b = self.add_product(name="Item B", stock="5")
        self.add_to_cart(a, 2)
        self.add_to_cart(b, 2)
        self.execute("UPDATE products SET stock = 1 WHERE id = ?", (b,))
        self.assertEqual(self.checkout().status_code, 400)
        self.assertEqual(self.stock(a), 5)  # first item's stock was rolled back
        self.assertEqual(self.query("SELECT COUNT(*) FROM orders")[0][0], 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM order_items")[0][0], 0)

    def test_hidden_product_blocks_checkout(self):
        pid = self.add_product()
        self.add_to_cart(pid, 1)
        self.post(f"/seller/products/{pid}/status", {"active": "0"}, client=self.seller, token_path="/seller/dashboard")
        self.assertEqual(self.checkout().status_code, 400)
        self.assertEqual(self.query("SELECT COUNT(*) FROM orders")[0][0], 0)

    def test_last_item_cannot_be_sold_twice(self):
        pid = self.add_product(stock="1")
        self.add_to_cart(pid, 1, client=self.buyer)
        self.add_to_cart(pid, 1, client=self.buyer2)
        results = []
        tokens = {id(c): self.token(c, "/checkout") for c in (self.buyer, self.buyer2)}

        def go(client):
            results.append(client.post("/checkout", data={**self.DELIVERY, "csrf_token": tokens[id(client)]}).status_code)

        threads = [threading.Thread(target=go, args=(c,)) for c in (self.buyer, self.buyer2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [302, 400])
        self.assertEqual(self.stock(pid), 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM orders")[0][0], 1)

    def test_validation_and_empty_cart(self):
        pid = self.add_product()
        self.assertEqual(self.buyer.get("/checkout").status_code, 302)  # empty cart
        self.add_to_cart(pid, 1)
        self.assertEqual(self.checkout(shipping_name="").status_code, 400)
        self.assertEqual(self.checkout(shipping_address="x").status_code, 400)
        self.assertEqual(self.checkout(confirm_simulated="").status_code, 400)
        self.assertEqual(self.query("SELECT COUNT(*) FROM orders")[0][0], 0)

    def test_no_card_fields_collected(self):
        pid = self.add_product()
        self.add_to_cart(pid, 1)
        page = self.buyer.get("/checkout").get_data(as_text=True).lower()
        for word in ("card number", "cvv", "cvc", "expiry"):
            self.assertNotIn(word, page)
        self.assertIn("simulated", page)

    def test_buyer_cannot_view_someone_elses_order(self):
        pid = self.add_product()
        self.add_to_cart(pid, 1)
        order_id = int(self.checkout().headers["Location"].rsplit("/", 1)[1])
        self.assertEqual(self.buyer2.get(f"/orders/{order_id}").status_code, 404)
        self.assertEqual(self.seller.get(f"/orders/{order_id}").status_code, 403)
        self.assertEqual(self.client.get(f"/orders/{order_id}").status_code, 302)


if __name__ == "__main__":
    unittest.main()
