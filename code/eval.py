"""M8 — Evaluate the router against the gold sample_messages.csv.

Runs the full pipeline on the solved sample rows and scores:
  - action accuracy + confusion
  - message_type accuracy (+ per-gold-type breakdown)
  - evidence validity (are predicted ids real?) and gold-evidence hit rate
  - confidence calibration (mean confidence on correct vs wrong)

    python code/eval.py                 # full gold sample
    python code/eval.py --limit 17      # first N rows
    python code/eval.py --no-cache      # force re-decide
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    from . import config
    from .ingest import load_dataset
    from .retrieval import EvidenceIndex
    from .decide import decide_message
    from .validate import to_output_row
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    from ingest import load_dataset
    from retrieval import EvidenceIndex
    from decide import decide_message
    from validate import to_output_row


def _ev_set(field: str) -> set[str]:
    return {x.strip() for x in (field or "").replace(";", ",").split(",")
            if x.strip() and x.strip().lower() != "none"}


def evaluate(input_path: Path, limit: int | None, use_cache: bool) -> int:
    ds = load_dataset()
    with Path(input_path).open(newline="", encoding="utf-8") as fh:
        gold = list(csv.DictReader(fh))
    if limit:
        gold = gold[:limit]
    idx = EvidenceIndex(ds)

    n = len(gold)
    a_ok = t_ok = 0
    act_conf = defaultdict(Counter)               # gold_action -> Counter(pred_action)
    type_by_gold = defaultdict(lambda: [0, 0])    # gold_type -> [correct, total]
    conf_correct, conf_wrong = [], []
    ev_pred_invalid = ev_gold_total = ev_gold_hit = 0
    ev_counts = []
    action_misses, type_misses = [], []

    for g in gold:
        d = decide_message(ds, g, idx, use_cache=use_cache)
        p = to_output_row(d)
        ga, gt = g["action"], g["message_type"]
        pa, pt = p["action"], p["message_type"]

        a_ok += pa == ga
        t_ok += pt == gt
        act_conf[ga][pa] += 1
        type_by_gold[gt][1] += 1
        type_by_gold[gt][0] += pt == gt
        (conf_correct if pa == ga else conf_wrong).append(float(p["confidence"]))
        if pa != ga:
            action_misses.append((g["message_id"], f"{pa}/{pt}", f"{ga}/{gt}"))
        elif pt != gt:
            type_misses.append((g["message_id"], f"{pa}/{pt}", f"{ga}/{gt}"))

        pred_ev = _ev_set(p["evidence_message_ids"])
        ev_counts.append(len(pred_ev))
        ev_pred_invalid += sum(1 for e in pred_ev if e not in ds.history_by_id)
        gold_ev = _ev_set(g.get("evidence_message_ids", ""))
        if gold_ev:
            ev_gold_total += 1
            if gold_ev & pred_ev:
                ev_gold_hit += 1

    def pct(x, d=n):
        return f"{x}/{d} = {x/d:.0%}" if d else "n/a"

    print(f"\n=== Evaluation on {n} gold rows ({input_path.name}) ===\n")
    print(f"ACTION accuracy      : {pct(a_ok)}")
    print(f"MESSAGE_TYPE accuracy: {pct(t_ok)}")

    print("\nAction confusion (rows=gold, cols=pred):")
    acts = ["notify", "digest", "mute"]
    print("        " + "".join(f"{a:>8}" for a in acts))
    for ga in acts:
        print(f"  {ga:6s}" + "".join(f"{act_conf[ga][pa]:>8}" for pa in acts))

    print("\nType accuracy by gold type:")
    for gt in sorted(type_by_gold, key=lambda k: -type_by_gold[k][1]):
        c, tot = type_by_gold[gt]
        print(f"  {gt:16s} {c}/{tot}")

    print("\nEvidence:")
    print(f"  avg predicted ids/row      : {sum(ev_counts)/n:.1f}")
    print(f"  invalid predicted ids      : {ev_pred_invalid} (should be 0)")
    print(f"  gold-evidence hit rate     : {pct(ev_gold_hit, ev_gold_total)} "
          f"(rows where our evidence includes a gold id)")

    mc = sum(conf_correct)/len(conf_correct) if conf_correct else 0
    mw = sum(conf_wrong)/len(conf_wrong) if conf_wrong else 0
    print("\nConfidence calibration:")
    print(f"  mean confidence when CORRECT: {mc:.2f}")
    print(f"  mean confidence when WRONG  : {mw:.2f}  {'✓ correct>wrong' if mc > mw else '✗'}")

    if action_misses:
        print("\nAction misses:")
        for mid, pv, gv in action_misses:
            print(f"  {mid}: pred {pv:22s} gold {gv}")
    if type_misses:
        print("\nType-only misses:")
        for mid, pv, gv in type_misses:
            print(f"  {mid}: pred {pv:22s} gold {gv}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate router on gold sample")
    ap.add_argument("--input", default=str(config.DATASET_DIR / "sample_messages.csv"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()
    return evaluate(Path(args.input), args.limit, not args.no_cache)


if __name__ == "__main__":
    raise SystemExit(main())
