"""STEP 2a — Session memory (R5): remembers filters and the last results, so follow-ups work."""
import json  # the state is stored as JSON text

from app import db  # SQLite access

EMPTY = {"filters": {}, "last_results": [], "clarify_streak": 0, "asked": []}  # memory of a brand-new conversation
KEYS = ("query", "category", "product_type", "brands", "budget_max", "color", "size", "use_case")  # filters that carry over


def load(session_id: str, customer_id: str) -> dict:  # get this conversation's memory
    rows = db.query("SELECT customer_id, state FROM sessions WHERE session_id = ?", (session_id,))  # saved state, if any
    if rows and rows[0]["customer_id"] == customer_id:  # a session is never shared between shoppers
        return json.loads(rows[0]["state"])  # JSON text -> dict
    return json.loads(json.dumps(EMPTY))  # fresh copy


def save(session_id: str, customer_id: str, state: dict) -> None:  # store memory for the next message
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?)", (session_id, customer_id, json.dumps(state, ensure_ascii=False)))  # create or overwrite


def merge(state: dict, ext, last_prices: list[int]) -> dict:
    """New product type -> new topic (start fresh). Otherwise keep the old filters and add what's new."""
    old = state["filters"]  # filters remembered from earlier messages
    new_topic = bool(ext.product_type and old.get("product_type") and ext.product_type != old["product_type"])  # e.g. speakers -> laptops
    if new_topic:
        state["clarify_streak"], state["asked"] = 0, []  # new topic: question counters start again
    f = {} if new_topic else dict(old)  # fresh filters, or a copy of the old ones
    for k in KEYS:  # "only JBL" sets brands and leaves everything else alone
        v = getattr(ext, k)  # the new message's value for this filter
        if v not in (None, [], ""):  # only values the message actually gave
            f[k] = v  # overwrite / add
    if old.get("query") and not new_topic and not ext.product_type:
        f["query"] = old["query"]  # "under 50k for study" keeps the old search text
    if ext.cheaper and last_prices:
        f["budget_max"] = min(last_prices) - 1  # "cheaper" = below the cheapest shown — maths in code
    state["filters"] = f  # remember the merged filters
    return f  # the active search for this turn
