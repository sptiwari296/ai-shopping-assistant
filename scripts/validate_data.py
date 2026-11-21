"""Check the data against the schema (brief 4.2: 'a small script that checks the data').

ERRORS   = broken data that must be fixed (exit code 1).
WARNINGS = messy data we added ON PURPOSE (missing brand/rating, out of stock); counted, not failed.

Run:  python scripts/validate_data.py
"""
import json
import re
import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"
REQUIRED = ["product_id", "name", "category", "brand", "price", "stock", "attributes", "description", "rating"]


def main() -> int:
    products = json.loads((DATA / "catalogue.json").read_text(encoding="utf-8"))
    shoppers = json.loads((DATA / "shoppers.json").read_text(encoding="utf-8"))
    orders = json.loads((DATA / "orders.json").read_text(encoding="utf-8"))
    errors, warn = [], {}

    def w(key):
        warn[key] = warn.get(key, 0) + 1

    ids = set()
    for p in products:
        pid = p.get("product_id", "?")
        for f in REQUIRED:
            if f not in p:
                errors.append(f"{pid}: missing key '{f}'")
        if not re.fullmatch(r"P-\d{4}", pid):
            errors.append(f"{pid}: bad id format")
        if pid in ids:
            errors.append(f"{pid}: duplicate id")
        ids.add(pid)
        if not isinstance(p.get("price"), int) or p["price"] <= 0:
            errors.append(f"{pid}: price must be a positive integer")
        if not isinstance(p.get("stock"), int) or p["stock"] < 0:
            errors.append(f"{pid}: stock must be >= 0")
        variants = p.get("attributes", {}).get("variants")
        if variants and sum(v["stock"] for v in variants) != p["stock"]:
            errors.append(f"{pid}: stock != sum of variant stock")
        if p.get("rating") is not None and not (0 <= p["rating"] <= 5):
            errors.append(f"{pid}: rating out of range")
        # intentional mess -> warnings
        if p.get("brand") is None:
            w("missing brand (intended)")
        if p.get("rating") is None:
            w("missing rating (intended)")
        if p["stock"] == 0:
            w("out of stock (intended)")
        elif p["stock"] <= 2:
            w("nearly out of stock (intended)")
        if re.search(r"[ऀ-ॿ]", p.get("description") or ""):
            w("Hindi description (intended)")
        if any("instruction" in r.lower() or "system:" in r.lower() for r in p.get("reviews", [])):
            w("injection review (intended)")

    cats = {p["category"] for p in products}
    if len(products) < 250:
        errors.append(f"only {len(products)} products (brief wants >= 250)")
    if len(cats) < 5:
        errors.append(f"only {len(cats)} categories (brief wants >= 5)")

    cids = set()
    for s in shoppers:
        cid = s.get("customer_id")
        if not re.fullmatch(r"C\d{3}", cid or ""):
            errors.append(f"{cid}: bad customer id")
        cids.add(cid)
        if s["budget_max"] <= s["budget_min"]:
            errors.append(f"{cid}: budget_max <= budget_min")
    if len(shoppers) < 25:
        errors.append(f"only {len(shoppers)} shoppers (brief wants >= 25)")

    per_shopper = {}
    for o in orders:
        if o["product_id"] not in ids:
            errors.append(f"{o['order_id']}: unknown product {o['product_id']}")
        if o["customer_id"] not in cids:
            errors.append(f"{o['order_id']}: unknown shopper {o['customer_id']}")
        per_shopper[o["customer_id"]] = per_shopper.get(o["customer_id"], 0) + 1
    with3 = sum(1 for c in cids if per_shopper.get(c, 0) >= 3)
    if with3 < len(cids) / 2:
        errors.append("fewer than half the shoppers have 3+ orders")
    edge = {s["edge_case"] for s in shoppers if s.get("edge_case")}
    for need in ("new_no_history", "contradictory_likes", "favourites_out_of_stock"):
        if need not in edge:
            errors.append(f"missing edge case: {need}")

    print(f"products={len(products)} categories={len(cats)} shoppers={len(shoppers)} orders={len(orders)} "
          f"shoppers_with_3plus_orders={with3}")
    print("intentional mess:", warn)
    if errors:
        print(f"{len(errors)} ERRORS:")
        for e in errors[:30]:
            print("  -", e)
        return 1
    print("0 violations")
    return 0


if __name__ == "__main__":
    sys.exit(main())
