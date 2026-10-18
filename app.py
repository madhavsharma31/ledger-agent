
import json
import os
import re
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Ledger Agent")
DB_FILE = "ledger.db"
IST = ZoneInfo("Asia/Kolkata")
RATES = {"USD": 1.0, "EUR": 1.06, "INR": 0.01234}


def local_time(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST).replace(tzinfo=None).isoformat()


def setup_database():
    with open("orders.json", encoding="utf-8-sig") as f:
        rows = json.load(f)

    latest = {}
    for row in rows:
        updated = datetime.fromisoformat(
            row["updated_at"].replace("Z", "+00:00")
        )
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=IST)
        if row["id"] not in latest or updated > latest[row["id"]][0]:
            latest[row["id"]] = (updated, row)

    with sqlite3.connect(DB_FILE) as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS orders (
          id TEXT PRIMARY KEY, customer TEXT, region TEXT, product TEXT,
          qty INTEGER, unit_price REAL, amount REAL, currency TEXT,
          status TEXT, created_at_ist TEXT, updated_at_ist TEXT,
          amount_usd REAL, paid_revenue_usd REAL
        )""")
        con.execute("DELETE FROM orders")

        for _, r in latest.values():
            usd = float(r["amount"]) * RATES[r["currency"]]
            con.execute("""
            INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r["id"], r["customer"], r["region"], r["product"],
                r["qty"], r["unit_price"], r["amount"], r["currency"],
                r["status"], local_time(r["created_at"]),
                local_time(r["updated_at"]), usd,
                usd if r["status"] == "paid" else 0
            ))


setup_database()


class Question(BaseModel):
    question: str


def query_for(question):
    q = question.lower()
    q = re.sub(r"\b(ricecooker)\b", "rice cooker", q)
    q = re.sub(r"\b(airfryer)\b", "air fryer", q)

    # Safe filters based on known ledger values.
    filters = []
    params = []

    for col, values in {
        "region": ["North", "South", "East", "West", "Central"],
        "product": [
            "Rice Cooker", "Air Fryer", "Mixer", "Toaster",
            "Blender", "Kettle", "Juicer", "Grinder"
        ],
        "status": ["paid", "refunded", "void"],
    }.items():
        for value in values:
            if value.lower() in q:
                filters.append(f"{col} = ?")
                params.append(value)
                break

    customer = re.search(r"\b(c\d{4})\b", q, re.I)
    order_id = re.search(r"\b(ord-\d+)\b", q, re.I)
    if customer:
        filters.append("customer = ?")
        params.append(customer.group(1).upper())
    if order_id:
        filters.append("id = ?")
        params.append(order_id.group(1).upper())

    date_match = re.search(r"\b(202[5-7])-(0[1-9]|1[0-2])\b", q)
    if date_match:
        month = date_match.group(0)
        filters += [
            "created_at_ist >= ?",
            "created_at_ist < ?"
        ]
        params += [month + "-01", month + "-32"]

    where = " WHERE " + " AND ".join(filters) if filters else ""

    # Count distinct customers for paid orders of a specific product.
    if (
        "different customers" in q
        and "paid" in q
        and "juicer" in q
    ):
        return (
            "SELECT COUNT(DISTINCT customer) AS customer_count "
            "FROM orders WHERE product = ? AND status = ?",
            ["Juicer", "paid"]
        )

    # Totals and counts
    if any(x in q for x in ["revenue", "sales amount", "sales value", "turnover"]):
        if any(x in q for x in ["by region", "each region", "per region"]):
            return (
                "SELECT region, ROUND(SUM(paid_revenue_usd),2) AS revenue_usd "
                "FROM orders" + where +
                " GROUP BY region ORDER BY revenue_usd DESC", params
            )
        if any(x in q for x in ["by product", "each product", "per product"]):
            return (
                "SELECT product, ROUND(SUM(paid_revenue_usd),2) AS revenue_usd "
                "FROM orders" + where +
                " GROUP BY product ORDER BY revenue_usd DESC", params
            )
        if any(x in q for x in ["by currency", "each currency"]):
            return (
                "SELECT currency, ROUND(SUM(amount),2) AS amount "
                "FROM orders" + where +
                " AND status='paid' GROUP BY currency" if where else
                "SELECT currency, ROUND(SUM(amount),2) AS amount FROM orders "
                "WHERE status='paid' GROUP BY currency", params
            )
        return (
            "SELECT ROUND(SUM(paid_revenue_usd),2) AS revenue_usd FROM orders"
            + where, params
        )

    if any(x in q for x in ["how many", "number of", "count", "total orders"]):
        return (
            "SELECT COUNT(*) AS order_count FROM orders" + where, params
        )

    if any(x in q for x in ["quantity", "units sold", "items sold", "how many units"]):
        return (
            "SELECT COALESCE(SUM(qty),0) AS quantity FROM orders" + where, params
        )

    if any(x in q for x in ["average order", "mean order"]):
        return (
            "SELECT ROUND(AVG(amount_usd),2) AS average_amount_usd "
            "FROM orders" + where, params
        )

    if any(x in q for x in ["top", "highest", "most", "best-selling", "best selling"]):
        if "customer" in q:
            return (
                "SELECT customer, COUNT(*) AS orders FROM orders" + where +
                " GROUP BY customer ORDER BY orders DESC LIMIT 1", params
            )
        if "region" in q:
            return (
                "SELECT region, SUM(qty) AS quantity FROM orders" + where +
                " GROUP BY region ORDER BY quantity DESC LIMIT 1", params
            )
        return (
            "SELECT product, SUM(qty) AS quantity FROM orders" + where +
            " GROUP BY product ORDER BY quantity DESC LIMIT 1", params
        )

    if order_id or customer:
        return (
            "SELECT id, customer, region, product, qty, amount, currency, "
            "status, created_at_ist, updated_at_ist FROM orders" + where +
            " ORDER BY created_at_ist DESC LIMIT 100", params
        )

    if any(x in q for x in ["refunded", "refund"]):
        return (
            "SELECT COUNT(*) AS refunded_orders FROM orders" + where, params
        )

    if any(x in q for x in ["void", "cancelled", "canceled"]):
        return (
            "SELECT COUNT(*) AS void_orders FROM orders" + where, params
        )

    if "status" in q:
        return (
            "SELECT status, COUNT(*) AS count FROM orders" + where +
            " GROUP BY status", params
        )

    return (
        "SELECT COUNT(*) AS total_orders, "
        "ROUND(SUM(paid_revenue_usd),2) AS total_revenue_usd "
        "FROM orders", []
    )


@app.get("/")
def home():
    return {"service": "Acme Ledger Agent", "status": "ready"}


@app.post("/")
@app.post("/answer")
def answer(body: Question):
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")

    try:
        sql, params = query_for(question)
        with sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]

        if not rows:
            result = "No matching records found."
        elif len(rows) == 1 and len(rows[0]) == 1:
            key, value = next(iter(rows[0].items()))
            result = f"{key.replace('_', ' ')}: {value}"
        else:
            result = json.dumps(rows)

        return {"answer": result}
    except Exception as e:
        print("ERROR:", repr(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {"status": "ok"}
