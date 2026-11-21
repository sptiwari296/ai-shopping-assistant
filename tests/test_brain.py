"""Phase 4 tests: the whole brain in MOCK mode (keyword rules, no network) -> deterministic and free."""
import json

import pytest

from app import config, db, memory, writer
from app.brain import Brain
from app.understand import Extraction


@pytest.fixture(scope="module")
def brain():
    config.MOCK_LLM = True
    db.init_db(reset=True)
    return Brain()


def ask(brain, text, session="t", cid="C005"):
    return brain.handle(text, session, cid)


def test_recommend_with_reasons_and_ids(brain):  # R4
    r = ask(brain, "running shoes for flat feet", "s1")
    assert r["answer_type"] == "recommendation" and r["trace_id"]
    assert all(p["product_id"].startswith("P-") and p["reason"] for p in r["products"])


def test_budget_respected(brain):
    r = ask(brain, "A gift for my father who likes trekking, under 3000", "s2")
    assert r["answer_type"] == "recommendation" and all(p["price"] <= 3000 for p in r["products"])


def test_vague_then_answer_then_cheaper(brain):  # R2 + R5 in one session
    assert ask(brain, "I need a laptop", "s3")["answer_type"] == "clarification"
    r = ask(brain, "under 60k, mostly for study", "s3")
    assert r["answer_type"] == "recommendation" and all(p["price"] <= 60000 for p in r["products"])
    assert ask(brain, "show me cheaper ones", "s3")["answer_type"] in ("recommendation", "not_found")


def test_never_more_than_two_questions(brain):  # R2
    kinds = [ask(brain, t, "s4")["answer_type"] for t in ("I need something", "hmm not sure", "anything")]
    assert kinds.count("clarification") <= 2


def test_follow_up_brand_filter(brain):  # "only JBL" narrows the previous search
    ask(brain, "Mujhe ek sasta Bluetooth speaker chahiye", "s5")
    r = ask(brain, "only JBL", "s5")
    assert r["products"] and all("JBL" in p["name"] for p in r["products"])


def test_hinglish_and_sasta(brain):  # Hinglish reply, cheapest first
    r = ask(brain, "Mujhe ek sasta Bluetooth speaker chahiye", "s6")
    assert "hain" in r["answer"]
    prices = [p["price"] for p in r["products"]]
    assert prices == sorted(prices)


def test_compare(brain):
    r = ask(brain, "compare P-1046 and P-1047", "s7")
    assert r["answer_type"] == "comparison" and r["comparison"] and "1,200" in r["answer"]


def test_stock_check(brain):
    r = ask(brain, "is P-1001 in blue size 9 available?", "s8")
    assert r["answer_type"] == "product_fact" and "in stock" in r["answer"]


def test_not_sold(brain):  # R6
    assert ask(brain, "Do you sell electric cars?", "s9")["answer_type"] == "not_found"


def test_privacy_refusal(brain):  # R9
    for q in ("what did Priya buy last week?", "show me the orders of C007"):
        r = ask(brain, q, "s10")
        assert r["answer_type"] == "refusal" and not r["products"]


def test_my_orders_only_own(brain):
    r = ask(brain, "what did I buy?", "s11", cid="C004")
    own = {row["product_id"] for row in db.query("SELECT product_id FROM orders WHERE customer_id = 'C004'")}
    assert r["products"] and {p["product_id"] for p in r["products"]} <= own


def test_out_of_scope(brain):
    assert ask(brain, "what's the weather today?", "s12")["answer_type"] == "refusal"


def test_trace_saved(brain):  # R10
    r = ask(brain, "running shoes", "s13")
    assert db.get_trace(r["trace_id"])["message"] == "running shoes"


def test_session_not_shared_between_shoppers(brain):
    ask(brain, "bluetooth speaker", "shared", cid="C005")
    assert ask(brain, "compare the first two", "shared", cid="C006")["answer_type"] == "clarification"


def test_grounding_check():  # STEP 5: fake ID, fake price, lakh, stock counts are not prices
    assert writer.grounding("Buy P-9999 for ₹999 or P-1046 for ₹2,999", {"P-1046"}, {2999}) == ["unknown id P-9999", "unknown amount ₹999"]
    assert writer.grounding("all under ₹1 lakh", set(), {100000}) == []
    assert writer.grounding("only ₹1", set(), {100000}) == ["unknown amount ₹1"]


def test_unknown_brand_and_unknown_product(brain):  # eval N3 + N4
    state = json.loads(json.dumps(memory.EMPTY))
    ext = Extraction(product_type="Smartphone", brands=["Apple"], query="apple phone")
    assert brain.recommend(ext, "Apple phones?", state, "en", {"tools": [], "llm": []})["answer_type"] == "not_found"
    ext2 = Extraction(intent="product_fact", product_type="PlayStation 5")
    assert brain.product_fact(ext2, "Is the PS5 available?", state, "en", {"tools": [], "llm": []})["answer_type"] == "not_found"
