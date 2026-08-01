"""Stage 4a — Deterministic rules gate.

Runs BEFORE the LLM. Only the highest-precision, safety-critical cases are hard-decided
here (forged-domain phishing and content scam patterns) — these must `mute` regardless
of the user's usual engagement, and short-circuiting them is also our defense against
prompt-injection messages that try to force a `notify`.

Everything else returns None → deferred to the decision LLM (Stage 4b), which receives
the same context + risk flags + evidence as hints.
"""
from __future__ import annotations

from typing import Optional


def _decision(action: str, mtype: str, reason: str, confidence: float, source: str) -> dict:
    return {
        "action": action,
        "message_type": mtype,
        "reason": reason,
        "confidence": confidence,
        "evidence_message_ids": [],   # rule-based; evidence attached by caller if useful
        "source": source,
    }


def apply_rules(ctx: dict, ev_summary: Optional[dict] = None) -> Optional[dict]:
    """Return a forced decision for clear safety cases, else None (defer to LLM)."""
    flags = set(ctx.get("risk_flags", []))
    b = ctx.get("business") or {}
    trusted_txn_sender = bool(
        b.get("verified") and not b.get("domain_mismatch") and b.get("has_relationship")
    )

    # 1) Business impersonation via a forged look-alike / brand-new domain → phishing.
    #    Extremely high precision (official vs sender domain mismatch + young domain).
    if "suspicious_domain" in flags:
        return _decision(
            "mute", "scam",
            "Sender uses a forged look-alike domain impersonating the brand — phishing.",
            0.94, "rule:suspicious_domain",
        )

    # 2) Content scam pattern: financial/account context + credential/payment exfiltration
    #    (OTP, PIN, 'send screenshot', pay via link/QR). Mutes group/personal scams that
    #    have no domain to check, and neutralizes router prompt-injections carrying such
    #    payloads. Skip only for a clearly-trusted transactional sender (verified, matching
    #    domain, existing relationship) to avoid muting a legitimate transactional message.
    if "scam_pattern" in flags and not trusted_txn_sender:
        return _decision(
            "mute", "scam",
            "Requests OTP/PIN/credentials or payment via link/QR in a financial context — scam pattern.",
            0.90, "rule:scam_pattern",
        )

    return None
