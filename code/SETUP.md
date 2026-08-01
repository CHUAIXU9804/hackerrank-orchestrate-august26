# Message Notification Router — Setup & Usage

Routes every message in `dataset/messages.csv` to `notify` / `digest` / `mute` with a
`message_type`, `reason`, `confidence`, and `evidence_message_ids`, personalized to the
receiving user. See `../PLAN.md` for the full architecture.

## Stack (split — decided by real free-tier limits)
- **Groq `llama-3.3-70b-versatile`** — the routing decisions (text reasoning) — *primary*
- **Groq Whisper `whisper-large-v3`** — voice-note transcription
- **Gemini `gemini-3.1-flash-lite`** — image OCR/caption (the only vision option; cached, one-time)
- **Local TF-IDF** (scikit-learn) — evidence retrieval, no API

Gemini free tier is ~20 requests/day/model, and Groq has no vision model — so Gemini handles images
only (cached) and Groq does everything recurring.

## Setup
```bash
cd code
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env      # then fill in GROQ_API_KEY and GEMINI_API_KEY
```
Secrets are read from environment variables only (`../.env`, git-ignored) — never hardcoded.

## Run
```bash
python config.py     # verify paths + that keys are set
python main.py       # full pipeline → writes ../dataset/output.csv (110 rows), then validates
python eval.py       # score against the solved sample_messages.csv
```

**Free-tier rate-limit note:** a fresh run makes ~90 Groq calls. Groq's free tier caps *tokens per
minute* (~12k), and this prompt is ~2.5k tokens/call, so bursts trigger `429`s. For a clean single-pass
run, set `GROQ_REQUESTS_PER_MINUTE = 6` in `config.py` (slower but no waits). At higher throttle the code
still finishes — it honors Groq's `retry-after` and the run is **resumable** (cached rows are skipped, so
re-running `main.py` only fills what's missing). A paid Groq tier removes the limit entirely.

## Reproducibility
Decisions are cached (`cache/media/` by `media_id`, `cache/llm/` by `message_id` + a `prompt_version`
hash). Re-running reproduces `output.csv` identically; editing the prompt auto-invalidates stale
decisions; error fallbacks are never cached. The cache is a hit/miss layer over **real** Groq/Gemini API
calls — delete `cache/` and the pipeline re-calls the APIs from scratch.

## Pipeline stages (all implemented)
| File | Stage |
|---|---|
| `config.py` | paths, env-var keys, model IDs, throttle/retry/timeout, allowed values |
| `ingest.py` | Stage 1 — load + index all 13 CSVs (stdlib only) |
| `media.py` | Stage 2 — Gemini image OCR + Groq Whisper ASR → `effective_text` (cached) |
| `context.py` | Stage 3 — per-message context object + `risk_flags` (incl. `scam_pattern`) |
| `retrieval.py` | Stage 3.5 — TF-IDF evidence + `message_events` reaction join |
| `rules.py` | Stage 4a — safety gate (forged-domain / scam-pattern hard-mutes; injection defense) |
| `decide.py` | Stage 4b — **Groq** `llama-3.3-70b` structured JSON decision (cache-versioned) |
| `validate.py` | Stage 5 — enum/schema/row-count checks + output formatting |
| `main.py` | CLI orchestrator (`--input` / `--output` / `--limit` / `--no-cache`) |
| `eval.py` | score vs `sample_messages.csv` (action/type/evidence/calibration) |
