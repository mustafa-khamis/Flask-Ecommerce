"""Guess a visitor's city from their IP address.

The IP is sent to ipwho.is only for the lookup and is never stored: the app keeps just the
resulting city and coordinates in the visitor's session. The result is approximate (often the
internet provider's nearest hub), which is why the weather page always offers "Change location".
"""
import ipaddress
import json
import logging
import math
import ssl
import urllib.error
import urllib.request

from config import GEOIP_ENABLED

log = logging.getLogger("geolocation")

# Free providers, tried in order. A shared hosting IP can hit one provider's free limit, so a
# second one keeps the feature working. Each entry: (name, url with {ip}, url used for "my own address").
PROVIDERS = (
    ("ipwho.is", "https://ipwho.is/{ip}", "https://ipwho.is/"),
    ("geojs.io", "https://get.geojs.io/v1/ip/geo/{ip}.json", "https://get.geojs.io/v1/ip/geo.json"),
)


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


last_error = ""  # why the most recent download failed, shown to the visitor in plain words


def fetch_json(url):
    """Download JSON, or return None on any network or format problem (the reason is logged)."""
    global last_error
    last_error = ""
    request = urllib.request.Request(url, headers={"User-Agent": "lumamarket-student-project/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=5, context=_ssl_context()) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        last_error = f"HTTP {error.code}" + (" (free limit reached)" if error.code == 429 else "")
    except TimeoutError:
        last_error = "timed out"
    except (urllib.error.URLError, OSError) as error:
        last_error = "could not connect"
        log.warning("IP lookup %s: %s", url.split("/")[2], getattr(error, "reason", error))  # detail for the logs
    except ValueError:
        last_error = "sent an unreadable answer"
    log.warning("IP lookup %s failed: %s", url.split("/")[2], last_error)
    return None


def first_public_ip(forwarded_for, fallback):
    """Pick the visitor's address from an X-Forwarded-For chain such as 'visitor, proxy1, proxy2'.

    Hosts like Render add several proxies, so the visitor is the first public address in the chain.
    Faking this header can only change your own estimated location, never anyone else's.
    """
    for part in (forwarded_for or "").split(","):
        try:
            if ipaddress.ip_address(part.strip()).is_global:
                return part.strip()
        except ValueError:
            continue
    return fallback


def lookup_target(ip):
    """What to ask the providers about, or None when no useful lookup is possible.

    A public IP is looked up directly. When developing on your own computer the address is
    127.0.0.1, so the lookup is made for the machine's own public address instead (the
    caller). Other private addresses (for example a proxy's) say nothing about the visitor.
    """
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if address.is_global:
        return ip
    if address.is_loopback:
        return ""
    return None


def parse_answer(data):
    """Turn one provider's JSON into our format, or None if it has no usable city and position."""
    try:
        if not isinstance(data, dict) or data.get("success") is False:
            return None
        lat, lon = float(data["latitude"]), float(data["longitude"])
        city = str(data.get("city") or "").strip()
        if not city or not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
            return None
        return {"city": city[:100], "country": str(data.get("country_code") or "")[:2], "lat": lat, "lon": lon}
    except (KeyError, TypeError, ValueError):
        return None


def locate_with_reason(ip):
    """Return (place, reason). place is {"city", "country", "lat", "lon"} or None; reason explains a None."""
    if not GEOIP_ENABLED:
        return None, "Automatic location is switched off on this site."
    target = lookup_target(ip or "")
    if target is None:
        log.warning("IP location skipped: %r is not a public address (is the proxy setting on? see TRUST_PROXY)", ip)
        return None, f"The server only saw a private network address ({ip or 'none'}), not yours."
    problems = []
    for name, url, own_url in PROVIDERS:
        data = fetch_json(url.format(ip=target) if target else own_url)
        place = parse_answer(data)
        if place:
            return place, ""
        problem = last_error if data is None and last_error else ("no answer" if data is None else "no city for this address")
        problems.append(f"{name}: {problem}")
        log.warning("IP location: %s gave nothing usable for %r (%s)", name, target or "own address", problem)
    return None, "The location services gave no usable answer (" + "; ".join(problems) + ")."


def locate(ip):
    """Return {"city", "country", "lat", "lon"} for the IP, or None if no provider could say."""
    return locate_with_reason(ip)[0]


def clean_place(form):
    """Validate a location reported by the visitor's browser. Returns the place or None."""
    city = " ".join(str(form.get("city", "")).split())[:100]
    country = str(form.get("country", "")).strip().upper()[:2]
    try:
        lat, lon = float(form.get("lat", "")), float(form.get("lon", ""))
    except ValueError:
        return None
    if not city or not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
        return None
    if not country.isalpha():
        country = ""
    return {"city": city, "country": country, "lat": lat, "lon": lon}
