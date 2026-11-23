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
    "🔒 <b>EUR/USD термінал</b>",
    "<i>Кілька команд — і весь стан ринку з телефона.</i>",
    "",
    "<b>Швидко глянути</b>",
    "/now — ціна, структура, сесія, новини",
    "/bias — напрямок H4 і H1",
    "/session — котра сесія зараз",
    "",
    "<b>Сценарії й результат</b>",
    "/setups — відкриті сценарії",
    "/stats — статистика і відсоток виграшів",
    "/history — останні сигнали",
    "/why — чому останнє рішення таке",
    "",
    "<b>Новини</b>",
    "/calendar — що буде попереду",
    "/news — що вже вийшло",
    "",
    "<b>Керування</b>",
    "/status — стан системи",
    "/pause — зупинити нові сигнали",
    "/resume — відновити",
    "/help — цей список",
    "",
    "<i>Аналітика, не наказ брокеру.</i>",
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

            def note(tf: str) -> str:
                fr = f[tf]
                if fr.choch:
                    return f" · злам ({fr.choch['dir']})"
                if fr.bos:
                    return f" · пробій {fr.bos['dir']}"
                return ""

            trend = "висхідний" if f["H4"].structure == "bull" else "низхідний" if f["H4"].structure == "bear" else "боковий"
            return "\n".join([
                f"<b>Напрямок EUR/USD: {trend}</b>",
                "",
                f"H4: <b>{f['H4'].structure}</b>{note('H4')}",
                f"H1: <b>{f['H1'].structure}</b>{note('H1')}",
                f"M15: {f['M15'].structure} · M5: {f['M5'].structure}",
                "",
                f"ATR M15 {f['M15'].atr / PIP:.1f} pips · RSI H1 {f['H1'].rsi14:.0f}",
                f"{warsaw_str(now)} за Варшавою",
                "<i>Напрямок — це контекст, не сигнал на вхід.</i>",
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
        status_word = {
            "READY": "🟡 готовий", "ACTIVE": "🔵 в угоді", "WATCHLIST": "👀 спостерігаю",
            "TP1_HIT": "✅ TP1", "TP2_HIT": "✅ TP2", "TP3_HIT": "✅ TP3",
            "SL_HIT": "🛑 стоп", "CANCELLED": "⚪ скасовано", "EXPIRED": "⌛ прострочено",
        }
        lines = ["<b>Останні сигнали</b>", ""]
        for s in recent:
            r = s.get("r_result")
            r_txt = f" · <b>{r:+.2f}R</b>" if r is not None else ""
            icon = status_word.get(str(s.get("status")), str(s.get("status")))
            arrow = "🟢" if s.get("side") == "BUY" else "🔴"
            lines.append(f"{arrow} <b>{_fmt_iso(s['created_at'])}</b> · {s.get('grade','')} · {icon}{r_txt}")
        lines += ["", "<i>/stats — повна статистика</i>"]
        return "\n".join(lines)

    if cmd == "/stats":
        all_signals = store.list_signals(limit=500)
        equity = ENV.risk.account_equity
        for s in sorted([x for x in all_signals if x.get("resolved_at") and x.get("r_result") is not None],
                        key=lambda x: x["resolved_at"]):
            equity += equity * (ENV.risk.risk_percent / 100) * s["r_result"]
        return notify.format_stats(all_signals, equity, ENV.risk.account_equity, ENV.risk.risk_percent)

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
