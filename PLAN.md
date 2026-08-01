# PLAN — Message Notification Router

Working plan for the HackerRank Orchestrate build. For every incoming WhatsApp message in
`dataset/messages.csv`, decide **`notify` / `digest` / `mute`**, assign a **`message_type`**, and
emit a `reason`, `confidence`, and `evidence_message_ids` — personalized to the receiving user.

**Output contract (exact columns, exact order):**
```
message_id,action,message_type,reason,confidence,evidence_message_ids
```
One row per `message_id` in `messages.csv`. `evidence_message_ids` = semicolon-separated historical
IDs, or `none`.

---

## 0. Core mental model

It is a **personalized decision engine**: the *same* message can route three different ways depending on who receives it, their relationship to the sender, and their history.

Guiding principle: **cheap deterministic work first, LLM reserved for genuine judgment.**
Do NOT stuff every message into one giant prompt — it's expensive, non-deterministic, and hard to debug.

Three separable concerns:
1. **What is this message?** — content understanding (multimodal).
2. **Who receives it & what's the relationship?** — deterministic context assembly.
3. **What should happen?** — rules gate, then LLM judgment, with evidence.

---

## 1. Dataset facts (verified from headers)

| File | Rows | Key columns we use |
|---|---|---|
| `messages.csv` | **110** (87 text · 15 image · 8 voice; 63 group · 30 business · 17 personal; 32 forwarded) | message_id, user_id, conversation_type(personal/group/business), group_id, business_id, sender_user_id, created_at, message_text, media_type(''/image/voice), media_id, forwarded_count |
| `sample_messages.csv` | ~few | same + gold action/message_type/reason/confidence/evidence — **eval + few-shot** |
| `users.csv` | 54 | do_not_disturb_window (e.g. `23:00-08:00`), messages_opened_30d, messages_replied_30d, notifications_dismissed_30d, messages_reported_30d |
| `groups.csv` | 23 | group_type, member_count, admin_count, messages_30d |
| `group_members.csv` | 401 | role, messages_read_30d, replies_sent_30d, notifications_dismissed_30d, **group_muted_by_user** |
| `business_accounts.csv` | 110 | verified, official_domain, **domain_used_by_sender**, account_age_days, user_reports_30d, **domain_used_by_sender_age_days** |
| `user_business_history.csv` | 106 | why_user_knows_account, **allows_promotions**, **promotions_opted_out_at**, activity_count_180d, messages_opened/dismissed/replied_30d |
| `message_history.csv` | 412 (40 users have history) | past messages — **evidence pool** (same schema as messages.csv) |
| `message_events.csv` | 412 | message_opened, message_replied, reaction_time_minutes, notification_dismissed, **muted_after_message**, **message_reported** |
| `images.csv` | 20 | image_id → file_path (`media/images/*.jpg`) |
| `voice_notes.csv` | 13 | voice_note_id → file_path (`media/audio/*.mp3`) |
| `daily_notification_summary.csv` | 755 | per-user/day notifications_sent, notifications_dismissed → **fatigue** |


---

## 2. Pipeline (5 stages)

```
messages.csv row
  → STAGE 1  Ingest & normalize        (deterministic: load + index + join keys)
  → STAGE 2  Media understanding       (OCR/ASR → effective_text; cached by media_id)
  → STAGE 3  Context assembly          (deterministic joins → compact context object)
  → STAGE 4  Decision  (a) rules gate  (b) LLM judgment → structured JSON
  → STAGE 5  Validate & write output.csv
```

### Stage 1 — Ingest & normalize (`ingest.py`)
- Load every CSV once into dicts indexed by primary key (`users[user_id]`, `groups[group_id]`,
  `group_members[(group_id,user_id)]`, `business[business_id]`, `ubh[(user_id,business_id)]`, …).
- Pre-index `message_history` by `user_id` and `message_events` by `(user_id, message_id)`.
- Normalize `created_at` to datetime; coerce empties to `None`; parse `forwarded_count` int.
- Resolve the sender identity per `conversation_type`: personal→sender_user_id, group→group+sender,
  business→business_id.

### Stage 2 — Media understanding (`media.py`)
Produce one unified **`effective_text`** so downstream stages ignore modality.
- **text** → `message_text` as-is.
- **image** → OCR the poster/screenshot **+** a one-line vision caption (semantic description).
  Scams/promos hide text in images on purpose — capture both literal text and intent.
- **voice** → ASR transcript.
- **Cache** every result to `code/cache/media/<media_id>.json`. Process all 33 files up front.
- Determinism: temperature 0; cache keyed by media_id so reruns are identical.

### Stage 3 — Context assembly (`context.py`) — the differentiator, no LLM
Assemble a compact context object per message:

| Signal | Source | Routing effect |
|---|---|---|
| Quiet hours active? | `users.do_not_disturb_window` vs `created_at` | In DND → downgrade `notify`→`digest` unless urgent/mention/trusted-payment |
| Fatigue / dismiss rate | `users.*_dismissed_30d`, `daily_notification_summary` | Overloaded/high-dismiss → raise the bar for `notify` |
| Group role & mute | `group_members.role`, `group_muted_by_user`, read/reply rates | Admin sender → trust ↑; muted group → `digest`/`mute` **unless direct mention** |
| Group type | `groups.group_type` | family/school/work/society priors |
| Business trust | `business.verified`, `account_age_days`, `user_reports_30d` | Unverified + young + reported → risk ↑ |
| **Domain integrity** | `official_domain` vs `domain_used_by_sender`, `domain_used_by_sender_age_days` | Mismatch + brand-new domain → `suspicious_domain` flag → **scam** |
| **Content scam pattern** | keyword co-occurrence in `effective_text` | Financial/account context + credential/payment exfiltration (OTP/PIN/link/QR/"send screenshot"), with a **negation guard** → `scam_pattern` flag. Catches group/personal scams (no domain) + router prompt-injections. |
| Relationship | `ubh.why_user_knows_account`, `activity_count_180d`, open/reply rates | Real customer → order/payment updates matter |
| **Opt-out** | `ubh.allows_promotions`, `promotions_opted_out_at` (explicit timestamp only) | Opted-out promo → **mute** |
| Business reports | `business.user_reports_30d ≥ 20` | `high_reports` flag (scams sit at 38–61; legit brands 3–9) |
| Mention | regex `@user_id` in `effective_text` | Direct mention overrides group mute — *unless* it's inside a chain-letter |
| Forwarding | `forwarded_count ≥ 5` | `high_forward` — viral-chain prior |

Risk flags are **multi-label** (a scam trips several at once — the *count/combination* is itself a
confidence signal); `action`/`message_type` remain single-label outputs.

### Stage 3.5 — Evidence / retrieval engine (`retrieval.py`) — scored, don't skip
`evidence_message_ids` is graded. Implemented with **local TF-IDF** (scikit-learn, word 1–2grams) — no
embeddings API. For each incoming message:
1. Retrieve top-k similar historical messages **for the same user** (filter by `user_id`; `+0.10` boost
   for same sender/group/business). Ignore near-zero matches (`sim < MIN_SIM = 0.06`).
2. Join each candidate to `message_events` → how the user reacted (opened/replied/dismissed/**muted**/
   **reported**, reaction_time) → an `evidence_summary` **hint + `strength`**.
3. Reaction history is the strongest personalization signal, calibrated by proportion: **strict-majority
   `reported` → strong mute; minority report → moderate; `muted_after` majority → mute; engaged → notify/digest.**
   Only sim≥MIN_SIM evidence votes on the hint (a boosted low-sim match can appear as evidence but doesn't skew the reaction signal).
4. Pass candidates into the LLM; it **echoes back only the IDs it used** (validated against real history IDs).
   Emit `none` when nothing is relevant. *Resolves the admin-relayed-scam & mention-in-chain edge cases —
   the user reported/dismissed near-identical messages before.*

### Stage 4 — Decision (`rules.py` then `decide.py`)
**(a) Deterministic gate (`rules.py`) — runs first; ONLY the highest-precision safety cases are hard-decided.**
Kept deliberately narrow (a "trusted admin" can still relay a scam, so most calls need the LLM):
- `suspicious_domain` (forged / brand-new look-alike domain impersonating a brand) → **mute / scam**, conf 0.94.
- `scam_pattern` (OTP/PIN/credentials or payment-via-link/QR in a financial context) → **mute / scam**, conf 0.90,
  *unless* a clearly-trusted transactional sender (verified + matching domain + existing relationship).
- **This is also the prompt-injection defense** — injection messages carrying scam payloads are hard-muted
  *before* the LLM ever sees them.
- Everything else → `None` → deferred to the LLM (opt-out, mention, transactional-notify are handled there
  *with hints*, not hard-coded — content nuance beats brittle rules).

**(b) LLM judgment (`decide.py`) — everything else:**
- Model: **Groq `llama-3.3-70b-versatile`**, temperature 0, JSON mode.
- Input: `effective_text` (untrusted) + rendered context + risk flags + retrieved evidence + `evidence_summary`.
- Output: **strict JSON** `{action, message_type, reason, confidence, evidence_message_ids}`, validated/coerced.
- Prompt encodes: **engagement ≠ urgency**, **content-over-role**, DND/fatigue/opt-out reasoning,
  transactional-update-for-active-order → notify, type-taxonomy guidance, injection resistance, calibrated confidence.
- Few-shot (3 examples). One call per message. Cached by `message_id` + `prompt_version` (see §10).

### Stage 5 — Validate & write (`validate.py`)
- `action` ∈ {notify,digest,mute}; `message_type` ∈ 11 allowed (fallback `unknown`).
- `confidence` clamp [0,1]; **calibrate** (see §4) — no blanket 0.99.
- Exact column order; exactly one row per input `message_id`; `none` when no evidence.
- Fail loudly if row count ≠ **110** or any enum invalid.

---

## 3. Label taxonomy — decision heuristics

**`message_type` (11):** personal, urgent, event, payment, business_update, promotion, greeting,
forward, spam, scam, unknown.

Rough guide (LLM makes the final call, rules bias it):
- **urgent** — time-sensitive, deadline, safety, direct ask needing action now → usually `notify`.
- **payment** — invoice/reminder/receipt. Trusted+known → notify/digest; unverified/new domain → `mute` scam.
- **event** — schedule/logistics (school bus, meeting) → notify if same-day, else digest.
- **business_update** — order/booking status from known business → notify/digest by urgency.
- **promotion** — sale/marketing → digest if opted-in & engaged, `mute` if opted-out/ignored.
- **greeting** — "good morning" / festival wishes → digest or mute (low value).
- **forward** — chain/forwarded content → digest or mute; if financial/urgent claim → scam check.
- **spam** — repetitive/unwanted bulk → `mute`.
- **scam** — phishing, fake payment, impersonation, credential/OTP bait → `mute` regardless of engagement.
- **personal** — 1:1 human message → usually notify/digest.
- **unknown** — genuinely unclassifiable fallback.

**Action defaults** (before personalization): urgent/direct-personal/mention → notify · event/payment/
business_update → notify-or-digest by recency & trust · promotion/greeting/forward → digest · spam/scam → mute.
Then apply DND, fatigue, mute-state, opt-out, and reaction-history modifiers.

---

## 4. Confidence calibration
- **0.9–0.97** — deterministic rule hit (opt-out, domain mismatch, verified transactional).
- **0.7–0.88** — LLM confident, evidence agrees.
- **0.5–0.68** — plausible but thin/conflicting signals.
- **<0.5** — `unknown` / genuine ambiguity.
Never emit a flat 0.99 everywhere — calibration is scored.

---

## 5. File layout (as built)
```
code/
├── main.py         # CLI orchestrator: read input CSV → write output.csv (--input/--output/--limit/--no-cache)
├── config.py       # paths, model IDs, env-var API keys, throttle/retry/timeout, allowed values
├── ingest.py       # Stage 1 — load + index all 13 CSVs (stdlib only)
├── media.py        # Stage 2 — Gemini image OCR + Groq Whisper ASR, cached by media_id
├── context.py      # Stage 3 — per-message context object + risk_flags (incl. scam_pattern)
├── retrieval.py    # Stage 3.5 — TF-IDF evidence + reaction join + evidence_summary/strength
├── rules.py        # Stage 4a — safety gate (suspicious_domain / scam_pattern hard-mutes)
├── decide.py       # Stage 4b — Groq LLM structured decision (cache-versioned by prompt)
├── validate.py     # Stage 5 — enum/schema/row-count checks + output formatting
├── eval.py         # score vs sample_messages.csv (action/type/evidence/calibration)
├── requirements.txt
├── SETUP.md        # setup + run instructions (the submission README)
└── cache/          # media transcripts + LLM decisions (deterministic reruns; NOT shipped in code.zip)
```

---

---

## 7. Constraints & determinism (AGENTS.md §6)
- Runnable from terminal; reads `dataset/`; writes valid `output.csv`
- API keys from **env vars only** — never hardcoded. `.env` git-ignored.
- No organizer-only files, no hardcoded labels.
- **Deterministic**: temperature 0 + cache media & LLM outputs keyed by message_id → identical reruns.
- Log every turn to `~/hackerrank_orchestrate_august26/log.txt` per AGENTS.md.

---

## 8. Tech stack (locked — verified live against the keys, 2026-08)


- Gemini free tier
- Groq Free Tier



**Guardrails (required):**
- **Cache by media_id / message_id** — a rerun costs zero quota; essential given Gemini's 20/day.
- Gemini touches images ONLY. Everything recurring is Groq.
- Never send secrets; keys are env-only.


---

**Run it:**
```bash
cd code && pip install -r requirements.txt
cp ../.env.example ../.env         # fill GEMINI_API_KEY + GROQ_API_KEY
python main.py                     # → dataset/output.csv   (set GROQ_REQUESTS_PER_MINUTE=6 for a clean free-tier run)
python eval.py                     # score vs gold sample
```

**Submission artifacts:** `code.zip` (source only — no secrets/cache/venv/dataset) · `dataset/output.csv`
(110 predictions) · transcript `~/hackerrank_orchestrate_august26/log.txt`.

**Known trade-offs / future work:** type accuracy (83%) limited by inherently-fuzzy `unknown`/boundary
labels — not chased further to avoid overfitting the 30-row gold; a paid Groq tier (higher TPM) would let a
fresh run finish in ~2–4 min at 30 RPM; optional Pro/OpenRouter escalation for low-confidence rows remains OFF.
