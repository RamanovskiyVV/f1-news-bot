"""HTML message formatters for every telemetry event type.

All public functions return a str with Telegram HTML markup.
Telegram supports: <b>, <i>, <u>, <s>, <a>, <code>, <pre>,
                   <tg-spoiler>, <blockquote>
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import (
    DRIVERS,
    POSITION_MEDALS,
    RC_KEYWORDS,
    TEAM_NAMES,
    TYRE_EMOJI,
    driver_label,
)

# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_lap_time(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}:{s:06.3f}"


def _fmt_gap(seconds: float | None) -> str:
    if seconds is None:
        return "+?"
    if seconds == 0:
        return "+0.000"
    return f"+{seconds:.3f}"


def _tyre(compound: str | None) -> str:
    c = (compound or "UNKNOWN").upper()
    em = TYRE_EMOJI.get(c, "⚫")
    return f"{em} {c.capitalize()}"


def _separator(char: str = "─", n: int = 20) -> str:
    return char * n


def _rc_emoji(message: str) -> str:
    msg_upper = message.upper()
    for kw, emoji in RC_KEYWORDS.items():
        if kw in msg_upper:
            return emoji
    return "📢"


# ── Session events ─────────────────────────────────────────────────────────────

def fmt_session_start(session: dict) -> str:
    stype = session.get("session_name", "Сессия")
    gp = session.get("meeting_name", "")
    year = ""
    if session.get("date_start"):
        year = " " + session["date_start"][:4]
    circuit = session.get("circuit_short_name", "")

    type_line = {
        "Race":        "🚦 <b>ГОНКА НАЧАЛАСЬ!</b>",
        "Sprint":      "🚦 <b>СПРИНТ НАЧАЛСЯ!</b>",
        "Qualifying":  "⏱ <b>КВАЛИФИКАЦИЯ НАЧАЛАСЬ!</b>",
        "Sprint Qualifying": "⏱ <b>СПРИНТ-КВАЛИ НАЧАЛАСЬ!</b>",
    }.get(stype, f"🏎 <b>{stype.upper()} НАЧАЛАСЬ!</b>")

    lines = [
        type_line,
        f"<b>{gp}{year}</b>",
    ]
    if circuit:
        lines.append(f"📍 {circuit}")
    return "\n".join(lines)


def fmt_session_end(session: dict) -> str:
    stype = session.get("session_name", "Сессия")
    gp = session.get("meeting_name", "")
    year = ""
    if session.get("date_start"):
        year = " " + session["date_start"][:4]

    type_line = {
        "Race":        "🏁 <b>ФИНИШ — ГОНКА ЗАВЕРШЕНА</b>",
        "Sprint":      "🏁 <b>ФИНИШ — СПРИНТ ЗАВЕРШЁН</b>",
        "Qualifying":  "🏁 <b>ФИНИШ — КВАЛИФИКАЦИЯ</b>",
        "Sprint Qualifying": "🏁 <b>ФИНИШ — СПРИНТ-КВАЛИ</b>",
    }.get(stype, f"🏁 <b>{stype.upper()} ЗАВЕРШЕНА</b>")

    return f"{type_line}\n<b>{gp}{year}</b>"


# ── Live events ────────────────────────────────────────────────────────────────

def fmt_overtake(
    overtaker: str,
    overtaken: str,
    new_pos: int,
    old_pos: int,
    lap: int | None,
) -> str:
    lap_str = f"  ·  Круг {lap}" if lap else ""
    medal = POSITION_MEDALS.get(new_pos, f"P{new_pos}")
    lines = [
        f"🔄 <b>ОБГОН!</b>{lap_str}",
        "",
        f"{driver_label(overtaker)} <b>P{old_pos} → {medal}</b>",
        f"обошёл {driver_label(overtaken)}",
    ]
    return "\n".join(lines)


def fmt_fastest_lap(
    acronym: str,
    lap_time: float,
    lap_number: int | None,
    is_overall: bool = False,
) -> str:
    lap_str = f"  ·  Круг {lap_number}" if lap_number else ""
    tag = "  ·  <i>🏆 Абсолютный рекорд гонки</i>" if is_overall else ""
    lines = [
        f"⚡ <b>БЫСТРЫЙ КРУГ</b>{lap_str}",
        "",
        f"{driver_label(acronym)}  <code>{_fmt_lap_time(lap_time)}</code>{tag}",
    ]
    return "\n".join(lines)


def fmt_pit_stop(
    acronym: str,
    compound: str | None,
    pit_duration: float | None,
    lap_number: int | None,
    pit_count: int | None = None,
) -> str:
    lap_str = f"  ·  Круг {lap_number}" if lap_number else ""
    dur_str = f"  ·  <code>{pit_duration:.1f}с</code>" if pit_duration else ""
    tyre_str = _tyre(compound)
    count_str = f"  (стоп №{pit_count})" if pit_count else ""
    lines = [
        f"🔧 <b>ПИТ-СТОП</b>{lap_str}",
        "",
        f"{driver_label(acronym)}{count_str}",
        f"→  {tyre_str}{dur_str}",
    ]
    return "\n".join(lines)


def fmt_race_control(message: str, lap_number: int | None, category: str | None) -> str:
    emoji = _rc_emoji(message)
    lap_str = f"  ·  Круг {lap_number}" if lap_number else ""
    cat_str = f"<i>{category}</i>\n" if category and category.upper() != "OTHER" else ""
    return f"{emoji} <b>ДИРЕКЦИЯ</b>{lap_str}\n\n{cat_str}{message}"


def fmt_team_radio(
    acronym: str,
    original_text: str,
    translated_text: str,
    lap_number: int | None = None,
    team: str | None = None,
) -> str:
    lap_str = f"  ·  Круг {lap_number}" if lap_number else ""
    d = DRIVERS.get(acronym.upper())
    team_name = team or (TEAM_NAMES.get(d["team"], "") if d else "")
    team_str = f"  ·  <i>{team_name}</i>" if team_name else ""
    lines = [
        f"📻 <b>TEAM RADIO</b>{lap_str}",
        "",
        f"{driver_label(acronym)}{team_str}",
        "",
        f'🇬🇧 <i>"{original_text}"</i>',
        "",
        f'🇷🇺 <i>«{translated_text}»</i>',
    ]
    return "\n".join(lines)


# ── Post-session summaries ─────────────────────────────────────────────────────

def fmt_race_results(
    session: dict,
    results: list[dict],
    pit_stats: dict,
    standings: list[dict],
) -> str:
    """Full race/sprint results with spoiler."""
    stype = session.get("session_name", "Race")
    gp = session.get("meeting_name", "")
    year = session.get("date_start", "")[:4]
    is_sprint = "Sprint" in stype and "Qualifying" not in stype

    header_emoji = "🏆" if not is_sprint else "⚡"
    header_label = "ИТОГИ СПРИНТА" if is_sprint else "ИТОГИ ГОНКИ"

    header = f"{header_emoji} <b>{header_label}</b>\n<b>{gp} {year}</b>"

    # Results block
    result_lines: list[str] = []
    for i, r in enumerate(results[:10], 1):
        acr = r.get("BroadcastName", r.get("Abbreviation", "???")).upper()
        time_val = r.get("Time", r.get("gap", ""))
        if i == 1:
            time_str = "<b>Победитель</b>"
        else:
            time_str = f"+{_fmt_timedelta(time_val)}" if time_val else "—"
        medal = POSITION_MEDALS.get(i, f"P{i} ·")
        result_lines.append(f"{medal} {driver_label(acr)}  {time_str}")

    # Pit stats
    pit_lines: list[str] = []
    if pit_stats.get("fastest"):
        p = pit_stats["fastest"]
        acr = p.get("acronym", "")
        dur = p.get("duration")
        pit_lines.append(f"🏆 Лучший пит: {driver_label(acr)} <code>{dur:.1f}с</code>")
    if pit_stats.get("total"):
        pit_lines.append(f"🔧 Всего стопов: {pit_stats['total']}")

    # Championship
    standings_lines: list[str] = []
    for i, s in enumerate(standings[:5], 1):
        acr = s.get("Abbreviation", s.get("acronym", "???")).upper()
        pts = s.get("Points", s.get("points", "?"))
        standings_lines.append(f"{i}. {driver_label(acr)}  {pts} очк.")

    sep = _separator()
    spoiler_parts: list[str] = []
    spoiler_parts.append("\n".join(result_lines))
    if pit_lines:
        spoiler_parts.append(f"\n{sep}\n" + "\n".join(pit_lines))
    if standings_lines:
        spoiler_parts.append(f"\n{sep}\n🏆 <b>ЧЕМПИОНАТ</b>\n" + "\n".join(standings_lines))

    spoiler_content = "".join(spoiler_parts)
    return f"{header}\n\n<tg-spoiler>{spoiler_content}</tg-spoiler>"


def fmt_qualifying_results(session: dict, q_results: dict[str, list[dict]]) -> str:
    """Qualifying results — no spoiler."""
    gp = session.get("meeting_name", "")
    year = session.get("date_start", "")[:4]
    stype = session.get("session_name", "Qualifying")
    sprint_tag = "СПРИНТ-" if "Sprint" in stype else ""

    header = f"📋 <b>{sprint_tag}КВАЛИФИКАЦИЯ</b>\n<b>{gp} {year}</b>"

    blocks: list[str] = []
    # Show Q3/Q2/Q1 in order
    for label in ("Q3", "Q2", "Q1"):
        entries = q_results.get(label)
        if not entries:
            continue
        lines = [f"\n<b>{label}</b>"]
        for i, r in enumerate(entries, 1):
            acr = r.get("BroadcastName", r.get("Abbreviation", "???")).upper()
            t = r.get("QualifyingTime", r.get("time", "—"))
            medal = POSITION_MEDALS.get(i, f"{i}.")
            lines.append(f"{medal} {driver_label(acr)}  <code>{t}</code>")
        blocks.append("\n".join(lines))

    return header + "".join(blocks)


def fmt_practice_top3(session: dict, top3: list[dict]) -> str:
    """Practice session top-3 — no spoiler."""
    stype = session.get("session_name", "Practice")
    gp = session.get("meeting_name", "")
    year = session.get("date_start", "")[:4]

    header = f"🕐 <b>{stype.upper()}</b>\n<b>{gp} {year}</b>"
    lines: list[str] = []
    for i, r in enumerate(top3[:3], 1):
        acr = r.get("BroadcastName", r.get("Abbreviation", "???")).upper()
        t = r.get("BestLapTime", r.get("time", "—"))
        medal = POSITION_MEDALS.get(i, f"{i}.")
        lines.append(f"{medal} {driver_label(acr)}  <code>{t if isinstance(t, str) else _fmt_lap_time(t)}</code>")
    return header + "\n\n" + "\n".join(lines)


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fmt_timedelta(val: Any) -> str:
    """Format a timedelta or float-seconds gap string."""
    if val is None:
        return "—"
    if hasattr(val, "total_seconds"):
        v = val.total_seconds()
    else:
        try:
            v = float(str(val).lstrip("+"))
        except (ValueError, TypeError):
            return str(val)
    if v >= 60:
        m = int(v // 60)
        s = v - m * 60
        return f"{m}:{s:06.3f}"
    return f"{v:.3f}"
