"""FastF1 wrapper for post-session summaries.

FastF1 loads sessions from the Ergast / official F1 API cache.
We run all blocking FastF1 calls in a thread pool to keep the
event loop free.
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fastf1")


def _run_sync(fn, *args, **kwargs):
    """Run a blocking function in the thread pool."""
    loop = asyncio.get_event_loop()
    return loop.run_in_executor(_executor, partial(fn, *args, **kwargs))


# ── FastF1 helpers ─────────────────────────────────────────────────────────────

def _load_session_sync(year: int, gp: str | int, session_identifier: str):
    import fastf1
    fastf1.Cache.enable_cache("/tmp/fastf1_cache")
    sess = fastf1.get_session(year, gp, session_identifier)
    sess.load(telemetry=False, weather=False, messages=False)
    return sess


# ── Public async API ───────────────────────────────────────────────────────────

async def get_race_results(year: int, gp: str | int) -> list[dict]:
    """Return top-10+ results for a Race session."""
    return await _run_sync(_get_race_results_sync, year, gp)


async def get_sprint_results(year: int, gp: str | int) -> list[dict]:
    return await _run_sync(_get_race_results_sync, year, gp, "Sprint")


async def get_qualifying_results(year: int, gp: str | int) -> dict[str, list[dict]]:
    """Returns {'Q1': [...], 'Q2': [...], 'Q3': [...]}"""
    return await _run_sync(_get_quali_results_sync, year, gp, "Qualifying")


async def get_sprint_qualifying_results(year: int, gp: str | int) -> dict[str, list[dict]]:
    return await _run_sync(_get_quali_results_sync, year, gp, "Sprint Qualifying")


async def get_practice_top3(year: int, gp: str | int, fp_number: int) -> list[dict]:
    session_map = {1: "FP1", 2: "FP2", 3: "FP3"}
    fp_id = session_map.get(fp_number, f"FP{fp_number}")
    return await _run_sync(_get_practice_sync, year, gp, fp_id)


async def get_pit_stats(year: int, gp: str | int, session_identifier: str = "Race") -> dict:
    return await _run_sync(_get_pit_stats_sync, year, gp, session_identifier)


async def get_driver_standings(year: int) -> list[dict]:
    return await _run_sync(_get_driver_standings_sync, year)


# ── Sync implementations ───────────────────────────────────────────────────────

def _get_race_results_sync(year: int, gp: str | int, session_identifier: str = "Race") -> list[dict]:
    try:
        sess = _load_session_sync(year, gp, session_identifier)
        results = sess.results
        out = []
        for _, row in results.iterrows():
            out.append({
                "Position": int(row.get("Position", 0)),
                "Abbreviation": str(row.get("Abbreviation", "???")),
                "BroadcastName": str(row.get("BroadcastName", row.get("Abbreviation", "???"))),
                "TeamName": str(row.get("TeamName", "")),
                "Time": row.get("Time"),
                "Status": str(row.get("Status", "")),
                "Points": float(row.get("Points", 0)),
            })
        out.sort(key=lambda r: r["Position"])
        return out
    except Exception:
        logger.exception("FastF1 get_race_results_sync failed")
        return []


def _get_quali_results_sync(year: int, gp: str | int, session_identifier: str = "Qualifying") -> dict[str, list]:
    try:
        sess = _load_session_sync(year, gp, session_identifier)
        results = sess.results
        out: dict[str, list] = {"Q1": [], "Q2": [], "Q3": []}
        for _, row in results.iterrows():
            acr = str(row.get("Abbreviation", "???"))
            broadcast = str(row.get("BroadcastName", acr))
            for q in ("Q1", "Q2", "Q3"):
                t = row.get(q)
                if t and str(t) not in ("NaT", "None", "nan"):
                    if hasattr(t, "total_seconds"):
                        secs = t.total_seconds()
                        mins = int(secs // 60)
                        rem = secs - mins * 60
                        time_str = f"{mins}:{rem:06.3f}"
                    else:
                        time_str = str(t)
                    out[q].append({
                        "Abbreviation": acr,
                        "BroadcastName": broadcast,
                        "QualifyingTime": time_str,
                    })
        # Sort each group by Q time (they come in driver order, re-sort by time)
        for q in out:
            out[q].sort(key=lambda r: r["QualifyingTime"])
        # Remove empty Q rounds
        return {k: v for k, v in out.items() if v}
    except Exception:
        logger.exception("FastF1 get_quali_results_sync failed")
        return {}


def _get_practice_sync(year: int, gp: str | int, fp_id: str) -> list[dict]:
    try:
        sess = _load_session_sync(year, gp, fp_id)
        results = sess.results
        out = []
        for _, row in results.iterrows():
            t = row.get("Time") or row.get("Q1")
            if hasattr(t, "total_seconds"):
                secs = t.total_seconds()
                mins = int(secs // 60)
                rem = secs - mins * 60
                time_str = f"{mins}:{rem:06.3f}"
            else:
                time_str = str(t) if t else "—"
            out.append({
                "Position": int(row.get("Position", 0)),
                "Abbreviation": str(row.get("Abbreviation", "???")),
                "BroadcastName": str(row.get("BroadcastName", row.get("Abbreviation", "???"))),
                "BestLapTime": time_str,
            })
        out.sort(key=lambda r: r["Position"])
        return out[:3]
    except Exception:
        logger.exception("FastF1 get_practice_sync failed")
        return []


def _get_pit_stats_sync(year: int, gp: str | int, session_identifier: str) -> dict:
    try:
        import fastf1
        fastf1.Cache.enable_cache("/tmp/fastf1_cache")
        sess = fastf1.get_session(year, gp, session_identifier)
        sess.load(telemetry=False, weather=False, messages=False)
        laps = sess.laps
        pit_laps = laps[laps["PitInTime"].notna()].copy()

        fastest_pit = None
        fastest_dur = None
        total = len(pit_laps)

        for _, row in pit_laps.iterrows():
            pit_in = row.get("PitInTime")
            pit_out = row.get("PitOutTime")
            if pit_in is not None and pit_out is not None:
                try:
                    dur = (pit_out - pit_in).total_seconds()
                    if dur > 0 and (fastest_dur is None or dur < fastest_dur):
                        fastest_dur = dur
                        acr = str(row.get("Abbreviation", "???"))
                        fastest_pit = {"acronym": acr, "duration": round(dur, 1)}
                except Exception:
                    pass

        return {"fastest": fastest_pit, "total": total}
    except Exception:
        logger.exception("FastF1 get_pit_stats_sync failed")
        return {}


def _get_driver_standings_sync(year: int) -> list[dict]:
    try:
        # FastF1 ergast interface
        import fastf1.ergast as ergast
        standings = ergast.get_driver_standings(season=year)
        if standings.content and len(standings.content) > 0:
            table = standings.content[0]
            out = []
            for _, row in table.iterrows():
                out.append({
                    "Position": int(row.get("position", 0)),
                    "Abbreviation": str(row.get("driverCode", "???")),
                    "Points": float(row.get("points", 0)),
                    "Wins": int(row.get("wins", 0)),
                })
            out.sort(key=lambda r: r["Position"])
            return out[:10]
        return []
    except Exception:
        logger.exception("FastF1 get_driver_standings_sync failed")
        return []
