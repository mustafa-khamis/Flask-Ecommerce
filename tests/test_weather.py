import io
import json
import unittest
import urllib.error
from unittest import mock

import weather as weather_module
from tests.helpers import AppTestCase

SAMPLE = {
    "name": "Kigali", "sys": {"country": "RW"}, "dt": 1790000000, "timezone": 7200,
    "main": {"temp": 21.46, "feels_like": 21.9, "humidity": 64},
    "weather": [{"main": "Clouds", "description": "scattered clouds", "icon": "03d"}],
    "wind": {"speed": 3.1},
}


def fake_response(payload):
    response = mock.MagicMock()
    response.__enter__.return_value = io.BytesIO(json.dumps(payload).encode())
    return response


def http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"{}"))


class WeatherTests(AppTestCase):
    def search(self, query, client=None):
        return (client or self.client).get("/weather?" + query)

    def test_public_page_loads_without_login(self):
        page = self.client.get("/weather")
        self.assertEqual(page.status_code, 200)
        self.assertIn("No search yet", page.get_data(as_text=True))
        self.assertIn("data-loading-form", page.get_data(as_text=True))  # loading state hook

    def test_city_search_shows_all_required_fields(self):
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)) as opened:
            response = self.search("mode=city&city=Kigali")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        for expected in ("Kigali, RW", "21.5", "Scattered clouds", "64%", "3.1 m/s", "03d@2x.png", "2026"):
            self.assertIn(expected, page)
        url = opened.call_args[0][0]
        self.assertIn("q=Kigali", url)
        self.assertIn("appid=owm_test", url)

    def test_coordinate_search(self):
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)) as opened:
            response = self.search("mode=coordinates&lat=-1.9441&lon=30.0619")
        self.assertEqual(response.status_code, 200)
        self.assertIn("lat=-1.9441", opened.call_args[0][0])
        self.assertIn("lon=30.0619", opened.call_args[0][0])

    def test_validation_errors_never_call_the_api(self):
        bad = ["mode=city&city=", "mode=city&city=a", "mode=city&city=12345", "mode=city&city=<script>",
               "mode=coordinates&lat=91&lon=0", "mode=coordinates&lat=0&lon=181",
               "mode=coordinates&lat=abc&lon=0", "mode=coordinates&lat=nan&lon=0", "mode=coordinates&lat=&lon="]
        with mock.patch("weather.urllib.request.urlopen") as opened:
            for query in bad:
                self.assertEqual(self.search(query).status_code, 400, query)
        opened.assert_not_called()

    def test_not_found(self):
        with mock.patch("weather.urllib.request.urlopen", side_effect=http_error(404)):
            response = self.search("mode=city&city=Nowhereville")
        self.assertEqual(response.status_code, 404)
        self.assertIn("could not find that place", response.get_data(as_text=True))

    def test_network_failure_and_timeout(self):
        for failure in (urllib.error.URLError("offline"), TimeoutError()):
            with mock.patch("weather.urllib.request.urlopen", side_effect=failure):
                response = self.search("mode=city&city=Kigali")
            self.assertEqual(response.status_code, 502)
            self.assertIn("could not reach the weather service", response.get_data(as_text=True))

    def test_api_errors_do_not_leak_details(self):
        for code in (401, 429, 500):
            with mock.patch("weather.urllib.request.urlopen", side_effect=http_error(code)):
                response = self.search("mode=city&city=Kigali")
            self.assertEqual(response.status_code, 503, code)
            self.assertNotIn("owm_test", response.get_data(as_text=True))

    def test_malformed_api_answer(self):
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response({"unexpected": 1})):
            self.assertEqual(self.search("mode=city&city=Kigali").status_code, 503)

    def test_missing_api_key_is_a_friendly_error(self):
        with mock.patch.object(weather_module, "OPENWEATHER_API_KEY", ""):
            response = self.search("mode=city&city=Kigali")
        self.assertEqual(response.status_code, 503)
        self.assertIn("not configured", response.get_data(as_text=True))

    def test_history_is_persisted_for_successes_and_failures(self):
        self.make_user("buyer@example.com", "Buyer", "Bob")
        client = self.client_for("buyer@example.com")
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            self.search("mode=city&city=Kigali")
            self.search("mode=city&city=Kigali", client=client)
        with mock.patch("weather.urllib.request.urlopen", side_effect=http_error(404)):
            self.search("mode=city&city=Zzzzz")
        self.search("mode=city&city=1")  # validation failure is recorded too
        rows = self.query("SELECT user_id, success, place, query FROM weather_searches ORDER BY id")
        self.assertEqual([r["success"] for r in rows], [1, 1, 0, 0])
        self.assertIsNone(rows[0]["user_id"])
        self.assertIsNotNone(rows[1]["user_id"])
        self.assertEqual(rows[0]["place"], "Kigali")

    def test_history_survives_a_fresh_connection(self):
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            self.search("mode=city&city=Kigali")
        self.assertEqual(len(weather_module.recent_searches()), 1)


class AdminWeatherTests(AppTestCase):
    def test_only_admin_can_view_history(self):
        self.make_user("admin@example.com", "Admin", "Ada")
        self.make_user("buyer@example.com", "Buyer", "Bob")
        self.make_user("seller@example.com", "Seller", "Sam")
        self.assertEqual(self.client.get("/admin/weather").status_code, 302)
        self.assertEqual(self.client_for("buyer@example.com").get("/admin/weather").status_code, 403)
        self.assertEqual(self.client_for("seller@example.com").get("/admin/weather").status_code, 403)
        self.assertEqual(self.client_for("admin@example.com").get("/admin/weather").status_code, 200)

    def test_statistics_match_the_stored_searches(self):
        self.make_user("admin@example.com", "Admin", "Ada")
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            for _ in range(3):
                self.client.get("/weather?mode=city&city=Kigali")
        with mock.patch("weather.urllib.request.urlopen", side_effect=http_error(404)):
            self.client.get("/weather?mode=city&city=Nowhere")
        stats = weather_module.usage_stats()
        self.assertEqual((stats["total"], stats["succeeded"], stats["failed"], stats["success_rate"]), (4, 3, 1, 75))
        self.assertEqual(tuple(stats["top_places"][0]), ("Kigali", "RW", 3))
        page = self.client_for("admin@example.com").get("/admin/weather").get_data(as_text=True)
        self.assertIn("75%", page)
        self.assertIn("Kigali, RW", page)

    def test_empty_history_state(self):
        self.make_user("admin@example.com", "Admin", "Ada")
        page = self.client_for("admin@example.com").get("/admin/weather").get_data(as_text=True)
        self.assertIn("No searches yet", page)

    def test_searches_are_rate_limited_per_ip(self):
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            codes = [self.client.get("/weather?mode=city&city=Kigali").status_code for _ in range(32)]
        self.assertEqual(codes[:30], [200] * 30)
        self.assertEqual(codes[30:], [503, 503])


if __name__ == "__main__":
    unittest.main()
