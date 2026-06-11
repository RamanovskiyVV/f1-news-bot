"""Fetch and format the F1 weekend schedule."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import OPENF1_BASE_URL

_TZ_MSK = ZoneInfo("Europe/Moscow")   # UTC+3, no DST (Minsk same)
_TZ_CET = ZoneInfo("Europe/Paris")    # CET/CEST with DST

_DAYS_RU = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
_MONTHS_RU = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]

_SESSION_LABELS: dict[str, tuple[str, str]] = {
    # (emoji, Russian name)
    "Practice 1":          ("🏎", "Свободная практика 1"),
    "Practice 2":          ("🏎", "Свободная практика 2"),
    "Practice 3":          ("🏎", "Свободная практика 3"),
    "Free Practice 1":     ("🏎", "Свободная практика 1"),
    "Free Practice 2":     ("🏎", "Свободная практика 2"),
    "Free Practice 3":     ("🏎", "Свободная практика 3"),
    "Qualifying":          ("⏱", "Квалификация"),
    "Sprint Qualifying":   ("⏱", "Спринт-квалификация"),
    "Sprint":              ("🏎", "Спринт"),
    "Race":                ("🏎", "Гонка"),
}


def _fmt_date_header(dt: datetime) -> str:
    """'Пятница, 5 июня' — bold."""
    day = _DAYS_RU[dt.weekday()]
    return f"<b>{day}, {dt.day} {_MONTHS_RU[dt.month]}:</b>"


def _fmt_times(dt_utc: datetime) -> str:
    """'14:00 МСК  ·  13:00 CET'"""
    msk = dt_utc.astimezone(_TZ_MSK)
    cet = dt_utc.astimezone(_TZ_CET)
    cet_label = "CEST" if cet.utcoffset().total_seconds() == 7200 else "CET"
    return f"{msk.strftime('%H:%M')} МСК  ·  {cet.strftime('%H:%M')} {cet_label}"


async def get_schedule_message(meeting_key: int | None = None) -> str:
    """
    Returns a formatted HTML schedule for the next/current GP weekend.
    If meeting_key is None, auto-detects next or current meeting.
    """
    async with httpx.AsyncClient(base_url=OPENF1_BASE_URL, timeout=20) as client:
        # Find the meeting
        if meeting_key is None:
            r = await client.get("/meetings", params={"year": datetime.now().year})
            r.raise_for_status()
            meetings = r.json()
            now = datetime.now(timezone.utc)
            target = None
            for m in meetings:
                try:
                    date_end = datetime.fromisoformat(m["date_end"])
                    date_start = datetime.fromisoformat(m["date_start"])
                except (KeyError, ValueError):
                    continue
                # Current or next meeting
                if date_end >= now:
                    target = m
                    break
            if target is None:
                return "Расписание недоступно — сезон завершён."
        else:
            r = await client.get("/meetings", params={"meeting_key": meeting_key})
            r.raise_for_status()
            data = r.json()
            if not data:
                return "Встреча не найдена."
            target = data[0]

        # Fetch sessions
        r2 = await client.get("/sessions", params={"meeting_key": target["meeting_key"]})
        r2.raise_for_status()
        sessions = r2.json()

    if not sessions:
        return "Сессии для этого уикенда ещё не опубликованы."

    # Sort by start time
    sessions.sort(key=lambda s: s.get("date_start", ""))

    # Build header
    gp_name = target.get("meeting_name", "Гран-при")
    circuit = target.get("circuit_short_name", "")
    country = target.get("country_name", "")
    location_str = f"📍 {circuit}" + (f"  ·  {country}" if country and country != circuit else "")

    # Date range
    try:
        d_start = datetime.fromisoformat(sessions[0]["date_start"]).astimezone(_TZ_MSK)
        d_end = datetime.fromisoformat(sessions[-1]["date_start"]).astimezone(_TZ_MSK)
        if d_start.month == d_end.month:
            date_range = f"{d_start.day}–{d_end.day} {_MONTHS_RU[d_start.month]}"
        else:
            date_range = f"{d_start.day} {_MONTHS_RU[d_start.month]} – {d_end.day} {_MONTHS_RU[d_end.month]}"
    except Exception:
        date_range = ""

    lines = [
        f"🗓 <b>{gp_name}</b>",
        location_str,
        f"<i>{date_range}</i>" if date_range else "",
    ]
    lines = [l for l in lines if l]
    lines.append("")

    # Group sessions by day
    from collections import defaultdict
    days: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        try:
            dt = datetime.fromisoformat(s["date_start"]).astimezone(_TZ_MSK)
            day_key = dt.strftime("%Y-%m-%d")
            days[day_key].append((dt, s))
        except Exception:
            continue

    for day_key in sorted(days.keys()):
        day_sessions = days[day_key]
        dt_day = day_sessions[0][0]
        lines.append(_fmt_date_header(dt_day))

        for dt_start, s in day_sessions:
            sname = s.get("session_name", "")
            emoji, label = _SESSION_LABELS.get(sname, ("🏎", sname))
            lines.append(f"{emoji}  <i>{label}</i>")
            lines.append(_fmt_times(dt_start))
            lines.append("")

    return "\n".join(lines).rstrip()
