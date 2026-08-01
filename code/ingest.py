"""Stage 1 — Ingest & normalize.

Loads every CSV in dataset/ once and builds lookup indexes so downstream stages
(context assembly, retrieval, decision) can join in O(1). Pure stdlib — runnable
before any third-party package is installed:

    python code/ingest.py        # prints a smoke-test summary
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    from . import config  # when imported as a package
except ImportError:  # when run directly: python code/ingest.py
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config

Row = dict[str, Any]

# Timestamp formats seen in the data ("2026-07-31 11:09"); tolerate a few variants.
_DT_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")


def parse_dt(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def to_int(value: str | None, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, AttributeError, TypeError):
        return default


def to_bool(value: str | None) -> Optional[bool]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in ("true", "1", "yes", "y"):
        return True
    if v in ("false", "0", "no", "n"):
        return False
    return None


def _read_csv(path: Path) -> list[Row]:
    if not path.exists():
        raise FileNotFoundError(f"Expected dataset file missing: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        return [ {k: (v if v != "" else None) for k, v in row.items()}
                 for row in csv.DictReader(fh) ]


@dataclass
class Dataset:
    """All dataset files, loaded and indexed for fast joins."""
    messages: list[Row] = field(default_factory=list)

    users: dict[str, Row] = field(default_factory=dict)
    groups: dict[str, Row] = field(default_factory=dict)
    group_members: dict[tuple[str, str], Row] = field(default_factory=dict)
    business: dict[str, Row] = field(default_factory=dict)
    ubh: dict[tuple[str, str], Row] = field(default_factory=dict)

    history_by_id: dict[str, Row] = field(default_factory=dict)
    history_by_user: dict[str, list[Row]] = field(default_factory=dict)
    events_by_msg: dict[str, Row] = field(default_factory=dict)

    images: dict[str, str] = field(default_factory=dict)          # image_id -> path
    voice: dict[str, str] = field(default_factory=dict)            # voice_note_id -> path
    daily_by_user: dict[str, list[Row]] = field(default_factory=dict)

    # ---- convenience lookups -------------------------------------------- #
    def media_path(self, media_id: str | None, media_type: str | None) -> Optional[Path]:
        if not media_id:
            return None
        rel = None
        if media_type == "image":
            rel = self.images.get(media_id)
        elif media_type == "voice":
            rel = self.voice.get(media_id)
        else:  # fall back to whichever table has it
            rel = self.images.get(media_id) or self.voice.get(media_id)
        if not rel:
            return None
        p = Path(rel)
        return p if p.is_absolute() else (config.DATASET_DIR / rel)

    def group_member(self, group_id: str | None, user_id: str | None) -> Optional[Row]:
        if not group_id or not user_id:
            return None
        return self.group_members.get((group_id, user_id))

    def user_business(self, user_id: str | None, business_id: str | None) -> Optional[Row]:
        if not user_id or not business_id:
            return None
        return self.ubh.get((user_id, business_id))

    def event_for(self, message_id: str | None) -> Optional[Row]:
        return self.events_by_msg.get(message_id) if message_id else None


def load_dataset(dataset_dir: Path | None = None) -> Dataset:
    d = dataset_dir or config.DATASET_DIR
    ds = Dataset()

    ds.messages = _read_csv(d / "messages.csv")

    for r in _read_csv(d / "users.csv"):
        ds.users[r["user_id"]] = r
    for r in _read_csv(d / "groups.csv"):
        ds.groups[r["group_id"]] = r
    for r in _read_csv(d / "group_members.csv"):
        ds.group_members[(r["group_id"], r["user_id"])] = r
    for r in _read_csv(d / "business_accounts.csv"):
        ds.business[r["business_id"]] = r
    for r in _read_csv(d / "user_business_history.csv"):
        ds.ubh[(r["user_id"], r["business_id"])] = r

    for r in _read_csv(d / "message_history.csv"):
        ds.history_by_id[r["message_id"]] = r
        ds.history_by_user.setdefault(r["user_id"], []).append(r)
    for r in _read_csv(d / "message_events.csv"):
        ds.events_by_msg[r["message_id"]] = r  # message_id is unique per event row

    for r in _read_csv(d / "images.csv"):
        ds.images[r["image_id"]] = r["file_path"]
    for r in _read_csv(d / "voice_notes.csv"):
        ds.voice[r["voice_note_id"]] = r["file_path"]
    for r in _read_csv(d / "daily_notification_summary.csv"):
        ds.daily_by_user.setdefault(r["user_id"], []).append(r)

    return ds


def _smoke_test() -> int:
    ds = load_dataset()
    print("=== Loaded dataset ===")
    print(f"messages            : {len(ds.messages)}")
    print(f"users               : {len(ds.users)}")
    print(f"groups              : {len(ds.groups)}")
    print(f"group_members       : {len(ds.group_members)}")
    print(f"business_accounts   : {len(ds.business)}")
    print(f"user_business_history: {len(ds.ubh)}")
    print(f"message_history     : {len(ds.history_by_id)} (users w/ history: {len(ds.history_by_user)})")
    print(f"message_events      : {len(ds.events_by_msg)}")
    print(f"images / voice      : {len(ds.images)} / {len(ds.voice)}")

    # verify one joined message end-to-end
    print("\n=== Sample joins (first message of each conversation_type) ===")
    seen = set()
    for m in ds.messages:
        ct = m["conversation_type"]
        if ct in seen:
            continue
        seen.add(ct)
        user = ds.users.get(m["user_id"])
        line = [f"[{ct}] {m['message_id']} -> user {m['user_id']}"]
        line.append(f"DND={user.get('do_not_disturb_window') if user else '??'}")
        if ct == "group":
            g = ds.groups.get(m["group_id"])
            gm = ds.group_member(m["group_id"], m["user_id"])
            line.append(f"group_type={g.get('group_type') if g else '?'}")
            line.append(f"muted={gm.get('group_muted_by_user') if gm else '?'}")
        if ct == "business":
            b = ds.business.get(m["business_id"])
            ub = ds.user_business(m["user_id"], m["business_id"])
            line.append(f"verified={b.get('verified') if b else '?'}")
            line.append(f"opted_out={ub.get('promotions_opted_out_at') if ub else 'no-hist'}")
        line.append(f"hist_for_user={len(ds.history_by_user.get(m['user_id'], []))}")
        if m.get("media_id"):
            p = ds.media_path(m["media_id"], m["media_type"])
            line.append(f"media={m['media_type']}:{'FOUND' if p and p.exists() else 'MISSING'}")
        print("  " + " | ".join(line))

    # integrity checks
    print("\n=== Integrity checks ===")
    bad_media = [m["message_id"] for m in ds.messages
                 if m.get("media_id") and not (ds.media_path(m["media_id"], m["media_type"]) or Path("/x")).exists()]
    print(f"messages with missing media file : {len(bad_media)} {bad_media[:5]}")
    dt_ok = sum(1 for m in ds.messages if parse_dt(m.get("created_at")))
    print(f"messages with parseable created_at: {dt_ok}/{len(ds.messages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_smoke_test())
