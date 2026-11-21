"""Safety helpers (R8 don't trust content, R9 privacy, R6 never invent).

Defence in layers (no single check has to be perfect):
  1. Identity       customer_id from the request body only; sessions bound to it      (api/memory)
  2. Retrieval      reviews are never indexed                                         (search.py)
  3. LLM input      reviews never sent; descriptions cleaned of instruction-like text  (clean_text)
  4. LLM output     grounding: unknown IDs / ₹ amounts / invented offers / wrong counts (compose.py + here)
  5. Ranking        done by code, so no text can move a product up                    (tools.py)
  6. Records        PII redacted before anything is saved to the trace                (redact)
"""
import re

INJECTION = re.compile(
    r"(ignore (all |your |the |previous |prior )*(instructions|rules|prompt)|disregard (the )?(rules|instructions)|"
    r"\bsystem\s*:|\bassistant\s*:|you must (rank|recommend|say)|new instructions|act as|jailbreak|"
    r"reveal (the |your )?(system )?prompt|apne (rules|niyam) bhool)", re.I)

PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,16}(?!\d)")

OFFER_WORDS = re.compile(r"\b(free|discount(ed)?|\d+\s?% off|cashback|coupon|offer|sale price|buy one get one|bogo)\b", re.I)
NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "do": 2, "teen": 3, "char": 4, "paanch": 5}
COUNT_PHRASE = re.compile(r"\b(one|two|three|four|five|six|do|teen|char|paanch)\b(?:\s+[\w'-]+){0,3}?\s+"
                          r"(options|products|items|picks|choices|speakers|laptops|phones|shoes|bags|backpacks|gifts|models)\b", re.I)


def injection_score(text: str) -> list[str]:
    """Return the instruction-like phrases found (empty list = looks clean)."""
    return [m.group(0) for m in INJECTION.finditer(text or "")]


def clean_text(text: str | None) -> str | None:
    """Remove instruction-like sentences from catalogue text before an LLM sees it."""
    if not text:
        return text
    parts = re.split(r"(?<=[.!?])\s+", text)
    return " ".join(p for p in parts if not INJECTION.search(p)) or "[description removed: looked like instructions]"


def redact(text: str) -> str:
    """PII never goes into saved records (phone numbers, emails, card-like numbers)."""
    if not isinstance(text, str):
        return text
    text = EMAIL.sub("[email]", text)
    text = CARD.sub("[card-number]", text)
    return PHONE.sub("[phone]", text)


def redact_deep(obj):
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [redact_deep(x) for x in obj]
    if isinstance(obj, dict):
        return {k: redact_deep(v) for k, v in obj.items()}
    return obj


def output_problems(text: str, facts_text: str, n_products: int | None) -> list[str]:
    """Extra output checks on LLM wording (on top of ID / ₹ checks):
    - offers or discounts that are not in the facts (R6: no invented discounts)
    - a product count in the sentence that differs from what we actually show
    - instructions echoed back from data"""
    problems = []
    for m in OFFER_WORDS.finditer(text):
        if m.group(0).lower() not in facts_text.lower():
            problems.append(f"invented offer '{m.group(0)}'")
    if n_products is not None:
        for m in COUNT_PHRASE.finditer(text):
            n = NUMBER_WORDS[m.group(1).lower()]
            if n != n_products:
                problems.append(f"says {m.group(1)} {m.group(2)} but shows {n_products}")
    if injection_score(text):
        problems.append("instruction-like text in output")
    return problems
