from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import DATA_DIR, LEGACY_ORDERS, ORDERS_FILE, PRODUCTS_FILE

DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()

STATUSES = (
    "new",
    "pending_review",
    "no_answer",
    "confirmed",
    "cancelled",
    "shipped",
    "delivered",
    "returned",
)

STATUS_AR = {
    "new": "جديدة",
    "pending_review": "مراجعة",
    "no_answer": "ماجاوبتش",
    "confirmed": "مؤكدة",
    "cancelled": "ملغاة",
    "shipped": "مرسلة",
    "delivered": "موصلة",
    "returned": "مرتجعة",
}

# Old YouCan SKUs — sale only. Cost unknown until set in admin.
LEGACY_PRICES: dict[str, int] = {
    "polo-wide-pants-set": 129,
    "moroccan-embroidered-cape": 249,
    "modest-4-piece-kimono": 299,
    "burkini-premium-2026": 279,
    "burkini-premium-2026-ref-02": 299,
    "burkini-ref-01": 349,
    "butterfly-set": 299,
    "two-piece-skirt-set": 219,
    "satin-set": 229,
    "oversized-scarf-jacket": 249,
    "jellaba-pockets": 249,
    "malak-dress": 219,
    "sweat-dress": 199,
    "tereza-dress": 219,
    "sultana-dress": 199,
    "lina-tracksuit": 279,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Any) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_products() -> dict[str, dict]:
    with _lock:
        raw = _read_json(PRODUCTS_FILE, {})
        if isinstance(raw, list):
            return {p["slug"]: p for p in raw if p.get("slug")}
        return raw if isinstance(raw, dict) else {}


def save_products(products: dict[str, dict]) -> None:
    with _lock:
        _write_json(PRODUCTS_FILE, products)


def get_product(slug: str) -> dict | None:
    products = load_products()
    if slug in products:
        return products[slug]
    if slug in LEGACY_PRICES:
        return {
            "slug": slug,
            "name_ar": slug,
            "sale_price": LEGACY_PRICES[slug],
            "cost_price": 0,
        }
    return None


def _migrate_legacy(orders: dict[str, dict]) -> dict[str, dict]:
    if not LEGACY_ORDERS.exists():
        return orders
    for line in LEGACY_ORDERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        oid = rec.get("order_id")
        if oid and oid not in orders:
            rec["status"] = _normalize_status(rec.get("status", "new"))
            rec.setdefault("source", "store")
            rec.setdefault("sale_total", rec.get("total", 0))
            rec.setdefault("cost_total", 0)
            rec.setdefault("shipping_cost", 0)
            rec.setdefault("history", [])
            rec.setdefault("delivery", {})
            orders[oid] = rec
    return orders


def _normalize_status(status: str) -> str:
    if status == "pending_confirmation":
        return "new"
    return status if status in STATUSES else "new"


def load_orders() -> dict[str, dict]:
    with _lock:
        raw = _read_json(ORDERS_FILE, {})
        if isinstance(raw, list):
            orders = {o["order_id"]: o for o in raw if o.get("order_id")}
        else:
            orders = raw if isinstance(raw, dict) else {}
        orders = _migrate_legacy(orders)
        return orders


def save_orders(orders: dict[str, dict]) -> None:
    with _lock:
        _write_json(ORDERS_FILE, orders)


def upsert_order(record: dict) -> dict:
    orders = load_orders()
    record["updated_at"] = now_iso()
    orders[record["order_id"]] = record
    save_orders(orders)
    return record


def get_order(order_id: str) -> dict | None:
    return load_orders().get(order_id)


def list_orders() -> list[dict]:
    orders = list(load_orders().values())
    orders.sort(key=lambda o: o.get("created_at", ""), reverse=True)
    return orders


def add_history(record: dict, status: str, note: str = "") -> None:
    record.setdefault("history", [])
    record["history"].append({"at": now_iso(), "status": status, "note": note})


def money_of(record: dict) -> dict:
    sale = int(record.get("sale_total") or record.get("total") or 0)
    cost = int(record.get("cost_total") or 0)
    ship = int(record.get("shipping_cost") or 0)
    status = _normalize_status(record.get("status", "new"))
    if status == "delivered":
        margin = sale - cost - ship
        collected = sale
    elif status == "returned":
        margin = 0 - ship
        collected = 0
    elif status in ("shipped", "confirmed"):
        margin = sale - cost - ship
        collected = 0
    else:
        margin = 0
        collected = 0
    return {
        "sale_total": sale,
        "cost_total": cost,
        "shipping_cost": ship,
        "margin": margin,
        "collected": collected,
    }


def compute_stats(orders: list[dict] | None = None) -> dict:
    rows = orders if orders is not None else list_orders()
    counts = {s: 0 for s in STATUSES}
    sale_all = cost_all = ship_all = collected = realized_margin = 0
    sale_delivered = cost_delivered = 0

    for rec in rows:
        status = _normalize_status(rec.get("status", "new"))
        counts[status] = counts.get(status, 0) + 1
        m = money_of(rec)
        sale_all += m["sale_total"]
        cost_all += m["cost_total"]
        if status in ("shipped", "delivered", "returned"):
            ship_all += m["shipping_cost"]
        if status == "delivered":
            collected += m["collected"]
            sale_delivered += m["sale_total"]
            cost_delivered += m["cost_total"]
            realized_margin += m["margin"]
        elif status == "returned":
            realized_margin += m["margin"]

    confirmed_funnel = (
        counts["confirmed"] + counts["shipped"] + counts["delivered"] + counts["returned"]
    )
    entered = len(rows)
    return {
        "entered": entered,
        "confirmed": confirmed_funnel,
        "delivered": counts["delivered"],
        "returned": counts["returned"],
        "cancelled": counts["cancelled"],
        "no_answer": counts["no_answer"],
        "shipped": counts["shipped"],
        "new": counts["new"],
        "counts": counts,
        "sale_all": sale_all,
        "cost_all": cost_all,
        "sale_delivered": sale_delivered,
        "cost_delivered": cost_delivered,
        "shipping_paid": ship_all,
        "collected": collected,
        "margin_net": realized_margin,
        "currency": "MAD",
    }
