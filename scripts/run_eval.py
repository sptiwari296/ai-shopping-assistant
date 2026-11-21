"""Run the labelled evaluation set and compute the 7 metrics from brief section 7.

  python scripts/run_eval.py --mode mock            rules only (MOCK_LLM), fast, free
  python scripts/run_eval.py --mode live --pause 3  real Groq LLM, pause between cases (rate limits)

Writes eval/results_<mode>.json (every case, every check) and prints the summary table.
All checks are CODE against the database: nothing here trusts the assistant's own words.
"""
import argparse
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from app import compose, config, db  # noqa: E402

CUSTOMER = "C005"
DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def product_ok(pid: str, exp: dict, prev_prices: list[int]) -> tuple[bool, list[str]]:
    """Does one returned product respect every constraint the shopper gave?"""
    p = db.get_product(pid)
    why = []
    if exp.get("budget_max") and p["price"] > exp["budget_max"]:
        why.append(f"{pid} ₹{p['price']} > budget {exp['budget_max']}")
    if exp.get("brand") and norm(p["brand"]) != norm(exp["brand"]):
        why.append(f"{pid} brand {p['brand']} != {exp['brand']}")
    if exp.get("color") or exp.get("size"):
        vs = (p["attributes"] or {}).get("variants", [])
        if not any((not exp.get("color") or v["color"] == exp["color"]) and (not exp.get("size") or v["size"] == exp["size"])
                   and v["stock"] > 0 for v in vs):
            why.append(f"{pid} has no {exp.get('color', '')} {exp.get('size', '')} in stock")
    if exp.get("cheaper_than_previous") and prev_prices and p["price"] >= min(prev_prices):
        why.append(f"{pid} ₹{p['price']} not cheaper than previous min ₹{min(prev_prices)}")
    return not why, why


def grounded(reply: dict, user_text: str) -> tuple[bool, list[str]]:
    """Every product/price/stock claim must match the catalogue."""
    why = []
    for p in reply["products"]:
        d = db.get_product(p["product_id"])
        if not d:
            why.append(f"invented product {p['product_id']}")
            continue
        if d["price"] != p["price"]:
            why.append(f"{p['product_id']} price {p['price']} != DB {d['price']}")
        if (d["stock"] > 0) != p["in_stock"]:
            why.append(f"{p['product_id']} in_stock wrong")
    # text claims: IDs must exist; ₹ amounts must be real prices / numbers the shopper said / computed gaps
    ids_in_text = set(compose.PID.findall(reply["answer"]))
    real = {i for i in ids_in_text if db.get_product(i)}
    why += [f"unknown id {i} in text" for i in ids_in_text - real]
    allowed_ids = real | {p["product_id"] for p in reply["products"]}
    prices = [db.get_product(i)["price"] for i in allowed_ids]
    nums = set(prices) | {int(float(x.replace(",", ""))) for x in re.findall(r"[\d,]{3,}", user_text) if x.replace(",", "")}
    nums |= {abs(a - b) for a in prices for b in prices}
    for k in re.findall(r"(\d+)\s*k\b", user_text.lower()):
        nums.add(int(k) * 1000)
    if "lakh" in user_text.lower():
        nums.add(100000)
    why += compose.grounding_check(reply["answer"], allowed_ids, nums)
    return not why, why


def run(mode: str, pause: float) -> dict:
    config.MOCK_LLM = mode == "mock"
    db.init_db(reset=True)
    from app.brain import Brain
    brain = Brain()
    cases = json.loads((ROOT / "eval" / "questions.json").read_text(encoding="utf-8"))
    with db.connect() as conn:
        own_products = {r[0] for r in conn.execute("SELECT product_id FROM orders WHERE customer_id=?", (CUSTOMER,))}
    results, latencies = [], []

    for case in cases:
        exp, sid = case["expect"], f"eval-{mode}-{case['id']}"
        replies, prev_prices = [], []
        for turn in case["turns"]:
            if replies:
                prev_prices = [p["price"] for p in replies[-1]["products"]]
            t0 = time.time()
            replies.append(brain.handle(turn, sid, CUSTOMER))
            latencies.append(time.time() - t0)
        r, user_text = replies[-1], case["turns"][-1]
        checks = {}

        if "answer_type" in exp:
            checks["type"] = r["answer_type"] in exp["answer_type"]
        top3 = [p["product_id"] for p in r["products"][:3]]
        if (exp.get("product_type") or exp.get("category")) and r["answer_type"] == "recommendation" and top3:
            fit = [db.get_product(i) for i in top3]
            ok = [(exp.get("product_type") and f["product_type"] == exp["product_type"]) or
                  (not exp.get("product_type") and f["category"] == exp["category"]) for f in fit]
            checks["relevance"] = sum(ok) / len(ok)
        if any(k in exp for k in ("budget_max", "brand", "color", "size", "cheaper_than_previous")) and r["products"]:
            per = [product_ok(p["product_id"], exp, prev_prices) for p in r["products"]]
            checks["constraint"] = sum(ok for ok, _ in per) / len(per)
            checks["constraint_why"] = [w for _, ws in per for w in ws]
        g_ok, g_why = grounded(r, " ".join(case["turns"]))
        checks["grounded"], checks["grounded_why"] = g_ok, g_why
        if case["group"] == "not_in_catalogue":
            checks["not_found"] = r["answer_type"] == "not_found"
        if case["group"] == "vague":
            checks["clarify"] = r["answer_type"] == "clarification" and bool(r.get("follow_up_question"))
        if case["group"] == "privacy":
            checks["safe"] = r["answer_type"] == "refusal" and not r["products"]
        if exp.get("forbidden"):
            low = r["answer"].lower()
            hits = [w for w in exp["forbidden"] if re.search(rf"\b{re.escape(w)}\b", low)]
            checks["safe"] = not hits
            checks["forbidden_hits"] = hits
        if exp.get("language") == "hi":
            checks["language"] = bool(DEVANAGARI.search(r["answer"]))
        elif exp.get("language") == "hinglish":
            checks["language"] = not DEVANAGARI.search(r["answer"])
        if exp.get("compare_first_two"):
            first = [p["product_id"] for p in replies[0]["products"][:2]]
            checks["compare_ok"] = [p["product_id"] for p in r["products"]] == first
        if exp.get("ids"):
            checks["compare_ok"] = [p["product_id"] for p in r["products"]] == exp["ids"]
        if exp.get("stock_answer"):
            ans = r["answer"].lower()
            checks["stock_ok"] = ("out of stock" in ans or "not available" in ans or "unavailable" in ans) == (exp["stock_answer"] == "out of stock")
        if exp.get("own_orders"):
            checks["own_ok"] = bool(r["products"]) and {p["product_id"] for p in r["products"]} <= own_products

        with db.connect() as conn:
            rec = json.loads(conn.execute("SELECT record FROM traces WHERE trace_id=?", (r["trace_id"],)).fetchone()[0])
        fell_back = [l.get("step") for l in rec["llm"] if l.get("source") == "rules" or l.get("writer") == "code" and l.get("reason")]
        results.append({"id": case["id"], "group": case["group"], "turns": case["turns"], "answer_type": r["answer_type"],
                        "answer": r["answer"], "products": [p["product_id"] for p in r["products"]],
                        "trace_id": r["trace_id"], "llm_fallbacks": fell_back, "checks": checks})
        bad = [k for k, v in checks.items() if v is False or (isinstance(v, float) and v < 1)]
        print(f"{case['id']:3s} {r['answer_type']:15s} {'OK ' if not bad else 'X  ' + ','.join(bad)}  | {r['answer'][:70]}")
        if pause:
            time.sleep(pause)

    def avg(key, rows=results):
        vals = [x["checks"][key] for x in rows if key in x["checks"]]
        return (sum(float(v) for v in vals) / len(vals), len(vals)) if vals else (None, 0)

    summary = {
        "mode": mode, "model": "rules only" if mode == "mock" else config.GROQ_MODEL, "cases": len(results),
        "answer_type_accuracy": avg("type"),
        "relevance_top3": avg("relevance"),
        "constraint_accuracy": avg("constraint"),
        "groundedness": avg("grounded"),
        "not_found_handling": avg("not_found"),
        "follow_up_questions": avg("clarify"),
        "safety": avg("safe"),
        "language_match": avg("language"),
        "latency_p50_s": round(statistics.median(latencies), 2),
        "latency_p95_s": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))], 2),
        "llm_fallback_cases": sum(1 for x in results if x["llm_fallbacks"]),
    }
    out = {"summary": summary, "results": results}
    (ROOT / "eval" / f"results_{mode}.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== SUMMARY ===")
    for k, v in summary.items():
        if isinstance(v, tuple):
            print(f"{k:22s} {'-' if v[0] is None else f'{v[0]*100:5.1f}%'}   (n={v[1]})")
        else:
            print(f"{k:22s} {v}")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "live"], default="mock")
    ap.add_argument("--pause", type=float, default=0)
    a = ap.parse_args()
    run(a.mode, a.pause)
