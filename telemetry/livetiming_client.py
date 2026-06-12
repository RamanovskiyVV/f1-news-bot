"""Live F1 timing data via the official public SignalR stream.

Connects to livetiming.formula1.com/signalr — the same free public
endpoint that FastF1 uses for recording. No authentication required.

Protocol: ASP.NET SignalR 2.x (NOT the modern ASP.NET Core SignalR).
  1. GET /signalr/negotiate → ConnectionToken
  2. WSS /signalr/connect?transport=webSockets&...
  3. Send {"H":"Streaming","M":"Subscribe","A":[topics],"I":1}
  4. Receive initial snapshot {"R":{topic: full_state}} then
     incremental updates {"M":[{"H":"Streaming","M":"feed","A":[topic,delta,...]}]}
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.parse
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

_HUB_URL = "https://livetiming.formula1.com/signalr"
_CONNECTION_DATA = '[{"name":"Streaming"}]'
_PROTOCOL = "1.5"
_HEADERS = {"User-Agent": "BestHTTP/2"}
_RECONNECT_DELAY = 5.0

# Topics we subscribe to (subset of what F1 provides)
TOPICS = [
    "SessionInfo",      # meeting name, session name, start/end dates
    "TimingData",       # positions, gap, lap times
    "TimingAppData",    # tyre compounds (stints)
    "DriverList",       # racing numbers, names, teams
    "RaceControlMessages",
    "TeamRadio",
    "PitLaneTimeCollection",
    "TrackStatus",
    "SessionStatus",
]

# Base URL for team radio audio files
AUDIO_BASE = "https://livetiming.formula1.com/"

MessageCallback = Callable[[str, Any], Awaitable[None]]


def _deep_update(target: dict, source: dict) -> dict:
    """Recursively merge *source* into *target* in-place."""
    for k, v in source.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            _deep_update(target[k], v)
        else:
            target[k] = v
    return target


def parse_lap_time(s: str | None) -> float | None:
    """Convert F1 time string '1:23.456' or '83.456' to seconds."""
    if not s:
        return None
    try:
        s = s.strip()
        if ":" in s:
            m, sec = s.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(s)
    except (ValueError, AttributeError):
        return None


class LiveTimingClient:
    """
    Streams F1 live timing data via the official public SignalR endpoint.

    Usage::

        client = LiveTimingClient()
        client.on_message = my_async_callback   # called as (topic, data)
        task = asyncio.create_task(client.run())
        ...
        task.cancel()

    *on_message* is called once for the initial full-state snapshot
    and then for every incremental update message.

    Accumulated state per topic is available via ``get_state(topic)``.
    """

    def __init__(self) -> None:
        self.on_message: MessageCallback | None = None
        self._state: dict[str, Any] = {}
        self._running = False

    def get_state(self, topic: str) -> Any:
        return self._state.get(topic)

    async def run(self) -> None:
        """Stream data until cancelled.  Auto-reconnects on transient errors."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if not self._running:
                    break
                logger.warning(
                    "LiveTiming disconnected (%s) — reconnecting in %.0fs",
                    e, _RECONNECT_DELAY,
                )
                await asyncio.sleep(_RECONNECT_DELAY)

    # ── Internal ───────────────────────────────────────────────────────────────

    async def _connect_and_stream(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Step 1 – negotiate
            token = await self._negotiate(session)
            if not token:
                raise RuntimeError("SignalR negotiate returned no ConnectionToken")

            # Step 2 – build WebSocket URL
            ws_url = (
                f"wss://livetiming.formula1.com/signalr/connect"
                f"?transport=webSockets"
                f"&clientProtocol={_PROTOCOL}"
                f"&connectionToken={urllib.parse.quote(token, safe='')}"
                f"&connectionData={urllib.parse.quote(_CONNECTION_DATA, safe='')}"
                f"&tid=10"
            )
            logger.info("Connecting to F1 live timing...")

            # Step 3 – open WebSocket
            async with session.ws_connect(
                ws_url,
                headers=_HEADERS,
                heartbeat=30,
                receive_timeout=120,
            ) as ws:
                # Step 4 – subscribe
                await ws.send_str(json.dumps({
                    "H": "Streaming",
                    "M": "Subscribe",
                    "A": [TOPICS],
                    "I": 1,
                }))
                logger.info("Subscribed to F1 live timing (topics: %s)", TOPICS)

                # Step 5 – receive
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_raw(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise RuntimeError(
                            f"WebSocket {msg.type.name}: {msg.data}"
                        )

    async def _negotiate(self, session: aiohttp.ClientSession) -> str | None:
        params = {
            "clientProtocol": _PROTOCOL,
            "connectionData": _CONNECTION_DATA,
            "_": str(int(time.time() * 1000)),
        }
        try:
            async with session.get(
                f"{_HUB_URL}/negotiate",
                params=params,
                headers=_HEADERS,
            ) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
                return data.get("ConnectionToken")
        except Exception as e:
            logger.warning("SignalR negotiate error: %s", e)
            return None

    async def _handle_raw(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # ── Initial snapshot (response to our Subscribe call) ─────────────────
        # {"R": {"TimingData": {...}, "DriverList": {...}, ...}, "I": "1"}
        if "R" in msg and isinstance(msg["R"], dict):
            for topic, data in msg["R"].items():
                if isinstance(data, dict):
                    _deep_update(self._state.setdefault(topic, {}), data)
                else:
                    self._state[topic] = data
                if self.on_message and self._running:
                    await self.on_message(topic, data)
            return

        # ── Incremental updates ────────────────────────────────────────────────
        # {"C": "...", "M": [{"H":"Streaming","M":"feed","A":[topic, delta, ts]}]}
        for item in msg.get("M", []):
            if item.get("M") != "feed":
                continue
            args = item.get("A", [])
            if len(args) < 2:
                continue
            topic, data = args[0], args[1]
            # Accumulate
            if isinstance(data, dict) and isinstance(self._state.get(topic), dict):
                _deep_update(self._state[topic], data)
            else:
                self._state[topic] = data
            # Notify
            if self.on_message and self._running:
                await self.on_message(topic, data)
