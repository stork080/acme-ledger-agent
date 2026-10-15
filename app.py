import json
import os
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

SOURCE_URL = os.getenv("SOURCE_URL", "").strip()
BUSINESS_TZ = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Acme Appliances Ledger Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://exam.sanand.workers.dev"],
    allow_credentials=False,
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

class QuestionRequest(BaseModel):
    question: str


def _get_json(url: str):
    with httpx.Client(timeout=httpx.Timeout(10.0, connect=5.0), follow_redirects=True) as client:
        response = client.get(url, headers={"Accept": "application/json, application/x-ndjson"})
        response.raise_for_status()
        return response


def _extract_order_records(payload):
    """Accept common JSON and NDJSON export shapes and return order dictionaries."""
    found = []

    def walk(obj):
        if isinstance(obj, list):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, dict):
            return

        # A normal order row
        if "id" in obj and ("customer" in obj or "product" in obj or "amount" in obj):
            found.append(obj)
            return

        # Common wrapper shapes
        for key in ("orders", "records", "data", "items", "results", "order", "record"):
            if key in obj:
                walk(obj[key])

    walk(payload)
    return found


def _parse_export(text):
    # First try a conventional JSON document (array or wrapper object).
    try:
        payload = json.loads(text)
        rows = _extract_order_records(payload)
        if rows:
            return rows
    except json.JSONDecodeError:
        pass

    # Export endpoint is documented by its content type as NDJSON:
    # one JSON object per line.
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.extend(_extract_order_records(json.loads(line)))
        except json.JSONDecodeError:
            continue
    if not rows:
        raise ValueError("The export endpoint returned no recognizable order records.")
    return rows


def _parse_dt(value):
    if not value:
        return datetime.min.replace(tzinfo=BUSINESS_TZ)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BUSINESS_TZ)
        return dt
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=BUSINESS_TZ)


def _deduplicate_orders(rows):
    # The API notes that repeated IDs represent revisions; keep latest updated_at.
    latest = {}
    for row in rows:
        order_id = str(row.get("id", ""))
        if not order_id:
            continue
        if order_id not in latest or _parse_dt(row.get("updated_at")) > _parse_dt(latest[order_id].get("updated_at")):
            latest[order_id] = row
    return list(latest.values())


@lru_cache(maxsize=1)
def load_ledger():
    if not SOURCE_URL:
        raise RuntimeError("Set the SOURCE_URL environment variable to your assigned questionData URL.")

    try:
        root_response = _get_json(SOURCE_URL)
        root = root_response.json()
        links = root.get("links", {})
        export_url = links.get("export")
        rates_url = links.get("rates")
        if not export_url or not rates_url:
            raise ValueError("The API root did not provide export and rates links.")

        # Fetch the bulk export and exchange rates concurrently to reduce cold-start latency.
        with ThreadPoolExecutor(max_workers=2) as pool:
            export_future = pool.submit(_get_json, export_url)
            rates_future = pool.submit(_get_json, rates_url)
            export_response = export_future.result()
            rates_response = rates_future.result()

        rows = _deduplicate_orders(_parse_export(export_response.text))
        rates_payload = rates_response.json()
        rates = rates_payload.get("usd_per_unit", {"USD": 1.0})
        rates = {str(k).upper(): Decimal(str(v)) for k, v in rates.items()}
        rates.setdefault("USD", Decimal("1"))
        return rows, rates
    except Exception as exc:
        raise RuntimeError(f"Could not load the assigned ledger API: {exc}") from exc


MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6,
    "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9,
    "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}


def _business_date(order):
    dt = _parse_dt(order.get("created_at"))
    return dt.astimezone(BUSINESS_TZ).date()


def _filters(question, rows):
    q = question.lower()
    filters = {}

    for region in ("north", "south", "east", "west", "central"):
        if re.search(r"\b" + region + r"\b", q):
            filters["region"] = region.title()
            break

    # Product names come from the API; preserve its spelling for answers.
    products = sorted({str(r.get("product")) for r in rows if r.get("product")}, key=len, reverse=True)
    for product in products:
        if product.lower() in q:
            filters["product"] = product
            break

    customer_match = re.search(r"\bC\d{3,}\b", question, re.I)
    if customer_match:
        filters["customer"] = customer_match.group(0).upper()

    # Explicit ISO date(s) take priority.
    iso_dates = re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", question)
    if len(iso_dates) >= 2 and re.search(r"\b(between|from|through|until|to)\b", q):
        try:
            filters["date_start"] = datetime.strptime(iso_dates[0], "%Y-%m-%d").date()
            filters["date_end"] = datetime.strptime(iso_dates[1], "%Y-%m-%d").date()
        except ValueError:
            pass
    elif len(iso_dates) == 1:
        try:
            filters["date_start"] = filters["date_end"] = datetime.strptime(iso_dates[0], "%Y-%m-%d").date()
        except ValueError:
            pass
    else:
        month = None
        for name, number in MONTHS.items():
            if re.search(r"\b" + re.escape(name) + r"\b", q):
                month = number
                break
        year_match = re.search(r"\b(20\d{2})\b", q)
        if month is not None:
            filters["month"] = month
        if year_match:
            filters["year"] = int(year_match.group(1))

    for status in ("paid", "refunded", "void", "pending", "cancelled", "canceled"):
        if re.search(r"\b" + status + r"\b", q):
            filters["status"] = status
            break

    return filters


def _apply_filters(rows, filters):
    result = []
    for row in rows:
        if "region" in filters and str(row.get("region", "")).lower() != filters["region"].lower():
            continue
        if "product" in filters and str(row.get("product", "")).lower() != filters["product"].lower():
            continue
        if "customer" in filters and str(row.get("customer", "")).upper() != filters["customer"]:
            continue
        if "status" in filters and str(row.get("status", "")).lower() != filters["status"]:
            continue

        d = _business_date(row)
        if "date_start" in filters and d < filters["date_start"]:
            continue
        if "date_end" in filters and d > filters["date_end"]:
            continue
        if "month" in filters and d.month != filters["month"]:
            continue
        if "year" in filters and d.year != filters["year"]:
            continue
        result.append(row)
    return result


def _amount_usd(row, rates):
    amount = row.get("amount")
    if amount is None:
        amount = Decimal(str(row.get("qty", 0))) * Decimal(str(row.get("unit_price", 0)))
    currency = str(row.get("currency", "USD")).upper()
    rate = rates.get(currency)
    if rate is None:
        # Unknown currency cannot be converted accurately; don't silently invent a rate.
        return Decimal("0")
    return Decimal(str(amount)) * rate


def _money(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def answer_question(question, rows, rates):
    q = question.strip().lower()
    if not q:
        return "Please provide a question about the order ledger."

    filters = _filters(question, rows)
    selected = _apply_filters(rows, filters)

    is_refund = any(word in q for word in ("refund", "refunded", "refunds"))
    asks_count = bool(re.search(r"\b(how many|count|number of|quantity of)\b", q))
    asks_average = any(word in q for word in ("average", "avg", "mean"))
    asks_list = any(word in q for word in ("list ", "which products", "what products", "list of products"))
    asks_top = any(word in q for word in ("most", "highest", "top", "best-selling", "best selling", "largest", "greatest"))
    asks_lowest = any(word in q for word in ("least", "lowest", "fewest", "smallest"))
    asks_units = any(word in q for word in ("units sold", "items sold", "quantity sold", "number sold", "most sold", "best-selling", "best selling"))
    asks_orders = "order" in q
    asks_customer = "customer" in q or bool(re.search(r"\bC\d{3,}\b", question, re.I))
    asks_product = "product" in q or bool(filters.get("product"))

    if asks_list and ("product" in q):
        return sorted({str(r.get("product")) for r in selected if r.get("product")})

    if "how many customers" in q or "number of customers" in q or "count of customers" in q:
        return len({str(r.get("customer")) for r in selected if r.get("customer")})

    if "how many products" in q or "number of products" in q:
        return len({str(r.get("product")) for r in selected if r.get("product")})

    # Refund metrics use the current order status.
    if is_refund:
        refunded = [r for r in selected if str(r.get("status", "")).lower() == "refunded"]
        if asks_count or "number of refunds" in q:
            return len(refunded)
        total = sum((_amount_usd(r, rates) for r in refunded), Decimal("0"))
        if asks_average:
            return _money(total / len(refunded)) if refunded else 0
        return _money(total)

    # Product rankings.
    if asks_product and asks_top:
        groups = {}
        for row in selected:
            if str(row.get("status", "")).lower() != "paid":
                continue
            name = str(row.get("product", ""))
            metric = Decimal(str(row.get("qty", 0))) if asks_units else _amount_usd(row, rates)
            groups[name] = groups.get(name, Decimal("0")) + metric
        if not groups:
            return ""
        chooser = min if asks_lowest else max
        return chooser(groups, key=groups.get)

    # Customer rankings.
    if asks_customer and asks_top and not filters.get("customer"):
        groups = {}
        for row in selected:
            if str(row.get("status", "")).lower() != "paid":
                continue
            name = str(row.get("customer", ""))
            if not name:
                continue
            if "order" in q and not any(w in q for w in ("spent", "spend", "revenue", "sales", "value")):
                metric = Decimal("1")
            else:
                metric = _amount_usd(row, rates)
            groups[name] = groups.get(name, Decimal("0")) + metric
        if not groups:
            return ""
        chooser = min if asks_lowest else max
        return chooser(groups, key=groups.get)

    if asks_count and asks_orders:
        return len(selected)

    # Revenue is defined by the API as paid orders only.
    if filters.get("status") == "paid":
        revenue_rows = selected
    else:
        revenue_rows = [r for r in selected if str(r.get("status", "")).lower() == "paid"]

    if asks_units:
        return sum(int(r.get("qty", 0) or 0) for r in revenue_rows)

    total = sum((_amount_usd(r, rates) for r in revenue_rows), Decimal("0"))

    if asks_average:
        return _money(total / len(revenue_rows)) if revenue_rows else 0

    if asks_count:
        if asks_customer:
            return len({str(r.get("customer")) for r in selected if r.get("customer")})
        return len(revenue_rows)

    if asks_customer and filters.get("customer"):
        if any(w in q for w in ("order", "orders", "how many")):
            return sum(1 for r in selected if str(r.get("status", "")).lower() == "paid")
        return _money(total)

    if asks_orders and not any(w in q for w in ("revenue", "sales", "spent", "spend", "amount", "value")):
        return len(selected)

    return _money(total)


@app.get("/")
def health():
    return {"service": "Acme Appliances ledger agent", "status": "ok"}


@app.post("/")
def ask(request: QuestionRequest):
    try:
        rows, rates = load_ledger()
        answer = answer_question(request.question, rows, rates)
        return {"answer": answer}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
