"""The exchange feed must survive every way a connection can end.

Runs a real local WebSocket server standing in for Binance, so the real
websockets library behaviour is exercised - in particular that a clean close
(1000/1001, which Binance sends at its 24h limit) ends `async for` silently.
"""
import asyncio
import json
import time

import pytest
import websockets

from src.exchanges.binance import BinanceConnection

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
STREAMS = ["btcusdt@trade", "ethusdt@trade"]


def trade(price: float) -> str:
    return json.dumps({"e": "trade", "s": "BTCUSDT", "p": str(price),
                       "T": int(time.time() * 1000)})


class FakeBinance:
    """Each connection runs the next step of `script` (default: stay open).

    A step is an async fn(ws, n) where n is the 1-based connection number.
    """

    def __init__(self) -> None:
        self.connections = 0
        self.subscribes: list[list[str]] = []
        self.script: list = []
        self.server = None

    async def _handler(self, ws) -> None:
        self.connections += 1
        n = self.connections
        msg = json.loads(await ws.recv())
        self.subscribes.append(msg["params"])
        step = self.script[n - 1] if n <= len(self.script) else stay_open
        await step(ws, n)

    @property
    def url(self) -> str:
        port = self.server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}"


def send_then_close(code: int):
    async def step(ws, n):
        await ws.send(trade(n))
        await ws.close(code=code)
    return step


async def close_immediately(ws, n):
    await ws.close(code=1000)


async def stay_open(ws, n):
    await ws.send(trade(n))
    await ws.wait_closed()


@pytest.fixture
async def exchange():
    fake = FakeBinance()
    fake.server = await websockets.serve(fake._handler, "127.0.0.1", 0)
    yield fake
    fake.server.close()
    await fake.server.wait_closed()


@pytest.fixture
async def make_conn(exchange):
    conns: list[BinanceConnection] = []

    def make(stable_after: float = 0.0) -> tuple[BinanceConnection, list]:
        conn = BinanceConnection(exchange.url, list(SYMBOLS))
        # 0 = treat every connection as long-lived, so drops reconnect
        # immediately (like Binance's 24h close) without slowing the tests.
        conn.STABLE_AFTER_SECONDS = stable_after
        prices: list[float] = []
        conn.add_callback(lambda s, p, t: prices.append(p))
        conns.append(conn)
        return conn, prices

    yield make
    for c in conns:
        await c.disconnect()


async def until(cond, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


# --- 1. reconnect however the connection ends --------------------------------

@pytest.mark.parametrize("code", [1000, 1001, 1011], ids=["clean", "going-away", "error"])
async def test_reconnects_and_resubscribes_after_close(exchange, make_conn, code):
    exchange.script = [send_then_close(code)]
    conn, prices = make_conn()

    await conn.connect()
    await until(lambda: prices == [1.0, 2.0])      # trades flow again on conn #2

    assert exchange.connections == 2
    assert exchange.subscribes == [STREAMS, STREAMS]  # every symbol resubscribed
    assert conn.is_connected


async def test_survives_repeated_daily_closes(exchange, make_conn):
    exchange.script = [send_then_close(1000)] * 3
    conn, prices = make_conn()

    await conn.connect()
    await until(lambda: prices == [1.0, 2.0, 3.0, 4.0])
    assert exchange.connections == 4


# --- 2. health must not lie ----------------------------------------------------

async def test_is_connected_false_once_the_feed_ends(exchange, make_conn):
    exchange.script = [close_immediately]
    conn, _ = make_conn()
    conn._reconnect = lambda: None                  # observe the gap before reconnect

    await conn.connect()
    assert conn.is_connected
    await until(lambda: not conn.is_connected)


# --- 3. intentional disconnect -------------------------------------------------

async def test_disconnect_does_not_reconnect(exchange, make_conn):
    conn, prices = make_conn()
    await conn.connect()
    await until(lambda: prices == [1.0])

    await conn.disconnect()
    await asyncio.sleep(0.3)
    assert exchange.connections == 1
    assert not conn.is_connected


async def test_disconnect_cancels_a_pending_reconnect(exchange, make_conn):
    exchange.script = [close_immediately]
    conn, _ = make_conn(stable_after=30)            # quick drop -> delayed reconnect
    conn.MAX_RECONNECT_DELAY_SECONDS = 0.3

    await conn.connect()
    await until(lambda: conn._reconnect_task is not None)
    await conn.disconnect()                         # while waiting to reconnect
    await asyncio.sleep(0.5)
    assert exchange.connections == 1


# --- 4. no reconnect storm -----------------------------------------------------

async def test_connections_that_drop_at_once_back_off(exchange, make_conn):
    exchange.script = [close_immediately] * 1000
    conn, _ = make_conn(stable_after=30)

    await conn.connect()
    await asyncio.sleep(1.5)
    # backoff 1s, 2s, ... -> 2 connections in 1.5s. Without it: hundreds.
    assert exchange.connections <= 2


def test_backoff_schedule():
    conn = BinanceConnection("ws://unused", [])
    conn._opened_at = time.monotonic()              # every drop is "quick"
    delays = [conn._reconnect_delay() for _ in range(8)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]

    conn._opened_at = time.monotonic() - 31         # a stable connection dropped
    assert conn._reconnect_delay() == 0
    conn._opened_at = time.monotonic()
    assert conn._reconnect_delay() == 1             # counter was reset
