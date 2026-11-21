"""Tools = plain code that does the EXACT work (R7). The LLM never does these.

  search_products  hybrid search -> SQL filters (price, brand, category, stock) -> size/colour check
  get_product      one product by ID
  check_stock      is a product (optionally a colour/size variant) available?
  compare          side-by-side table of real catalogue fields
  my_orders        THIS shopper's orders only (customer_id comes from the server, never the LLM)

WHY code: prices, stock and comparisons are facts. Code is exact, fast and testable;
an LLM can misread "under 3000" or invent a stock level.
"""
import re

from app import db
from app.search import HybridIndex


def _norm(s: str | None) -> str:
    """'Levi's' == 'levis' == 'LEVIS'"""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def card(p: dict, variant: dict | None = None) -> dict:
    """The small product view that goes to the LLM, the UI and the trace."""
    attrs = {k: v for k, v in (p.get("attributes") or {}).items() if k != "variants"}
    out = {
        "product_id": p["product_id"], "name": p["name"], "brand": p.get("brand"), "category": p["category"],
        "product_type": p.get("product_type"), "price": p["price"], "in_stock": p["stock"] > 0, "stock": p["stock"],
        "rating": p.get("rating"), "description": p.get("description"), "attributes": attrs,
    }
    if (p.get("attributes") or {}).get("variants"):
        out["variants_in_stock"] = sorted({f"{v['color']} {v['size']}" for v in p["attributes"]["variants"] if v["stock"] > 0})
    if variant:
        out["matched_variant"] = variant
    return out


def _variant_match(p: dict, color: str | None, size: str | None) -> dict | None | bool:
    """True = no size/colour asked. dict = matching in-stock variant. None = no match."""
    if not color and not size:
        return True
    variants = (p.get("attributes") or {}).get("variants")
    if not variants:
        # Non-sized product: only colour can be checked
        if size:
            return None
        return True if _norm((p.get("attributes") or {}).get("color")) == _norm(color) else None
    for v in variants:
        if (not color or _norm(v["color"]) == _norm(color)) and (not size or _norm(v["size"]) == _norm(str(size))) and v["stock"] > 0:
            return v
    return None


def search_products(index: HybridIndex, query: str, category: str | None = None, product_type: str | None = None,
                    brands: list[str] | None = None, budget_min: int | None = None, budget_max: int | None = None,
                    color: str | None = None, size: str | None = None, in_stock_only: bool = True,
                    exclude_ids: list[str] | None = None, k: int = 5,
                    max_per_type: int | None = None) -> list[dict]:
    """Ranked by FIT to the query (hybrid search order), never by popularity (R3).
    max_per_type: for open questions ("gift for a trekker") show variety, e.g. at most 2 poles."""
    candidates = [pid for pid, _ in index.search(query or category or "", k=80)]
    if not candidates:
        return []

    # Exact filters in SQL: price, stock, category. Brand compared after normalising.
    sql = f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(candidates))})"
    args: list = list(candidates)
    if budget_max is not None:
        sql += " AND price <= ?"
        args.append(int(budget_max))
    if budget_min is not None:
        sql += " AND price >= ?"
        args.append(int(budget_min))
    if in_stock_only:
        sql += " AND stock > 0"
    if category:
        sql += " AND category = ?"
        args.append(category)
    with db.connect() as conn:
        rows = {r["product_id"]: db.row_to_dict(r) for r in conn.execute(sql, args)}

    want_brands = {_norm(b) for b in (brands or []) if b}
    want_type = _norm(product_type)
    skip = set(exclude_ids or [])
    results, per_type = [], {}
    for pid in candidates:  # keep search (fit) order
        p = rows.get(pid)
        if not p or pid in skip:
            continue
        if max_per_type and per_type.get(p.get("product_type"), 0) >= max_per_type:
            continue
        if want_brands and _norm(p.get("brand")) not in want_brands:
            continue
        if want_type and want_type not in _norm(p.get("product_type")) and _norm(p.get("product_type")) not in want_type:
            continue
        v = _variant_match(p, color, size)
        if v is None:
            continue
        results.append(card(p, v if isinstance(v, dict) else None))
        per_type[p.get("product_type")] = per_type.get(p.get("product_type"), 0) + 1
        if len(results) >= k:
            break
    return results


def get_product(product_id: str) -> dict | None:
    p = db.get_product(product_id)
    return card(p) if p else None


def check_stock(product_id: str, color: str | None = None, size: str | None = None) -> dict:
    p = db.get_product(product_id)
    if not p:
        return {"product_id": product_id, "found": False}
    variants = (p.get("attributes") or {}).get("variants")
    result = {"product_id": product_id, "found": True, "name": p["name"], "price": p["price"],
              "total_stock": p["stock"], "color": color, "size": size}
    if (color or size) and variants:
        matches = [v for v in variants if (not color or _norm(v["color"]) == _norm(color))
                   and (not size or _norm(v["size"]) == _norm(str(size)))]
        units = sum(v["stock"] for v in matches)
        result.update(variant_exists=bool(matches), units=units, available=units > 0)
    elif size and not variants:
        result.update(variant_exists=False, units=0, available=False, note="this product has no sizes")
    else:
        result.update(units=p["stock"], available=p["stock"] > 0)
    return result


COMPARE_FIELDS = ["price", "brand", "rating", "in_stock", "stock"]


def compare(product_ids: list[str]) -> dict:
    """Side-by-side table built only from catalogue fields (no LLM opinions)."""
    products = [card(p) for p in db.get_products(product_ids)]
    if len(products) < 2:
        return {"products": products, "rows": [], "error": "need at least 2 known products"}
    attr_keys = sorted({k for p in products for k in p["attributes"]})
    rows = [{"field": f, "values": [p[f] for p in products]} for f in COMPARE_FIELDS]
    rows += [{"field": k, "values": [p["attributes"].get(k, "—") for p in products]} for k in attr_keys]
    cheapest = min(products, key=lambda p: p["price"])
    rated = [p for p in products if p["rating"] is not None]
    best_rated = max(rated, key=lambda p: p["rating"]) if rated else None
    return {"products": [{"product_id": p["product_id"], "name": p["name"]} for p in products], "rows": rows,
            "facts": {"cheapest": cheapest["product_id"], "best_rated": best_rated["product_id"] if best_rated else None,
                      "price_gap": max(p["price"] for p in products) - cheapest["price"]}}


def my_orders(customer_id: str) -> list[dict]:
    """customer_id must come from the verified request (server), never from message text (R9)."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT o.order_id, o.order_date, o.price_paid, p.product_id, p.name, p.category, p.stock
               FROM orders o JOIN products p ON p.product_id = o.product_id
               WHERE o.customer_id = ? ORDER BY o.order_date DESC""", (customer_id,)).fetchall()
    return [dict(r) for r in rows]
