"""Scan orchestration. One function does a full cycle, safely and idempotently.

Order matters: resolve what already exists first, then read news, then look for a new
setup. A new signal is stored BEFORE it is sent, so a Telegram failure can never
create a duplicate setup on the next run.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import notify
from analysis import analyse, day_week_levels
from config import ENV, PIP, SYMBOL
from datafeed import Snapshot, build_snapshot
from lifecycle import OPEN_STATUSES, advance, apply
from markettime import day_bounds, news_risk, session_info, utcnow
from signals import build_signal, evaluate
from store import Store


@dataclass
class ScanResult:
    at: datetime
    verdict: str = "NO_TRADE"
    status: str = "UNKNOWN"
    details: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    telegram_sent: int = 0
    telegram_skipped: int = 0
    telegram_errors: list[str] = field(default_factory=list)
    duration_ms: int = 0


_RUN_LOCK = threading.Lock()


def snapshot_frames(snap: Snapshot) -> dict:
    return {tf: analyse(tf, snap.candles.get(tf, [])) for tf in ("M5", "M15", "H1", "H4")}


def _send_once(store: Store, key: str, kind: str, text: str, signal_id: str | None = None) -> tuple[str, str | None]:
    """Return ('sent'|'skipped'|'error', message)."""
    if not store.claim_message(key, kind, signal_id):
        return "skipped", None
    ok, err = notify.send(ENV.telegram_token, ENV.telegram_chat_id, text)
    if not ok:
        store.release_message(key)
        return "error", err
    return "sent", None


def calendar_usable(calendar_ok: bool | None, stored_events: list[dict], news_enabled: bool) -> bool:
    """Can release risk be judged from what we have?

    A successful fetch that happens to contain no EUR/USD releases is a valid
    answer. A failed fetch is only acceptable if earlier cycles already stored a
    calendar. Anything else means "unknown", and unknown must block new entries.
    """
    if not news_enabled:
        return True
    if calendar_ok is True:
        return True
    return bool([e for e in stored_events if str(e.get("source", "")).startswith("Economic calendar")])


def _today_signals(store: Store, now: datetime) -> list[dict]:
    start, end, _ = day_bounds(now)
    todays = []
    for s in store.list_signals(limit=300):
        created = s.get("created_at")
        ts = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else created
        if ts and start <= ts.astimezone(timezone.utc) < end:
            todays.append(s)
    return todays


def run_scan(store: Store, *, send: bool = True) -> ScanResult:
    now = utcnow()
    result = ScanResult(at=now)
    started = datetime.now(timezone.utc)

    with _RUN_LOCK:
        try:
            snap = build_snapshot()
            result.status = snap.quality.status
            frames = snapshot_frames(snap)
            session = session_info(now)
            _, _, day_key = day_bounds(now)

            state = store.state()
            runs_today = state.get("runs_today", 0) + 1 if state.get("runs_day") == day_key else 1
            store.set_state(last_run_at=now.isoformat(), runs_day=day_key, runs_today=runs_today)

            # 1) Advance existing signals with real closed candles.
            open_signals = store.list_signals(statuses=list(OPEN_STATUSES), limit=20)
            for sig in open_signals:
                event = advance(sig, snap.candles.get("M5", []), now)
                if not event:
                    continue
                updated = apply(sig, event, now)
                store.save_signal(updated)
                result.updates.append(updated["id"])
                if send and ENV.telegram_ready:
                    outcome, err = _send_once(
                        store,
                        f"update:{updated['id']}:{updated['status']}",
                        "signal_update",
                        notify.format_update(updated, event["note"]),
                        updated["id"],
                    )
                    _tally(result, outcome, err)
                store.log("info", "lifecycle", f"{updated['id']} → {updated['status']} ({event['note']})")

            # 2) News + calendar.
            events: list[dict] = []
            # Whether the release calendar could actually be read this cycle. Starts
            # as "unknown" so a crash inside the block can never look like success.
            calendar_ok: bool | None = None
            if ENV.enable_news:
                try:
                    from newsfeed import fetch_calendar, fetch_news

                    # The free calendar host rate-limits hard (HTTP 429). Events are
                    # weekly, so re-reading them every cycle is both wasteful and
                    # risky: refresh at most every 30 min, and only if the stored
                    # calendar has expired or is missing.
                    cal: list[dict] = []
                    cal_status = None
                    have_cal = [
                        e for e in store.list_events(now - timedelta(days=1), now + timedelta(days=7))
                        if str(e.get("source", "")).startswith("Economic calendar")
                    ]
                    last_fetch = store.get_setting("calendar_fetched_at", None)
                    stale = True
                    if last_fetch:
                        try:
                            fetched = datetime.fromisoformat(str(last_fetch).replace("Z", "+00:00"))
                            if fetched.tzinfo is None:
                                fetched = fetched.replace(tzinfo=timezone.utc)
                            stale = (now - fetched) > timedelta(minutes=30)
                        except (ValueError, TypeError):
                            stale = True

                    if have_cal and not stale:
                        cal = have_cal
                        calendar_ok = True
                    else:
                        cal, cal_status = fetch_calendar()
                        if cal_status.ok:
                            store.set_setting("calendar_fetched_at", now.isoformat())
                            # A successful empty answer is a valid "no releases this
                            # week"; only a failed request means we cannot judge risk.
                            calendar_ok = True
                        else:
                            # Keep using what we already stored rather than dropping it.
                            cal = have_cal
                            calendar_ok = bool(have_cal)

                    press, press_statuses = fetch_news()
                    store.save_events(cal + press)
                    events = cal + press
                    failed = [s for s in [cal_status, *press_statuses] if s is not None and not s.ok]
                    if failed:
                        store.log("warn", "news", f"Недоступні фіди: {', '.join(s.source for s in failed)}")
                except Exception as exc:  # noqa: BLE001
                    store.log("warn", "news", f"Помилка новин: {exc}")

            window_start = now - timedelta(days=1)
            window_end = now + timedelta(days=7)
            stored_events = store.list_events(window_start, window_end) or events
            # "Feed unreachable" is not the same as "no releases this week". Entries
            # are blocked only when the request failed and nothing was cached before.
            usable = calendar_usable(calendar_ok, stored_events, ENV.enable_news)
            risk = news_risk(
                stored_events,
                now,
                ENV.data.news_blackout_before_min,
                ENV.data.news_blackout_after_min,
                feed_ok=usable,
            )
            if not usable:
                store.log("warn", "news", "Календар недоступний — нові входи заблоковано до відновлення фіду.")

            # 3) Decide.
            todays = _today_signals(store, now)
            daily_r = sum((s.get("r_result") or 0) for s in todays if s.get("resolved_at"))
            levels = day_week_levels(snap.candles.get("H1", []), int(now.timestamp()))
            open_now = store.list_signals(statuses=["READY", "ACTIVE", "TP1_HIT", "TP2_HIT"], limit=10)

            decision = evaluate(
                snap, frames, levels, session, risk,
                open_count=len(open_now),
                signals_today=len(todays),
                daily_r=daily_r,
                risk=ENV.risk,
                data_cfg=ENV.data,
            )
            result.verdict = decision.verdict
            result.details = decision.details

            store.log_decision({
                "at": now,
                "verdict": decision.verdict,
                "session": session.active,
                "details": decision.details[:25],
            })

            # 4) Emit a new signal only when the engine produced an A/A+ READY setup.
            if decision.verdict == "SIGNAL" and decision.candidate and ENV.enable_signals:
                if store.state().get("paused"):
                    result.details.append("Сигнали призупинено командою /pause.")
                elif _duplicate(decision.candidate, open_now):
                    result.details.append("Схожий сценарій уже відкритий — повтор не надсилаємо.")
                else:
                    sig = build_signal(decision.candidate, snap, session, ENV.risk, now, SYMBOL, risk)
                    store.save_signal(sig)          # persist BEFORE sending
                    result.created.append(sig["id"])
                    if send and ENV.telegram_ready:
                        outcome, err = _send_once(store, f"signal:{sig['id']}:READY", "signal_new", notify.format_signal(sig), sig["id"])
                        _tally(result, outcome, err)
                    store.log("info", "signal", f"Створено {sig['id']} {sig['side']} {sig['grade']} score={sig['score']}")

            store.set_state(last_ok_at=now.isoformat(), last_error=None)
            result.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            return result

        except Exception as exc:  # noqa: BLE001
            store.log("error", "scan", str(exc))
            store.set_state(last_error=str(exc))
            result.verdict = "BLOCKED"
            result.details = [f"Помилка сканування: {exc}"]
            result.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            return result


def _duplicate(candidate, open_signals: list[dict]) -> bool:
    for s in open_signals:
        if s["side"] != candidate.side:
            continue
        if abs(float(s["trigger"]) - candidate.trigger) / PIP < 5:
            return True
    return False


def _tally(result: ScanResult, outcome: str, err: str | None) -> None:
    if outcome == "sent":
        result.telegram_sent += 1
    elif outcome == "skipped":
        result.telegram_skipped += 1
    else:
        result.telegram_errors.append(err or "невідома помилка Telegram")
