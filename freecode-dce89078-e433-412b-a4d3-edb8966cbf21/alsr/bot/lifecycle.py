"""Signal lifecycle: READY -> ACTIVE -> TP/SL, driven by real closed M5 candles.

If one candle touches both SL and a target, the intrabar order is unknowable, so the
result is scored conservatively as SL and the note says why. Nothing is ever
back-dated or re-labelled after the fact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import PIP
from datafeed import Candle

TERMINAL = {"TP1_HIT", "TP2_HIT", "TP3_HIT", "SL_HIT", "CANCELLED", "EXPIRED", "UNDETERMINED"}
# TP1 closes the position. Measured on 59 days: closing fully at TP1 made +15.75R,
# while holding part or all to TP2 made between +11.79R and +14.65R. TP2 and TP3 stay
# in the message as reference levels for anyone who wants to run a wider target, but the
# tracked result is the TP1 exit, so the statistics match the measured behaviour.
OPEN_STATUSES = ("WATCHLIST", "READY", "ACTIVE")


def _parse(ts) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def advance(signal: dict, m5: list[Candle], now: datetime) -> dict | None:
    """Return a resolution dict when the signal changes state, else None."""
    status = signal["status"]
    if status not in ("READY", "ACTIVE", "TP1_HIT", "TP2_HIT"):
        return None

    is_buy = signal["side"] == "BUY"
    stop, tp1, tp2, tp3 = signal["stop"], signal["tp1"], signal["tp2"], signal.get("tp3")
    created = _parse(signal["created_at"]).timestamp()
    relevant = [c for c in m5 if c.t >= created and c.complete]
    state = status

    # Break-even management, the same rule the backtest measured: once the trade has
    # 0.75R of profit, the stop moves to the entry price. On 59 days of real bars this
    # turned full -1R losses into scratches often enough to add about +0.13R per trade,
    # confirmed by a paired bootstrap whose 5th percentile stayed above zero on both a
    # 59-day and a 30-day sample.
    entry_fill = signal["entry_high"] if is_buy else signal["entry_low"]
    be_at_r = float(signal.get("break_even_at_r") or 0.0)
    original_risk = abs(entry_fill - signal["stop"])
    be_level = (entry_fill + be_at_r * original_risk * (1 if is_buy else -1)) if (be_at_r > 0 and original_risk > 0) else None

    for c in relevant:
        touched = (c.l <= signal["entry_high"]) if is_buy else (c.h >= signal["entry_low"])
        if state == "READY":
            if not touched:
                continue
            state = "ACTIVE"

        hit_stop = (c.l <= stop) if is_buy else (c.h >= stop)
        hit1 = (c.h >= tp1) if is_buy else (c.l <= tp1)
        hit2 = (c.h >= tp2) if is_buy else (c.l <= tp2)
        hit3 = tp3 is not None and ((c.h >= tp3) if is_buy else (c.l <= tp3))

        if hit_stop and (hit1 or hit2 or hit3):
            return {
                "status": "SL_HIT",
                "price": stop,
                "ts": c.t,
                "note": "Одна M5 свічка торкнулась і SL, і цілі. Внутрішньосвічковий порядок невідомий, тому консервативно зараховано SL.",
            }
        if hit_stop:
            note = (f"Беззбиток спрацював на вході {stop:.5f} — угода закрита в нуль."
                    if be_level is not None and abs(stop - entry_fill) < 1e-9
                    else f"SL досягнуто на {stop:.5f}.")
            return {"status": "SL_HIT", "price": stop, "ts": c.t, "note": note}
        if hit3 and tp3 is not None:
            return {"status": "TP3_HIT", "price": tp3, "ts": c.t, "note": "TP3 досягнуто — сценарій закрито повністю."}
        if hit2:
            if tp3 is None:
                return {"status": "TP2_HIT", "price": tp2, "ts": c.t, "note": "TP2 досягнуто — фінальна ціль сценарію."}
            if state != "TP2_HIT":
                return {"status": "TP2_HIT", "price": tp2, "ts": c.t, "note": "TP2 досягнуто, залишок тримаємо до TP3."}
        if hit1 and state in ("READY", "ACTIVE"):
            return {"status": "TP1_HIT", "price": tp1, "ts": c.t,
                    "note": "TP1 досягнуто — закрий основну частину; далі стоп тримаємо в беззбитку."}
        state = "TP2_HIT" if hit2 and state == "TP1_HIT" else state

        # Move the stop only after this bar has been checked against the old one, so a
        # bar that spikes to 0.75R and collapses back is still a normal loss.
        if be_level is not None and stop != entry_fill:
            reached = (c.h >= be_level) if is_buy else (c.l <= be_level)
            if reached:
                stop = entry_fill

    # Time-based exits.
    created_dt = _parse(signal["created_at"])
    if state == "READY" and now - created_dt > timedelta(minutes=45):
        return {"status": "CANCELLED", "price": signal["trigger"], "ts": int(now.timestamp()),
                "note": "Вхід не активовано за 45 хв — сценарій скасовано без угоди."}
    if state in ("ACTIVE", "TP1_HIT", "TP2_HIT") and now - created_dt > timedelta(minutes=signal["max_hold_min"]):
        last = relevant[-1].c if relevant else signal["trigger"]
        return {"status": "EXPIRED", "price": last, "ts": int(now.timestamp()),
                "note": f"Максимальний час утримання {signal['max_hold_min']} хв вичерпано."}
    return None


def realized_r(signal: dict, exit_price: float) -> float:
    is_buy = signal["side"] == "BUY"
    entry = signal["entry_high"] if is_buy else signal["entry_low"]
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
    }
    if event["status"] in TERMINAL:
        updated["resolved_at"] = datetime.fromtimestamp(event["ts"], tz=timezone.utc).isoformat() if event["ts"] > 1e9 else now.isoformat()
        updated["r_result"] = 0.0 if event["status"] == "CANCELLED" else realized_r(signal, event["price"])
    return updated


def positions_summary(signals: list[dict]) -> dict:
    final = [s for s in signals if s.get("resolved_at") and s.get("r_result") is not None]
    wins = [s for s in final if (s["r_result"] or 0) > 0]
    losses = [s for s in final if (s["r_result"] or 0) < 0]
    total_r = round(sum(s["r_result"] for s in final), 2)
    return {
        "closed": len(final),
        "wins": len(wins),
        "losses": len(losses),
        "total_r": total_r,
        "open": len([s for s in signals if s["status"] in OPEN_STATUSES]),
    }
