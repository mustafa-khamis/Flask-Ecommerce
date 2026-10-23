"""Current weather from OpenWeather, plus the saved search history used by the Admin dashboard."""
import json
import math
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from config import OPENWEATHER_API_KEY
from functions import connect_db

API_URL = "https://api.openweathermap.org/data/2.5/weather"


class WeatherError(Exception):
    """kind is one of: validation, not_found, network, service. The message is safe to show to users."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def clean_city(text):
    city = " ".join(text.split())
    if not 2 <= len(city) <= 100:
        raise WeatherError("validation", "Enter a city name between 2 and 100 characters.")
    if not all(c.isalpha() or c in " .,'-’" for c in city):
        raise WeatherError("validation", "City names can only contain letters, spaces, commas, hyphens and apostrophes.")
    return city


def clean_coordinate(text, name, low, high):
    try:
        value = float(text.strip())
    except ValueError:
        raise WeatherError("validation", f"{name} must be a number.") from None
    if not math.isfinite(value) or not low <= value <= high:
        raise WeatherError("validation", f"{name} must be between {low} and {high}.")
    return value


def request_weather(params):
    """Call OpenWeather. Network and API problems become WeatherError."""
    if not OPENWEATHER_API_KEY:
        raise WeatherError("service", "The weather service is not configured yet. Please try again later.")
    query = urllib.parse.urlencode({**params, "units": "metric", "appid": OPENWEATHER_API_KEY})
    try:
        with urllib.request.urlopen(f"{API_URL}?{query}", timeout=8, context=_ssl_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise WeatherError("not_found", "We could not find that place. Check the spelling or try coordinates.") from None
        if error.code == 429:
            raise WeatherError("service", "The weather service is busy right now. Please try again in a minute.") from None
        print(f"OpenWeather returned HTTP {error.code}")
        raise WeatherError("service", "The weather service could not answer right now. Please try again later.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise WeatherError("network", "We could not reach the weather service. Check your connection and try again.") from None
    except ValueError:
        raise WeatherError("service", "The weather service sent an unreadable answer. Please try again.") from None


def parse_report(data):
    try:
        offset = int(data.get("timezone", 0))
        observed = datetime.fromtimestamp(int(data["dt"]), tz=timezone.utc) + timedelta(seconds=offset)
        sign = "+" if offset >= 0 else "-"
        hours, minutes = divmod(abs(offset) // 60, 60)
        icon = data["weather"][0]["icon"]
        return {
            "place": data.get("name") or "Unnamed location",
            "country": data.get("sys", {}).get("country", ""),
            "temperature": round(float(data["main"]["temp"]), 1),
            "feels_like": round(float(data["main"].get("feels_like", data["main"]["temp"])), 1),
            "condition": data["weather"][0]["main"],
            "description": data["weather"][0]["description"].capitalize(),
            "humidity": int(data["main"]["humidity"]),
            "wind_speed": round(float(data["wind"]["speed"]), 1),
            "icon_url": f"https://openweathermap.org/img/wn/{icon}@2x.png",
            "observed": f"{observed:%d %b %Y, %H:%M} (local time, UTC{sign}{hours:02d}:{minutes:02d})",
        }
    except (KeyError, IndexError, TypeError, ValueError):
        raise WeatherError("service", "The weather service sent incomplete data. Please try again.") from None


def lookup(mode, form):
    """Validate the input, ask OpenWeather and return (report, query_text). Raises WeatherError."""
    if mode == "coordinates":
        lat = clean_coordinate(form.get("lat", ""), "Latitude", -90, 90)
        lon = clean_coordinate(form.get("lon", ""), "Longitude", -180, 180)
        return parse_report(request_weather({"lat": lat, "lon": lon})), f"{lat}, {lon}"
    city = clean_city(form.get("city", ""))
    return parse_report(request_weather({"q": city})), city


# --- History and statistics ------------------------------------------------------
def record_search(user_id, search_type, query, report=None, error=None):
    connection = connect_db()
    try:
        connection.execute(
            """INSERT INTO weather_searches
               (user_id, search_type, query, place, country, temperature, condition, success, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, search_type, query[:100],
             report["place"] if report else None, report["country"] if report else None,
             report["temperature"] if report else None, report["condition"] if report else None,
             1 if report else 0, error))
        connection.commit()
    finally:
        connection.close()


def recent_searches(limit=50):
    connection = connect_db()
    try:
        return connection.execute(
            """SELECT w.*, u.name AS user_name FROM weather_searches w
               LEFT JOIN users u ON u.id = w.user_id ORDER BY w.id DESC LIMIT ?""", (limit,)).fetchall()
    finally:
        connection.close()


def usage_stats():
    connection = connect_db()
    try:
        one = lambda sql: connection.execute(sql).fetchone()[0]
        total = one("SELECT COUNT(*) FROM weather_searches")
        succeeded = one("SELECT COUNT(*) FROM weather_searches WHERE success = 1")
        return {
            "total": total,
            "succeeded": succeeded,
            "failed": total - succeeded,
            "success_rate": round(succeeded * 100 / total) if total else 0,
            "today": one("SELECT COUNT(*) FROM weather_searches WHERE date(created_at) = date('now')"),
            "signed_in": one("SELECT COUNT(*) FROM weather_searches WHERE user_id IS NOT NULL"),
            "top_places": connection.execute(
                """SELECT place, country, COUNT(*) AS searches FROM weather_searches
                   WHERE success = 1 GROUP BY place, country ORDER BY searches DESC, place LIMIT 5""").fetchall(),
        }
    finally:
        connection.close()
