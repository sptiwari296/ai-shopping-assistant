"""The orchestrator: one message in -> one typed reply out (+ a saved trace).

  1 extract (LLM / rules)  ->  2 memory + router (code)  ->  3 tools (code)
  ->  4 write (LLM)  ->  5 grounding check (code)  ->  reply + trace

WHY a router and not agents: the intents are a fixed list, so a plain if/elif picks the path.
That is exact, cheap, testable and can't wander off. Only steps 1 and 4 use the LLM.
"""
import json  # trace is saved as JSON; colour search inside attributes
import re  # code safety nets (IDs, names, Devanagari)
import time  # latency of the whole message
import uuid  # random trace IDs
from datetime import datetime  # trace timestamp

from app import compose, db, extract, memory, safety, tools  # all the steps
from app.search import HybridIndex  # the retrieval index

# Things that need a budget or purpose before we can recommend well (R2).
HIGH_CONSIDERATION = {"Laptop": ["study", "office", "gaming"], "Smartphone": ["camera", "gaming", "battery life"],  # type -> use options to offer
                      "Headphones": ["music", "calls", "travel"], "Wireless Earbuds": ["gym", "calls", "music"],
                      "Smartwatch": ["fitness", "style", "calls"], "Camping Tent": ["2 people", "family", "solo"],
                      "Air Fryer": ["1-2 people", "family"]}
GENERIC_TYPES = {"gift", "present", "something", "item", "product", "stuff", "thing", "things"}  # not real product types -> treat as "no type"

MSG = {  # every fixed reply, in 3 languages (code-written = always correct)
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


def msg(key: str, lang: str, **kw) -> str:  # pick a fixed reply in the shopper's language and fill its {placeholders}
    return MSG[key].get(lang, MSG[key]["en"]).format(**kw)  # unknown language -> English


def _norm(s: str) -> str:  # "Running Shoes" -> "runningshoe" (compare types/brands loosely)
    s = re.sub(r"[^a-z0-9]", "", (s or "").lower())  # lowercase, letters/digits only
    return s[:-1] if s.endswith("s") else s  # drop a trailing "s" so singular == plural


class Brain:
    def __init__(self, index: HybridIndex | None = None):
        db.init_db()  # tables exist + seed data loaded
        self.index = index or HybridIndex()  # reuse an index if given (tests), else create one
        if not self.index.ids:  # empty index -> build it from the catalogue (~10 s)
            self.index.build(db.all_products())
        self.refresh_vocab()  # learn the current types / brands / shopper names

    def refresh_vocab(self) -> None:  # re-read what the catalogue contains (called again after /ingest)
        with db.connect() as conn:
            self.type_category = {r[0]: r[1] for r in conn.execute("SELECT DISTINCT product_type, category FROM products") if r[0]}  # type -> category
            self.types = sorted(self.type_category)  # list of real product types (sent to the LLM prompt)
            self.brands = sorted({r[0] for r in conn.execute("SELECT DISTINCT brand FROM products") if r[0]})  # brands we carry (None skipped)
            self.shoppers = {r[0]: r[1] for r in conn.execute("SELECT customer_id, name FROM shoppers")}  # id -> name (for the privacy net)

    def known_type(self, t: str | None) -> str | None:
        """Map the LLM's product_type to a real catalogue type, or None if we don't sell it."""
        if not t:
            return None
        n = _norm(t)  # e.g. "bluetoothspeaker"
        for real in self.types:
            r = _norm(real)
            if n == r or n in r or r in n:  # exact, or one contains the other ("speaker" in "bluetoothspeaker")
                return real  # the official catalogue spelling
        return None  # we don't sell it

    # ------------------------------------------------------------------ main entry
    def handle(self, message: str, session_id: str, customer_id: str) -> dict:  # called by POST /chat
        t0 = time.time()  # start the latency clock
        trace = {"trace_id": uuid.uuid4().hex[:8], "session_id": session_id, "customer_id": customer_id,  # 8-char random ID
                 "message": message, "tools": [], "llm": [], "input_mode": "text",  # filled in as we go
                 # R8: instructions inside the message are noted, never obeyed (they only ever become search filters)
                 "safety": {"injection_in_message": safety.injection_score(message)}}
        state = memory.load(session_id, customer_id)  # this conversation's memory (fresh if new or another shopper's)
        own_name = (self.shoppers.get(customer_id) or "").lower()  # the asking shopper's own name
        other_names = [n for cid, n in self.shoppers.items() if cid != customer_id and n]  # everyone else's full names
        # first names too ("what did Priya buy?"), except our own first name
        other_names += [n.split()[0] for n in other_names if n.split()[0].lower() not in own_name.split()]

        ext, info = extract.extract(message, self.types, self.brands, other_names)  # STEP 1: understand (LLM, or rules if it fails)
        trace["extraction"] = ext.model_dump()  # what was understood
        trace["llm"].append({"step": "extract", **info})  # llm or rules, latency, tokens
        # Script is an objective fact -> code decides it (live bug: Roman-script Hinglish labelled "hi" -> Devanagari reply)
        has_devanagari = bool(re.search(r"[ऀ-ॿ]", message))  # any Devanagari character?
        if ext.language == "hi" and not has_devanagari:  # LLM said Hindi but text is Roman -> Hinglish
            ext.language = "hinglish"
        elif has_devanagari:  # Devanagari present -> Hindi, whatever the LLM said
            ext.language = "hi"
        lang = ext.language
        state["language"] = lang  # remember for the session

        # Code safety net on top of the LLM (R9): another shopper's id or name + order words -> others_data
        if re.search(r"\bC\d{3}\b", message) and not re.search(rf"\b{customer_id}\b", message):  # mentions a customer ID that isn't theirs
            ext.intent = "others_data"
        # another shopper's name AND an order/buy word
        elif any(re.search(rf"\b{re.escape(n.lower())}\b", message.lower()) for n in other_names) and \
                re.search(r"\b(buy|bought|order|orders|purchase|kharida|liya)\b", message.lower()):
            ext.intent = "others_data"
        # a relation word (friend/dost/...) AND an order/buy word
        elif re.search(r"\b(friend|dost|neighbou?r|padosi|brother|sister|bhai|behen|wife|husband|someone|another customer|other customer)\b",
                       message.lower()) and re.search(r"\b(bought|buy|order|ordered|orders|purchase|kharida|kiya|liya)\b", message.lower()):
            # someone else's purchases, even if the name is not in our shopper list (eval P3 failed in rules mode)
            ext.intent = "others_data"

        reply = self.route(ext, message, state, customer_id, trace)  # STEP 2-5: route -> tools -> write -> check
        reply["trace_id"] = trace["trace_id"]  # so the shopper/judge can replay it
        reply["answer"] = safety.redact(reply["answer"])  # never echo a phone number / email back
        if reply["answer_type"] != "clarification":  # a real answer ends the run of questions
            state["clarify_streak"] = 0
        memory.save(session_id, customer_id, state)  # persist memory for the next turn

        trace.update(answer_type=reply["answer_type"], answer=reply["answer"],  # finish the audit record
                     products=[p["product_id"] for p in reply["products"]], filters=state.get("filters"),
                     latency_ms=int((time.time() - t0) * 1000), created_at=datetime.now().isoformat(timespec="seconds"))
        trace = safety.redact_deep(trace)  # PII never reaches the saved record
        with db.connect() as conn:
            conn.execute("INSERT INTO traces VALUES (?,?,?,?,?)", (trace["trace_id"], session_id, customer_id,  # R10: save the trace
                         trace["created_at"], json.dumps(trace, ensure_ascii=False, default=str)))  # default=str: anything odd -> text
        return reply

    # ------------------------------------------------------------------ router
    def route(self, ext, message, state, customer_id, trace) -> dict:  # STEP 2: plain if/elif on the intent
        lang = ext.language
        trace["route"] = ext.intent  # record the chosen path
        if ext.intent == "others_data":  # privacy -> refuse, show nothing
            return reply_("refusal", msg("refuse_other", lang))
        if ext.intent == "out_of_scope":
            # "Do you sell electric cars?" is a product question (-> not_found), not off-topic (live bug).
            rules = extract.rule_extract(message, self.brands, [])  # second opinion from the rules
            if rules.product_type and re.search(r"\b(sell|have|buy|stock|available|milta|milega|chahiye)\b", message.lower()):
                ext.intent, ext.product_type = "recommend", rules.product_type  # treat as a product request
                trace["route"] = "out_of_scope->product_question"
                return self.recommend(ext, message, state, lang, trace)  # -> will answer not_found
            return reply_("refusal", msg("refuse_scope", lang))  # truly off-topic
        if ext.intent == "smalltalk":  # "hi" -> greet and ask what they need
            return reply_("clarification", msg("hello", lang), follow_up=msg("hello", lang))
        if ext.intent == "my_orders":  # own history
            return self.my_orders(customer_id, lang, trace)
        if ext.intent == "compare":
            return self.compare(ext, message, state, lang, trace)
        if ext.intent == "product_fact":  # stock / price / attribute of one product
            return self.product_fact(ext, message, state, lang, trace)
        return self.recommend(ext, message, state, lang, trace)  # default path

    def _targets(self, ext, state) -> list[str]:  # which products the shopper is pointing at
        last = state.get("last_results", [])  # IDs shown last turn, in order
        ids = list(ext.product_ids)  # explicit IDs first ("P-1046")
        ids += [last[i - 1] for i in ext.refers_to if 0 < i <= len(last)]  # "first two" -> last[0], last[1] (positions are 1-based)
        return list(dict.fromkeys(ids))  # unique, keep order

    # ------------------------------------------------------------------ recommend
    def recommend(self, ext, message, state, lang, trace) -> dict:
        last_prices = [p["price"] for p in db.get_products(state.get("last_results", []))]  # for "cheaper"
        if ext.product_type and _norm(ext.product_type) in {_norm(g) for g in GENERIC_TYPES}:  # "gift" is not a type
            ext.product_type = None
        asked_type = ext.product_type
        if asked_type and not self.known_type(asked_type):  # asked for something we don't sell ("electric car")
            trace["route"] = "not_sold"
            return reply_("not_found", msg("not_sold", lang, what=asked_type))
        unknown_brands = [b for b in ext.brands if _norm(b) not in {_norm(x) for x in self.brands}]  # brands we don't carry
        if unknown_brands and not set(ext.brands) - set(unknown_brands):  # ALL asked brands are unknown
            # every brand asked for is one we don't carry ("Apple smartphones", eval N3)
            trace["route"] = "brand_not_sold"
            return reply_("not_found", msg("not_sold", lang, what=f"{', '.join(unknown_brands)} products"))
        ext.product_type = self.known_type(asked_type)  # official spelling of the type (or None)
        if ext.product_type:
            # The catalogue, not the LLM, decides which category a type belongs to.
            # (Live bug: LLM said running shoes = sports_fitness -> category filter removed everything.)
            ext.category = self.type_category[ext.product_type]

        merged = memory.merge(state, ext, last_prices)  # STEP 2a: combine with remembered filters
        f = merged["filters"]  # the active search
        if merged["new_topic"]:  # new product type -> forget old questions
            state["clarify_streak"], state["asked"] = 0, []
        state["filters"] = f  # remember
        trace["merged_filters"] = f

        # R2: ask only when needed, at most 2 in a row, never the same thing twice
        need = self.missing_info(f)  # e.g. ["budget", "use"]
        need = [n for n in need if n not in state.get("asked", [])]  # drop things we already asked
        if need and state.get("clarify_streak", 0) < 2:  # still allowed to ask
            state["clarify_streak"] = state.get("clarify_streak", 0) + 1  # count this question
            state["asked"] = state.get("asked", []) + need  # never ask these again
            if "type" in need:  # no idea what they want
                q = msg("clarify_type", lang)
            elif "use" in need:  # e.g. laptop: budget + study/office/gaming
                q = msg("clarify_budget_use", lang, uses=", ".join(HIGH_CONSIDERATION[f["product_type"]]))
            else:  # only budget missing
                q = msg("clarify_budget", lang)
            trace["route"] = f"clarify:{','.join(need)}"
            return reply_("clarification", q, follow_up=q)

        query = " ".join(x for x in [f.get("search_query"), f.get("use_case")] if x) or (f.get("product_type") or "")  # text to search with
        args = dict(query=query, category=f.get("category"), product_type=f.get("product_type"), brands=f.get("brands"),  # all filters for the tool
                    budget_min=f.get("budget_min"), budget_max=f.get("budget_max"), color=f.get("color"), size=f.get("size"),
                    k=5, max_per_type=None if f.get("product_type") else 2)  # 5 results; variety only for open questions
        # R10: record what retrieval returned BEFORE filters (so a judge can see why items were kept or dropped)
        trace["retrieved"] = [{"product_id": pid, "rrf": round(s, 4)} for pid, s in self.index.search(query, 10)]
        cards = tools.search_products(self.index, **args)  # STEP 3: retrieve + exact filters
        trace["tools"].append({"tool": "search_products", "input": args, "output": [c["product_id"] for c in cards]})

        if not cards:  # nothing passes the filters -> not_found (R6), never invent
            relaxed = dict(args, budget_max=None, budget_min=None, color=None, size=None, k=20)  # same search without the strict filters
            near = tools.search_products(self.index, **relaxed)
            trace["tools"].append({"tool": "search_products(relaxed)", "input": relaxed, "output": [c["product_id"] for c in near]})
            if ext.cheaper and state.get("last_results"):
                # nothing cheaper exists: say so instead of suggesting something pricier
                return reply_("not_found", msg("already_cheapest", lang))
            text = msg("no_match", lang)
            if near:
                # budget was the blocker -> suggest the CHEAPEST match, not the best fit
                best = min(near, key=lambda c: c["price"]) if f.get("budget_max") else near[0]
                text += msg("widen", lang, name=best["name"], price=best["price"], pid=best["product_id"])  # "shall I widen the search?"
            return reply_("not_found", text)

        if ext.cheaper and not last_prices:
            # "sasta speaker" with no earlier results: shopper explicitly wants low price -> sort by price
            more = tools.search_products(self.index, **dict(args, k=15))  # wider pool
            cards = sorted(more, key=lambda c: c["price"])[:5]  # cheapest 5
            trace["tools"].append({"tool": "sort_by_price", "input": "cheaper requested", "output": [c["product_id"] for c in cards]})

        answer, reasons, report = compose.write_recommendation(message, lang, cards, f)  # STEP 4 + 5: LLM writes, code checks
        trace["llm"].append({"step": "compose", **report})  # which writer, any grounding problems
        state["last_results"] = [c["product_id"] for c in cards]  # remember for "first two" / "cheaper"
        products = [product_out(c, reasons[c["product_id"]]) for c in cards]  # brief's product shape
        return reply_("recommendation", answer, products=products, sources=sources_(cards))

    def missing_info(self, f: dict) -> list[str]:  # what we must ask before recommending
        if not f.get("product_type") and not f.get("category"):  # no idea what they want
            return ["type"]
        if f.get("product_type") in HIGH_CONSIDERATION and not f.get("budget_max") and not f.get("brands"):  # pricey item, no budget/brand
            return ["budget", "use"] if not f.get("use_case") else ["budget"]
        return []  # enough info

    # ------------------------------------------------------------------ compare
    def compare(self, ext, message, state, lang, trace) -> dict:
        ids = self._targets(ext, state)  # explicit IDs / "first two"
        if len(ids) < 2:  # "compare them" -> default to the first two shown
            ids = state.get("last_results", [])[:2]
        if len(ids) < 2:  # still nothing to compare -> ask
            return reply_("clarification", msg("clarify_which", lang), follow_up=msg("clarify_which", lang))
        table = tools.compare(ids)  # side-by-side table + facts, all from code
        trace["tools"].append({"tool": "compare", "input": ids, "output": table.get("facts")})
        if table.get("error"):  # unknown IDs
            return reply_("not_found", msg("no_match", lang))
        cards = [tools.get_product(i) for i in ids]
        fa = table["facts"]  # cheapest, best_rated, price_gap
        cheap = next(c for c in cards if c["product_id"] == fa["cheapest"])  # the cheapest product's card
        fallback = f"{cheap['name']} ({cheap['product_id']}) is cheaper by ₹{fa['price_gap']:,}."  # code verdict (always true)
        if fa.get("best_rated"):
            best = next(c for c in cards if c["product_id"] == fa["best_rated"])
            fallback += f" Best rated: {best['name']} ({best['rating']})."
        nums = compose.allowed_numbers(cards, [fa["price_gap"]])  # prices + the gap may be mentioned
        answer, report = compose.write_simple(message, lang, "comparison", {"table": table["rows"], "facts": fa,  # LLM words the verdict, code checks it
                                              "products": [compose._lite(c) for c in cards]}, fallback, nums, set(ids))
        trace["llm"].append({"step": "compose", **report})
        state["last_results"] = ids
        products = [product_out(c, compose.code_reason(c, {}, lang)) for c in cards]
        r = reply_("comparison", answer, products=products, sources=sources_(cards))
        r["comparison"] = table["rows"]  # the table, for the UI
        return r

    # ------------------------------------------------------------------ product fact
    def product_fact(self, ext, message, state, lang, trace) -> dict:
        # "Is the PlayStation 5 available?" -> a thing we don't sell is not_found, not "which product?" (eval N4)
        if ext.product_type and not ext.product_ids and not ext.refers_to and not self.known_type(ext.product_type) \
                and _norm(ext.product_type) not in {_norm(g) for g in GENERIC_TYPES}:
            trace["route"] = "not_sold"
            return reply_("not_found", msg("not_sold", lang, what=ext.product_type))
        ids = self._targets(ext, state)  # explicit ID or "the first one"
        if not ids:
            last = db.get_products(state.get("last_results", []))  # products shown last turn
            if ext.color:  # "is the blue one in size 9?" -> every shown product that comes in blue
                ids = [p["product_id"] for p in last if ext.color in json.dumps(p.get("attributes", {})).lower()][:3]
            elif len(last) == 1:  # only one product shown -> "it" means that one
                ids = [last[0]["product_id"]]
        if len(ids) > 1 and not ext.product_ids:  # ambiguous -> check them all instead of guessing
            return self.multi_stock(ids, ext, lang, state, trace)
        if not ids:
            # Only search when a product is actually named ("is the JBL Go speaker in stock?").
            # "the blue one" with nothing shown before -> ask, never guess (live bug: it guessed jeans).
            if self.known_type(ext.product_type) or ext.brands:
                found = tools.search_products(self.index, ext.search_query or ext.product_type, brands=ext.brands,
                                              in_stock_only=False, k=1)  # include out-of-stock: they asked about stock
                ids = [found[0]["product_id"]] if found else []
        if not ids:  # still don't know which product -> ask
            return reply_("clarification", msg("clarify_which", lang), follow_up=msg("clarify_which", lang))
        pid = ids[0]
        stock = tools.check_stock(pid, ext.color, ext.size)  # exact stock from the database
        trace["tools"].append({"tool": "check_stock", "input": {"product_id": pid, "color": ext.color, "size": ext.size},
                               "output": stock})
        if not stock.get("found"):  # unknown ID
            return reply_("not_found", msg("no_match", lang))
        card = tools.get_product(pid)
        which = " ".join(x for x in [ext.color, f"size {ext.size}" if ext.size else None] if x)  # e.g. "blue size 9"
        if stock.get("variant_exists") is False:  # never made in that colour/size
            fallback = f"{card['name']} ({pid}) doesn't come in {which}."
        elif stock["available"]:  # in stock -> say how many left + price
            fallback = f"Yes, {card['name']} ({pid}){' in ' + which if which else ''} is in stock ({stock['units']} left) at ₹{card['price']:,}."
        else:  # exists but sold out
            fallback = f"Sorry, {card['name']} ({pid}){' in ' + which if which else ''} is out of stock right now."
        nums = compose.allowed_numbers([card], [])  # only the real price may be mentioned
        answer, report = compose.write_simple(message, lang, "product_fact", {"stock_check": stock, "product": compose._lite(card)},
                                              fallback, nums, {pid})  # LLM words it, code checks it
        trace["llm"].append({"step": "compose", **report})
        state["last_results"] = [pid]
        return reply_("product_fact", answer, products=[product_out(card, fallback)], sources=sources_([card], ["price", "stock"]))

    def multi_stock(self, ids, ext, lang, state, trace) -> dict:
        """'The blue one' matched several shown products: check each instead of guessing one."""
        lines, outs, cards = [], [], []
        which = " ".join(x for x in [ext.color, f"size {ext.size}" if ext.size else None] if x)
        for pid in ids:  # check every candidate
            s = tools.check_stock(pid, ext.color, ext.size)
            trace["tools"].append({"tool": "check_stock", "input": {"product_id": pid, "color": ext.color, "size": ext.size}, "output": s})
            c = tools.get_product(pid)
            status = f"in stock ({s['units']} left)" if s.get("available") else ("not made in " + which if s.get("variant_exists") is False else "out of stock")  # 3 possible states
            lines.append(f"{c['name']} ({pid}): {status}")
            outs.append(product_out(c, f"{which}: {status}"))
            cards.append(c)
        state["last_results"] = ids
        return reply_("product_fact", f"Several items you saw come in {ext.color}. For {which}: " + "; ".join(lines) + ".",  # code-written, always true
                      products=outs, sources=sources_(cards, ["stock"]))

    # ------------------------------------------------------------------ my orders
    def my_orders(self, customer_id, lang, trace) -> dict:
        orders = tools.my_orders(customer_id)  # customer_id from the request, never the message
        trace["tools"].append({"tool": "my_orders", "input": {"customer_id": customer_id}, "output": [o["order_id"] for o in orders]})
        if not orders:  # e.g. C001, the brand-new shopper
            return reply_("product_fact", msg("no_orders", lang))
        items = ", ".join(f"{o['name']} ({o['order_date']})" for o in orders[:5])  # newest 5
        cards = [tools.get_product(o["product_id"]) for o in orders[:5]]
        return reply_("product_fact", msg("orders", lang, items=items),
                      products=[product_out(c, f"ordered on {o['order_date']}") for c, o in zip(cards, orders)],  # pair each card with its order
                      sources=sources_(cards, ["name"]))


def product_out(card: dict, reason: str) -> dict:  # the product shape from the brief's §6.1 JSON
    return {"product_id": card["product_id"], "name": card["name"], "price": card["price"],
            "in_stock": card["in_stock"], "reason": reason}


def sources_(cards: list[dict], fields: list[str] | None = None) -> list[dict]:  # which catalogue fields each answer relied on
    return [{"product_id": c["product_id"], "fields": fields or ["price", "stock", "description"]} for c in cards]


def reply_(answer_type: str, answer: str, products=None, sources=None, follow_up=None) -> dict:  # build the /chat response (trace_id added later)
    return {"trace_id": None, "answer_type": answer_type, "answer": answer, "products": products or [],
            "sources": sources or [], "follow_up_question": follow_up}
