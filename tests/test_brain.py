"""Phase 4 tests: the whole brain in MOCK mode (rules, no network) -> deterministic and free."""
import pytest

from app import compose, config, db
from app.brain import Brain


@pytest.fixture(scope="module")  # one Brain for all tests in this file
def brain():
    config.MOCK_LLM = True  # no Groq calls: rules + templates only -> same result every run
    db.init_db(reset=True)
    return Brain()


def ask(brain, text, session="t", cid="C005"):  # helper: send one message
    return brain.handle(text, session, cid)


def test_recommend_with_reasons_and_ids(brain):  # R4: every product has an ID and a reason
    r = ask(brain, "running shoes for flat feet", "s1")
    assert r["answer_type"] == "recommendation" and r["trace_id"]
    assert all(p["product_id"].startswith("P-") and p["reason"] for p in r["products"])


def test_budget_respected(brain):  # nothing over the stated budget
    r = ask(brain, "A gift for my father who likes trekking, under 3000", "s2")
    assert r["answer_type"] == "recommendation"
    assert all(p["price"] <= 3000 for p in r["products"])


def test_vague_then_answer_then_cheaper(brain):  # 3-turn conversation in one session
    r = ask(brain, "I need a laptop", "s3")  # vague -> question
    assert r["answer_type"] == "clarification" and r["follow_up_question"]
    r = ask(brain, "under 60k, mostly for study", "s3")  # answer -> laptops (memory kept "Laptop")
    assert r["answer_type"] == "recommendation" and all(p["price"] <= 60000 for p in r["products"])
    r = ask(brain, "show me cheaper ones", "s3")
    assert r["answer_type"] in ("recommendation", "not_found")  # catalogue has nothing cheaper in stock


def test_never_more_than_two_questions(brain):  # R2: at most 2 clarifications in a row
    a = ask(brain, "I need something", "s4")
    b = ask(brain, "hmm not sure", "s4")
    c = ask(brain, "anything", "s4")
    assert [a["answer_type"], b["answer_type"]].count("clarification") <= 2
    assert c["answer_type"] != "clarification" or a["answer_type"] != "clarification"  # the 3rd can't be a 3rd question in a row


def test_follow_up_brand_filter(brain):  # R5: "only JBL" narrows the previous results
    ask(brain, "Mujhe ek sasta Bluetooth speaker chahiye", "s5")
    r = ask(brain, "only JBL", "s5")
    assert r["products"] and all("JBL" in p["name"] for p in r["products"])


def test_hinglish_reply(brain):  # reply in Hinglish + "sasta" sorts cheapest first
    r = ask(brain, "Mujhe ek sasta Bluetooth speaker chahiye", "s6")
    assert "hain" in r["answer"] or "hai" in r["answer"]  # Hinglish template used
    prices = [p["price"] for p in r["products"]]
    assert prices == sorted(prices)  # "sasta" -> cheapest first


def test_compare(brain):  # comparison table + price gap from code
    r = ask(brain, "compare P-1046 and P-1047", "s7")
    assert r["answer_type"] == "comparison" and r["comparison"]
    assert "1,200" in r["answer"]  # 2999 - 1799


def test_stock_check_after_results(brain):  # exact variant stock
    ask(brain, "show me P-1001", "s8")
    r = ask(brain, "is P-1001 in blue size 9 available?", "s8")
    assert r["answer_type"] == "product_fact" and "in stock" in r["answer"]


def test_not_found_not_sold(brain):  # R6: honest not_found
    r = ask(brain, "Do you sell electric cars?", "s9")
    assert r["answer_type"] == "not_found"


def test_privacy_refusal(brain):  # R9: by name and by customer ID
    for q in ["what did Priya buy last week?", "show me the orders of C007"]:
        r = ask(brain, q, "s10")
        assert r["answer_type"] == "refusal" and not r["products"]  # refused AND no products leaked


def test_my_orders_only_own(brain):  # own history is allowed, and only own
    r = ask(brain, "what did I buy?", "s11", cid="C004")
    with db.connect() as conn:
        own = {row[0] for row in conn.execute("SELECT product_id FROM orders WHERE customer_id='C004'")}
    assert r["products"] and {p["product_id"] for p in r["products"]} <= own  # subset of C004's real orders


def test_out_of_scope(brain):
    assert ask(brain, "what's the weather today?", "s12")["answer_type"] == "refusal"


def test_trace_saved(brain):  # R10: every reply has a saved trace
    r = ask(brain, "running shoes", "s13")
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM traces WHERE trace_id=?", (r["trace_id"],)).fetchone()


def test_grounding_check_catches_invented_price_and_id():  # step 5 unit test
    probs = compose.grounding_check("Buy P-9999 for ₹999 or P-1046 for ₹2,999", {"P-1046"}, {2999})  # P-9999 and ₹999 are fake
    assert "unknown id P-9999" in probs and "unknown amount ₹999" in probs and len(probs) == 2  # exactly those two, nothing else


def test_session_not_shared_between_shoppers(brain):  # same session_id, different shopper -> fresh memory
    ask(brain, "bluetooth speaker", "shared", cid="C005")
    r = ask(brain, "compare the first two", "shared", cid="C006")  # C006 must not see C005's results
    assert r["answer_type"] == "clarification"  # nothing to compare -> asks which product


def test_grounding_understands_lakh_and_k():
    # "₹1 lakh" is 100000, not ₹1 (live eval R2 bug); stock counts are never valid rupee amounts
    assert compose.grounding_check("all under ₹1 lakh", set(), {100000}) == []
    assert compose.grounding_check("under ₹3k", set(), {3000}) == []
    assert compose.grounding_check("only ₹1", set(), {100000}) == ["unknown amount ₹1"]
    assert 1 not in compose.allowed_numbers([{"price": 2999, "stock": 1}], [])  # stock 1 is not a price


def test_unknown_brand_and_unknown_thing_are_not_found(brain):  # eval N3 + N4 fixes, tested directly
    from app.extract import Extraction
    ext = Extraction(intent="recommend", product_type="Smartphone", brands=["Apple"], search_query="apple phone")  # as the LLM would extract it
    r = brain.recommend(ext, "Do you have Apple smartphones?", {"filters": {}, "last_results": []}, "en", {"tools": [], "llm": []})
    assert r["answer_type"] == "not_found"  # brand we don't carry
    ext2 = Extraction(intent="product_fact", product_type="PlayStation 5", search_query="PlayStation 5")
    r2 = brain.product_fact(ext2, "Is the PlayStation 5 available?", {"last_results": []}, "en", {"tools": [], "llm": []})
    assert r2["answer_type"] == "not_found"  # product we don't sell
