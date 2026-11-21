# Data Note (brief §4 — one page)

## Where the data came from
| File | Source | How |
|---|---|---|
| `catalogue.json` (276 products) | **invented** shop "NovaMart" | `scripts/generate_catalogue.py`: Python templates + fixed seed 42 (reproducible). Brand names are used only as labels; all descriptions, specs, prices and stock are invented |
| `shoppers.json` (29 shoppers) | **LLM-generated** | `scripts/generate_shoppers.py` → Groq `openai/gpt-oss-120b`, JSON mode, temperature 0.8, prompt in `prompts/shoppers_prompt.txt` (verbatim), raw output in `prompts/shoppers_raw_output.json` |
| `orders.json` (105 orders) | **code-generated** | 3–5 orders per shopper, picked from real catalogue products that match the shopper's likes and budget |

No real customer data, no scraped personal information, no copied catalogue text.

## Fields
**Product:** `product_id` (P-1001…), `name`, `category` (6), `product_type`, `brand`, `price` (₹ integer), `stock` (total units), `attributes` (type-specific specs; footwear/fashion have `variants` [{color, size, stock}]), `description`, `rating` (0–5), `reviews` (list of short texts).
**Shopper:** `customer_id` (C001…), `name`, `city`, `preferred_language` (en/hi/hinglish), `budget_min`, `budget_max`, `likes` (categories), `favourite_brands`, `notes`, `edge_case`.
**Order:** `order_id`, `customer_id`, `product_id`, `quantity`, `price_paid`, `order_date`.

## Messy on purpose (brief §4.1)
| Case | Count |
|---|---|
| Out of stock (stock 0) | 29 |
| Nearly out (1–2 left) | 47 |
| Missing rating / missing brand | 5 / 2 |
| Odd weight units ("450 g", "0.45 kg", "0.5kg") | outdoor, kitchen, sports items |
| Hindi descriptions | 16 |
| Hinglish reviews | several |
| Look-alikes: JBL vs boAt "Go 20W" speaker, same specs, ₹2,999 vs ₹1,799 | P-1046 / P-1047 |
| Injection reviews (R8 test) | 2 (one speaker, one earbuds) |

## Edge-case shoppers (brief §4.2)
- `C001`: brand-new, no orders
- `C002`: contradictory (premium brands only, ₹2,000 budget)
- `C003`: old favourites now out of stock
- `C004 Priya Verma`: used in the privacy test ("what did Priya buy?")

## Validation
`python scripts/validate_data.py` → **0 violations**. It checks the schema, ID formats, duplicates, price > 0, stock ≥ 0, variant stock adds up, rating range, ≥ 250 products, ≥ 5 categories, ≥ 25 shoppers, every order → a real product and shopper, ≥ half the shoppers have 3+ orders, and all edge cases present.

## What went wrong (and was caught)
- The LLM returned **27, then 29** shoppers when asked for 28, so LLM counts are unreliable. The validator checks for ≥ 25.
- The validator caught **2 bugs in our own generator**: an edge-case shopper with `budget_max < budget_min`, and the random "missing brand" step hitting a demo product. Both fixed.

## What it does NOT cover
No real prices or real stock; no product images; no shipping, discounts or returns; only English, Hindi and Hinglish text; reviews are short one-liners.
