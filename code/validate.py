"""Stage 5 — Validate & format output.

Converts decision dicts into the exact required output schema and enforces the
submission contract (exact columns/order, one row per input message_id, valid
enums, confidence in [0,1], evidence 'none' or semicolon-joined ids).
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

try:
    from . import config
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config


def to_output_row(decision: dict) -> dict:
    """Decision dict → ordered output row (evidence joined with ';' or 'none')."""
    ev = decision.get("evidence_message_ids") or []
    if isinstance(ev, str):
        ev = [x for x in ev.replace(";", ",").split(",") if x.strip() and x.strip() != "none"]
    ev_str = ";".join(ev) if ev else "none"
    action = decision.get("action", "digest")
    mtype = decision.get("message_type", "unknown")
    try:
        conf = f'{max(0.0, min(1.0, float(decision.get("confidence", 0.5)))):.2f}'
    except (TypeError, ValueError):
        conf = "0.50"
    reason = str(decision.get("reason", "")).replace("\n", " ").replace("\r", " ").strip() or "Routed."
    return {
        "message_id": decision.get("message_id", ""),
        "action": action,
        "message_type": mtype,
        "reason": reason,
        "confidence": conf,
        "evidence_message_ids": ev_str,
    }


def validate(rows: list[dict], expected_ids: Iterable[str]) -> list[str]:
    """Return a list of contract violations (empty = valid)."""
    problems: list[str] = []
    expected = list(expected_ids)
    ids = [r["message_id"] for r in rows]

    if set(ids) != set(expected):
        missing = set(expected) - set(ids)
        extra = set(ids) - set(expected)
        if missing:
            problems.append(f"missing {len(missing)} message_ids (e.g. {sorted(missing)[:3]})")
        if extra:
            problems.append(f"{len(extra)} unexpected message_ids (e.g. {sorted(extra)[:3]})")
    if len(ids) != len(set(ids)):
        dupes = [i for i in set(ids) if ids.count(i) > 1]
        problems.append(f"{len(dupes)} duplicate message_ids (e.g. {dupes[:3]})")
    if len(rows) != len(expected):
        problems.append(f"row count {len(rows)} != expected {len(expected)}")

    for r in rows:
        mid = r["message_id"]
        if r["action"] not in config.ALLOWED_ACTIONS:
            problems.append(f"{mid}: bad action {r['action']!r}")
        if r["message_type"] not in config.ALLOWED_MESSAGE_TYPES:
            problems.append(f"{mid}: bad message_type {r['message_type']!r}")
        try:
            c = float(r["confidence"])
            if not 0.0 <= c <= 1.0:
                problems.append(f"{mid}: confidence out of range {r['confidence']}")
        except (TypeError, ValueError):
            problems.append(f"{mid}: non-numeric confidence {r['confidence']!r}")
        if not str(r.get("reason", "")).strip():
            problems.append(f"{mid}: empty reason")
        if not str(r.get("evidence_message_ids", "")).strip():
            problems.append(f"{mid}: empty evidence field (use 'none')")
    return problems


def write_csv(path: str | Path, rows: list[dict]) -> None:
    """Write rows with the exact required columns, in the exact order."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=config.OUTPUT_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
