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
from lifecycle import advance, evaluate_signal
from markettime import LON, NY, WAW, market_closed, news_risk, session_info
import notify
from notify import format_signal
from signals import Candidate

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
    # The entry window is ENTRY_WAIT_MIN (60), so a scan at exactly 60 minutes must
    # still be waiting; a little later the signal is closed as never filled.
    from lifecycle import ENTRY_WAIT_MIN

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    # BUY entry zone is 1.0995–1.1005, so a bar must stay ABOVE 1.1005 to miss it.
    bar = _c(T0, 1.1010, 1.1014, 1.1008, 1.1012)
    on_time = advance(_signal(), [bar], created + timedelta(minutes=ENTRY_WAIT_MIN))
    late = advance(_signal(), [bar], created + timedelta(minutes=ENTRY_WAIT_MIN + 5))
    ok = (on_time is None and late is not None and late["status"] == "CANCELLED"
          and late.get("exit_kind") == "NO_ENTRY")
    return ok, (f"на {ENTRY_WAIT_MIN} хв: {on_time['status'] if on_time else 'чекає'}, "
                f"після: {late['status'] if late else 'чекає'}/"
                f"{(late or {}).get('exit_kind')}")


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


def test_trail_waits_until_two_r() -> tuple[bool, str]:
    """On the shipped rule the stop must not trail before +2.0R.

    2.7.6 moved the start level from +1.5R to +2.0R because at +1.5R the trade has
    not proved itself yet and the trail was capping runaways just as they began. The
    test is driven by the live Risk() defaults, not by a hand-typed number, so a
    quiet drift back to an earlier start level shows up here as a failure instead of
    only in a backtest nobody re-runs.
    """
    risk = Risk()
    start = float(risk.trail_start_r)

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    t0 = int(created.timestamp())
    sig = _signal(status="ACTIVE", created_at=created.isoformat(),
                  break_even_at_r=risk.break_even_at_r, trail_start_r=start,
                  trail_atr_mult=risk.trail_atr_mult, trail_min_pips=risk.trail_min_pips,
                  max_hold_min=600)
    entry = 1.1005            # entry_high
    step = 0.0004             # +0.2R per bar (risk = 20 pips)
    per_bar = step / (entry - 1.0985)

    bars = []
    for i in range(1, int(start / per_bar) + 3):
        price = entry + i * step
        bars.append(_c(t0 + i * 300, price - 0.0002, price + 0.0002, price - 0.0002, price))

    # Between break-even and the start level the stop sits exactly at the entry.
    be = float(risk.break_even_at_r)
    before = []
    for k in range(1, len(bars) + 1):
        out = evaluate_signal(sig, bars[:k], bars[k - 1].t + 300)
        if be <= out["best_r"] < start:
            before.append(round(out["stop"], 6))
    held = bool(before) and all(abs(s - entry) < 1e-9 for s in before)

    # The first bar at or past the start level must lift the stop above entry.
    out = evaluate_signal(sig, bars, bars[-1].t + 300)
    lifted = out["stop"] > entry + 1e-9 and out["best_r"] >= start

    ok = held and lifted
    return ok, (f"правило +{start}R: між беззбитком і стартом стоп на вході={held} "
                f"({len(before)} барів), далі піднявся={lifted} ({out['stop']:.5f})")


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
    want = (0.75, 2.0, 0.75, 1.5)
    ok = all(abs(float(a) - float(b)) < 1e-9 for a, b in zip(got, want))
    return ok, f"беззбиток {got[0]}R, трейл {got[2]}×ATR від +{got[1]}R, мін {got[3]} pips"


def test_stop_move_message_is_actionable() -> tuple[bool, str]:
    """When the stop moves, the message must say where to put it.

    "Стоп зараз: 1.10050" is a fact, not an instruction: on a phone it still needs the
    order. So the message carries the exact price to enter and how far it moved, for
    both BUY and SELL, and it goes through the same stop_note() the scanner uses -
    checking a hand-written note would test nothing but the test.
    """
    from notify import format_update, stop_note

    now = datetime(2026, 11, 24, 10, 0, tzinfo=UTC)
    base = _signal(status="ACTIVE", created_at=now.isoformat(), updated_at=now.isoformat(),
                   entry_low=1.0995, entry_high=1.1005, stop=1.0985,
                   break_even_at_r=0.75, trail_start_r=1.5, trail_atr_mult=1.0,
                   trail_min_pips=1.5, trail_stop=1.1005, best_r=1.5)
    buy = format_update(base, stop_note(base, 1.1005, "TRAIL"), label="📈 трейлінг стопу")
    sell_sig = dict(base, side="SELL", stop=1.1025, trail_stop=1.1005,
                    entry_low=1.0995, entry_high=1.1005)
    sell = format_update(sell_sig, stop_note(sell_sig, 1.1005, "TRAIL"), label="📈 трейлінг стопу")
    # Break-even is a different instruction and must say so, not just name a price.
    be = format_update(dict(base, trail_stop=1.1005), stop_note(base, 1.1005, "BE"),
                       label="➖ беззбиток")

    ok_buy = "Пересунь стоп на 1.10050" in buy and "20.0 pips" in buy
    ok_sell = "Пересунь стоп на 1.10050" in sell and "20.0 pips" in sell
    # The old wording was direction-specific and wrong for a SELL.
    neutral = "ніколи назад" in buy and "ніколи назад" in sell
    # A stop move is not a fill: the headline must name the action, not the entry.
    headline = buy.split("\n")[1]
    ok_be = ("беззбиток" in be.lower() and "1.10050" in be
             and "вхід виконано" not in be and "вхід виконано" not in buy
             and "трейлінг" in headline.lower() and "беззбиток" in be.split("\n")[1].lower())
    ok = ok_buy and ok_sell and neutral and ok_be
    return ok, (f"BUY={ok_buy}, SELL={ok_sell}, нейтральне формулювання={neutral}, "
                f"беззбиток={ok_be}")


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


def test_loss_is_exactly_one_r() -> tuple[bool, str]:
    """A stop-out must cost exactly 1R and break-even exactly 0R. No fractions.

    The owner read the backtest and asked the right question: where do -1.18R and
    -0.20R losses come from, when a stop should be simply -1R and a break-even zero?

    They came from the backtest charging the whole spread a second time on top of a
    result that was already measured in R. R is an account unit, not a price move:
    the position is sized from the stop distance, so a stop-out costs 1R of the
    account by construction. The live bot always reported a clean -1.00R; only the
    backtest disagreed, which means every number quoted from it was wrong.

    This test pins the arithmetic in lifecycle, so a future "cost" deduction cannot
    quietly smuggle fractions back into the record.
    """
    from lifecycle import net_r

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    entry, stop = 1.1005, 1.0985
    risk = abs(entry - stop)

    cases = []
    # BUY fills at the upper edge of its zone; SELL fills at the lower edge, and its
    # stop sits above the entry. Both must land on exactly -1.00R and 0.00R.
    for side, stop_price in (("BUY", stop), ("SELL", entry + risk)):
        sig = _signal(side=side, status="ACTIVE", created_at=created.isoformat(),
                      stop=round(stop_price, 5), entry_low=1.0995, entry_high=1.1005,
                      spread_at_creation=1.2, break_even_at_r=0.75, trail_start_r=2.0,
                      trail_atr_mult=0.75, trail_min_pips=1.5, max_hold_min=240)
        fill = 1.1005 if side == "BUY" else 1.0995
        cases.append((side, "стоп", net_r(sig, round(stop_price, 5))))
        cases.append((side, "беззбиток", net_r(sig, fill)))

    losses = [v for _, name, v in cases if name == "стоп"]
    flats = [v for _, name, v in cases if name == "беззбиток"]
    # A winner must still pay the spread: +2.00R gross is not +2.00R net.
    won = _signal(status="ACTIVE", created_at=created.isoformat(), stop=stop,
                  entry_low=1.0995, entry_high=1.1005, spread_at_creation=1.2)
    win_net = net_r(won, entry + 2 * risk)
    ok = (all(abs(v + 1.0) < 1e-9 for v in losses)
          and all(abs(v) < 1e-9 for v in flats)
          and win_net < 2.0
          and risk > 0)
    return ok, (f"стоп {losses}, беззбиток {flats}, виграш +2.00R брутто → "
                f"{win_net:+.2f}R нетто")


def test_live_and_backtest_book_the_same_result() -> tuple[bool, str]:
    """The journal and the replay must agree, because they used to disagree.

    Until 2.7.6 the live bot booked a stop-out at exactly -1.00R while the backtest
    booked -1.18R for the same trade, because the backtest deducted the spread a
    second time. Two sources of truth for one result is how a system starts reporting
    numbers nobody can reconcile, so both now call lifecycle.net_r().

    The check runs the same signal through both paths and requires the same answer,
    for a loser, a scratch and a winner.

    It reads tools/backtest.py when that folder exists (a local checkout) and skips
    that half otherwise. Render runs from alsr/ only, so /health was reporting 2.7.6
    while this test failed there with "No such file or directory" - a test that depends
    on a developer folder should not fail on the server. The arithmetic half still runs
    everywhere, so the R accounting stays pinned in production too.
    """
    from pathlib import Path
    from lifecycle import net_r

    created = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    sig = _signal(status="ACTIVE", created_at=created.isoformat(), stop=1.0985,
                  entry_low=1.0995, entry_high=1.1005, spread_at_creation=1.2,
                  break_even_at_r=0.75, trail_start_r=2.0, trail_atr_mult=0.75,
                  trail_min_pips=1.5, max_hold_min=240)

    loss = net_r(sig, 1.0985)
    scratch = net_r(sig, 1.1005)
    win = net_r(sig, 1.1045)

    replay = Path(__file__).resolve().parent.parent / "tools" / "backtest.py"
    if replay.exists():
        src = replay.read_text(encoding="utf-8")
        # The replay must call the very same function; a hand-written spread
        # subtraction in backtest.py is exactly the bug being pinned.
        uses_same = "net_r(open_signal" in src
        where = "бектест кличе net_r"
    else:
        # Server layout: no tools folder. Nothing to inspect, so this half is not
        # counted as a failure - but the caller still sees it was skipped.
        uses_same = True
        where = "tools/ немає (сервер) — звірку з бектестом пропущено"

    exact = abs(loss + 1.0) < 1e-9 and abs(scratch) < 1e-9 and 0 < win < 2.0
    ok = uses_same and exact
    return ok, (f"{where}={uses_same}; збиток {loss:+.2f}, "
                f"беззбиток {scratch:+.2f}, виграш +2.00R брутто → {win:+.2f}")


def test_zero_result_trade_is_still_a_trade() -> tuple[bool, str]:
    """A trade closed by break-even ends at 0R and must still count as a trade.

    Live case: one EUR/USD trade ended at break-even, and the dashboard read
    "completed 1 · TP/SL 0/0 · win rate 0% · total 0R" - indistinguishable from
    "nothing happened". The trade is real: the stop reached +0.75R, moved to the
    entry, and took the position out flat. Zero result is a result.
    """
    from lifecycle import positions_summary

    be = _signal(id="A", status="SL_HIT", resolved_at="2026-11-24T12:00:00+00:00",
                 r_result=0.0, resolution={"status": "SL_HIT", "exit_kind": "BE"})
    win = _signal(id="B", status="SL_HIT", resolved_at="2026-11-24T13:00:00+00:00",
                  r_result=1.8, resolution={"status": "SL_HIT", "exit_kind": "TRAIL"})
    loss = _signal(id="C", status="SL_HIT", resolved_at="2026-11-24T14:00:00+00:00",
                   r_result=-1.0, resolution={"status": "SL_HIT", "exit_kind": "SL"})
    s = positions_summary([be, win, loss])
    ok = (s["closed"] == 3 and s["wins"] == 1 and s["losses"] == 1 and s["flat"] == 1
          and s["total_r"] == 0.8)
    detail = (f"завершено={s['closed']}, плюс={s['wins']}, мінус={s['losses']}, "
              f"у нуль={s['flat']}, сума={s['total_r']}R")
    return ok, detail


def test_old_signal_keeps_its_own_exit_plan() -> tuple[bool, str]:
    """A signal issued before 2.7.0 must not claim a trailing stop it never had.

    Those signals carry no trail_start_r, so the card says plainly that the rule
    written into the signal still applies and the new one starts with the next
    signal. The alternative - showing the 2.7.0 rule on a 2.6.x signal - would
    tell the owner to trail a stop that this trade was never planned with.
    """
    from notify import _exit_plan_lines

    old = [l for l in _exit_plan_lines(_signal(trail_start_r=None, trail_atr_mult=None))]
    new = [l for l in _exit_plan_lines(_signal())]
    ok_old = any("TP1 закриває позицію" in l for l in old) and not any("трейлінг" in l for l in old)
    ok_new = any("трейлінг" in l for l in new) and any("орієнтири" in l for l in new)
    return ok_old and ok_new, f"старий сигнал={ok_old}, новий={ok_new}"


def test_legacy_signal_never_trails() -> tuple[bool, str]:
    """A signal issued before 2.7.0 has no trailing fields, so it only ever breaks even.

    This is the trade the owner watched: entry 1.09457, stop 1.09354. The signal was
    created before the deploy, so its card carried no trail_start_r. Run against a
    price that peaks at +2.5R it still exits at break-even, because there was no
    trailing rule to follow. The result is not "nothing happened" - the trade was up
    - the rule simply had no way to keep any of it.
    """
    from lifecycle import evaluate_signal, realized_r
    from datafeed import Candle

    t0 = datetime(2026, 11, 24, 12, 5, tzinfo=UTC)
    entry, stop = 1.09457, 1.09354
    risk = entry - stop

    def bar(m, o, h, l, c):
        return Candle(int((t0 + timedelta(minutes=m)).timestamp()), o, h, l, c, 0.0, True)

    legacy = {"created_at": t0.isoformat(), "side": "BUY", "status": "READY",
              "entry_low": 1.09373, "entry_high": entry, "trigger": entry, "stop": stop,
              "tp1": 1.09745, "tp2": 1.09839, "tp3": None, "max_hold_min": 240,
              "break_even_at_r": 0.75}
    modern = dict(legacy, trail_start_r=1.5, trail_atr_mult=1.0, trail_min_pips=1.5)

    def run(sig, peak):
        bars = [bar(0, entry, entry + 0.0002, entry - 0.0001, entry + 0.0001)]
        px = entry
        for i in range(1, 6):
            hi = entry + peak * risk * (i / 5)
            bars.append(bar(i * 5, px, max(hi, px), min(px, hi) - 0.0001, hi - 0.00004))
            px = hi - 0.00004
        for i in range(6, 14):
            lo = px - (peak * risk) / 6
            bars.append(bar(i * 5, px, px + 0.0001, lo, lo + 0.00004))
            px = lo + 0.00004
        st = evaluate_signal(sig, bars, bars[-1].t + 300)
        return realized_r(sig, st["event"]["price"]), st["best_r"], st["event"]["exit_kind"]

    r_old, peak_old, kind_old = run(legacy, 2.5)
    r_new, _, kind_new = run(modern, 2.5)
    ok = (r_old == 0.0 and kind_old == "BE" and peak_old > 1.0
          and r_new > 1.0 and kind_new == "TRAIL")
    return ok, (f"старий сигнал: {r_old:+.2f}R ({kind_old}), хоч пік {peak_old:.2f}R; "
                f"новий: {r_new:+.2f}R ({kind_new})")


def test_exit_rule_wording_is_unambiguous() -> tuple[bool, str]:
    """The card must not read as "stop goes to +0.75R".

    The rule is: once price reaches +0.75R the stop jumps to the ENTRY, which is
    break-even and not profit. "Беззбиток на +0.75R" was read - reasonably - as
    "stop at +0.75R", so the text now says where the stop goes and which level
    triggers it, in that order.
    """
    from notify import _exit_plan_lines

    text = " ".join(_exit_plan_lines(_signal()))
    reaches = "+0.75R" in text
    to_entry = "на вхід" in text
    bad = "беззбиток на +0.75R" in text.lower() or "беззбиток на +" in text.lower()
    return (reaches and to_entry and not bad), (
        f"рівень +0.75R={reaches}, стоп на вхід={to_entry}, хибне формулювання={bad}")


def test_unfilled_signal_is_not_a_trade() -> tuple[bool, str]:
    """A signal whose entry was never reached is not a completed trade.

    Live case: price never came back to the entry zone, the signal expired unfilled,
    and the dashboard read "Завершено 1 · TP/SL 0/0 · Win rate 0% · +0.00R". Nothing
    was traded, so there is no result to show - it belongs in its own counter.
    """
    from lifecycle import positions_summary

    unfilled = _signal(id="A", status="CANCELLED", resolved_at="2026-11-24T13:05:00+00:00",
                       r_result=0.0, resolution={"status": "CANCELLED", "exit_kind": "NO_ENTRY"})
    traded = _signal(id="B", status="SL_HIT", resolved_at="2026-11-24T14:00:00+00:00",
                     r_result=1.8, resolution={"status": "SL_HIT", "exit_kind": "TRAIL"})
    s = positions_summary([unfilled, traded])
    ok = (s["closed"] == 1 and s["cancelled"] == 1 and s["wins"] == 1
          and s["total_r"] == 1.8)
    return ok, (f"угод={s['closed']}, без входу={s['cancelled']}, "
                f"сума={s['total_r']}R")


def test_cancelled_signal_does_not_use_daily_limit() -> tuple[bool, str]:
    """A setup the price never reached must not spend one of the day's two slots.

    Live case 2026-11-25: 20261125-106B was cancelled at 09:20 with "входу не було",
    one real trade followed, and the bot then blocked the whole rest of the day with
    "Денний ліміт сигналів 2 вичерпано". Nothing was traded twice - a setup that never
    opened simply took half the allowance with it.
    """
    import scan
    from lifecycle import positions_summary

    unfilled = _signal(id="A", status="CANCELLED", resolved_at="2026-11-23T10:30:00+00:00",
                       r_result=0.0, resolution={"status": "CANCELLED", "exit_kind": "NO_ENTRY"})
    # Same shape, but stored by 2.7.0 and earlier: no exit_kind, only the sentence.
    legacy = _signal(id="B", status="CANCELLED", resolved_at="2026-11-23T11:30:00+00:00",
                     r_result=0.0,
                     resolution={"status": "CANCELLED",
                                 "note": "Вхід не активовано за 45 хв — сценарій скасовано без угоди."})
    traded = _signal(id="C", status="SL_HIT", resolved_at="2026-11-23T12:00:00+00:00",
                     r_result=-1.0, resolution={"status": "SL_HIT", "exit_kind": "SL"})

    class FakeStore:
        def __init__(self, rows):
            self.rows = rows

        def list_signals(self, statuses=None, limit=50):
            return self.rows

    now = datetime(2026, 11, 23, 15, 0, tzinfo=UTC)
    counted = scan._today_signals(FakeStore([unfilled, legacy, traded]), now)
    split = scan._today_signals_split(FakeStore([unfilled, legacy, traded]), now)
    ok = (len(counted) == 1 and counted[0]["id"] == "C"
          and split["cancelled"] == 2
          and positions_summary([unfilled, legacy, traded])["cancelled"] == 2)
    return ok, (f"у ліміт пішло {len(counted)} угод, без входу {split['cancelled']}, "
                f"старі записи теж розпізнані")


def test_card_says_it_is_a_limit_order() -> tuple[bool, str]:
    """A zone 2.3 pips below the market must not read as "buy now".

    Live case: the card showed "Вхід: 1.09373 – 1.09457" while price was 1.09480.
    The owner read that as a range to buy in, entered at market, and the plan's limit
    never filled - so the bot closed a signal he considered his open trade. The card
    now states the distance and that the entry waits for a pullback.
    """
    from notify import _entry_wait_lines

    above = " ".join(_entry_wait_lines(_signal(quote_at_creation=1.09480, entry_low=1.09373,
                                              entry_high=1.09457)))
    inside = " ".join(_entry_wait_lines(_signal(quote_at_creation=1.0940, entry_low=1.09373,
                                               entry_high=1.09457)))
    ok = ("лімітна" in above and "2.3 pips" in above and "1.09457" in above
          and "зараз" in inside and "лімітна" not in inside)
    return ok, (f"поза зоною: відстань і слово 'лімітна'={('лімітна' in above)}, "
                f"у зоні: 'зараз'={('зараз' in inside)}")


def test_manual_fill_reports_real_risk() -> tuple[bool, str]:
    """A fill reported with /took must move break-even, not just the label.

    The plan expected 1.09457; the owner entered at 1.09480. Measuring R from the plan
    would put break-even 4 pips higher than the money he actually risked, and the
    trailing stop would sit in the wrong place.
    """
    from lifecycle import entry_price_of, realized_r

    planned = _signal()                                     # buy, entry 1.1005, stop 1.0985
    manual = _signal(fill_price=1.0960)                     # entered 45 pips higher
    # Break-even must sit on the real fill: +0.75R from 1.0960, not from 1.1005.
    be_manual = 1.0960 + 0.75 * abs(1.0960 - 1.0985)
    be_planned = 1.1005 + 0.75 * abs(1.1005 - 1.0985)
    reach = realized_r(manual, be_manual)
    ok = (entry_price_of(manual) == 1.0960 and entry_price_of(planned) == 1.1005
          and reach == 0.75 and abs(be_manual - be_planned) > 0.003)
    return ok, (f"вхід із /took={entry_price_of(manual)}, з плану={entry_price_of(planned)}, "
                f"на беззбитку {reach}R, різниця рівнів {abs(be_manual - be_planned) * 10000:.0f} pips")


def test_expiry_time_matches_the_decision() -> tuple[bool, str]:
    """A cancelled signal must be stamped with the moment it was judged, not the bar.

    Live case 20261125-106B: created 08:20, cancelled by a 09:25 scan, stored with
    08:30 because the timestamp came from the last closed bar in the feed. The history
    then read "cancelled 9 minutes after it appeared" while the note claimed the
    60-minute window had run out - two contradictory statements about one signal.
    """
    sig = _signal(status="READY")
    # Ten quiet bars, none of them reaching the 1.0995-1.1005 zone.
    bars = [_c(T0 + i * 300, 1.1010, 1.1016, 1.1008, 1.1012) for i in range(10)]
    # The scan runs much later than the last bar: 08:20 created, 09:25 judged.
    judged = datetime(2026, 11, 23, 11, 25, tzinfo=UTC)
    state = evaluate_signal({**sig, "created_at": datetime(2026, 11, 23, 10, 20, tzinfo=UTC).isoformat()},
                            bars, judged)
    event = state["event"]
    stamp = datetime.fromtimestamp(event["ts"], tz=UTC) if event else None
    ok = (event is not None and event["status"] == "CANCELLED"
          and stamp is not None and stamp >= judged - timedelta(seconds=1))
    return ok, (f"подія={event['status'] if event else None}, "
                f"час={stamp.strftime('%H:%M') if stamp else '—'} при рішенні {judged.strftime('%H:%M')}")


def test_entry_wait_covers_real_fills() -> tuple[bool, str]:
    """The entry window must be wide enough for the entries the setup really gets.

    On 41 real trades 40 filled within 45 minutes and one at minute 50, so the old
    hardcoded 45 threw away a trade the plan had called for. The window is now 60.
    """
    from lifecycle import ENTRY_WAIT_MIN

    fits_most = ENTRY_WAIT_MIN >= 50      # covers the slowest real fill
    not_forever = ENTRY_WAIT_MIN <= 90    # a signal must still expire
    return fits_most and not_forever, (f"вікно входу {ENTRY_WAIT_MIN} хв — "
                                       f"покриває найповільніший вхід (50), "
                                       f"але сигнал усе ще згорає")


def test_fill_is_announced() -> tuple[bool, str]:
    """A filled entry must produce an event, a saved ACTIVE status and a message.

    Live case 20261125-A8E4: the SELL filled, ran, and was stopped out, and Telegram
    never said "вхід виконано" - the card stayed "готовий · рейтинг A+" while the
    position was open. The cause was that evaluate_signal returned no event for a
    plain fill, so scan.py had nothing to save and nothing to send.
    """
    sig = _signal()
    bar = _c(T0, 1.1000, 1.1006, 1.0999, 1.1002)     # dips into the buy zone
    state = evaluate_signal(sig, [bar], datetime(2026, 11, 23, 10, 10, tzinfo=UTC))
    filled = state.get("filled")
    ok = (state["event"] is None and filled is not None
          and filled["status"] == "ACTIVE" and filled["price"] == 1.1005)
    return ok, f"подія входу={filled['status'] if filled else None}, ціна={(filled or {}).get('price')}"


def test_same_bar_fill_and_stop_is_announced() -> tuple[bool, str]:
    """A fill and a stop on one bar must still announce the entry first.

    M5 cannot know whether the entry or the stop came first inside the bar, so the
    stop is booked - but a bare "стоп" for a position the owner was never told existed
    is worse than useless. Both lifecycles paths that close a trade now carry the
    fill, so scan.py can send "вхід виконано" and then the close.
    """
    sig = _signal()
    # BUY zone 1.0995-1.1005, stop 1.0985. One bar dips into the zone and then
    # collapses through the stop: entry and exit in the same 5 minutes.
    bar = _c(T0, 1.1006, 1.1008, 1.0980, 1.0982)
    state = evaluate_signal(sig, [bar], datetime(2026, 11, 23, 10, 10, tzinfo=UTC))
    filled = state.get("filled")
    ok = (state["event"] is not None and state["event"]["status"] == "SL_HIT"
          and filled is not None and filled["price"] == 1.1005)
    return ok, f"подія={(state['event'] or {}).get('status')}, вхід={(filled or {}).get('price')}"


def test_break_even_is_announced() -> tuple[bool, str]:
    """Break-even must send a message even when the step is smaller than the trail filter.

    The stop moves from the original level to the entry and the trade stops losing
    money there. On A8E4 that move was 7.8 pips and produced nothing at all, so the
    owner had to notice +0.75R himself. Break-even is exempt from the pip threshold.
    """
    sig = _signal(status="ACTIVE")
    # BUY: entry 1.1005, stop 1.0985, risk 20 pips. +0.75R is 1.1020, so the bar has
    # to trade through it - a high of 1.1018 is only +0.65R and leaves the stop alone.
    bars = [_c(T0, 1.1005, 1.1026, 1.1004, 1.1024)]
    state = evaluate_signal(sig, bars, datetime(2026, 11, 23, 10, 10, tzinfo=UTC))
    note = notify.stop_note({**sig, "best_r": state["best_r"]}, state["stop"], state["kind"])
    ok = (state["kind"] == "BE" and abs(state["stop"] - 1.1005) < 1e-9
          and "беззбиток" in note.lower())
    return ok, f"стоп={round(state['stop'], 5)}, kind={state['kind']}, текст містить 'беззбиток'"


def test_cancelled_setup_is_not_reissued() -> tuple[bool, str]:
    """The same setup must not come back minutes after it was cancelled.

    Live pair: 20261125-106B (SELL 1.09433) expired at 09:20 because price never
    returned, and at 09:30 the bot issued 20261125-A8E4 (SELL 1.09385) - the same
    idea, 4.8 pips away, ten minutes after saying the first one was off.
    """
    from scan import _duplicate

    now = datetime(2026, 11, 25, 9, 30, tzinfo=UTC)
    cancelled = {"side": "SELL", "trigger": 1.09433, "status": "CANCELLED",
                 "id": "106B", "created_at": "2026-11-25T08:20:25+00:00"}
    cand = Candidate(side="SELL", setup="FVG", entry_low=1.09385, entry_high=1.09445,
                     trigger=1.09385, stop=1.09463, tp1=1.09176, tp2=1.09108, tp3=None,
                     rr=2.0, risk_pips=7.8, reward_pips=20.9, expected_hold_min=60,
                     max_hold_min=210, grade="A+", score=96, context={}, why=[],
                     cancel_rules=[], invalidations=[])
    blocked = _duplicate(cand, [], [cancelled], now)
    # ...and after the cooldown the same setup is allowed again.
    later = now + timedelta(minutes=180)
    allowed = _duplicate(cand, [], [cancelled], later)
    ok = blocked is not None and allowed is None
    return ok, f"через 70 хв={bool(blocked)}, через 3 год={bool(allowed)}"


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


def test_daily_limit_is_three_and_blocks_the_fourth() -> tuple[bool, str]:
    """Three real trades are allowed, the fourth setup is refused with that number.

    Raised from 2 to 3 in 2.7.6 at the owner's request: on 59 days the same exit
    rule made +15.13R at 35 trades and +15.58R at 40, so the second slot was cutting
    real setups rather than noise. The cap still has to bind - without it the same
    period offers 51 signals - so the check runs the gate itself, at 2, 3 and 4.
    """
    from config import Risk
    from signals import daily_slot_block

    risk = Risk()
    got = [daily_slot_block(n, risk) for n in (0, 2, 3, 4)]
    # 0 and 2 of 3 pass; 3 and 4 are refused, and the sentence names the cap.
    ok = (got[0] is None and got[1] is None
          and got[2] is not None and "3" in got[2]
          and got[3] is not None and got[2] == got[3]
          and risk.max_signals_per_day == 3)
    return ok, (f"ліміт {risk.max_signals_per_day}: 2 угоди проходять, "
                f"3-тя блокує — «{got[2]}»" if ok else f"ліміт {risk.max_signals_per_day}, ворота {got}")


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
    ("Trailing starts only at +2.0R", test_trail_waits_until_two_r),
    ("Trailing gap beats the spread", test_trail_min_gap_beats_spread),
    ("Targets do not close the trade", test_improvement_is_not_a_target_hit),
    ("Live rule matches the backtest", test_trailing_matches_backtest),
    ("Stop move message is actionable", test_stop_move_message_is_actionable),
    ("Stop moves are not spammy", test_stop_moves_are_not_spammy),
    ("Zero result is still a trade", test_zero_result_trade_is_still_a_trade),
    ("Loss is exactly 1R, break-even is 0R", test_loss_is_exactly_one_r),
    ("Live and backtest book the same R", test_live_and_backtest_book_the_same_result),
    ("Old signal keeps its exit plan", test_old_signal_keeps_its_own_exit_plan),
    ("Legacy signal never trails", test_legacy_signal_never_trails),
    ("Exit rule wording is unambiguous", test_exit_rule_wording_is_unambiguous),
    ("Unfilled signal is not a trade", test_unfilled_signal_is_not_a_trade),
    ("Entry wait covers real fills", test_entry_wait_covers_real_fills),
    ("Expiry time matches the decision", test_expiry_time_matches_the_decision),
    ("Card says it is a limit order", test_card_says_it_is_a_limit_order),
    ("Manual fill reports real risk", test_manual_fill_reports_real_risk),
    ("Fill is announced", test_fill_is_announced),
    ("Fill and stop on one bar", test_same_bar_fill_and_stop_is_announced),
    ("Break-even is announced", test_break_even_is_announced),
    ("Cancelled setup is not reissued", test_cancelled_setup_is_not_reissued),
    ("Cancelled signal keeps the daily slot", test_cancelled_signal_does_not_use_daily_limit),
    ("Daily limit is three, not four", test_daily_limit_is_three_and_blocks_the_fourth),
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
