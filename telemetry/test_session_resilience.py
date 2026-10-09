"""Regression tests for delayed races and incomplete live snapshots."""
from __future__ import annotations

import sys
import asyncio
import types
import unittest

try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from .session_tracker import SessionState, SessionTracker


class _RunningTask:
    @staticmethod
    def done() -> bool:
        return False


class _OpenF1ShouldNotBeCalled:
    def __init__(self) -> None:
        self.called = False

    async def get_latest_session(self):
        self.called = True
        return {
            "session_key": 999,
            "session_name": "Practice 1",
            "meeting_name": "Wrong GP",
        }


class _LiveState:
    def __init__(self, state: dict) -> None:
        self.state = state

    def get_state(self, topic: str):
        return self.state.get(topic)


class SessionResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_lap_after_finished_is_in_results(self):
        tracker = SessionTracker()
        state = SessionState(1, "Practice 1", "Test GP", 1, started=True)
        tracker._state = state
        results = []

        async def on_end(session, **data):
            results.append(data)

        tracker.on_session_end = on_end
        await tracker._on_live_message("TimingData", {
            "Lines": {"63": {"BestLapTime": {"Value": "1:32.000"}}}
        })
        await tracker._on_live_message("SessionStatus", {"Status": "Finished"})
        self.assertFalse(state.ended)
        self.assertEqual([], results)
        await tracker._on_live_message("TimingData", {
            "Lines": {"63": {"BestLapTime": {"Value": "1:30.000"}}}
        })
        # Exercise the real call path: completion runs in the receive task.
        tracker._livetiming_task = asyncio.current_task()
        await tracker._on_live_message("SessionStatus", {"Status": "Finalised"})
        await tracker._on_live_message("SessionStatus", {"Status": "Finalised"})
        self.assertTrue(state.ended)
        self.assertEqual(1, len(results))
        self.assertEqual(90.0, results[0]["live_laps"][63])

    async def test_confirmed_live_session_ignores_openf1_until_finished(self):
        tracker = SessionTracker()
        original = SessionState(
            session_key=11731,
            session_name="Race",
            meeting_name="Bahrain Grand Prix",
            meeting_key=1,
            started=True,
        )
        tracker._state = original
        tracker._livetiming_session_confirmed = True
        tracker._livetiming_task = _RunningTask()  # type: ignore[assignment]
        client = _OpenF1ShouldNotBeCalled()

        await tracker._poll_inner(client)  # type: ignore[arg-type]

        self.assertFalse(client.called)
        self.assertIs(original, tracker.current_session)
        self.assertEqual("Race", tracker.current_session.session_name)

    async def test_radio_recovers_static_path_from_live_session_info(self):
        tracker = SessionTracker()
        state = SessionState(11731, "Race", "Test GP", 1, started=True)
        state.driver_map[63] = "RUS"
        tracker._state = state
        tracker._livetiming_client = _LiveState({
            "SessionInfo": {
                "Path": "2026/2026-10-04_Test_GP/2026-10-04_Race/"
            }
        })  # type: ignore[assignment]
        events: list[dict] = []

        async def on_team_radio(**event):
            events.append(event)

        tracker.on_team_radio = on_team_radio
        await tracker._process_team_radio({
            "Captures": [{
                "Path": "TeamRadio/RUS_63_test.mp3",
                "RacingNumber": "63",
            }]
        }, state)

        self.assertEqual(1, len(events))
        self.assertEqual(
            "https://livetiming.formula1.com/static/2026/"
            "2026-10-04_Test_GP/2026-10-04_Race/TeamRadio/RUS_63_test.mp3",
            events[0]["recording_url"],
        )

    async def test_radio_without_session_path_is_deferred_not_lost(self):
        tracker = SessionTracker()
        state = SessionState(-1, "Race", "Test GP", 1, started=True)
        tracker._state = state
        tracker._livetiming_client = _LiveState({"SessionInfo": {}})  # type: ignore[assignment]
        events: list[dict] = []

        async def on_team_radio(**event):
            events.append(event)

        tracker.on_team_radio = on_team_radio
        await tracker._process_team_radio({
            "Captures": [{
                "Path": "TeamRadio/RUS_63_retry.mp3",
                "RacingNumber": "63",
            }]
        }, state)

        self.assertEqual([], events)
        self.assertEqual(set(), state.seen_radio)

    async def test_incomplete_positions_do_not_emit_empty_summary(self):
        tracker = SessionTracker()
        state = SessionState(1, "Race", "Test GP", 1, started=True)
        state.last_positions = {1: 1}
        tracker._state = state
        summaries: list[dict] = []

        async def on_summary(**event):
            summaries.append(event)

        tracker.on_race_summary = on_summary
        await tracker._process_lap_count(
            {"CurrentLap": 22, "TotalLaps": 57}, state, emit_summary=True
        )

        self.assertEqual([], summaries)
        self.assertEqual(0, state.last_summary_lap)

        state.last_positions = {dn: dn for dn in range(1, 11)}
        await tracker._process_lap_count(
            {"CurrentLap": 22, "TotalLaps": 57}, state, emit_summary=True
        )
        self.assertEqual(1, len(summaries))
        self.assertEqual(22, state.last_summary_lap)


if __name__ == "__main__":
    unittest.main()
