"""Talk to the brain from the terminal (no API needed).

  python scripts/chat_cli.py            interactive, as shopper C005
  python scripts/chat_cli.py --demo     runs the demo conversations
Set MOCK_LLM=true in the environment to use rules only.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make "app" importable
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

from app.brain import Brain  # noqa: E402

DEMO = [  # (shopper, list of messages) — each tuple is one conversation
    ("C005", ["running shoes for flat feet", "is the blue one in size 9?"]),
    ("C005", ["A gift for my father who likes trekking, under ₹3,000"]),
    ("C006", ["I need a laptop", "under 60k, mostly for study", "show me cheaper ones"]),
    ("C007", ["Mujhe ek sasta Bluetooth speaker chahiye", "only JBL", "compare P-1046 and P-1047"]),
    ("C008", ["Do you sell electric cars?", "what did Priya buy last week?", "what's the weather today?", "what did I buy?"]),
]


def show(r: dict) -> None:  # print one reply
    print(f"  [{r['answer_type']}] {r['answer']}")
    for p in r["products"]:
        print(f"     - {p['product_id']} {p['name']} ₹{p['price']}  | {p['reason']}")


def main():
    brain = Brain()  # builds DB + index
    if "--demo" in sys.argv:
        for i, (cid, turns) in enumerate(DEMO):
            print(f"\n=== conversation {i + 1} (shopper {cid}) ===")
            for t in turns:
                print(f"> {t}")
                show(brain.handle(t, f"demo-{i}", cid))  # same session for all turns of one conversation
        return
    while True:  # interactive loop
        try:
            t = input("> ")
        except EOFError:  # Ctrl+Z / end of input -> quit
            break
        show(brain.handle(t, "cli", "C005"))


if __name__ == "__main__":
    main()
