"""Signal lifecycle: READY -> ACTIVE -> TP/SL, driven by real closed M5 candles.

If one candle touches both SL and a target, the intrabar order is unknowable, so the
result is scored conservatively as SL and the note says why. Nothing is ever
back-dated or re-labelled after the fact.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config import PIP
from datafeed import Candle

TERMINAL = {"TP2_HIT", "TP3_HIT", "SL_HIT", "CANCELLED", "EXPIRED", "UNDETERMINED"}
OPEN_STATUSES = ("WATCHLIST", "READY", "ACTIVE", "TP1_HIT", "TP2_HIT")


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
            return {"status": "SL_HIT", "price": stop, "ts": c.t, "note": f"SL досягнуто на {stop:.5f}."}
        if hit3 and tp3 is not None:
            return {"status": "TP3_HIT", "price": tp3, "ts": c.t, "note": "TP3 досягнуто — сценарій закрито повністю."}
        if hit2:
            if tp3 is None:
                return {"status": "TP2_HIT", "price": tp2, "ts": c.t, "note": "TP2 досягнуто — фінальна ціль сценарію."}
            if state != "TP2_HIT":
                return {"status": "TP2_HIT", "price": tp2, "ts": c.t, "note": "TP2 досягнуто, залишок тримаємо до TP3."}
        if hit1 and state in ("READY", "ACTIVE"):
            return {"status": "TP1_HIT", "price": tp1, "ts": c.t, "note": "TP1 досягнуто — стоп можна перевести в беззбиток."}
        state = "TP2_HIT" if hit2 and state == "TP1_HIT" else state

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
