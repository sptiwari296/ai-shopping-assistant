# Build Log

## Phase 0 — Setup ✅
**Built:** a `.venv` on Python 3.12, the packages, `.env` (GROQ_API_KEY, MOCK_LLM, DB_PATH), `.gitignore`, `app/config.py`, and `app/api.py` with `GET /health`.

**How:** `config.py` loads `.env` once with python-dotenv, and every other file imports its settings from there, so there's one source of truth.

**Why:**
- `MOCK_LLM` flag from day one: the brief's tip, so tests never depend on Groq
- `.env` is git-ignored: the API key never reaches GitHub

**Bug hit:** Groq `gpt-oss-120b` returned an **empty reply** with `max_tokens=20`. It's a reasoning model and spends its first tokens thinking. Fix: `reasoning_effort="low"` and a larger `max_tokens`.

**Check:** `/health` → 200 `{"status":"ok","mock_llm":false}`, and a live Groq call → "OK."

## Phase 1 — Data ✅
**Built:** `scripts/generate_catalogue.py` (276 products, 6 categories), `scripts/generate_shoppers.py` (29 LLM shoppers + 105 code orders), `scripts/validate_data.py`, `data/DATA_NOTE.md`, and the prompt + raw LLM output in `data/prompts/`.

**How:**
- Catalogue = templates + a fixed seed → reproducible
- Shoppers = Groq JSON mode → Pydantic validation → retry up to 3 times
- Orders = code picks real product IDs
- Edge cases are assigned in code, so they're guaranteed

**Why:**
- Products need exact, checkable prices, so they come from code
- People need variety, so they come from the LLM
- Never trust LLM output without a schema check

**Bugs caught by the validator:** C002 had `budget_max < budget_min` (my override forgot `budget_min`), and the random missing-brand step hit the JBL demo product. The LLM also returned 27 or 29 shoppers instead of 28.

**Check:** `validate_data.py` → 0 violations.

## Phase 2 — SQLite + Hybrid search ✅
**Built:** `app/db.py` (5 tables, seeded from JSON on first start) and `app/search.py` (`HybridIndex`: FAISS + BM25 + RRF). Tests in `tests/test_search.py`.

**How:**
- Embedding model = multilingual MiniLM (384-d, local CPU, through fastembed)
- Vectors are L2-normalised, so the inner product equals cosine similarity
- BM25 over the same text
- RRF `1/(60+rank)` merges the two lists
- Embeddings are **cached**, so an upsert embeds only the changed product

**Why:**
- Meaning + exact words
- RRF uses ranks, so there's no tuning of scores on different scales
- **Reviews are NOT indexed**: they're untrusted (R8), so injection text can't pull a product into results

**Findings:**
- The first start downloads the model once (0.22 GB, ~90 s). Later builds take ~11 s, and each search takes 7–17 ms
- Live upsert was 5.5 s (re-embedding everything) → fixed with the vector cache → **0.033 s**
- A bare "JBL" query: the dense side ranks random items (an embedding of a brand name carries little meaning), and BM25 rescues it. That's the proof that we need hybrid search
- "electric car" still returns kettles, because search always returns *something*. So **not_found must be decided in the brain (Phase 4)**, not by search
- A Hindi query finds shoes but ranks formal shoes first. Phase 4 has the LLM rewrite the query into English before searching
- My test wrongly assumed there were ≥ 3 JBL products in the top 5, but the catalogue has only 4 JBL items. I fixed the test, not the code

**Check:** 6 tests pass.

## Phase 3 — Tools ✅
**Built:** `app/tools.py` with `search_products`, `get_product`, `check_stock`, `compare` and `my_orders`, plus `card()` (the small product view). Tests in `tests/test_tools.py`.

**How:** `search_products` works in stages:
1. Hybrid search gives the top 80 candidates in **fit order**
2. **SQL** `WHERE price <= ? AND stock > 0 AND category = ?`
3. Python checks brand (normalised: Levi's = levis), product type and the size/colour variant
4. Optional `max_per_type` for variety
5. Keep the first k

`check_stock` reads per-variant stock. `compare` builds rows only from catalogue fields and computes `cheapest`, `best_rated` and `price_gap` in code. `my_orders` filters by `customer_id`.

**Why:**
- R7: prices, stock and comparisons are facts, so code handles them
- Ranking keeps the search order (fit), never sorted by rating (R3)
- `my_orders` takes `customer_id` only from the server (R9)
- `compare` produces the "verdict facts" in code, so the LLM only words them

**Finding:** "trekking gift under ₹3000" returned 4 trekking poles. They're relevant but repetitive, so I added `max_per_type=2` → 2 poles + 2 backpacks.

**Check:** 15 tests pass, including the budget **boundary** (₹2,999 included at `budget_max=2999`, excluded at 2998), blue size 9 in stock vs size 10 out, and C001 (new shopper) has no orders.

## Phase 4 — The brain ✅
**Built:**
- `app/llm.py`: Groq JSON calls, retry on 429, raises `LLMError`
- `app/extract.py`: **step 1**. LLM → Pydantic `Extraction`, plus a rule-based fallback
- `app/memory.py`: **step 2a**. Session state in SQLite, merge rules, "cheaper" maths
- `app/compose.py`: **steps 4 + 5**. LLM wording + **grounding check**, with code templates as the fallback
- `app/brain.py`: the **router**: recommend / compare / product_fact / my_orders / refusals / not_found, plus clarify rules and the trace
- `scripts/chat_cli.py`: terminal demo
- `tests/test_brain.py`: 15 tests in mock mode

**How a message flows:** extract → code safety net (other shopper's ID or name → refusal) → route → tools → write → check → save session + trace.

**Key rules in code:**
- Clarify only when needed: no product type, or a high-consideration item with no budget. At most 2 questions in a row, and never ask the same thing twice
- "cheaper" → `budget_max = min(last prices) - 1`
- New product type → new topic, so the filters reset
- Unknown product type → `not_found` ("we don't sell X")
- Zero results → `not_found` + suggest the **cheapest** near match ("shall I widen?")
- A session is bound to its `customer_id`

**Why:**
- The LLM only extracts filters and words the answer. The facts come from tools, and the grounding check removes any unknown ID or ₹ amount
- The rules fallback means the system works with no API at all

**Bugs found in testing (the important part):**
| # | Mode | Bug | Fix |
|---|---|---|---|
| 1 | mock | "cheaper laptops" suggested a **₹92,999** laptop | suggest the cheapest when the budget blocked; say "already the cheapest" |
| 2 | mock | "sasta speaker" showed a ₹7,899 speaker | if cheap is requested with no earlier results → sort by price |
| 3 | mock | "Here are 1 options" | singular template |
| 4 | mock | "electric cars" was reported as "cars" | regex `electric cars?` |
| 5 | **live** | flat-feet shoes → not_found: the LLM said running shoes = `sports_fitness` | **category comes from the catalogue** once the type is known |
| 6 | **live** | "the blue one" with nothing shown → it guessed **jeans** | only search when a product is actually named; otherwise ask |
| 7 | **live** | "Do you sell electric cars?" → LLM said out_of_scope → refusal | prompt example + code net: a product question → `not_found` |
| 8 | **live** | "sasta" was not marked as cheaper | **my prompt example said `cheaper:false`**, fixed |
| 9 | live | "the blue one" was ambiguous across several shown items | check every shown item that comes in that colour |

Lesson: **mock mode hides LLM mistakes. Test both modes.** And the catalogue must be the source of truth over the LLM.

**Check:** 30 tests pass. The live demo runs with 0 grounding problems. Latency is 0.6–13 s (the slow ones are 429 retries on the Groq free tier).

## Phase 5 — API + UI + Voice ✅
**Built:**
- `app/api.py`: `POST /chat`, `GET /products/{id}`, `POST /ingest`, `GET /trace/{id}`, `GET /health`, plus `GET /shoppers` (UI picker) and `POST /transcribe` (voice)
- `ui/streamlit_app.py`: shopper picker, chat, product cards, comparison table, trace link, 🎤 mic
- `tests/test_api.py`: 7 tests
- Launch configs `shop-api` / `shop-ui` in `C:\HCL Tech\.claude\launch.json`

**How:**
- One `Brain` is built at startup (lifespan) and shared by all requests
- Pydantic validates the bodies, so bad input gets a 422
- `/ingest` → SQLite upsert → `index.upsert` → `refresh_vocab()` (new brands and types are known instantly)
- Voice: `st.audio_input` → `/transcribe` (Groq Whisper) → normal `/chat` with `input_mode="voice"` (recorded in the trace)
- Switching shopper in the UI = a new session

**Why:**
- The UI is a thin client: all logic sits behind the API that the judges test
- Voice at the edge reuses the tested pipeline

**Bugs found:**
- Ingesting a **new product type** ("Meditation Cushion") wasn't understood in mock mode, because the rule extractor had a hard-coded type list. Fix: the rules also match the live catalogue types (R11)
- The preview tool reads `C:\HCL Tech\.claude\launch.json`, so "api" started the **hospital** API. Fix: separate `shop-api` / `shop-ui` entries

**Verified in the browser** (live LLM): laptop → clarify → "under 60k study" → 2 laptops → "compare the first two" → table + verdict. Voice: a Windows-TTS WAV → Whisper → *"Show me a Bluetooth speaker under Rs. 2000."* → 3 speakers ≤ ₹2,000, and the trace shows `input_mode: voice`.

**Known nit:** the LLM sentence said "two speakers" while 3 were listed. Counts in the wording aren't checked yet (Phase 6).

**Check:** 37 tests pass.

## Phase 6 — Safety + trace ✅
**Built:** `app/safety.py` (injection detector, `clean_text`, PII `redact`, `output_problems`), wired into `compose.py` and `brain.py`, plus `tests/test_safety.py` (7 tests) and `scripts/redteam.py` (11 live attacks).

**Defence in layers:**
1. **Identity:** `customer_id` comes from the body. Sessions are bound to it, plus a code safety net for other names and IDs
2. **Retrieval:** reviews are never indexed
3. **LLM input:** reviews are never sent, and descriptions have instruction-like sentences stripped (covers poisoned `/ingest`)
4. **LLM output:** unknown IDs, unknown ₹ amounts, **invented offers** ("free", "% off") and **wrong counts** ("two speakers" when 3 are shown) → replaced by code text
5. **Ranking** is done by code
6. **Records:** phone, email and card numbers are redacted in the trace and the answer

**Trace now has:** `safety.injection_in_message`, `retrieved` (top 10 RRF before filters), `input_mode`, extraction, filters, tools, the LLM steps with grounding problems, answer and latency.

**Bugs found:**
- My test assumed the injection-review speaker should "never be first", but it's simply a good fit. The right test: **ranking is identical with and without the review** ✅
- Live: Roman-script Hinglish was labelled `hi` by the LLM → Devanagari reply. **The script is decided by code** (does the text contain Devanagari?)
- My red-team check flagged "**off**ice" as "off". Fixed with a word-boundary regex

**Known limitation:** the "P-1050 is free" attack got a safe but unhelpful "I can't provide that information".

**Check:** 44 tests pass. Live red-team: **11/11 attacks handled safely**, and 3 injection attempts were flagged in the traces.

## Phase 7 — Evaluation ✅
**Built:** `eval/questions.json` (43 labelled cases in the brief's mix), `scripts/run_eval.py` (7 brief metrics + language, all checked in code against the DB), `scripts/export_traces.py` (3 sample audit records), `eval/report.md`.

**How:** each case runs in a fresh session for C005; multi-turn cases check the last reply (and the previous turn for "cheaper" / "first two"). Two configurations: **live** (Groq) vs **mock** (rules only).

**Results:**
- Live v1: type 95.2%, groundedness 97.7%, not-found 50%, everything else 100%
- Live v2 (after fixes): 100% on all metrics, p50 1.6 s
- Mock: type 90.5%, not-found 50%, groundedness and safety 100%

**Bugs the eval found:**
- **The app accepted "₹1 lakh" as ₹1**, because stock counts were in the allowed-rupee set. Fix: a k/lakh parser, and stocks excluded
- The LLM dropped the unknown brand "Apple" → code `not_found` for brands we don't carry
- "PlayStation 5 available?" → `not_found`
- A friend's orders weren't refused when the name was unknown → code safety net

**Honesty:** v2 fixes were made after seeing the cases (overfitting risk), so v1 is reported too, and caveats are listed in `report.md`.

**Also hit:** the ONNX embedding model crashed with the API server also running (7.7 GB RAM). Ran the eval with the servers stopped.

**Check:** 46 tests pass.

## Phase 8a — Docker ✅
**Built:** `Dockerfile` (python:3.12-slim, the embedding model **downloaded at build time** into `/models`), `docker-compose.yml` (`api` + `ui` from one image, SQLite on a named volume, API healthcheck, UI waits for a healthy API), `.dockerignore` (keeps `.env`, `.venv` and tests out of the image).

**How:**
- `search.py` reads `FASTEMBED_CACHE_PATH`, so the container uses the pre-downloaded model (no 90 s first start)
- The UI uses `API_URL=http://api:8000` for container-to-container calls and `PUBLIC_API_URL=http://localhost:8000` for links in the browser
- `.env` is optional (`required: false`)

**Why:**
- The brief requires `docker compose up`
- The secret is never baked into the image; it's passed at runtime from `.env`

**Verified:** `docker compose up` → api healthy (276 indexed) + ui 200. A live Hinglish chat via the container returned 5 cheap speakers sorted by price. The browser UI → "Do you sell electric cars?" → `not_found`. Image size: 1.61 GB.

## Phase 8b — README + final ✅
`README.md` covers the approach, the architecture image, the 5-step flow, run steps (Docker + local), curl examples, the API table, the stack with reasons (including the Ollama → Groq swap), the 6 safety layers, the eval results (v1/v2/rules), the data summary, the layout, limitations, the AI-usage note and the team table. Tagged `final`.

## Phase 9 — Simplify for explainability ✅
**Why:** 1,541 lines in `app/` was too much to explain in an interview. Most of it was edge-case branches and a large rule fallback, not the core idea.

**What changed (behaviour kept):**
- `extract.py` → **`understand.py`** (step 1). Same LLM prompt and schema; the 120-line rule system became a ~40-line keyword fallback
- `compose.py` → **`writer.py`** (steps 4+5). One `write()` function for all reply types, plus one `grounding()` check
- `brain.py` 380 → ~210 lines: fixed replies became one `(en, hi, hinglish)` table; removed multi-product stock check, the out-of-scope rescue (now handled by the prompt) and the near-match suggestion (`not_found` now offers to widen the search)
- `db.py`, `tools.py`, `memory.py`, `safety.py`, `llm.py`, `api.py` were compacted
- `search.py`: same algorithm. `index_text` was **proved identical to full-v1 for all 276 products**, so retrieval is unchanged

**Result:** `app/` **1,541 → ~900 lines (−41%)**, one job per file. The old version is kept as the tag `full-v1`.

**Bug caught while rewriting:** reordering the words in `index_text` changed the embeddings, and a bare "JBL" search put a kurta at #2. I restored the exact original order and proved it identical.

**Check:** 45 tests pass. Live re-run: 95.2% type, 100% grounded + safety (26 cases rate-limited to rules). Red-team 11/11. Mock eval: answer-type accuracy **92.9%** (was 90.5%), not-found **75%** (was 50%), groundedness / relevance / safety **100%**.

## Phase 10 — Speed fix ✅
**Problem:** replies took 24–40 s. **Cause (from Groq's 429 message):** the daily limit for `gpt-oss-120b` (200,000 tokens/day) was used up by the evals: "Used 199490". The old retry code then waited 3 + 6 + 12 s per call.

**Fix:**
- Switched to `openai/gpt-oss-20b` (its own quota, ~0.3 s per call vs 1.6 s)
- On a 429: wait once for 2 s, then fail fast to the code-written reply

**Result:** 1–4 s per message, with all steps using the LLM.

**New bugs from the smaller model:**
- "Is the blue one in size 9?" → it put "blue one" in product_type → not_found. Fix: a stock question right after results is a follow-up, never "not sold"; the colour and size are read from the message if the LLM misses them
- It **contradicted the stock check** ("no information about blue size 9"). Fix: stock answers always use the code sentence (pure facts, no LLM)

**Check:** 45 tests pass.

## Phase 11 — Voice accuracy ✅
**Problem (from real UI traces):** Whisper heard "running shoes" as "running **soosh**", and one recording became "**Bueno...**" (it guessed Spanish). The slow replies in the same traces (22–44 s) came from the UI still running the old process (120b model plus old retries); a restart picks up Phase 10.

**Fix:** `/transcribe` now sends Whisper a **vocabulary prompt** (shopping words in English and Hinglish) with `temperature=0`. A transcript of fewer than 2 words returns 422 "Couldn't hear that clearly" instead of searching for junk. The UI shows that message in a friendly form.

**Also:** the embedding model is now loaded once per process and shared (`search.get_model()`). With the UI app running, the test suite loaded 5 copies and ran out of RAM ("bad allocation").

**Check:** TTS test clips transcribe exactly ("Give me running shoes under 5000 rupees.", "Show me a cheap bluetooth speaker."). A 1-word clip → 422. Chat replies take 1.7–2.0 s. 45 tests pass.
