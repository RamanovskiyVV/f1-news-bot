"""Core state machine: detects sessions via OpenF1, tracks live events via SignalR.

Architecture:
  - poll()              -- called every TELEMETRY_POLL_INTERVAL seconds via job queue.
                          Uses OpenF1 REST API for session detection (works fine between
                          sessions).  When OpenF1 is unavailable (401 during live sessions),
                          falls back to cached session dates to track state transitions.
  - _livetiming_task    -- long-running asyncio.Task started when a session goes live.
                          Connects to livetiming.formula1.com/signalr (free, no auth) and
                          fires event callbacks in real time.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from .livetiming_client import AUDIO_BASE, LiveTimingClient, parse_lap_time
from .openf1_client import OpenF1Client
from .config import DRIVERS, RACING_NUMBER_TO_ACR


def _resolve_driver(dn: int, driver_map: dict) -> str:
    """Resolve driver acronym from driver_map, falling back to RACING_NUMBER_TO_ACR."""
    return driver_map.get(dn) or RACING_NUMBER_TO_ACR.get(dn) or str(dn)


async def _fetch_session_path(session_key: int) -> str:
    """Fetch static session path from F1 livetiming Index.json (no auth required)."""
    import httpx
    from datetime import datetime, timezone
    year = datetime.now(timezone.utc).year
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"https://livetiming.formula1.com/static/{year}/Index.json")
            if r.status_code != 200:
                return ""
            import json
            data = json.loads(r.content.decode("utf-8-sig"))
            for meeting in data.get("Meetings", []):
                for session in meeting.get("Sessions", []):
                    if session.get("Key") == session_key:
                        path = session.get("Path", "")
                        if path:
                            return path
                        # Path is empty for the live session — construct it from meeting path
                        # Find any sibling session that has a path to derive the meeting folder
                        meeting_path = ""
                        for sibling in meeting.get("Sessions", []):
                            p = sibling.get("Path", "")
                            if p:
                                # e.g. "2026/2026-06-14_Barcelona_Grand_Prix/2026-06-13_Qualifying/"
                                # strip last segment to get meeting folder
                                parts = p.rstrip("/").rsplit("/", 1)
                                if len(parts) == 2:
                                    meeting_path = parts[0] + "/"
                                break
                        if meeting_path:
                            # Build session folder from date and name
                            start = session.get("StartDate", "")[:10]  # "2026-06-14"
                            name = session.get("Name", "Race").replace(" ", "_")
                            return f"{meeting_path}{start}_{name}/"
    except Exception as exc:
        logger.debug("_fetch_session_path error: %s", exc)
    return ""

logger = logging.getLogger(__name__)

EventCallback = Callable[..., Awaitable[None]]

# Session types that get full live tracking (positions, laps, pits, radio)
LIVE_SESSION_TYPES = {"Race", "Sprint", "Qualifying", "Sprint Qualifying"}
# Session types that only emit race-control + end-of-session summary
PRACTICE_SESSION_TYPES = {
    "Practice 1", "Practice 2", "Practice 3",
    "Free Practice 1", "Free Practice 2", "Free Practice 3",
}


def _session_is_active(session_doc: dict) -> bool:
    """True if now is within session window (+90-min buffer)."""
    try:
        date_start = datetime.fromisoformat(session_doc["date_start"])
        date_end   = datetime.fromisoformat(session_doc["date_end"])
        now = datetime.now(timezone.utc)
        return date_start <= now <= date_end + timedelta(minutes=90)
    except (KeyError, ValueError):
        return False


def _session_is_finished(session_doc: dict) -> bool:
    """True if now is past the session end (+90-min buffer)."""
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
    # Cached OpenF1 session document (for date-based checks when API unavailable)
    session_doc: dict = field(default_factory=dict)
    # {driver_number(int): position(int)}
    last_positions: dict[int, int] = field(default_factory=dict)
    # {driver_number(int): best_lap_seconds(float)}
    best_laps: dict[int, float] = field(default_factory=dict)
    overall_fastest: float | None = None
    overall_fastest_driver: int | None = None
    # set of (driver_number, lap_number) already announced
    seen_pits: set[tuple[int, int]] = field(default_factory=set)
    # set of "utc:text" keys already announced
    seen_rc: set[str] = field(default_factory=set)
    # set of recording URLs already processed
    seen_radio: set[str] = field(default_factory=set)
    # {driver_number: total pit count}
    pit_counts: dict[int, int] = field(default_factory=dict)
    # {driver_number: monotonic_timestamp} — drivers who pitted recently (suppress fake overtakes for 60s)
    recently_pitted: dict[int, float] = field(default_factory=dict)
    # {driver_number: stint_count} — for pit detection via TimingAppData
    _stint_counts: dict[int, int] = field(default_factory=dict)
    # Static path for this session (from SessionInfo.Path) e.g. "2026/2026-06-14_Barcelona_Grand_Prix/2026-06-14_Race/"
    session_path: str = ""
    # {driver_number(int): acronym(str)} -- built from DriverList
    driver_map: dict[int, str] = field(default_factory=dict)
    # {driver_number(int): team_key(str)}
    team_map: dict[int, str] = field(default_factory=dict)
    # {acronym(str): driver_number(int)}
    acronym_to_dn: dict[str, int] = field(default_factory=dict)
    # {racing_number_str: tyre_compound_str} from TimingAppData
    current_tyre: dict[str, str] = field(default_factory=dict)
    # {driver_number(int): gap_to_leader_str} -- accumulated during race/sprint
    race_gaps: dict[int, str] = field(default_factory=dict)
    # {driver_number(int): {"Q1": str, "Q2": str, "Q3": str}} -- qualifying segment times
    quali_q_times: dict[int, dict] = field(default_factory=dict)


class SessionTracker:
    """
    Polls OpenF1 for session transitions and streams live events via SignalR.

    Assign async callables to the on_* attributes before calling poll().
    """

    on_session_start:    EventCallback | None = None
    on_session_end:      EventCallback | None = None
    on_session_restored: EventCallback | None = None  # fires on first session_key resolution (before any events)
    on_overtake:         EventCallback | None = None
    on_fastest_lap:      EventCallback | None = None
    on_pit_stop:         EventCallback | None = None
    on_race_control:     EventCallback | None = None
    on_team_radio:       EventCallback | None = None

    def __init__(self) -> None:
        self._state: SessionState | None = None
        self._livetiming_task: asyncio.Task | None = None
        self._livetiming_client: LiveTimingClient | None = None
        self._startup_check_done: bool = False  # fire missed-session only once on startup

    # -- Public -----------------------------------------------------------------

    async def poll(self) -> None:
        """Main entry point -- called by the job queue every poll interval."""
        try:
            async with OpenF1Client() as client:
                await self._poll_inner(client)
        except Exception:
            logger.exception("Unhandled error in SessionTracker.poll()")

    @property
    def current_session(self) -> SessionState | None:
        return self._state

    # -- Session detection ------------------------------------------------------

    async def _poll_inner(self, client: OpenF1Client) -> None:
        # Try OpenF1 first (works between sessions; returns 401 during live)
        session_doc = await client.get_latest_session()

        # Fallback: use cached session doc when OpenF1 is unavailable
        if not session_doc and self._state and self._state.session_doc:
            session_doc = self._state.session_doc

        if not session_doc:
            # OpenF1 unavailable AND no cache (e.g. bot restarted mid-session).
            # Bootstrap: start SignalR blind -- SessionInfo topic will populate state.
            if self._state is None:
                logger.info(
                    "OpenF1 unavailable and no cached state -- bootstrapping SignalR"
                )
                self._state = SessionState(
                    session_key=-1,
                    session_name="Unknown",
                    meeting_name="Unknown",
                    meeting_key=-1,
                    started=True,   # assume live since we can't verify
                )
            self._start_livetiming()
            return

        sk    = session_doc["session_key"]
        sname = session_doc.get("session_name", "")
        is_active   = _session_is_active(session_doc)
        is_finished = _session_is_finished(session_doc)

        # -- New session --------------------------------------------------------
        if self._state is None or self._state.session_key != sk:

            # Case 1: Transitioning from bootstrap state (ran during 401 window).
            # Case 2: Fresh start (_state is None) — check if a session finished
            #         while the bot was down and we missed its on_session_end.
            is_bootstrap_transition = (
                self._state is not None
                and self._state.session_key == -1
                and self._state.started
                and not self._state.ended
            )
            is_fresh_start = self._state is None and not self._startup_check_done

            if is_bootstrap_transition or is_fresh_start:
                self._startup_check_done = True

            if is_bootstrap_transition or is_fresh_start:
                missed = await self._find_missed_session(client, sk)
                if missed:
                    label = "Bootstrap ended" if is_bootstrap_transition else "Fresh start"
                    logger.info(
                        "%s -- firing on_session_end for missed: %s %s",
                        label,
                        missed.get("meeting_name"), missed.get("session_name"),
                    )
                    if is_bootstrap_transition:
                        self._state.ended = True
                        await self._stop_livetiming()
                    if self.on_session_end:
                        await self.on_session_end(missed)

            meeting_key  = session_doc.get("meeting_key", 0)
            meeting_name = session_doc.get("meeting_name", "")
            if not meeting_name and meeting_key:
                meeting = await client.get_meeting(meeting_key)
                if meeting:
                    meeting_name = meeting.get("meeting_name", "") or meeting.get("location", "")
                    session_doc["meeting_name"] = meeting_name
                    session_doc["circuit_short_name"] = (
                        session_doc.get("circuit_short_name")
                        or meeting.get("circuit_short_name", "")
                    )

            self._state = SessionState(
                session_key=sk,
                session_name=sname,
                meeting_name=meeting_name,
                meeting_key=meeting_key,
                session_doc=session_doc,
            )

            if is_finished:
                self._state.started = True
                self._state.ended   = True
                logger.info("Session already finished on detection: %s -- %s", meeting_name, sname)
                return

            # Pre-populate driver map (OpenF1 is free between sessions)
            drivers = await client.get_drivers(sk)
            for d in drivers:
                dn  = d.get("driver_number")
                acr = d.get("name_acronym", "???").upper()
                if dn:
                    self._state.driver_map[dn] = acr
                    self._state.acronym_to_dn[acr] = dn
                    if acr in DRIVERS:
                        self._state.team_map[dn] = DRIVERS[acr].get("team", "")

            logger.info("New session detected: %s -- %s (key=%s)", meeting_name, sname, sk)

        else:
            # Keep meeting name consistent and refresh cached doc
            session_doc["meeting_name"] = self._state.meeting_name
            self._state.session_doc = session_doc

        state = self._state

        # -- Announce start + launch live timing --------------------------------
        if is_active and not state.started:
            state.started = True
            logger.info("Session started: %s -- %s", state.meeting_name, sname)
            if self.on_session_start:
                await self.on_session_start(session_doc)
            self._start_livetiming()

        # -- Ensure live timing stays running during session --------------------
        elif is_active and state.started and not state.ended:
            self._start_livetiming()  # no-op if already running

        # -- DateTime fallback for session end ----------------------------------
        if is_finished and state.started and not state.ended:
            await self._trigger_session_end()

    async def _find_missed_session(
        self, client: OpenF1Client, current_sk: int
    ) -> dict | None:
        """Find the most recently finished session that was active during bootstrap.

        Called when transitioning from a bootstrap state (key=-1) to a real
        session once OpenF1 becomes available again.
        """
        all_sessions = await client.get_year_sessions()
        if not all_sessions:
            return None
        # Find finished sessions that are not the newly detected one
        # Use a small buffer (10 min) to allow for sessions that just ended,
        # rather than the 90-min live-tracking buffer used by _session_is_finished.
        def _recently_ended(s: dict) -> bool:
            try:
                de = datetime.fromisoformat(s["date_end"])
                now = datetime.now(timezone.utc)
                return now > de + timedelta(minutes=10)
            except (KeyError, ValueError):
                return False

        finished = [
            s for s in all_sessions
            if _recently_ended(s) and s.get("session_key") != current_sk
        ]
        if not finished:
            return None
        # Take the most recently finished
        recent = max(finished, key=lambda s: s.get("date_end", ""))
        # Enrich with meeting name if missing
        mk = recent.get("meeting_key", 0)
        if not recent.get("meeting_name") and mk:
            m = await client.get_meeting(mk)
            if m:
                recent["meeting_name"] = m.get("meeting_name", "") or m.get("location", "")
                recent["circuit_short_name"] = recent.get("circuit_short_name") or m.get("circuit_short_name", "")
        return recent

    # -- Live timing task management --------------------------------------------

    def _start_livetiming(self) -> None:
        """Start the SignalR live timing task if not already running."""
        if self._livetiming_task and not self._livetiming_task.done():
            return
        logger.info("Starting F1 live timing SignalR task")
        self._livetiming_client = LiveTimingClient()
        self._livetiming_client.on_message = self._on_live_message
        self._livetiming_task = asyncio.create_task(
            self._livetiming_client.run(),
            name="f1-livetiming",
        )
        self._livetiming_task.add_done_callback(
            lambda t: logger.info(
                "Live timing task finished: %s",
                t.exception() if not t.cancelled() else "cancelled",
            )
        )

    async def _stop_livetiming(self) -> None:
        """Stop the SignalR live timing task gracefully."""
        if self._livetiming_client:
            self._livetiming_client._running = False
        if self._livetiming_task and not self._livetiming_task.done():
            self._livetiming_task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self._livetiming_task), timeout=3.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        self._livetiming_task   = None
        self._livetiming_client = None

    async def _trigger_session_end(self) -> None:
        """Mark session as ended, stop live timing, fire on_session_end."""
        state = self._state
        if state is None or state.ended:
            return
        state.ended = True
        logger.info("Session ended: %s -- %s", state.meeting_name, state.session_name)
        await self._stop_livetiming()
        if self.on_session_end:
            await self.on_session_end(
                state.session_doc,
                live_laps=dict(state.best_laps),
                driver_map=dict(state.driver_map),
                live_positions=dict(state.last_positions),
                race_gaps=dict(state.race_gaps),
                quali_q_times=dict(state.quali_q_times),
                pit_counts=dict(state.pit_counts),
            )

    # -- Live message dispatcher ------------------------------------------------

    async def _on_live_message(self, topic: str, data: Any) -> None:
        state = self._state
        if state is None or state.ended:
            return

        sname = state.session_name
        is_live = (
            sname in LIVE_SESSION_TYPES
            or any(sname.startswith(p) for p in ("Race", "Sprint", "Qualifying"))
        )

        try:
            if topic == "SessionInfo":
                await self._process_session_info(data, state)
            elif topic == "DriverList":
                self._process_driver_list(data, state)
            elif topic == "TimingAppData":
                await self._process_timing_app_data(data, state, emit_events=is_live)
            elif topic == "TimingData":
                await self._process_timing_data(data, state, emit_events=is_live)
            elif topic == "PitLaneTimeCollection" and is_live:
                await self._process_pit_data(data, state)
            elif topic == "RaceControlMessages":
                await self._process_rc_messages(data, state)
            elif topic == "TeamRadio" and is_live:
                await self._process_team_radio(data, state)
            elif topic == "SessionStatus":
                await self._process_session_status(data, state)
        except Exception:
            logger.exception("Error processing live topic %s", topic)

    # -- SessionInfo (bootstraps state when OpenF1 is unavailable) --------------

    async def _process_session_info(self, data: dict, state: SessionState) -> None:
        """Populate / update session state from the SignalR SessionInfo topic.

        Called once with the full snapshot on connect, then again on changes.
        Most important when the bot restarted mid-session and has no OpenF1 cache.
        """
        if not isinstance(data, dict):
            return

        session_name = data.get("Name", "")
        meeting      = data.get("Meeting", {}) or {}
        meeting_name = meeting.get("Name") or meeting.get("OfficialName", "")
        meeting_key  = meeting.get("Key", -1)
        # F1 SignalR key ≠ OpenF1 session_key but unique enough for dedup
        session_key  = data.get("Key", -1)
        start_str    = data.get("StartDate", "")
        end_str      = data.get("EndDate", "")
        gmt_offset   = data.get("GmtOffset", "00:00:00")  # e.g. "02:00:00"

        if not session_name or session_key == -1:
            return  # incomplete snapshot, wait for next

        # Convert local time + GMT offset to UTC
        def _to_utc(local_str: str) -> str | None:
            try:
                from datetime import datetime, timezone, timedelta
                dt = datetime.fromisoformat(local_str)
                if dt.tzinfo is None:
                    h, m, _ = (int(x) for x in gmt_offset.split(":"))
                    dt = dt.replace(tzinfo=timezone(timedelta(hours=h, minutes=m)))
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                return None

        utc_start = _to_utc(start_str)
        utc_end   = _to_utc(end_str)

        is_bootstrap = (state.session_key == -1)

        session_path = data.get("Path", "")  # e.g. "2026/2026-06-14_Barcelona_Grand_Prix/2026-06-14_Race/"
        logger.debug("SessionInfo Path from SignalR: %r", session_path)

        # Update bootstrap state with real values
        if is_bootstrap or state.session_key != session_key:
            state.session_key  = session_key
            state.session_name = session_name
            state.meeting_name = meeting_name
            state.meeting_key  = meeting_key
            if session_path:
                state.session_path = session_path
                logger.info("session_path set from SignalR: %s", session_path)
            else:
                # Live race path is often empty in SignalR — fetch from static index
                fetched = await _fetch_session_path(session_key)
                if fetched:
                    state.session_path = fetched
                    logger.info("session_path fetched from static index: %s", fetched)
            if utc_start:
                state.session_doc["date_start"] = utc_start
                state.session_doc["session_key"] = session_key
                state.session_doc["meeting_name"] = meeting_name
                state.session_doc["session_name"] = session_name
            if utc_end:
                state.session_doc["date_end"] = utc_end
            logger.info(
                "SessionInfo received: %s -- %s (key=%s)",
                meeting_name, session_name, session_key,
            )
            # Fire on_session_restored immediately so seen events are loaded
            # before any RC/pit/radio messages arrive from SignalR replay.
            if is_bootstrap and self.on_session_restored:
                await self.on_session_restored(session_key)

        # If this was a bootstrap and session is active, fire on_session_start
        if is_bootstrap and not state.ended:
            if utc_start and utc_end:
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                try:
                    ds = datetime.fromisoformat(state.session_doc["date_start"])
                    de = datetime.fromisoformat(state.session_doc["date_end"])
                    if ds <= now <= de:
                        logger.info("Bootstrap confirms active session, firing on_session_start")
                        if self.on_session_start:
                            await self.on_session_start(state.session_doc)
                except Exception:
                    pass

    # -- DriverList -------------------------------------------------------------

    def _process_driver_list(self, data: dict, state: SessionState) -> None:
        if not isinstance(data, dict):
            return
        for rn_str, info in data.items():
            if not isinstance(info, dict):
                continue
            try:
                dn = int(rn_str)
            except ValueError:
                continue
            acr = (info.get("Tla") or "").upper()
            if acr:
                state.driver_map[dn]    = acr
                state.acronym_to_dn[acr] = dn
            if acr in DRIVERS:
                state.team_map[dn] = DRIVERS[acr].get("team", "")
            elif info.get("TeamName") and dn not in state.team_map:
                state.team_map[dn] = info["TeamName"]

    # -- TimingAppData (tyres + pit detection via stint count) ------------------

    async def _process_timing_app_data(self, data: dict, state: SessionState, emit_events: bool = True) -> None:
        lines = data.get("Lines", {})
        if not isinstance(lines, dict):
            return
        for rn_str, info in lines.items():
            if not isinstance(info, dict):
                continue
            try:
                dn = int(rn_str)
            except ValueError:
                continue

            stints = info.get("Stints", {})
            if isinstance(stints, dict):
                stints = list(stints.values())
            if not isinstance(stints, list) or not stints:
                continue

            last_stint = stints[-1]
            compound = last_stint.get("Compound", "")
            if compound:
                state.current_tyre[rn_str] = compound.upper()

            # Pit detection: new stint appeared = driver pitted
            stint_count = len(stints)
            prev_count = state._stint_counts.get(dn, 0)
            if stint_count > prev_count and prev_count > 0 and emit_events:
                # New stint = completed pit stop
                import time as _time
                state.recently_pitted[dn] = _time.monotonic()
                lap = last_stint.get("LapNumber") or last_stint.get("StartLaps")
                try:
                    lap = int(lap) if lap is not None else None
                except (TypeError, ValueError):
                    lap = None
                key = (dn, lap)
                if key not in state.seen_pits:
                    state.seen_pits.add(key)
                    state.pit_counts[dn] = state.pit_counts.get(dn, 0) + 1
                    acr = _resolve_driver(dn, state.driver_map)
                    if self.on_pit_stop:
                        await self.on_pit_stop(
                            acronym=acr,
                            compound=compound.upper() if compound else "UNKNOWN",
                            pit_duration=None,
                            lap_number=lap,
                            pit_count=state.pit_counts[dn],
                        )
            state._stint_counts[dn] = stint_count

    # -- TimingData: positions + fastest laps -----------------------------------

    async def _process_timing_data(self, data: dict, state: SessionState, emit_events: bool = True) -> None:
        lines = data.get("Lines", {})
        if not isinstance(lines, dict):
            return

        new_positions: dict[int, int] = {}

        for rn_str, info in lines.items():
            if not isinstance(info, dict):
                continue
            try:
                dn = int(rn_str)
            except ValueError:
                continue

            # Position
            pos_str = info.get("Position")
            if pos_str:
                try:
                    new_positions[dn] = int(pos_str)
                except ValueError:
                    pass

            # Race gap to leader
            gap = info.get("GapToLeader", "")
            if gap and isinstance(gap, str):
                state.race_gaps[dn] = gap

            # Qualifying segment times (BestLapTimes keys "0"=Q1, "1"=Q2, "2"=Q3)
            blt = info.get("BestLapTimes", {})
            if isinstance(blt, dict):
                for k, qlabel in (("0", "Q1"), ("1", "Q2"), ("2", "Q3")):
                    entry = blt.get(k)
                    if isinstance(entry, dict) and entry.get("Value"):
                        if dn not in state.quali_q_times:
                            state.quali_q_times[dn] = {}
                        state.quali_q_times[dn][qlabel] = entry["Value"]

            # Fastest lap
            best = info.get("BestLapTime", {})
            if isinstance(best, dict):
                lap_str    = best.get("Value", "")
                is_overall = best.get("OverallFastest", False)
                lap_secs   = parse_lap_time(lap_str)
                if lap_secs and lap_secs > 0:
                    prev = state.best_laps.get(dn)
                    if prev is None or lap_secs < prev:
                        state.best_laps[dn] = lap_secs
                        if is_overall or (
                            state.overall_fastest is None
                            or lap_secs < state.overall_fastest
                        ):
                            state.overall_fastest = lap_secs
                            state.overall_fastest_driver = dn
                            acr = _resolve_driver(dn, state.driver_map)
                            if emit_events and self.on_fastest_lap:
                                await self.on_fastest_lap(
                                    acronym=acr,
                                    lap_time=lap_secs,
                                    lap_number=None,
                                    is_overall=True,
                                )

        # Overtake detection
        if new_positions:
            import time as _time
            now = _time.monotonic()
            # Clear recently_pitted entries older than 60s (position shuffle should be done)
            state.recently_pitted = {dn: ts for dn, ts in state.recently_pitted.items()
                                      if now - ts < 60}

            old = state.last_positions
            if old:
                for dn, new_pos in new_positions.items():
                    old_pos = old.get(dn)
                    if old_pos is None or new_pos >= old_pos:
                        continue
                    for other_dn, other_new_pos in new_positions.items():
                        if other_dn == dn:
                            continue
                        other_old = old.get(other_dn, other_new_pos)
                        if other_old == new_pos and other_new_pos == old_pos:
                            # Suppress if either driver recently pitted (position swap is pit-caused)
                            if dn in state.recently_pitted or other_dn in state.recently_pitted:
                                break
                            overtaker = _resolve_driver(dn, state.driver_map)
                            overtaken  = _resolve_driver(other_dn, state.driver_map)
                            if self.on_overtake:
                                await self.on_overtake(
                                    overtaker=overtaker,
                                    overtaken=overtaken,
                                    new_pos=new_pos,
                                    old_pos=old_pos,
                                    lap=None,
                                )
                            break
            state.last_positions.update(new_positions)

    # -- PitLaneTimeCollection --------------------------------------------------

    async def _process_pit_data(self, data: dict, state: SessionState) -> None:
        pit_times = data.get("PitTimes", {})
        if not isinstance(pit_times, dict):
            return
        for rn_str, info in pit_times.items():
            if not isinstance(info, dict):
                continue
            if info.get("InProgress", False):
                continue
            try:
                dn = int(rn_str)
            except ValueError:
                continue

            lap = info.get("Lap") or info.get("LapNumber")
            try:
                lap = int(lap) if lap is not None else None
            except (TypeError, ValueError):
                lap = None

            key = (dn, lap)
            if key in state.seen_pits:
                continue
            state.seen_pits.add(key)

            duration_raw = info.get("Duration") or info.get("PitTime")
            duration: float | None = None
            if duration_raw:
                try:
                    duration = float(duration_raw)
                except ValueError:
                    pass

            compound = state.current_tyre.get(rn_str, "UNKNOWN")
            state.pit_counts[dn] = state.pit_counts.get(dn, 0) + 1
            acr = _resolve_driver(dn, state.driver_map)
            # Mark as recently pitted to suppress fake overtake events for 60s
            import time as _time
            state.recently_pitted[dn] = _time.monotonic()

            if self.on_pit_stop:
                await self.on_pit_stop(
                    acronym=acr,
                    compound=compound,
                    pit_duration=duration,
                    lap_number=lap,
                    pit_count=state.pit_counts[dn],
                )

    # -- RaceControlMessages ----------------------------------------------------

    async def _process_rc_messages(self, data: dict, state: SessionState) -> None:
        messages = data.get("Messages", {})
        if isinstance(messages, dict):
            items = list(messages.values())
        elif isinstance(messages, list):
            items = messages
        else:
            return

        for msg in items:
            if not isinstance(msg, dict):
                continue
            text          = msg.get("Message", "")
            utc           = msg.get("Utc", "")
            category      = msg.get("Category", "")
            lap           = msg.get("Lap")
            flag          = msg.get("Flag", "")
            racing_number = msg.get("RacingNumber", "")
            scope         = msg.get("Scope", "")
            sector        = msg.get("Sector")
            key = f"{utc}:{text}"
            if not text or key in state.seen_rc:
                continue
            state.seen_rc.add(key)
            # Resolve driver acronym from racing number
            driver_acr = None
            if racing_number:
                try:
                    dn = int(racing_number)
                    driver_acr = _resolve_driver(dn, state.driver_map)
                except (ValueError, TypeError):
                    pass
            # Fallback: extract car number from message text e.g. "CAR 16 (LEC)" or "CAR 16"
            if not driver_acr:
                import re
                m = re.search(r'\bCAR\s+(\d+)', text, re.IGNORECASE)
                if m:
                    try:
                        dn = int(m.group(1))
                        driver_acr = _resolve_driver(dn, state.driver_map)
                    except (ValueError, TypeError):
                        pass
            # Fallback: extract acronym from parentheses e.g. "(LEC)"
            if not driver_acr:
                import re
                m = re.search(r'\(([A-Z]{2,3})\)', text)
                if m:
                    driver_acr = m.group(1)
            logger.debug("RC message raw: flag=%r scope=%r racing_number=%r driver=%r text=%r",
                         flag, scope, racing_number, driver_acr, text)
            if self.on_race_control:
                await self.on_race_control(
                    message=text,
                    lap_number=lap,
                    category=category,
                    flag=flag,
                    driver=driver_acr,
                    scope=scope,
                    sector=sector,
                )

    # -- TeamRadio --------------------------------------------------------------

    async def _process_team_radio(self, data: dict, state: SessionState) -> None:
        captures = data.get("Captures", [])
        if isinstance(captures, dict):
            captures = list(captures.values())
        if not isinstance(captures, list):
            return

        for capture in captures:
            if not isinstance(capture, dict):
                continue
            path = capture.get("Path", "")
            if not path:
                continue
            # Build correct URL: base + "static/" + session_path + path
            # e.g. https://livetiming.formula1.com/static/2026/.../TeamRadio/file.mp3
            if state.session_path and not path.startswith("static/"):
                url = AUDIO_BASE + "static/" + state.session_path + path
            else:
                url = AUDIO_BASE + path
            if url in state.seen_radio:
                continue
            state.seen_radio.add(url)

            rn_str = capture.get("RacingNumber", "")
            try:
                dn = int(rn_str)
            except (ValueError, TypeError):
                dn = None
            acr = _resolve_driver(dn, state.driver_map) if dn else rn_str
            utc = capture.get("Utc", "")

            if self.on_team_radio:
                await self.on_team_radio(
                    acronym=acr,
                    recording_url=url,
                    date=utc,
                    driver_number=dn,
                )

    # -- SessionStatus ----------------------------------------------------------

    async def _process_session_status(self, data: dict, state: SessionState) -> None:
        status = data.get("Status", "")
        logger.debug("SessionStatus: %s", status)
        # "Ends" fires at end of each Q segment (Q1/Q2) — do NOT trigger session end.
        # Only "Finalised" / "Finished" mean the entire session is done.
        if status in ("Finalised", "Finished"):
            logger.info("SessionStatus=%s -> triggering end", status)
            await self._trigger_session_end()
