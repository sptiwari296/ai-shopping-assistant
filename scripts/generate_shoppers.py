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
import json
import random
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import config  # noqa: E402

DATA = ROOT / "data"
CATEGORIES = ["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]
rng = random.Random(7)


class Shopper(BaseModel):
    name: str = Field(min_length=3)
    city: str
    preferred_language: Literal["en", "hi", "hinglish"]
    budget_min: int = Field(ge=0)
    budget_max: int = Field(ge=1000, le=100000)
    likes: list[Literal["footwear", "electronics", "outdoor", "home_kitchen", "fashion", "sports_fitness"]] = Field(min_length=1, max_length=4)
    favourite_brands: list[str] = Field(max_length=3)
    notes: str

    @model_validator(mode="after")
    def budget_order(self):
        if self.budget_max <= self.budget_min:
            raise ValueError("budget_max must be greater than budget_min")
        return self


def from_llm() -> list[dict]:
    from groq import Groq

    prompt = (DATA / "prompts" / "shoppers_prompt.txt").read_text(encoding="utf-8")
    client = Groq(api_key=config.GROQ_API_KEY)
    last_err = None
    for attempt in range(1, 4):
        resp = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.8,
            max_tokens=8000,
            reasoning_effort="low",
        )
        raw = resp.choices[0].message.content
        (DATA / "prompts" / "shoppers_raw_output.json").write_text(raw, encoding="utf-8")
        try:
            items = json.loads(raw)["shoppers"]
            valid = [Shopper(**s).model_dump() for s in items]
            if len(valid) < 25:
                raise ValueError(f"only {len(valid)} shoppers")
            print(f"LLM attempt {attempt}: {len(valid)} valid shoppers")
            return valid
        except (json.JSONDecodeError, KeyError, ValidationError, ValueError) as e:
            last_err = e
            print(f"LLM attempt {attempt} invalid: {str(e)[:200]}")
    raise RuntimeError(f"LLM failed 3 times: {last_err}")


def offline() -> list[dict]:
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


def make_orders(shoppers: list[dict], products: list[dict]) -> list[dict]:
    orders, oid = [], 1
    in_stock = [p for p in products if p["stock"] > 0]
    out_of_stock = [p for p in products if p["stock"] == 0]
    for s in shoppers:
        role = s.get("edge_case")
        if role == "new_no_history":
            continue
        if role == "favourites_out_of_stock":
            pool = out_of_stock
        else:
            pool = [p for p in in_stock if p["category"] in s["likes"] and p["price"] <= s["budget_max"]] or in_stock
        for _ in range(rng.randint(3, 5)):
            p = rng.choice(pool)
            d = date(2026, 1, 1) + timedelta(days=rng.randint(0, 300))
            orders.append({"order_id": f"O-{oid:04d}", "customer_id": s["customer_id"], "product_id": p["product_id"],
                           "quantity": 1, "price_paid": p["price"], "order_date": d.isoformat()})
            oid += 1
    return orders


def main():
    products = json.loads((DATA / "catalogue.json").read_text(encoding="utf-8"))
    use_llm = "--no-llm" not in sys.argv and not config.MOCK_LLM
    shoppers = from_llm() if use_llm else offline()
    for i, s in enumerate(shoppers, start=1):
        s["customer_id"] = f"C{i:03d}"
        s["edge_case"] = None
    # Guaranteed edge cases (brief 4.2), assigned in code:
    shoppers[0]["edge_case"] = "new_no_history"
    shoppers[0]["notes"] += " (Brand-new shopper, no orders yet.)"
    shoppers[1]["edge_case"] = "contradictory_likes"
    shoppers[1].update(favourite_brands=["Sony", "Apple"], budget_min=0, budget_max=2000,
                       notes="Wants only premium brands but has a very small budget.")
    shoppers[2]["edge_case"] = "favourites_out_of_stock"
    shoppers[2]["notes"] += " (Old favourites are now out of stock.)"
    # Shopper 'Priya' is used by the privacy demo ("what did Priya buy?").
    shoppers[3]["name"] = "Priya Verma"

    orders = make_orders(shoppers, products)
    (DATA / "shoppers.json").write_text(json.dumps(shoppers, ensure_ascii=False, indent=1), encoding="utf-8")
    (DATA / "orders.json").write_text(json.dumps(orders, indent=1), encoding="utf-8")
    meta = {"generator": "Groq " + config.GROQ_MODEL if use_llm else "offline fallback (no LLM)",
            "prompt_file": "data/prompts/shoppers_prompt.txt", "temperature": 0.8, "shoppers": len(shoppers), "orders": len(orders)}
    (DATA / "prompts" / "generation_meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"wrote {len(shoppers)} shoppers, {len(orders)} orders  ({meta['generator']})")


if __name__ == "__main__":
    main()
