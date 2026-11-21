"""The orchestrator: message -> 1 understand (LLM) -> 2 memory + router (code) -> 3 tools (code)
-> 4 write (LLM) -> 5 check (code) -> typed reply + saved trace.

A router, not agents: the intents are a fixed list, so a plain if/elif picks the path.
"""
import json
import time
import uuid

from app import db, memory, safety, tools, understand, writer
from app.search import HybridIndex
from app.tools import norm

HIGH_CONSIDERATION = {"Laptop": "study, office, gaming", "Smartphone": "camera, gaming, battery",  # ask budget first
                      "Headphones": "music, calls, travel", "Wireless Earbuds": "gym, calls, music", "Smartwatch": "fitness, style"}
GENERIC = {"gift", "present", "something", "item", "product", "thing"}
LANGS = ("en", "hi", "hinglish")
MSG = {  # fixed replies: (English, Hindi, Hinglish) — written by code, so always correct
    "ask_type": ("What kind of product are you looking for (e.g. shoes, speaker, backpack)?",
                 "आप किस तरह का प्रोडक्ट ढूंढ रहे हैं (जैसे जूते, स्पीकर, बैग)?", "Aap kis tarah ka product dhoondh rahe hain (jaise shoes, speaker, bag)?"),
    "ask_budget": ("What's your budget, and what will you mostly use it for ({uses})?",
                   "आपका बजट कितना है, और ज़्यादातर किस काम के लिए ({uses})?", "Aapka budget kitna hai, aur zyada kis kaam ke liye ({uses})?"),
    "ask_which": ("Which product do you mean? Say 'the first one' or give its ID (like P-1046).",
                  "आप कौन सा प्रोडक्ट पूछ रहे हैं? 'पहला वाला' कहें या ID दें।", "Aap kaunsa product pooch rahe hain? 'pehla wala' bolein ya ID dein."),
    "not_sold": ("I couldn't find {what} in our catalogue. We sell footwear, electronics, outdoor, kitchen, fashion and sports items.",
                 "हमारे कैटलॉग में {what} नहीं मिला।", "Hamare catalogue mein {what} nahi mila."),
    "no_match": ("I couldn't find a match in our catalogue. Shall I widen the search (for example a higher budget)?",
                 "कोई मैच नहीं मिला। क्या मैं खोज बढ़ाऊँ (जैसे ज़्यादा बजट)?", "Koi match nahi mila. Kya search badha doon (jaise zyada budget)?"),
    "cheapest": ("Those are already our cheapest matching options in stock.", "ये पहले से ही सबसे सस्ते विकल्प हैं।",
                 "Ye pehle se hi sabse saste options hain."),
    "found": ("Here are {n} option(s) that fit what you asked for.", "आपकी ज़रूरत के हिसाब से ये {n} विकल्प हैं।",
              "Aapki zaroorat ke hisaab se ye {n} options hain."),
    "refuse_other": ("Sorry, I can't share another shopper's information.", "माफ़ कीजिए, मैं किसी और ग्राहक की जानकारी नहीं दे सकता।",
                     "Sorry, main kisi aur shopper ki jaankari share nahi kar sakta."),
    "refuse_scope": ("I can only help with shopping at NovaMart.", "मैं केवल NovaMart पर शॉपिंग में मदद कर सकता हूँ।",
                     "Main sirf NovaMart shopping mein madad kar sakta hoon."),
    "hello": ("Hi! What are you shopping for, and what's your budget?", "नमस्ते! आप क्या खरीदना चाहते हैं?", "Namaste! Aap kya khareedna chahte hain?"),
    "orders": ("Your recent orders: {items}.", "आपके हाल के ऑर्डर: {items}।", "Aapke recent orders: {items}."),
    "no_orders": ("You don't have any past orders yet.", "आपका अभी तक कोई ऑर्डर नहीं है।", "Aapka abhi tak koi order nahi hai."),
}


def say(key: str, lang: str, **kw) -> str:
    return MSG[key][LANGS.index(lang)].format(**kw)


def reply(kind: str, answer: str, products=(), follow_up=None) -> dict:  # the /chat response shape (brief §6.1)
    return {"trace_id": None, "answer_type": kind, "answer": answer, "products": list(products),
            "sources": [{"product_id": p["product_id"], "fields": ["price", "stock", "description"]} for p in products],
            "follow_up_question": follow_up}


def product_out(c: dict, reason: str) -> dict:
    return {"product_id": c["product_id"], "name": c["name"], "price": c["price"], "in_stock": c["in_stock"], "reason": reason}


class Brain:
    def __init__(self):
        db.init_db()
        self.index = HybridIndex()
        self.index.build(db.all_products())
        self.refresh()

    def refresh(self) -> None:  # what the catalogue contains right now (called again after /ingest)
        rows = db.query("SELECT DISTINCT product_type, category, brand FROM products")
        self.type_category = {r["product_type"]: r["category"] for r in rows if r["product_type"]}
        self.brands = sorted({r["brand"] for r in rows if r["brand"]})
        self.shoppers = {r["customer_id"]: r["name"] for r in db.query("SELECT customer_id, name FROM shoppers")}

    def known_type(self, t) -> str | None:  # LLM's product_type -> the real catalogue type, or None if we don't sell it
        n = norm(t).rstrip("s")
        return next((real for real in self.type_category if n and (n in norm(real) or norm(real).rstrip("s") in n)), None)

    def handle(self, message: str, session_id: str, customer_id: str) -> dict:
        t0 = time.time()
        trace = {"trace_id": uuid.uuid4().hex[:8], "session_id": session_id, "customer_id": customer_id, "message": message,
                 "input_mode": "text", "injection_flags": safety.injection_flags(message), "tools": []}
        state = memory.load(session_id, customer_id)
        own = (self.shoppers.get(customer_id) or "?").split()[0]
        others = [n for cid, n in self.shoppers.items() if cid != customer_id]
        others = [n for n in others + [n.split()[0] for n in others] if n != own]  # full names + first names

        ext, info = understand.understand(message, list(self.type_category), self.brands, others)  # STEP 1
        if understand.DEVANAGARI.search(message):  # the script is a fact -> code decides (live bug: Hinglish labelled "hi")
            ext.language = "hi"
        elif ext.language == "hi":
            ext.language = "hinglish"
        if safety.asks_about_others(message, customer_id, others):  # R9 code net on top of the LLM
            ext.intent = "others_data"
        trace.update(extraction=ext.model_dump(), llm=[{"step": "understand", **info}])

        r = self.route(ext, message, state, customer_id, trace)  # STEPS 2-5
        r["trace_id"], r["answer"] = trace["trace_id"], safety.redact(r["answer"])
        if r["answer_type"] != "clarification":
            state["clarify_streak"] = 0
        memory.save(session_id, customer_id, state)
        trace.update(answer_type=r["answer_type"], answer=r["answer"], products=[p["product_id"] for p in r["products"]],
                     latency_ms=int((time.time() - t0) * 1000))
        db.save_trace(safety.redact_deep(trace))  # R10, PII removed
        return r

    def route(self, ext, message, state, customer_id, trace) -> dict:  # STEP 2: plain if/elif on the intent
        trace["route"], lang = ext.intent, ext.language
        if ext.intent == "others_data":
            return reply("refusal", say("refuse_other", lang))
        if ext.intent == "out_of_scope":
            return reply("refusal", say("refuse_scope", lang))
        if ext.intent == "smalltalk":
            return reply("clarification", say("hello", lang), follow_up=say("hello", lang))
        if ext.intent == "my_orders":
            return self.my_orders(customer_id, lang, trace)
        if ext.intent == "compare":
            return self.compare(ext, message, state, lang, trace)
        if ext.intent == "product_fact":
            return self.product_fact(ext, message, state, lang, trace)
        return self.recommend(ext, message, state, lang, trace)

    def targets(self, ext, state) -> list[str]:  # "P-1046" or "the first two" -> product IDs
        last = state["last_results"]
        ids = ext.product_ids + [last[i - 1] for i in ext.refers_to if 0 < i <= len(last)]
        return list(dict.fromkeys(ids))  # unique, keep order

    def recommend(self, ext, message, state, lang, trace) -> dict:
        asked = None if norm(ext.product_type) in GENERIC else ext.product_type
        ext.product_type = self.known_type(asked)
        if asked and not ext.product_type:  # something we don't sell -> not_found (R6)
            return reply("not_found", say("not_sold", lang, what=asked))
        if ext.brands and not any(norm(b) in {norm(x) for x in self.brands} for b in ext.brands):  # only brands we don't carry
            return reply("not_found", say("not_sold", lang, what=" / ".join(ext.brands) + " products"))
        if ext.product_type:  # the catalogue decides the category, not the LLM (live bug: running shoes = sports)
            ext.category = self.type_category[ext.product_type]
        last = db.get_products(state["last_results"])
        f = memory.merge(state, ext, [p["price"] for p in last])

        need = "type" if not f.get("product_type") and not f.get("category") else \
            "budget" if f.get("product_type") in HIGH_CONSIDERATION and not f.get("budget_max") and not f.get("brands") else None
        if need and need not in state["asked"] and state["clarify_streak"] < 2:  # R2: ask only if needed, max 2 in a row, never twice
            state["clarify_streak"] += 1
            state["asked"].append(need)
            q = say("ask_type", lang) if need == "type" else say("ask_budget", lang, uses=HIGH_CONSIDERATION[f["product_type"]])
            return reply("clarification", q, follow_up=q)

        query = " ".join(x for x in (f.get("query"), f.get("use_case")) if x) or f.get("product_type") or ""
        trace["retrieved"] = [pid for pid, _ in self.index.search(query, 10)]  # R10: raw retrieval, before filters
        cards = tools.search_products(self.index, query, f.get("category"), f.get("product_type"), f.get("brands"),
                                      f.get("budget_max"), f.get("color"), f.get("size"), k=15,
                                      max_per_type=None if f.get("product_type") else 2)  # STEP 3
        cards = sorted(cards, key=lambda c: c["price"])[:5] if ext.cheaper and not last else cards[:5]  # "sasta" -> cheapest first
        trace["tools"].append({"tool": "search_products", "filters": f, "output": [c["product_id"] for c in cards]})
        if not cards:
            return reply("not_found", say("cheapest" if ext.cheaper and last else "no_match", lang))

        ids, budget = {c["product_id"] for c in cards}, f.get("budget_max")
        answer, reasons, report = writer.write(message, lang, "recommendation", {"filters": f, "products": [writer.lite(c) for c in cards]},
                                               ids, {c["price"] for c in cards} | {budget}, say("found", lang, n=len(cards)), count=len(cards))
        trace["llm"].append({"step": "write", **report})
        state["last_results"] = [c["product_id"] for c in cards]
        return reply("recommendation", answer, [product_out(c, reasons.get(c["product_id"]) or writer.code_reason(c, budget)) for c in cards])

    def compare(self, ext, message, state, lang, trace) -> dict:
        ids = self.targets(ext, state)
        table = tools.compare(ids if len(ids) >= 2 else state["last_results"][:2])
        if not table:
            return reply("clarification", say("ask_which", lang), follow_up=say("ask_which", lang))
        cards = table["products"]
        facts = {k: table[k] for k in ("rows", "cheapest", "best_rated", "price_gap")}
        trace["tools"].append({"tool": "compare", "output": {k: table[k] for k in ("cheapest", "best_rated", "price_gap")}})
        cheap = next(c for c in cards if c["product_id"] == table["cheapest"])
        fallback = f"{cheap['name']} ({cheap['product_id']}) is cheaper by ₹{table['price_gap']:,}."
        answer, _, report = writer.write(message, lang, "comparison verdict", facts, {c["product_id"] for c in cards},
                                         {c["price"] for c in cards} | {table["price_gap"]}, fallback)
        trace["llm"].append({"step": "write", **report})
        state["last_results"] = [c["product_id"] for c in cards]
        r = reply("comparison", answer, [product_out(c, writer.code_reason(c)) for c in cards])
        r["comparison"] = table["rows"]
        return r

    def product_fact(self, ext, message, state, lang, trace) -> dict:
        if ext.product_type and not ext.product_ids and not self.known_type(ext.product_type) and norm(ext.product_type) not in GENERIC:
            return reply("not_found", say("not_sold", lang, what=ext.product_type))  # "Is the PlayStation 5 available?"
        ids = self.targets(ext, state)
        if not ids:  # "is the blue one...?" -> the first shown product that comes in blue; never guess from nothing
            last = db.get_products(state["last_results"])
            match = [p for p in last if ext.color and ext.color in json.dumps(p["attributes"]).lower()] or (last if len(last) == 1 else [])
            ids = [match[0]["product_id"]] if match else []
        if not ids:
            return reply("clarification", say("ask_which", lang), follow_up=say("ask_which", lang))
        s = tools.check_stock(ids[0], ext.color, ext.size)
        trace["tools"].append({"tool": "check_stock", "input": [ids[0], ext.color, ext.size], "output": s})
        if not s["found"]:
            return reply("not_found", say("no_match", lang))
        c = tools.card(db.get_product(ids[0]))
        which = " ".join(x for x in (ext.color, f"size {ext.size}" if ext.size else None) if x)
        name = f"{c['name']} ({c['product_id']})"
        if not s["variant_exists"]:
            fallback = f"{name} doesn't come in {which}."
        elif s["available"]:
            fallback = f"Yes, {name}{' in ' + which if which else ''} is in stock ({s['units']} left) at ₹{c['price']:,}."
        else:
            fallback = f"Sorry, {name}{' in ' + which if which else ''} is out of stock right now."
        answer, _, report = writer.write(message, lang, "stock answer", {"stock": s, "product": writer.lite(c)}, {c["product_id"]}, {c["price"]}, fallback)
        trace["llm"].append({"step": "write", **report})
        state["last_results"] = [c["product_id"]]
        return reply("product_fact", answer, [product_out(c, fallback)])

    def my_orders(self, customer_id, lang, trace) -> dict:
        orders = tools.my_orders(customer_id)[:5]  # identity from the request, never the message
        trace["tools"].append({"tool": "my_orders", "output": [o["order_id"] for o in orders]})
        if not orders:
            return reply("product_fact", say("no_orders", lang))
        cards = [tools.card(p) for p in db.get_products([o["product_id"] for o in orders])]
        items = ", ".join(f"{o['name']} ({o['order_date']})" for o in orders)
        return reply("product_fact", say("orders", lang, items=items), [product_out(c, "you ordered this") for c in cards])
