"""Stage 3.5 — Evidence / retrieval engine (local TF-IDF, no API).

For each incoming message, retrieve the most similar PAST messages *for the same
user* from message_history, and join how the user reacted (message_events). That
reaction history is the strongest personalization signal and supplies the graded
`evidence_message_ids` output.

    python code/retrieval.py        # smoke test on a few messages
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

try:
    from . import config
    from .ingest import Dataset, load_dataset, to_int
    from . import media
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    from ingest import Dataset, load_dataset, to_int
    import media

MIN_SIM = 0.06          # ignore near-zero-overlap matches
SAME_CONTEXT_BOOST = 0.10   # nudge for same sender/group/business


def _reaction(ds: Dataset, message_id: str) -> Optional[dict]:
    ev = ds.event_for(message_id)
    if not ev:
        return None
    return {
        "opened": to_int(ev.get("message_opened")) == 1,
        "replied": to_int(ev.get("message_replied")) == 1,
        "dismissed": to_int(ev.get("notification_dismissed")) == 1,
        "muted_after": to_int(ev.get("muted_after_message")) == 1,
        "reported": to_int(ev.get("message_reported")) == 1,
        "reaction_time_min": ev.get("reaction_time_minutes"),
    }


def _reaction_gloss(r: Optional[dict]) -> str:
    if not r:
        return "no recorded reaction"
    parts = []
    if r["reported"]:
        parts.append("REPORTED")
    if r["muted_after"]:
        parts.append("muted-after")
    if r["dismissed"]:
        parts.append("dismissed")
    if r["replied"]:
        parts.append("replied")
    if r["opened"] and not r["dismissed"]:
        parts.append("opened")
    return ", ".join(parts) if parts else "ignored"


class EvidenceIndex:
    """TF-IDF index over all historical messages, queried per user."""

    def __init__(self, ds: Dataset):
        self.ds = ds
        self.records: list[dict[str, Any]] = []
        for uid, rows in ds.history_by_user.items():
            for r in rows:
                text = media.effective_text_for_message(ds, r)  # cache-only for media
                if not text:
                    continue
                self.records.append({
                    "row": self.records.__len__(),  # placeholder, set below
                    "message_id": r["message_id"],
                    "user_id": uid,
                    "group_id": r.get("group_id"),
                    "business_id": r.get("business_id"),
                    "sender_user_id": r.get("sender_user_id"),
                    "text": text,
                })
        for i, rec in enumerate(self.records):
            rec["row"] = i
        # index rows by user for fast candidate filtering
        self.by_user: dict[str, list[int]] = {}
        for rec in self.records:
            self.by_user.setdefault(rec["user_id"], []).append(rec["row"])

        corpus = [rec["text"] for rec in self.records] or ["_"]
        self.vectorizer = TfidfVectorizer(
            lowercase=True, strip_accents="unicode",
            ngram_range=(1, 2), min_df=1, sublinear_tf=True,
        )
        self.matrix = self.vectorizer.fit_transform(corpus)

    def retrieve(self, msg: dict, k: int = None, min_sim: float = MIN_SIM) -> list[dict]:
        k = k or config.TOP_K
        uid = msg.get("user_id")
        cand = self.by_user.get(uid, [])
        # never let a message match itself (if it appears in history)
        cand = [i for i in cand if self.records[i]["message_id"] != msg.get("message_id")]
        if not cand:
            return []

        query = media.effective_text_for_message(self.ds, msg)
        if not query:
            return []
        qv = self.vectorizer.transform([query])
        sims = cosine_similarity(qv, self.matrix[cand])[0]

        scored = []
        for pos, row in enumerate(cand):
            rec = self.records[row]
            sim = float(sims[pos])
            # boost same-sender / same-group / same-business history
            boost = 0.0
            if msg.get("business_id") and rec["business_id"] == msg.get("business_id"):
                boost += SAME_CONTEXT_BOOST
            if msg.get("group_id") and rec["group_id"] == msg.get("group_id"):
                boost += SAME_CONTEXT_BOOST
            if msg.get("sender_user_id") and rec["sender_user_id"] == msg.get("sender_user_id"):
                boost += SAME_CONTEXT_BOOST
            score = sim + boost
            if sim >= min_sim or boost > 0:
                scored.append((score, sim, rec))
        scored.sort(key=lambda t: t[0], reverse=True)

        out = []
        for score, sim, rec in scored[:k]:
            reaction = _reaction(self.ds, rec["message_id"])
            out.append({
                "message_id": rec["message_id"],
                "similarity": round(sim, 3),
                "score": round(score, 3),
                "text": rec["text"],
                "reaction": reaction,
                "reaction_gloss": _reaction_gloss(reaction),
            })
        return out


def evidence_summary(evidence: list[dict]) -> dict:
    """Aggregate the reaction pattern across retrieved evidence (personalization hint).

    Only *meaningfully similar* evidence (similarity >= MIN_SIM) votes on the hint, so
    a low-similarity match pulled in by the same-context boost can still be shown as
    evidence without skewing the reaction signal. `muted_after` is weighted toward
    `mute` (a stronger "stop showing me this" signal than a lone dismiss).
    """
    n = len(evidence)
    if not n:
        return {"n": 0, "voting": 0, "hint": "no similar history"}

    relevant = [e["reaction"] for e in evidence if e["reaction"] and e["similarity"] >= MIN_SIM]
    m = len(relevant)
    if not m:
        return {"n": n, "voting": 0, "hint": "similar messages exist but no strong reaction signal"}

    reported = sum(1 for r in relevant if r["reported"])
    muted = sum(1 for r in relevant if r["muted_after"])
    dismissed = sum(1 for r in relevant if r["dismissed"] and not r["muted_after"])
    engaged = sum(1 for r in relevant
                  if r["replied"] or (r["opened"] and not r["dismissed"] and not r["muted_after"]))
    neg = muted + dismissed

    def majority(x):        # strict majority: more than half of the voting matches
        return x * 2 > m

    def at_least_half(x):
        return x * 2 >= m

    # Reporting is rare/high-intent → a mute signal even in the minority, but its
    # STRENGTH scales with proportion (1/4 reported is not the same as 3/3).
    if reported and majority(reported):
        hint = f"user REPORTED {reported}/{m} similar → strong mute"
        strength = "strong"
    elif reported:
        extra = f" (+{neg} muted/dismissed)" if neg else ""
        hint = f"user reported {reported}/{m} similar{extra} → mute"
        strength = "moderate"
    elif at_least_half(muted):
        hint = f"user muted {muted}/{m} similar → mute"
        strength = "moderate"
    elif at_least_half(neg):
        hint = f"user dismissed {neg}/{m} similar → digest/mute"
        strength = "weak"
    elif at_least_half(engaged):
        hint = f"user engaged with {engaged}/{m} similar → notify/digest"
        strength = "moderate" if engaged == m else "weak"
    else:
        hint = "mixed/neutral history"
        strength = "weak"
    return {"n": n, "voting": m, "reported": reported, "muted": muted,
            "dismissed": dismissed, "engaged": engaged, "strength": strength, "hint": hint}


def _smoke_test() -> int:
    ds = load_dataset()
    print("Building TF-IDF evidence index…")
    idx = EvidenceIndex(ds)
    print(f"indexed {len(idx.records)} historical messages across {len(idx.by_user)} users\n")

    shown = 0
    covered = 0
    for msg in ds.messages:
        ev = idx.retrieve(msg)
        if ev:
            covered += 1
        if shown < 6 and ev:
            shown += 1
            q = media.effective_text_for_message(ds, msg).replace("\n", " ")[:70]
            print(f"--- {msg['message_id']} [{msg['conversation_type']}] {q}")
            for e in ev:
                print(f"    → {e['message_id']} sim={e['similarity']} score={e['score']} "
                      f"[{e['reaction_gloss']}] {e['text'][:55].replace(chr(10),' ')}")
            print("   summary:", evidence_summary(ev)["hint"], "\n")

    print(f"=== coverage: {covered}/{len(ds.messages)} messages have ≥1 evidence match ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke_test())
