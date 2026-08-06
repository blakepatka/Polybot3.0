"""Chainlink Data Streams — the venue's own resolution source.

Every one of these markets resolves against a Chainlink data stream, stated in
the market description ("the resolution source for this market is information
from Chainlink, specifically the <ASSET>/USD data stream"). Everything else in
this project has been anchoring and settling against Coinbase/Kraken spot,
which *leads* Chainlink but is not identical to it — a basis that can flip the
outcome of a near-flat window.

Reading the stream directly removes that error entirely: the same number the
market resolves on becomes the number the model trades on. It is also lower
latency than polling public REST tickers, since Data Streams pushes over a
WebSocket.

Access is credentialed. Keys are stored in the vault like any other secret and
never leave this machine.

Authentication (Data Streams v1): each request carries

    Authorization:                    <client id / API key>
    X-Authorization-Timestamp:        <unix milliseconds>
    X-Authorization-Signature-SHA256: <hex HMAC-SHA256>

where the signed payload is

    METHOD + " " + PATH_AND_QUERY + " " + SHA256(body) + " " + CLIENT_ID + " " + TIMESTAMP

signed with the API secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

REST_HOST = "https://api.dataengine.chain.link"
WS_HOST = "wss://ws.dataengine.chain.link"

# Reports are big-endian fixed point with 18 decimals.
WEI = 10 ** 18

# Mainnet V3 reference-price stream IDs published by Chainlink. The entitled
# /feeds response currently contains only feedID values, despite older API
# examples including names, so discovery must be able to map IDs directly.
MAINNET_FEED_IDS = {
    "btc": "0x00039d9e45394f473ab1f050a1b963e6b05351e52d71e507509ada0c95ed75b8",
    "eth": "0x000362205e10b3a147d02792eccee483dca6c7b44ecce7012cb8c6e0b68b3ae9",
    "sol": "0x0003b778d3f6b2ac4991302b89cb313f99a42467d6c9c5f96f57c29c0d2bc24f",
    "xrp": "0x0003c16c6aed42294f5cb4741f6e59ba2d728f0eae2eb9e6d3f555808c59fc45",
    "bnb": "0x000335fd3f3ffa06cfd9297b97367f77145d7a5f132e84c736cc471dd98621fe",
    "doge": "0x000356ca64d3b32135e17dc0dc721a645bf50d0303be8ceb2cdca0a50bab8fdc",
    "hype": "0x0003d34539af562867c3cb309b59efccf40e74b404fb415eeb7699d61322aed9",
}


def _sign(method: str, path_and_query: str, body: bytes, client_id: str,
          secret: str, timestamp_ms: int) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{method} {path_and_query} {body_hash} {client_id} {timestamp_ms}"
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def auth_headers(method: str, url: str, client_id: str, secret: str,
                 body: bytes = b"") -> dict[str, str]:
    parsed = urlparse(url)
    path_and_query = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    ts = int(time.time() * 1000)
    return {
        "Authorization": client_id,
        "X-Authorization-Timestamp": str(ts),
        "X-Authorization-Signature-SHA256": _sign(
            method, path_and_query, body, client_id, secret, ts
        ),
    }


def credentials() -> tuple[str, str]:
    # The portal's display Username may begin with an uppercase character,
    # while Data Streams authentication requires the canonical lowercase
    # Streams User ID. It is included verbatim in the signed payload.
    return os.getenv("CHAINLINK_API_KEY", "").lower(), os.getenv("CHAINLINK_API_SECRET", "")


def configured() -> bool:
    key, secret = credentials()
    return bool(key and secret)


@dataclass
class StreamPrice:
    feed_id: str
    asset: str
    price: float
    observed_at: float          # venue timestamp (seconds)
    received_at: float          # local arrival
    source: str = "chainlink"

    @property
    def latency_ms(self) -> float:
        return max(0.0, (self.received_at - self.observed_at) * 1000.0)


@dataclass
class ChainlinkStreams:
    """REST + WebSocket client for Data Streams.

    Feed IDs are *discovered*, never hardcoded: the set of available streams
    depends on the account's entitlements, and a wrong hardcoded ID fails in
    the worst possible way — silently returning another asset's price.
    """

    assets: list[str]
    feeds: dict[str, str] = field(default_factory=dict)      # asset -> feed id
    latest: dict[str, StreamPrice] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=lambda: {
        "active": False, "error": None, "latency": None,
        "url": "api.dataengine.chain.link", "role": "Venue resolution source (Chainlink)",
        "kind": "REST+WS", "configured": False, "feeds": 0,
    })
    _client: httpx.AsyncClient | None = None
    _task: asyncio.Task | None = None
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> bool:
        """Connect and discover feeds. Returns False when unconfigured.

        A missing key is not an error: the engine falls back to exchange spot
        and says so in the UI.
        """
        self.health["configured"] = configured()
        if not configured():
            self.health.update(active=False, error="no API key in vault")
            return False

        await self._ensure_client()
        ok = await self.discover_feeds()
        if not ok:
            return False
        self._stop.clear()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop(), name="chainlink")
        return True

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Return a live client, including after credentials are added at runtime."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- discovery ---------------------------------------------------------

    async def discover_feeds(self) -> bool:
        """List entitled feeds and map the ones we trade."""
        self.health["configured"] = configured()
        if not configured():
            self.health.update(active=False, error="no API key in vault", feeds=0)
            return False
        client = await self._ensure_client()
        key, secret = credentials()
        url = f"{REST_HOST}/api/v1/feeds"
        try:
            resp = await client.get(url, headers=auth_headers("GET", url, key, secret))
            if resp.status_code == 401:
                self.health.update(active=False, error="401 — key/secret rejected")
                return False
            if resp.status_code != 200:
                self.health.update(active=False, error=f"feeds HTTP {resp.status_code}")
                return False
            body = resp.json()
        except Exception as exc:
            self.health.update(active=False, error=str(exc)[:160])
            return False

        entries = body.get("feeds") if isinstance(body, dict) else body
        found: dict[str, str] = {}
        entitled_ids = {
            str(entry.get("feedID") or entry.get("feedId") or entry.get("id") or "").lower()
            for entry in entries or []
        }
        for asset in self.assets:
            published_id = MAINNET_FEED_IDS.get(asset.lower())
            if published_id and published_id.lower() in entitled_ids:
                found[asset] = published_id

        for entry in entries or []:
            name = str(entry.get("feedName") or entry.get("name") or "").upper()
            feed_id = entry.get("feedID") or entry.get("feedId") or entry.get("id")
            if not feed_id:
                continue
            for asset in self.assets:
                # Match "BTC/USD" style names, avoiding partial hits like
                # "BTC/USDT" mapping onto a USD request.
                if name.replace("-", "/").startswith(f"{asset.upper()}/USD"):
                    found.setdefault(asset, feed_id)

        self.feeds = found
        self.health.update(active=bool(found), feeds=len(found),
                           error=None if found else "no matching feeds entitled")
        return bool(found)

    # -- data --------------------------------------------------------------

    async def fetch_latest(self, asset: str) -> StreamPrice | None:
        assert self._client is not None
        feed_id = self.feeds.get(asset)
        if not feed_id:
            return None
        key, secret = credentials()
        url = f"{REST_HOST}/api/v1/reports/latest?feedID={feed_id}"
        try:
            resp = await self._client.get(url, headers=auth_headers("GET", url, key, secret))
            if resp.status_code != 200:
                return None
            report = (resp.json() or {}).get("report") or {}
            price = _decode_price(report)
            if price is None:
                return None
            observed = float(report.get("observationsTimestamp")
                             or report.get("validFromTimestamp") or time.time())
            sp = StreamPrice(feed_id, asset, price, observed, time.time())
            self.latest[asset] = sp
            return sp
        except Exception:
            return None

    async def _poll_loop(self) -> None:
        """Poll every entitled feed.

        REST polling is the portable path; a WebSocket subscription is lower
        latency still and is the natural upgrade once this is proven against a
        real key. Kept as polling here because it is what can be verified
        without credentials.
        """
        while not self._stop.is_set():
            started = time.time()
            try:
                results = await asyncio.gather(
                    *(self.fetch_latest(a) for a in self.feeds), return_exceptions=True
                )
                ok = sum(1 for r in results if isinstance(r, StreamPrice))
                self.health.update(
                    active=ok > 0,
                    latency=round(time.time() - started, 3),
                    error=None if ok else "no reports returned",
                    resolved=ok,
                    polled=len(self.feeds),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.update(active=False, error=str(exc)[:160])
            await asyncio.sleep(max(0.25, 1.0 - (time.time() - started)))

    def price(self, asset: str) -> float | None:
        sp = self.latest.get(asset)
        return sp.price if sp else None

    def snapshot(self) -> dict[str, Any]:
        return {
            "configured": configured(),
            "health": self.health,
            "feeds": {a: self.feeds.get(a) for a in self.assets},
            "prices": {
                a: {"price": p.price, "latency_ms": round(p.latency_ms, 1),
                    "age_s": round(time.time() - p.received_at, 2)}
                for a, p in self.latest.items()
            },
        }


def _decode_price(report: dict[str, Any]) -> float | None:
    """Pull a human price out of a report payload.

    Data Streams returns prices as 18-decimal fixed point, sometimes as a
    decoded field and sometimes only inside the ABI-encoded blob. The decoded
    field is used when present; the blob is left to the caller rather than
    guessed at, because mis-decoding it silently yields a plausible-looking
    wrong number.
    """
    for field_name in ("price", "benchmarkPrice", "midPrice"):
        raw = report.get(field_name)
        if raw is None:
            continue
        try:
            value = int(raw, 16) if isinstance(raw, str) and raw.startswith("0x") else int(raw)
            return value / WEI
        except (TypeError, ValueError):
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue

    # Production REST responses carry the signed ABI-encoded report rather
    # than a decoded price field. All streams used by this bot are V3
    # reference-price reports: the benchmark price is tuple element 6.
    full_report = report.get("fullReport")
    feed_id = str(report.get("feedID") or report.get("feedId") or "")
    if isinstance(full_report, str) and full_report.startswith("0x") and feed_id[2:6] == "0003":
        try:
            from eth_abi import decode

            envelope = bytes.fromhex(full_report[2:])
            _, report_blob, _, _, _ = decode(
                ["bytes32[3]", "bytes", "bytes32[]", "bytes32[]", "bytes32"],
                envelope,
            )
            values = decode(
                [
                    "bytes32", "uint32", "uint32", "uint192", "uint192",
                    "uint32", "int192", "int192", "int192",
                ],
                report_blob,
            )
            return int(values[6]) / WEI
        except (ValueError, TypeError, OverflowError):
            return None
    return None
