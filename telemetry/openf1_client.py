"""Async client for the OpenF1 REST API (https://api.openf1.org/v1)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import OPENF1_BASE_URL

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_RATE_LIMIT_DELAY = 0.4  # seconds between requests to avoid 429
_semaphore = asyncio.Semaphore(1)  # one request at a time


class OpenF1Client:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OpenF1Client":
        self._client = httpx.AsyncClient(
            base_url=OPENF1_BASE_URL,
            timeout=_TIMEOUT,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> list[dict]:
        assert self._client, "Use as async context manager"
        async with _semaphore:
            try:
                r = await self._client.get(path, params=params)
                r.raise_for_status()
                result = r.json()
                await asyncio.sleep(_RATE_LIMIT_DELAY)
                return result
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    logger.warning("OpenF1 rate limited on %s, retrying in 2s", path)
                    await asyncio.sleep(2.0)
                    try:
                        r = await self._client.get(path, params=params)
                        r.raise_for_status()
                        return r.json()
                    except Exception:
                        pass
                logger.warning("OpenF1 HTTP error %s: %s", path, e)
                return []
            except Exception as e:
                logger.warning("OpenF1 request error %s: %s", path, e)
                return []

    # ── Session ────────────────────────────────────────────────────────────────

    async def get_year_sessions(self) -> list[dict]:
        """Return all sessions for the current year."""
        from datetime import datetime, timezone
        year = datetime.now(timezone.utc).year
        data = await self._get("/sessions", {"year": year})
        return data if data else []

    async def get_latest_session(self) -> dict | None:
        """Return the active or next upcoming session.
        Falls back to the most recent past session only if nothing else found.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        # Get all sessions for current year
        year = now.year
        data = await self._get("/sessions", {"year": year})
        if not data:
            # Fallback to API's own "latest" if year query fails
            fallback = await self._get("/sessions", {"session_key": "latest"})
            return fallback[0] if fallback else None

        # Sort by start time
        def _dt(s):
            try:
                return datetime.fromisoformat(s["date_start"])
            except Exception:
                return datetime.min.replace(tzinfo=timezone.utc)

        data.sort(key=_dt)

        # 1. Currently active session (started but not yet ended)
        for s in data:
            try:
                ds = datetime.fromisoformat(s["date_start"])
                de = datetime.fromisoformat(s["date_end"])
                if ds <= now <= de:
                    return s
            except Exception:
                continue

        # 2. Next upcoming session
        for s in data:
            try:
                ds = datetime.fromisoformat(s["date_start"])
                if ds > now:
                    return s
            except Exception:
                continue

        # 3. Most recent past session
        past = [s for s in data if _dt(s) <= now]
        return past[-1] if past else None

    async def get_session(self, session_key: int) -> dict | None:
        data = await self._get("/sessions", {"session_key": session_key})
        return data[0] if data else None

    async def get_sessions_for_meeting(self, meeting_key: int) -> list[dict]:
        return await self._get("/sessions", {"meeting_key": meeting_key})

    async def get_meeting(self, meeting_key: int) -> dict | None:
        data = await self._get("/meetings", {"meeting_key": meeting_key})
        return data[0] if data else None

    # ── Drivers ────────────────────────────────────────────────────────────────

    async def get_drivers(self, session_key: int) -> list[dict]:
        return await self._get("/drivers", {"session_key": session_key})

    # ── Positions ──────────────────────────────────────────────────────────────

    async def get_positions(self, session_key: int, after_date: str | None = None) -> list[dict]:
        params: dict = {"session_key": session_key}
        if after_date:
            params["date>"] = after_date
        return await self._get("/position", params)

    async def get_latest_positions(self, session_key: int) -> dict[int, int]:
        """Return {driver_number: position} for the most recent snapshot."""
        raw = await self._get("/position", {"session_key": session_key})
        latest: dict[int, int] = {}
        for entry in raw:
            dn = entry["driver_number"]
            # API returns chronological, last entry per driver wins
            latest[dn] = entry["position"]
        return latest

    # ── Laps ───────────────────────────────────────────────────────────────────

    async def get_laps(self, session_key: int, driver_number: int | None = None) -> list[dict]:
        params: dict = {"session_key": session_key}
        if driver_number is not None:
            params["driver_number"] = driver_number
        return await self._get("/laps", params)

    async def get_fastest_laps(self, session_key: int) -> dict[int, dict]:
        """Return {driver_number: lap_dict} for the fastest lap of each driver."""
        laps = await self.get_laps(session_key)
        best: dict[int, dict] = {}
        for lap in laps:
            if not lap.get("lap_duration"):
                continue
            dn = lap["driver_number"]
            if dn not in best or lap["lap_duration"] < best[dn]["lap_duration"]:
                best[dn] = lap
        return best

    # ── Pit stops ─────────────────────────────────────────────────────────────

    async def get_pit_stops(self, session_key: int) -> list[dict]:
        return await self._get("/pit", {"session_key": session_key})

    # ── Race control ──────────────────────────────────────────────────────────

    async def get_race_control(self, session_key: int, after_date: str | None = None) -> list[dict]:
        params: dict = {"session_key": session_key}
        if after_date:
            params["date>"] = after_date
        return await self._get("/race_control", params)

    # ── Team radio ────────────────────────────────────────────────────────────

    async def get_team_radio(self, session_key: int, after_date: str | None = None) -> list[dict]:
        params: dict = {"session_key": session_key}
        if after_date:
            params["date>"] = after_date
        return await self._get("/team_radio", params)

    # ── Stints (tyre info) ────────────────────────────────────────────────────

    async def get_stints(self, session_key: int) -> list[dict]:
        return await self._get("/stints", {"session_key": session_key})

    async def get_current_tyre(self, session_key: int, driver_number: int) -> str | None:
        """Return latest compound for given driver."""
        stints = await self._get("/stints", {
            "session_key": session_key,
            "driver_number": driver_number,
        })
        if not stints:
            return None
        return stints[-1].get("compound", "UNKNOWN").upper()

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def download_audio(self, url: str) -> bytes | None:
        """Download team radio audio bytes."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url)
                r.raise_for_status()
                return r.content
        except Exception as e:
            logger.warning("Failed to download audio %s: %s", url, e)
            return None


# Module-level singleton helper for one-off calls
async def fetch_latest_session() -> dict | None:
    async with OpenF1Client() as c:
        return await c.get_latest_session()
