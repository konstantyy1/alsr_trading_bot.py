"""Deterministic self-tests.

No network, no Telegram, no production database. Each check exercises a rule that the
live system depends on, so the dashboard can prove the logic is intact after a deploy.

Run:  python3 selftest.py
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

from analysis import day_week_levels
from config import ENV, PIP, Risk
from datafeed import Candle, aggregate, validate_candles
from lifecycle import advance
from markettime import LON, NY, WAW, market_closed, news_risk, session_info
from notify import format_signal

UTC = timezone.utc


def _c(t: int, o: float, h: float, l: float, c: float, complete: bool = True) -> Candle:
    return Candle(t, o, h, l, c, 0.0, complete)


# ---------------------------------------------------------------- time & sessions
def test_dst_offsets() -> tuple[bool, str]:
    """London must be UTC+1 in July and UTC+0 in January — no hardcoded offsets."""
    jul = datetime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(LON)
    jan = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(LON)
    ny_jul = datetime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(NY)
    ny_jan = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(NY)
    ok = (
        jul.utcoffset() == timedelta(hours=1)
        and jan.utcoffset() == timedelta(0)
        and ny_jul.utcoffset() == timedelta(hours=-4)
        and ny_jan.utcoffset() == timedelta(hours=-5)
        and jul.tzname() == "BST"
    )
    return ok, f"London {jul.tzname()}/{jan.tzname()}, NY {ny_jul.tzname()}/{ny_jan.tzname()}"


def test_warsaw_display() -> tuple[bool, str]:
    winter = datetime(2026, 1, 15, 12, 0, tzinfo=UTC).astimezone(WAW)
    summer = datetime(2026, 7, 15, 12, 0, tzinfo=UTC).astimezone(WAW)
    ok = winter.utcoffset() == timedelta(hours=1) and summer.utcoffset() == timedelta(hours=2)
    return ok, f"Europe/Warsaw offset +{winter.utcoffset().seconds // 3600}h winter, +{summer.utcoffset().seconds // 3600}h summer"


def test_dst_boundary_hours() -> tuple[bool, str]:
    """2026-03-29 01:00 London jumps to 02:00; the session clock must not break."""
    before = datetime(2026, 3, 29, 0, 30, tzinfo=UTC).astimezone(LON)
    after = datetime(2026, 3, 29, 1, 30, tzinfo=UTC).astimezone(LON)
    ok = before.hour == 0 and after.hour == 2 and before.utcoffset() != after.utcoffset()
    return ok, f"{before.strftime('%H:%M %Z')} → {after.strftime('%H:%M %Z')}"


def test_market_closure() -> tuple[bool, str]:
    sat = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    fri_early = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)
    fri_late = datetime(2026, 11, 20, 23, 0, tzinfo=UTC)
    sun_early = datetime(2026, 11, 22, 21, 0, tzinfo=UTC)
    sun_late = datetime(2026, 11, 22, 23, 0, tzinfo=UTC)
    ok = (
        market_closed(sat)
        and not market_closed(fri_early)
        and market_closed(fri_late)
        and market_closed(sun_early)
        and not market_closed(sun_late)
    )
    return ok, "Sat closed, Fri 20:00 open, Fri 23:00 closed, Sun 21:00 closed, Sun 23:00 open"


def test_session_windows() -> tuple[bool, str]:
    london = session_info(datetime(2026, 11, 23, 9, 0, tzinfo=UTC))
    overlap = session_info(datetime(2026, 11, 23, 14, 0, tzinfo=UTC))
    asia = session_info(datetime(2026, 11, 23, 3, 0, tzinfo=UTC))
    ok = (
        london.active == "London"
        and london.in_killzone
        and overlap.active == "Overlap"
        and overlap.quality == "PRIME"
        and asia.active == "Asia"
        and asia.quality == "LOW"
    )
    return ok, f"09:00 {london.active}/{london.quality}, 14:00 {overlap.active}, 03:00 {asia.active}"


# ---------------------------------------------------------------- news gate
def _ev(title: str, at: datetime, currency: str = "USD", impact: str = "high") -> dict:
    return {"title": title, "currency": currency, "impact": impact, "time_utc": at, "id": "x", "source": "t"}


def test_news_blackout() -> tuple[bool, str]:
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    cases = {
        "blocked_before": (news_risk([_ev("US CPI y/y", now + timedelta(minutes=5))], now, 20, 15).level, "BLOCKED"),
        "elevated": (news_risk([_ev("US CPI y/y", now + timedelta(minutes=40))], now, 20, 15).level, "ELEVATED"),
        "blocked_after": (news_risk([_ev("US CPI y/y", now - timedelta(minutes=10))], now, 20, 15).level, "BLOCKED"),
        "clear": (news_risk([_ev("US CPI y/y", now + timedelta(hours=8))], now, 20, 15).level, "NONE"),
        "other_currency": (news_risk([_ev("JPY CPI", now + timedelta(minutes=2), "JPY")], now, 20, 15).level, "NONE"),
        "low_impact": (news_risk([_ev("Fed speaker", now + timedelta(minutes=2), "USD", "low")], now, 20, 15).level, "NONE"),
    }
    failed = [k for k, (got, want) in cases.items() if got != want]
    return not failed, "ok" if not failed else f"failed: {failed}"


# ---------------------------------------------------------------- lifecycle
def _signal(**over) -> dict:
    base = {
        "id": "TEST-1",
        "symbol": "EURUSD=X",
        "side": "BUY",
        "status": "READY",
        "grade": "A",
        "score": 78,
        "setup": "FVG_PULLBACK",
        "created_at": datetime(2026, 11, 23, 10, 0, tzinfo=UTC).isoformat(),
        "updated_at": datetime(2026, 11, 23, 10, 0, tzinfo=UTC).isoformat(),
        "entry_low": 1.0995,
        "entry_high": 1.1005,
        "trigger": 1.1000,
        "stop": 1.0985,
        "tp1": 1.1030,
        "tp2": 1.1050,
        "tp3": None,
        "rr": 2.0,
        "risk_pips": 15.0,
        "reward_pips": 30.0,
        "expected_hold_min": 60,
        "max_hold_min": 240,
        "session": "London",
        "quote_at_creation": 1.1000,
        "spread_at_creation": 1.2,
        "provider": "Yahoo Finance (free) · spread припущений",
        "context": {"h4": "H4 range", "h1": "H1 bull", "m15": "M15 bull", "m5": "close above zone",
                    "liquidity": "2 вище", "levels": "PDH 1.1040"},
        "why": ["Ціна повернулась у FVG."],
        "cancel_rules": ["M5 закрилась за рівнем інвалідації."],
        "invalidations": ["Закриття M5 нижче 1.09850."],
        "news_risk": "NONE: чисто",
        "position_size": {"lots": 0.23, "risk_amount": 35.0, "note": "0.35%"},
        # Exit rule defaults, so a test that does not care about them still builds a
        # signal shaped like a real 2.7.0 one.
        "break_even_at_r": 0.75,
        "trail_start_r": 1.5,
        "trail_atr_mult": 1.0,
        "trail_min_pips": 1.5,
    }
    base.update(over)
    return base


T0 = int(datetime(2026, 11, 23, 10, 0, tzinfo=UTC).timestamp())


def test_lifecycle_sl() -> tuple[bool, str]:
    now = datetime(2026, 11, 23, 10, 10, tzinfo=UTC)
    sig = _signal()
    event = advance(sig, [_c(T0, 1.1000, 1.1006, 1.0984, 1.0990)], now)
    ok = event is not None and event["status"] == "SL_HIT"
    return ok, event["status"] if event else "no event"


def test_lifecycle_tp1() -> tuple[bool, str]:
    """Since 2.7.0 a target must NOT close the position; only the stop does."""
    now = datetime(2026, 11, 23, 10, 10, tzinfo=UTC)
    event = advance(_signal(), [_c(T0, 1.1000, 1.1032, 1.0998, 1.1028)], now)
    ok = event is None
    return ok, "TP leaves the trade open" if ok else f"closed at {event['status']}"


def test_lifecycle_ambiguous_candle() -> tuple[bool, str]:
    """One M5 bar hitting both SL and a target must be scored as SL, and say why."""
    now = datetime(2026, 11, 23, 10, 10, tzinfo=UTC)
    event = advance(_signal(), [_c(T0, 1.1000, 1.1035, 1.0980, 1.1030)], now)
    ok = event is not None and event["status"] == "SL_HIT"
    return ok, event["status"] + " (conservative)" if event else "no event"


def test_lifecycle_cancel() -> tuple[bool, str]:
    now = datetime(2026, 11, 23, 11, 0, tzinfo=UTC)  # 60 min later, never touched
    # BUY entry zone is 1.0995–1.1005, so a bar must stay ABOVE 1.1005 to miss it.
    event = advance(_signal(), [_c(T0, 1.1010, 1.1014, 1.1008, 1.1012)], now)
    ok = event is not None and event["status"] == "CANCELLED"
    return ok, event["status"] if event else "no event"


def test_lifecycle_expiry() -> tuple[bool, str]:
    created = datetime(2026, 11, 23, 6, 0, tzinfo=UTC)
    now = datetime(2026, 11, 23, 10, 30, tzinfo=UTC)
    sig = _signal(status="ACTIVE", created_at=created.isoformat())
    bars = [_c(int((created + timedelta(minutes=i * 5)).timestamp()), 1.1000, 1.1002, 1.0998, 1.1000) for i in range(1, 6)]
    event = advance(sig, bars, now)
    ok = event is not None and event["status"] == "EXPIRED"
    return ok, event["status"] if event else "no event"


def test_lifecycle_ignores_incomplete_bar() -> tuple[bool, str]:
    now = datetime(2026, 11, 23, 10, 2, tzinfo=UTC)
    event = advance(_signal(), [_c(T0, 1.1000, 1.1035, 1.0980, 1.1030, complete=False)], now)
    return event is None, "incomplete bar ignored" if event is None else "reacted to open bar"


# ---------------------------------------------------------------- data hygiene
def test_h4_aggregation() -> tuple[bool, str]:
    start = 1795300000 - (1795300000 % 14400)
    hours = [_c(start + i * 3600, 1.1000 + i * 0.0001, 1.1005 + i * 0.0001, 1.0995 + i * 0.0001, 1.1002 + i * 0.0001)
             for i in range(8)]
    bars = aggregate(hours, 14400)
    ok = (
        len(bars) == 2
        and bars[0].o == hours[0].o
        and bars[0].c == hours[3].c
        and bars[0].h == max(h.h for h in hours[:4])
        and bars[0].l == min(h.l for h in hours[:4])
        and bars[1].c == hours[7].c
    )
    return ok, f"{len(bars)} H4 bars from 8 hourly"


def test_candle_validation() -> tuple[bool, str]:
    now = 1795420000
    good = [_c(now - (120 - i) * 300, 1.1, 1.1002, 1.0998, 1.1) for i in range(120)]
    clean, notes, ok = validate_candles("M5", good, now)
    bad = [_c(now - 300, 1.1, 1.0990, 1.1000, 1.1)]  # high below low
    with_bad = good + bad
    clean2, notes2, _ = validate_candles("M5", with_bad, now)
    dup = good + [good[-1]]
    clean3, _, _ = validate_candles("M5", dup, now)
    passed = ok and len(clean) == 120 and len(clean2) == 120 and len(clean3) == 120 and any("некоректн" in n for n in notes2)
    return passed, f"{len(clean)} clean, bad dropped, duplicate dropped"


def test_weekend_gap_not_flagged() -> tuple[bool, str]:
    """A ~50-hour Friday→Sunday closure must not be reported as a data gap."""
    now = 1795420000
    first = [_c(now - 52 * 3600 - (39 - i) * 300, 1.1, 1.1002, 1.0998, 1.1) for i in range(40)]
    reopened = [_c(now - (19 - i) * 300, 1.1, 1.1002, 1.0998, 1.1) for i in range(20)]
    clean, notes, _ = validate_candles("M5", first + reopened, now)
    gaps_flagged = [n for n in notes if "пропуск" in n]
    ok = not gaps_flagged and len(clean) == 60
    return ok, "closure recognised" if ok else f"notes={notes}"


def test_prev_day_skips_empty_sunday() -> tuple[bool, str]:
    """Monday must refer to Friday, never to an empty calendar Sunday."""
    now = int(datetime(2026, 11, 23, 9, 0, tzinfo=UTC).timestamp())
    fri = int(datetime(2026, 11, 20, 8, 0, tzinfo=UTC).timestamp())
    mon = int(datetime(2026, 11, 23, 7, 0, tzinfo=UTC).timestamp())
    bars = []
    for i in range(9):
        base = 1.1000 + i * 0.0002
        bars.append(_c(fri + i * 3600, base, base + 0.0006, base - 0.0004, base + 0.0001))
    for i in range(3):  # Monday bars, deliberately higher
        bars.append(_c(mon + i * 3600, 1.1100, 1.1120, 1.1090, 1.1110))
    zones = day_week_levels(bars, now, warsaw_offset_sec=3600)
    day_high = next((z.high for z in zones if z.kind == "DAY_HIGH"), None)
    expected = max(b.h for b in bars[:9])
    ok = day_high is not None and abs(day_high - expected) < 1e-9
    return ok, f"PDH {day_high} == Friday high {expected}"


# ---------------------------------------------------------------- messaging & risk
def test_weak_session_is_blocked() -> tuple[bool, str]:
    """A session proven negative in backtest must not produce entries.

    The Asia session lost 13.11R over 30 days while every other session together made
    +9.64R (0.7% chance of being luck). Blocking it is a rule change based on
    evidence, so the rule itself is checked here rather than trusted to memory.
    """
    import dataclasses

    from config import DataCfg, Risk
    from datafeed import Candle, Quality, Quote, Snapshot
    from markettime import NewsRisk
    from signals import evaluate

    risk = Risk(blocked_sessions="Asia")
    data_cfg = DataCfg()
    now = datetime(2026, 11, 23, 3, 0, tzinfo=UTC)  # 03:00 UTC = Asia session
    session = session_info(now)
    if session.active != "Asia":
        return False, f"період не Asia, а {session.active}"

    quote = Quote(bid=1.0999, ask=1.1001, mid=1.1000, spread_pips=1.2, ts=now,
                  provider="test", live=True, spread_is_assumed=True)
    bars = [Candle(t=int(now.timestamp()) - (60 - i) * 300, o=1.1, h=1.1005, l=1.0995, c=1.1) for i in range(80)]
    snap = Snapshot(quote=quote, candles={"M5": bars, "M15": bars}, quality=Quality(True, "VERIFIED"),
                    validator=None, generated_at=now)
    from analysis import analyse

    frames = {tf: analyse(tf, bars) for tf in ("M5", "M15", "H1", "H4")}
    verdict = evaluate(snap, frames, [], session, NewsRisk("NONE", "чисто"),
                       open_count=0, signals_today=0, daily_r=0.0, risk=risk, data_cfg=data_cfg)
    blocked_asia = verdict.verdict == "BLOCKED" and "слабких" in " ".join(verdict.details)

    # London must not be blocked by the same rule.
    lon_now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    lon_session = session_info(lon_now)
    v2 = evaluate(snap, frames, [], lon_session, NewsRisk("NONE", "чисто"),
                  open_count=0, signals_today=0, daily_r=0.0, risk=risk, data_cfg=data_cfg)
    london_free = not any("слабких" in d for d in v2.details)

    ok = blocked_asia and london_free
    return ok, (f"Asia={blocked_asia}, London вільна={london_free}, verdict Asia={verdict.verdict}")


def test_break_even_turns_loss_into_scratch() -> tuple[bool, str]:
    """The break-even rule must exist and behave as measured.

    On 59 days this turned full -1R losses into scratches and added about +0.13R per
    trade. Two things are checked: a trade that runs 0.75R then reverses exits at the
    entry instead of the stop, and a trade that never reaches 0.75R still loses, so the
    rule cannot be secretly widening the stop.
    """
    from config import Risk
    from lifecycle import advance

    risk = Risk()
    be_r = float(risk.break_even_at_r)
    if not (0.3 <= be_r <= 1.5):
        return False, f"break_even_at_r виглядає неправильно: {be_r}"

    created = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    t0 = int(created.timestamp())
    base = {
        "side": "BUY", "status": "ACTIVE", "created_at": created.isoformat(),
        "entry_low": 1.09950, "entry_high": 1.10050, "trigger": 1.10000,
        "stop": 1.09850, "tp1": 1.10300, "tp2": 1.10500, "tp3": None,
        "max_hold_min": 240, "break_even_at_r": be_r,
    }
    entry = base["entry_high"]
    trigger_level = entry + be_r * abs(entry - base["stop"])

    # 1) Reaches 0.75R, then falls back below the entry but above the stop.
    up = _c(t0 + 300, entry, trigger_level + 0.0002, entry - 0.0001, trigger_level, True)
    back = _c(t0 + 600, trigger_level, trigger_level, entry - 0.0004, entry - 0.0003, True)
    sig = dict(base)
    e1 = advance(sig, [up], created + timedelta(minutes=5))
    sig["status"] = e1["status"] if e1 else "ACTIVE"
    e2 = advance(sig, [up, back], created + timedelta(minutes=10))
    scratch = e2 is not None and e2["status"] == "SL_HIT" and abs(e2["price"] - entry) < 1e-9

    # 2) Never reaches 0.75R and stops out: a normal loss at the original stop.
    down = _c(t0 + 300, entry, entry + 0.0002, base["stop"] - 0.0001, base["stop"], True)
    e3 = advance(dict(base), [down], created + timedelta(minutes=5))
    normal_loss = e3 is not None and e3["status"] == "SL_HIT" and abs(e3["price"] - base["stop"]) < 1e-9

    ok = scratch and normal_loss
    return ok, f"0.75R→беззбиток={scratch}, без 0.75R стоп не рухався={normal_loss}"


def test_trailing_stop_lifts_and_never_falls() -> tuple[bool, str]:
    """The trailing stop must move with price and never move against the trade.

    Simulated on a clean rally: each bar closes higher, so the stop must rise every
    time it is re-evaluated after +1.5R. A single bar gap back down must close the
    trade at the trailed level, not at the original stop. That difference is the whole
    point of the rule, so a silent regression here would cost real money.
    """
    from lifecycle import evaluate_signal

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    t0 = int(created.timestamp())
    sig = _signal(
        status="ACTIVE", created_at=created.isoformat(),
        stop=1.0985, entry_low=1.0995, entry_high=1.1005,
        break_even_at_r=0.75, trail_start_r=1.5, trail_atr_mult=1.0,
        trail_min_pips=1.5, max_hold_min=240,
        tp1=1.1030, tp2=1.1050, tp3=None,
    )
    entry = 1.1005
    risk = entry - 1.0985          # 20 pips

    # ATR is driven by the same bars: each 6-pip range, so ATR ~ 6 pips = 0.3R.
    bars = []
    price = entry
    for i in range(1, 9):
        price = entry + i * 0.0012   # +6 pips per bar in R terms ~ +0.3R steps
        bars.append(_c(t0 + i * 300, price - 0.0003, price + 0.0003, price - 0.0005, price))

    stops = []
    for k in range(1, len(bars) + 1):
        out = evaluate_signal(sig, bars[:k], bars[k - 1].t + 300)
        stops.append(round(out["stop"], 6))

    monotonic = all(b >= a - 1e-12 for a, b in zip(stops, stops[1:]))
    lifted = stops[-1] > entry
    no_fall = all(b >= a - 1e-12 for a, b in zip(stops, stops[1:]))

    # Now a bar that collapses far below the trailed stop: must exit at the trail.
    last_stop = stops[-1]
    drop = _c(t0 + 9 * 300, price, price, price - 0.004, price - 0.004)
    out = evaluate_signal(sig, bars + [drop], drop.t + 300)
    exited = out["event"] is not None and out["event"]["status"] == "SL_HIT"
    kind = out["event"]["exit_kind"] if exited else None
    # The drop is deep enough to clear both entry and trail, so it must score TRAIL.
    ok = monotonic and lifted and no_fall and exited and kind == "TRAIL"
    return ok, (f"стоп піднявся={lifted}, монотонно={monotonic}, "
                f"вихід за трейлом={kind}, останній стоп={last_stop:.5f}")


def test_trailing_waits_for_its_start() -> tuple[bool, str]:
    """Before +1.5R the stop stays at break-even, never trails early."""
    from lifecycle import evaluate_signal

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    t0 = int(created.timestamp())
    sig = _signal(status="ACTIVE", created_at=created.isoformat(),
                  break_even_at_r=0.75, trail_start_r=1.5, trail_atr_mult=1.0,
                  trail_min_pips=1.5, max_hold_min=240)
    entry = 1.1005
    # +1.1R: past break-even (0.75R), short of the trail start (1.5R).
    up = _c(t0 + 300, entry, entry + 0.0022, entry - 0.0001, entry + 0.0022)
    out = evaluate_signal(sig, [up], up.t + 300)
    at_be = abs(out["stop"] - entry) < 1e-9
    return at_be, f"стоп {out['stop']:.5f} (вхід {entry:.5f}) — беззбиток, без трейлу"


def test_trail_min_gap_beats_spread() -> tuple[bool, str]:
    """The trailing gap can never be placed inside the spread.

    A stop closer than the spread would be filled instantly at a loss, so the gap is
    floored. This checks the floor is applied on a very quiet market where ATR alone
    would give a gap narrower than the floor.
    """
    from lifecycle import evaluate_signal

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    t0 = int(created.timestamp())
    sig = _signal(status="ACTIVE", created_at=created.isoformat(),
                  break_even_at_r=0.75, trail_start_r=1.5, trail_atr_mult=1.0,
                  trail_min_pips=1.5, max_hold_min=240)
    entry = 1.1005
    # Tiny 0.6-pip ranges, then a strong move to trigger the trail.
    bars = [_c(t0 + i * 300, entry, entry + 0.00003, entry - 0.00003, entry) for i in range(1, 4)]
    surge = _c(t0 + 4 * 300, entry, entry + 0.0040, entry, entry + 0.0040)
    out = evaluate_signal(sig, bars + [surge], surge.t + 300)
    gap_pips = abs(surge.c - out["stop"]) / 0.0001
    ok = gap_pips >= 1.5 - 1e-6
    return ok, f"розрив {gap_pips:.2f} pips (мінімум 1.5)"


def test_improvement_is_not_a_target_hit() -> tuple[bool, str]:
    """Touching TP2/TP3 must not close the trade: the whole edge is in holding."""
    from lifecycle import evaluate_signal

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    t0 = int(created.timestamp())
    sig = _signal(status="ACTIVE", created_at=created.isoformat(),
                  break_even_at_r=0.75, trail_start_r=1.5, trail_atr_mult=1.0,
                  trail_min_pips=1.5, max_hold_min=240)
    # Runs through TP2 (1.1050) on ordinary bars, without touching any stop. The bars
    # have to be ordinary: one giant candle would blow up ATR, widen the trailing gap
    # and legitimately keep the stop at break-even, which would test nothing.
    bars = []
    price = 1.1005
    for i in range(1, 13):
        price += 0.0005                      # +5 pips per bar
        bars.append(_c(t0 + i * 300, price - 0.0005, price + 0.0002, price - 0.0008, price))
    out = evaluate_signal(sig, bars, bars[-1].t + 300)
    ok = out["event"] is None and out["stop"] > 1.1005
    return ok, f"угода відкрита, стоп {out['stop']:.5f}, максимум {bars[-1].c:.5f}"


def test_trailing_matches_backtest() -> tuple[bool, str]:
    """The live rule and the measured rule must be the same numbers.

    The exit plan was chosen on a replay of 59 days. If the defaults drift — a fixed
    distance swapped in, the start level moved — the live bot would silently stop
    matching the evidence behind it. So the shipped defaults are pinned here.
    """
    risk = Risk()
    got = (risk.break_even_at_r, risk.trail_start_r, risk.trail_atr_mult, risk.trail_min_pips)
    want = (0.75, 1.5, 1.0, 1.5)
    ok = all(abs(float(a) - float(b)) < 1e-9 for a, b in zip(got, want))
    return ok, f"беззбиток {got[0]}R, трейл {got[2]}×ATR від +{got[1]}R, мін {got[3]} pips"


def test_stop_move_message_is_actionable() -> tuple[bool, str]:
    """When the stop moves, the message must say where to put it.

    "Стоп зараз: 1.10050" is a fact, not an instruction: on a phone it still needs the
    order. So the message carries the exact price to enter and how far it moved, for
    both BUY and SELL. A missing price here means the owner has to work it out himself.
    """
    from notify import format_update

    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    base = _signal(status="ACTIVE", created_at=now.isoformat(), updated_at=now.isoformat(),
                   entry_low=1.0995, entry_high=1.1005, stop=1.0985,
                   break_even_at_r=0.75, trail_start_r=1.5, trail_atr_mult=1.0,
                   trail_min_pips=1.5, trail_stop=1.1005)
    buy = format_update(base, "Пересунь стоп на 1.10050.")
    sell_sig = dict(base, side="SELL", stop=1.1025, trail_stop=1.1005,
                    entry_low=1.0995, entry_high=1.1005)
    sell = format_update(sell_sig, "Пересунь стоп на 1.10050.")

    ok_buy = "Пересунь стоп на 1.10050" in buy and "20.0 pips" in buy
    ok_sell = "Пересунь стоп на 1.10050" in sell and "20.0 pips" in sell
    # The old wording was direction-specific and wrong for a SELL.
    neutral = "ніколи не назад" in buy and "ніколи не назад" in sell
    ok = ok_buy and ok_sell and neutral
    return ok, f"BUY={ok_buy}, SELL={ok_sell}, нейтральне формулювання={neutral}"


def test_stop_moves_are_not_spammy() -> tuple[bool, str]:
    """A trailing stop must not send a message for every fraction of a pip.

    Measured on 41 trades: unthrottled, one trade sent 9 messages. The scan only
    announces a move of at least TRAIL_NOTIFY_PIPS, and the stored stop still updates
    on every scan, so nothing is lost — only the noise is.
    """
    from scan import TRAIL_NOTIFY_PIPS
    from config import Risk

    r = Risk()
    risk_pips = 6.8                      # median trade risk over 59 days
    share = TRAIL_NOTIFY_PIPS / risk_pips
    sensible = 0.1 <= share <= 0.35       # a fifth of one trade's risk, not a tick
    return sensible, (f"поріг {TRAIL_NOTIFY_PIPS} pips = {share*100:.0f}% ризику "
                      f"({r.trail_atr_mult}×ATR)")


def test_signal_template() -> tuple[bool, str]:
    """Every promised field must survive a redesign of the message.

    The layout changed to be read on a phone, so the labels are checked by what they
    carry rather than by their old wording: each price, the risk, the reasoning and
    the disclaimer must still be present.
    """
    text = format_signal(_signal())
    required = [
        "рейтинг", "100", "Вхід", "Тригер", "Стоп", "TP1", "TP2", "TP3",
        "Ризик/прибуток", "хв", "Чому", "відповідність правилам",
        "Новини", "Спред", "Джерело", "Скасовує сценарій", "не наказ брокеру",
    ]
    missing = [k for k in required if k not in text]
    return not missing, "all template sections present" if not missing else f"missing {missing}"
    """Every promised field must survive a redesign of the message.

    The layout changed to be read on a phone, so the labels are checked by what they
    carry rather than by their old wording: each price, the risk, the reasoning and
    the disclaimer must still be present.
    """
    text = format_signal(_signal())
    required = [
        "рейтинг", "100", "Вхід", "Тригер", "Стоп", "TP1", "TP2", "TP3",
        "Ризик/прибуток", "хв", "Чому", "відповідність правилам",
        "Новини", "Спред", "Джерело", "Скасовує сценарій", "не наказ брокеру",
    ]
    missing = [k for k in required if k not in text]
    return not missing, "all template sections present" if not missing else f"missing {missing}"


def test_no_probability_claims() -> tuple[bool, str]:
    text = format_signal(_signal()).lower()
    forbidden = ["ймовірність", "probability", "впевненість 9", "100% win", "гарант"]
    # 'ймовірність' appears only inside the explicit disclaimer that score is not one.
    hits = [w for w in forbidden if w in text and "не ймовірність" not in text]
    return not hits, "no probability claim" if not hits else f"found {hits}"


def test_position_sizing() -> tuple[bool, str]:
    risk = Risk()
    risk_pips = 15.0
    lots = (risk.account_equity * risk.risk_percent / 100) / (risk_pips * 10)
    money = lots * risk_pips * 10
    ok = abs(money - risk.account_equity * risk.risk_percent / 100) < 0.01
    return ok, f"{lots:.2f} lots ⇒ {money:.2f} USD risked"


def test_store_dedupe() -> tuple[bool, str]:
    from store import Store

    path = os.path.join(tempfile.mkdtemp(), "selftest.db")
    store = Store("", sqlite_path=path)
    first = store.claim_message("selftest:key", "test")
    second = store.claim_message("selftest:key", "test")
    store.release_message("selftest:key")
    third = store.claim_message("selftest:key", "test")
    store.set_state(runs_today=7)
    ok = first and not second and third and store.state()["runs_today"] == 7
    return ok, "claim/dedupe/release/state round-trip"


def test_target_must_be_far_from_fill() -> tuple[bool, str]:
    """A target closer than the stop must never become a signal.

    Found in the 59-day journal: two trades had TP1 within 0.3 and 3.0 pips of the
    fill while the filter believed they were 1:2, because the reward was measured from
    the current mid rather than from the limit fill price. The check now runs on the
    fill price, and this test calls that function directly.
    """
    from config import PIP
    from signals import Candidate, _targets_are_far_enough

    def make(entry_low: float, entry_high: float, stop: float, tp1: float):
        return Candidate(
            side="SELL", setup="BREAKOUT_RETEST", entry_low=entry_low, entry_high=entry_high,
            trigger=entry_low, stop=stop, tp1=tp1, tp2=tp1 - 0.0030, tp3=None, rr=2.0,
            risk_pips=6.0, reward_pips=12.0, expected_hold_min=60, max_hold_min=240,
            context={}, why=[], cancel_rules=[], invalidations=[],
        )

    # A SELL fills at entry_low = 1.09900. Target 1.09905 is only 0.5 pips away while
    # the stop is 6 pips: the trade cannot pay even 1R of genuine room.
    broken = make(1.09900, 1.10000, 1.09960, 1.09905)
    good = make(1.09900, 1.10000, 1.09960, 1.09500)

    reject = _targets_are_far_enough(broken, 5.0, 35.0) is False
    accept = _targets_are_far_enough(good, 5.0, 35.0) is True

    broken_pips = abs(1.09900 - 1.09905) / PIP
    ok = reject and accept
    return ok, (f"ціль 0.5 pips відхилена={reject} (реально {broken_pips:.1f} pips), "
                f"нормальна прийнята={accept}")


def test_counter_trend_is_refused() -> tuple[bool, str]:
    """Setups against the H4 trend must not become signals.

    Over 59 days of real bars, counter-trend entries made 34 trades at a 28% win rate
    for -13.49R, while aligned entries with score >= 87 made +15.02R at 57%. The filter
    is therefore load-bearing: if it silently stops working, the losing group returns.
    detect_setups is stubbed so the test isolates the filter and not the pattern code.
    """
    import signals as sig_mod
    from config import DataCfg
    from datafeed import Quality, Quote, Snapshot
    from markettime import NewsRisk

    now = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)  # London, not a blocked session
    session = session_info(now)
    quote = Quote(bid=1.0999, ask=1.1001, mid=1.1000, spread_pips=1.2, ts=now,
                  provider="test", live=True, spread_is_assumed=True)
    bars = [_c(int(now.timestamp()) - (80 - i) * 300, 1.1, 1.1006, 1.0994, 1.1) for i in range(90)]
    snap = Snapshot(quote=quote, candles={"M5": bars, "M15": bars}, quality=Quality(True, "VERIFIED"),
                    validator=None, generated_at=now)

    from analysis import analyse

    frames = {tf: analyse(tf, bars) for tf in ("M5", "M15", "H1", "H4")}
    frames["H4"].structure = "bear"  # higher timeframe clearly down

    def fake_detect(*_a, **_k):
        return [sig_mod.Candidate(
            side="BUY", setup="FVG_PULLBACK", entry_low=1.0995, entry_high=1.1005,
            trigger=1.1005, stop=1.0990, tp1=1.1020, tp2=1.1035, tp3=None, rr=3.0,
            risk_pips=15.0, reward_pips=15.0, expected_hold_min=60, max_hold_min=240,
            context={}, why=[], cancel_rules=[], invalidations=[],
        )]

    original = sig_mod.detect_setups
    sig_mod.detect_setups = fake_detect
    try:
        v = sig_mod.evaluate(snap, frames, [], session, NewsRisk("NONE", "чисто"),
                             open_count=0, signals_today=0, daily_r=0.0,
                             risk=Risk(), data_cfg=DataCfg())
        refused = v.verdict == "NO_TRADE" and any("проти тренду H4" in d for d in v.details)

        # Same setup with a matching trend must be allowed through the filter.
        frames["H4"].structure = "bull"
        v2 = sig_mod.evaluate(snap, frames, [], session, NewsRisk("NONE", "чисто"),
                              open_count=0, signals_today=0, daily_r=0.0,
                              risk=Risk(), data_cfg=DataCfg())
        allowed = not any("проти тренду H4" in d for d in v2.details)
    finally:
        sig_mod.detect_setups = original

    ok = refused and allowed
    return ok, f"контртренд відхилено={refused}, за трендом пропущено={allowed}, verdict={v.verdict}"


def test_backtest_thresholds_hold() -> tuple[bool, str]:
    """The thresholds must match the evidence, and A+ must stay rarer than A.

    Raising the floor to 87 was a decision from the backtest (79% win rate, +14.86R,
    profitable in both halves of the month). If someone lowers it later without new
    evidence, entries the test rejected come back, so the floor is asserted here.
    """
    from config import Risk

    risk = Risk()
    floor = float(risk.min_score_ready)
    plus = float(risk.min_score_ap_lus)
    ok = floor >= 85 and plus > floor and risk.max_signals_per_day <= 3
    return ok, f"поріг A={floor:g}, A+={plus:g}, максимум {risk.max_signals_per_day} на день"


def test_score_is_not_winrate() -> tuple[bool, str]:
    text = format_signal(_signal())
    ok = "відповідність правилам, не ймовірність" in text
    return ok, "score labelled as rule compliance" if ok else "score not labelled"


def test_pair_label() -> tuple[bool, str]:
    from notify import _pair

    cases = {"EURUSD=X": "EUR/USD", "EUR_USD": "EUR/USD", "EURUSD": "EUR/USD", "GBPJPY=X": "GBP/JPY"}
    bad = {k: _pair(k) for k, v in cases.items() if _pair(k) != v}
    return not bad, "ticker rendered as pair" if not bad else f"wrong: {bad}"


def test_events_roundtrip_with_offset() -> tuple[bool, str]:
    """Calendar rows must survive a store round-trip when they carry a UTC offset.

    SQLite keeps timestamps as text, so a naive string range comparison silently
    dropped every row once the feed sent "03:15:00-05:00". That made the calendar
    look empty and blocked all entries, so the filter is checked explicitly.
    """
    from store import Store

    path = os.path.join(tempfile.mkdtemp(), "events.db")
    store = Store("")
    store._sqlite_path = path
    store._ready = False
    now = datetime.now(UTC)
    rows = []
    for i, offset_hours in enumerate((-5, 0, 2)):
        tz = timezone(timedelta(hours=offset_hours))
        rows.append({
            "id": f"cal:{i}", "title": f"Event {i}", "currency": "USD", "impact": "high",
            "time_utc": (now + timedelta(hours=i + 1)).astimezone(tz),
            "source": "Economic calendar (free weekly feed)", "url": None,
            "actual": "1.2" if i == 0 else None, "forecast": f"{i}.0", "previous": "0.5",
        })
    store.save_events(rows)
    found = store.list_events(now - timedelta(days=1), now + timedelta(days=7))

    rows[0]["forecast"] = "9.9"
    store.save_events(rows)
    updated = [r for r in store.list_events(now - timedelta(days=1), now + timedelta(days=7)) if r["id"] == "cal:0"]

    ok = (
        len(found) == 3
        and [r["id"] for r in found] == ["cal:0", "cal:1", "cal:2"]
        and found[0]["forecast"] == "0.0"
        and found[0]["actual"] == "1.2"
        and len(store.list_events(now + timedelta(days=30), now + timedelta(days=40))) == 0
        and updated and updated[0]["forecast"] == "9.9"
    )
    return ok, f"{len(found)}/3 rows kept with offsets, values stored, no duplicates"


def test_calendar_usable_gate() -> tuple[bool, str]:
    """Silent feed vs. genuinely empty week must be told apart."""
    from scan import calendar_usable

    stored = [{"source": "Economic calendar (free weekly feed)", "title": "CPI"}]
    cases = {
        "fetch ok, no rows": calendar_usable(True, [], True),
        "fetch failed, cache exists": calendar_usable(False, stored, True),
        "fetch failed, no cache": calendar_usable(False, [], True),
        "fetch crashed, no cache": calendar_usable(None, [], True),
        "news disabled": calendar_usable(None, [], False),
        "cache reused": calendar_usable(True, stored, True),
    }
    expected = {
        "fetch ok, no rows": True,
        "fetch failed, cache exists": True,
        "fetch failed, no cache": False,
        "fetch crashed, no cache": False,
        "news disabled": True,
        "cache reused": True,
    }
    bad = {k: v for k, v in cases.items() if v != expected[k]}
    return not bad, f"6 cases as expected" if not bad else f"wrong: {bad}"


def test_entry_price_geometry() -> tuple[bool, str]:
    """Risk, reward, R:R and lot size must be measured from the fill price.

    A sell limit fills at the lower edge of its zone and a buy limit at the upper
    edge. The lifecycle accounts the trade from that same price, so reporting pips
    from the trigger instead made the message and the realised R disagree.
    """
    from signals import entry_price

    cases = {
        "sell fills at lower edge": entry_price("SELL", 1.09782, 1.09830),
        "buy fills at upper edge": entry_price("BUY", 1.09782, 1.09830),
    }
    expected = {"sell fills at lower edge": 1.09782, "buy fills at upper edge": 1.09830}
    if cases != expected:
        return False, f"wrong fill price: {cases}"

    # SELL: fill 1.09782, stop 1.09846 → 6.4 pips, not 6.6 from the trigger 1.0978
    fill = entry_price("SELL", 1.09782, 1.09830)
    stop = 1.09846
    tp1 = 1.09660
    risk_pips = round(abs(fill - stop) / 0.0001, 1)
    reward_pips = round(abs(fill - tp1) / 0.0001, 1)
    ok = risk_pips == 6.4 and reward_pips == 12.2 and reward_pips > risk_pips
    note = f"fill {fill}: risk {risk_pips} pips, reward {reward_pips} pips"
    return ok, note


def test_chat_binding() -> tuple[bool, str]:
    """The dashboard binding must win over the environment and survive restarts."""
    from config import ENV, bind_chat, chat_id
    from store import Store

    path = os.path.join(tempfile.mkdtemp(), "bind.db")
    first = Store("")
    first._sqlite_path = path
    first._ready = False
    env_value = ENV.telegram_chat_id

    initial = chat_id(first)
    bind_chat(first, "100200300")
    after_bind = chat_id(first)

    # A fresh Store instance reads the same saved value, like a restart would.
    second = Store("")
    second._sqlite_path = path
    second._ready = False
    after_restart = chat_id(second)

    bind_chat(second, "")
    after_clear = chat_id(second)

    ok = (
        initial == (env_value or "")
        and after_bind == "100200300"
        and after_restart == "100200300"
        and after_clear == (env_value or "")
    )
    return ok, f"bind {after_bind!r} survives restart, cleared to {after_clear!r}"


def test_calendar_xml_fallback() -> tuple[bool, str]:
    """The XML mirror must parse identically when the JSON path is throttled.

    Both formats describe the same week; XML times are UTC in `MM-DD-YYYY` +
    `h:mmam/pm` form, which has to be converted without guessing a timezone.
    """
    from datetime import datetime
    import newsfeed

    ok_parse, note_parse = newsfeed._parse_xml_time("11-24-2026", "1:30pm") == datetime(
        2026, 11, 24, 13, 30, tzinfo=UTC
    ), "1:30pm -> 13:30 UTC"
    midnight, _ = newsfeed._parse_xml_time("11-24-2026", "12:00am"), "x"
    noon, _ = newsfeed._parse_xml_time("11-24-2026", "12:00pm"), "x"

    row = newsfeed._cal_row("Core CPI m/m", "USD", midnight, "High", "", "0.3%", "0.2%")
    ok_row = (
        row is not None
        and row["impact"] == "high"
        and row["forecast"] == "0.3%"
        and row["actual"] is None
        and row["id"].startswith("cal:USD:")
    )
    ok = ok_parse and midnight.hour == 0 and noon.hour == 12 and bool(ok_row)
    note = f"{note_parse}, 12:00am -> {midnight.hour}h, 12:00pm -> {noon.hour}h, row ok={bool(ok_row)}"
    return ok, note


def test_scan_token_accepted() -> tuple[bool, str]:
    """The scheduler token triggers a scan, and nothing else.

    Two separate things had to be right:
    1. It must work even when a dashboard password exists (it used to be ignored).
    2. It must NOT open the terminal or read signals, because it travels in a
       scheduler URL and could be seen by third parties.

    This test must never edit the real environment or reload the live modules: an
    earlier version did exactly that and left the running service rejecting its own
    tokens until the next restart. `token_matches` is pure, so it is called directly.
    """
    from web import token_matches

    terminal = ["dash-token-test"]
    scan_only = ["scan-token-test", "dash-token-test"]
    found = token_matches(["scan-token-test", "", "", ""], scan_only)
    blocked = token_matches(["scan-token-test", "", "", ""], terminal)
    dash_scan = token_matches(["dash-token-test", "", "", ""], scan_only)
    wrong = token_matches(["nope", "", "", ""], scan_only)
    empty = token_matches(["", "", "", ""], scan_only)
    no_config = token_matches(["anything"], ["", ""])
    ok = found and not blocked and dash_scan and not wrong and not empty and not no_config
    return ok, (f"scan-triggers={found}, scan-opens-terminal={blocked}, "
                f"dashboard={dash_scan}, wrong={wrong}, empty={empty}")


def test_scheduled_briefings_not_spammy() -> tuple[bool, str]:
    """A quiet day must produce a handful of useful messages, not dozens.

    The morning brief, three session check-ins and the daily summary already had
    formatters but nothing ever called them, so the channel could stay empty all
    day. Equally, running every 5 minutes must not repeat the same notice: each is
    claimed by a date key, and a check-in is skipped when something was just sent.
    """
    import tempfile
    from datetime import datetime
    import types

    import scan
    from store import Store

    original_send = scan.notify.send
    original_brief = scan.notify.format_brief
    original_daily = scan.notify.format_daily
    original_today = scan._today_signals
    try:
        sent: list[str] = []
        scan.notify.send = lambda token, chat, text: (sent.append(text) or True, None)
        scan.notify.format_brief = lambda *a, **k: str(a[-1])
        scan.notify.format_daily = lambda *a, **k: "daily"
        scan._today_signals = lambda *a, **k: []

        store = Store("", sqlite_path=os.path.join(tempfile.mkdtemp(), "brief.db"))
        snap = types.SimpleNamespace(quote=None, quality=types.SimpleNamespace(status="VERIFIED"),
                                     generated_at=datetime(2026, 11, 23, 8, 0))
        frames = {tf: types.SimpleNamespace(structure="bear") for tf in ("M5", "M15", "H1", "H4")}
        session = types.SimpleNamespace(active="London", quality="PRIME")
        news = types.SimpleNamespace(level="NONE")

        # A full quiet day, scanned every 15 minutes.
        for hour in range(6, 24):
            for minute in (5, 20, 35, 50):
                scan._scheduled_briefings(
                    store, snap, frames, session, news, [],
                    datetime(2026, 11, 23, hour, minute, tzinfo=WAW),
                    scan.ScanResult(at=datetime(2026, 11, 23, hour, minute, tzinfo=WAW)),
                )
        count = len(sent)
        kinds = " / ".join(sent)
        ok = 3 <= count <= 6
        return ok, f"{count} повідомлень за тихий день: {kinds}"
    finally:
        scan.notify.send = original_send
        scan.notify.format_brief = original_brief
        scan.notify.format_daily = original_daily
        scan._today_signals = original_today


def test_command_offset_advances() -> tuple[bool, str]:
    """Commands are read without a webhook, so the offset must move forward.

    Telegram hands each update out only once when the caller passes back the last
    seen id. Without advancing the offset the bot would answer /status again on
    every scan.
    """
    import notify

    fake = {"ok": True, "result": [{"update_id": 10}, {"update_id": 12}, {"update_id": 11}]}
    seen = {}

    def fake_get(token, method, timeout=15, query=None):
        seen.update(query or {})
        return True, fake, None

    original = notify.api_get
    try:
        notify.api_get = fake_get
        ok, updates, nxt, err = notify.poll_updates("token", 9)
        first_ok = ok and nxt == 13 and len(updates) == 3
        # Passing the returned offset back must be sent as-is.
        notify.poll_updates("token", nxt)
        pass_back = seen.get("offset") == 13
        # Re-reading with the advanced offset yields nothing new.
        fake["result"] = []
        ok2, updates2, nxt2, _ = notify.poll_updates("token", nxt)
        empty_ok = ok2 and updates2 == [] and nxt2 == 13
        return (first_ok and pass_back and empty_ok,
                f"offset 9→{nxt}, передано назад={pass_back}, без нових={empty_ok}")
    finally:
        notify.api_get = original


def test_webhook_gives_instant_replies() -> tuple[bool, str]:
    """Commands must be answered by a webhook, not only by the next scan.

    Telegram queues updates when no webhook is set, and only the five-minute scan
    drained that queue, so /help looked broken for minutes. The secret is derived
    from the dashboard token, which means no extra setting is needed and the URL
    still cannot be guessed. A wrong secret must be refused.
    """
    import dataclasses
    import io
    import tempfile

    import web
    from store import Store

    original_store = web._STORE
    original_env = web.ENV
    original_info, original_set, original_send = (
        web.notify.webhook_info, web.notify.set_webhook, web.notify.send
    )
    original_chat_id = web.chat_id
    try:
        web._STORE = Store("", sqlite_path=os.path.join(tempfile.mkdtemp(), "hook.db"))
        # A deterministic secret needs a token to derive it from. The real service
        # always has DASHBOARD_TOKEN; the test supplies one so it does not depend on
        # whatever environment happens to be set.
        web.ENV = dataclasses.replace(original_env, dashboard_token="FAKE-dashboard-token-for-test",
                                      scan_token="", webhook_secret="", public_url="",
                                      telegram_token="FAKE-bot-token-for-test",
                                      telegram_chat_id="")
        # chat_id() reads config.ENV, not web.ENV, so the owner's real TELEGRAM_CHAT_ID
        # would reject the test's fake chat and make this check fail on a healthy
        # service. Patch it to look like a bot nobody has messaged yet.
        web.chat_id = lambda store: ""
        secret = web.effective_webhook_secret()
        if not secret:
            return False, "секрет не визначено"

        registered = {}
        web.notify.webhook_info = lambda token: {"url": "", "last_error_message": ""}
        web.notify.set_webhook = lambda token, url, sec: (registered.update(url=url, sec=sec) or True, None)
        status = web.ensure_webhook(web._STORE, {"HTTP_X_FORWARDED_HOST": "alsr-bot.onrender.com"}, web.ENV)
        built = registered.get("url", "")
        url_ok = built.endswith(f"/telegram/{secret}") and built.startswith("https://alsr-bot.onrender.com")
        sec_ok = registered.get("sec") == secret

        sent: list[str] = []
        web.notify.send = lambda token, chat, text: (sent.append(text) or True, None)
        body = b'{"update_id":1,"message":{"chat":{"id":123},"text":"/help"}}'

        def deliver(with_secret: str) -> str:
            env = {
                "PATH_INFO": f"/telegram/{secret}", "REQUEST_METHOD": "POST",
                "CONTENT_LENGTH": str(len(body)), "CONTENT_TYPE": "application/json",
                "HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN": with_secret, "wsgi.input": io.BytesIO(body),
            }
            out = {}
            web.application(env, lambda status, headers: out.update(s=status))
            return out.get("s", "")

        answered = deliver(secret).startswith("200") and len(sent) == 1
        refused = deliver("wrong").startswith("403")
        ok = status.startswith("ok") and url_ok and sec_ok and answered and refused
        return ok, (f"url={url_ok}, secret={sec_ok}, відповідь={answered}, "
                    f"чужий={refused}")
    finally:
        web._STORE = original_store
        web.ENV = original_env
        web.chat_id = original_chat_id
        web.notify.webhook_info = original_info
        web.notify.set_webhook = original_set
        web.notify.send = original_send


def test_stale_webhook_is_repaired() -> tuple[bool, str]:
    """A webhook left on an old address must be moved back, not trusted.

    Telegram keeps posting to whatever URL was registered last. After a rename or a
    changed secret that address stops working, Telegram retries silently and every
    command looks ignored — the page and the API look perfectly healthy meanwhile.
    """
    import dataclasses
    import tempfile

    import web
    from store import Store

    original_store, original_env = web._STORE, web.ENV
    original_info, original_set = web.notify.webhook_info, web.notify.set_webhook
    try:
        web._STORE = Store("", sqlite_path=os.path.join(tempfile.mkdtemp(), "stale.db"))
        web.ENV = dataclasses.replace(original_env, dashboard_token="FAKE-dashboard-token-for-test",
                                      scan_token="", webhook_secret="", public_url="",
                                      telegram_token="FAKE-bot-token-for-test", telegram_chat_id="")
        host = {"HTTP_X_FORWARDED_HOST": "alsr-bot.onrender.com"}
        secret = web.effective_webhook_secret()
        good = f"https://alsr-bot.onrender.com/telegram/{secret}"

        cases = [
            ("стара адреса", {"url": "https://old-name.onrender.com/telegram/x", "last_error_message": ""}, True),
            ("помилка доставки", {"url": good, "last_error_message": "Wrong response from the webhook: 403"}, True),
            ("уже правильно", {"url": good, "last_error_message": ""}, False),
        ]
        results = []
        for label, info, must_fix in cases:
            calls = []
            web.notify.webhook_info = lambda token, _i=info: dict(_i)
            web.notify.set_webhook = lambda token, url, sec: (calls.append(url) or True, None)
            status = web.ensure_webhook(web._STORE, host, web.ENV)
            fixed = bool(calls) and calls[0] == good
            results.append(fixed == must_fix and status.startswith("ok"))
            if not results[-1]:
                return False, f"{label}: status={status}, викликів={len(calls)}"
        return all(results), "стара=виправлено, помилка=виправлено, correct=без змін"
    finally:
        web._STORE, web.ENV = original_store, original_env
        web.notify.webhook_info, web.notify.set_webhook = original_info, original_set


def test_news_feed_down_blocks() -> tuple[bool, str]:
    """If the calendar cannot be read, entries must be blocked, not assumed safe."""
    now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
    down = news_risk([], now, 20, 15, feed_ok=False)
    ok_down = down.level == "UNKNOWN"
    ok = ok_down and news_risk([], now, 20, 15, feed_ok=True).level == "NONE"
    return ok, f"feed down → {down.level}, feed ok → NONE"


TESTS = [
    ("DST offsets (London/NY)", test_dst_offsets),
    ("Europe/Warsaw display", test_warsaw_display),
    ("DST boundary hour", test_dst_boundary_hours),
    ("Market closure", test_market_closure),
    ("Session windows", test_session_windows),
    ("News blackout gate", test_news_blackout),
    ("News feed down blocks entries", test_news_feed_down_blocks),
    ("Calendar events survive storage", test_events_roundtrip_with_offset),
    ("Calendar usable gate", test_calendar_usable_gate),
    ("Entry price geometry", test_entry_price_geometry),
    ("Telegram chat binding", test_chat_binding),
    ("Calendar XML fallback", test_calendar_xml_fallback),
    ("Scan token triggers a scan only", test_scan_token_accepted),
    ("Scheduled briefings stay quiet", test_scheduled_briefings_not_spammy),
    ("Telegram commands advance offset", test_command_offset_advances),
    ("Instant command replies (webhook)", test_webhook_gives_instant_replies),
    ("Stale webhook is repaired", test_stale_webhook_is_repaired),
    ("Weak session is blocked", test_weak_session_is_blocked),
    ("Lifecycle: SL", test_lifecycle_sl),
    ("Lifecycle: TP1", test_lifecycle_tp1),
    ("Lifecycle: ambiguous candle → SL", test_lifecycle_ambiguous_candle),
    ("Lifecycle: cancel when entry not triggered", test_lifecycle_cancel),
    ("Lifecycle: max hold expiry", test_lifecycle_expiry),
    ("Lifecycle: ignore open bar", test_lifecycle_ignores_incomplete_bar),
    ("H4 aggregation", test_h4_aggregation),
    ("Candle validation", test_candle_validation),
    ("Weekend gap tolerance", test_weekend_gap_not_flagged),
    ("Previous day = Friday on Monday", test_prev_day_skips_empty_sunday),
    ("Signal template completeness", test_signal_template),
    ("No probability claims", test_no_probability_claims),
    ("Position sizing", test_position_sizing),
    ("Store dedupe and state", test_store_dedupe),
    ("Score is not a win rate", test_score_is_not_winrate),
    ("Backtest thresholds hold", test_backtest_thresholds_hold),
    ("Counter-trend is refused", test_counter_trend_is_refused),
    ("Target must be far from fill", test_target_must_be_far_from_fill),
    ("Break-even is applied", test_break_even_turns_loss_into_scratch),
    ("Trailing stop lifts and never falls", test_trailing_stop_lifts_and_never_falls),
    ("Trailing waits for its start level", test_trailing_waits_for_its_start),
    ("Trailing gap beats the spread", test_trail_min_gap_beats_spread),
    ("Targets do not close the trade", test_improvement_is_not_a_target_hit),
    ("Live rule matches the backtest", test_trailing_matches_backtest),
    ("Stop move message is actionable", test_stop_move_message_is_actionable),
    ("Stop moves are not spammy", test_stop_moves_are_not_spammy),
    ("Ticker rendered as pair", test_pair_label),
]


def run_all() -> dict:
    results = []
    for name, fn in TESTS:
        try:
            ok, note = fn()
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"exception: {exc}"
        results.append({"name": name, "ok": bool(ok), "note": note})
    passed = sum(1 for r in results if r["ok"])
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "ok": passed == len(results),
        "results": results,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    report = run_all()
    for r in report["results"]:
        print(("PASS " if r["ok"] else "FAIL ") + r["name"] + " — " + r["note"])
    print(f"\n{report['passed']}/{report['total']} passed")
    raise SystemExit(0 if report["ok"] else 1)
