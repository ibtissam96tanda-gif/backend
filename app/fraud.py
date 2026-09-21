"""MaxMind GeoIP Insights fraud gate.

Three outcomes: allow | review | block.
See docs/58-fraud-and-order-quality.md for the full rationale.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.config import settings

log = logging.getLogger("fraud")

# ---------------------------------------------------------------------------
# IP cache — avoids repeated MaxMind calls for the same IP within 24 h
# ---------------------------------------------------------------------------
_cache: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 86_400  # 24 hours


def _cache_get(ip: str) -> dict | None:
    entry = _cache.get(ip)
    if entry and time.time() - entry[0] < _CACHE_TTL:
        return entry[1]
    if entry:
        del _cache[ip]
    return None


def _cache_set(ip: str, result: dict) -> None:
    _cache[ip] = (time.time(), result)


# ---------------------------------------------------------------------------
# MaxMind GeoIP Insights call
# ---------------------------------------------------------------------------
_MAXMIND_URL = "https://geoip.maxmind.com/geoip/v2.1/insights/{ip}"
_TIMEOUT = 1.5  # seconds — fail open if MaxMind is slow


def _call_maxmind(ip: str) -> dict | None:
    """Call MaxMind Insights API. Returns the JSON body or None on failure."""
    if not settings.maxmind_account_id or not settings.maxmind_license_key:
        return None
    try:
        resp = httpx.get(
            _MAXMIND_URL.format(ip=ip),
            auth=(settings.maxmind_account_id, settings.maxmind_license_key),
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("MaxMind call failed for %s: %s", ip, exc)
        return None


# ---------------------------------------------------------------------------
# Decision engine
# ---------------------------------------------------------------------------

def _is_whitelisted_phone(phone: str) -> bool:
    """Check if the phone (already normalised to +212...) is in the test whitelist."""
    if not settings.test_phone_whitelist:
        return False
    whitelist = [p.strip() for p in settings.test_phone_whitelist.split(",") if p.strip()]
    for w in whitelist:
        # Normalise the whitelist entry the same way
        cleaned = w.replace(" ", "").replace("-", "")
        if cleaned.startswith("0"):
            cleaned = "+212" + cleaned[1:]
        elif not cleaned.startswith("+"):
            cleaned = "+212" + cleaned
        if phone == cleaned:
            return True
    return False


def check_fraud(ip: str, phone: str) -> dict[str, Any]:
    """Run the fraud gate. Returns a dict with at least 'decision' and 'reasons'.

    decision: "allow" | "review" | "block"
    is_test: True if phone is whitelisted
    """
    result: dict[str, Any] = {
        "decision": "allow",
        "reasons": [],
        "is_test": False,
        "ip_country": None,
        "ip_is_anonymous": False,
        "ip_anonymizer_type": None,
        "ip_anonymizer_conf": 0,
        "ip_risk_score": 0.0,
    }

    # --- Test whitelist bypass ---
    if _is_whitelisted_phone(phone):
        result["is_test"] = True
        result["reasons"].append("whitelisted_test_phone")
        return result

    # --- MaxMind not configured → allow (open gate) ---
    if not settings.maxmind_account_id or not settings.maxmind_license_key:
        result["reasons"].append("maxmind_not_configured")
        return result

    # --- Check cache ---
    geo = _cache_get(ip)
    if geo is None:
        geo = _call_maxmind(ip)
        if geo is None:
            # MaxMind down → review, don't block
            result["decision"] = "review"
            result["reasons"].append("maxmind_unavailable")
            return result
        _cache_set(ip, geo)

    # --- Extract fields ---
    country_code = (geo.get("country") or {}).get("iso_code", "")
    anonymizer = geo.get("anonymizer") or geo.get("traits") or {}
    is_anonymous = anonymizer.get("is_anonymous", False)
    is_vpn = anonymizer.get("is_anonymous_vpn", False)
    is_proxy = anonymizer.get("is_public_proxy", False)
    is_tor = anonymizer.get("is_tor_exit_node", False)
    is_hosting = anonymizer.get("is_hosting_provider", False)
    is_residential_proxy = anonymizer.get("is_residential_proxy", False)
    anon_confidence = anonymizer.get("confidence", 0) or 0
    risk_score = (geo.get("traits") or {}).get("ip_risk_snapshot", 0) or 0

    result["ip_country"] = country_code
    result["ip_is_anonymous"] = is_anonymous
    result["ip_risk_score"] = risk_score
    result["ip_anonymizer_conf"] = anon_confidence

    if is_vpn:
        result["ip_anonymizer_type"] = "vpn"
    elif is_proxy:
        result["ip_anonymizer_type"] = "public_proxy"
    elif is_tor:
        result["ip_anonymizer_type"] = "tor"
    elif is_hosting:
        result["ip_anonymizer_type"] = "hosting"
    elif is_residential_proxy:
        result["ip_anonymizer_type"] = "residential_proxy"

    allowed_countries = [
        c.strip().upper()
        for c in settings.maxmind_allowed_countries.split(",")
        if c.strip()
    ]

    # --- BLOCK conditions ---
    block_conf = settings.maxmind_block_confidence

    if is_tor:
        result["decision"] = "block"
        result["reasons"].append("tor_exit_node")
        return result

    if (is_vpn or is_proxy) and anon_confidence >= block_conf:
        result["decision"] = "block"
        result["reasons"].append(f"high_confidence_anonymizer:{anon_confidence}")
        return result

    # --- REVIEW conditions ---
    review_conf = settings.maxmind_review_confidence
    risk_threshold = settings.maxmind_risk_review_threshold

    if country_code and country_code not in allowed_countries:
        result["decision"] = "review"
        result["reasons"].append(f"non_allowed_country:{country_code}")

    if is_anonymous and anon_confidence >= review_conf:
        result["decision"] = "review"
        result["reasons"].append(f"anonymizer_confidence:{anon_confidence}")

    if risk_score >= risk_threshold:
        result["decision"] = "review"
        result["reasons"].append(f"high_risk_score:{risk_score}")

    return result
