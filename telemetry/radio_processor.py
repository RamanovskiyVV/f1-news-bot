"""Team radio pipeline: download → Whisper transcription → translate."""
from __future__ import annotations

import asyncio
import io
import logging

import httpx
from openai import AsyncOpenAI

from .config import (
    DRIVERS,
    F1_SUBSCRIPTION_TOKEN,
    OPENAI_API_KEY,
    OPENAI_FILTER_MODEL,
    OPENAI_WHISPER_MODEL,
    RADIO_GLOSSARY_PROMPT,
    RADIO_TERMS_RU,
    TEAM_NAMES,
)

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Max concurrent radio jobs (prevents burst API calls on replay/reconnect)
_semaphore = asyncio.Semaphore(3)
# Max audio file size to download (2 MB — typical clip is 100-400 KB)
_MAX_AUDIO_BYTES = 2 * 1024 * 1024

# Relevance is intentionally a narrow deterministic deny-list.  A previous GPT
# classifier dropped memorable messages (sarcasm, comments about rivals, etc.),
# so any meaningful multi-word transcription is now shared by default.
_TRIVIAL_RADIO = {
    "copy", "ok", "okay", "roger", "understood", "affirm", "affirmative",
    "yes", "no", "sure", "alright", "alright then", "fine", "noted",
}
_WHISPER_HALLUCINATIONS = (
    "thank you for watching",
    "like and subscribe",
    "subtitles by",
    "thanks for watching",
)


def _is_junk_transcription(text: str) -> bool:
    normalized = " ".join(text.lower().strip().rstrip(".!?,").split())
    return (
        normalized in _TRIVIAL_RADIO
        or any(phrase in normalized for phrase in _WHISPER_HALLUCINATIONS)
    )


_TRANSLATE_TERMS_HINT = "; ".join(f'"{en}" -> "{ru}"' for en, ru in RADIO_TERMS_RU.items())

_TRANSLATE_SYSTEM = (
    "You are a professional translator specializing in Formula 1. "
    "Translate the following team radio message from English to Russian. "
    "Use natural, idiomatic Russian — translate idioms and expressions by meaning, not word-for-word. "
    "Preserve the emotional tone, exclamations, and urgency. "
    "You will be told which driver and team the message is from — use that to resolve ambiguous "
    "engineer callsigns, pronouns, and team-specific shorthand. "
    "Use these established F1 term translations where relevant instead of a literal translation: "
    f"{_TRANSLATE_TERMS_HINT}. "
    "CRITICAL RULE: in F1, a phrase like 'a five second penalty' or '10 second penalty' means a "
    "penalty measured in SECONDS (5s, 10s) -- F1 time penalties are never given in minutes. "
    "Example: 'five second penalty' -> 'штраф 5 секунд' (correct), NOT 'пятиминутный штраф' (WRONG, never do this). "
    "Reply with only the translated text, no extra commentary."
)


async def process_radio(
    recording_url: str,
    acronym: str,
) -> dict | None:
    """
    Full pipeline for one team radio entry.

    Returns:
        {
            'original': str,
            'translated': str,
            'audio_bytes': bytes,
        }
        or None if transcription is empty/junk or download failed.
    """
    team_key = DRIVERS.get(acronym.upper(), {}).get("team", "")
    team_name = TEAM_NAMES.get(team_key, "")

    async with _semaphore:
        # 1. Download audio
        audio_bytes = await _download_audio(recording_url)
        if not audio_bytes:
            return None

        # 2. Transcribe via Whisper (~$0.006/min, typical clip ≈ 20s ≈ $0.002)
        original = await _transcribe(audio_bytes, acronym=acronym, team=team_name, filename="radio.mp3")
        if not original or len(original.strip()) < 3:
            logger.info("Radio skipped (empty transcription): %s", recording_url.split("/")[-1])
            return None

        # 3. Skip only obvious confirmations and known Whisper hallucinations.
        # Everything else is worth sharing; subjective AI relevance filtering
        # previously discarded some of the best radio messages of a session.
        if _is_junk_transcription(original):
            logger.info("Radio skipped (junk): %s — %s", acronym, original[:80])
            return None

        logger.info("Radio PASSED filter: %s — %s", acronym, original[:80])

        # 4. Translate
        translated = await _translate(original, acronym=acronym, team=team_name)

        return {
            "original": original.strip(),
            "translated": translated.strip(),
            "audio_bytes": audio_bytes,
        }


async def _download_audio(url: str) -> bytes | None:
    try:
        headers = {}
        if F1_SUBSCRIPTION_TOKEN:
            headers["Authorization"] = f"Bearer {F1_SUBSCRIPTION_TOKEN}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=headers)
            if r.status_code == 403:
                logger.warning("Radio 403 (auth failed or wrong URL): %s", url)
                return None
            r.raise_for_status()
            if len(r.content) > _MAX_AUDIO_BYTES:
                logger.warning("Radio file too large (%d bytes), skipping: %s", len(r.content), url)
                return None
            return r.content
    except Exception as e:
        logger.warning("Failed to download radio audio: %s", e)
        return None


async def _transcribe(audio_bytes: bytes, acronym: str = "", team: str = "", filename: str = "radio.mp3") -> str:
    driver = DRIVERS.get(acronym.upper(), {})
    who = f" Driver: {driver.get('name', acronym)} ({team})." if acronym else ""
    prompt = f"{RADIO_GLOSSARY_PROMPT}{who}"

    for attempt in range(2):
        try:
            buf = io.BytesIO(audio_bytes)
            buf.name = filename
            response = await _client.audio.transcriptions.create(
                model=OPENAI_WHISPER_MODEL,
                file=buf,
                language="en",
                prompt=prompt,
                response_format="verbose_json",
            )
            segments = getattr(response, "segments", None) or []
            if not segments:
                # Some SDK/model combos don't return segments even for verbose_json;
                # fall back to the plain aggregated text in that case.
                return response.text
            kept = [
                s.text for s in segments
                if getattr(s, "no_speech_prob", 0.0) < 0.6 and getattr(s, "avg_logprob", 0.0) > -1.0
            ]
            return "".join(kept).strip()
        except Exception as e:
            if attempt == 0:
                logger.warning("Whisper transcription failed, retrying: %s", e)
                continue
            logger.warning("Whisper transcription failed: %s", e)
            return ""
    return ""


async def _translate(text: str, acronym: str = "", team: str = "") -> str:
    who = f"Driver: {acronym} ({team}).\n" if acronym else ""
    user_content = f"{who}Radio message: {text}"
    for attempt in range(2):
        try:
            response = await _client.chat.completions.create(
                model=OPENAI_FILTER_MODEL,
                messages=[
                    {"role": "system", "content": _TRANSLATE_SYSTEM},
                    {"role": "user", "content": user_content},
                ],
                max_tokens=200,
                temperature=0.3,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            if attempt == 0:
                logger.warning("GPT translation failed, retrying: %s", e)
                continue
            logger.warning("GPT translation failed: %s", e)
            return text  # fallback: return original
