
import json
import re
import sqlite3
import calendar
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Ledger Agent")

DB_FILE = "ledger.db"
IST = ZoneInfo("Asia/Kolkata")

RATES = {
    "USD": 1.0,
    "EUR": 1.06,
    "INR": 0.01234,
}

PRODUCTS = [
    "Rice Cooker",
    "Air Fryer",
    "Mixer",
    "Toaster",
    "Blender",
    "Kettle",
    "Juicer",
    "Grinder",
]

REGIONS = ["North", "South", "East", "West", "Central"]
STATUSES = ["paid", "refunded", "void"]


def local_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).replace(tzinfo=None).isoformat()


def setup_database():
    with open("orders.json", encoding="utf-8-sig") as f:
        rows = json.load(f)

    # Keep the latest version of every order.
    latest = {}

    for row in rows:
        updated = datetime.fromisoformat(
            row["updated_at"].replace("Z", "+00:00")
        )
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=IST)

        order_id = row["id"]

        if (
            order_id not in latest
            or updated > latest[order_id][0]
        ):
            latest[order_id] = (updated, row)

    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT,
                region TEXT,
                product TEXT,
                qty INTEGER,
                unit_price REAL,
                amount REAL,
                currency TEXT,
                status TEXT,
                created_at_ist TEXT,
                updated_at_ist TEXT,
                amount_usd REAL,
                paid_revenue_usd REAL
            )
        """)

        con.execute("DELETE FROM orders")

        for _, row in latest.values():
            amount_usd = float(row["amount"]) * RATES[row["currency"]]
            paid_revenue = (
                amount_usd if row["status"].lower() == "paid" else 0
            )

            con.execute("""
                INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                row["id"],
                row["customer"],
                row["region"],
                row["product"],
                row["qty"],
                row["unit_price"],
                row["amount"],
                row["currency"],
                row["status"].lower(),
                local_time(row["created_at"]),
                local_time(row["updated_at"]),
                amount_usd,
                paid_revenue,
            ))


setup_database()


class Question(BaseModel):
    question: str


def query_for(question):
    q = question.lower()
    q = re.sub(r"\brice[\s_-]*cooker\b", "rice cooker", q)
    q = re.sub(r"\bair[\s_-]*fryer\b", "air fryer", q)

    filters = []
    params = []

    # Recognize exact known values.
    for column, values in {
        "region": REGIONS,
        "product": PRODUCTS,
        "status": STATUSES,
    }.items():
        for value in values:
            if value.lower() in q:
                filters.append(f"{column} = ?")
                params.append(value if column != "status" else value.lower())
                break

    customer_match = re.search(r"\b(c\d{4})\b", q, re.I)
    order_match = re.search(r"\b(ord-\d+)\b", q, re.I)

    if customer_match:
        filters.append("customer = ?")
        params.append(customer_match.group(1).upper())

    if order_match:
        filters.append("id = ?")
        params.append(order_match.group(1).upper())

    # Correct month boundaries, interpreted in IST.
    date_match = re.search(
        r"\b(202[5-7])-(0[1-9]|1[0-2])\b", q
    )

    if date_match:
        year = int(date_match.group(1))
        month_num = int(date_match.group(2))
        last_day = calendar.monthrange(year, month_num)[1]

        start = f"{year:04d}-{month_num:02d}-01T00:00:00"
        if month_num == 12:
            end = f"{year + 1:04d}-01-01T00:00:00"
        else:
            end = f"{year:04d}-{month_num + 1:02d}-01T00:00:00"

        filters.extend([
            "created_at_ist >= ?",
            "created_at_ist < ?",
        ])
        params.extend([start, end])

    where = " WHERE " + " AND ".join(filters) if filters else ""

    # Distinct customers must be checked before generic count questions.
    if (
        any(x in q for x in [
            "different customers",
            "distinct customers",
            "unique customers",
        ])
        and any(x in q for x in [
            "paid", "payment", "at least one paid"
        ])
    ):
        product = next(
            (p for p in PRODUCTS if p.lower() in q),
            None,
        )

        if product:
            return (
                """
                SELECT COUNT(DISTINCT customer) AS customer_count
                FROM orders
                WHERE product = ? AND status = 'paid'
                """
                ,
                [product],
            )

        return (
            """
            SELECT COUNT(DISTINCT customer) AS customer_count
            FROM orders WHERE status = 'paid'
            """
            ,
            [],
        )

    # Revenue queries: only paid orders contribute to revenue.
    if any(x in q for x in [
        "revenue", "sales amount", "sales value", "turnover"
    ]):
        if any(x in q for x in [
            "by region", "each region", "per region"
        ]):
            return (
                """
                SELECT region,
                       ROUND(SUM(paid_revenue_usd), 2) AS revenue_usd
                FROM orders
                """
                + where
                + " GROUP BY region ORDER BY revenue_usd DESC",
                params,
            )

        if any(x in q for x in [
            "by product", "each product", "per product"
        ]):
            return (
                """
                SELECT product,
                       ROUND(SUM(paid_revenue_usd), 2) AS revenue_usd
                FROM orders
                """
                + where
                + " GROUP BY product ORDER BY revenue_usd DESC",
                params,
            )

        if any(x in q for x in ["by currency", "each currency"]):
            currency_where = (
                where + " AND status = 'paid'"
                if where
                else " WHERE status = 'paid'"
            )
            return (
                """
                SELECT currency, ROUND(SUM(amount), 2) AS amount
                FROM orders
                """
                + currency_where
                + " GROUP BY currency ORDER BY currency",
                params,
            )

        return (
            """
            SELECT ROUND(SUM(paid_revenue_usd), 2) AS revenue_usd
            FROM orders
            """
            + where,
            params,
        )

    # Quantity questions must be evaluated before generic counts.
    if any(x in q for x in [
        "units sold", "items sold", "total quantity",
        "total units", "quantity", "how many units"
    ]):
        return (
            "SELECT COALESCE(SUM(qty), 0) AS quantity FROM orders" + where,
            params,
        )

    # Average order value.
    if any(x in q for x in [
        "average order", "mean order", "average value"
    ]):
        return (
            """
            SELECT ROUND(AVG(amount_usd), 2) AS average_amount_usd
            FROM orders
            """
            + where,
            params,
        )

    # Top/best-selling queries.
    if any(x in q for x in [
        "top", "highest", "most", "best-selling", "best selling"
    ]):
        if "customer" in q:
            return (
                """
                SELECT customer, COUNT(*) AS order_count
                FROM orders
                """
                + where
                + " GROUP BY customer ORDER BY order_count DESC LIMIT 1",
                params,
            )

        if "region" in q:
            return (
                """
                SELECT region, SUM(qty) AS quantity
                FROM orders
                """
                + where
                + " GROUP BY region ORDER BY quantity DESC LIMIT 1",
                params,
            )

        return (
            """
            SELECT product, SUM(qty) AS quantity
            FROM orders
            """
            + where
            + " GROUP BY product ORDER BY quantity DESC LIMIT 1",
            params,
        )

    # Specific order or customer lookup.
    if order_match or customer_match:
        return (
            """
            SELECT id, customer, region, product, qty, amount, currency,
                   status, created_at_ist, updated_at_ist
            FROM orders
            """
            + where
            + " ORDER BY created_at_ist DESC LIMIT 100",
            params,
        )

    # Status-specific counts.
    if "refunded" in q or "refund" in q:
        return (
            "SELECT COUNT(*) AS refunded_orders FROM orders" + where,
            params,
        )

    if any(x in q for x in ["void", "cancelled", "canceled"]):
        return (
            "SELECT COUNT(*) AS void_orders FROM orders" + where,
            params,
        )

    if "status" in q:
        return (
            """
            SELECT status, COUNT(*) AS count
            FROM orders
            """
            + where
            + " GROUP BY status ORDER BY status",
            params,
        )

    # Generic count questions.
    if any(x in q for x in [
        "how many", "number of", "count", "total orders"
    ]):
        return (
            "SELECT COUNT(*) AS order_count FROM orders" + where,
            params,
        )

    # Fallback summary.
    return (
        """
        SELECT COUNT(*) AS total_orders,
               ROUND(SUM(paid_revenue_usd), 2) AS total_revenue_usd
        FROM orders
        """,
        [],
    )


@app.get("/")
def home():
    return {
        "service": "Acme Ledger Agent",
        "status": "ready",
    }


@app.post("/")
@app.post("/answer")
def answer(body: Question):
    question = body.question.strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail="Question is required",
        )

    try:
        sql, params = query_for(question)

        with sqlite3.connect(
            f"file:{DB_FILE}?mode=ro", uri=True
        ) as con:
            con.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in con.execute(sql, params).fetchall()
            ]

        if not rows:
            result = "No matching records found."
        elif len(rows) == 1 and len(rows[0]) == 1:
            key, value = next(iter(rows[0].items()))
            result = f"{key.replace('_', ' ')}: {value}"
        else:
            result = json.dumps(rows)

        return {"answer": result}

    except Exception as exc:
        print("ERROR:", repr(exc))
        raise HTTPException(
            status_code=500,
            detail="Unable to process question",
        )


@app.get("/health")
def health():
    return {"status": "ok"}
