"""Stage 3 — Context assembly (deterministic, no LLM).

Turns a thin `messages.csv` row into a rich, personalized context object by joining
the receiver, group/business, relationship, and behavioral tables, and computing
deterministic signals (DND active, @mention, domain integrity, opt-out, fatigue,
sender trust). This is the personalization backbone the rules gate (Stage 4a) and
the decision LLM (Stage 4b) reason over.

    python code/context.py            # smoke test on a few messages
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

try:
    from . import config
    from .ingest import Dataset, load_dataset, parse_dt, to_int
    from . import media
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    from ingest import Dataset, load_dataset, parse_dt, to_int
    import media

# --- tunables -------------------------------------------------------------- #
DOMAIN_NEW_DAYS = 180        # domain younger than this = suspicious
HIGH_FORWARD_COUNT = 5       # forwarded_count >= this = viral-chain signal
HIGH_DISMISS_RATE = 0.5      # dismissed / (opened+dismissed) above this = fatigue
HIGH_REPORTS = 20            # business user_reports_30d >= this = genuinely risky (scams sit at 38-61)

MENTION_RE = re.compile(r"@(u_\w+)", re.IGNORECASE)

# Content-based scam detection — works for ANY conversation type (group/personal
# scams have no business domain to check). Fires when a financial/account context
# co-occurs with a credential/proof-exfiltration ask, UNLESS negated (a legit
# safety notice like "no OTP is required" / "never share your OTP").
SCAM_FINANCIAL = (
    "pay", "payment", "upi", "fee", "due", "refund", "invoice", "bill",
    "clearance", "penalty", "deposit", "wallet", "bank", "account", "card",
    "payout", "kyc", "amount", "transaction",
)
SCAM_EXFIL = (
    "send screenshot", "share screenshot", "send the screenshot", "send me screenshot",
    "otp", "one time password", "login code", "verification code", "wallet pin",
    "upi pin", "atm pin", "enter your pin", "password", "scan this qr", "scan the qr",
    "scan qr", "use this link", "click the link", "payment link", "share the code",
    "send the code", "bank details", "account details", "confirm your wallet",
    "verify at", "complete verification", "final verification",
)
SCAM_NEGATION = (
    "no payment or otp", "not required", "never share", "do not share", "don't share",
    "will never ask", "never ask for", "no otp", "without otp",
    "don't use any payment link", "do not use any payment link",
)


def _has_any(text: str, terms) -> bool:
    """Multi-word terms match as substrings; single words match on word boundaries
    (so 'fee' does not match 'feedback', 'due' not 'overdue', 'card' not 'discard')."""
    for term in terms:
        if " " in term:
            if term in text:
                return True
        elif re.search(rf"\b{re.escape(term)}\b", text):
            return True
    return False


def detect_scam_pattern(text: str | None) -> bool:
    t = (text or "").lower()
    if not t or any(neg in t for neg in SCAM_NEGATION):
        return False
    return _has_any(t, SCAM_FINANCIAL) and _has_any(t, SCAM_EXFIL)


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y") if v is not None else False


def _parse_window(window: str | None) -> Optional[tuple[dtime, dtime]]:
    """'22:00-07:00' -> (time(22,0), time(7,0)). Handles midnight wrap at use-site."""
    if not window or "-" not in window:
        return None
    try:
        a, b = window.split("-", 1)
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
        return dtime(ah, am), dtime(bh, bm)
    except (ValueError, TypeError):
        return None


def dnd_active(window: str | None, when: datetime | None) -> Optional[bool]:
    """Is `when` inside the quiet-hours window (which may wrap past midnight)?"""
    parsed = _parse_window(window)
    if not parsed or when is None:
        return None
    start, end = parsed
    t = when.time()
    if start <= end:                 # same-day window
        return start <= t < end
    return t >= start or t < end     # wraps midnight


def _rate(numer: int, denom: int) -> Optional[float]:
    return round(numer / denom, 3) if denom > 0 else None


def assemble(ds: Dataset, msg: dict) -> dict:
    """Build the context object for one incoming message."""
    uid = msg.get("user_id")
    ctype = msg.get("conversation_type")
    created = parse_dt(msg.get("created_at"))
    user = ds.users.get(uid) or {}

    effective_text = media.effective_text_for_message(ds, msg)  # cache-only for media
    mentions = MENTION_RE.findall(effective_text or "")
    mentioned = uid in mentions if uid else False

    dnd_window = user.get("do_not_disturb_window")
    ctx: dict[str, Any] = {
        "message_id": msg.get("message_id"),
        "user_id": uid,
        "conversation_type": ctype,
        "created_at": msg.get("created_at"),
        "media_type": msg.get("media_type") or "text",
        "forwarded_count": to_int(msg.get("forwarded_count")),
        "high_forward": to_int(msg.get("forwarded_count")) >= HIGH_FORWARD_COUNT,
        "effective_text": effective_text,
        "mentioned": mentioned,
        # receiver behavior / fatigue
        "dnd_window": dnd_window,
        "dnd_active": dnd_active(dnd_window, created),
        "user_opened_30d": to_int(user.get("messages_opened_30d")),
        "user_dismissed_30d": to_int(user.get("notifications_dismissed_30d")),
        "user_reported_30d": to_int(user.get("messages_reported_30d")),
        "risk_flags": [],
    }
    ctx["user_dismiss_rate"] = _rate(ctx["user_dismissed_30d"],
                                     ctx["user_opened_30d"] + ctx["user_dismissed_30d"])
    ctx["user_fatigued"] = bool(ctx["user_dismiss_rate"] and ctx["user_dismiss_rate"] >= HIGH_DISMISS_RATE)

    # content-based scam signal (any conversation type)
    if detect_scam_pattern(effective_text):
        ctx["risk_flags"].append("scam_pattern")

    # -------- group context -------------------------------------------------- #
    if ctype == "group":
        gid = msg.get("group_id")
        g = ds.groups.get(gid) or {}
        my = ds.group_member(gid, uid) or {}
        sender = ds.group_member(gid, msg.get("sender_user_id")) or {}
        ctx["group"] = {
            "group_id": gid,
            "group_type": g.get("group_type"),
            "member_count": to_int(g.get("member_count")),
            "sender_user_id": msg.get("sender_user_id"),
            "sender_role": sender.get("role"),
            "sender_is_admin": (sender.get("role") == "admin"),
            "user_role": my.get("role"),
            "user_muted_group": _truthy(my.get("group_muted_by_user")),
            "user_reads_30d": to_int(my.get("messages_read_30d")),
            "user_replies_30d": to_int(my.get("replies_sent_30d")),
        }
        if ctx["group"]["user_muted_group"] and not mentioned:
            ctx["risk_flags"].append("muted_group_no_mention")
        if ctx["group"]["sender_is_admin"]:
            ctx["risk_flags"].append("trusted_admin_sender")

    # -------- business context ---------------------------------------------- #
    elif ctype == "business":
        bid = msg.get("business_id")
        b = ds.business.get(bid) or {}
        ubh = ds.user_business(uid, bid) or {}
        official = (b.get("official_domain") or "").strip().lower()
        used = (b.get("domain_used_by_sender") or "").strip().lower()
        domain_age = to_int(b.get("domain_used_by_sender_age_days"), default=-1)
        domain_mismatch = bool(official and used and official != used)
        domain_new = 0 <= domain_age < DOMAIN_NEW_DAYS
        opted_out = ubh.get("promotions_opted_out_at") is not None or not _truthy(ubh.get("allows_promotions"))
        ctx["business"] = {
            "business_id": bid,
            "brand_name": b.get("brand_name"),
            "category": b.get("category"),
            "verified": _truthy(b.get("verified")),
            "account_age_days": to_int(b.get("account_age_days")),
            "user_reports_30d": to_int(b.get("user_reports_30d")),
            "official_domain": official,
            "domain_used_by_sender": used,
            "domain_used_age_days": domain_age,
            "domain_mismatch": domain_mismatch,
            "domain_new": domain_new,
            "why_user_knows_account": ubh.get("why_user_knows_account"),
            "has_relationship": bool(ubh),
            "allows_promotions": _truthy(ubh.get("allows_promotions")),
            "promotions_opted_out": ubh.get("promotions_opted_out_at") is not None,
            "activity_count_180d": to_int(ubh.get("activity_count_180d")),
        }
        if domain_mismatch and domain_new:
            ctx["risk_flags"].append("suspicious_domain")
        elif domain_mismatch:
            ctx["risk_flags"].append("domain_mismatch")
        if ctx["business"]["promotions_opted_out"]:   # explicit opt-out timestamp only
            ctx["risk_flags"].append("promo_opted_out")
        if not ctx["business"]["verified"]:
            ctx["risk_flags"].append("unverified_business")
        if to_int(b.get("user_reports_30d")) >= HIGH_REPORTS:
            ctx["risk_flags"].append("high_reports")

    # -------- personal context ---------------------------------------------- #
    else:
        ctx["personal"] = {"sender_user_id": msg.get("sender_user_id")}

    return ctx


def render(ctx: dict) -> str:
    """Compact human-readable block for the decision LLM prompt."""
    lines = [
        f"conversation_type: {ctx['conversation_type']}",
        f"media_type: {ctx['media_type']}",
        f"forwarded_count: {ctx['forwarded_count']}" + (" (HIGH — viral chain)" if ctx["high_forward"] else ""),
        f"quiet_hours_active: {ctx['dnd_active']}",
        f"receiver_dismiss_rate_30d: {ctx['user_dismiss_rate']}" + (" (fatigued)" if ctx["user_fatigued"] else ""),
    ]
    if ctx.get("mentioned"):
        lines.append("DIRECT @MENTION of the receiver: yes")
    if "group" in ctx:
        g = ctx["group"]
        lines.append(f"group: type={g['group_type']} members={g['member_count']} "
                     f"sender_role={g['sender_role']} user_muted={g['user_muted_group']} "
                     f"user_reads_30d={g['user_reads_30d']} user_replies_30d={g['user_replies_30d']}")
    if "business" in ctx:
        b = ctx["business"]
        lines.append(f"business: brand={b['brand_name']} category={b['category']} verified={b['verified']} "
                     f"reports_30d={b['user_reports_30d']} account_age_days={b['account_age_days']}")
        lines.append(f"  domain: official={b['official_domain']} used={b['domain_used_by_sender']} "
                     f"used_age_days={b['domain_used_age_days']} mismatch={b['domain_mismatch']} new={b['domain_new']}")
        lines.append(f"  relationship: why={b['why_user_knows_account']} "
                     f"allows_promotions={b['allows_promotions']} opted_out={b['promotions_opted_out']} "
                     f"activity_180d={b['activity_count_180d']}")
    if ctx["risk_flags"]:
        lines.append("risk_flags: " + ", ".join(ctx["risk_flags"]))
    return "\n".join(lines)


def _smoke_test() -> int:
    ds = load_dataset()
    print("=== Context smoke test (first of each conversation_type + a risky one) ===\n")
    shown = set()
    for msg in ds.messages:
        ct = msg["conversation_type"]
        ctx = assemble(ds, msg)
        risky = bool(ctx["risk_flags"])
        key = ct + ("!" if risky else "")
        if ct in shown and not (risky and (ct + "!") not in shown):
            continue
        shown.add(key)
        print(f"--- {msg['message_id']} [{ct}] ---")
        print(render(ctx))
        print("effective_text:", (ctx["effective_text"] or "")[:120].replace("\n", " "), "\n")
        if len(shown) >= 6:
            break

    # aggregate signal coverage across all 110
    from collections import Counter
    flags = Counter(f for m in ds.messages for f in assemble(ds, m)["risk_flags"])
    dnd = sum(1 for m in ds.messages if assemble(ds, m)["dnd_active"])
    ment = sum(1 for m in ds.messages if assemble(ds, m)["mentioned"])
    print("=== Signal coverage over 110 messages ===")
    print("risk_flag counts:", dict(flags))
    print(f"messages during quiet hours: {dnd}")
    print(f"messages with a direct @mention: {ment}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke_test())
