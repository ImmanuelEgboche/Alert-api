"""On-disk format for raw exchange feeds.

A recording is gzip-compressed JSON Lines:

    line 1:  {"format": "crypto-alert-recording", "version": 1, "exchange": ...,
              "symbols": [...], "ws_url": ..., "started_at": ...}
    line 2+: {"t": <wall-clock ns when received>, "m": "<raw message, verbatim>"}

Messages are stored exactly as the exchange sent them (not parsed), so replay
goes through the real parser and a parser bug found later can be re-tested
against old recordings.

The writer flushes the gzip stream every second, so a crash or Ctrl-C loses at
most about a second of data; the reader stops cleanly at a truncated tail.
"""
from __future__ import annotations

import gzip
import json
import time
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

FORMAT_NAME = "crypto-alert-recording"
FORMAT_VERSION = 1


class RecordingFormatError(ValueError):
    """The file is not a recording this code can read."""


@dataclass(slots=True)
class RecordedMessage:
    recv_ns: int   # wall clock (time.time_ns) when the message was received
    raw: str       # message exactly as the exchange sent it


class RecordingWriter:
    """Append raw messages to a recording file."""

    FLUSH_EVERY_SECONDS = 1.0

    def __init__(self, path: str | Path, exchange: str, symbols: list[str], ws_url: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file: TextIO = gzip.open(self.path, "wt", encoding="utf-8")  # type: ignore[assignment]
        self._last_flush = time.monotonic()
        self.messages = 0
        self.header = {
            "format": FORMAT_NAME,
            "version": FORMAT_VERSION,
            "exchange": exchange,
            "symbols": symbols,
            "ws_url": ws_url,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write_line(self.header)

    def _write_line(self, obj: dict[str, Any]) -> None:
        self._file.write(json.dumps(obj, separators=(",", ":")) + "\n")

    def write(self, recv_ns: int, raw: str) -> None:
        """Record one message. Usable directly as an ExchangeConnection raw hook."""
        self._write_line({"t": recv_ns, "m": raw})
        self.messages += 1
        now = time.monotonic()
        if now - self._last_flush >= self.FLUSH_EVERY_SECONDS:
            self._file.flush()  # gzip sync-flush: everything so far is readable
            self._last_flush = now

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> "RecordingWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_header(path: str | Path) -> dict[str, Any]:
    """The recording's metadata line."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return _parse_header(f.readline())


def _parse_header(line: str) -> dict[str, Any]:
    try:
        header = json.loads(line)
    except json.JSONDecodeError as e:
        raise RecordingFormatError(f"not a recording: {e}") from e
    if not isinstance(header, dict) or header.get("format") != FORMAT_NAME:
        raise RecordingFormatError("not a recording (missing format header)")
    if header.get("version") != FORMAT_VERSION:
        raise RecordingFormatError(f"unsupported recording version {header.get('version')}")
    return header


def read_messages(path: str | Path) -> Iterator[RecordedMessage]:
    """Yield recorded messages in order.

    A recording cut off mid-write (crash, Ctrl-C, disk full) ends at the last
    complete line instead of raising.
    """
    with gzip.open(path, "rt", encoding="utf-8") as f:
        _parse_header(f.readline())
        try:
            for line in f:
                if not line.endswith("\n"):
                    return  # truncated final line
                rec = json.loads(line)
                yield RecordedMessage(recv_ns=rec["t"], raw=rec["m"])
        except (EOFError, zlib.error, gzip.BadGzipFile):
            return  # truncated gzip stream: keep what was readable
