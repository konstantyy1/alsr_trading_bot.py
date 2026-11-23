"""Telegram command handling. Private: only the configured chat id is served."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import notify
from analysis import analyse, day_week_levels
from config import ENV, PIP, chat_id, telegram_ready
from datafeed import build_snapshot
from lifecycle import positions_summary
from markettime import day_bounds, news_risk, session_info, utcnow, warsaw_str
from store import Store

HELP_TEXT = "\n".join([
    "🔒 <b>Приватний EUR/USD термінал</b>",
    "",
    "/status — стан системи",
    "/now — поточний аналіз",
    "/bias — напрямок H4/H1",
    "/calendar — найближчі події",
    "/news — останні події",
    "/session — активна сесія",
    "/setups — активні сценарії",
    "/stats — статистика",
    "/history — історія сигналів",
    "/why — останнє рішення",
    "/pause, /resume — керування сигналами",
    "",
    "<i>Це аналітика, не наказ брокеру.</i>",
])


def handle_command(store: Store, text: str) -> str:
    cmd = (text or "").strip().split()[0].lower().split("@")[0] if text.strip() else ""
    now = utcnow()

    if cmd in ("/start", "/help"):
        return HELP_TEXT

    if cmd == "/pause":
        store.set_state(paused=True)
        return "⏸ Сигнали призупинено. Сканування й аналіз тривають, нові входи не надсилаються."

    if cmd == "/resume":
        store.set_state(paused=False)
        return "▶️ Сигнали відновлено."

    if cmd == "/status":
        state = store.state()
        logs = store.list_logs(5)
        lines = [
            "<b>Стан системи</b>",
            f"Telegram: {'✅ налаштовано' if telegram_ready(store) else '❌ немає токена або чату'}",
            f"Джерело даних: Yahoo Finance (безкоштовне, без ключа)",
            f"База: {'Postgres' if store.backend == 'postgres' else 'SQLite (локальний фолбек)'}",
            f"Сигнали: {'⏸ пауза' if state.get('paused') else '▶️ активні'}",
            f"Останній запуск: {_fmt_iso(state.get('last_run_at'))}",
            f"Останній успіх: {_fmt_iso(state.get('last_ok_at'))}",
            f"Запусків сьогодні: {state.get('runs_today', 0)}",
        ]
        if state.get("last_error"):
            lines.append(f"\nОстання помилка: {notify._esc(state['last_error'])}")
        if logs:
            lines.append("\n<b>Останні логи</b>")
            lines.extend(f"• {_fmt_iso(l['at'])} {l['level']} {l['scope']}: {notify._esc(l['message'])}" for l in logs)
        return "\n".join(lines)

    if cmd in ("/now", "/bias"):
        snap = build_snapshot()
        frames = {tf: analyse(tf, snap.candles.get(tf, [])) for tf in ("M5", "M15", "H1", "H4")}
        session = session_info(now)
        if cmd == "/bias":
            f = frames
            return "\n".join([
                "<b>Bias EUR/USD</b>",
                f"H4: <b>{f['H4'].structure}</b>" + (f" (BOS {f['H4'].bos['dir']})" if f['H4'].bos else ""),
                f"H1: <b>{f['H1'].structure}</b>" + (f" (CHoCH {f['H1'].choch['dir']})" if f['H1'].choch else f" (BOS {f['H1'].bos['dir']})" if f['H1'].bos else ""),
                f"M15: {f['M15'].structure} · M5: {f['M5'].structure}",
                f"ATR M15: {f['M15'].atr / PIP:.1f} pips",
                f"Якість даних: {snap.quality.status}",
                f"Час: {warsaw_str(now)}",
            ])
        events = store.list_events(now - timedelta(days=1), now + timedelta(days=7))
        risk = news_risk(events, now, ENV.data.news_blackout_before_min, ENV.data.news_blackout_after_min)
        return notify.format_now(snap, frames, session, risk)

    if cmd == "/session":
        s = session_info(now)
        lines = [
            "<b>Сесія</b>",
            f"Зараз: <b>{s.active}</b>",
            f"Якість: {s.quality}" + (f" · {s.killzone_label}" if s.in_killzone else ""),
            f"Варшава: {warsaw_str(now)}",
            f"Відкриття Лондона: {'відкрито' if s.london_open_in_min is None else str(s.london_open_in_min) + ' хв'}",
            f"Відкриття NY: {'відкрито' if s.ny_open_in_min is None else str(s.ny_open_in_min) + ' хв'}",
        ]
        if s.is_weekend:
            lines.append("Ринок закритий (вихідні).")
        lines.extend(f"• {n}" for n in s.notes)
        return "\n".join(lines)

    if cmd == "/calendar":
        events = store.list_events(now - timedelta(days=1), now + timedelta(days=7))
        return notify.format_calendar(events, now)

    if cmd == "/news":
        events = store.list_events(now - timedelta(days=4), now + timedelta(days=1))
        past = sorted([e for e in events if e["time_utc"] <= now], key=lambda e: e["time_utc"], reverse=True)[:10]
        if not past:
            return "Нових подій у перевірених фідах немає."
        lines = ["<b>Останні події EUR/USD</b>", ""]
        for e in past:
            badge = "🔴" if e["impact"] == "high" else "🟠"
            lines.append(f"{badge} <b>{notify._esc(e['title'])}</b>")
            lines.append(f"{warsaw_str(e['time_utc'])} · {notify._esc(e['source'])}")
            if e.get("url"):
                lines.append(notify._esc(e["url"]))
            lines.append("")
        return "\n".join(lines)

    if cmd == "/setups":
        open_signals = store.list_signals(statuses=["WATCHLIST", "READY", "ACTIVE", "TP1_HIT", "TP2_HIT"], limit=10)
        if not open_signals:
            return "Активних сценаріїв немає. Відсутність сигналу — теж результат."
        return "\n\n———\n\n".join(notify.format_signal(s) for s in open_signals)

    if cmd == "/history":
        recent = store.list_signals(limit=12)
        if not recent:
            return "Історія порожня."
        lines = ["<b>Останні сигнали</b>", ""]
        for s in recent:
            r = s.get("r_result")
            r_txt = f" ({r:+.2f}R)" if r is not None else ""
            lines.append(f"• {_fmt_iso(s['created_at'])} {s['side']} {s['grade']} {s['status']}{r_txt} · <code>{s['id']}</code>")
        return "\n".join(lines)

    if cmd == "/stats":
        all_signals = store.list_signals(limit=500)
        summary = positions_summary(all_signals)
        equity = ENV.risk.account_equity
        for s in sorted([x for x in all_signals if x.get("resolved_at") and x.get("r_result") is not None], key=lambda x: x["resolved_at"]):
            equity += equity * (ENV.risk.risk_percent / 100) * s["r_result"]
        lines = [
            "<b>Статистика (віртуальний облік)</b>",
            f"Всього: {len(all_signals)} · завершено: {summary['closed']} · відкрито: {summary['open']}",
            f"Перемог / збитків: {summary['wins']} / {summary['losses']}",
            f"Сумарно: <b>{summary['total_r']:+.2f}R</b>",
            f"Капітал: {ENV.risk.account_equity} → <b>{equity:.2f} USD</b>",
        ]
        if summary["closed"] < 100:
            lines.append(f"\n⚠️ Лише {summary['closed']} завершених угод. Висновки попередні.")
        return "\n".join(lines)

    if cmd == "/why":
        decisions = store.list_decisions(1)
        if not decisions:
            return "Ще немає зафіксованих рішень."
        d = decisions[0]
        lines = [
            f"<b>Останнє рішення: {d['verdict']}</b>",
            f"{_fmt_iso(d['at'])} · сесія {d['session']}",
            "",
        ]
        lines.extend(f"• {notify._esc(x)}" for x in d["details"][:15])
        return "\n".join(lines)

    return "Невідома команда. /start — список команд."


def _fmt_iso(value) -> str:
    if not value:
        return "—"
    try:
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return warsaw_str(dt)
    except (ValueError, TypeError):
        return str(value)
