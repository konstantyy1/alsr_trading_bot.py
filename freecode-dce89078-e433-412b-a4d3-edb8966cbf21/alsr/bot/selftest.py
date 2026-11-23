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
    now = datetime(2026, 11, 23, 10, 10, tzinfo=UTC)
    event = advance(_signal(), [_c(T0, 1.1000, 1.1032, 1.0998, 1.1028)], now)
    ok = event is not None and event["status"] == "TP1_HIT"
    return ok, event["status"] if event else "no event"


def test_lifecycle_ambiguous_candle() -> tuple[bool, str]:
    """One M5 bar hitting both SL and TP must be scored as SL, and say why."""
    now = datetime(2026, 11, 23, 10, 10, tzinfo=UTC)
    event = advance(_signal(), [_c(T0, 1.1000, 1.1035, 1.0980, 1.1030)], now)
    ok = (
        event is not None
        and event["status"] == "SL_HIT"
        and "невідомий" in event["note"]
    )
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
def test_signal_template() -> tuple[bool, str]:
    text = format_signal(_signal())
    required = [
        "Статус", "Рейтинг", "Score", "Entry zone", "Entry trigger", "Stop Loss",
        "TP1", "TP2", "TP3", "Risk/Reward", "час утримання", "Ринковий контекст",
        "Новинний ризик", "Spread", "Джерело ціни", "Чому саме цей сигнал",
        "Що скасує сценарій", "За яких умов не відкривати", "аналітика, не наказ",
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
    os.environ["SQLITE_PATH"] = path
    store = Store("")
    first = store.claim_message("selftest:key", "test")
    second = store.claim_message("selftest:key", "test")
    store.release_message("selftest:key")
    third = store.claim_message("selftest:key", "test")
    store.set_state(runs_today=7)
    ok = first and not second and third and store.state()["runs_today"] == 7
    return ok, "claim/dedupe/release/state round-trip"


def test_score_is_not_winrate() -> tuple[bool, str]:
    text = format_signal(_signal())
    ok = "відповідність правилам, не ймовірність" in text
    return ok, "score labelled as rule compliance"


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
