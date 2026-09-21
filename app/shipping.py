from __future__ import annotations

import httpx

from app.config import settings

BASE = "https://app.sendit.ma/api/v1"

_token: str | None = None


def configured() -> bool:
    return bool(settings.sendit_public_key and settings.sendit_secret_key)


def login() -> str:
    global _token
    if not configured():
        raise RuntimeError("Sendit keys missing")
    with httpx.Client(timeout=20) as client:
        res = client.post(
            f"{BASE}/login",
            json={
                "public_key": settings.sendit_public_key,
                "secret_key": settings.sendit_secret_key,
            },
        )
        res.raise_for_status()
        data = res.json()
    if not data.get("success"):
        raise RuntimeError(data.get("message") or "Sendit login failed")
    _token = data["data"]["token"]
    return _token


def _auth_headers() -> dict[str, str]:
    token = _token or login()
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def district_id(city_name: str) -> int:
    with httpx.Client(timeout=20) as client:
        res = client.get(
            f"{BASE}/districts",
            headers=_auth_headers(),
            params={"querystring": city_name, "page": 1},
        )
        if res.status_code == 401:
            login()
            res = client.get(
                f"{BASE}/districts",
                headers=_auth_headers(),
                params={"querystring": city_name, "page": 1},
            )
        res.raise_for_status()
        payload = res.json()
    rows = payload.get("data") or []
    city_l = city_name.strip().lower()
    for row in rows:
        if str(row.get("name", "")).strip().lower() == city_l:
            return int(row["id"])
    if rows:
        return int(rows[0]["id"])
    raise RuntimeError(f"Sendit district not found: {city_name}")


def create_delivery(order: dict) -> dict:
    if not configured():
        raise RuntimeError("Sendit not configured")
    login()
    ville = order.get("ville") or "Casablanca"
    dest = district_id(ville)
    pickup = district_id(settings.sendit_pickup_district)
    products = []
    for item in order.get("items") or []:
        products.append(
            f"{item.get('name_ar') or item.get('slug')}/{item.get('size')}/{item.get('color_name') or item.get('color')}"
        )
    body = {
        "pickup_district_id": pickup,
        "district_id": dest,
        "name": order.get("name"),
        "amount": int(order.get("sale_total") or order.get("total") or 0),
        "address": f"{order.get('adresse')}, {ville}",
        "phone": str(order.get("phone", "")).replace("+", ""),
        "comment": order.get("notes") or "TANDA COD — confirmer taille",
        "reference": order.get("order_id"),
        "allow_open": settings.sendit_allow_open,
        "allow_try": settings.sendit_allow_try,
        "products_from_stock": 0,
        "products": ", ".join(products),
        "packaging_id": 1,
        "option_exchange": 0,
        "delivery_exchange_id": "",
    }
    with httpx.Client(timeout=20) as client:
        res = client.post(f"{BASE}/deliveries", headers=_auth_headers(), json=body)
        if res.status_code == 401:
            login()
            res = client.post(f"{BASE}/deliveries", headers=_auth_headers(), json=body)
        res.raise_for_status()
        data = res.json()
    if data.get("error"):
        raise RuntimeError(str(data["error"]))
    inner = data.get("data") or data
    code = inner.get("code") or inner.get("tracking") or inner.get("id")
    return {
        "provider": "sendit",
        "tracking": str(code) if code else "",
        "raw": inner,
    }


SENDIT_STATUS_MAP = {
    "delivered": "delivered",
    "livré": "delivered",
    "livre": "delivered",
    "returned": "returned",
    "retourné": "returned",
    "retournee": "returned",
    "return": "returned",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "annulé": "cancelled",
    "picked_up": "shipped",
    "in_transit": "shipped",
    "out_for_delivery": "shipped",
    "en cours": "shipped",
}


def map_status(label: str | None) -> str | None:
    if not label:
        return None
    key = label.strip().lower()
    return SENDIT_STATUS_MAP.get(key)
