"""Safety checks (R8 injection, R9 privacy, R6 never invent). Plain regex: fast and predictable."""
import re  # all checks here are regular expressions (no LLM involved)

# Phrases that try to give the AI orders ("ignore your instructions", "SYSTEM:", "you must rank...")
INJECTION = re.compile(r"ignore (all |your |the |previous )*(instructions|rules)|\bsystem\s*:|you must (rank|recommend|say)|"
                       r"reveal (your |the )?(system )?prompt|act as", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")  # Indian mobile number (optional +91)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")  # something@domain.tld
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)")  # 13-16 digits = card-like number
OFFERS = re.compile(r"\b(free|discount(ed)?|\d+\s?% off|cashback|coupon|offer)\b", re.I)  # offer words an LLM might invent
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "do": 2, "teen": 3, "paanch": 5}  # English + Hindi number words
# "two great speakers": a number word, up to 3 words, then a plural product noun
COUNT = re.compile(r"\b(one|two|three|four|five|do|teen|paanch)\b(?:\s+[\w-]+){0,3}?\s+(options|products|items|speakers|laptops|shoes|picks)\b", re.I)


def injection_flags(text: str) -> list[str]:  # flagged in the trace, never obeyed
    return [m.group(0) for m in INJECTION.finditer(text or "")]  # every suspicious phrase found


def clean_text(text: str | None) -> str | None:  # drop instruction-like sentences before the LLM sees catalogue text
    if not text:  # None or empty -> nothing to clean
        return text
    kept = [s for s in re.split(r"(?<=[.!?])\s+", text) if not INJECTION.search(s)]  # split into sentences, keep the clean ones
    return " ".join(kept) or "[removed]"  # if every sentence was bad, a placeholder


def redact(text):  # PII never goes into answers or saved traces
    if not isinstance(text, str):  # numbers, None, etc. pass through
        return text
    return PHONE.sub("[phone]", CARD.sub("[card]", EMAIL.sub("[email]", text)))  # emails, then cards, then phones


def redact_deep(obj):  # redact every string inside a dict / list
    if isinstance(obj, dict):
        return {k: redact_deep(v) for k, v in obj.items()}  # go into each value
    if isinstance(obj, list):
        return [redact_deep(v) for v in obj]  # go into each item
    return redact(obj)  # a single value


def asks_about_others(message: str, customer_id: str, other_names: list[str]) -> bool:  # R9 code net on top of the LLM
    m = message.lower()  # case-insensitive matching
    if re.search(r"\bc\d{3}\b", m) and customer_id.lower() not in m:  # someone else's customer ID
        return True
    order_words = re.search(r"\b(buy|bought|order|orders|ordered|purchase|kharida|kiya|liya)\b", m)  # talking about purchases?
    named = any(re.search(rf"\b{re.escape(n.lower())}\b", m) for n in other_names)  # another shopper's name?
    relation = re.search(r"\b(friend|dost|neighbou?r|padosi|brother|sister|bhai|wife|husband|someone else)\b", m)  # "my friend..."
    return bool(order_words and (named or relation))  # purchases + (a name or a relation) = someone else's data


def output_problems(text: str, facts_text: str, count: int | None = None) -> list[str]:  # checks on LLM wording
    problems = [f"invented offer '{o.group(0)}'" for o in OFFERS.finditer(text) if o.group(0).lower() not in facts_text.lower()]  # offer not in the facts
    if count is not None:  # only for recommendations
        problems += [f"says {c.group(1)} but shows {count}" for c in COUNT.finditer(text) if NUMBERS[c.group(1).lower()] != count]  # wrong count
    if injection_flags(text):  # the LLM repeated an instruction from the data
        problems.append("instruction echoed in output")
    return problems  # empty list = passed
