"""Telegram bot for the F1 telemetry channel."""
from __future__ import annotations

import io
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from .config import (
    TELEMETRY_BOT_TOKEN,
    TELEMETRY_CHANNEL_ID,
    TELEMETRY_POLL_INTERVAL,
)
from . import fastf1_client as ff1
from .formatter import (
    fmt_fastest_lap,
    fmt_overtake,
    fmt_pit_stop,
    fmt_practice_top3,
    fmt_qualifying_results,
    fmt_race_control,
    fmt_race_results,
    fmt_session_end,
    fmt_session_start,
    fmt_team_radio,
)
from .radio_processor import process_radio
from .schedule import get_schedule_message
from .session_tracker import PRACTICE_SESSION_TYPES, SessionTracker

logger = logging.getLogger(__name__)

# ── Bot application singleton ref (set in build_app) ──────────────────────────
_app: Application | None = None
_tracker = SessionTracker()


# ── Helpers ────────────────────────────────────────────────────────────────────

async def _send(text: str, **kwargs) -> None:
    """Send a HTML message to the telemetry channel."""
    assert _app
    try:
        await _app.bot.send_message(
            chat_id=TELEMETRY_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            **kwargs,
        )
    except Exception:
        logger.exception("Failed to send message to channel")


async def _send_voice(audio_bytes: bytes, caption: str) -> int | None:
    """Send a voice message; returns message_id."""
    assert _app
    try:
        buf = io.BytesIO(audio_bytes)
        buf.name = "radio.ogg"
        msg = await _app.bot.send_voice(
            chat_id=TELEMETRY_CHANNEL_ID,
            voice=buf,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
        return msg.message_id
    except Exception:
        logger.exception("Failed to send voice message")
        return None


# ── Event callbacks ────────────────────────────────────────────────────────────

async def _on_session_start(session: dict) -> None:
    text = fmt_session_start(session)
    await _send(text)


async def _on_session_end(session: dict) -> None:
    # 1. End-of-session header
    await _send(fmt_session_end(session))

    sname = session.get("session_name", "")
    year_str = (session.get("date_start") or "")[:4]
    year = int(year_str) if year_str.isdigit() else 2026
    gp = session.get("meeting_name", "")
    if not gp:
        return

    # 2. Dispatch to the right summary fetcher
    is_race   = sname == "Race"
    is_sprint = sname == "Sprint"
    is_quali  = "Qualifying" in sname
    is_fp     = any(sname.startswith(p) for p in ("Practice", "Free Practice", "FP"))

    if is_race or is_sprint:
        session_id = "Race" if is_race else "Sprint"
        results, pit_stats, standings = await _gather_race_data(year, gp, session_id)
        if results:
            text = fmt_race_results(session, results, pit_stats, standings)
            await _send(text)

    elif is_quali:
        if "Sprint" in sname:
            q_results = await ff1.get_sprint_qualifying_results(year, gp)
        else:
            q_results = await ff1.get_qualifying_results(year, gp)
        if q_results:
            text = fmt_qualifying_results(session, q_results)
            await _send(text)

    elif is_fp:
        fp_num = 1
        for n in (1, 2, 3):
            if str(n) in sname:
                fp_num = n
                break
        top3 = await ff1.get_practice_top3(year, gp, fp_num)
        if top3:
            text = fmt_practice_top3(session, top3)
            await _send(text)


async def _gather_race_data(year: int, gp: str, session_id: str):
    import asyncio
    results, pit_stats, standings = await asyncio.gather(
        ff1.get_race_results(year, gp) if session_id == "Race" else ff1.get_sprint_results(year, gp),
        ff1.get_pit_stats(year, gp, session_id),
        ff1.get_driver_standings(year),
    )
    return results, pit_stats, standings


async def _on_overtake(
    overtaker: str,
    overtaken: str,
    new_pos: int,
    old_pos: int,
    lap: int | None,
) -> None:
    text = fmt_overtake(overtaker, overtaken, new_pos, old_pos, lap)
    await _send(text)


async def _on_fastest_lap(
    acronym: str,
    lap_time: float,
    lap_number: int | None,
    is_overall: bool,
) -> None:
    text = fmt_fastest_lap(acronym, lap_time, lap_number, is_overall)
    await _send(text)


async def _on_pit_stop(
    acronym: str,
    compound: str | None,
    pit_duration: float | None,
    lap_number: int | None,
    pit_count: int | None,
) -> None:
    text = fmt_pit_stop(acronym, compound, pit_duration, lap_number, pit_count)
    await _send(text)


async def _on_race_control(
    message: str,
    lap_number: int | None,
    category: str | None,
) -> None:
    text = fmt_race_control(message, lap_number, category)
    await _send(text)


async def _on_team_radio(
    acronym: str,
    recording_url: str,
    date: str,
    driver_number: int | None,
) -> None:
    from .config import DRIVERS, TEAM_NAMES
    result = await process_radio(recording_url, acronym)
    if not result:
        return

    d = DRIVERS.get(acronym.upper())
    team_name = TEAM_NAMES.get(d["team"], "") if d else ""

    # Send voice first
    flag = d["flag"] if d else ""
    voice_caption = f"📻 <b>TEAM RADIO</b>  ·  {flag} {acronym}" + (f"  ·  <i>{team_name}</i>" if team_name else "")
    voice_msg_id = await _send_voice(result["audio_bytes"], voice_caption)

    # Then send text with translation as reply to the voice
    text = fmt_team_radio(acronym, result["original"], result["translated"], team=team_name)
    kwargs = {}
    if voice_msg_id:
        kwargs["reply_to_message_id"] = voice_msg_id
    await _send(text, **kwargs)


# ── Job ────────────────────────────────────────────────────────────────────────

async def job_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _tracker.poll()


# ── Commands ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _tracker.current_session
    if state is None:
        text = "📡 <b>Телеметрия</b>\n\nАктивных сессий нет."
    else:
        status = "🟢 Активна" if state.started and not state.ended else "⏳ Ожидание"
        text = (
            f"📡 <b>Телеметрия</b>\n\n"
            f"Сессия: <b>{state.session_name}</b>\n"
            f"Гран-при: <b>{state.meeting_name}</b>\n"
            f"Статус: {status}\n"
            f"Пит-стопов обнаружено: {sum(state.pit_counts.values())}\n"
            f"Race control сообщений: {len(state.seen_rc)}\n"
            f"Team radio обработано: {len(state.seen_radio)}"
        )
    await update.message.reply_html(text)


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the upcoming weekend schedule to the channel."""
    assert _app
    try:
        text = await get_schedule_message()
        await _app.bot.send_message(
            chat_id=TELEMETRY_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text("✅ Расписание отправлено в канал")
    except Exception as e:
        logger.exception("cmd_schedule failed")
        await update.message.reply_text(f"❌ Ошибка: {e}")


# ── App builder ────────────────────────────────────────────────────────────────

def build_app() -> Application:
    global _app

    # Wire tracker callbacks
    _tracker.on_session_start = _on_session_start
    _tracker.on_session_end   = _on_session_end
    _tracker.on_overtake      = _on_overtake
    _tracker.on_fastest_lap   = _on_fastest_lap
    _tracker.on_pit_stop      = _on_pit_stop
    _tracker.on_race_control  = _on_race_control
    _tracker.on_team_radio    = _on_team_radio

    _app = (
        Application.builder()
        .token(TELEMETRY_BOT_TOKEN)
        .build()
    )

    # Commands
    _app.add_handler(CommandHandler("status", cmd_status))
    _app.add_handler(CommandHandler("schedule", cmd_schedule))

    # Job queue for polling
    _app.job_queue.run_repeating(
        job_poll,
        interval=TELEMETRY_POLL_INTERVAL,
        first=10,
        name="telemetry_poll",
    )

    logger.info(
        "Telemetry bot built. Polling every %ds → channel %s",
        TELEMETRY_POLL_INTERVAL,
        TELEMETRY_CHANNEL_ID,
    )
    return _app
