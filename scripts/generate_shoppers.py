"""Generate shoppers with the LLM (brief 4.2), then past orders with code.

HOW:
  1. Send data/prompts/shoppers_prompt.txt to Groq in JSON mode.
  2. Validate every shopper with a Pydantic schema; retry up to 3 times if invalid.
  3. Assign IDs C001.. and add 3 edge-case roles in code (so they are guaranteed):
       - a brand-new shopper with no orders
       - a contradictory-likes shopper (already asked from the LLM, flagged here)
       - a shopper whose old favourites are now out of stock
  4. Generate 3-5 past orders per shopper from REAL catalogue products.

WHY LLM for people but code for orders: names and tastes need variety (LLM is good
at that); orders must point at real product IDs (code guarantees it).

Run:  python scripts/generate_shoppers.py          (uses Groq)
      python scripts/generate_shoppers.py --no-llm (offline fallback, same schema)
"""
import json  # read catalogue, write shoppers/orders
import random  # pick products / dates for orders
import sys  # command-line flags + import path
from datetime import date, timedelta  # order dates
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator  # schema for LLM output

ROOT = Path(__file__).resolve().parent.parent  # project root
sys.path.insert(0, str(ROOT))  # so "from app import config" works when run as a script
from app import config  # noqa: E402  (import after the path fix, on purpose)

DATA = ROOT / "data"
CATEGORIES = ["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]
rng = random.Random(7)  # fixed seed for the code-generated parts (orders)


class Shopper(BaseModel):  # what ONE valid shopper must look like
    name: str = Field(min_length=3)
    city: str
    preferred_language: Literal["en", "hi", "hinglish"]  # only these 3 values
    budget_min: int = Field(ge=0)  # >= 0
    budget_max: int = Field(ge=1000, le=100000)  # between ₹1,000 and ₹1,00,000
    likes: list[Literal["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]] = Field(min_length=1, max_length=4)  # 1-4 real categories
    favourite_brands: list[str] = Field(max_length=3)
    notes: str

    @model_validator(mode="after")  # a rule across two fields, checked after both are set
    def budget_order(self):
        if self.budget_max <= self.budget_min:
            raise ValueError("budget_max must be greater than budget_min")
        return self


def from_llm() -> list[dict]:  # ask Groq for shoppers, validate, retry
    from groq import Groq

    prompt = (DATA / "prompts" / "shoppers_prompt.txt").read_text(encoding="utf-8")  # the exact prompt (saved for the brief)
    client = Groq(api_key=config.GROQ_API_KEY)
    last_err = None
    for attempt in range(1, 4):  # up to 3 tries
        resp = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},  # JSON mode
            temperature=0.8,  # creative: we WANT varied names, cities, tastes
            max_tokens=8000,  # ~28 shoppers need a long reply
            reasoning_effort="low",
        )
        raw = resp.choices[0].message.content
        (DATA / "prompts" / "shoppers_raw_output.json").write_text(raw, encoding="utf-8")  # keep the raw output for transparency
        try:
            items = json.loads(raw)["shoppers"]  # the list inside {"shoppers": [...]}
            valid = [Shopper(**s).model_dump() for s in items]  # Pydantic checks EVERY shopper
            if len(valid) < 25:  # brief minimum
                raise ValueError(f"only {len(valid)} shoppers")
            print(f"LLM attempt {attempt}: {len(valid)} valid shoppers")
            return valid
        except (json.JSONDecodeError, KeyError, ValidationError, ValueError) as e:  # bad JSON / missing key / schema error / too few
            last_err = e
            print(f"LLM attempt {attempt} invalid: {str(e)[:200]}")  # then retry
    raise RuntimeError(f"LLM failed 3 times: {last_err}")


def offline() -> list[dict]:  # fallback without an LLM (same schema)
    first = ["Aarav", "Diya", "Kabir", "Ananya", "Rohan", "Meera", "Vivaan", "Isha", "Arjun", "Sana", "Karan", "Pooja", "Nikhil", "Riya"]
    last = ["Sharma", "Iyer", "Khan", "Patel", "Reddy", "Das", "Mehta", "Nair", "Gupta", "Singh"]
    cities = ["Prayagraj", "Lucknow", "Pune", "Bengaluru", "Jaipur", "Kolkata", "Chennai", "Delhi"]
    out = []
    for i in range(28):
        bmax = rng.choice([2000, 3000, 5000, 10000, 25000, 60000])
        out.append(dict(name=f"{rng.choice(first)} {rng.choice(last)}", city=rng.choice(cities),
                        preferred_language=rng.choice(["en", "hi", "hinglish"]), budget_min=0, budget_max=bmax,
                        likes=rng.sample(CATEGORIES, k=rng.randint(1, 3)), favourite_brands=[], notes="Generated offline."))
    return out


def make_orders(shoppers: list[dict], products: list[dict]) -> list[dict]:  # past orders, always pointing at REAL products
    orders, oid = [], 1
    in_stock = [p for p in products if p["stock"] > 0]
    out_of_stock = [p for p in products if p["stock"] == 0]
    for s in shoppers:
        role = s.get("edge_case")
        if role == "new_no_history":  # brand-new shopper -> no orders
            continue
        if role == "favourites_out_of_stock":  # their old buys are now sold out
            pool = out_of_stock
        else:  # products they like, within their budget (fallback: any in-stock product)
            pool = [p for p in in_stock if p["category"] in s["likes"] and p["price"] <= s["budget_max"]] or in_stock
        for _ in range(rng.randint(3, 5)):  # 3-5 orders each
            p = rng.choice(pool)
            d = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))  # a date in 2026
            orders.append({"order_id": f"O-{oid:04d}", "customer_id": s["customer_id"], "product_id": p["product_id"],  # O-0001...
                           "quantity": 1, "price_paid": p["price"], "order_date": d.isoformat()})
            oid += 1
    return orders


def main():
    products = json.loads((DATA / "catalogue.json").read_text(encoding="utf-8"))  # orders need the real catalogue
    use_llm = "--no-llm" not in sys.argv and not config.MOCK_LLM  # LLM unless told otherwise
    shoppers = from_llm() if use_llm else offline()
    for i, s in enumerate(shoppers, start=1):  # IDs assigned by CODE
        s["customer_id"] = f"C{i:03d}"  # C001, C002...
        s["edge_case"] = None
    # Guaranteed edge cases (brief 4.2), assigned in code:
    shoppers[0]["edge_case"] = "new_no_history"
    shoppers[0]["notes"] += " (Brand-new shopper, no orders yet.)"
    shoppers[1]["edge_case"] = "contradictory_likes"
    shoppers[1].update(favourite_brands=["Sony", "Apple"], budget_min=0, budget_max=2000,  # budget_min=0 was the Phase 1 bug fix
                       notes="Wants only premium brands but has a very small budget.")
    shoppers[2]["edge_case"] = "favourites_out_of_stock"
    shoppers[2]["notes"] += " (Old favourites are now out of stock.)"
    # Shopper 'Priya' is used by the privacy demo ("what did Priya buy?").
    shoppers[3]["name"] = "Priya Verma"

    orders = make_orders(shoppers, products)
    (DATA / "shoppers.json").write_text(json.dumps(shoppers, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "orders.json").write_text(json.dumps(orders, indent=1), encoding="utf-8")
    meta = {"generator": "Groq " + config.GROQ_MODEL if use_llm else "offline fallback (no LLM)",  # which model made the data (brief asks)
            "prompt_file": "data/prompts/shoppers_prompt.txt", "temperature": 0.8, "shoppers": len(shoppers), "orders": len(orders)}
    (DATA / "prompts" / "generation_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"wrote {len(shoppers)} shoppers, {len(orders)} orders  ({meta['generator']})")


if __name__ == "__main__":
    main()
