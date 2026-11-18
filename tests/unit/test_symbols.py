"""SymbolRegistry: loading Binance's trading symbol list."""
import asyncio
import json

import httpx
import pytest

from src.exchanges import symbols as symbols_mod
from src.exchanges.symbols import SymbolRegistry


def exchange_info(*pairs: tuple[str, str]) -> bytes:
    return json.dumps({"symbols": [{"symbol": s, "status": st} for s, st in pairs]}).encode()


def registry(responses: list) -> tuple[SymbolRegistry, list[httpx.Request]]:
    """Registry whose HTTP calls return `responses` in order."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        r = responses[min(len(seen), len(responses)) - 1]
        if isinstance(r, Exception):
            raise r
        return r

    return SymbolRegistry("https://binance.test", transport=httpx.MockTransport(handler)), seen


async def test_unknown_until_loaded():
    reg, _ = registry([httpx.Response(200, content=exchange_info(("BTCUSDT", "TRADING")))])
    assert reg.is_tradable("BTCUSDT") is None       # can't tell yet - not "False"
    assert await reg.refresh() == 1
    assert reg.is_tradable("BTCUSDT") is True
    assert reg.is_tradable("NOTACOIN") is False


async def test_requests_trading_symbols_and_keeps_only_trading():
    reg, seen = registry([httpx.Response(200, content=exchange_info(
        ("BTCUSDT", "TRADING"), ("OLDUSDT", "BREAK"), ("ETHUSDT", "TRADING")))])
    await reg.refresh()
    assert seen[0].url.path == "/api/v3/exchangeInfo"
    assert seen[0].url.params["symbolStatus"] == "TRADING"
    assert reg.is_tradable("ETHUSDT") and not reg.is_tradable("OLDUSDT")


@pytest.mark.parametrize("bad", [
    httpx.Response(500),
    httpx.Response(200, content=exchange_info()),           # empty list
    httpx.Response(200, content=b"not json"),
    httpx.ConnectError("down"),
], ids=["5xx", "empty", "garbage", "network"])
async def test_failed_refresh_keeps_the_previous_list(bad):
    reg, _ = registry([httpx.Response(200, content=exchange_info(("BTCUSDT", "TRADING"))), bad])
    await reg.refresh()
    with pytest.raises(Exception):
        await reg.refresh()
    assert reg.is_tradable("BTCUSDT") is True


async def test_run_retries_a_failed_first_load(monkeypatch):
    monkeypatch.setattr(symbols_mod, "INITIAL_RETRY_SECONDS", 0.01)
    reg, seen = registry([httpx.ConnectError("down"), httpx.ConnectError("down"),
                          httpx.Response(200, content=exchange_info(("BTCUSDT", "TRADING")))])
    task = asyncio.create_task(reg.run())
    try:
        for _ in range(200):
            if reg.loaded:
                break
            await asyncio.sleep(0.01)
        assert reg.is_tradable("BTCUSDT") is True
        assert len(seen) == 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
