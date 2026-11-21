"""Tools = plain code doing the exact work (R7): filter, stock, compare, own orders. The LLM never does these."""
import re

from app import db


def norm(s) -> str:  # "Levi's" == "levis"
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def card(p: dict, variant: dict | None = None) -> dict:  # the product view for the LLM, UI and trace (no reviews)
    c = {k: p.get(k) for k in ("product_id", "name", "brand", "category", "product_type", "price", "stock", "rating", "description")}
    c["in_stock"] = p["stock"] > 0
    c["attributes"] = {k: v for k, v in p["attributes"].items() if k != "variants"}
    if variant:
        c["matched_variant"] = variant
    return c


def variant_ok(p: dict, color, size) -> tuple[bool, dict | None]:  # is this colour + size IN STOCK?
    if not color and not size:
        return True, None
    for v in p["attributes"].get("variants", []):
        if (not color or norm(v["color"]) == norm(color)) and (not size or norm(v["size"]) == norm(size)) and v["stock"] > 0:
            return True, v
    return (not size and norm(p["attributes"].get("color")) == norm(color)), None  # products without sizes


def search_products(index, query: str, category=None, product_type=None, brands=None, budget_max=None,
                    color=None, size=None, k: int = 5, max_per_type=None, in_stock_only: bool = True) -> list[dict]:
    candidates = [pid for pid, _ in index.search(query, k=80)]  # 1. RETRIEVE 80 candidates, in fit order
    if not candidates:
        return []
    sql = f"SELECT * FROM products WHERE product_id IN ({','.join('?' * len(candidates))})"  # 2. exact filters in SQL
    args = list(candidates)
    if budget_max:
        sql += " AND price <= ?"
        args.append(int(budget_max))
    if in_stock_only:
        sql += " AND stock > 0"
    if category:
        sql += " AND category = ?"
        args.append(category)
    with db.connect() as conn:
        rows = {r["product_id"]: db.to_product(r) for r in conn.execute(sql, args)}
    want_brands, want_type, per_type, out = {norm(b) for b in brands or []}, norm(product_type), {}, []
    for pid in candidates:  # 3. walk in FIT order (never sorted by rating, R3)
        p = rows.get(pid)
        if not p or (want_brands and norm(p["brand"]) not in want_brands) or (want_type and norm(p["product_type"]) != want_type):
            continue
        if max_per_type and per_type.get(p["product_type"], 0) >= max_per_type:  # variety for open questions
            continue
        ok, v = variant_ok(p, color, size)
        if ok:
            out.append(card(p, v))
            per_type[p["product_type"]] = per_type.get(p["product_type"], 0) + 1
        if len(out) == k:
            break
    return out


def check_stock(pid: str, color=None, size=None) -> dict:  # "is the blue one in size 9?"
    p = db.get_product(pid)
    if not p:
        return {"product_id": pid, "found": False}
    if not color and not size:
        return {"product_id": pid, "found": True, "variant_exists": True, "units": p["stock"], "available": p["stock"] > 0}
    match = [v for v in p["attributes"].get("variants", [])
             if (not color or norm(v["color"]) == norm(color)) and (not size or norm(v["size"]) == norm(size))]
    units = sum(v["stock"] for v in match)
    return {"product_id": pid, "found": True, "variant_exists": bool(match), "units": units, "available": units > 0}


def compare(ids: list[str]) -> dict | None:  # side-by-side table + verdict facts, all computed by code
    ps = [card(p) for p in db.get_products(ids)]
    if len(ps) < 2:
        return None
    rows = [{"field": f, "values": [p[f] for p in ps]} for f in ("price", "brand", "rating", "in_stock")]
    rows += [{"field": key, "values": [p["attributes"].get(key, "—") for p in ps]}
             for key in sorted({key for p in ps for key in p["attributes"]})]
    cheapest = min(ps, key=lambda p: p["price"])
    rated = [p for p in ps if p["rating"]]
    return {"products": ps, "rows": rows, "cheapest": cheapest["product_id"],
            "best_rated": max(rated, key=lambda p: p["rating"])["product_id"] if rated else None,
            "price_gap": max(p["price"] for p in ps) - cheapest["price"]}


def my_orders(customer_id: str) -> list[dict]:  # customer_id comes from the request, never the message (R9)
    return db.query("SELECT o.order_id, o.order_date, p.product_id, p.name FROM orders o JOIN products p USING (product_id) "
                    "WHERE o.customer_id = ? ORDER BY o.order_date DESC", (customer_id,))
