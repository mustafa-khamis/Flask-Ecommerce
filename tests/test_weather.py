import io
import json
import unittest
import urllib.error
from unittest import mock

import geolocation
import weather as weather_module
from tests.helpers import AppTestCase

SAMPLE = {
    "name": "Kigali", "coord": {"lat": -1.95, "lon": 30.06}, "sys": {"country": "RW"}, "dt": 1790000000, "timezone": 7200,
    "main": {"temp": 21.46, "feels_like": 21.9, "humidity": 64},
    "weather": [{"main": "Clouds", "description": "scattered clouds", "icon": "03d"}],
    "wind": {"speed": 3.1},
}


def fake_response(payload):
    response = mock.MagicMock()
    # A fresh stream for every call, so one mock can serve several requests.
    response.__enter__.side_effect = lambda: io.BytesIO(json.dumps(payload).encode())
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

    def test_search_form_offers_the_city_name_only(self):
        page = self.client.get("/weather").get_data(as_text=True)
        self.assertIn('id="city"', page)
        for gone in ("Coordinates", 'id="lat"', 'id="lon"', "Latitude", "Longitude"):
            self.assertNotIn(gone, page)
        self.assertIn('name="mode" value="city"', page)
        # even if someone opens an old coordinates link, the form still shows the city box
        old_link = self.client.get("/weather?mode=coordinates").get_data(as_text=True)
        self.assertIn('id="city"', old_link)
        self.assertNotIn('id="lat"', old_link)

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


IP_ANSWER = {"success": True, "city": "Kigali", "country_code": "RW", "latitude": -1.9499, "longitude": 30.0588}


class IpLocationTests(AppTestCase):
    """Weather for the visitor's own city, estimated from their IP address."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(geolocation, "GEOIP_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def visit(self, path="/weather", client=None, ip=None, answer=IP_ANSWER, weather=SAMPLE):
        """Open a page with both outside services mocked. Returns (response, geo mock, weather mock)."""
        with mock.patch("geolocation.fetch_json", return_value=answer) as geo, \
                mock.patch("weather.urllib.request.urlopen", return_value=fake_response(weather)) as owm:
            kwargs = {"environ_overrides": {"REMOTE_ADDR": ip}} if ip else {}
            response = (client or self.client).get(path, **kwargs)
        return response, geo, owm

    def test_first_visit_detects_the_city_and_shows_its_weather(self):
        response, geo, owm = self.visit()
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        geo.assert_called_once()
        self.assertIn("lat=-1.9499", owm.call_args[0][0])      # weather is fetched by the detected coordinates
        self.assertIn("lon=30.0588", owm.call_args[0][0])
        self.assertIn("Weather in Kigali, RW", page)
        self.assertIn("Detected from your internet connection", page)
        self.assertIn("Change location", page)
        self.assertIn("21.5", page)
        self.assertNotIn('id="weather-form"', page)             # search form is tucked away until requested

    def test_public_ip_is_looked_up_directly_and_loopback_uses_the_callers_own_address(self):
        _, geo, _ = self.visit(ip="41.186.10.20")
        self.assertEqual(geo.call_args[0][0], "https://ipwho.is/41.186.10.20")
        self.setUp()
        _, geo, _ = self.visit()  # test client address is 127.0.0.1
        self.assertEqual(geo.call_args[0][0], "https://ipwho.is/")

    def test_private_addresses_are_not_looked_up(self):
        response, geo, _ = self.visit(ip="10.0.0.5")
        geo.assert_not_called()
        page = response.get_data(as_text=True)
        self.assertIn("could not work out your location", page)
        self.assertIn('id="weather-form"', page)

    def test_location_is_remembered_so_the_lookup_happens_once(self):
        self.visit()
        _, geo, _ = self.visit()
        geo.assert_not_called()

    def test_failed_lookup_shows_a_helpful_message_and_the_search_form(self):
        for answer in (None, {"success": False, "message": "limit"},
                       {"success": True, "city": "", "latitude": 1, "longitude": 1},
                       {"success": True, "city": "X", "latitude": 999, "longitude": 1},
                       {"success": True, "city": "X", "latitude": "nan", "longitude": 1}):
            self.setUp()
            response, _, owm = self.visit(answer=answer)
            page = response.get_data(as_text=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Try detecting again", page)
            self.assertIn('id="weather-form"', page)
            owm.assert_not_called()

    def test_change_location_button_reveals_the_form(self):
        self.visit()
        response, _, _ = self.visit("/weather?change=1")
        page = response.get_data(as_text=True)
        self.assertIn('id="weather-form"', page)
        self.assertIn("Weather in Kigali", page)               # current weather stays visible

    def test_choosing_another_place_replaces_and_remembers_the_location(self):
        self.visit()
        huye = dict(SAMPLE, name="Huye", coord={"lat": -2.6, "lon": 29.74})
        response, geo, _ = self.visit("/weather?mode=city&city=Huye", weather=huye)
        self.assertEqual(response.status_code, 200)
        response, geo, owm = self.visit(weather=huye)
        page = response.get_data(as_text=True)
        geo.assert_not_called()
        self.assertIn("Weather in Huye", page)
        self.assertIn("lat=-2.6", owm.call_args[0][0])
        self.assertIn("Use my current location", page)
        self.assertNotIn("Detected from your internet connection", page)

    def test_the_page_after_a_search_already_shows_the_new_location(self):
        huye = dict(SAMPLE, name="Huye", coord={"lat": -2.6, "lon": 29.74})
        page = self.visit("/weather?mode=city&city=Huye", weather=huye)[0].get_data(as_text=True)
        self.assertIn("Weather in Huye", page)
        self.assertIn("Use my current location", page)

    def test_use_my_current_location_button_detects_again(self):
        huye = dict(SAMPLE, name="Huye", coord={"lat": -2.6, "lon": 29.74})
        self.visit("/weather?mode=city&city=Huye", weather=huye)
        token = self.token(path="/weather")
        with mock.patch("geolocation.fetch_json", return_value=IP_ANSWER) as geo:
            response = self.client.post("/weather/detect", data={"csrf_token": token})
        self.assertEqual(response.status_code, 302)
        geo.assert_called_once()
        page = self.visit()[0].get_data(as_text=True)
        self.assertIn("Weather in Kigali", page)
        self.assertIn("Detected from your internet connection", page)

    def test_detect_endpoint_needs_a_csrf_token(self):
        self.assertEqual(self.client.post("/weather/detect").status_code, 400)

    def test_failed_redetect_clears_the_old_place_and_says_so(self):
        self.visit("/weather?mode=city&city=Huye")
        token = self.token(path="/weather")
        with mock.patch("geolocation.fetch_json", return_value=None):
            response = self.client.post("/weather/detect", data={"csrf_token": token}, follow_redirects=True)
        self.assertIn("could not work out your location", response.get_data(as_text=True))

    def test_auto_detected_searches_appear_in_admin_history_with_a_label(self):
        self.visit()
        row = self.query("SELECT query, search_type, success FROM weather_searches")[0]
        self.assertEqual((row["query"], row["search_type"], row["success"]), ("Kigali (detected from IP)", "coordinates", 1))

    def test_weather_failure_for_a_detected_city_is_shown_with_retry(self):
        with mock.patch("geolocation.fetch_json", return_value=IP_ANSWER), \
                mock.patch("weather.urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            response = self.client.get("/weather")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 502)
        self.assertIn("Try again", page)
        self.assertIn("Change location", page)

    def test_feature_can_be_switched_off(self):
        with mock.patch.object(geolocation, "GEOIP_ENABLED", False):
            response, geo, _ = self.visit()
        geo.assert_not_called()
        self.assertNotIn("could not work out your location", response.get_data(as_text=True))
        self.assertIn('id="weather-form"', response.get_data(as_text=True))

    def test_forwarded_for_header_is_ignored_unless_a_proxy_is_trusted(self):
        with mock.patch("geolocation.fetch_json", return_value=IP_ANSWER) as geo, \
                mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            self.client.get("/weather", headers={"X-Forwarded-For": "41.186.1.1"})
        self.assertEqual(geo.call_args[0][0], "https://ipwho.is/")  # not the spoofed address

    def test_forwarded_chain_is_used_for_the_lookup_when_a_proxy_is_trusted(self):
        import app as app_module_
        with mock.patch.object(app_module_, "TRUST_PROXY", True), \
                mock.patch("geolocation.fetch_json", return_value=IP_ANSWER) as geo, \
                mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            self.client.get("/weather", headers={"X-Forwarded-For": "41.186.10.20, 172.68.1.1"})
        self.assertEqual(geo.call_args_list[0][0][0], "https://ipwho.is/41.186.10.20")

    def test_privacy_note_is_shown_for_ip_based_locations(self):
        page = self.visit()[0].get_data(as_text=True)
        self.assertIn("is not stored", page)


class ClearErrorAndBrowserFallbackTests(AppTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(geolocation, "GEOIP_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def page(self, path="/weather", ip=None, fetch=None):
        kwargs = {"environ_overrides": {"REMOTE_ADDR": ip}} if ip else {}
        with mock.patch("geolocation.fetch_json", side_effect=fetch or (lambda url: None)):
            return self.client.get(path, **kwargs).get_data(as_text=True)

    def test_private_address_reason_is_shown(self):
        page = self.page(ip="10.0.0.5")
        self.assertIn("Why:", page)
        self.assertIn("private network address (10.0.0.5)", page)

    def test_provider_failure_reason_names_each_service(self):
        def fail(url):
            geolocation.last_error = "HTTP 429 (free limit reached)"
            return None
        page = self.page(ip="41.186.10.20", fetch=fail)
        self.assertIn("ipwho.is: HTTP 429 (free limit reached)", page)
        self.assertIn("geojs.io: HTTP 429", page)

    def test_failure_page_asks_the_browser_to_try(self):
        page = self.page(ip="10.0.0.5")
        self.assertIn("data-browser-locate", page)
        self.assertIn("asking your browser", page)
        self.assertIn('action="/weather/browser-location"', page)
        self.assertIn('id="weather-form"', page)              # search form is always there as well

    def test_browser_result_is_accepted_and_shows_that_citys_weather(self):
        self.page(ip="10.0.0.5")
        token = self.token(path="/weather")
        response = self.client.post("/weather/browser-location", data={
            "csrf_token": token, "city": "Kigali", "country": "rw", "lat": "-1.95", "lon": "30.06"})
        self.assertEqual(response.status_code, 302)
        with mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)) as owm:
            page = self.client.get("/weather").get_data(as_text=True)
        self.assertIn("Weather in Kigali, RW", page)
        self.assertIn("Detected from your internet connection", page)
        self.assertIn("lat=-1.95", owm.call_args[0][0])
        self.assertNotIn("Why:", page)

    def test_bad_browser_values_are_rejected_with_a_clear_message(self):
        self.page(ip="10.0.0.5")
        token = self.token(path="/weather")
        for bad in ({"city": "", "lat": "1", "lon": "1"}, {"city": "X", "lat": "200", "lon": "1"},
                    {"city": "X", "lat": "abc", "lon": "1"}, {"city": "X", "lat": "inf", "lon": "1"}):
            response = self.client.post("/weather/browser-location", data={"csrf_token": token, **bad})
            self.assertIn("browser_failed=1", response.headers["Location"])
        page = self.page("/weather?browser_failed=1", ip="10.0.0.5")
        self.assertIn("Please search for your city below", page)
        self.assertNotIn("data-browser-locate", page)          # no endless retry loop
        self.assertIsNone(self.query("SELECT 1 FROM weather_searches WHERE query LIKE 'X%'") or None)

    def test_browser_location_needs_csrf(self):
        response = self.client.post("/weather/browser-location", data={"city": "Kigali", "lat": "1", "lon": "1"})
        self.assertEqual(response.status_code, 400)

    def test_success_clears_the_old_error(self):
        self.page(ip="10.0.0.5")
        with mock.patch("geolocation.fetch_json", return_value=IP_ANSWER), \
                mock.patch("weather.urllib.request.urlopen", return_value=fake_response(SAMPLE)):
            token = self.token(path="/weather")
            self.client.post("/weather/detect", data={"csrf_token": token})
            page = self.client.get("/weather").get_data(as_text=True)
        self.assertIn("Weather in Kigali", page)
        self.assertNotIn("Why:", page)


class ProviderFallbackTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(geolocation, "GEOIP_ENABLED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_provider_is_used_when_the_first_fails(self):
        geojs = {"city": "Kigali", "country_code": "RW", "latitude": "-1.95", "longitude": "30.06"}
        with mock.patch("geolocation.fetch_json", side_effect=[None, geojs]) as fetch:
            place = geolocation.locate("41.186.10.20")
        self.assertEqual((place["city"], place["country"], place["lat"]), ("Kigali", "RW", -1.95))
        self.assertEqual([c[0][0] for c in fetch.call_args_list],
                         ["https://ipwho.is/41.186.10.20", "https://get.geojs.io/v1/ip/geo/41.186.10.20.json"])

    def test_an_answer_without_a_city_moves_on_to_the_next_provider(self):
        no_city = {"success": True, "city": "", "latitude": 1, "longitude": 1}
        good = {"city": "Huye", "country_code": "RW", "latitude": "-2.6", "longitude": "29.7"}
        with mock.patch("geolocation.fetch_json", side_effect=[no_city, good]):
            self.assertEqual(geolocation.locate("41.186.10.20")["city"], "Huye")

    def test_all_providers_failing_returns_none_and_logs_why(self):
        with mock.patch("geolocation.fetch_json", return_value=None), self.assertLogs("geolocation", "WARNING") as logs:
            self.assertIsNone(geolocation.locate("41.186.10.20"))
        self.assertTrue(any("gave nothing usable" in line for line in logs.output))

    def test_private_address_is_logged_with_a_hint_about_the_proxy_setting(self):
        with self.assertLogs("geolocation", "WARNING") as logs:
            self.assertIsNone(geolocation.locate("10.1.2.3"))
        self.assertIn("TRUST_PROXY", logs.output[0])

    def test_visitor_is_the_first_public_address_in_the_forwarded_chain(self):
        pick = geolocation.first_public_ip
        self.assertEqual(pick("41.186.10.20, 172.68.1.1, 10.0.0.2", "10.0.0.2"), "41.186.10.20")
        self.assertEqual(pick("10.0.0.9, 41.186.10.20", "10.0.0.2"), "41.186.10.20")   # skips private hops
        self.assertEqual(pick("2c0f:eb68::1, 172.68.1.1", "10.0.0.2"), "2c0f:eb68::1")  # IPv6 visitor
        self.assertEqual(pick("garbage, 10.0.0.1", "10.0.0.2"), "10.0.0.2")              # nothing usable
        self.assertEqual(pick(None, "10.0.0.2"), "10.0.0.2")


class ProxySupportTests(unittest.TestCase):
    def test_proxyfix_uses_the_forwarded_address(self):
        from flask import Flask, request
        from werkzeug.middleware.proxy_fix import ProxyFix
        demo = Flask(__name__)
        demo.wsgi_app = ProxyFix(demo.wsgi_app, x_for=1, x_proto=1, x_host=1)
        demo.add_url_rule("/", "ip", lambda: request.remote_addr)
        reply = demo.test_client().get("/", headers={"X-Forwarded-For": "41.186.1.1"})
        self.assertEqual(reply.get_data(as_text=True), "41.186.1.1")

    def test_trust_proxy_switches_on_for_render(self):
        import importlib, os, config
        original = dict(os.environ)
        try:
            os.environ.pop("TRUST_PROXY", None)
            os.environ["RENDER"] = "true"
            self.assertTrue(importlib.reload(config).TRUST_PROXY)
            os.environ.pop("RENDER")
            self.assertFalse(importlib.reload(config).TRUST_PROXY)
        finally:
            os.environ.clear(); os.environ.update(original); importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
