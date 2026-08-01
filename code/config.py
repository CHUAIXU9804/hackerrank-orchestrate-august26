"""Central configuration for the Message Notification Router.

All secrets are read from environment variables (see .env.example). Nothing here
is hardcoded. A tiny dependency-free .env loader is included so the ingest smoke
test runs before any third-party packages are installed.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
CODE_DIR = Path(__file__).resolve().parent
ROOT_DIR = CODE_DIR.parent
DATASET_DIR = ROOT_DIR / "dataset"
MEDIA_DIR = DATASET_DIR / "media"
IMAGES_DIR = MEDIA_DIR / "images"
AUDIO_DIR = MEDIA_DIR / "audio"
CACHE_DIR = CODE_DIR / "cache"
MEDIA_CACHE_DIR = CACHE_DIR / "media"
LLM_CACHE_DIR = CACHE_DIR / "llm"
OUTPUT_CSV = DATASET_DIR / "output.csv"

for _d in (CACHE_DIR, MEDIA_CACHE_DIR, LLM_CACHE_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Minimal .env loader (no python-dotenv dependency required)
# --------------------------------------------------------------------------- #
def load_env(env_path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing env vars are NOT overwritten.

    Tolerant of `KEY = 'value'`, `KEY=value`, quotes, and blank/comment lines.
    """
    env_path = env_path or (ROOT_DIR / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


load_env()

# --------------------------------------------------------------------------- #
# API keys (env only)
# --------------------------------------------------------------------------- #
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
# --- Split stack (forced by real free-tier limits, verified 2026-08) --------- #
# Gemini free tier = ~20 requests/DAY/model, and Groq has NO vision model (Llama 4
# was removed). So: images -> Gemini (only vision option; one-time + cached, <=20),
# everything recurring (110 decisions + voice ASR) -> Groq (generous free tier).

# Gemini: IMAGES ONLY. gemini-3.5-flash's daily quota is easily exhausted, so images
# use a model with its own separate daily quota.
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite")
GEMINI_FLASH = os.getenv("GEMINI_FLASH_MODEL", "gemini-3.5-flash")   # spare / optional escalation
GEMINI_PRO = os.getenv("GEMINI_PRO_MODEL", "gemini-pro-latest")      # optional escalation

# Groq: PRIMARY workhorse — decisions (text) + voice transcription (Whisper).
GROQ_LLM = os.getenv("GROQ_LLM_MODEL", "llama-3.3-70b-versatile")
GROQ_WHISPER = os.getenv("GROQ_WHISPER_MODEL", "whisper-large-v3")

# Optional escalation for ambiguous cases (premium models via OpenRouter).
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet")

# --------------------------------------------------------------------------- #
# Behavior / tuning
# --------------------------------------------------------------------------- #
TEMPERATURE = 0.0            # determinism
TOP_K = 5                    # evidence candidates retrieved per message
REQUESTS_PER_MINUTE = 12     # Gemini image throttle (tiny free-tier daily cap)
GROQ_REQUESTS_PER_MINUTE = 18  # Groq's free-tier RPM
MAX_RETRIES = 8             # on HTTP 429 / transient errors (429s honor Groq's retry-after)
BACKOFF_BASE_SECONDS = 2.0   # exponential backoff base
MAX_RETRY_WAIT = 150         # cap a single 429 wait (s); longer ⇒ daily cap → stop gracefully, resume later
REQUEST_TIMEOUT_MS = 90000   # per-request client timeout (ms) — avoid hung calls stalling the run
ESCALATE_TO_PRO = False      # route low-confidence msgs to Pro/OpenRouter
ESCALATE_CONFIDENCE_BELOW = 0.55

# --------------------------------------------------------------------------- #
# Output schema / allowed values (validation source of truth)
# --------------------------------------------------------------------------- #
OUTPUT_COLUMNS = [
    "message_id", "action", "message_type", "reason",
    "confidence", "evidence_message_ids",
]
ALLOWED_ACTIONS = {"notify", "digest", "mute"}
ALLOWED_MESSAGE_TYPES = {
    "personal", "urgent", "event", "payment", "business_update",
    "promotion", "greeting", "forward", "spam", "scam", "unknown",
}


def missing_keys() -> list[str]:
    """Return the list of required keys that are absent (for a friendly startup check)."""
    missing = []
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    return missing


if __name__ == "__main__":
    print("ROOT_DIR   :", ROOT_DIR)
    print("DATASET_DIR:", DATASET_DIR, "(exists)" if DATASET_DIR.exists() else "(MISSING)")
    print("CACHE_DIR  :", CACHE_DIR)
    print("GEMINI key :", "set ✅" if GEMINI_API_KEY else "MISSING ❌")
    print("GROQ key   :", "set ✅" if GROQ_API_KEY else "not set (optional)")
    print("OpenRouter :", "set ✅" if OPENROUTER_API_KEY else "not set (optional)")
    print("Flash model:", GEMINI_FLASH)
