# Project workflow

The whole project in three stages: build the data, start the server, answer each message.

> **Core rule:** the LLM handles language (understanding the question, writing the answer). Code handles facts (products, prices, orders) and checks the LLM did not invent anything.

## Stage 1: prepare the data (offline, once)

```
scripts/generate_catalogue.py  →  products (type, brand, price, rating, stock, reviews)
scripts/generate_shoppers.py   →  shoppers and their past orders
scripts/validate_data.py       →  checks the data is clean before it is used
```

## Stage 2: server startup (`app/api.py` → `Brain()`)

```
app/db.py      →  loads products, shoppers and orders into SQLite
app/search.py  →  builds two indexes over the products, once:
                    FAISS  (meaning:  "running shoes" ≈ "jogging sneakers")
                    BM25   (exact words: brand names, product IDs)
```

## Stage 3: one message, start to finish

```
Streamlit UI (ui/streamlit_app.py)
  typed text ─────────────┐
  voice → POST /transcribe ┘
            │
            ▼
       POST /chat  (app/api.py)  →  Brain.handle (app/brain.py)
            │
 ⓪ REQUEST GUARD   app/safety.py      customer_id comes from the request, never the message;
            │                         injection blocked, phone/email redacted,
            │                         "show me another shopper's orders" refused
            ▼
 ① UNDERSTAND      app/understand.py  LLM → JSON filters, validated by Pydantic
            │                         "shoes under 3000" → {type: shoes, max_price: 3000}
            │                         LLM fails → keyword-rule fallback
            ▼
 ② MEMORY+ROUTER   app/memory.py      merge with the session ("cheaper", "only Nike", "first two")
            │      app/brain.py       if/elif picks one path:
            │                         recommend · compare · fact · clarify · refuse
            ▼
 ③ TOOLS           app/tools.py       hybrid search: FAISS + BM25 merged with RRF
            │      app/search.py      → SQL filters (price, stock, category)
            │                         → brand / size / colour checks
            ▼
 ④ EXPLAIN         app/writer.py      LLM writes a short reply, one reason per product,
            │                         in the shopper's language, from tool facts only
            ▼
 ⑤ GROUNDING CHECK app/brain.py       every product ID, ₹ amount, offer and count must match
            │                         the tool output, else replaced by code-written text
            ▼
 ⑥ SAVE + REPLY    app/db.py          session + full trace saved (GET /trace/{id})
            │
            ▼
Streamlit shows the reply and product cards
```

Only steps ① and ④ call the LLM (Groq). Everything else is plain, testable code.

## Stage 4: proving it works

| File | What it proves |
|---|---|
| `tests/` (pytest) | API, brain and safety behave as expected |
| `scripts/run_eval.py` → `eval/report.md` | accuracy scores on a fixed set of test questions |
| `scripts/redteam.py` | attack prompts ("ignore your rules…") are blocked |
| `scripts/export_traces.py` | the step-by-step record of any answer |
| `Dockerfile`, `docker-compose.yml` | the whole app runs with one command |

## 20-second pitch

> A shopper asks in text or voice. We first check the message is safe. An LLM turns it into structured filters, and memory keeps the context. A router picks the path, and hybrid RAG search finds real products from our database. The LLM then writes the answer from those results only, and a grounding check confirms every product and price is real before the reply goes out. Every step is saved as a trace, and tests, an eval report and red-team checks back it up.
