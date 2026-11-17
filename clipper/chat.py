"""Read-only Twitch chat for many channels over ONE anonymous IRC connection."""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger(__name__)

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697


@dataclass
class ChatMessage:
    channel: str
    user: str
    text: str
    t: float  # local wall clock when received (same clock as the capture buffer)
    emote_chars: int = 0
    emote_only: bool = False
    tags: dict = field(default_factory=dict)


def _unescape_tag(v: str) -> str:
    return v.replace("\\s", " ").replace("\\:", ";").replace("\\\\", "\\").replace("\\r", "\r").replace("\\n", "\n")


def parse_irc(line: str, now: float | None = None) -> ChatMessage | None:
    """Parse a PRIVMSG line (with IRCv3 tags) into a ChatMessage; anything else -> None."""
    tags: dict[str, str] = {}
    rest = line
    if rest.startswith("@"):
        raw_tags, rest = rest[1:].split(" ", 1)
        for kv in raw_tags.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape_tag(v)
    if not rest.startswith(":"):
        return None
    prefix, _, rest = rest[1:].partition(" ")
    cmd, _, rest = rest.partition(" ")
    if cmd != "PRIVMSG":
        return None
    chan, _, text = rest.partition(" :")
    user = tags.get("display-name") or prefix.split("!", 1)[0]
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):
        text = text[8:-1]
    emote_chars = 0
    if tags.get("emotes"):
        for spec in tags["emotes"].split("/"):
            _, _, ranges = spec.partition(":")
            for rg in ranges.split(","):
                a, _, b = rg.partition("-")
                if a.isdigit() and b.isdigit():
                    emote_chars += int(b) - int(a) + 1
    stripped = len(text.replace(" ", ""))
    return ChatMessage(
        channel=chan.lstrip("#").lower(), user=user.lower(), text=text,
        t=now if now is not None else time.time(), emote_chars=emote_chars,
        emote_only=tags.get("emote-only") == "1" or (stripped > 0 and emote_chars >= stripped),
        tags=tags,
    )


class TwitchChat:
    """One connection, any number of channels. Re-joins everything after a reconnect."""

    def __init__(self, on_message: Callable[[ChatMessage], None]):
        self.on_message = on_message
        self.channels: set[str] = set()
        self._writer: asyncio.StreamWriter | None = None
        self._join_q: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    async def join(self, channel: str) -> None:
        channel = channel.lower()
        if channel not in self.channels:
            self.channels.add(channel)
            await self._join_q.put(("JOIN", channel))

    async def part(self, channel: str) -> None:
        channel = channel.lower()
        if channel in self.channels:
            self.channels.discard(channel)
            await self._join_q.put(("PART", channel))

    async def _send(self, line: str) -> None:
        if self._writer:
            self._writer.write((line + "\r\n").encode())
            await self._writer.drain()

    async def _joiner(self) -> None:
        # Anonymous connections may JOIN ~20 channels per 10s; stay well under it.
        while True:
            cmd, ch = await self._join_q.get()
            await self.connected.wait()
            await self._send(f"{cmd} #{ch}")
            await asyncio.sleep(0.6)

    async def run(self) -> None:
        joiner = asyncio.create_task(self._joiner())
        backoff = 1.0
        try:
            while not self._stop.is_set():
                try:
                    await self._session()
                    backoff = 1.0
                except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
                    log.warning("chat connection lost: %s", e)
                self.connected.clear()
                if self._stop.is_set():
                    break
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)
        finally:
            joiner.cancel()

    async def _session(self) -> None:
        reader, writer = await asyncio.open_connection(IRC_HOST, IRC_PORT, ssl=ssl.create_default_context())
        self._writer = writer
        await self._send("CAP REQ :twitch.tv/tags twitch.tv/commands")
        await self._send(f"NICK justinfan{random.randint(10000, 99999)}")
        self.connected.set()
        # re-join after reconnect
        while not self._join_q.empty():
            self._join_q.get_nowait()
        for ch in sorted(self.channels):
            await self._join_q.put(("JOIN", ch))
        log.info("chat connected (%d channels)", len(self.channels))
        try:
            while True:
                raw = await asyncio.wait_for(reader.readline(), timeout=360)
                if not raw:
                    raise ConnectionError("EOF")
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("PING"):
                    await self._send("PONG" + line[4:])
                    continue
                if " RECONNECT" in line[:200]:
                    raise ConnectionError("server requested reconnect")
                msg = parse_irc(line)
                if msg:
                    try:
                        self.on_message(msg)
                    except Exception:  # never let a consumer bug kill the connection
                        log.exception("chat handler failed")
        except asyncio.TimeoutError as e:
            raise ConnectionError("chat timed out") from e
        finally:
            self._writer = None
            writer.close()

    def stop(self) -> None:
        self._stop.set()
