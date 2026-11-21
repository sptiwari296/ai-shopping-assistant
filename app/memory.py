"""STEP 2a — Session memory (R5): remember filters and last results per session.

HOW: one row per session_id in the sessions table, state stored as JSON:
  filters       the active search (category, type, brands, budget, colour, size, use_case, query)
  last_results  product IDs shown last time, in order (so "first two" / "cheaper" work)
  clarify_streak how many clarifying questions in a row (max 2, R2)
  asked         which things we already asked about (never ask twice, R2)
WHY in SQLite: survives an API restart and is visible to judges; tiny data.
The session is bound to the customer_id that created it (a session can't be reused by another shopper).
"""
import json  # the state dict is stored as JSON text
from datetime import datetime  # updated_at timestamp

from app import db  # SQLite access

EMPTY = {"filters": {}, "last_results": [], "clarify_streak": 0, "asked": [], "language": "en"}  # state of a brand-new conversation
FILTER_KEYS = ["category", "product_type", "brands", "budget_min", "budget_max", "color", "size", "use_case", "search_query"]  # fields remembered across turns


def load(session_id: str, customer_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT customer_id, state FROM sessions WHERE session_id = ?", (session_id,)).fetchone()  # saved state, if any
    if not row or row["customer_id"] != customer_id:  # new session, OR the session belongs to a different shopper
        return json.loads(json.dumps(EMPTY))  # fresh copy; another shopper's session is never reused
    return json.loads(row["state"])  # JSON text -> dict


def save(session_id: str, customer_id: str, state: dict) -> None:
    with db.connect() as conn:
        conn.execute(  # insert, or overwrite the existing row for this session_id
            """INSERT INTO sessions VALUES (?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET
               customer_id=excluded.customer_id, state=excluded.state, updated_at=excluded.updated_at""",
            (session_id, customer_id, json.dumps(state, ensure_ascii=False), datetime.now().isoformat(timespec="seconds")))  # ensure_ascii=False keeps Hindi readable


def merge(state: dict, ext, last_prices: list[int]) -> dict:
    """Combine the new message's filters with the remembered ones.

    New product type/category -> new topic -> start filters fresh.
    Otherwise -> keep old filters, overwrite with whatever the new message says.
    "cheaper" -> budget_max = (cheapest price shown last time) - 1, computed in code.
    """
    old = state.get("filters", {})  # what we remembered from earlier turns
    # new topic = the shopper named a DIFFERENT product type or category than before (e.g. speakers -> laptops)
    new_topic = (ext.product_type and old.get("product_type") and ext.product_type.lower() != old["product_type"].lower()) or \
                (ext.category and old.get("category") and ext.category != old["category"])
    f = {} if new_topic else dict(old)  # new topic -> forget old filters; else start from a copy of them
    for k in FILTER_KEYS:  # overwrite with anything the new message actually says
        v = getattr(ext, k)  # e.g. ext.brands
        if v not in (None, [], ""):  # only non-empty values ("only JBL" sets brands, leaves budget alone)
            f[k] = v
    if new_topic or not old:  # fresh search -> use the new query as-is
        f["search_query"] = ext.search_query
    elif ext.search_query and ext.product_type is None and ext.category is None:
        # follow-up like "under 50k for study": keep the old query, add the new detail
        f["search_query"] = f"{old.get('search_query', '')} {ext.use_case or ''}".strip()
    if ext.cheaper and last_prices:  # "show cheaper ones" with results already shown
        f["budget_max"] = min(last_prices) - 1  # strictly cheaper than the cheapest shown — maths in code, not the LLM
    return {"filters": f, "new_topic": bool(new_topic)}  # brain resets clarify counters on a new topic
