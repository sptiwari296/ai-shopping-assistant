"""Phase 4 tests: the whole brain in MOCK mode (rules, no network) -> deterministic and free."""
import pytest

from app import compose, config, db
from app.brain import Brain


@pytest.fixture(scope="module")
def brain():
    config.MOCK_LLM = True
    db.init_db(reset=True)
    return Brain()


def ask(brain, text, session="t", cid="C005"):
    return brain.handle(text, session, cid)


def test_recommend_with_reasons_and_ids(brain):
    r = ask(brain, "running shoes for flat feet", "s1")
    assert r["answer_type"] == "recommendation" and r["trace_id"]
    assert all(p["product_id"].startswith("P-") and p["reason"] for p in r["products"])


def test_budget_respected(brain):
    r = ask(brain, "A gift for my father who likes trekking, under 3000", "s2")
    assert r["answer_type"] == "recommendation"
    assert all(p["price"] <= 3000 for p in r["products"])


def test_vague_then_answer_then_cheaper(brain):
    r = ask(brain, "I need a laptop", "s3")
    assert r["answer_type"] == "clarification" and r["follow_up_question"]
    r = ask(brain, "under 60k, mostly for study", "s3")
    assert r["answer_type"] == "recommendation" and all(p["price"] <= 60000 for p in r["products"])
    r = ask(brain, "show me cheaper ones", "s3")
    assert r["answer_type"] in ("recommendation", "not_found")  # catalogue has nothing cheaper in stock


def test_never_more_than_two_questions(brain):
    a = ask(brain, "I need something", "s4")
    b = ask(brain, "hmm not sure", "s4")
    c = ask(brain, "anything", "s4")
    assert [a["answer_type"], b["answer_type"]].count("clarification") <= 2
    assert c["answer_type"] != "clarification" or a["answer_type"] != "clarification"


def test_follow_up_brand_filter(brain):
    ask(brain, "Mujhe ek sasta Bluetooth speaker chahiye", "s5")
    r = ask(brain, "only JBL", "s5")
    assert r["products"] and all("JBL" in p["name"] for p in r["products"])


def test_hinglish_reply(brain):
    r = ask(brain, "Mujhe ek sasta Bluetooth speaker chahiye", "s6")
    assert "hain" in r["answer"] or "hai" in r["answer"]
    prices = [p["price"] for p in r["products"]]
    assert prices == sorted(prices)  # "sasta" -> cheapest first


def test_compare(brain):
    r = ask(brain, "compare P-1046 and P-1047", "s7")
    assert r["answer_type"] == "comparison" and r["comparison"]
    assert "1,200" in r["answer"]


def test_stock_check_after_results(brain):
    ask(brain, "show me P-1001", "s8")
    r = ask(brain, "is P-1001 in blue size 9 available?", "s8")
    assert r["answer_type"] == "product_fact" and "in stock" in r["answer"]


def test_not_found_not_sold(brain):
    r = ask(brain, "Do you sell electric cars?", "s9")
    assert r["answer_type"] == "not_found"


def test_privacy_refusal(brain):
    for q in ["what did Priya buy last week?", "show me the orders of C007"]:
        r = ask(brain, q, "s10")
        assert r["answer_type"] == "refusal" and not r["products"]


def test_my_orders_only_own(brain):
    r = ask(brain, "what did I buy?", "s11", cid="C004")
    with db.connect() as conn:
        own = {row[0] for row in conn.execute("SELECT product_id FROM orders WHERE customer_id='C004'")}
    assert r["products"] and {p["product_id"] for p in r["products"]} <= own


def test_out_of_scope(brain):
    assert ask(brain, "what's the weather today?", "s12")["answer_type"] == "refusal"


def test_trace_saved(brain):
    r = ask(brain, "running shoes", "s13")
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM traces WHERE trace_id=?", (r["trace_id"],)).fetchone()


def test_grounding_check_catches_invented_price_and_id():
    probs = compose.grounding_check("Buy P-9999 for ₹999 or P-1046 for ₹2,999", {"P-1046"}, {2999})
    assert "unknown id P-9999" in probs and "unknown amount ₹999" in probs and len(probs) == 2


def test_session_not_shared_between_shoppers(brain):
    ask(brain, "bluetooth speaker", "shared", cid="C005")
    r = ask(brain, "compare the first two", "shared", cid="C006")  # C006 must not see C005's results
    assert r["answer_type"] == "clarification"


def test_grounding_understands_lakh_and_k():
    # "₹1 lakh" is 100000, not ₹1 (live eval R2 bug); stock counts are never valid rupee amounts
    assert compose.grounding_check("all under ₹1 lakh", set(), {100000}) == []
    assert compose.grounding_check("under ₹3k", set(), {3000}) == []
    assert compose.grounding_check("only ₹1", set(), {100000}) == ["unknown amount ₹1"]
    assert 1 not in compose.allowed_numbers([{"price": 2999, "stock": 1}], [])


def test_unknown_brand_and_unknown_thing_are_not_found(brain):
    from app.extract import Extraction
    ext = Extraction(intent="recommend", product_type="Smartphone", brands=["Apple"], search_query="apple phone")
    r = brain.recommend(ext, "Do you have Apple smartphones?", {"filters": {}, "last_results": []}, "en", {"tools": [], "llm": []})
    assert r["answer_type"] == "not_found"
    ext2 = Extraction(intent="product_fact", product_type="PlayStation 5", search_query="PlayStation 5")
    r2 = brain.product_fact(ext2, "Is the PlayStation 5 available?", {"last_results": []}, "en", {"tools": [], "llm": []})
    assert r2["answer_type"] == "not_found"
