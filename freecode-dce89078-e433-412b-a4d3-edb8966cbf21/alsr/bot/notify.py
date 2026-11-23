"""Telegram formatting and delivery.

Every outgoing message is deduplicated by a stable key (signal id + state), so the
bot can never repeat the same notice. Delivery is restricted to one chat id.
"""
from __future__ import annotations

from datetime import datetime

import urllib.error
import urllib.parse
import urllib.request
import json as _json

from markettime import warsaw_clock, warsaw_str

API = "https://api.telegram.org"
MAX_LEN = 3900


def _esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _f5(v) -> str:
    try:
        return f"{float(v):.5f}"
    except (TypeError, ValueError):
        return "—"


def send(token: str, chat_id: str, text: str, *, disable_preview: bool = True) -> tuple[bool, str | None]:
    if not token or not chat_id:
        return False, "Telegram не налаштовано"
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 20] + "\n…"
    payload = _json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview,
    }).encode()
    req = urllib.request.Request(
        f"{API}/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            data = _json.loads(res.read().decode())
        if not data.get("ok"):
            return False, str(data.get("description", "Telegram error"))
        return True, None
    except urllib.error.HTTPError as exc:
        try:
            body = _json.loads(exc.read().decode())
            return False, str(body.get("description", f"HTTP {exc.code}"))
        except Exception:  # noqa: BLE001
            return False, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def api_get(token: str, method: str, timeout: int = 15) -> tuple[bool, dict, str | None]:
    """Call a read-only Bot API method. Never logs or echoes the token."""
    if not token:
        return False, {}, "токен не заданий"
    try:
        with urllib.request.urlopen(f"{API}/bot{token}/{method}", timeout=timeout) as res:
            data = _json.loads(res.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = _json.loads(exc.read().decode())
            return False, body, str(body.get("description", f"HTTP {exc.code}"))
        except Exception:  # noqa: BLE001
            return False, {}, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, {}, str(exc)
    return bool(data.get("ok")), data, None if data.get("ok") else str(data.get("description"))


def get_me(token: str) -> tuple[bool, dict, str | None]:
    ok, data, err = api_get(token, "getMe")
    return ok, (data.get("result") or {}), err


def discover_chats(token: str) -> tuple[bool, list[dict], str | None, bool]:
    """List chats that messaged the bot while no webhook was set.

    Returns (ok, chats, error, webhook_blocking). Telegram only returns updates to
    getUpdates when no webhook is registered, so the flag tells the user what to do.
    """
    ok, info, _ = api_get(token, "getWebhookInfo")
    webhook_url = ((info.get("result") or {}).get("url") or "") if ok else ""

    ok, data, err = api_get(token, "getUpdates")
    if not ok:
        return False, [], err, bool(webhook_url)

    chats: dict[str, dict] = {}
    for update in data.get("result") or []:
        msg = update.get("message") or update.get("edited_message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        chats[str(cid)] = {
            "chat_id": str(cid),
            "type": chat.get("type"),
            "title": chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username") or "",
            "username": chat.get("username"),
        }
    return True, list(chats.values()), None, bool(webhook_url)


def set_webhook(token: str, url: str, secret: str) -> tuple[bool, str | None]:
    if not token or not url:
        return False, "немає токена або URL"
    params = {"url": url, "secret_token": secret} if secret else {"url": url}
    req = urllib.request.Request(
        f"{API}/bot{token}/setWebhook?{urllib.parse.urlencode(params)}",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            data = _json.loads(res.read().decode())
        return bool(data.get("ok")), None if data.get("ok") else str(data)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def format_hello(env_ok: bool, chat_id: str, backend: str, interval_min: int) -> str:
    return "\n".join([
        "✅ <b>Термінал підключено</b>",
        "",
        "Це повідомлення підтверджує, що бот може писати в цей чат.",
        f"Chat ID: <code>{_esc(chat_id)}</code>",
        f"База даних: <b>{_esc(backend)}</b>",
        f"Розклад: кожні {interval_min} хв",
        "",
        "Напиши /start — побачиш список команд.",
        "",
        "<i>Це аналітика, не наказ брокеру.</i>",
    ])


def _pair(symbol: str) -> str:
    """Render a vendor ticker as a human pair: EURUSD=X -> EUR/USD."""
    s = str(symbol or "").strip().upper()
    for suffix in ("=X", "=F", ":CUR"):
        if s.endswith(suffix):
            s = s[: -len(suffix)]
    s = s.replace("_", "/")
    if "/" not in s and len(s) == 6 and s.isalpha():
        s = f"{s[:3]}/{s[3:]}"
    return s


def format_signal(sig: dict) -> str:
    ctx = sig.get("context", {})
    pos = sig.get("position_size", {})
    lines = [
        f"<b>{_esc(_pair(sig['symbol']))} | {sig['side']}</b>",
        "",
        f"Статус: <b>{sig['status']}</b>",
        f"Рейтинг: <b>{sig['grade']}</b>",
        f"Score: <b>{sig['score']}/100</b> <i>(відповідність правилам, не ймовірність)</i>",
        "",
        f"Час: {warsaw_clock(_dt(sig['created_at']))} Europe/Warsaw",
        f"Сесія: {_esc(sig['session'])}",
        f"ID: <code>{sig['id']}</code>",
        "",
        f"<b>Entry zone:</b> {_f5(sig['entry_low'])} – {_f5(sig['entry_high'])}",
        f"<b>Entry trigger:</b> {_f5(sig['trigger'])}",
        f"<b>Stop Loss:</b> {_f5(sig['stop'])} <i>({sig['risk_pips']} pips)</i>",
        f"<b>TP1:</b> {_f5(sig['tp1'])}",
        f"<b>TP2:</b> {_f5(sig['tp2'])}",
        f"<b>TP3:</b> {_f5(sig['tp3']) if sig.get('tp3') else '— (сценарій із двома цілями)'}",
        "",
        f"Risk/Reward: <b>1:{sig['rr']}</b> (до TP1)",
        f"Орієнтовний час угоди: {sig['expected_hold_min']}–{sig['max_hold_min']} хв",
        f"Максимальний час утримання: {sig['max_hold_min']} хв",
        "",
        "<b>Ринковий контекст</b>",
        f"Структура H4: {_esc(ctx.get('h4', '—'))}",
        f"Структура H1: {_esc(ctx.get('h1', '—'))}",
        f"Підтвердження M15: {_esc(ctx.get('m15', '—'))}",
        f"Тригер M5: {_esc(ctx.get('m5', '—'))}",
        f"Ліквідність: {_esc(ctx.get('liquidity', '—'))}",
        f"Ключові рівні: {_esc(ctx.get('levels', '—'))}",
        "",
        f"<b>Новинний ризик:</b> {_esc(sig.get('news_risk', '—'))}",
        f"<b>Spread:</b> {sig['spread_at_creation']} pips",
        f"<b>Джерело ціни:</b> {_esc(sig['provider'])}",
        f"<b>Ціна на момент створення:</b> {_f5(sig['quote_at_creation'])}",
        "",
        "<b>Чому саме цей сигнал:</b>",
        *[f"• {_esc(w)}" for w in sig.get("why", [])],
        "",
        "<b>Що скасує сценарій:</b>",
        *[f"• {_esc(w)}" for w in sig.get("cancel_rules", [])],
        "",
        "<b>За яких умов не відкривати:</b>",
        *[f"• {_esc(w)}" for w in sig.get("invalidations", [])],
        "",
        f"Позиція: <b>{pos.get('lots', 0)}</b> lot · ризик {pos.get('risk_amount', 0)} USD",
        f"<i>{_esc(pos.get('note', ''))}</i>",
        "",
        "<i>Це аналітика, не наказ брокеру. Виконання — вручну.</i>",
    ]
    return "\n".join(lines)


def format_update(sig: dict, note: str) -> str:
    r = sig.get("r_result")
    r_line = f"Результат: <b>{'+' if (r or 0) > 0 else ''}{r}R</b>" if r is not None else ""
    return "\n".join(filter(None, [
        f"<b>Оновлення {_esc(_pair(sig['symbol']))} {sig['side']}</b>",
        f"ID: <code>{sig['id']}</code>",
        f"Статус: <b>{sig['status']}</b>",
        r_line,
        "",
        _esc(note),
    ]))


def format_now(snap, frames, session, news) -> str:
    q = snap.quote
    if q is None:
        head = "<b>DATA UNAVAILABLE</b>"
    else:
        head = f"Ціна: <b>{_f5(q.mid)}</b>\nBid / Ask: {_f5(q.bid)} / {_f5(q.ask)} · spread {q.spread_pips} pips"
    return "\n".join([
        "<b>EUR/USD — стан ринку</b>",
        f"Час: {warsaw_str()} (Europe/Warsaw)",
        f"Сесія: <b>{session.active}</b>{' · ' + session.killzone_label if session.in_killzone else ''} · {session.quality}",
        "",
        head,
        f"Джерело: {_esc(q.provider) if q else '—'}",
        f"Якість даних: <b>{snap.quality.status}</b>",
        "",
        "<b>Структура</b>",
        " · ".join(f"{tf} {frames[tf].structure}" for tf in ("H4", "H1", "M15", "M5")),
        f"ATR M15: {frames['M15'].atr / 0.0001:.1f} pips · RSI H1: {frames['H1'].rsi14:.0f}",
        "",
        f"<b>Новини:</b> {news.level} — {_esc(news.reason)}",
    ])


def format_calendar(events: list[dict], now: datetime, limit: int = 10) -> str:
    upcoming = sorted([e for e in events if e["time_utc"] >= now], key=lambda e: e["time_utc"])[:limit]
    if not upcoming:
        return "Найближчих подій EUR/USD не знайдено."
    lines = ["<b>Найближчі події EUR/USD</b>", ""]
    for e in upcoming:
        mins = int((e["time_utc"] - now).total_seconds() // 60)
        when = f"через {mins} хв" if mins < 120 else f"через {mins / 60:.1f} год"
        badge = "🔴" if e["impact"] == "high" else "🟠" if e["impact"] == "medium" else "⚪"
        lines.append(f"{badge} <b>{_esc(e['title'])}</b>")
        lines.append(f"{warsaw_str(e['time_utc'])} · {when} · {e['currency']} · {_esc(e['source'])}")
        if any([e.get("actual"), e.get("forecast"), e.get("previous")]):
            lines.append(f"actual {e.get('actual') or '—'} / forecast {e.get('forecast') or '—'} / previous {e.get('previous') or '—'}")
        if e.get("url"):
            lines.append(_esc(e["url"]))
        lines.append("")
    return "\n".join(lines)


def format_brief(snap, frames, session, news, events, label: str) -> str:
    q = snap.quote
    high = [e for e in events if e["impact"] == "high" and e["time_utc"] >= snap.generated_at][:4]
    return "\n".join([
        f"<b>{label}</b>",
        f"{warsaw_str(snap.generated_at)} · Europe/Warsaw",
        "",
        f"EUR/USD <b>{_f5(q.mid)}</b> · {snap.quality.status}" if q else "<b>DATA UNAVAILABLE</b>",
        f"H4 {frames['H4'].structure} / H1 {frames['H1'].structure} / M15 {frames['M15'].structure}",
        f"Сесія: {session.active} ({session.quality})",
        f"Новинний ризик: {news.level}",
        "",
        ("<b>High-impact сьогодні:</b>\n" + "\n".join(f"• {warsaw_clock(e['time_utc'])} {_esc(e['title'])}" for e in high))
        if high else "High-impact подій у календарі немає.",
        "",
        "<i>Сигнал надійде лише за підтвердження ціною. Відсутність сигналу — теж результат.</i>",
    ])


def format_daily(day: str, signals: list[dict], equity: float, start_equity: float, risk_percent: float) -> str:
    final = [s for s in signals if s.get("resolved_at")]
    wins = [s for s in final if (s.get("r_result") or 0) > 0]
    losses = [s for s in final if (s.get("r_result") or 0) < 0]
    total_r = sum(s.get("r_result") or 0 for s in final)
    return "\n".join([
        f"<b>Підсумок дня {day}</b>",
        "",
        f"Завершено: {len(final)} · TP: {len(wins)} · SL: {len(losses)}",
        f"Сумарний результат: <b>{'+' if total_r > 0 else ''}{total_r:.2f}R</b>",
        f"Капітал: {start_equity} → <b>{equity:.2f} USD</b> (ризик {risk_percent}% на угоду)",
        "",
        ("<b>Угоди</b>\n" + "\n".join(f"• {s['side']} {s['grade']} → {s['status']} ({s.get('r_result') or 0:+.2f}R)" for s in final))
        if final else "Завершених угод сьогодні немає.",
        "",
        "<i>Віртуальний облік. Реальні накази не надсилались.</i>",
    ])


def _dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
