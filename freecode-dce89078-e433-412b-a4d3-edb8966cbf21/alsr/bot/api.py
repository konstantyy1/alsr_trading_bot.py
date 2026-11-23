"""Payload builders for the JSON API.

Pure functions, no Flask imports, so they can be exercised directly in tests and
from a plain REPL. The Flask app in app.py is only routing + auth on top of this.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from analysis import analyse, day_week_levels
from config import ENV, PIP
from datafeed import build_snapshot
from lifecycle import OPEN_STATUSES, positions_summary
from markettime import news_risk, session_info, utcnow, warsaw_str
from store import Store

VERSION = "2.0.0"
_started_at = datetime.now(timezone.utc)

# A snapshot costs 4-5 upstream requests, so cache it briefly for the dashboard.
_SNAP_CACHE: dict = {"at": None, "snap": None, "frames": None}
SNAP_TTL_SEC = 45

# The free calendar feed rate-limits (HTTP 429) if polled too often, so the
# dashboard may only fetch it itself once per cooldown. The scanner stores events
# on every run, which is the normal source.
_CAL_FETCH: dict = {"at": 0.0, "note": None}
CAL_COOLDOWN_SEC = 600


def _calendar_self_heal(store: Store, now) -> str | None:
    """Try to populate an empty calendar, at most once per cooldown.

    Returns a short honest note when the fetch could not happen.
    """
    last = _CAL_FETCH["at"]
    wait = CAL_COOLDOWN_SEC - (time.time() - last)
    if last and wait > 0:
        return f"Календар підтягнеться після наступного скану (ліміт запитів до фіду, ще {int(wait // 60) + 1} хв)."
    _CAL_FETCH["at"] = time.time()
    try:
        from newsfeed import fetch_calendar

        fresh, status = fetch_calendar()
    except Exception as exc:  # noqa: BLE001
        return f"Календар недоступний: {exc}"
    if not status.ok:
        if "429" in str(status.error or ""):
            return "Безкоштовний календар тимчасово обмежив запити (HTTP 429). Події зʼявляться після наступного скану."
        return f"Календар недоступний: {status.error}"
    if fresh:
        store.save_events(fresh)
        # Share the fetch time with the scanner so a dashboard refresh does not make
        # the next scheduled scan hit the feed again.
        store.set_setting("calendar_fetched_at", now.isoformat())
    return None


def _now_iso() -> str:
    return utcnow().isoformat()


def status_payload(store: Store) -> dict:
    state = store.state()
    logs = store.list_logs(8)
    last_run = state.get("last_run_at")
    stale = True
    if last_run:
        try:
            ts = datetime.fromisoformat(str(last_run).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            stale = (utcnow() - ts).total_seconds() > ENV.poll_interval_min * 60 * 3
        except (ValueError, TypeError):
            stale = True
    return {
        "version": VERSION,
        "now_warsaw": warsaw_str(),
        "uptime_sec": int((datetime.now(timezone.utc) - _started_at).total_seconds()),
        "data": {
            "provider": "Yahoo Finance (free) · spread припущений",
            "database": store.backend,
            "database_persistent": store.backend == "postgres",
            "scan_interval_min": ENV.poll_interval_min,
        },
        "telegram": {
            "configured": ENV.telegram_ready,
            "chat_bound": bool(ENV.telegram_chat_id),
            "webhook_secret_set": bool(ENV.webhook_secret),
            "signals_enabled": ENV.enable_signals and not state.get("paused"),
        },
        "scan": {
            "last_run_at": last_run,
            "last_ok_at": state.get("last_ok_at"),
            "last_error": state.get("last_error"),
            "runs_today": state.get("runs_today", 0),
            "paused": bool(state.get("paused")),
            "stale": stale,
        },
        "limits": {
            "max_signals_per_day": ENV.risk.max_signals_per_day,
            "max_open_setups": ENV.risk.max_open_setups,
            "risk_percent": ENV.risk.risk_percent,
            "daily_loss_limit_r": ENV.risk.daily_loss_limit_r,
            "min_score": ENV.risk.min_score_ready,
            "min_rr": ENV.risk.min_rr,
        },
        "logs": [
            {
                "at": log["at"].isoformat() if isinstance(log["at"], datetime) else log["at"],
                "level": log["level"],
                "scope": log["scope"],
                "message": log["message"],
            }
            for log in logs
        ],
    }


def _snapshot():
    cached = _SNAP_CACHE["snap"]
    at = _SNAP_CACHE["at"]
    if cached is not None and at and (datetime.now(timezone.utc) - at).total_seconds() < SNAP_TTL_SEC:
        return cached, _SNAP_CACHE["frames"]
    snap = build_snapshot()
    frames = {tf: analyse(tf, snap.candles.get(tf, [])) for tf in ("M5", "M15", "H1", "H4")}
    _SNAP_CACHE.update({"at": datetime.now(timezone.utc), "snap": snap, "frames": frames})
    return snap, frames


def market_payload(store: Store) -> dict:
    snap, frames = _snapshot()
    now = utcnow()
    session = session_info(now)
    events = store.list_events(now - timedelta(days=1), now + timedelta(days=7))
    risk = news_risk(events, now, ENV.data.news_blackout_before_min, ENV.data.news_blackout_after_min)
    levels = day_week_levels(snap.candles.get("H1", []), int(now.timestamp()))
    quote = snap.quote

    return {
        "at": now.isoformat(),
        "status": snap.quality.status,
        "notes": snap.quality.notes[:8],
        "quote": None if quote is None else {
            "mid": round(quote.mid, 5),
            "bid": round(quote.bid, 5),
            "ask": round(quote.ask, 5),
            "spread_pips": quote.spread_pips,
            "spread_is_assumed": quote.spread_is_assumed,
            "age_sec": snap.quality.quote_age_sec,
            "provider": quote.provider,
            "ts": quote.ts.isoformat(),
        },
        "reference": None if snap.validator is None else {
            "mid": round(snap.validator.mid, 5),
            "note": "добовий довідковий курс, не спред",
            "deviation_pips": snap.quality.cross_deviation_pips,
        },
        "frames": {
            tf: {
                "structure": frames[tf].structure,
                "atr_pips": round(frames[tf].atr / PIP, 1),
                "rsi": round(frames[tf].rsi14, 0),
                "bos": frames[tf].bos["dir"] if frames[tf].bos else None,
                "choch": frames[tf].choch["dir"] if frames[tf].choch else None,
                "displacement": frames[tf].displacement,
                "range_high": frames[tf].range_high,
                "range_low": frames[tf].range_low,
                "bars": len(snap.candles.get(tf, [])),
            }
            for tf in ("M5", "M15", "H1", "H4")
        },
        "session": {
            "active": session.active,
            "quality": session.quality,
            "killzone": session.killzone_label if session.in_killzone else None,
            "closed": session.is_weekend,
            "notes": list(session.notes),
        },
        "news": {
            "level": risk.level,
            "reason": risk.reason,
            "minutes_to_next": risk.minutes_to_next,
            "next": None if not risk.next_event else {
                "title": risk.next_event["title"],
                "currency": risk.next_event["currency"],
                "impact": risk.next_event["impact"],
                "at": risk.next_event["time_utc"].isoformat(),
            },
        },
        "levels": [
            {"kind": z.kind, "price": round(z.low, 5), "label": z.label}
            for z in levels
        ],
    }


def signals_payload(store: Store, limit: int = 40) -> dict:
    all_sigs = store.list_signals(limit=limit)
    open_sigs = [s for s in all_sigs if s.get("status") in OPEN_STATUSES]
    return {
        "open": open_sigs,
        "recent": all_sigs,
        "summary": positions_summary(store.list_signals(limit=500)),
    }


def stats_payload(store: Store, limit: int = 500) -> dict:
    sigs = store.list_signals(limit=limit)
    closed = [s for s in sigs if s.get("resolved_at") and s.get("r_result") is not None]
    closed.sort(key=lambda s: str(s["resolved_at"]))

    start_equity = ENV.risk.account_equity
    equity = start_equity
    peak = start_equity
    max_dd_pct = 0.0
    curve = []
    for s in closed:
        r = float(s["r_result"])
        equity += equity * (ENV.risk.risk_percent / 100) * r
        peak = max(peak, equity)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, (peak - equity) / peak * 100)
        curve.append({"at": s["resolved_at"], "equity": round(equity, 2), "r": r, "id": s["id"]})

    wins = [s for s in closed if float(s["r_result"]) > 0]
    losses = [s for s in closed if float(s["r_result"]) < 0]
    flat = [s for s in closed if float(s["r_result"]) == 0]
    total_r = sum(float(s["r_result"]) for s in closed)

    def group(field: str) -> list[dict]:
        buckets: dict[str, list[float]] = {}
        for s in closed:
            buckets.setdefault(str(s.get(field, "—")), []).append(float(s["r_result"]))
        rows = [
            {
                "key": k,
                "count": len(v),
                "wins": len([x for x in v if x > 0]),
                "total_r": round(sum(v), 2),
                "avg_r": round(sum(v) / len(v), 2),
            }
            for k, v in buckets.items()
        ]
        return sorted(rows, key=lambda x: -x["count"])

    return {
        "sample": {
            "closed": len(closed),
            "open": len([s for s in sigs if s.get("status") in OPEN_STATUSES]),
            "total": len(sigs),
            "enough_for_conclusions": len(closed) >= 100,
        },
        "results": {
            "wins": len(wins),
            "losses": len(losses),
            "flat": len(flat),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "total_r": round(total_r, 2),
            "expectancy_r": round(total_r / len(closed), 2) if closed else None,
            "avg_win_r": round(sum(float(s["r_result"]) for s in wins) / len(wins), 2) if wins else None,
            "avg_loss_r": round(sum(float(s["r_result"]) for s in losses) / len(losses), 2) if losses else None,
        },
        "equity": {
            "start": start_equity,
            "now": round(equity, 2),
            "risk_percent": ENV.risk.risk_percent,
            "max_drawdown_pct": round(max_dd_pct, 1),
            "curve": curve[-120:],
            "virtual": True,
        },
        "by_setup": group("setup"),
        "by_grade": group("grade"),
        "disclaimer": "Віртуальний облік. Жодного наказу брокеру не надсилалось.",
    }


def calendar_payload(store: Store, days: int = 7, self_heal: bool = False) -> dict:
    now = utcnow()
    events = store.list_events(now - timedelta(days=1), now + timedelta(days=days))

    # A fresh deploy has no events until the first scan. Rather than showing an
    # empty calendar, fetch it once (with a cooldown) and store it.
    note = None
    if self_heal and len([e for e in events if e["time_utc"] >= now]) < 3:
        note = _calendar_self_heal(store, now)
        events = store.list_events(now - timedelta(days=1), now + timedelta(days=days))

    upcoming = sorted([e for e in events if e["time_utc"] >= now], key=lambda e: e["time_utc"])
    past = sorted([e for e in events if e["time_utc"] < now], key=lambda e: e["time_utc"], reverse=True)
    fmt = lambda e: {  # noqa: E731
        "title": e["title"],
        "currency": e["currency"],
        "impact": e["impact"],
        "at": e["time_utc"].isoformat(),
        "at_warsaw": warsaw_str(e["time_utc"]),
        "source": e["source"],
        "url": e.get("url"),
        "actual": e.get("actual"),
        "forecast": e.get("forecast"),
        "previous": e.get("previous"),
        "minutes": int((e["time_utc"] - now).total_seconds() // 60),
    }
    return {
        "upcoming": [fmt(e) for e in upcoming[:20]],
        "recent": [fmt(e) for e in past[:10]],
        "counts": {"upcoming": len(upcoming), "recent": len(past)},
        # A fetch failure only matters when it left us with nothing to show.
        "note": (note if note and not upcoming else None)
        or "Порожні actual/forecast/previous означають, що джерело їх не публікує.",
    }


def decisions_payload(store: Store, limit: int = 15) -> dict:
    rows = store.list_decisions(limit)
    return {
        "decisions": [
            {
                "at": r["at"].isoformat() if isinstance(r["at"], datetime) else r["at"],
                "verdict": r["verdict"],
                "session": r["session"],
                "details": r["details"][:15],
            }
            for r in rows
        ]
    }
