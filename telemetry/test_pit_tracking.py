"""Regression tests for live pit-stop tracking."""
from __future__ import annotations

import unittest
import sys
import types

# The regression tests exercise the state machine only. Keep them runnable in
# minimal CI environments that have not installed the websocket transport extra.
try:
    import aiohttp  # noqa: F401
except ModuleNotFoundError:
    sys.modules["aiohttp"] = types.ModuleType("aiohttp")

from .session_tracker import SessionState, SessionTracker


class _StateClient:
    def __init__(self) -> None:
        self.state: dict = {}

    def get_state(self, topic: str):
        return self.state.get(topic)


class PitTrackingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tracker = SessionTracker()
        self.state = SessionState(1, "Race", "Test GP", 1)
        self.tracker._state = self.state
        self.client = _StateClient()
        self.tracker._livetiming_client = self.client  # type: ignore[assignment]
        self.events: list[dict] = []

        async def on_pit_stop(**event):
            self.events.append(event)

        self.tracker.on_pit_stop = on_pit_stop

    async def test_snapshot_pits_are_only_used_as_baseline(self) -> None:
        snapshot = {
            "PitTimes": {
                "1": {"Lap": 10, "Duration": "2.4"},
                "4": {"Lap": 11, "Duration": "2.7"},
            }
        }

        await self.tracker._on_live_message(
            "PitLaneTimeCollection", snapshot, is_snapshot=True
        )

        self.assertEqual([], self.events)
        self.assertEqual({1: 10, 4: 11}, self.state._last_pit_lap)
        self.assertEqual({}, self.state._pending_pits)

    async def test_sparse_updates_emit_one_stop_with_new_compound(self) -> None:
        initial_timing = {
            "Lines": {"1": {"Stints": {"0": {"Compound": "SOFT", "LapNumber": 1}}}}
        }
        self.client.state["TimingAppData"] = initial_timing
        await self.tracker._on_live_message(
            "TimingAppData", initial_timing, is_snapshot=True
        )

        merged_timing = {
            "Lines": {
                "1": {
                    "Stints": {
                        "0": {"Compound": "SOFT", "LapNumber": 1},
                        "1": {"Compound": "MEDIUM", "LapNumber": 20},
                    }
                }
            }
        }
        self.client.state["TimingAppData"] = merged_timing
        await self.tracker._on_live_message(
            "TimingAppData",
            {"Lines": {"1": {"Stints": {"1": {"Compound": "MEDIUM"}}}}},
        )

        full_pit = {"PitTimes": {"1": {"Lap": 20, "Duration": "2.5"}}}
        self.client.state["PitLaneTimeCollection"] = full_pit
        await self.tracker._on_live_message(
            "PitLaneTimeCollection",
            {"PitTimes": {"1": {"Duration": "2.5"}}},
        )
        await self.tracker._on_live_message(
            "PitLaneTimeCollection",
            {"PitTimes": {"1": {"Duration": "2.5"}}},
        )

        self.assertEqual(1, len(self.events))
        self.assertEqual("MEDIUM", self.events[0]["compound"])
        self.assertEqual(20, self.events[0]["lap_number"])

    async def test_snapshot_rebuilds_pit_count_without_replaying_events(self) -> None:
        snapshot = {
            "Lines": {
                "1": {
                    "Stints": {
                        "0": {"Compound": "SOFT", "LapNumber": 1},
                        "1": {"Compound": "MEDIUM", "LapNumber": 18},
                        "2": {"Compound": "HARD", "LapNumber": 36},
                    }
                }
            }
        }
        self.client.state["TimingAppData"] = snapshot

        await self.tracker._on_live_message(
            "TimingAppData", snapshot, is_snapshot=True
        )

        self.assertEqual([], self.events)
        self.assertEqual(2, self.state.pit_counts[1])


if __name__ == "__main__":
    unittest.main()
