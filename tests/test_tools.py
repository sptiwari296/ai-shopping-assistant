"""Phase 3 tests: every tool, including the budget boundary and privacy."""
import pytest

from app import db, tools
from app.search import HybridIndex


@pytest.fixture(scope="module")
def index():
    db.init_db(reset=True)
    idx = HybridIndex()
    idx.build(db.all_products())
    return idx


def test_budget_boundary_inclusive(index):
    # "under 2999" includes a product priced exactly 2999, excludes 3000+
    res = tools.search_products(index, "bluetooth speaker", product_type="Bluetooth Speaker", budget_max=2999, k=20)
    assert res and all(p["price"] <= 2999 for p in res)
    assert "P-1046" in [p["product_id"] for p in res]  # JBL Go 20W at exactly 2999
    res2 = tools.search_products(index, "bluetooth speaker", product_type="Bluetooth Speaker", budget_max=2998, k=20)
    assert "P-1046" not in [p["product_id"] for p in res2]


def test_brand_filter_normalised(index):
    res = tools.search_products(index, "speaker", brands=["jbl"], k=10)
    assert res and all(p["brand"] == "JBL" for p in res)


def test_out_of_stock_excluded(index):
    res = tools.search_products(index, "laptop", product_type="Laptop", k=50)
    assert res and all(p["in_stock"] for p in res)


def test_size_colour_filter(index):
    res = tools.search_products(index, "running shoes flat feet", product_type="Running Shoes", color="blue", size="9", k=10)
    assert res and all(p["matched_variant"]["color"] == "blue" and p["matched_variant"]["size"] == "9" for p in res)


def test_check_stock_variant():
    assert tools.check_stock("P-1001", color="blue", size="9")["available"] is True    # 4 left
    assert tools.check_stock("P-1001", color="blue", size="10")["available"] is False  # 0 left
    assert tools.check_stock("P-1001", color="red", size="9")["variant_exists"] is False
    assert tools.check_stock("P-0000")["found"] is False


def test_compare_uses_real_fields():
    out = tools.compare(["P-1046", "P-1047"])  # JBL vs boAt look-alikes
    assert out["facts"]["cheapest"] == "P-1047"
    assert out["facts"]["price_gap"] == 2999 - 1799
    assert {r["field"] for r in out["rows"]} >= {"price", "brand", "output_watts"}


def test_my_orders_only_own():
    mine = tools.my_orders("C004")
    assert mine
    with db.connect() as conn:
        own = {r[0] for r in conn.execute("SELECT order_id FROM orders WHERE customer_id='C004'")}
    assert {o["order_id"] for o in mine} == own


def test_new_shopper_has_no_orders():
    assert tools.my_orders("C001") == []


def test_variety_for_open_questions(index):
    res = tools.search_products(index, "gift for father who likes trekking", category="outdoor",
                                budget_max=3000, k=4, max_per_type=2)
    types = [p["product_type"] for p in res]
    assert len(res) == 4 and max(types.count(t) for t in types) <= 2
