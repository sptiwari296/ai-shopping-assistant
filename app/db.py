"""SQLite storage: catalogue, shoppers, orders, session memory and traces.

HOW: one file (data/shop.db). On first start it is filled from the JSON files in
data/. Lists and dicts (attributes, reviews, likes) are stored as JSON text.
WHY SQLite: zero setup, one file, real SQL for exact filters (price, brand, stock),
and the brief suggests it.
"""
import json  # store lists/dicts (attributes, reviews, state) as JSON text in SQLite
import sqlite3  # Python's built-in SQLite driver (no server needed)
from contextlib import contextmanager  # lets connect() be used with "with ... as conn:"

from app import config  # DB_PATH and DATA_DIR

# The table definitions. "IF NOT EXISTS" = safe to run on every start. (SQL comments inside use --)
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

JSON_COLS = {"attributes", "reviews", "likes", "favourite_brands", "state", "record"}  # columns holding JSON text


@contextmanager  # turns this generator into a "with" block: code before yield = enter, after = exit
def connect():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)  # open the DB file; allow use from API threads
    conn.row_factory = sqlite3.Row  # rows behave like dicts: row["price"] instead of row[5]
    try:
        yield conn  # hand the connection to the "with" block
        conn.commit()  # block finished without error -> save the changes
    finally:
        conn.close()  # always close, even if the block raised an error


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)  # sqlite Row -> normal dict
    for k in JSON_COLS & d.keys():  # only the JSON columns present in this row (set intersection)
        if d[k] is not None:  # NULL stays None
            d[k] = json.loads(d[k])  # JSON text -> Python list/dict
    return d  # e.g. {"product_id": "P-1046", "attributes": {...}, ...}


def init_db(reset: bool = False) -> None:
    """Create tables; load seed JSON if the products table is empty."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)  # make sure the folder exists
    if reset and config.DB_PATH.exists():  # tests/eval ask for a clean start
        config.DB_PATH.unlink()  # delete the old DB file
    with connect() as conn:  # open (creates the file if missing)
        conn.executescript(SCHEMA)  # create all tables that don't exist yet
        if conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:  # empty catalogue?
            _load_seed(conn)  # first start: fill from data/*.json


def _load_seed(conn) -> None:
    read = lambda f: json.loads((config.DATA_DIR / f).read_text(encoding="utf-8"))  # small helper: read one JSON file
    for p in read("catalogue.json"):  # 276 products
        _upsert(conn, p)  # insert each product
    for s in read("shoppers.json"):  # 29 shoppers
        conn.execute(
            "INSERT INTO shoppers VALUES (?,?,?,?,?,?,?,?,?,?)",  # ? = placeholders: values are passed separately (no SQL injection)
            (s["customer_id"], s["name"], s["city"], s["preferred_language"], s["budget_min"], s["budget_max"],
             json.dumps(s["likes"]), json.dumps(s["favourite_brands"], ensure_ascii=False), s["notes"], s.get("edge_case")),  # lists -> JSON text
        )
    for o in read("orders.json"):  # 105 orders
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?)",  # one row per order
                     (o["order_id"], o["customer_id"], o["product_id"], o["quantity"], o["price_paid"], o["order_date"]))


def _upsert(conn, p: dict) -> None:
    conn.execute(  # insert a product, or update it if the product_id already exists ("upsert")
        """INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(product_id) DO UPDATE SET name=excluded.name, category=excluded.category,
             product_type=excluded.product_type, brand=excluded.brand, price=excluded.price, stock=excluded.stock,
             attributes=excluded.attributes, description=excluded.description, rating=excluded.rating,
             reviews=excluded.reviews""",
        (p["product_id"], p["name"], p["category"], p.get("product_type"), p.get("brand"), int(p["price"]),  # .get -> None if missing
         int(p["stock"]), json.dumps(p.get("attributes", {}), ensure_ascii=False), p.get("description"),  # ensure_ascii=False keeps Hindi readable
         p.get("rating"), json.dumps(p.get("reviews", []), ensure_ascii=False)),
    )


def upsert_product(p: dict) -> None:  # public version used by POST /ingest
    with connect() as conn:  # own connection + commit
        _upsert(conn, p)  # insert or update


def get_product(product_id: str) -> dict | None:  # one product by ID, or None
    with connect() as conn:
        row = conn.execute("SELECT * FROM products WHERE product_id = ?", (product_id,)).fetchone()  # first match or None
    return row_to_dict(row) if row else None  # dict with JSON columns decoded


def get_products(ids: list[str]) -> list[dict]:  # many products by ID, in the given order
    if not ids:  # nothing asked
        return []
    with connect() as conn:
        q = f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(ids))})"  # builds "IN (?,?,?)" — one ? per id
        rows = {r["product_id"]: row_to_dict(r) for r in conn.execute(q, ids)}  # id -> product (SQL returns any order)
    return [rows[i] for i in ids if i in rows]  # keep the caller's order


def all_products() -> list[dict]:  # whole catalogue (used to build the search index)
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute("SELECT * FROM products")]


def get_shopper(customer_id: str) -> dict | None:  # one shopper profile, or None
    with connect() as conn:
        row = conn.execute("SELECT * FROM shoppers WHERE customer_id = ?", (customer_id,)).fetchone()
    return row_to_dict(row) if row else None
