"""SQLite: products, shoppers, orders, sessions (memory) and traces (audit log). One file, zero setup."""
import json  # lists/dicts (attributes, reviews, traces) are stored as JSON text
import sqlite3  # Python's built-in database driver, no server needed
from contextlib import contextmanager  # lets connect() be used as "with connect() as conn:"

from app import config  # DB_PATH and DATA_DIR

# The 5 tables. "IF NOT EXISTS" = safe to run on every start.
SCHEMA = """
CREATE TABLE IF NOT EXISTS products (product_id TEXT PRIMARY KEY, name TEXT, category TEXT, product_type TEXT, brand TEXT,
    price INTEGER, stock INTEGER, attributes TEXT, description TEXT, rating REAL, reviews TEXT);
CREATE TABLE IF NOT EXISTS shoppers (customer_id TEXT PRIMARY KEY, name TEXT, profile TEXT);
CREATE TABLE IF NOT EXISTS orders (order_id TEXT PRIMARY KEY, customer_id TEXT, product_id TEXT, price_paid INTEGER, order_date TEXT);
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, customer_id TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS traces (trace_id TEXT PRIMARY KEY, session_id TEXT, record TEXT);
"""
COLS = ["product_id", "name", "category", "product_type", "brand", "price", "stock", "attributes", "description", "rating", "reviews"]  # products columns, in order


@contextmanager  # code before "yield" runs on enter, code after it on exit
def connect():  # "with connect() as conn:" -> opens, commits, closes
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)  # open the file; allow use from API threads
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["price"]
    try:
        yield conn  # hand the connection to the "with" block
        conn.commit()  # block finished OK -> save changes
    finally:
        conn.close()  # always close, even after an error


def query(sql: str, args=()) -> list[dict]:  # run a SELECT, get a list of dicts
    with connect() as conn:  # open + auto-close
        return [dict(r) for r in conn.execute(sql, args)]  # each row -> plain dict


def to_product(row) -> dict:  # DB row -> product dict (JSON columns decoded)
    p = dict(row)  # Row -> dict
    p["attributes"], p["reviews"] = json.loads(p["attributes"] or "{}"), json.loads(p["reviews"] or "[]")  # JSON text -> dict / list
    return p  # ready-to-use product


def init_db(reset: bool = False) -> None:  # create tables; on first start load data/*.json
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)  # make sure the folder exists
    if reset and config.DB_PATH.exists():  # tests / eval want a clean start
        config.DB_PATH.unlink()  # delete the old database file
    with connect() as conn:
        conn.executescript(SCHEMA)  # create any missing tables
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]:  # products already there?
            return  # already loaded
        load = lambda f: json.loads((config.DATA_DIR / f).read_text(encoding="utf-8"))  # tiny helper: read one JSON file
        for p in load("catalogue.json"):  # 276 products
            _upsert(conn, p)
        for s in load("shoppers.json"):  # 29 shoppers (name + full profile as JSON)
            conn.execute("INSERT INTO shoppers VALUES (?,?,?)", (s["customer_id"], s["name"], json.dumps(s, ensure_ascii=False)))
        for o in load("orders.json"):  # 105 orders
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?)",
                         (o["order_id"], o["customer_id"], o["product_id"], o["price_paid"], o["order_date"]))  # "?" = safe placeholders


def _upsert(conn, p: dict) -> None:  # insert or replace one product; "?" placeholders = no SQL injection
    vals = [p.get(c) for c in COLS]  # values in column order (None if missing)
    vals[7] = json.dumps(p.get("attributes") or {}, ensure_ascii=False)  # attributes dict -> JSON text
    vals[10] = json.dumps(p.get("reviews") or [], ensure_ascii=False)  # reviews list -> JSON text
    conn.execute(f"INSERT OR REPLACE INTO products VALUES ({','.join('?' * len(COLS))})", vals)  # 11 placeholders


def upsert_product(p: dict) -> None:  # public version, used by POST /ingest
    with connect() as conn:  # own connection + commit
        _upsert(conn, p)


def get_products(ids: list[str]) -> list[dict]:  # in the same order as ids
    if not ids:  # nothing asked
        return []
    with connect() as conn:
        rows = {r["product_id"]: to_product(r) for r in conn.execute(  # id -> product (SQL returns any order)
            f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(ids))})", ids)}  # one "?" per id
    return [rows[i] for i in ids if i in rows]  # put back in the caller's order


def get_product(pid: str) -> dict | None:  # one product, or None
    found = get_products([pid])
    return found[0] if found else None


def all_products() -> list[dict]:  # the whole catalogue (to build the search index)
    with connect() as conn:
        return [to_product(r) for r in conn.execute("SELECT * FROM products")]


def save_trace(trace: dict) -> None:  # R10: one audit record per reply
    with connect() as conn:
        conn.execute("INSERT INTO traces VALUES (?,?,?)",
                     (trace["trace_id"], trace["session_id"], json.dumps(trace, ensure_ascii=False, default=str)))  # whole trace as JSON


def get_trace(trace_id: str) -> dict | None:  # used by GET /trace/{id}
    rows = query("SELECT record FROM traces WHERE trace_id = ?", (trace_id,))
    return json.loads(rows[0]["record"]) if rows else None  # JSON text -> dict, or None if unknown
