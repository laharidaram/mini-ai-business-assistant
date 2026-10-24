"""Storage layer: CSV -> SQLite `sales` table (every readable row is kept) + `rejected_rows` audit table."""
import csv
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path


class AppError(Exception):
    """An error whose message is safe to show to the user."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


REQUIRED = ["transaction_id", "order_date", "product", "category", "region",
            "sales_channel", "quantity", "unit_price", "discount_amount", "status"]

# revenue_status: 'valid' = quantity x price - discount | 'zero_quantity' = known to be 0
#                 | 'unknown_price' = revenue cannot be calculated, stored as NULL (never guessed)
SCHEMA = """
CREATE TABLE sales (
  transaction_id  TEXT PRIMARY KEY,
  order_date      TEXT NOT NULL,                       -- ISO yyyy-mm-dd (source is dd-mm-yyyy)
  month           TEXT NOT NULL,                       -- yyyy-mm, for fast grouping
  product TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL, sales_channel TEXT NOT NULL,
  quantity        INTEGER NOT NULL CHECK (quantity >= 0),
  unit_price      REAL CHECK (unit_price IS NULL OR unit_price > 0),
  discount_amount REAL NOT NULL CHECK (discount_amount >= 0),
  status          TEXT NOT NULL,
  revenue         REAL CHECK (revenue IS NULL OR revenue >= 0),
  revenue_status  TEXT NOT NULL CHECK (revenue_status IN ('valid', 'zero_quantity', 'unknown_price')),
  CHECK ((revenue_status = 'unknown_price') = (revenue IS NULL))
);
CREATE INDEX idx_sales_status_month ON sales (status, month);
CREATE TABLE rejected_rows (line_no INTEGER, transaction_id TEXT, reason TEXT, raw TEXT);
"""


def _num(v):
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _clean(row, seen):
    """Return (record, None) for a readable row or (None, reason) for one that cannot be trusted at all.
    Rows with a zero quantity or a missing price are KEPT and flagged through `revenue_status`."""
    r = {k: (row.get(k) or "").strip() for k in REQUIRED}
    if not r["transaction_id"]:
        return None, "missing_transaction_id"
    if r["transaction_id"] in seen:
        return None, "duplicate_transaction_id"
    try:
        d = datetime.strptime(r["order_date"], "%d-%m-%Y")      # dd-mm-yyyy, NOT US order
    except ValueError:
        return None, "invalid_date"
    for f in ("product", "category", "region", "sales_channel", "status"):
        if not r[f]:
            return None, f"missing_{f}"
    q = _num(r["quantity"])
    if q is None or q != int(q):
        return None, "invalid_quantity"
    if q < 0:
        return None, "negative_quantity"
    disc = _num(r["discount_amount"]) if r["discount_amount"] else 0.0   # blank discount = no discount
    if disc is None or disc < 0:
        return None, "invalid_discount"
    p = _num(r["unit_price"])
    p = p if p is not None and p > 0 else None                  # blank / non-numeric / non-positive = no valid price
    if q == 0:
        revenue, status = 0.0, "zero_quantity"                  # nothing sold -> revenue is known to be 0
    elif p is None:
        revenue, status = None, "unknown_price"                 # cannot be calculated; do not guess
    elif disc > q * p:
        return None, "discount_exceeds_total"
    else:
        revenue, status = q * p - disc, "valid"
    return (r["transaction_id"], d.date().isoformat(), d.strftime("%Y-%m"), r["product"], r["category"],
            r["region"], r["sales_channel"], int(q), p, disc, r["status"].title(), revenue, status), None


def load_csv(csv_path, db_path):
    """(Re)build the database from the CSV. Returns (rows_loaded, rows_rejected)."""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rd = csv.DictReader(f)
        rd.fieldnames = [h.strip().lower() for h in rd.fieldnames or []]
        missing = set(REQUIRED) - set(rd.fieldnames)
        if missing:
            raise ValueError(f"CSV is missing columns: {sorted(missing)}")
        good, bad, seen = [], [], set()
        for line, row in enumerate(rd, start=2):                  # line 1 is the header
            rec, why = _clean(row, seen)
            if rec:
                seen.add(rec[0])
                good.append(rec)
            else:
                bad.append((line, (row.get("transaction_id") or "").strip(), why, json.dumps(row)))
    Path(db_path).unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    with con:
        con.executescript(SCHEMA)
        con.executemany("INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", good)
        con.executemany("INSERT INTO rejected_rows VALUES (?,?,?,?)", bad)
    con.close()
    return len(good), len(bad)


def connect(db_path):
    """Read-only connection: even a bug cannot modify the data."""
    return sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)


def data_quality(conn):
    """Single source for every data-quality figure shown anywhere (answers, notes, UI, API)."""
    flagged_cols = ("transaction_id", "product", "order_date", "quantity", "unit_price", "status", "revenue_status")
    return {
        "loaded": conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0],
        "by_status": dict(conn.execute("SELECT status, COUNT(*) FROM sales GROUP BY status ORDER BY status")),
        "revenue_status": dict(conn.execute("SELECT revenue_status, COUNT(*) FROM sales GROUP BY revenue_status")),
        "flagged": [dict(zip(flagged_cols, r)) for r in conn.execute(
            f"SELECT {', '.join(flagged_cols)} FROM sales WHERE revenue_status != 'valid' ORDER BY transaction_id")],
        "rejected": [dict(zip(("line", "transaction_id", "reason", "raw"), r))
                     for r in conn.execute("SELECT * FROM rejected_rows ORDER BY line_no")],
    }
