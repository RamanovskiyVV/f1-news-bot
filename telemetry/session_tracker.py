"""Core state machine: polls OpenF1 and emits event callbacks."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from .openf1_client import OpenF1Client
from .config import DRIVERS

logger = logging.getLogger(__name__)

EventCallback = Callable[..., Awaitable[None]]

# Sessions that get full live tracking
LIVE_SESSION_TYPES = {"Race", "Sprint", "Qualifying", "Sprint Qualifying"}
# Sessions that only get top-3 at the end
PRACTICE_SESSION_TYPES = {"Practice 1", "Practice 2", "Practice 3",
                           "Free Practice 1", "Free Practice 2", "Free Practice 3"}


def _session_is_active(session_doc: dict) -> bool:
    """True if current UTC time is within session window (+90 min buffer for long sessions)."""
    try:
        date_start = datetime.fromisoformat(session_doc["date_start"])
        date_end = datetime.fromisoformat(session_doc["date_end"])
        now = datetime.now(timezone.utc)
        return date_start <= now <= date_end + timedelta(minutes=90)
    except (KeyError, ValueError):
        return False


def _session_is_finished(session_doc: dict) -> bool:
    """True if current UTC time is past the session end (+90 min buffer)."""
    try:
        date_end = datetime.fromisoformat(session_doc["date_end"])
        now = datetime.now(timezone.utc)
        return now > date_end + timedelta(minutes=90)
    except (KeyError, ValueError):
        return False


@dataclass
class SessionState:
    session_key: int
    session_name: str
    meeting_name: str
    meeting_key: int
    started: bool = False
    ended: bool = False
    # {driver_number: position}
    last_positions: dict[int, int] = field(default_factory=dict)
    # {driver_number: best_lap_seconds}
    best_laps: dict[int, float] = field(default_factory=dict)
    overall_fastest: float | None = None
    overall_fastest_driver: int | None = None
    # set of (driver_number, lap_number) already seen
    seen_pits: set[tuple[int, int]] = field(default_factory=set)
    # set of rc message keys already sent
    seen_rc: set[str] = field(default_factory=set)
    # set of recording_url already processed
    seen_radio: set[str] = field(default_factory=set)
    # driver_number → number of pits so far
    pit_counts: dict[int, int] = field(default_factory=dict)
    # {driver_number: acronym} built from /drivers
    driver_map: dict[int, str] = field(default_factory=dict)
    # last date for incremental polling (iso str)
    last_poll_date: str | None = None


class SessionTracker:
    """
    Polls OpenF1 at a configured interval and calls event callbacks.

    Usage:
        tracker = SessionTracker()
        tracker.on_overtake = my_async_fn
        ...
        await tracker.poll()  # called by job queue every TELEMETRY_POLL_INTERVAL
    """

    # Assign async callables to these before polling
    on_session_start:   EventCallback | None = None
    on_session_end:     EventCallback | None = None
    on_overtake:        EventCallback | None = None
    on_fastest_lap:     EventCallback | None = None
    on_pit_stop:        EventCallback | None = None
    on_race_control:    EventCallback | None = None
    on_team_radio:      EventCallback | None = None

    def __init__(self) -> None:
        self._state: SessionState | None = None
        self._client = OpenF1Client()

    # ── Public ─────────────────────────────────────────────────────────────────

    async def poll(self) -> None:
        """Main poll entry point — call this from the job queue."""
        try:
            async with OpenF1Client() as client:
                await self._poll_inner(client)
        except Exception:
            logger.exception("Unhandled error in SessionTracker.poll()")

    @property
    def current_session(self) -> SessionState | None:
        return self._state

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _poll_inner(self, client: OpenF1Client) -> None:
        session_doc = await client.get_latest_session()
        if not session_doc:
            return

        sk = session_doc["session_key"]
        sname = session_doc.get("session_name", "")
        is_active = _session_is_active(session_doc)
        is_finished = _session_is_finished(session_doc)

        # ── New session detected ───────────────────────────────────────────────
        if self._state is None or self._state.session_key != sk:
            # Fetch meeting name from /meetings
            meeting_key = session_doc.get("meeting_key", 0)
            meeting_name = session_doc.get("meeting_name", "")
            if not meeting_name and meeting_key:
                meeting = await client.get_meeting(meeting_key)
                if meeting:
                    meeting_name = meeting.get("meeting_name", "") or meeting.get("location", "")
                    # Enrich session_doc so formatters get the data
                    session_doc["meeting_name"] = meeting_name
                    session_doc["circuit_short_name"] = session_doc.get("circuit_short_name") or meeting.get("circuit_short_name", "")

            self._state = SessionState(
                session_key=sk,
                session_name=sname,
                meeting_name=meeting_name,
                meeting_key=meeting_key,
            )
            # If session is already over when we first see it — mark as done
            if _session_is_finished(session_doc):
                self._state.started = True
                self._state.ended = True
                logger.info("Session already finished on detection, skipping: %s — %s", meeting_name, sname)
            # Store enriched doc for later use
            self._state._session_doc = session_doc
            # Build driver map
            drivers = await client.get_drivers(sk)
            for d in drivers:
                dn = d.get("driver_number")
                acr = d.get("name_acronym", "???")
                if dn:
                    self._state.driver_map[dn] = acr.upper()
            logger.info("New session detected: %s — %s (key=%s)", meeting_name, sname, sk)
        else:
            # Keep session_doc up to date with meeting_name
            if self._state.meeting_name:
                session_doc["meeting_name"] = self._state.meeting_name
            session_doc["circuit_short_name"] = session_doc.get("circuit_short_name") or getattr(self._state, "_session_doc", {}).get("circuit_short_name", "")

        state = self._state

        # ── Announce start ─────────────────────────────────────────────────────
        if is_active and not state.started:
            state.started = True
            logger.info("Session started: %s — %s", state.meeting_name, sname)
            if self.on_session_start:
                await self.on_session_start(session_doc)

        # ── If session not active, skip live events ────────────────────────────
        if not is_active:
            if is_finished and state.started and not state.ended:
                state.ended = True
                logger.info("Session ended: %s — %s", state.meeting_name, sname)
                if self.on_session_end:
                    await self.on_session_end(session_doc)
            return

        # ── Live event polling ─────────────────────────────────────────────────
        is_live_session = sname in LIVE_SESSION_TYPES or any(
            sname.startswith(p) for p in ("Race", "Sprint", "Qualifying")
        )
        is_practice = not is_live_session

        if is_live_session:
            await self._check_positions(client, state)
            await self._check_fastest_laps(client, state)
            await self._check_pits(client, state)
            await self._check_race_control(client, state)
            await self._check_team_radio(client, state)
        else:
            await self._check_race_control(client, state)

    # ── Position / overtake detection ──────────────────────────────────────────

    async def _check_positions(self, client: OpenF1Client, state: SessionState) -> None:
        new_positions = await client.get_latest_positions(state.session_key)
        if not new_positions:
            return

        old = state.last_positions
        if old:
            # Detect position improvements (lower number = ahead)
            for dn, new_pos in new_positions.items():
                old_pos = old.get(dn)
                if old_pos is None or new_pos >= old_pos:
                    continue
                # This driver gained positions — find who was displaced
                for other_dn, other_new_pos in new_positions.items():
                    if other_dn == dn:
                        continue
                    other_old = old.get(other_dn, other_new_pos)
                    # other was ahead (lower pos), now behind (higher pos)
                    if other_old == new_pos and other_new_pos == old_pos:
                        overtaker = state.driver_map.get(dn, str(dn))
                        overtaken = state.driver_map.get(other_dn, str(other_dn))
                        if self.on_overtake:
                            await self.on_overtake(
                                overtaker=overtaker,
                                overtaken=overtaken,
                                new_pos=new_pos,
                                old_pos=old_pos,
                                lap=None,
                            )
                        break

        state.last_positions = new_positions

    # ── Fastest lap detection ──────────────────────────────────────────────────

    async def _check_fastest_laps(self, client: OpenF1Client, state: SessionState) -> None:
        laps = await client.get_laps(state.session_key)
        for lap in laps:
            dn = lap.get("driver_number")
            dur = lap.get("lap_duration")
            lap_num = lap.get("lap_number")
            if dn is None or not dur:
                continue

            prev_best = state.best_laps.get(dn)
            if prev_best is None or dur < prev_best:
                state.best_laps[dn] = dur
                # Is this the overall fastest in the session?
                is_overall = (
                    state.overall_fastest is None
                    or dur < state.overall_fastest
                )
                if is_overall:
                    state.overall_fastest = dur
                    state.overall_fastest_driver = dn
                    acr = state.driver_map.get(dn, str(dn))
                    if self.on_fastest_lap:
                        await self.on_fastest_lap(
                            acronym=acr,
                            lap_time=dur,
                            lap_number=lap_num,
                            is_overall=True,
                        )

    # ── Pit stop detection ────────────────────────────────────────────────────

    async def _check_pits(self, client: OpenF1Client, state: SessionState) -> None:
        pits = await client.get_pit_stops(state.session_key)
        for pit in pits:
            dn = pit.get("driver_number")
            lap = pit.get("lap_number")
            key = (dn, lap)
            if key in state.seen_pits:
                continue
            state.seen_pits.add(key)

            # Fetch tyre compound from stints
            compound = pit.get("compound")  # may not always be in /pit
            if not compound:
                stints = await client.get_stints(state.session_key)
                for s in reversed(stints):
                    if s.get("driver_number") == dn:
                        compound = s.get("compound", "UNKNOWN")
                        break

            state.pit_counts[dn] = state.pit_counts.get(dn, 0) + 1
            acr = state.driver_map.get(dn, str(dn))
            if self.on_pit_stop:
                await self.on_pit_stop(
                    acronym=acr,
                    compound=compound,
                    pit_duration=pit.get("pit_duration"),
                    lap_number=lap,
                    pit_count=state.pit_counts[dn],
                )

    # ── Race control messages ─────────────────────────────────────────────────

    async def _check_race_control(self, client: OpenF1Client, state: SessionState) -> None:
        messages = await client.get_race_control(
            state.session_key, state.last_poll_date
        )
        for msg in messages:
            text = msg.get("message", "")
            date = msg.get("date", "")
            key = f"{date}:{text}"
            if key in state.seen_rc:
                continue
            state.seen_rc.add(key)
            if self.on_race_control:
                await self.on_race_control(
                    message=text,
                    lap_number=msg.get("lap_number"),
                    category=msg.get("category"),
                )

    # ── Team radio ────────────────────────────────────────────────────────────

    async def _check_team_radio(self, client: OpenF1Client, state: SessionState) -> None:
        radios = await client.get_team_radio(
            state.session_key, state.last_poll_date
        )
        for radio in radios:
            url = radio.get("recording_url", "")
            if not url or url in state.seen_radio:
                continue
            state.seen_radio.add(url)
            dn = radio.get("driver_number")
            acr = state.driver_map.get(dn, str(dn)) if dn else "???"
            date = radio.get("date", "")
            # Approximate lap from last known positions
            if self.on_team_radio:
                await self.on_team_radio(
                    acronym=acr,
                    recording_url=url,
                    date=date,
                    driver_number=dn,
                )
