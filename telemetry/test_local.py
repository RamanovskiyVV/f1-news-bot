"""
Local integration test — no live race needed.
Run: python -m telemetry.test_local [--bot]

Tests:
  1. All HTML formatters (no network)
  2. OpenF1 client against the latest known past session
  3. SessionTracker dry-run: prints events it would have sent
  4. Radio processor pipeline with a real OpenF1 audio URL
  5. (--bot flag) Bot startup + /status smoke test
"""
from __future__ import annotations

import asyncio
import sys

# ── 1. Formatters ──────────────────────────────────────────────────────────────

def test_formatters():
    from telemetry.formatter import (
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

    mock_session = {
        "session_name": "Race",
        "meeting_name": "Гран-при Монако",
        "date_start": "2026-05-24T13:00:00",
        "circuit_short_name": "Монте-Карло",
    }

    print("\n── 1. Formatters ─────────────────────────────────────")

    print("\n[session_start]")
    print(fmt_session_start(mock_session))

    print("\n[session_end]")
    print(fmt_session_end(mock_session))

    print("\n[overtake]")
    print(fmt_overtake("VER", "NOR", 1, 2, lap=14))

    print("\n[fastest_lap]")
    print(fmt_fastest_lap("NOR", 78.234, lap_number=22, is_overall=True))

    print("\n[pit_stop]")
    print(fmt_pit_stop("ALO", "MEDIUM", 2.4, lap_number=31, pit_count=2))

    print("\n[race_control — SC]")
    print(fmt_race_control("SAFETY CAR DEPLOYED", lap_number=5, category="SafetyCar"))

    print("\n[race_control — flag]")
    print(fmt_race_control("RED FLAG - ACCIDENT AT TURN 3", lap_number=12, category="Flag"))

    print("\n[team_radio]")
    print(fmt_team_radio(
        "VER",
        "I don't know what's happening with the car, something is very wrong",
        "Я не понимаю что происходит с машиной, что-то очень не так",
        lap_number=8,
    ))

    print("\n[race_results with spoiler]")
    mock_results = [
        {"Position": i, "Abbreviation": a, "BroadcastName": a, "Time": None if i == 1 else i * 4.2}
        for i, a in enumerate(["VER","NOR","LEC","HAM","RUS","ALO","SAI","PIA","TSU","GAS"], 1)
    ]
    mock_pit = {"fastest": {"acronym": "ALO", "duration": 2.1}, "total": 28}
    mock_standings = [
        {"Abbreviation": a, "Points": p}
        for a, p in [("VER",195),("NOR",178),("LEC",156),("HAM",134),("RUS",112)]
    ]
    print(fmt_race_results(mock_session, mock_results, mock_pit, mock_standings))

    print("\n[qualifying_results]")
    mock_q = {
        "Q3": [{"Abbreviation": "VER", "BroadcastName": "VER", "QualifyingTime": "1:10.457"}],
        "Q2": [{"Abbreviation": "NOR", "BroadcastName": "NOR", "QualifyingTime": "1:11.012"}],
    }
    mock_session_q = {**mock_session, "session_name": "Qualifying"}
    print(fmt_qualifying_results(mock_session_q, mock_q))

    print("\n[practice_top3]")
    mock_fp = [
        {"Position": 1, "Abbreviation": "LEC", "BroadcastName": "LEC", "BestLapTime": "1:12.345"},
        {"Position": 2, "Abbreviation": "HAM", "BroadcastName": "HAM", "BestLapTime": "1:12.567"},
        {"Position": 3, "Abbreviation": "VER", "BroadcastName": "VER", "BestLapTime": "1:12.789"},
    ]
    mock_session_fp = {**mock_session, "session_name": "Practice 1"}
    print(fmt_practice_top3(mock_session_fp, mock_fp))

    print("\n✅ All formatters OK")


# ── 2. OpenF1 client — latest past session ─────────────────────────────────────

async def test_openf1_client():
    from telemetry.openf1_client import OpenF1Client
    print("\n── 2. OpenF1 Client ──────────────────────────────────")

    async with OpenF1Client() as c:
        session = await c.get_latest_session()
        if not session:
            print("❌ Could not fetch latest session")
            return

        sk = session["session_key"]
        sname = session.get("session_name", "?")
        gp = session.get("meeting_name", "?")
        status = session.get("session_status", "?")
        print(f"✅ Latest session: {gp} — {sname} (key={sk}, status={status})")

        drivers = await c.get_drivers(sk)
        print(f"   Drivers loaded: {len(drivers)}")

        positions = await c.get_latest_positions(sk)
        print(f"   Position entries: {len(positions)}")

        pits = await c.get_pit_stops(sk)
        print(f"   Pit stops: {len(pits)}")

        rc = await c.get_race_control(sk)
        print(f"   Race control messages: {len(rc)}")
        if rc:
            print(f"   Last RC: {rc[-1].get('message', '')[:80]}")

        radio = await c.get_team_radio(sk)
        print(f"   Team radio entries: {len(radio)}")
        if radio:
            url = radio[0].get("recording_url", "")
            print(f"   First radio URL: {url[:80]}")
            return url  # return for radio test

    return None


# ── 3. SessionTracker dry-run ──────────────────────────────────────────────────

async def test_tracker():
    from telemetry.session_tracker import SessionTracker
    print("\n── 3. SessionTracker dry-run ─────────────────────────")

    events: list[str] = []

    tracker = SessionTracker()

    async def on_session_start(session): events.append(f"[SESSION START] {session.get('session_name')} — {session.get('meeting_name')}")
    async def on_session_end(session):   events.append(f"[SESSION END] {session.get('session_name')}")
    async def on_overtake(**kw):         events.append(f"[OVERTAKE] {kw['overtaker']} P{kw['old_pos']}→P{kw['new_pos']}")
    async def on_fastest_lap(**kw):      events.append(f"[FASTEST LAP] {kw['acronym']} {kw['lap_time']:.3f}s (overall={kw['is_overall']})")
    async def on_pit_stop(**kw):         events.append(f"[PIT] {kw['acronym']} → {kw['compound']} lap={kw['lap_number']} dur={kw['pit_duration']}")
    async def on_race_control(**kw):     events.append(f"[RC] {kw['message'][:60]}")
    async def on_team_radio(**kw):       events.append(f"[RADIO] {kw['acronym']} url={kw['recording_url'][:50]}")

    tracker.on_session_start = on_session_start
    tracker.on_session_end   = on_session_end
    tracker.on_overtake      = on_overtake
    tracker.on_fastest_lap   = on_fastest_lap
    tracker.on_pit_stop      = on_pit_stop
    tracker.on_race_control  = on_race_control
    tracker.on_team_radio    = on_team_radio

    await tracker.poll()

    if not events:
        print("   No events emitted (session may be inactive — that's OK)")
    else:
        for e in events[:20]:
            print(f"   {e}")
        if len(events) > 20:
            print(f"   ... and {len(events)-20} more")
    print(f"✅ Tracker poll completed, {len(events)} events")


# ── 4. Radio processor — real audio URL ───────────────────────────────────────

async def test_radio(url: str | None):
    print("\n── 4. Radio Processor ────────────────────────────────")
    if not url:
        print("   Skipped — no radio URL available from previous test")
        return

    from telemetry.radio_processor import process_radio
    from telemetry.formatter import fmt_team_radio
    print(f"   Testing URL: {url[:80]}")
    result = await process_radio(url, acronym="NOR")
    if result:
        print(f"   Original:   {result['original'][:100]}")
        print(f"   Translated: {result['translated'][:100]}")
        print(f"   Audio size: {len(result['audio_bytes'])} bytes")
        print(f"\n   Formatted message:\n{fmt_team_radio('NOR', result['original'], result['translated'], team='McLaren')}")
        print("✅ Radio pipeline OK")
    else:
        print("   Message was filtered out as 'not interesting' (also OK)")
        print("✅ Radio pipeline ran successfully")


# ── 5. Bot startup (optional, --bot flag) ─────────────────────────────────────

async def test_bot_startup():
    print("\n── 5. Bot Startup ────────────────────────────────────")
    from telemetry.config import TELEMETRY_BOT_TOKEN, TELEMETRY_CHANNEL_ID
    if not TELEMETRY_BOT_TOKEN or TELEMETRY_BOT_TOKEN == "your_telemetry_bot_token_here":
        print("   ⚠️  TELEMETRY_BOT_TOKEN not set — skipping bot test")
        return
    if not TELEMETRY_CHANNEL_ID or TELEMETRY_CHANNEL_ID == "@your_telemetry_channel_here":
        print("   ⚠️  TELEMETRY_CHANNEL_ID not set — skipping bot test")
        return

    from telemetry.bot import build_app
    app = build_app()
    await app.initialize()
    me = await app.bot.get_me()
    print(f"✅ Bot connected: @{me.username} ({me.first_name})")
    await app.shutdown()


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    run_bot = "--bot" in sys.argv

    # 1. Formatters (sync, wrapped)
    test_formatters()

    # 2–4. Async tests
    radio_url = await test_openf1_client()
    await test_tracker()
    await test_radio(radio_url)

    if run_bot:
        await test_bot_startup()
    else:
        print("\n💡 Tip: run with --bot flag to also test Telegram connection")

    print("\n─────────────────────────────────────────────────────")
    print("All tests done.")


if __name__ == "__main__":
    asyncio.run(main())
