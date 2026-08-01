"""Stage 2 — Media understanding (OCR / ASR → effective_text).

Every image is OCR'd + captioned and every voice note transcribed via Gemini's
native multimodal Flash model, then normalized into a single `effective_text`
string so downstream stages ignore modality. Results are cached to
code/cache/media/<media_id>.json so media is never re-processed (critical on the
free tier — a rerun costs zero quota).

    python code/media.py            # process all referenced media, print summary
    python code/media.py --force    # ignore cache and re-process
"""
from __future__ import annotations

import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    from . import config
    from .ingest import Dataset, load_dataset
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import config
    from ingest import Dataset, load_dataset

# Lazy clients so ingest.py stays dependency-free.
_CLIENT = None      # Gemini (images)
_GROQ = None        # Groq (voice ASR)


def _client():
    global _CLIENT
    if _CLIENT is None:
        from google import genai
        if not config.GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY is not set (see .env).")
        _CLIENT = genai.Client(api_key=config.GEMINI_API_KEY)
    return _CLIENT


def _groq():
    global _GROQ
    if _GROQ is None:
        from groq import Groq
        if not config.GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY is not set (see .env).")
        _GROQ = Groq(api_key=config.GROQ_API_KEY)
    return _GROQ


# --------------------------------------------------------------------------- #
# Rate-limit throttle + retry with exponential backoff (free-tier guardrails)
# --------------------------------------------------------------------------- #
_last_call = [0.0]
_min_interval = 60.0 / max(1, config.REQUESTS_PER_MINUTE)


def _throttle() -> None:
    wait = _min_interval - (time.time() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.time()


def _generate(parts: list[Any], model: str | None = None) -> str:
    from google.genai import types
    model = model or config.GEMINI_FLASH
    cfg = types.GenerateContentConfig(
        temperature=config.TEMPERATURE,
        response_mime_type="application/json",
        http_options=types.HttpOptions(timeout=config.REQUEST_TIMEOUT_MS),
    )
    last_err: Optional[Exception] = None
    for attempt in range(config.MAX_RETRIES):
        _throttle()
        try:
            r = _client().models.generate_content(model=model, contents=parts, config=cfg)
            return r.text or ""
        except Exception as e:  # noqa: BLE001 - retry on 429/5xx/transient/timeout
            last_err = e
            msg = str(e).lower()
            transient = any(code in msg for code in (
                "429", "500", "502", "503", "resource_exhausted", "unavailable", "timeout", "deadline"))
            if not transient or attempt == config.MAX_RETRIES - 1:
                break
            time.sleep(config.BACKOFF_BASE_SECONDS * (2 ** attempt))
    raise RuntimeError(f"Gemini call failed after retries: {type(last_err).__name__}: {last_err}")


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # tolerate accidental fencing / prose around the JSON
        a, b = text.find("{"), text.rfind("}")
        if a != -1 and b != -1 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except json.JSONDecodeError:
                pass
    return {"_raw": text}


def _mime(path: Path, kind: str) -> str:
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed:
        return guessed
    return "image/jpeg" if kind == "image" else "audio/mpeg"


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
IMAGE_PROMPT = (
    "You are analyzing an image attached to a WhatsApp message (often a poster, "
    "flyer, screenshot, or payment/QR image). Return STRICT JSON with keys:\n"
    '  "ocr": all visible text transcribed verbatim in its ORIGINAL language '
    "(keep Hindi/Hinglish/French/etc as-is; empty string if none),\n"
    '  "caption": one concise English sentence describing what the image is,\n'
    '  "has_link_or_qr": true/false if it contains a URL, QR code, or payment/login prompt,\n'
    '  "language": best-guess primary language of the text.\n'
    "Return only the JSON object."
)

def analyze_image(path: Path) -> dict:
    """Gemini vision — the only image OCR path (Groq has no vision model)."""
    from google.genai import types
    data = path.read_bytes()
    parts = [types.Part.from_bytes(data=data, mime_type=_mime(path, "image")), IMAGE_PROMPT]
    return _parse_json(_generate(parts, model=config.GEMINI_IMAGE_MODEL))


def analyze_voice(path: Path) -> dict:
    """Groq Whisper ASR — generous free tier, no per-day cap concern."""
    with path.open("rb") as fh:
        for attempt in range(config.MAX_RETRIES):
            _throttle()
            try:
                tr = _groq().audio.transcriptions.create(
                    file=(path.name, fh.read()),
                    model=config.GROQ_WHISPER,
                    response_format="verbose_json",
                    temperature=0.0,
                )
                return {
                    "transcript": getattr(tr, "text", "") or "",
                    "language": getattr(tr, "language", None),
                }
            except Exception as e:  # noqa: BLE001
                fh.seek(0)
                msg = str(e).lower()
                transient = any(c in msg for c in ("429", "500", "502", "503", "timeout", "unavailable"))
                if not transient or attempt == config.MAX_RETRIES - 1:
                    raise
                time.sleep(config.BACKOFF_BASE_SECONDS * (2 ** attempt))


def _effective_text(media_type: str, info: dict) -> str:
    if media_type == "image":
        ocr = (info.get("ocr") or "").strip()
        cap = (info.get("caption") or "").strip()
        flag = " [contains link/QR/payment prompt]" if info.get("has_link_or_qr") else ""
        return f"[IMAGE TEXT]: {ocr}\n[IMAGE DESCRIPTION]: {cap}{flag}".strip()
    if media_type == "voice":
        tr = (info.get("transcript") or "").strip()
        lang = (info.get("language") or "").strip()
        suffix = f"\n[VOICE LANGUAGE]: {lang}" if lang else ""
        return f"[VOICE TRANSCRIPT]: {tr}{suffix}".strip()
    return ""


# --------------------------------------------------------------------------- #
# Per-media processing with disk cache
# --------------------------------------------------------------------------- #
def process_media(media_id: str, media_type: str, path: Path, *, force: bool = False) -> dict:
    cache_path = config.MEDIA_CACHE_DIR / f"{media_id}.json"
    if cache_path.exists() and not force:
        return json.loads(cache_path.read_text(encoding="utf-8"))

    if media_type == "image":
        info = analyze_image(path)
    elif media_type == "voice":
        info = analyze_voice(path)
    else:
        info = {}

    record = {
        "media_id": media_id,
        "media_type": media_type,
        "model": config.GEMINI_FLASH,
        "info": info,
        "effective_text": _effective_text(media_type, info),
    }
    cache_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def effective_text_for_message(ds: Dataset, message: dict, *, force: bool = False) -> str:
    """The unified text a message should be reasoned over, regardless of modality."""
    mtype = message.get("media_type")
    if not mtype:  # plain text message
        return (message.get("message_text") or "").strip()

    media_id = message.get("media_id")
    path = ds.media_path(media_id, mtype)
    text = (message.get("message_text") or "").strip()  # sometimes both exist (image + caption)
    if path and path.exists():
        rec = process_media(media_id, mtype, path, force=force)
        media_text = rec.get("effective_text", "")
        return (text + "\n" + media_text).strip() if text else media_text
    return text or f"[missing {mtype} media: {media_id}]"


def build_all(ds: Dataset, *, force: bool = False) -> dict:
    """Process every media file in images.csv + voice_notes.csv (covers messages + history)."""
    jobs = [(mid, "image", ds.media_path(mid, "image")) for mid in ds.images]
    jobs += [(mid, "voice", ds.media_path(mid, "voice")) for mid in ds.voice]
    done, failed = 0, []
    for mid, mtype, path in jobs:
        if not path or not path.exists():
            failed.append((mid, "missing file"))
            continue
        try:
            rec = process_media(mid, mtype, path, force=force)
            done += 1
            preview = rec["effective_text"].replace("\n", " ")[:90]
            print(f"  ✓ {mtype:5s} {mid:12s} {preview}")
        except Exception as e:  # noqa: BLE001
            failed.append((mid, str(e)[:80]))
            print(f"  ✗ {mtype:5s} {mid:12s} {e}")
    return {"processed": done, "failed": failed, "total": len(jobs)}


def _main(argv: list[str]) -> int:
    force = "--force" in argv
    ds = load_dataset()
    print(f"Processing media (force={force}) — cache dir: {config.MEDIA_CACHE_DIR}")
    summary = build_all(ds, force=force)
    print(f"\nProcessed {summary['processed']}/{summary['total']}; failed {len(summary['failed'])}")
    for mid, why in summary["failed"]:
        print(f"  FAILED {mid}: {why}")
    return 0 if not summary["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
