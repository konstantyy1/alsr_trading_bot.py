"""Signal lifecycle: READY -> ACTIVE -> exit, driven by real closed M5 candles.

Exit plan since 2.7.0 (measured on 59 days of EUR/USD M5 bars, 41 trades):

  1. break-even once +0.75R is banked,
  2. trailing stop of 1.0 x ATR(14) that starts at +1.5R and never moves back,
  3. TP1/TP2/TP3 are reference levels only - they do NOT close the position.

Compared with the old rule (break-even + full close at TP1) this keeps the same total
(+22.60R vs +20.60R) with roughly half the drawdown (-2.57R vs -5.46R) and a shorter
losing streak. It also won the "worst slice" test: better in 15 of 18 time/spread
slices and never worse than -0.10R in the remaining three.

If one candle touches both the stop and a reference level, the intrabar order is
unknowable, so the stop is scored - the conservative choice. Nothing is ever
back-dated or re-labelled after the fact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from datafeed import Candle

# How long a signal may wait for its entry to fill. Measured on 41 real trades: 40
# filled within 45 minutes and one at minute 50, so 60 keeps every trade the setup was
# meant to take while still closing the ones the market never gave an entry on.
ENTRY_WAIT_MIN = 60

# Statuses that end a signal. TP1_HIT / TP2_HIT / TP3_HIT stay listed because signals
# created by 2.6.x and earlier were stored with them and must keep counting as closed.
TERMINAL = {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CANCELLED", "EXPIRED", "UNDETERMINED"}
OPEN_STATUSES = ("WATCHLIST", "READY", "ACTIVE")
# Statuses advance() will still follow. TP1_HIT and TP2_HIT are deliberately absent:
# since 2.7.0 no target closes a trade, so a signal in one of those states was closed
# by 2.6.x and must stay closed instead of being re-managed.
FOLLOWED = ("READY", "ACTIVE")

ATR_PERIOD = 14


def _parse(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _atr_series(candles: list[Candle], period: int = ATR_PERIOD) -> list[float]:
    """Wilder ATR per bar, same formula the backtest used.

    Computed here instead of being stored on the signal so the trailing distance is
    always read from the same bars the stop is judged on, and a signal restored from
    the database months later still gets a correct value.
    """
    out: list[float] = [0.0] * len(candles)
    run = 0.0
    for i, cur in enumerate(candles):
        prev_c = candles[i - 1].c if i else cur.c
        tr = max(cur.h - cur.l, abs(cur.h - prev_c), abs(cur.l - prev_c))
        if i < period:
            run += tr
            out[i] = run / (i + 1)
        else:
            out[i] = (out[i - 1] * (period - 1) + tr) / period
    return out


def _exit_kind(price: float, entry: float, original_stop: float) -> str:
    """Which stop ended the trade: the real one, break-even, or a trailed profit."""
    if abs(price - original_stop) < 1e-12:
        return "SL"
    if abs(price - entry) < 1e-12:
        return "BE"
    return "TRAIL"


def entry_price_of(signal: dict) -> float:
    """The price the position was actually opened at.

    Normally the edge of the plan's zone: a buy limit fills at its upper edge, a sell
    limit at its lower one. But the plan says "виконання вручну", so the owner may have
    taken it at a different price and reported it with /took. When that happens every R
    figure - break-even, trailing, realised result - must be measured from the real
    fill, not from the price the plan hoped for.
    """
    if signal.get("fill_price") is not None:
        return float(signal["fill_price"])
    return signal["entry_high"] if signal["side"] == "BUY" else signal["entry_low"]


def evaluate_signal(signal: dict, m5: list[Candle], now: datetime | None = None) -> dict:
    """Replay one signal against closed M5 bars.

    Returns {"event": resolution or None, "stop": current stop, "best_r": best R seen,
             "kind": "SL" | "BE" | "TRAIL" describing where the current stop sits}.

    The whole history is replayed on every call rather than kept as state, so a
    restart, a retry or a duplicated scan can never leave a half-applied stop. The
    result is a pure function of the signal and the bars.
    """
    is_buy = signal["side"] == "BUY"
    entry = entry_price_of(signal)
    original_stop = float(signal["stop"])
    risk = abs(entry - original_stop)
    stop = original_stop
    empty = {"event": None, "stop": stop, "best_r": 0.0, "kind": "SL", "moved": False,
             "filled": None}
    if risk <= 0:
        return empty

    created_dt = _parse(signal["created_at"])
    # A manually reported fill starts the replay at the fill, not at the signal.
    # Otherwise the bars between the alert and the real entry would be replayed as if
    # the position had been open through them, and a dip that happened before the
    # owner entered could take a stop he never had.
    start_dt = _parse(signal["filled_at"]) if signal.get("filled_at") else created_dt
    relevant = [c for c in m5 if c.t >= start_dt.timestamp() and c.complete]
    if not relevant:
        return empty
    atr = _atr_series(m5)
    index = {id(c): i for i, c in enumerate(m5)}

    direction = 1.0 if is_buy else -1.0
    to_r = lambda price: direction * (price - entry) / risk  # noqa: E731

    be_at_r = float(signal.get("break_even_at_r") or 0.0)
    trail_start_r = signal.get("trail_start_r")
    trail_atr = signal.get("trail_atr_mult")
    min_trail_pips = float(signal.get("trail_min_pips") or 0.0)
    max_hold = int(signal.get("max_hold_min") or 240)
    be_level = entry + direction * be_at_r * risk if be_at_r > 0 else None

    state = signal["status"]
    best_r = 0.0
    # The moment the entry fills must be reported. Without this the signal sat at
    # "READY" for its whole life: scan.py only saves when an event or a moved stop
    # comes back, so a fill that was not immediately stopped out left the status
    # untouched. The live signal 20261125-A8E4 filled, ran, and was stopped out -
    # and Telegram never said "вхід виконано", while the card still read "готовий".
    entry_event = None

    def clock(bar: Candle) -> datetime:
        """Decision time for a bar: its close, or the scan clock if that is later.

        The scan clock matters in one case: if the feed stops updating, the last
        closed bar stays old forever and a trade would never reach its holding limit.
        The backtest passes the bar close as `now`, so both callers agree.
        """
        close = datetime.fromtimestamp(bar.t + 300, tz=timezone.utc)
        if now is None:
            return close
        stamp = now if isinstance(now, datetime) else datetime.fromtimestamp(now, tz=timezone.utc)
        return max(close, stamp)

    for c in relevant:
        if state == "READY":
            touched = (c.l <= signal["entry_high"]) if is_buy else (c.h >= signal["entry_low"])
            if not touched:
                # The wait clock runs out on its own, so it is checked here rather
                # than after the loop: an unfilled signal must not scan days of bars.
                # The window follows the signal's own plan (60-210 min on the card),
                # not a hardcoded 45: on 41 real trades one entry filled at minute 50,
                # and a fixed 45 would have thrown that trade away.
                if clock(c) - created_dt > timedelta(minutes=ENTRY_WAIT_MIN):
                    # The recorded time is the moment the window was judged closed, not
                    # the close of the bar that happened to be last in the feed. With a
                    # stale feed those differ a lot: 20261125-106B was created at 08:20,
                    # cancelled by a 09:25 scan, and stored with 08:30 - so the history
                    # read "cancelled 10 minutes after it appeared" while the note said
                    # the 60-minute window had run out. The scan clock is the honest one.
                    return {"event": {"status": "CANCELLED", "price": signal["trigger"],
                                      "ts": int(clock(c).timestamp()),
                                      "note": (f"Ціна не зайшла в зону входу за {ENTRY_WAIT_MIN} хв — "
                                               f"входу не було, угода не відкривалась."),
                                      "exit_kind": "NO_ENTRY", "r_at_exit": 0.0},
                            "stop": stop, "best_r": best_r, "kind": "SL", "moved": False}
                continue
            state = "ACTIVE"
            fill = signal["entry_high"] if is_buy else signal["entry_low"]
            entry_event = {
                "status": "ACTIVE", "price": fill, "ts": c.t + 300,
                "note": f"Вхід виконано на {fill:.5f} — угода відкрита.",
                "exit_kind": None, "r_at_exit": 0.0,
            }

        # Time limit is checked before the stop on the same bar, so a trade that has
        # run out of time is closed at the bar it expired on instead of being carried
        # through every later bar in the feed.
        if clock(c) - created_dt > timedelta(minutes=max_hold):
            return {"event": {"status": "EXPIRED", "price": c.c, "ts": c.t + 300,
                              "note": f"Максимальний час утримання {max_hold} хв вичерпано.",
                              "exit_kind": "TIME", "r_at_exit": round(to_r(c.c), 2)},
                    "stop": stop, "best_r": best_r,
                    "kind": _exit_kind(stop, entry, original_stop),
                    "moved": stop != original_stop, "filled": entry_event}

        # Stop is judged before anything else on the bar, so a spike that runs to
        # +2R and collapses back is still an ordinary loss at the stop it opened with.
        hit_stop = (c.l <= stop) if is_buy else (c.h >= stop)
        if hit_stop:
            kind = _exit_kind(stop, entry, original_stop)
            if kind == "SL":
                note = f"SL досягнуто на {stop:.5f}."
            elif kind == "BE":
                note = f"Беззбиток спрацював на вході {stop:.5f} — угода закрита в нуль."
            else:
                note = (f"Трейлінг закрив позицію на {stop:.5f} "
                        f"(+{to_r(stop):.2f}R) — прибуток зафіксовано стопом.")
            # Same bar, both a stop and a reference level: the intrabar order is not
            # knowable from M5, so the stop stands and the note says so plainly.
            ref_hit = [name for name, price in
                       (("TP1", signal.get("tp1")), ("TP2", signal.get("tp2")), ("TP3", signal.get("tp3")))
                       if price is not None and ((c.h >= price) if is_buy else (c.l <= price))]
            if ref_hit:
                note += (f" Ця ж свічка торкнулась {' і '.join(ref_hit)}, але внутрішньосвічковий "
                         f"порядок невідомий — тому зараховано стоп.")
            return {
                "event": {"status": "SL_HIT", "price": stop, "ts": c.t, "note": note,
                          "exit_kind": kind, "r_at_exit": round(to_r(stop), 2)},
                "stop": stop, "best_r": best_r, "kind": kind, "moved": True,
                "filled": entry_event,
            }

        # Reference targets. They are reported, never acted on: closing at TP1 scored
        # +19.29R against +20.61R for pure trailing on the same bars.
        best_r = max(best_r, to_r(c.h if is_buy else c.l))

        # Stop management at the bar close, effective from the next bar.
        if be_level is not None:
            reached = (c.h >= be_level) if is_buy else (c.l <= be_level)
            if reached:
                stop = max(stop, entry) if is_buy else min(stop, entry)
        if trail_start_r is not None and trail_atr and best_r >= float(trail_start_r):
            i = index.get(id(c), 0)
            dist = max(float(trail_atr) * atr[i], min_trail_pips * 0.0001)
            candidate_r = best_r - dist / risk
            if candidate_r > to_r(stop):
                trail_price = entry + direction * candidate_r * risk
                # A stop level is also the price the broker will fill at, so it can
                # never be placed beyond the close that produced it. Compared with a
                # 1e-9 tolerance: prices come from a float division, so a "clamp" that
                # misses by 5e-13 is a rounding artefact, not an unexecutable order.
                if is_buy:
                    if trail_price > stop:
                        clamped = min(trail_price, c.c)
                        stop = clamped if clamped > stop + 1e-9 else stop
                else:
                    if trail_price < stop:
                        clamped = max(trail_price, c.c)
                        stop = clamped if clamped < stop - 1e-9 else stop

    return {"event": None, "stop": stop, "best_r": best_r,
            "kind": _exit_kind(stop, entry, original_stop), "moved": stop != original_stop,
            "filled": entry_event}


def advance(signal: dict, m5: list[Candle], now: datetime | None = None) -> dict | None:
    """Return a resolution dict when the signal closes, else None."""
    if signal["status"] not in FOLLOWED:
        return None
    return evaluate_signal(signal, m5, now)["event"]


def realized_r(signal: dict, exit_price: float) -> float:
    is_buy = signal["side"] == "BUY"
    entry = entry_price_of(signal)
    risk = abs(entry - signal["stop"])
    if risk <= 0:
        return 0.0
    move = (exit_price - entry) if is_buy else (entry - exit_price)
    return round(move / risk, 2)


def apply(signal: dict, event: dict, now: datetime) -> dict:
    updated = dict(signal)
    updated["status"] = event["status"]
    updated["updated_at"] = now.isoformat()
    updated["resolution"] = {
        "status": event["status"],
        "price": event["price"],
        "ts": event["ts"],
        "note": event["note"],
        "exit_kind": event.get("exit_kind"),
    }
    if event["status"] in TERMINAL:
        updated["resolved_at"] = datetime.fromtimestamp(event["ts"], tz=timezone.utc).isoformat() if event["ts"] > 1e9 else now.isoformat()
        updated["r_result"] = 0.0 if event["status"] == "CANCELLED" else realized_r(signal, event["price"])
    return updated


def positions_summary(signals: list[dict]) -> dict:
    # A cancelled signal never got filled: the price never reached the entry zone, so
    # no position ever existed. Counting it as a "completed trade" with 0R made the
    # dashboard read "Завершено 1 · TP/SL 0/0" after a day in which nothing was traded.
    final = [s for s in signals if s.get("resolved_at") and s.get("r_result") is not None
             and s.get("status") != "CANCELLED"]
    cancelled = [s for s in signals if s.get("status") == "CANCELLED"]
    wins = [s for s in final if (s["r_result"] or 0) > 0]
    losses = [s for s in final if (s["r_result"] or 0) < 0]
    flat = [s for s in final if (s["r_result"] or 0) == 0]
    total_r = round(sum(s["r_result"] for s in final), 2)
    return {
        "closed": len(final),
        "cancelled": len(cancelled),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(flat),
        "total_r": total_r,
        "open": len([s for s in signals if s["status"] in OPEN_STATUSES]),
    }
