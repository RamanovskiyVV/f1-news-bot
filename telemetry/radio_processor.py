"""Team radio pipeline: download → Whisper transcription → GPT-mini filter → translate."""
from __future__ import annotations

import asyncio
import io
import logging

import httpx
from openai import AsyncOpenAI

from .config import OPENAI_API_KEY, OPENAI_FILTER_MODEL, OPENAI_WHISPER_MODEL, F1_SUBSCRIPTION_TOKEN, CF_POLICY, CF_SIGNATURE, CF_KEY_PAIR_ID, F1_COOKIE_LOGIN_SESSION, F1_COOKIE_ENTITLEMENT_TOKEN

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# Max concurrent radio jobs (prevents burst API calls on replay/reconnect)
_semaphore = asyncio.Semaphore(3)
# Max audio file size to download (2 MB — typical clip is 100-400 KB)
_MAX_AUDIO_BYTES = 2 * 1024 * 1024

_FILTER_SYSTEM = (
    "You are a Formula 1 team radio analyst. "
    "Decide if this transcription is interesting or entertaining for F1 fans. "
    "Interesting = strategic drama, driver emotion, frustration, team conflict, "
    "funny moment, incident, mechanical problem, safety car reaction, memorable quote. "
    "NOT interesting = routine gap/position updates, generic box calls, simple acknowledgements. "
    "Reply with exactly one line: YES or NO"
)

_TRANSLATE_SYSTEM = (
    "You are a professional translator. "
    "Translate the following Formula 1 team radio message from English to Russian. "
    "Keep it natural and concise. Preserve exclamations and tone. "
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
        or None if not interesting / download failed.
    """
    async with _semaphore:
        # 1. Download audio
        audio_bytes = await _download_audio(recording_url)
        if not audio_bytes:
            return None

        # 2. Transcribe via Whisper (~$0.006/min, typical clip ≈ 20s ≈ $0.002)
        original = await _transcribe(audio_bytes, filename="radio.mp3")
        if not original or len(original.strip()) < 3:
            return None

        # 3. Filter via GPT-mini (fractions of a cent per call)
        if not await _is_interesting(original, acronym):
            logger.debug("Radio skipped (not interesting): %s — %s", acronym, original[:60])
            return None

        # 4. Translate
        translated = await _translate(original)

        return {
            "original": original.strip(),
            "translated": translated.strip(),
            "audio_bytes": audio_bytes,
        }


async def _download_audio(url: str) -> bytes | None:
    try:
        headers = {}
        cookies = {}
        if F1_SUBSCRIPTION_TOKEN:
            headers["Authorization"] = f"Bearer {F1_SUBSCRIPTION_TOKEN}"
        # CloudFront signed cookies (if available)
        if CF_POLICY and CF_SIGNATURE and CF_KEY_PAIR_ID:
            cookies.update({
                "CloudFront-Policy":      CF_POLICY,
                "CloudFront-Signature":   CF_SIGNATURE,
                "CloudFront-Key-Pair-Id": CF_KEY_PAIR_ID,
            })
        # F1TV session cookies (from browser, formula1.com)
        if F1_COOKIE_LOGIN_SESSION:
            cookies["login-session"] = F1_COOKIE_LOGIN_SESSION
        if F1_COOKIE_ENTITLEMENT_TOKEN:
            cookies["entitlement_token"] = F1_COOKIE_ENTITLEMENT_TOKEN

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=headers, cookies=cookies)
            if r.status_code == 403:
                if not cookies:
                    logger.debug("Radio 403 (need F1TV Pro cookies — run get_cf_cookies.py): %s", url.split("/")[-1])
                else:
                    logger.warning("Radio 403 even with session cookies (expired?): %s", url.split("/")[-1])
                return None
            r.raise_for_status()
            if len(r.content) > _MAX_AUDIO_BYTES:
                logger.warning("Radio file too large (%d bytes), skipping: %s", len(r.content), url)
                return None
            return r.content
    except Exception as e:
        logger.warning("Failed to download radio audio: %s", e)
        return None


async def _transcribe(audio_bytes: bytes, filename: str = "radio.mp3") -> str:
    try:
        buf = io.BytesIO(audio_bytes)
        buf.name = filename
        response = await _client.audio.transcriptions.create(
            model=OPENAI_WHISPER_MODEL,
            file=buf,
            language="en",
        )
        return response.text
    except Exception as e:
        logger.warning("Whisper transcription failed: %s", e)
        return ""


async def _is_interesting(text: str, acronym: str) -> bool:
    try:
        prompt = f"Driver: {acronym}\nRadio message: {text}"
        response = await _client.chat.completions.create(
            model=OPENAI_FILTER_MODEL,
            messages=[
                {"role": "system", "content": _FILTER_SYSTEM},
                {"role": "user", "content": prompt},
            ],
            max_tokens=5,
            temperature=0,
        )
        answer = response.choices[0].message.content.strip().upper()
        return answer.startswith("YES")
    except Exception as e:
        logger.warning("GPT filter failed, defaulting to skip: %s", e)
        return False


async def _translate(text: str) -> str:
    try:
        response = await _client.chat.completions.create(
            model=OPENAI_FILTER_MODEL,
            messages=[
                {"role": "system", "content": _TRANSLATE_SYSTEM},
                {"role": "user", "content": text},
            ],
            max_tokens=200,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.warning("GPT translation failed: %s", e)
        return text  # fallback: return original
