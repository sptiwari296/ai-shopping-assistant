"""SQLite: products, shoppers, orders, sessions (memory) and traces (audit log). One file, zero setup."""
import json
import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (product_id TEXT PRIMARY KEY, name TEXT, category TEXT, product_type TEXT, brand TEXT,
    price INTEGER, stock INTEGER, attributes TEXT, description TEXT, rating REAL, reviews TEXT);
CREATE TABLE IF NOT EXISTS shoppers (customer_id TEXT PRIMARY KEY, name TEXT, profile TEXT);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, customer_id TEXT, product_id TEXT, price_paid INTEGER, order_date TEXT);
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, customer_id TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS traces (trace_id TEXT PRIMARY KEY, session_id TEXT, record TEXT);
"""
COLS = ["product_id", "name", "category", "product_type", "brand", "price", "stock", "attributes", "description", "rating", "reviews"]


@contextmanager
def connect():  # "with connect() as conn:" -> opens, commits, closes
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["price"]
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def query(sql: str, args=()) -> list[dict]:  # run a SELECT, get a list of dicts
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def to_product(row) -> dict:  # DB row -> product dict (JSON columns decoded)
    p = dict(row)
    p["attributes"], p["reviews"] = json.loads(p["attributes"] or "{}"), json.loads(p["reviews"] or "[]")
    return p


def init_db(reset: bool = False) -> None:  # create tables; on first start load data/*.json
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    with connect() as conn:
        conn.executescript(SCHEMA)
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:
            return  # already loaded
        load = lambda f: json.loads((config.DATA_DIR / f).read_text(encoding="utf-8"))
        for p in load("catalogue.json"):
            _upsert(conn, p)
        for s in load("shoppers.json"):
            conn.execute("INSERT INTO shoppers VALUES (?,?,?)", (s["customer_id"], s["name"], json.dumps(s, ensure_ascii=False)))
        for o in load("orders.json"):
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)",
                         (o["order_id"], o["customer_id"], o["product_id"], o["price_paid"], o["order_date"]))


def _upsert(conn, p: dict) -> None:  # insert or replace one product; "?" placeholders = no SQL injection
    vals = [p.get(c) for c in COLS]
    vals[7] = json.dumps(p.get("attributes") or {}, ensure_ascii=False)
    vals[10] = json.dumps(p.get("reviews") or [], ensure_ascii=False)
    conn.execute(f"INSERT OR REPLACE INTO products VALUES ({','.join('?' * len(COLS))})", vals)


def upsert_product(p: dict) -> None:
    with connect() as conn:
        _upsert(conn, p)


def get_products(ids: list[str]) -> list[dict]:  # in the same order as ids
    if not ids:
        return []
    with connect() as conn:
        rows = {r["product_id"]: to_product(r) for r in conn.execute(
            f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(ids))})", ids)}
    return [rows[i] for i in ids if i in rows]


def get_product(pid: str) -> dict | None:
    found = get_products([pid])
    return found[0] if found else None


def all_products() -> list[dict]:
    with connect() as conn:
        return [to_product(r) for r in conn.execute("SELECT * FROM products")]


def save_trace(trace: dict) -> None:  # R10: one audit record per reply
    with connect() as conn:
        conn.execute("INSERT INTO traces VALUES (?,?,?)",
                     (trace["trace_id"], trace["session_id"], json.dumps(trace, ensure_ascii=False, default=str)))


def get_trace(trace_id: str) -> dict | None:
    rows = query("SELECT record FROM traces WHERE trace_id = ?", (trace_id,))
    return json.loads(rows[0]["record"]) if rows else None
