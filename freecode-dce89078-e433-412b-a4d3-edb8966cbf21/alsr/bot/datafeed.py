"""Market data adapters.

Primary feed: Yahoo Finance public chart endpoint (free, no key, no SDK).
It provides real EUR/USD M5/M15/H1/H4 OHLC bars, published with roughly a
30-second delay and WITHOUT bid/ask. Spread is therefore an explicit assumption
shown to the user, never presented as a broker quote.

HTTP uses the standard library so the service has no extra runtime dependency and
behaves the same on Render, locally and in tests.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config import PIP, SYMBOL

TF_SECONDS = {"M5": 300, "M15": 900, "H1": 3600, "H4": 14400}
YAHOO_TF = {"M5": ("5m", "5d"), "M15": ("15m", "1mo"), "H1": ("60m", "3mo"), "H4": ("60m", "1y")}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; alsr-bot/2.0)",
    "Accept": "application/json",
}


class FeedError(RuntimeError):
    """Raised when a provider returns unusable data."""


def http_json(url: str, params: dict | None = None, timeout: int = 15) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code} від {urllib.parse.urlparse(url).netloc}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise FeedError(f"Мережа недоступна: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FeedError("Провайдер повернув не JSON") from exc


@dataclass
class Quote:
    bid: float
    ask: float
    mid: float
    spread_pips: float
    ts: datetime
    provider: str
    live: bool
    spread_is_assumed: bool = True


@dataclass
class Candle:
    t: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0
    complete: bool = True


@dataclass
class Quality:
    ok: bool
    status: str                          # VERIFIED | UNVERIFIED | DATA UNAVAILABLE
    notes: list[str] = field(default_factory=list)
    quote_age_sec: int | None = None
    cross_deviation_pips: float | None = None


class YahooFeed:
    name = "Yahoo Finance (free)"
    base = "https://query1.finance.yahoo.com/v8/finance/chart"

    def __init__(self, symbol: str = SYMBOL, assumed_spread_pips: float = 1.2) -> None:
        self.symbol = symbol
        self.assumed_spread_pips = assumed_spread_pips

    def _fetch(self, interval: str, rng: str) -> dict:
        payload = http_json(f"{self.base}/{self.symbol}", {"interval": interval, "range": rng})
        result = (payload.get("chart") or {}).get("result")
        error = (payload.get("chart") or {}).get("error")
        if not result:
            raise FeedError(f"Yahoo: {error or 'порожня відповідь'}")
        return result[0]

    def quote(self) -> Quote:
        data = self._fetch("1m", "1d")
        meta = data.get("meta") or {}
        price = meta.get("regularMarketPrice")
        if not price:
            raise FeedError("Yahoo: немає актуальної ціни")
        ts_raw = meta.get("regularMarketTime")
        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc) if ts_raw else datetime.now(timezone.utc)
        half = self.assumed_spread_pips * PIP / 2
        return Quote(
            bid=round(price - half, 5),
            ask=round(price + half, 5),
            mid=float(price),
            spread_pips=self.assumed_spread_pips,
            ts=ts,
            provider=f"{self.name} · spread припущений",
            live=True,
            spread_is_assumed=True,
        )

    def candles(self, tf: str, limit: int = 1500) -> list[Candle]:
        interval, rng = YAHOO_TF[tf]
        data = self._fetch(interval, rng)
        ts_list = data.get("timestamp") or []
        blocks = (data.get("indicators") or {}).get("quote") or [{}]
        q = blocks[0]
        opens, highs, lows, closes, vols = q.get("open"), q.get("high"), q.get("low"), q.get("close"), q.get("volume")
        if not ts_list or not closes:
            raise FeedError(f"Yahoo: немає свічок {tf}")

        out: list[Candle] = []
        for i, t in enumerate(ts_list):
            try:
                o, h, l, c = opens[i], highs[i], lows[i], closes[i]
            except (IndexError, TypeError):
                continue
            if None in (o, h, l, c):
                continue
            vol = vols[i] if vols and i < len(vols) and vols[i] is not None else 0
            out.append(Candle(int(t), float(o), float(h), float(l), float(c), float(vol)))

        step = TF_SECONDS[tf]
        now = int(time.time())
        if tf == "H4":
            out = aggregate(out, 14400)
        if out:
            out[-1].complete = (out[-1].t + step) <= now
        return out[-limit:]


class ReferenceValidator:
    """Daily reference rate used only to spot a wildly wrong primary quote."""

    name = "open.er-api (добовий довідковий курс)"
    url = "https://open.er-api.com/v6/latest/EUR"

    @staticmethod
    def quote() -> Quote:
        data = http_json(ReferenceValidator.url)
        rate = (data.get("rates") or {}).get("USD")
        if not rate:
            raise FeedError("Довідковий курс недоступний")
        ts_raw = data.get("time_last_update_unix")
        ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc) if ts_raw else datetime.now(timezone.utc)
        return Quote(float(rate), float(rate), float(rate), 0.0, ts, ReferenceValidator.name, True, spread_is_assumed=False)


def aggregate(candles: list[Candle], bucket_seconds: int) -> list[Candle]:
    """Aggregate base candles into fixed UTC buckets (e.g. 1h -> H4)."""
    if not candles:
        return []
    buckets: dict[int, Candle] = {}
    for c in candles:
        key = (c.t // bucket_seconds) * bucket_seconds
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = Candle(key, c.o, c.h, c.l, c.c, c.v, True)
        else:
            existing.h = max(existing.h, c.h)
            existing.l = min(existing.l, c.l)
            existing.c = c.c
            existing.v += c.v
    ordered = [buckets[k] for k in sorted(buckets)]
    now = int(time.time())
    if ordered:
        ordered[-1].complete = (ordered[-1].t + bucket_seconds) <= now
    return ordered


def _expected_gap(a: Candle, b: Candle, step: int) -> bool:
    """True if the gap between two bars is NOT explained by a market closure."""
    delta = b.t - a.t
    if delta <= step * 1.5:
        return False
    # A gap that spans more than the normal step may be a weekend/holiday closure.
    # Weekend in FX: Fri 21:00 UTC -> Sun 22:00 UTC (approx 49h).
    if delta >= 36 * 3600:
        return False
    return True


def validate_candles(tf: str, candles: list[Candle], now: int | None = None) -> tuple[list[Candle], list[str], bool]:
    now = now or int(time.time())
    step = TF_SECONDS[tf]
    notes: list[str] = []
    seen: set[int] = set()
    clean: list[Candle] = []
    bad = 0

    for c in sorted(candles, key=lambda x: x.t):
        if c.h < c.l or c.c > c.h * 1.0002 or c.c < c.l * 0.9998:
            bad += 1
            continue
        if c.t > now + step:
            bad += 1
            continue
        if c.t in seen:
            continue
        seen.add(c.t)
        clean.append(c)

    gaps = sum(1 for a, b in zip(clean, clean[1:]) if _expected_gap(a, b, step))
    if bad:
        notes.append(f"{tf}: {bad} некоректних свічок відкинуто.")
    if gaps:
        notes.append(f"{tf}: {gaps} пропуск(ів) у свічках.")
    if clean:
        age = now - clean[-1].t
        if age > step * 3:
            notes.append(f"{tf}: остання свічка віком {age // 60} хв.")
    else:
        notes.append(f"{tf}: свічок немає.")

    ok = bool(clean) and len(clean) >= 60 and gaps <= max(3, len(clean) // 50) and (now - clean[-1].t) <= step * 3
    return clean, notes, ok


@dataclass
class Snapshot:
    quote: Quote | None
    candles: dict[str, list[Candle]]
    quality: Quality
    validator: Quote | None
    generated_at: datetime


def build_snapshot(limit: int = 1500) -> Snapshot:
    feed = YahooFeed()
    notes: list[str] = []
    now = int(time.time())

    quote: Quote | None = None
    try:
        quote = feed.quote()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Котирування недоступне: {exc}")

    validator: Quote | None = None
    deviation: float | None = None
    try:
        validator = ReferenceValidator.quote()
        if quote:
            deviation = round(abs(quote.mid - validator.mid) / PIP, 1)
    except Exception as exc:  # noqa: BLE001
        notes.append(f"Звірка ціни недоступна: {exc}")

    candles: dict[str, list[Candle]] = {}
    all_ok = True
    for tf in ("M5", "M15", "H1", "H4"):
        try:
            clean, tf_notes, ok = validate_candles(tf, feed.candles(tf, limit), now)
            candles[tf] = clean
            notes.extend(tf_notes)
            all_ok = all_ok and ok
        except Exception as exc:  # noqa: BLE001
            candles[tf] = []
            notes.append(f"{tf}: помилка завантаження — {exc}")
            all_ok = False

    age = None
    status = "VERIFIED"
    if quote is None:
        status = "DATA UNAVAILABLE"
    else:
        age = max(0, int((datetime.now(timezone.utc) - quote.ts).total_seconds()))
        if age > 600:
            status = "UNVERIFIED"
            notes.append(f"Котирування застаріле на {age} с.")
        elif not all_ok:
            status = "UNVERIFIED"

    if deviation is not None and deviation > 150:
        status = "UNVERIFIED"
        notes.append(f"Primary і довідковий курс розходяться на {deviation} pips — перевір джерело.")

    return Snapshot(
        quote=quote,
        candles=candles,
        quality=Quality(ok=status == "VERIFIED", status=status, notes=notes, quote_age_sec=age, cross_deviation_pips=deviation),
        validator=validator,
        generated_at=datetime.now(timezone.utc),
    )
