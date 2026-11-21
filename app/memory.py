"""STEP 2a — Session memory (R5): remembers filters and the last results, so follow-ups work."""
import json

from app import db

EMPTY = {"filters": {}, "last_results": [], "clarify_streak": 0, "asked": []}
KEYS = ("query", "category", "product_type", "brands", "budget_max", "color", "size", "use_case")


def load(session_id: str, customer_id: str) -> dict:
    rows = db.query("SELECT customer_id, state FROM sessions WHERE session_id = ?", (session_id,))
    if rows and rows[0]["customer_id"] == customer_id:  # a session is never shared between shoppers
        return json.loads(rows[0]["state"])
    return json.loads(json.dumps(EMPTY))  # fresh copy


def save(session_id: str, customer_id: str, state: dict) -> None:
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (session_id, customer_id, json.dumps(state, ensure_ascii=False)))


def merge(state: dict, ext, last_prices: list[int]) -> dict:
    """New product type -> new topic (start fresh). Otherwise keep the old filters and add what's new."""
    old = state["filters"]
    new_topic = bool(ext.product_type and old.get("product_type") and ext.product_type != old["product_type"])
    if new_topic:
        state["clarify_streak"], state["asked"] = 0, []
    f = {} if new_topic else dict(old)
    for k in KEYS:  # "only JBL" sets brands and leaves everything else alone
        v = getattr(ext, k)
        if v not in (None, [], ""):
            f[k] = v
    if old.get("query") and not new_topic and not ext.product_type:
        f["query"] = old["query"]  # "under 50k for study" keeps the old search text
    if ext.cheaper and last_prices:
        f["budget_max"] = min(last_prices) - 1  # "cheaper" = below the cheapest shown — maths in code
    state["filters"] = f
    return f
