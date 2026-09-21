from __future__ import annotations

import re
import threading
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from app.config import settings
from app.store import (
    STATUS_AR,
    STATUSES,
    add_history,
    compute_stats,
    get_order,
    get_product,
    list_orders,
    load_products,
    now_iso,
    save_products,
    upsert_order,
)
from app import sheets
from app import shipping
from app import fraud

app = FastAPI(title="TANDA Brand API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def normalize_phone(raw: str) -> str | None:
    cleaned = raw.strip().replace(" ", "").replace("-", "").replace(".", "")
    m = re.match(r"^(\+?212)(6|7)(\d{8})$", cleaned)
    if m:
        return f"+212{m.group(2)}{m.group(3)}"
    m = re.match(r"^0(6|7)(\d{8})$", cleaned)
    if m:
        return f"+212{m.group(1)}{m.group(2)}"
    m = re.match(r"^(6|7)(\d{8})$", cleaned)
    if m:
        return f"+212{m.group(1)}{m.group(2)}"
    return None


def resolve_client_ip(
    cf_connecting_ip: str = Header(default="", alias="cf-connecting-ip"),
    x_forwarded_for: str = Header(default="", alias="x-forwarded-for"),
    x_real_ip: str = Header(default="", alias="x-real-ip"),
) -> str:
    """Resolve the real client IP from proxy headers."""
    if cf_connecting_ip:
        return cf_connecting_ip.strip()
    if x_forwarded_for:
        return x_forwarded_for.split(",")[0].strip()
    if x_real_ip:
        return x_real_ip.strip()
    return ""


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not x_admin_token or x_admin_token != settings.admin_token:
        raise HTTPException(status_code=401, detail="غير مصرح")


def sync_sheet(record: dict) -> None:
    def _run() -> None:
        try:
            sheets.upsert(record)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


class OrderItemIn(BaseModel):
    slug: str
    color: str = ""
    color_name: str = ""
    size: str
    qty: int = Field(ge=1, le=10)
    name_ar: str = ""


class CheckoutRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str
    ville: str = Field(min_length=2, max_length=80)
    adresse: str = Field(min_length=5, max_length=300)
    items: list[OrderItemIn] = Field(min_length=1)
    source: str = "store"
    notes: str = ""


class StatusPatch(BaseModel):
    status: str
    note: str = ""
    confirmed_by: str = ""


class TrackingPatch(BaseModel):
    tracking: str
    provider: str = "manual"


class ProductIn(BaseModel):
    slug: str
    name_ar: str
    sale_price: int = Field(ge=0)
    cost_price: int = Field(ge=0)


class WebhookIn(BaseModel):
    code: str = ""
    tracking: str = ""
    status: str = ""
    reference: str = ""
    order_id: str = ""


def build_lines(items: list[OrderItemIn]) -> tuple[list[dict], int, int]:
    lines = []
    sale_total = 0
    cost_total = 0
    for item in items:
        product = get_product(item.slug)
        if product is None:
            raise HTTPException(status_code=422, detail=f"منتج غير موجود: {item.slug}")
        price = int(product["sale_price"])
        cost = int(product.get("cost_price") or 0)
        line_total = price * item.qty
        line_cost = cost * item.qty
        sale_total += line_total
        cost_total += line_cost
        lines.append(
            {
                "slug": item.slug,
                "name_ar": item.name_ar or product.get("name_ar") or item.slug,
                "color": item.color,
                "color_name": item.color_name or item.color,
                "size": item.size,
                "qty": item.qty,
                "unit_price": price,
                "unit_cost": cost,
                "line_total": line_total,
                "line_cost": line_cost,
            }
        )
    return lines, sale_total, cost_total


def create_order_record(body: CheckoutRequest, client_ip: str = "") -> dict:
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(status_code=422, detail="رقم الهاتف غير صحيح")

    # --- Fraud gate ---
    fraud_result = fraud.check_fraud(client_ip or "0.0.0.0", phone)

    if fraud_result["decision"] == "block":
        raise HTTPException(
            status_code=403,
            detail="عذراً، لا يمكن إتمام الطلب. تواصلي معنا عبر واتساب",
        )

    lines, sale_total, cost_total = build_lines(body.items)
    order_id = str(uuid.uuid4())

    initial_status = "new"
    if fraud_result["decision"] == "review":
        initial_status = "pending_review"

    record = {
        "order_id": order_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "name": body.name.strip(),
        "phone": phone,
        "ville": body.ville.strip(),
        "adresse": body.adresse.strip(),
        "source": body.source.strip() or "store",
        "notes": body.notes.strip(),
        "items": lines,
        "total": sale_total,
        "sale_total": sale_total,
        "cost_total": cost_total,
        "shipping_cost": 0,
        "currency": "MAD",
        "status": initial_status,
        "confirmed_by": "",
        "delivery": {},
        "is_test": fraud_result.get("is_test", False),
        "fraud": {
            "decision": fraud_result["decision"],
            "reasons": fraud_result["reasons"],
            "ip_country": fraud_result["ip_country"],
            "ip_is_anonymous": fraud_result["ip_is_anonymous"],
            "ip_anonymizer_type": fraud_result["ip_anonymizer_type"],
            "ip_anonymizer_conf": fraud_result["ip_anonymizer_conf"],
            "ip_risk_score": fraud_result["ip_risk_score"],
        },
        "history": [
            {
                "at": now_iso(),
                "status": initial_status,
                "note": f"source:{body.source} fraud:{fraud_result['decision']}",
            }
        ],
    }
    upsert_order(record)
    sync_sheet(record)
    return record


@app.get("/health")
def health():
    return {
        "status": "ok",
        "brand": "TANDA",
        "sheet": sheets.configured(),
        "sendit": shipping.configured(),
    }


@app.post("/api/v1/orders/checkout")
def checkout(body: CheckoutRequest, client_ip: str = Depends(resolve_client_ip)):
    record = create_order_record(body, client_ip)
    return {
        "order_id": record["order_id"],
        "total": record["sale_total"],
        "currency": "MAD",
    }


@app.post("/api/v1/admin/login")
def admin_login(x_admin_token: str = Header(default="")):
    require_admin(x_admin_token)
    return {"ok": True}


@app.get("/api/v1/admin/overview")
def admin_overview(_: None = Depends(require_admin)):
    orders = list_orders()
    return {
        "stats": compute_stats(orders),
        "integrations": {
            "sheet": sheets.configured(),
            "sendit": shipping.configured(),
            "shipping_cost_mad": settings.shipping_cost_mad,
        },
        "status_labels": STATUS_AR,
        "orders": orders,
    }


@app.get("/api/v1/admin/orders/{order_id}")
def admin_order(order_id: str, _: None = Depends(require_admin)):
    record = get_order(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="الطلب غير موجود")
    return record


@app.post("/api/v1/admin/orders")
def admin_create_order(body: CheckoutRequest, _: None = Depends(require_admin)):
    return create_order_record(body)


@app.patch("/api/v1/admin/orders/{order_id}/status")
def admin_status(order_id: str, body: StatusPatch, _: None = Depends(require_admin)):
    if body.status not in STATUSES:
        raise HTTPException(status_code=422, detail="حالة غير صحيحة")
    record = get_order(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="الطلب غير موجود")
    record["status"] = body.status
    if body.confirmed_by:
        record["confirmed_by"] = body.confirmed_by
    if body.status in ("shipped", "delivered", "returned") and not record.get("shipping_cost"):
        record["shipping_cost"] = settings.shipping_cost_mad
    add_history(record, body.status, body.note)
    upsert_order(record)
    sync_sheet(record)
    return record


@app.post("/api/v1/admin/orders/{order_id}/ship")
def admin_ship(order_id: str, _: None = Depends(require_admin)):
    record = get_order(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="الطلب غير موجود")
    if record.get("status") not in ("confirmed", "shipped"):
        raise HTTPException(status_code=422, detail="أكد الطلب قبل الإرسال للناقل")
    if shipping.configured():
        try:
            delivery = shipping.create_delivery(record)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Sendit: {exc}") from exc
        record["delivery"] = {
            "provider": "sendit",
            "tracking": delivery.get("tracking", ""),
            "raw": delivery.get("raw"),
        }
    record["status"] = "shipped"
    record["shipping_cost"] = record.get("shipping_cost") or settings.shipping_cost_mad
    add_history(record, "shipped", record.get("delivery", {}).get("tracking", "manual"))
    upsert_order(record)
    sync_sheet(record)
    return record


@app.patch("/api/v1/admin/orders/{order_id}/tracking")
def admin_tracking(order_id: str, body: TrackingPatch, _: None = Depends(require_admin)):
    record = get_order(order_id)
    if not record:
        raise HTTPException(status_code=404, detail="الطلب غير موجود")
    record.setdefault("delivery", {})
    record["delivery"]["provider"] = body.provider
    record["delivery"]["tracking"] = body.tracking.strip()
    if record.get("status") in ("new", "no_answer", "confirmed"):
        record["status"] = "shipped"
        record["shipping_cost"] = record.get("shipping_cost") or settings.shipping_cost_mad
        add_history(record, "shipped", body.tracking)
    upsert_order(record)
    sync_sheet(record)
    return record


@app.post("/api/v1/admin/sheet/rebuild")
def admin_sheet_rebuild(_: None = Depends(require_admin)):
    if not sheets.configured():
        raise HTTPException(status_code=400, detail="Google Sheet غير مربوط")
    try:
        n = sheets.rebuild(list_orders())
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "rows": n}


@app.get("/api/v1/admin/products")
def admin_products(_: None = Depends(require_admin)):
    return list(load_products().values())


@app.put("/api/v1/admin/products")
def admin_save_product(body: ProductIn, _: None = Depends(require_admin)):
    products = load_products()
    products[body.slug] = body.model_dump()
    save_products(products)
    return body.model_dump()


@app.post("/api/v1/shipping/webhook")
def shipping_webhook(body: WebhookIn):
    tracking = body.tracking or body.code
    order_id = body.order_id or body.reference
    record = None
    if order_id:
        record = get_order(order_id)
    if record is None and tracking:
        for rec in list_orders():
            if (rec.get("delivery") or {}).get("tracking") == tracking:
                record = rec
                break
    if record is None:
        raise HTTPException(status_code=404, detail="طلب غير موجود")
    mapped = shipping.map_status(body.status)
    if mapped:
        record["status"] = mapped
        if mapped in ("shipped", "delivered", "returned"):
            record["shipping_cost"] = record.get("shipping_cost") or settings.shipping_cost_mad
        add_history(record, mapped, f"webhook:{body.status}")
        upsert_order(record)
        sync_sheet(record)
    return {"ok": True, "order_id": record["order_id"], "status": record["status"]}
