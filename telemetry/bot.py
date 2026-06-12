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


async def _send_session_results(session: dict) -> bool:
    """Fetch and send FastF1 summary for the given session.
    Returns True if data was available and sent, False if no data yet."""
    sname = session.get("session_name", "")
    year_str = (session.get("date_start") or "")[:4]
    year = int(year_str) if year_str.isdigit() else 2026
    gp = session.get("meeting_name", "")
    if not gp or not sname:
        return False

    is_race   = sname == "Race"
    is_sprint = sname == "Sprint"
    is_quali  = "Qualifying" in sname
    is_fp     = any(sname.startswith(p) for p in ("Practice", "Free Practice", "FP"))

    if is_race or is_sprint:
        session_id = "Race" if is_race else "Sprint"
        results, pit_stats, standings = await _gather_race_data(year, gp, session_id)
        if results:
            await _send(fmt_race_results(session, results, pit_stats, standings))
            return True

    elif is_quali:
        if "Sprint" in sname:
            q_results = await ff1.get_sprint_qualifying_results(year, gp)
        else:
            q_results = await ff1.get_qualifying_results(year, gp)
        if q_results:
            await _send(fmt_qualifying_results(session, q_results))
            return True

    elif is_fp:
        fp_num = 1
        for n in (1, 2, 3):
            if str(n) in sname:
                fp_num = n
                break
        top3 = await ff1.get_practice_top3(year, gp, fp_num)
        if top3:
            await _send(fmt_practice_top3(session, top3))
            return True

    return False


async def _on_session_end(
    session: dict,
    live_laps: dict | None = None,
    driver_map: dict | None = None,
    live_positions: dict | None = None,
    race_gaps: dict | None = None,
    quali_q_times: dict | None = None,
    pit_counts: dict | None = None,
) -> None:
    if not session.get("meeting_name") or not session.get("session_name"):
        return

    sname = session.get("session_name", "")
    is_fp    = any(sname.startswith(p) for p in ("Practice", "Free Practice", "FP"))
    is_race  = sname in ("Race", "Sprint")
    is_quali = "Qualifying" in sname
    dmap = driver_map or {}

    # ── Practice: instant from SignalR lap times ─────────────────────────────
    if is_fp and live_laps:
        sorted_laps = sorted(live_laps.items(), key=lambda x: x[1])
        results = []
        for pos, (dn, lap_secs) in enumerate(sorted_laps, 1):
            acr = dmap.get(dn, str(dn))
            mins = int(lap_secs // 60)
            rem = lap_secs - mins * 60
            results.append({"Position": pos, "Abbreviation": acr, "BroadcastName": acr,
                             "BestLapTime": f"{mins}:{rem:06.3f}"})
        if results:
            await _send(fmt_practice_top3(session, results))
            return

    # ── Race / Sprint: instant from SignalR positions + gaps ─────────────────
    if is_race and live_positions:
        sorted_pos = sorted(live_positions.items(), key=lambda x: x[1])
        results = []
        for dn, pos in sorted_pos:
            acr = dmap.get(dn, str(dn))
            gap = (race_gaps or {}).get(dn, "")
            results.append({"Position": pos, "Abbreviation": acr, "BroadcastName": acr,
                             "Time": gap})
        if results:
            total_pits = sum((pit_counts or {}).values())
            pit_stats = {"total": total_pits} if total_pits else {}
            await _send(fmt_race_results(session, results, pit_stats, []))
            return

    # ── Qualifying: instant from SignalR Q1/Q2/Q3 times ─────────────────────
    if is_quali and quali_q_times:
        q_results: dict[str, list] = {"Q3": [], "Q2": [], "Q1": []}
        for dn, qtimes in quali_q_times.items():
            acr = dmap.get(dn, str(dn))
            for qlabel in ("Q3", "Q2", "Q1"):
                t = qtimes.get(qlabel)
                if t:
                    q_results[qlabel].append({"Abbreviation": acr, "BroadcastName": acr,
                                               "QualifyingTime": t})
                    break  # assign driver to best (highest) Q segment only
        for qlabel in ("Q3", "Q2", "Q1"):
            q_results[qlabel].sort(key=lambda r: r["QualifyingTime"])
        q_results = {k: v for k, v in q_results.items() if v}
        if q_results:
            await _send(fmt_qualifying_results(session, q_results))
            return

    # ── Fallback: FastF1 with retries ────────────────────────────────────────
    sent = await _send_session_results(session)
    if not sent:
        logger.info("FastF1 data not available yet for %s %s -- scheduling retries",
                    session.get("meeting_name"), session.get("session_name"))
        assert _app
        # Retry at 30, 60, 90, 120 minutes
        for delay_min in (30, 60, 90, 120):
            _app.job_queue.run_once(
                _retry_results_job,
                when=delay_min * 60,
                data=session,
                name=f"retry_results_{session.get('session_key', 'unknown')}_{delay_min}",
            )


async def _retry_results_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job queue callback: retry sending FastF1 results."""
    session = context.job.data
    if not session:
        return
    logger.info("Retrying FastF1 results for %s %s",
                session.get("meeting_name"), session.get("session_name"))
    sent = await _send_session_results(session)
    if sent:
        logger.info("Retry succeeded for %s %s",
                    session.get("meeting_name"), session.get("session_name"))
        # Cancel remaining retries for this session
        name_prefix = f"retry_results_{session.get('session_key', 'unknown')}"
        assert _app
        for job in _app.job_queue.get_jobs_by_name(""):
            if hasattr(job, 'name') and job.name and job.name.startswith(name_prefix):
                job.schedule_removal()


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
        text = "📡 <b>Телеметрия</b>\n\nСессий ещё не обнаружено."
    elif state.ended:
        text = (
            f"📡 <b>Телеметрия</b>\n\n"
            f"Последняя сессия: <b>{state.session_name}</b>\n"
            f"Гран-при: <b>{state.meeting_name}</b>\n"
            f"Статус: ✅ Завершена\n\n"
            f"<i>Ожидание следующей сессии...</i>"
        )
    elif state.started:
        text = (
            f"📡 <b>Телеметрия</b>\n\n"
            f"Сессия: <b>{state.session_name}</b>\n"
            f"Гран-при: <b>{state.meeting_name}</b>\n"
            f"Статус: 🟢 Активна\n"
            f"Пит-стопов обнаружено: {sum(state.pit_counts.values())}\n"
            f"Race control сообщений: {len(state.seen_rc)}\n"
            f"Team radio обработано: {len(state.seen_radio)}"
        )
    else:
        text = (
            f"📡 <b>Телеметрия</b>\n\n"
            f"Следующая сессия: <b>{state.session_name}</b>\n"
            f"Гран-при: <b>{state.meeting_name}</b>\n"
            f"Статус: ⏳ Ещё не началась"
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


async def cmd_results(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger FastF1 results for the last detected session."""
    state = _tracker.current_session
    if state is None or not state.session_doc:
        await update.message.reply_text("❌ Нет информации о сессии.")
        return

    session = state.session_doc
    gp   = session.get("meeting_name", "")
    name = session.get("session_name", "")
    if not gp or not name:
        await update.message.reply_text("❌ Неизвестная сессия (данных нет).")
        return

    await update.message.reply_text(f"⏳ Запрашиваю результаты {gp} — {name}...")
    try:
        sent = await _send_session_results(session)
        if sent:
            await update.message.reply_text("✅ Результаты отправлены в канал.")
        else:
            await update.message.reply_text(
                "⚠️ Данные FastF1 ещё не доступны. "
                "Попробуй ещё раз через 30–60 минут после сессии."
            )
    except Exception as e:
        logger.exception("cmd_results failed")
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
    _app.add_handler(CommandHandler("results", cmd_results))

    # Telegram UI menu
    async def post_init(app):
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("status",   "📡 Статус трекера и текущей сессии"),
            BotCommand("schedule", "🗓 Расписание ближайшего уикенда"),
            BotCommand("results",  "🏁 Запросить итоги последней сессии"),
        ])

    _app.post_init = post_init

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
