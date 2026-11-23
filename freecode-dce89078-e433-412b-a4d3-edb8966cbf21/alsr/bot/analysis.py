"""Market structure engine: swings, BOS/CHoCH, displacement, FVG, levels.

Pure functions over candles. No I/O, so the same code runs live and in backtests.
Indicators are filters, never the reason for an entry.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from datafeed import Candle

PIP = 0.0001


@dataclass
class Swing:
    t: int
    price: float
    kind: str  # high | low


@dataclass
class Zone:
    kind: str        # FVG | ORDER_BLOCK | DAY_HIGH | DAY_LOW | WEEK_HIGH | WEEK_LOW
    low: float
    high: float
    label: str
    direction: str | None = None
    t: int | None = None


@dataclass
class Frame:
    tf: str
    structure: str                 # bull | bear | range
    swings: list[Swing] = field(default_factory=list)
    bos: dict | None = None
    choch: dict | None = None
    displacement: dict | None = None
    zones: list[Zone] = field(default_factory=list)
    range_high: float | None = None
    range_low: float | None = None
    atr: float = 0.0
    ema20: float = 0.0
    ema50: float = 0.0
    rsi14: float = 50.0
    notes: list[str] = field(default_factory=list)


def completed(candles: list[Candle]) -> list[Candle]:
    return [c for c in candles if c.complete]


def ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    k = 2 / (period + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def atr(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(candles, candles[1:]):
        trs.append(max(cur.h - cur.l, abs(cur.h - prev.c), abs(cur.l - prev.c)))
    tail = trs[-period:]
    return sum(tail) / len(tail) if tail else 0.0


def rsi(candles: list[Candle], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 50.0
    gains = losses = 0.0
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:]):
        delta = cur.c - prev.c
        if delta >= 0:
            gains += delta
        else:
            losses -= delta
    avg_gain, avg_loss = gains / period, losses / period
    if avg_loss == 0:
        return 100.0 if avg_gain else 50.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def find_swings(candles: list[Candle], strength: int = 2) -> list[Swing]:
    out: list[Swing] = []
    for i in range(strength, len(candles) - strength):
        c = candles[i]
        is_high = all(candles[i - j].h < c.h and candles[i + j].h < c.h for j in range(1, strength + 1))
        is_low = all(candles[i - j].l > c.l and candles[i + j].l > c.l for j in range(1, strength + 1))
        if is_high:
            out.append(Swing(c.t, c.h, "high"))
        if is_low:
            out.append(Swing(c.t, c.l, "low"))
    return sorted(out, key=lambda s: s.t)


def detect_displacement(candles: list[Candle], atr_value: float) -> dict | None:
    if len(candles) < 3 or atr_value <= 0:
        return None
    c = candles[-1]
    body = abs(c.c - c.o)
    rng = max(c.h - c.l, 1e-9)
    strength = body / atr_value
    if strength >= 1.35 and body / rng >= 0.62:
        return {"dir": "up" if c.c > c.o else "down", "strength": round(strength, 2)}
    return None


def fair_value_gaps(candles: list[Candle], lookback: int = 40) -> list[Zone]:
    zones: list[Zone] = []
    start = max(2, len(candles) - lookback)
    for i in range(start, len(candles)):
        a, mid, c = candles[i - 2], candles[i - 1], candles[i]
        if c.l > a.h:
            zones.append(Zone("FVG", a.h, c.l, "Bullish FVG", "bull", mid.t))
        elif c.h < a.l:
            zones.append(Zone("FVG", c.h, a.l, "Bearish FVG", "bear", mid.t))
    return zones[-6:]


def analyse(tf: str, input_candles: list[Candle]) -> Frame:
    candles = completed(input_candles)
    frame = Frame(tf=tf, structure="range")
    if not candles:
        frame.notes.append(f"{tf}: немає закритих свічок.")
        return frame

    closes = [c.c for c in candles]
    frame.atr = atr(candles)
    frame.ema20 = ema(closes[-120:], 20)
    frame.ema50 = ema(closes[-200:], 50)
    frame.rsi14 = rsi(candles)
    strength = 3 if tf == "H4" else 2
    swings = find_swings(candles, strength)[-14:]
    frame.swings = swings

    last = candles[-1]
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]

    structure = "range"
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1].price > highs[-2].price
        hl = lows[-1].price > lows[-2].price
        lh = highs[-1].price < highs[-2].price
        ll = lows[-1].price < lows[-2].price
        if (hh and hl) or (hh and not ll):
            structure = "bull"
        elif (lh and ll) or (ll and not hh):
            structure = "bear"

    if highs and last.c > highs[-1].price:
        frame.bos = {"dir": "up", "level": highs[-1].price, "t": last.t}
        structure = "bull"
    elif lows and last.c < lows[-1].price:
        frame.bos = {"dir": "down", "level": lows[-1].price, "t": last.t}
        structure = "bear"

    if frame.bos:
        flip = (frame.bos["dir"] == "up" and structure == "bear") or (frame.bos["dir"] == "down" and structure == "bull")
        if flip:
            frame.choch = frame.bos

    frame.structure = structure
    frame.displacement = detect_displacement(candles, frame.atr)
    frame.zones = fair_value_gaps(candles, 40 if tf == "M5" else 60)

    window = candles[-40:]
    frame.range_high = max(c.h for c in window)
    frame.range_low = min(c.l for c in window)

    if len(candles) < 60:
        frame.notes.append(f"{tf}: мало історії ({len(candles)} свічок).")
    return frame


def day_week_levels(h1: list[Candle], now_epoch: int, warsaw_offset_sec: int = 3600) -> list[Zone]:
    """Previous trading day / previous week extremes, Warsaw-local day buckets.

    Uses the most recent *trading* day that actually has bars, so Monday correctly
    refers back to Friday instead of an empty calendar Sunday.
    """
    zones: list[Zone] = []
    if not h1:
        return zones
    day = 86400
    shift = warsaw_offset_sec

    def bucket(ts: int) -> int:
        return ((ts + shift) // day) * day - shift

    today = bucket(now_epoch)
    by_day: dict[int, list[Candle]] = {}
    for c in h1:
        b = bucket(c.t)
        if b < today:
            by_day.setdefault(b, []).append(c)

    if not by_day:
        return zones

    last_day_key = max(by_day)
    prev = by_day[last_day_key]
    if len(prev) >= 4:  # ignore partial/holiday stubs
        hi, lo = max(c.h for c in prev), min(c.l for c in prev)
        zones.append(Zone("DAY_HIGH", hi, hi, "Previous day high"))
        zones.append(Zone("DAY_LOW", lo, lo, "Previous day low"))

    # Week bucket: Monday-based, Warsaw local.
    def week_key(ts: int) -> int:
        import datetime as _dt

        local = _dt.datetime.fromtimestamp(ts + shift, tz=_dt.timezone.utc)
        monday = (local - _dt.timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(monday.timestamp()) - shift

    current_week = week_key(now_epoch)
    week_bars = [c for c in h1 if week_key(c.t) < current_week]
    if week_bars:
        last_week = max(week_key(c.t) for c in week_bars)
        prev_week = [c for c in week_bars if week_key(c.t) == last_week]
        if len(prev_week) >= 20:
            hi, lo = max(c.h for c in prev_week), min(c.l for c in prev_week)
            zones.append(Zone("WEEK_HIGH", hi, hi, "Previous week high"))
            zones.append(Zone("WEEK_LOW", lo, lo, "Previous week low"))
    return zones


def liquidity_targets(frame: Frame, price: float, limit: int = 3) -> tuple[list[float], list[float]]:
    above = sorted(s.price for s in frame.swings if s.price > price)[:limit]
    below = sorted((s.price for s in frame.swings if s.price < price), reverse=True)[:limit]
    return above, below
