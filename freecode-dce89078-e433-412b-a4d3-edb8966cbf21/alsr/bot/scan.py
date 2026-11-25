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
from config import ENV, PIP, SYMBOL, chat_id, telegram_ready

# Smallest stop move worth a Telegram message. A fifth of the median trade risk
# (~6.8 pips), so the number follows the market instead of being a fixed pip guess.
TRAIL_NOTIFY_PIPS = 1.5

# A setup that was just cancelled is not re-issued in the same direction within this
# window and distance. Measured on the live pair 20261125-106B -> 20261125-A8E4: the
# same SELL came back 4.8 pips away and 70 minutes after the alert, 10 after the
# cancellation, right after the owner was told the first one never filled.
DUPLICATE_COOLDOWN_MIN = 120
DUPLICATE_COOLDOWN_PIPS = 8.0
from datafeed import Snapshot, build_snapshot
from lifecycle import OPEN_STATUSES, apply, evaluate_signal
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


def _send_once(store: Store, key: str, kind: str, text: str, signal_id: str | None = None,
               at: datetime | None = None) -> tuple[str, str | None]:
    """Return ('sent'|'skipped'|'error', message)."""
    if not store.claim_message(key, kind, signal_id):
        return "skipped", None
    ok, err = notify.send(ENV.telegram_token, chat_id(store), text)
    if not ok:
        store.release_message(key)
        return "error", err
    store.set_setting("last_message_at", (at or utcnow()).isoformat())
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
                state_now = evaluate_signal(sig, snap.candles.get("M5", []), now)
                event = state_now["event"]
                if event is None:
                    _advance_open(sig, state_now, store, now, result, send)
                    continue
                updated = apply(sig, event, now)
                updated["trail_stop"] = round(event["price"], 5)
                updated["best_r"] = round(state_now["best_r"], 2)
                store.save_signal(updated)
                result.updates.append(updated["id"])
                if send and telegram_ready(store):
                    # A close that happened on the same bar as the fill must still say
                    # the trade was opened: otherwise the owner gets "стоп" for a
                    # position he was never told existed. Two messages, fill first.
                    filled = state_now.get("filled")
                    if filled and sig.get("status") == "READY":
                        opened = {**sig,
                                  "status": "ACTIVE",
                                  "filled_at": datetime.fromtimestamp(
                                      filled["ts"], tz=timezone.utc).isoformat(),
                                  "trail_stop": round(filled["price"], 5),
                                  "updated_at": now.isoformat()}
                        outcome, err = _send_once(
                            store, f"fill:{sig['id']}", "signal_update",
                            notify.format_update(opened, notify.fill_note(opened)), sig["id"],
                        )
                        _tally(result, outcome, err)
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
                else:
                    # A cancelled setup is not "open", so it is passed separately:
                    # without it the bot re-issued the same idea minutes after
                    # telling the owner it was off (106B -> A8E4 on 2026-11-25).
                    dupe = _duplicate(decision.candidate, open_now,
                                      store.list_signals(limit=12), now)
                    if dupe:
                        result.details.append(dupe)
                    else:
                        sig = build_signal(decision.candidate, snap, session, ENV.risk, now, SYMBOL, risk)
                        store.save_signal(sig)          # persist BEFORE sending
                        result.created.append(sig["id"])
                        if send and telegram_ready(store):
                            outcome, err = _send_once(store, f"signal:{sig['id']}:READY", "signal_new", notify.format_signal(sig), sig["id"])
                            _tally(result, outcome, err)
                        store.log("info", "signal", f"Створено {sig['id']} {sig['side']} {sig['grade']} score={sig['score']}")

            # 5) Scheduled messages, then commands. Both are idempotent: each
            # notification is claimed by a date key and each command by update id.
            if send and telegram_ready(store):
                _scheduled_briefings(store, snap, frames, session, risk, stored_events, now, result, levels)
                _release_pending_commands(store, result)

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


def _quiet_for_minutes(store: Store, minutes: int, now: datetime | None = None) -> bool:
    """True when no outgoing message was recorded within the last `minutes`."""
    from datetime import timezone as _tz

    last = store.get_setting("last_message_at", None)
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=_tz.utc)
    return ((now or utcnow()) - ts).total_seconds() >= minutes * 60


def _release_pending_commands(store: Store, result: ScanResult) -> None:
    """Answer Telegram commands left in the bot's queue.

    Render runs without a webhook, so nothing pushes updates to us: the bot would
    stay silent on /status and /now forever. Every scan drains the queue instead,
    which makes commands work with the same five-minute scheduler. The offset is
    stored so a command is never answered twice.
    """
    token = ENV.telegram_token
    target = chat_id(store)
    if not token or not target:
        return

    url, err = notify.webhook_state(token)
    if err is None and url:
        # A webhook is registered: Telegram delivers to it and getUpdates is closed.
        return

    offset = store.get_setting("telegram_offset", None)
    ok, updates, next_offset, perr = notify.poll_updates(token, offset)
    if not ok:
        store.log("warn", "telegram", f"getUpdates: {perr}")
        return

    from commands import handle_command

    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = str((message.get("chat") or {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not text:
            continue
        if chat != target:
            store.log("warn", "telegram", f"Відхилено команду з чужого чату {chat}")
            continue
        try:
            reply = handle_command(store, text)
        except Exception as exc:  # noqa: BLE001
            reply = f"Помилка обробки команди: {notify._esc(exc)}"
        outcome, serr = _send_once(store, f"cmd:{update.get('update_id')}", "command", reply)
        _tally(result, outcome, serr)

    if next_offset and next_offset != offset:
        store.set_setting("telegram_offset", next_offset)


def _scheduled_briefings(store: Store, snap: Snapshot, frames: dict, session, risk,
                          events: list[dict], now: datetime, result: ScanResult,
                          levels=None) -> None:
    """Morning brief, evening summary, and a heartbeat when nothing else was sent.

    All three already had formatters but nothing ever called them, so the terminal
    could be silent for a whole day. They are keyed by date, so each is sent once.
    """
    from markettime import WAW

    local = now.astimezone(WAW)
    day_key = local.strftime("%Y-%m-%d")
    sent_any = bool(result.created or result.updates)

    # Morning brief belongs to the morning only: an evening "morning brief" would be
    # both confusing and a duplicate of the daily summary.
    if store.state().get("brief_sent") != day_key and 6 <= local.hour < 11:
        text = notify.format_brief(snap, frames, session, risk, events, "Ранковий огляд EUR/USD", levels=levels)
        outcome, err = _send_once(store, f"brief:{day_key}", "brief", text, at=now)
        _tally(result, outcome, err)
        if outcome == "sent":
            store.set_state(brief_sent=day_key)
            sent_any = True

    if store.state().get("daily_sent") != day_key and local.hour >= 21:
        from api import stats_payload

        stats = stats_payload(store)
        text = notify.format_daily(
            day_key,
            _today_signals(store, now),
            stats["equity"]["now"],
            ENV.risk.account_equity,
            ENV.risk.risk_percent,
        )
        outcome, err = _send_once(store, f"daily:{day_key}", "daily", text, at=now)
        _tally(result, outcome, err)
        if outcome == "sent":
            store.set_state(daily_sent=day_key)
            sent_any = True

    if not sent_any:
        # Three quiet check-ins a day, at session opens. A heartbeat is skipped when
        # any other message went out recently, so a morning brief is never followed
        # two minutes later by a "no signals" note.
        block = None
        if local.hour == 8:
            block, label = f"{day_key}-0800", "London відкривається"
        elif local.hour == 13:
            block, label = f"{day_key}-1300", "NY / overlap початок"
        elif local.hour == 18:
            block, label = f"{day_key}-1800", "Вечірній зріз"
        quiet = _quiet_for_minutes(store, 55, now)
        if block and quiet and store.state().get("heartbeat_sent") != block:
            text = notify.format_brief(snap, frames, session, risk, events, f"{label} — сигналів немає", levels=levels)
            outcome, err = _send_once(store, f"beat:{block}", "heartbeat", text, at=now)
            _tally(result, outcome, err)
            if outcome == "sent":
                store.set_state(heartbeat_sent=block)
                sent_any = True


def _duplicate(candidate, open_signals: list[dict],
               recent_signals: list[dict] | None = None,
               now: datetime | None = None) -> str | None:
    """Why this setup must not become a new signal, or None when it is fresh.

    Two separate checks, because they catch different mistakes:

    * An open setup in the same direction within 5 pips is the same trade twice.
    * A *cancelled* setup is the case that actually happened: 20261125-106B (SELL
      1.09433) expired at 09:20 because price never came back, and ten minutes later
      the bot issued 20261125-A8E4 (SELL 1.09385) - the same idea, 4.8 pips away,
      right after telling the owner the first one was off. Re-issuing it looks like a
      contradiction and doubles the risk on one market move, so a cancelled setup is
      given a cooldown before the same direction may speak again.
    """
    for s in open_signals:
        if s.get("side") != candidate.side:
            continue
        if abs(float(s["trigger"]) - candidate.trigger) / PIP < 5:
            return "схожий сценарій уже відкритий — повтор не надсилаємо"

    if not recent_signals or now is None:
        return None
    for s in recent_signals:
        if s.get("side") != candidate.side:
            continue
        if str(s.get("status")) != "CANCELLED":
            continue
        if abs(float(s["trigger"]) - candidate.trigger) / PIP >= DUPLICATE_COOLDOWN_PIPS:
            continue
        created = s.get("created_at")
        try:
            since = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
        if (now - since) < timedelta(minutes=DUPLICATE_COOLDOWN_MIN):
            return (f"той самий сетап щойно скасовано ({s.get('id')}) — "
                    f"чекаємо {DUPLICATE_COOLDOWN_MIN} хв, щоб не видавати його вдруге")
    return None


def _advance_open(sig: dict, state_now: dict, store: Store, now: datetime,
                  result: ScanResult, send: bool) -> None:
    """An open signal that has not closed: record the fill, and keep the stop current.

    Three things were missing here, and all three were visible on live trades:

    1. The fill was never saved. A trade whose stop had not yet moved stayed at
       "READY" forever - the dashboard said "готовий" while the position was open -
       and Telegram never announced "вхід виконано" (signal 20261125-A8E4).
    2. Break-even was silent. The stop jumped to entry at +0.75R and nothing was sent,
       so the owner had to move it by hand without being told (same signal: the stop
       went 1.09463 -> 1.09385 and no message arrived).
    3. The trailing message carried a raw status code and no plan, because it passed
       only "resolution" while format_update reads "status".
    """
    patch: dict = {}
    stop_now = round(state_now["stop"], 5)
    if state_now["moved"] and sig.get("trail_stop") != stop_now:
        patch["trail_stop"] = stop_now
    best = round(state_now["best_r"], 2)
    if sig.get("best_r") != best:
        patch["best_r"] = best

    filled = state_now.get("filled")
    is_new_fill = bool(filled) and sig.get("status") == "READY"

    if is_new_fill:
        patch["status"] = "ACTIVE"
        patch["filled_at"] = datetime.fromtimestamp(filled["ts"], tz=timezone.utc).isoformat()
    if not patch:
        return
    patch["updated_at"] = now.isoformat()
    updated = {**sig, **patch}
    store.save_signal(updated)
    result.updates.append(sig["id"])

    if not (send and telegram_ready(store)):
        return

    if is_new_fill:
        # The one message the owner acts on before anything else: the trade exists.
        # It shows the stop the trade was opened with, not one already moved on the
        # same bar - the card would read "стоп 1.09385" while the text says "Стоп
        # 1.09463", which is two different numbers for one trade.
        outcome, err = _send_once(
            store, f"fill:{sig['id']}", "signal_update",
            notify.format_update({**updated, "trail_stop": updated["stop"]},
                                 notify.fill_note(updated)),
            sig["id"],
        )
        _tally(result, outcome, err)
        # No early return: a fast move can fill the trade and reach break-even inside
        # the same scan. Returning here swallowed the break-even instruction - the
        # stop was saved at entry and the owner was never told to move it.
        if "trail_stop" not in patch:
            return
    old_stop = sig.get("trail_stop") or sig["stop"]
    step_pips = abs(patch["trail_stop"] - float(old_stop)) / PIP
    # Break-even is the exception to the distance filter: the owner must move the
    # stop even if the step looks small, because the trade stops losing money there.
    kind = state_now["kind"]
    if step_pips < TRAIL_NOTIFY_PIPS and kind != "BE":
        return
    outcome, err = _send_once(
        store,
        f"trail:{sig['id']}:{patch['trail_stop']}",
        "signal_update",
        notify.format_update(
            {**updated, "status": "ACTIVE"},
            notify.stop_note(updated, patch["trail_stop"], kind),
            # The headline must name the action, not repeat "вхід виконано": the trade
            # is still open, and saying the entry just happened on a +4R trailing
            # message is wrong twice over.
            label="➖ беззбиток" if kind == "BE" else "📈 трейлінг стопу",
        ),
        sig["id"],
    )
    _tally(result, outcome, err)


def _tally(result: ScanResult, outcome: str, err: str | None) -> None:
    if outcome == "sent":
        result.telegram_sent += 1
    elif outcome == "skipped":
        result.telegram_skipped += 1
    else:
        result.telegram_errors.append(err or "невідома помилка Telegram")
