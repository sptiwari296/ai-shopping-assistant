"""SQLite storage: catalogue, shoppers, orders, session memory and traces.

HOW: one file (data/shop.db). On first start it is filled from the JSON files in
data/. Lists and dicts (attributes, reviews, likes) are stored as JSON text.
WHY SQLite: zero setup, one file, real SQL for exact filters (price, brand, stock),
and the brief suggests it.
"""
import json
import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL,
    product_type TEXT,
    brand        TEXT,            -- may be NULL (messy data on purpose)
    price        INTEGER NOT NULL,
    stock        INTEGER NOT NULL,
    attributes   TEXT,            -- JSON
    description  TEXT,
    rating       REAL,            -- may be NULL
    reviews      TEXT             -- JSON list
);
CREATE TABLE IF NOT EXISTS shoppers (
    customer_id        TEXT PRIMARY KEY,
    name               TEXT,
    city               TEXT,
    preferred_language TEXT,
    budget_min         INTEGER,
    budget_max         INTEGER,
    likes              TEXT,      -- JSON
    favourite_brands   TEXT,      -- JSON
    notes              TEXT,
    edge_case          TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id    TEXT PRIMARY KEY,
    customer_id TEXT REFERENCES shoppers(customer_id),
    product_id  TEXT REFERENCES products(product_id),
    quantity    INTEGER,
    price_paid  INTEGER,
    order_date  TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    customer_id TEXT,
    state       TEXT,             -- JSON: last filters, last results, questions asked
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS traces (
    trace_id    TEXT PRIMARY KEY,
    session_id  TEXT,
    customer_id TEXT,
    created_at  TEXT,
    record      TEXT              -- JSON: question, extracted, retrieved, tools, answer, latency
);
"""

JSON_COLS = {"attributes", "reviews", "likes", "favourite_brands", "state", "record"}


@contextmanager
def connect():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_COLS & d.keys():
        if d[k] is not None:
            d[k] = json.loads(d[k])
    return d


def init_db(reset: bool = False) -> None:
    """Create tables; load seed JSON if the products table is empty."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            _load_seed(conn)


def _load_seed(conn) -> None:
    read = lambda f: json.loads((config.DATA_DIR / f).read_text(encoding="utf-8"))
    for p in read("catalogue.json"):
        _upsert(conn, p)
    for s in read("shoppers.json"):
        conn.execute(
            "INSERT INTO shoppers VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s["customer_id"], s["name"], s["city"], s["preferred_language"], s["budget_min"], s["budget_max"],
             json.dumps(s["likes"]), json.dumps(s["favourite_brands"], ensure_ascii=False), s["notes"], s.get("edge_case")),
        )
    for o in read("orders.json"):
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",
                     (o["order_id"], o["customer_id"], o["product_id"], o["quantity"], o["price_paid"], o["order_date"]))


def _upsert(conn, p: dict) -> None:
    conn.execute(
        """INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(product_id) DO UPDATE SET name=excluded.name, category=excluded.category,
             product_type=excluded.product_type, brand=excluded.brand, price=excluded.price, stock=excluded.stock,
             attributes=excluded.attributes, description=excluded.description, rating=excluded.rating,
             reviews=excluded.reviews""",
        (p["product_id"], p["name"], p["category"], p.get("product_type"), p.get("brand"), int(p["price"]),
         int(p["stock"]), json.dumps(p.get("attributes", {}), ensure_ascii=False), p.get("description"),
         p.get("rating"), json.dumps(p.get("reviews", []), ensure_ascii=False)),
    )


def upsert_product(p: dict) -> None:
    with connect() as conn:
        _upsert(conn, p)


def get_product(product_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()
    return row_to_dict(row) if row else None


def get_products(ids: list[str]) -> list[dict]:
    if not ids:
        return []
    with connect() as conn:
        q = f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(ids))})"
        rows = {r["product_id"]: row_to_dict(r) for r in conn.execute(q, ids)}
    return [rows[i] for i in ids if i in rows]  # keep the caller's order


def all_products() -> list[dict]:
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute("SELECT * FROM products")]


def get_shopper(customer_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM shoppers WHERE customer_id = ?", (customer_id,)).fetchone()
    return row_to_dict(row) if row else None
