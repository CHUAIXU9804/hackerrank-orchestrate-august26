"""CLI orchestrator — the full message-notification-router pipeline.

Reads an input CSV (default dataset/messages.csv), runs every row through
ingest → media(cached) → context → retrieval → rules → decide → validate,
and writes the submission output.csv.

    python code/main.py                                  # full run → dataset/output.csv
    python code/main.py --limit 8                        # first 8 rows only
    python code/main.py --input dataset/sample_messages.csv --output /tmp/sample_output.csv --limit 8
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

try:
    from . import config
    from .ingest import load_dataset
    from .retrieval import EvidenceIndex
    from .decide import decide_message
    from . import validate as validate_mod
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    from ingest import load_dataset
    from retrieval import EvidenceIndex
    from decide import decide_message
    import validate as validate_mod


def run(input_path: Path, output_path: Path, limit: int | None, use_cache: bool) -> int:
    ds = load_dataset()
    with Path(input_path).open(newline="", encoding="utf-8") as fh:
        messages = list(csv.DictReader(fh))
    if limit:
        messages = messages[:limit]

    print(f"Routing {len(messages)} messages from {input_path}")
    print(f"  rules gate → {config.GROQ_LLM} (cache {'on' if use_cache else 'off'})\n")

    idx = EvidenceIndex(ds)
    decisions, t0 = [], time.time()
    n_rule = n_llm = 0
    for i, msg in enumerate(messages, 1):
        d = decide_message(ds, msg, idx, use_cache=use_cache)
        decisions.append(d)
        src = d.get("source", "?")
        n_rule += src.startswith("rule")
        n_llm += src == "llm"
        if i % 10 == 0 or i == len(messages):
            print(f"  {i}/{len(messages)} done ({time.time()-t0:.0f}s)")

    rows = [validate_mod.to_output_row(d) for d in decisions]
    problems = validate_mod.validate(rows, [m["message_id"] for m in messages])
    validate_mod.write_csv(output_path, rows)

    from collections import Counter
    print("\n=== Summary ===")
    print("action:", dict(Counter(r["action"] for r in rows)))
    print("type  :", dict(Counter(r["message_type"] for r in rows)))
    print(f"decided by: rules={n_rule}  llm={n_llm}")
    print(f"wrote {len(rows)} rows → {output_path}")
    if problems:
        print(f"\n❌ VALIDATION FAILED ({len(problems)}):")
        for p in problems[:20]:
            print("  -", p)
        return 1
    print("\n✅ VALIDATION PASSED — output conforms to the submission contract.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Message Notification Router")
    ap.add_argument("--input", default=str(config.DATASET_DIR / "messages.csv"))
    ap.add_argument("--output", default=str(config.OUTPUT_CSV))
    ap.add_argument("--limit", type=int, default=None, help="process only the first N rows")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached decisions")
    args = ap.parse_args()
    return run(Path(args.input), Path(args.output), args.limit, not args.no_cache)


if __name__ == "__main__":
    raise SystemExit(main())
