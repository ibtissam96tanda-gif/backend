from __future__ import annotations

import json
from pathlib import Path

from app.config import settings
from app.store import STATUS_AR, money_of

HEADERS = [
    "id",
    "date",
    "source",
    "nom",
    "tel",
    "ville",
    "adresse",
    "produits",
    "qty",
    "vente_MAD",
    "achat_MAD",
    "livraison_MAD",
    "marge_MAD",
    "encaisse_MAD",
    "statut",
    "statut_ar",
    "tracking",
    "transporteur",
    "notes",
]


def _credentials():
    raw = settings.google_service_account_json.strip()
    file_path = settings.google_service_account_file.strip()
    if raw:
        return json.loads(raw)
    if file_path:
        return json.loads(Path(file_path).read_text(encoding="utf-8"))
    return None


def configured() -> bool:
    return bool(settings.google_sheet_id and _credentials())


def _worksheet():
    import gspread

    creds = _credentials()
    if not creds or not settings.google_sheet_id:
        raise RuntimeError("Google Sheet not configured")
    gc = gspread.service_account_from_dict(creds)
    sh = gc.open_by_key(settings.google_sheet_id)
    ws = sh.sheet1
    existing = ws.row_values(1)
    if existing[: len(HEADERS)] != HEADERS:
        ws.clear()
        ws.append_row(HEADERS, value_input_option="RAW")
    return ws


def _products_cell(record: dict) -> str:
    parts = []
    for item in record.get("items") or []:
        name = item.get("name_ar") or item.get("slug")
        parts.append(
            f"{name} / {item.get('color_name') or item.get('color')} / {item.get('size')} × {item.get('qty')}"
        )
    return " | ".join(parts)


def row_for(record: dict) -> list:
    m = money_of(record)
    status = record.get("status", "new")
    delivery = record.get("delivery") or {}
    qty = sum(int(i.get("qty") or 0) for i in (record.get("items") or []))
    return [
        record.get("order_id", ""),
        (record.get("created_at") or "")[:19].replace("T", " "),
        record.get("source", "store"),
        record.get("name", ""),
        record.get("phone", ""),
        record.get("ville", ""),
        record.get("adresse", ""),
        _products_cell(record),
        qty,
        m["sale_total"],
        m["cost_total"],
        m["shipping_cost"],
        m["margin"],
        m["collected"],
        status,
        STATUS_AR.get(status, status),
        delivery.get("tracking", ""),
        delivery.get("provider", ""),
        record.get("notes", ""),
    ]


def upsert(record: dict) -> None:
    if not configured():
        return
    ws = _worksheet()
    oid = record.get("order_id", "")
    cell = ws.find(oid, in_column=1) if oid else None
    values = row_for(record)
    if cell:
        ws.update(f"A{cell.row}:S{cell.row}", [values], value_input_option="RAW")
    else:
        ws.append_row(values, value_input_option="RAW")


def rebuild(orders: list[dict]) -> int:
    if not configured():
        raise RuntimeError("Google Sheet not configured")
    ws = _worksheet()
    ws.clear()
    rows = [HEADERS] + [row_for(o) for o in orders]
    ws.update("A1", rows, value_input_option="RAW")
    return len(orders)
