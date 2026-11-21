"""Tools = plain code that does the EXACT work (R7). The LLM never does these.

  search_products  hybrid search -> SQL filters (price, brand, category, stock) -> size/colour check
  get_product      one product by ID
  check_stock      is a product (optionally a colour/size variant) available?
  compare          side-by-side table of real catalogue fields
  my_orders        THIS shopper's orders only (customer_id comes from the server, never the LLM)

WHY code: prices, stock and comparisons are facts. Code is exact, fast and testable;
an LLM can misread "under 3000" or invent a stock level.
"""
import re  # used by _norm to strip punctuation

from app import db  # SQLite access
from app.search import HybridIndex  # the retrieval index (type hint)


def _norm(s: str | None) -> str:
    """'Levi's' == 'levis' == 'LEVIS'"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())  # None -> "", lowercase, keep only letters/digits


def card(p: dict, variant: dict | None = None) -> dict:
    """The small product view that goes to the LLM, the UI and the trace."""
    attrs = {k: v for k, v in (p.get("attributes") or {}).items() if k != "variants"}  # specs without the raw variants list
    out = {  # only safe, useful fields — note: NO reviews (untrusted text, R8)
        "product_id": p["product_id"], "name": p["name"], "brand": p.get("brand"), "category": p["category"],
        "product_type": p.get("product_type"), "price": p["price"], "in_stock": p["stock"] > 0, "stock": p["stock"],  # in_stock computed by code
        "rating": p.get("rating"), "description": p.get("description"), "attributes": attrs,
    }
    if (p.get("attributes") or {}).get("variants"):  # sized product (shoes/clothes)
        out["variants_in_stock"] = sorted({f"{v['color']} {v['size']}" for v in p["attributes"]["variants"] if v["stock"] > 0})  # e.g. ["blue 9", "black 9"]
    if variant:  # the exact colour+size that matched the shopper's request
        out["matched_variant"] = variant
    return out


def _variant_match(p: dict, color: str | None, size: str | None) -> dict | None | bool:
    """True = no size/colour asked. dict = matching in-stock variant. None = no match."""
    if not color and not size:  # shopper didn't ask for colour/size -> any product is fine
        return True
    variants = (p.get("attributes") or {}).get("variants")  # list of {color, size, stock} or None
    if not variants:
        # Non-sized product: only colour can be checked
        if size:  # asked for a size, but this product has no sizes -> reject
            return None
        return True if _norm((p.get("attributes") or {}).get("color")) == _norm(color) else None  # single colour must match
    for v in variants:  # find a variant matching colour AND size AND in stock
        if (not color or _norm(v["color"]) == _norm(color)) and (not size or _norm(v["size"]) == _norm(str(size))) and v["stock"] > 0:
            return v  # first match wins
    return None  # nothing in stock in that colour/size


def search_products(index: HybridIndex, query: str, category: str | None = None, product_type: str | None = None,
                    brands: list[str] | None = None, budget_min: int | None = None, budget_max: int | None = None,
                    color: str | None = None, size: str | None = None, in_stock_only: bool = True,
                    exclude_ids: list[str] | None = None, k: int = 5,
                    max_per_type: int | None = None) -> list[dict]:
    """Ranked by FIT to the query (hybrid search order), never by popularity (R3).
    max_per_type: for open questions ("gift for a trekker") show variety, e.g. at most 2 poles."""
    candidates = [pid for pid, _ in index.search(query or category or "", k=80)]  # RETRIEVE 80 deep: filters may remove most of them
    if not candidates:  # nothing even remotely matches
        return []

    # Exact filters in SQL: price, stock, category. Brand compared after normalising.
    sql = f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(candidates))})"  # only the candidates; one ? per id
    args: list = list(candidates)  # values for the ? placeholders (never pasted into SQL -> no injection)
    if budget_max is not None:  # "under 3000" -> price <= 3000 (inclusive, tested at the boundary)
        sql += " AND price <= ?"
        args.append(int(budget_max))
    if budget_min is not None:  # "above 1000"
        sql += " AND price >= ?"
        args.append(int(budget_min))
    if in_stock_only:  # never recommend out-of-stock items
        sql += " AND stock > 0"
    if category:  # e.g. "outdoor" (decided by the catalogue, not the LLM)
        sql += " AND category = ?"
        args.append(category)
    with db.connect() as conn:
        rows = {r["product_id"]: db.row_to_dict(r) for r in conn.execute(sql, args)}  # survivors, as id -> product

    want_brands = {_norm(b) for b in (brands or []) if b}  # e.g. {"jbl"}; empty set = any brand
    want_type = _norm(product_type)  # e.g. "bluetoothspeaker"; "" = any type
    skip = set(exclude_ids or [])  # products to leave out
    results, per_type = [], {}  # final list; count per product type (for variety)
    for pid in candidates:  # keep search (fit) order
        p = rows.get(pid)  # None if SQL filtered it out
        if not p or pid in skip:
            continue
        if max_per_type and per_type.get(p.get("product_type"), 0) >= max_per_type:  # already have enough of this type
            continue
        if want_brands and _norm(p.get("brand")) not in want_brands:  # wrong brand ("Levi's" == "levis")
            continue
        if want_type and want_type not in _norm(p.get("product_type")) and _norm(p.get("product_type")) not in want_type:  # type must match (either contains the other)
            continue
        v = _variant_match(p, color, size)  # colour/size check
        if v is None:  # requested variant not available
            continue
        results.append(card(p, v if isinstance(v, dict) else None))  # keep it (with the matched variant if any)
        per_type[p.get("product_type")] = per_type.get(p.get("product_type"), 0) + 1  # count for variety
        if len(results) >= k:  # enough results
            break
    return results  # best fit first


def get_product(product_id: str) -> dict | None:  # one product as a card, or None
    p = db.get_product(product_id)
    return card(p) if p else None


def check_stock(product_id: str, color: str | None = None, size: str | None = None) -> dict:  # "is the blue one in size 9?"
    p = db.get_product(product_id)
    if not p:  # unknown ID
        return {"product_id": product_id, "found": False}
    variants = (p.get("attributes") or {}).get("variants")  # per colour/size stock, if sized
    result = {"product_id": product_id, "found": True, "name": p["name"], "price": p["price"],
              "total_stock": p["stock"], "color": color, "size": size}
    if (color or size) and variants:  # specific variant asked on a sized product
        matches = [v for v in variants if (not color or _norm(v["color"]) == _norm(color))
                   and (not size or _norm(v["size"]) == _norm(str(size)))]  # variants matching the request
        units = sum(v["stock"] for v in matches)  # units left across them
        result.update(variant_exists=bool(matches), units=units, available=units > 0)  # e.g. blue 10 exists but 0 left
    elif size and not variants:  # asked a size on a product without sizes
        result.update(variant_exists=False, units=0, available=False, note="this product has no sizes")
    else:  # no variant asked -> total stock
        result.update(units=p["stock"], available=p["stock"] > 0)
    return result


COMPARE_FIELDS = ["price", "brand", "rating", "in_stock", "stock"]  # rows every comparison table starts with


def compare(product_ids: list[str]) -> dict:
    """Side-by-side table built only from catalogue fields (no LLM opinions)."""
    products = [card(p) for p in db.get_products(product_ids)]  # real products, in the order asked
    if len(products) < 2:  # can't compare fewer than 2
        return {"products": products, "rows": [], "error": "need at least 2 known products"}
    attr_keys = sorted({k for p in products for k in p["attributes"]})  # union of all spec names
    rows = [{"field": f, "values": [p[f] for p in products]} for f in COMPARE_FIELDS]  # standard rows
    rows += [{"field": k, "values": [p["attributes"].get(k, "—") for p in products]} for k in attr_keys]  # spec rows; "—" if a product lacks it
    cheapest = min(products, key=lambda p: p["price"])  # computed by code, not the LLM
    rated = [p for p in products if p["rating"] is not None]  # some ratings are missing (messy data)
    best_rated = max(rated, key=lambda p: p["rating"]) if rated else None
    return {"products": [{"product_id": p["product_id"], "name": p["name"]} for p in products], "rows": rows,
            "facts": {"cheapest": cheapest["product_id"], "best_rated": best_rated["product_id"] if best_rated else None,
                      "price_gap": max(p["price"] for p in products) - cheapest["price"]}}  # verdict facts the LLM may only word


def my_orders(customer_id: str) -> list[dict]:
    """customer_id must come from the verified request (server), never from message text (R9)."""
    with db.connect() as conn:
        rows = conn.execute(  # join orders with product names; only THIS customer's rows
            """SELECT o.order_id, o.order_date, o.price_paid, p.product_id, p.name, p.category, p.stock
               FROM orders o JOIN products p ON p.product_id = o.product_id
               WHERE o.customer_id = ? ORDER BY o.order_date DESC""", (customer_id,)).fetchall()  # newest first
    return [dict(r) for r in rows]  # list of plain dicts
