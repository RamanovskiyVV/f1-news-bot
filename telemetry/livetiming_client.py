"""Live F1 timing data via the official SignalR Core stream.

Protocol: ASP.NET Core SignalR (JSON, NOT the legacy SignalR 2.x).
Endpoint: wss://livetiming.formula1.com/signalrcore

Authentication:
  Requires an F1TV subscription token (F1TV Access = free tier works).
  Set F1_SUBSCRIPTION_TOKEN env var with the JWT token.
  To get the token for the first time, run:
    python -c "from fastf1.internals.f1auth import get_auth_token; get_auth_token()"
  Then open the printed URL in your browser, log in, and copy the token.

Connection flow:
  1. OPTIONS /signalrcore/negotiate  -> get AWSALBCORS cookie
  2. POST /signalrcore/negotiate?negotiateVersion=1 with Bearer token
     -> get connectionToken / URL
  3. WSS with ?access_token=<token>
  4. Send handshake: {"protocol":"json","version":1} + \x1e
  5. Server responds: {} + \x1e
  6. Send Subscribe invocation for desired topics
  7. Server sends feed messages as type=1 invocations
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable

import aiohttp

logger = logging.getLogger(__name__)

_NEGOTIATE_URL   = "https://livetiming.formula1.com/signalrcore/negotiate"
_CONNECTION_URL  = "wss://livetiming.formula1.com/signalrcore"
_MSG_SEPARATOR   = "\x1e"          # SignalR Core record separator
_RECONNECT_DELAY = 5.0

# Topics we subscribe to
TOPICS = [
    "SessionInfo",
    "TimingData",
    "TimingAppData",
    "DriverList",
    "RaceControlMessages",
    "TeamRadio",
    "PitLaneTimeCollection",
    "TrackStatus",
    "SessionStatus",
    "LapCount",
    "Heartbeat",
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
    Streams F1 live timing data via the official SignalR Core endpoint.

    Usage::

        client = LiveTimingClient()
        client.on_message = my_async_callback   # called as (topic, data)
        task = asyncio.create_task(client.run())
        ...
        task.cancel()

    Requires F1_SUBSCRIPTION_TOKEN environment variable (free F1TV account).
    """

    def __init__(self) -> None:
        self.on_message: MessageCallback | None = None
        self._state: dict[str, Any] = {}
        self._running = False
        self._token: str = os.getenv("F1_SUBSCRIPTION_TOKEN", "")

    def get_state(self, topic: str) -> Any:
        return self._state.get(topic)

    async def run(self) -> None:
        """Stream data until cancelled. Auto-reconnects on transient errors."""
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
                    "LiveTiming disconnected (%s) -- reconnecting in %.0fs",
                    e, _RECONNECT_DELAY,
                )
                await asyncio.sleep(_RECONNECT_DELAY)

    # -- Internal ---------------------------------------------------------------

    async def _connect_and_stream(self) -> None:
        if not self._token:
            raise RuntimeError(
                "F1_SUBSCRIPTION_TOKEN is not set. "
                "Get a free F1TV Access token via: "
                "python -c \"from fastf1.internals.f1auth import get_auth_token; get_auth_token()\""
            )

        timeout = aiohttp.ClientTimeout(total=None, connect=15, sock_connect=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Step 1: pre-negotiate to get AWSALBCORS cookie
            awsalb_cookie = await self._get_awsalb_cookie(session)

            # Step 2: negotiate to get connection URL / token
            ws_url = await self._negotiate(session, awsalb_cookie)

            # Step 3: open WebSocket
            headers = {}
            if awsalb_cookie:
                headers["Cookie"] = f"AWSALBCORS={awsalb_cookie}"

            logger.info("Connecting to F1 live timing (SignalR Core)...")
            async with session.ws_connect(
                ws_url,
                headers=headers,
                heartbeat=30,
                receive_timeout=120,
            ) as ws:
                # Step 4: SignalR Core handshake
                await ws.send_str(
                    json.dumps({"protocol": "json", "version": 1}) + _MSG_SEPARATOR
                )

                # Step 5: wait for handshake response {}
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        frames = msg.data.split(_MSG_SEPARATOR)
                        for frame in frames:
                            if not frame.strip():
                                continue
                            parsed = json.loads(frame)
                            if parsed == {}:
                                # Handshake OK
                                logger.info(
                                    "F1 live timing connected. Subscribing to %d topics.",
                                    len(TOPICS),
                                )
                                # Step 6: subscribe
                                await ws.send_str(
                                    json.dumps({
                                        "type": 1,
                                        "target": "Subscribe",
                                        "arguments": [TOPICS],
                                        "invocationId": "0",
                                    }) + _MSG_SEPARATOR
                                )
                                break
                        break  # exit inner loop, move to main receive loop
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        raise RuntimeError(f"WS {msg.type.name} during handshake")

                # Step 7: receive data stream
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_raw(msg.data)
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        raise RuntimeError(f"WebSocket {msg.type.name}: {msg.data}")

    async def _get_awsalb_cookie(self, session: aiohttp.ClientSession) -> str | None:
        """Pre-negotiate OPTIONS to get AWSALBCORS sticky cookie."""
        try:
            async with session.options(_NEGOTIATE_URL) as r:
                cookie = r.cookies.get("AWSALBCORS")
                if cookie:
                    logger.debug("Got AWSALBCORS cookie")
                    return cookie.value
        except Exception as e:
            logger.debug("AWSALBCORS pre-negotiate failed (ok): %s", e)
        return None

    async def _negotiate(
        self, session: aiohttp.ClientSession, awsalb_cookie: str | None
    ) -> str:
        """POST negotiate to get the WebSocket URL with access_token."""
        headers = {"Authorization": f"Bearer {self._token}"}
        if awsalb_cookie:
            headers["Cookie"] = f"AWSALBCORS={awsalb_cookie}"

        try:
            async with session.post(
                f"{_NEGOTIATE_URL}?negotiateVersion=1",
                headers=headers,
            ) as r:
                if r.status == 401:
                    text = await r.text()
                    raise RuntimeError(
                        f"F1 auth failed (401) -- token may be expired. "
                        f"Re-authenticate with: python -c \"from fastf1.internals.f1auth import get_auth_token; get_auth_token()\""
                        f"\nResponse: {text[:200]}"
                    )
                r.raise_for_status()
                data = await r.json(content_type=None)
        except aiohttp.ClientError as e:
            raise RuntimeError(f"Negotiate request failed: {e}") from e

        # SignalR Core returns either a URL redirect or connection info
        if "url" in data:
            # The server redirected us to a different hub URL
            hub_url = data["url"]
        else:
            hub_url = _CONNECTION_URL

        # Append access_token query param
        sep = "&" if "?" in hub_url else "?"
        return hub_url.replace("https://", "wss://").replace("http://", "ws://") + sep + f"access_token={self._token}"

    async def _handle_raw(self, raw: str) -> None:
        frames = raw.split(_MSG_SEPARATOR)
        for frame in frames:
            frame = frame.strip()
            if not frame:
                continue
            try:
                msg = json.loads(frame)
            except json.JSONDecodeError:
                continue
            await self._process_message(msg)

    async def _process_message(self, msg: dict) -> None:
        msg_type = msg.get("type")

        # Type 1: Invocation (server -> client data)
        if msg_type == 1:
            target = msg.get("target", "")
            args   = msg.get("arguments", [])

            if target == "feed" and args:
                # args = [topic, data, ...extra]
                topic = args[0]
                data  = args[1] if len(args) > 1 else {}
                if topic in ("TeamRadio", "RaceControlMessages"):
                    logger.info("SignalR live feed: %s", topic)
                # Accumulate state
                if isinstance(data, dict) and isinstance(self._state.get(topic), dict):
                    _deep_update(self._state[topic], data)
                else:
                    self._state[topic] = data
                # Notify handler
                if self.on_message and self._running:
                    await self.on_message(topic, data)

            elif target == "Subscribe" or args:
                # Initial full-state snapshot from Subscribe result
                # args = [[topic, full_data], ...] or {"topic": full_data}
                if args and isinstance(args[0], dict):
                    for topic, data in args[0].items():
                        if isinstance(data, dict):
                            _deep_update(self._state.setdefault(topic, {}), data)
                        else:
                            self._state[topic] = data
                        if self.on_message and self._running:
                            await self.on_message(topic, data)

        # Type 3: Completion (response to our Subscribe invocation)
        elif msg_type == 3:
            result = msg.get("result", {})
            if isinstance(result, dict):
                logger.info("Subscribe snapshot topics: %s", list(result.keys()))
                for topic, data in result.items():
                    if topic == "TeamRadio":
                        caps = data.get("Captures", {}) if isinstance(data, dict) else {}
                        logger.info("TeamRadio in snapshot: %d captures",
                                    len(caps) if isinstance(caps, (list, dict)) else 0)
                    if isinstance(data, dict):
                        _deep_update(self._state.setdefault(topic, {}), data)
                    else:
                        self._state[topic] = data
                    if self.on_message and self._running:
                        await self.on_message(topic, data)

        # Type 6: Ping / keep-alive
        elif msg_type == 6:
            pass  # heartbeat, nothing to do
