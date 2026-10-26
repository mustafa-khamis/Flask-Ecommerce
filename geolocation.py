"""Guess a visitor's city from their IP address.

The IP is sent to ipwho.is only for the lookup and is never stored: the app keeps just the
resulting city and coordinates in the visitor's session. The result is approximate (often the
internet provider's nearest hub), which is why the weather page always offers "Change location".
"""
import ipaddress
import json
import math
import ssl
import urllib.error
import urllib.request

from config import GEOIP_ENABLED

LOOKUP_URL = "https://ipwho.is/{ip}"


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch_json(url):
    """Download JSON, or return None on any network or format problem."""
    request = urllib.request.Request(url, headers={"User-Agent": "lumamarket-student-project/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=4, context=_ssl_context()) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def lookup_target(ip):
    """What to ask ipwho.is about, or None when no useful lookup is possible.

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


def locate(ip):
    """Return {"city", "country", "lat", "lon"} for the IP, or None if it cannot be worked out."""
    if not GEOIP_ENABLED:
        return None
    target = lookup_target(ip or "")
    if target is None:
        return None
    data = fetch_json(LOOKUP_URL.format(ip=target))
    try:
        if not data or data.get("success") is not True:
            return None
        lat, lon = float(data["latitude"]), float(data["longitude"])
        city = str(data.get("city") or "").strip()
        if not city or not (math.isfinite(lat) and math.isfinite(lon)) or abs(lat) > 90 or abs(lon) > 180:
            return None
        return {"city": city[:100], "country": str(data.get("country_code") or "")[:2], "lat": lat, "lon": lon}
    except (KeyError, TypeError, ValueError):
        return None
