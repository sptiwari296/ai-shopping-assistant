"""The orchestrator: one message in -> one typed reply out (+ a saved trace).

  1 extract (LLM / rules)  ->  2 memory + router (code)  ->  3 tools (code)
  ->  4 write (LLM)  ->  5 grounding check (code)  ->  reply + trace

WHY a router and not agents: the intents are a fixed list, so a plain if/elif picks the path.
That is exact, cheap, testable and can't wander off. Only steps 1 and 4 use the LLM.
"""
import json
import re
import time
import uuid
from datetime import datetime

from app import compose, db, extract, memory, safety, tools
from app.search import HybridIndex

# Things that need a budget or purpose before we can recommend well (R2).
HIGH_CONSIDERATION = {"Laptop": ["study", "office", "gaming"], "Smartphone": ["camera", "gaming", "battery life"],
                      "Headphones": ["music", "calls", "travel"], "Wireless Earbuds": ["gym", "calls", "music"],
                      "Smartwatch": ["fitness", "style", "calls"], "Camping Tent": ["2 people", "family", "solo"],
                      "Air Fryer": ["1-2 people", "family"]}
GENERIC_TYPES = {"gift", "present", "something", "item", "product", "stuff", "thing", "things"}

MSG = {
    "clarify_type": {"en": "Happy to help! What kind of product are you looking for (for example shoes, speaker, backpack)?",
                     "hi": "ज़रूर! आप किस तरह का प्रोडक्ट ढूंढ रहे हैं (जैसे जूते, स्पीकर, बैग)?",
                     "hinglish": "Zaroor! Aap kis tarah ka product dhoondh rahe hain (jaise shoes, speaker, bag)?"},
    "clarify_budget_use": {"en": "What's your budget, and will you use it mostly for {uses}?",
                           "hi": "आपका बजट कितना है, और ज़्यादातर किस काम के लिए चाहिए: {uses}?",
                           "hinglish": "Aapka budget kitna hai, aur zyada kis kaam ke liye chahiye: {uses}?"},
    "clarify_budget": {"en": "What's your budget?", "hi": "आपका बजट कितना है?", "hinglish": "Aapka budget kitna hai?"},
    "clarify_which": {"en": "Which product do you mean? You can say 'the first one' or give its ID (like P-1046).",
                      "hi": "आप कौन सा प्रोडक्ट पूछ रहे हैं? 'पहला वाला' कहें या ID दें (जैसे P-1046)।",
                      "hinglish": "Aap kaunsa product pooch rahe hain? 'pehla wala' bolein ya ID dein (jaise P-1046)."},
    "not_sold": {"en": "I couldn't find {what} in our catalogue. I can help with footwear, electronics, outdoor gear, kitchen, fashion and sports items.",
                 "hi": "हमारे कैटलॉग में {what} नहीं मिला। मैं जूते, इलेक्ट्रॉनिक्स, आउटडोर, किचन, फैशन और स्पोर्ट्स में मदद कर सकता हूँ।",
                 "hinglish": "Hamare catalogue mein {what} nahi mila. Main footwear, electronics, outdoor, kitchen, fashion aur sports mein madad kar sakta hoon."},
    "no_match": {"en": "I couldn't find a match in our catalogue for that.",
                 "hi": "इसके लिए हमारे कैटलॉग में कुछ नहीं मिला।",
                 "hinglish": "Iske liye hamare catalogue mein kuch nahi mila."},
    "already_cheapest": {"en": "Those are already the cheapest matching options in our catalogue that are in stock.",
                         "hi": "ये पहले से ही हमारे कैटलॉग के सबसे सस्ते उपलब्ध विकल्प हैं।",
                         "hinglish": "Ye pehle se hi hamare catalogue ke sabse saste available options hain."},
    "widen": {"en": " The closest is {name} at ₹{price:,} ({pid}). Shall I widen the search?",
              "hi": " सबसे नज़दीकी विकल्प {name} है, ₹{price:,} ({pid})। क्या मैं खोज बढ़ाऊँ?",
              "hinglish": " Sabse kareeb option {name} hai, ₹{price:,} ({pid}). Kya search badha doon?"},
    "refuse_other": {"en": "Sorry, I can't share another shopper's information. I can help with your own orders or find products for you.",
                     "hi": "माफ़ कीजिए, मैं किसी और ग्राहक की जानकारी नहीं दे सकता। मैं आपके अपने ऑर्डर या प्रोडक्ट खोजने में मदद कर सकता हूँ।",
                     "hinglish": "Sorry, main kisi aur shopper ki jaankari share nahi kar sakta. Aapke apne orders ya products mein madad kar sakta hoon."},
    "refuse_scope": {"en": "I can only help with shopping at NovaMart: finding, comparing and checking products.",
                     "hi": "मैं केवल NovaMart पर शॉपिंग में मदद कर सकता हूँ: प्रोडक्ट ढूंढना, तुलना करना और स्टॉक देखना।",
                     "hinglish": "Main sirf NovaMart shopping mein madad kar sakta hoon: products dhoondhna, compare karna aur stock check karna."},
    "hello": {"en": "Hi! Tell me what you're shopping for, and your budget if you have one.",
              "hi": "नमस्ते! बताइए आप क्या खरीदना चाहते हैं, और आपका बजट कितना है।",
              "hinglish": "Namaste! Bataiye aap kya khareedna chahte hain, aur budget kitna hai."},
    "no_orders": {"en": "You don't have any past orders yet.", "hi": "आपका अभी तक कोई ऑर्डर नहीं है।",
                  "hinglish": "Aapka abhi tak koi order nahi hai."},
    "orders": {"en": "Your recent orders: {items}.", "hi": "आपके हाल के ऑर्डर: {items}।", "hinglish": "Aapke recent orders: {items}."},
}


def msg(key: str, lang: str, **kw) -> str:
    return MSG[key].get(lang, MSG[key]["en"]).format(**kw)


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return s[:-1] if s.endswith("s") else s


class Brain:
    def __init__(self, index: HybridIndex | None = None):
        db.init_db()
        self.index = index or HybridIndex()
        if not self.index.ids:
            self.index.build(db.all_products())
        self.refresh_vocab()

    def refresh_vocab(self) -> None:
        with db.connect() as conn:
            self.type_category = {r[0]: r[1] for r in conn.execute("SELECT DISTINCT product_type, category FROM products") if r[0]}
            self.types = sorted(self.type_category)
            self.brands = sorted({r[0] for r in conn.execute("SELECT DISTINCT brand FROM products") if r[0]})
            self.shoppers = {r[0]: r[1] for r in conn.execute("SELECT customer_id, name FROM shoppers")}

    def known_type(self, t: str | None) -> str | None:
        """Map the LLM's product_type to a real catalogue type, or None if we don't sell it."""
        if not t:
            return None
        n = _norm(t)
        for real in self.types:
            r = _norm(real)
            if n == r or n in r or r in n:
                return real
        return None

    # ------------------------------------------------------------------ main entry
    def handle(self, message: str, session_id: str, customer_id: str) -> dict:
        t0 = time.time()
        trace = {"trace_id": uuid.uuid4().hex[:8], "session_id": session_id, "customer_id": customer_id,
                 "message": message, "tools": [], "llm": [], "input_mode": "text",
                 # R8: instructions inside the message are noted, never obeyed (they only ever become search filters)
                 "safety": {"injection_in_message": safety.injection_score(message)}}
        state = memory.load(session_id, customer_id)
        own_name = (self.shoppers.get(customer_id) or "").lower()
        other_names = [n for cid, n in self.shoppers.items() if cid != customer_id and n]
        # first names too ("what did Priya buy?"), except our own first name
        other_names += [n.split()[0] for n in other_names if n.split()[0].lower() not in own_name.split()]

        ext, info = extract.extract(message, self.types, self.brands, other_names)
        trace["extraction"] = ext.model_dump()
        trace["llm"].append({"step": "extract", **info})
        # Script is an objective fact -> code decides it (live bug: Roman-script Hinglish labelled "hi" -> Devanagari reply)
        has_devanagari = bool(re.search(r"[ऀ-ॿ]", message))
        if ext.language == "hi" and not has_devanagari:
            ext.language = "hinglish"
        elif has_devanagari:
            ext.language = "hi"
        lang = ext.language
        state["language"] = lang

        # Code safety net on top of the LLM (R9): another shopper's id or name + order words -> others_data
        if re.search(r"\bC\d{3}\b", message) and not re.search(rf"\b{customer_id}\b", message):
            ext.intent = "others_data"
        elif any(re.search(rf"\b{re.escape(n.lower())}\b", message.lower()) for n in other_names) and \
                re.search(r"\b(buy|bought|order|orders|purchase|kharida|liya)\b", message.lower()):
            ext.intent = "others_data"
        elif re.search(r"\b(friend|dost|neighbou?r|padosi|brother|sister|bhai|behen|wife|husband|someone|another customer|other customer)\b",
                       message.lower()) and re.search(r"\b(bought|buy|order|ordered|orders|purchase|kharida|kiya|liya)\b", message.lower()):
            # someone else's purchases, even if the name is not in our shopper list (eval P3 failed in rules mode)
            ext.intent = "others_data"

        reply = self.route(ext, message, state, customer_id, trace)
        reply["trace_id"] = trace["trace_id"]
        reply["answer"] = safety.redact(reply["answer"])  # never echo a phone number / email back
        if reply["answer_type"] != "clarification":
            state["clarify_streak"] = 0
        memory.save(session_id, customer_id, state)

        trace.update(answer_type=reply["answer_type"], answer=reply["answer"],
                     products=[p["product_id"] for p in reply["products"]], filters=state.get("filters"),
                     latency_ms=int((time.time() - t0) * 1000), created_at=datetime.now().isoformat(timespec="seconds"))
        trace = safety.redact_deep(trace)  # PII never reaches the saved record
        with db.connect() as conn:
            conn.execute("INSERT INTO traces VALUES (?,?,?,?,?)", (trace["trace_id"], session_id, customer_id,
                         trace["created_at"], json.dumps(trace, ensure_ascii=False, default=str)))
        return reply

    # ------------------------------------------------------------------ router
    def route(self, ext, message, state, customer_id, trace) -> dict:
        lang = ext.language
        trace["route"] = ext.intent
        if ext.intent == "others_data":
            return reply_("refusal", msg("refuse_other", lang))
        if ext.intent == "out_of_scope":
            # "Do you sell electric cars?" is a product question (-> not_found), not off-topic (live bug).
            rules = extract.rule_extract(message, self.brands, [])
            if rules.product_type and re.search(r"\b(sell|have|buy|stock|available|milta|milega|chahiye)\b", message.lower()):
                ext.intent, ext.product_type = "recommend", rules.product_type
                trace["route"] = "out_of_scope->product_question"
                return self.recommend(ext, message, state, lang, trace)
            return reply_("refusal", msg("refuse_scope", lang))
        if ext.intent == "smalltalk":
            return reply_("clarification", msg("hello", lang), follow_up=msg("hello", lang))
        if ext.intent == "my_orders":
            return self.my_orders(customer_id, lang, trace)
        if ext.intent == "compare":
            return self.compare(ext, message, state, lang, trace)
        if ext.intent == "product_fact":
            return self.product_fact(ext, message, state, lang, trace)
        return self.recommend(ext, message, state, lang, trace)

    def _targets(self, ext, state) -> list[str]:
        last = state.get("last_results", [])
        ids = list(ext.product_ids)
        ids += [last[i - 1] for i in ext.refers_to if 0 < i <= len(last)]
        return list(dict.fromkeys(ids))  # unique, keep order

    # ------------------------------------------------------------------ recommend
    def recommend(self, ext, message, state, lang, trace) -> dict:
        last_prices = [p["price"] for p in db.get_products(state.get("last_results", []))]
        if ext.product_type and _norm(ext.product_type) in {_norm(g) for g in GENERIC_TYPES}:
            ext.product_type = None
        asked_type = ext.product_type
        if asked_type and not self.known_type(asked_type):
            trace["route"] = "not_sold"
            return reply_("not_found", msg("not_sold", lang, what=asked_type))
        ext.product_type = self.known_type(asked_type)
        if ext.product_type:
            # The catalogue, not the LLM, decides which category a type belongs to.
            # (Live bug: LLM said running shoes = sports_fitness -> category filter removed everything.)
            ext.category = self.type_category[ext.product_type]

        merged = memory.merge(state, ext, last_prices)
        f = merged["filters"]
        if merged["new_topic"]:
            state["clarify_streak"], state["asked"] = 0, []
        state["filters"] = f
        trace["merged_filters"] = f

        # R2: ask only when needed, at most 2 in a row, never the same thing twice
        need = self.missing_info(f)
        need = [n for n in need if n not in state.get("asked", [])]
        if need and state.get("clarify_streak", 0) < 2:
            state["clarify_streak"] = state.get("clarify_streak", 0) + 1
            state["asked"] = state.get("asked", []) + need
            if "type" in need:
                q = msg("clarify_type", lang)
            elif "use" in need:
                q = msg("clarify_budget_use", lang, uses=", ".join(HIGH_CONSIDERATION[f["product_type"]]))
            else:
                q = msg("clarify_budget", lang)
            trace["route"] = f"clarify:{','.join(need)}"
            return reply_("clarification", q, follow_up=q)

        query = " ".join(x for x in [f.get("search_query"), f.get("use_case")] if x) or (f.get("product_type") or "")
        args = dict(query=query, category=f.get("category"), product_type=f.get("product_type"), brands=f.get("brands"),
                    budget_min=f.get("budget_min"), budget_max=f.get("budget_max"), color=f.get("color"), size=f.get("size"),
                    k=5, max_per_type=None if f.get("product_type") else 2)
        # R10: record what retrieval returned BEFORE filters (so a judge can see why items were kept or dropped)
        trace["retrieved"] = [{"product_id": pid, "rrf": round(s, 4)} for pid, s in self.index.search(query, 10)]
        cards = tools.search_products(self.index, **args)
        trace["tools"].append({"tool": "search_products", "input": args, "output": [c["product_id"] for c in cards]})

        if not cards:
            relaxed = dict(args, budget_max=None, budget_min=None, color=None, size=None, k=20)
            near = tools.search_products(self.index, **relaxed)
            trace["tools"].append({"tool": "search_products(relaxed)", "input": relaxed, "output": [c["product_id"] for c in near]})
            if ext.cheaper and state.get("last_results"):
                # nothing cheaper exists: say so instead of suggesting something pricier
                return reply_("not_found", msg("already_cheapest", lang))
            text = msg("no_match", lang)
            if near:
                # budget was the blocker -> suggest the CHEAPEST match, not the best fit
                best = min(near, key=lambda c: c["price"]) if f.get("budget_max") else near[0]
                text += msg("widen", lang, name=best["name"], price=best["price"], pid=best["product_id"])
            return reply_("not_found", text)

        if ext.cheaper and not last_prices:
            # "sasta speaker" with no earlier results: shopper explicitly wants low price -> sort by price
            more = tools.search_products(self.index, **dict(args, k=15))
            cards = sorted(more, key=lambda c: c["price"])[:5]
            trace["tools"].append({"tool": "sort_by_price", "input": "cheaper requested", "output": [c["product_id"] for c in cards]})

        answer, reasons, report = compose.write_recommendation(message, lang, cards, f)
        trace["llm"].append({"step": "compose", **report})
        state["last_results"] = [c["product_id"] for c in cards]
        products = [product_out(c, reasons[c["product_id"]]) for c in cards]
        return reply_("recommendation", answer, products=products, sources=sources_(cards))

    def missing_info(self, f: dict) -> list[str]:
        if not f.get("product_type") and not f.get("category"):
            return ["type"]
        if f.get("product_type") in HIGH_CONSIDERATION and not f.get("budget_max") and not f.get("brands"):
            return ["budget", "use"] if not f.get("use_case") else ["budget"]
        return []

    # ------------------------------------------------------------------ compare
    def compare(self, ext, message, state, lang, trace) -> dict:
        ids = self._targets(ext, state)
        if len(ids) < 2:
            ids = state.get("last_results", [])[:2]
        if len(ids) < 2:
            return reply_("clarification", msg("clarify_which", lang), follow_up=msg("clarify_which", lang))
        table = tools.compare(ids)
        trace["tools"].append({"tool": "compare", "input": ids, "output": table.get("facts")})
        if table.get("error"):
            return reply_("not_found", msg("no_match", lang))
        cards = [tools.get_product(i) for i in ids]
        fa = table["facts"]
        cheap = next(c for c in cards if c["product_id"] == fa["cheapest"])
        fallback = f"{cheap['name']} ({cheap['product_id']}) is cheaper by ₹{fa['price_gap']:,}."
        if fa.get("best_rated"):
            best = next(c for c in cards if c["product_id"] == fa["best_rated"])
            fallback += f" Best rated: {best['name']} ({best['rating']})."
        nums = compose.allowed_numbers(cards, [fa["price_gap"]])
        answer, report = compose.write_simple(message, lang, "comparison", {"table": table["rows"], "facts": fa,
                                              "products": [compose._lite(c) for c in cards]}, fallback, nums, set(ids))
        trace["llm"].append({"step": "compose", **report})
        state["last_results"] = ids
        products = [product_out(c, compose.code_reason(c, {}, lang)) for c in cards]
        r = reply_("comparison", answer, products=products, sources=sources_(cards))
        r["comparison"] = table["rows"]
        return r

    # ------------------------------------------------------------------ product fact
    def product_fact(self, ext, message, state, lang, trace) -> dict:
        ids = self._targets(ext, state)
        if not ids:
            last = db.get_products(state.get("last_results", []))
            if ext.color:  # "is the blue one in size 9?" -> every shown product that comes in blue
                ids = [p["product_id"] for p in last if ext.color in json.dumps(p.get("attributes", {})).lower()][:3]
            elif len(last) == 1:
                ids = [last[0]["product_id"]]
        if len(ids) > 1 and not ext.product_ids:
            return self.multi_stock(ids, ext, lang, state, trace)
        if not ids:
            # Only search when a product is actually named ("is the JBL Go speaker in stock?").
            # "the blue one" with nothing shown before -> ask, never guess (live bug: it guessed jeans).
            if self.known_type(ext.product_type) or ext.brands:
                found = tools.search_products(self.index, ext.search_query or ext.product_type, brands=ext.brands,
                                              in_stock_only=False, k=1)
                ids = [found[0]["product_id"]] if found else []
        if not ids:
            return reply_("clarification", msg("clarify_which", lang), follow_up=msg("clarify_which", lang))
        pid = ids[0]
        stock = tools.check_stock(pid, ext.color, ext.size)
        trace["tools"].append({"tool": "check_stock", "input": {"product_id": pid, "color": ext.color, "size": ext.size},
                               "output": stock})
        if not stock.get("found"):
            return reply_("not_found", msg("no_match", lang))
        card = tools.get_product(pid)
        which = " ".join(x for x in [ext.color, f"size {ext.size}" if ext.size else None] if x)
        if stock.get("variant_exists") is False:
            fallback = f"{card['name']} ({pid}) doesn't come in {which}."
        elif stock["available"]:
            fallback = f"Yes, {card['name']} ({pid}){' in ' + which if which else ''} is in stock ({stock['units']} left) at ₹{card['price']:,}."
        else:
            fallback = f"Sorry, {card['name']} ({pid}){' in ' + which if which else ''} is out of stock right now."
        nums = compose.allowed_numbers([card], [stock.get("units")])
        answer, report = compose.write_simple(message, lang, "product_fact", {"stock_check": stock, "product": compose._lite(card)},
                                              fallback, nums, {pid})
        trace["llm"].append({"step": "compose", **report})
        state["last_results"] = [pid]
        return reply_("product_fact", answer, products=[product_out(card, fallback)], sources=sources_([card], ["price", "stock"]))

    def multi_stock(self, ids, ext, lang, state, trace) -> dict:
        """'The blue one' matched several shown products: check each instead of guessing one."""
        lines, outs, cards = [], [], []
        which = " ".join(x for x in [ext.color, f"size {ext.size}" if ext.size else None] if x)
        for pid in ids:
            s = tools.check_stock(pid, ext.color, ext.size)
            trace["tools"].append({"tool": "check_stock", "input": {"product_id": pid, "color": ext.color, "size": ext.size}, "output": s})
            c = tools.get_product(pid)
            status = f"in stock ({s['units']} left)" if s.get("available") else ("not made in " + which if s.get("variant_exists") is False else "out of stock")
            lines.append(f"{c['name']} ({pid}): {status}")
            outs.append(product_out(c, f"{which}: {status}"))
            cards.append(c)
        state["last_results"] = ids
        return reply_("product_fact", f"Several items you saw come in {ext.color}. For {which}: " + "; ".join(lines) + ".",
                      products=outs, sources=sources_(cards, ["stock"]))

    # ------------------------------------------------------------------ my orders
    def my_orders(self, customer_id, lang, trace) -> dict:
        orders = tools.my_orders(customer_id)  # customer_id from the request, never the message
        trace["tools"].append({"tool": "my_orders", "input": {"customer_id": customer_id}, "output": [o["order_id"] for o in orders]})
        if not orders:
            return reply_("product_fact", msg("no_orders", lang))
        items = ", ".join(f"{o['name']} ({o['order_date']})" for o in orders[:5])
        cards = [tools.get_product(o["product_id"]) for o in orders[:5]]
        return reply_("product_fact", msg("orders", lang, items=items),
                      products=[product_out(c, f"ordered on {o['order_date']}") for c, o in zip(cards, orders)],
                      sources=sources_(cards, ["name"]))


def product_out(card: dict, reason: str) -> dict:
    return {"product_id": card["product_id"], "name": card["name"], "price": card["price"],
            "in_stock": card["in_stock"], "reason": reason}


def sources_(cards: list[dict], fields: list[str] | None = None) -> list[dict]:
    return [{"product_id": c["product_id"], "fields": fields or ["price", "stock", "description"]} for c in cards]


def reply_(answer_type: str, answer: str, products=None, sources=None, follow_up=None) -> dict:
    return {"trace_id": None, "answer_type": answer_type, "answer": answer, "products": products or [],
            "sources": sources or [], "follow_up_question": follow_up}
