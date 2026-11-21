"""Export three sample audit records (brief section 8) from the last eval run.

  python scripts/export_traces.py   -> eval/sample_audit_records.json
Picks one recommendation, one clarification and one refusal (different kinds of question).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app import db  # noqa: E402

WANT = ["recommendation", "clarification", "refusal"]


def main():
    picked = {}
    with db.connect() as conn:
        for (rec,) in conn.execute("SELECT record FROM traces WHERE session_id LIKE 'eval-live-%' ORDER BY created_at"):
            t = json.loads(rec)
            if t["answer_type"] in WANT and t["answer_type"] not in picked:
                picked[t["answer_type"]] = t
    out = [picked[k] for k in WANT if k in picked]
    (ROOT / "eval" / "sample_audit_records.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"exported {len(out)} records:", [(t["trace_id"], t["answer_type"], t["message"]) for t in out])


if __name__ == "__main__":
    main()
