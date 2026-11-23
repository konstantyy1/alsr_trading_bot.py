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


def api_get(token: str, method: str, timeout: int = 15, query: dict | None = None) -> tuple[bool, dict, str | None]:
    """Call a read-only Bot API method. Never logs or echoes the token."""
    if not token:
        return False, {}, "токен не заданий"
    url = f"{API}/bot{token}/{method}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as res:
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


def poll_updates(token: str, offset: int | None = None, timeout: int = 0) -> tuple[bool, list[dict], int | None, str | None]:
    """Read new messages sent to the bot, without a webhook.

    Telegram only hands updates to getUpdates when no webhook is registered, and
    only once per update. The caller must store the returned offset and pass it
    back, otherwise old commands would be replayed on every scan.
    """
    ok, data, err = api_get(token, "getUpdates", query={"offset": offset, "timeout": timeout, "limit": 20})
    if not ok:
        return False, [], offset, err
    updates = data.get("result") or []
    next_offset = offset
    for update in updates:
        uid = update.get("update_id")
        if isinstance(uid, int) and (next_offset is None or uid + 1 > next_offset):
            next_offset = uid + 1
    return True, updates, next_offset, None


def webhook_state(token: str) -> tuple[str, str | None]:
    """Return (webhook_url, error). A non-empty url blocks getUpdates."""
    ok, info, err = api_get(token, "getWebhookInfo")
    if not ok:
        return "", err
    return ((info.get("result") or {}).get("url") or ""), None


def webhook_info(token: str) -> dict:
    """Raw getWebhookInfo result: url, pending updates and the last delivery error.

    Exposed in the dashboard because a webhook still pointing at an old address is
    the one failure that leaves the bot silent while every other check looks fine:
    Telegram posts the command to the stale URL, gets a 403, and keeps retrying.
    """
    ok, data, _ = api_get(token, "getWebhookInfo")
    return (data.get("result") or {}) if ok else {}


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


def _pips_text(a, b) -> str:
    """Distance between two prices in pips, always with a sign-free magnitude."""
    try:
        return f"{abs(float(a) - float(b)) / 0.0001:.1f}"
    except (TypeError, ValueError):
        return "—"


def _news_badge(raw: str) -> str:
    """Turn the internal news code into a sentence a person reads without decoding.

    The engine stores values like "NONE: чисто" or "BLOCKED: CPI за 12 хв". Sentinel
    words such as NONE never reach the reader, because a code in a trading message
    reads like an error instead of a state.
    """
    text = str(raw or "").strip()
    code, _, reason = text.partition(":")
    code = code.strip().upper()
    reason = reason.strip()
    if code == "NONE" or not code:
        return "✅ Новини: вільно" + (f" — {_esc(reason)}" if reason and reason.lower() != "чисто" else "")
    if code == "UNKNOWN":
        return "❔ Новини: календар недоступний, вхід заблоковано"
    if code == "ELEVATED":
        return f"⚠️ Новини: підвищений ризик — {_esc(reason)}"
    if code == "BLOCKED":
        return f"⛔ Новини: вхід заборонено — {_esc(reason)}"
    return f"⚠️ Новини: {_esc(text)}"


def format_signal(sig: dict) -> str:
    """One signal, readable on a phone in a couple of glances.

    The order is deliberate: what to do, at what price, where the risk is, and where
    it ends. Context and reasoning come after, because they are read once and the
    prices are read every few minutes. Emoji carry the state so the eye can skip
    straight to the row it needs.
    """
    ctx = sig.get("context", {})
    pos = sig.get("position_size", {})
    side = str(sig.get("side", "")).upper()
    arrow = "🟢 BUY" if side == "BUY" else "🔴 SELL"
    status = str(sig.get("status", ""))
    status_icon = {
        "READY": "🟡 готовий",
        "ACTIVE": "🔵 в угоді",
        "TP1_HIT": "✅ TP1 узято",
        "TP2_HIT": "✅ TP2 узято",
        "WATCHLIST": "👀 спостереження",
        "STOPPED": "🛑 стоп",
        "CANCELLED": "⚪ скасовано",
        "EXPIRED": "⌛ прострочено",
    }.get(status, status)
    tp3 = _f5(sig["tp3"]) if sig.get("tp3") else "—"

    lines = [
        f"<b>{_esc(_pair(sig['symbol']))} · {arrow}</b>",
        f"{status_icon} · рейтинг <b>{sig['grade']}</b> · {sig['score']}/100",
        f"{warsaw_clock(_dt(sig['created_at']))} за Варшавою · сесія {_esc(sig['session'])}",
        "",
        "<b>Ціни</b>",
        f"┌ Вхід: <b>{_f5(sig['entry_low'])} – {_f5(sig['entry_high'])}</b>",
        f"├ Тригер: <b>{_f5(sig['trigger'])}</b>",
        f"├ Стоп: <b>{_f5(sig['stop'])}</b>  (−{sig['risk_pips']} pips)",
        f"├ TP1: <b>{_f5(sig['tp1'])}</b>  (+{_pips_text(sig['trigger'], sig['tp1'])} pips)",
        f"├ TP2: <b>{_f5(sig['tp2'])}</b>  (+{_pips_text(sig['trigger'], sig['tp2'])} pips)",
        f"└ TP3: <b>{tp3}</b>",
        "",
        f"<b>Ризик/прибуток 1:{sig['rr']}</b> · тримати {sig['expected_hold_min']}–{sig['max_hold_min']} хв",
        f"План виходу: TP1 — закрити всю позицію. Коли прибуток дійде 0.75R, стоп на вхід (беззбиток).",
        f"Позиція {pos.get('lots', 0)} lot · ризик {pos.get('risk_amount', 0)} USD",
        "",
        "<b>Чому</b> <i>(score — відповідність правилам, не ймовірність)</i>",
        f"{_esc(ctx.get('h4', '—'))} · {_esc(ctx.get('h1', '—'))}",
        f"{_esc(ctx.get('m15', '—'))}",
        f"M5: {_esc(ctx.get('m5', '—'))}",
        f"Ліквідність: {_esc(ctx.get('liquidity', '—'))}",
        f"Рівні: {_esc(ctx.get('levels', '—'))}",
        "",
        *[f"• {_esc(w)}" for w in sig.get("why", [])],
        "",
        f"{_news_badge(sig.get('news_risk', ''))}",
        f"Спред: {sig['spread_at_creation']} pips (припущення) · ціна на вході {_f5(sig['quote_at_creation'])}",
        f"Джерело: {_esc(sig['provider'])} · <code>{sig['id']}</code>",
    ]

    cancel = [*sig.get("cancel_rules", []), *sig.get("invalidations", [])]
    if cancel:
        lines += ["", "<b>Скасовує сценарій</b>", *[f"• {_esc(w)}" for w in cancel]]

    lines += ["", "<i>Аналітика, не наказ брокеру. Виконання вручну.</i>"]
    return "\n".join(lines)


def format_update(sig: dict, note: str) -> str:
    """A state change on an open signal, short enough to read as a notification."""
    r = sig.get("r_result")
    side = "🟢 BUY" if sig.get("side") == "BUY" else "🔴 SELL"
    status = str(sig.get("status", ""))
    icon = {
        "ACTIVE": "🔵 вхід виконано",
        "TP1_HIT": "✅ TP1 взято",
        "TP2_HIT": "✅ TP2 взято",
        "TP3_HIT": "🎯 TP3 взято",
        "SL_HIT": "🛑 стоп",
        "CANCELLED": "⚪ скасовано",
        "EXPIRED": "⌛ прострочено",
        "UNDETERMINED": "❔ результат неясний",
    }.get(status, status)

    r_line = ""
    if r is not None:
        r_line = f"Результат: <b>{r:+.2f}R</b>" if r else "Результат: <b>0.00R</b>"

    lines = [
        f"<b>{_esc(_pair(sig['symbol']))} {side}</b>",
        f"{icon}",
        r_line,
        "",
        _esc(note),
        "",
        f"<sub>{warsaw_clock(_dt(sig['updated_at'] or sig['created_at']))} · <code>{sig['id']}</code></sub>",
    ]
    return "\n".join([line for line in lines if line is not None])


def format_now(snap, frames, session, news) -> str:
    q = snap.quote
    if q is None:
        head = "⛔ Ціна недоступна"
    else:
        head = f"Ціна: <b>{_f5(q.mid)}</b>\nBid / Ask: {_f5(q.bid)} / {_f5(q.ask)} · спред {q.spread_pips} pips"

    quality = {
        "VERIFIED": "✅ дані перевірені",
        "STALE": "⚠️ дані застарілі",
        "INSUFFICIENT": "⚠️ даних замало",
        "UNAVAILABLE": "⛔ даних немає",
    }.get(str(snap.quality.status), str(snap.quality.status))

    def struct(tf: str) -> str:
        f = frames[tf]
        return f"{tf} <b>{f.structure}</b>"

    kill = f" · {session.killzone_label}" if getattr(session, "in_killzone", False) else ""
    lines = [
        "<b>EUR/USD — стан ринку</b>",
        f"{warsaw_str()} за Варшавою",
        f"Сесія: <b>{session.active}</b>{kill} · {session.quality}",
        "",
        head,
        quality,
        "",
        "<b>Структура</b>",
        f"{struct('H4')} · {struct('H1')}",
        f"{struct('M15')} · {struct('M5')}",
        f"ATR M15 {frames['M15'].atr / 0.0001:.1f} pips · RSI H1 {frames['H1'].rsi14:.0f}",
        "",
        _news_badge(news.level + ": " + news.reason),
        f"<i>{_esc(q.provider) if q else '—'}</i>",
    ]
    return "\n".join(lines)


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


def format_brief(snap, frames, session, news, events, label: str, levels=None) -> str:
    """Session summary designed to be read on a phone, without opening the terminal."""
    q = snap.quote
    now = snap.generated_at

    def upcoming(impact: str, limit: int) -> list[dict]:
        return [e for e in events
                if e["impact"] == impact and e["time_utc"] >= now][:limit]

    high = upcoming("high", 4)
    medium = upcoming("medium", 3)

    lines = [
        f"<b>{label}</b>",
        f"{warsaw_str(now)} · Europe/Warsaw",
        "",
        f"EUR/USD <b>{_f5(q.mid)}</b> · {snap.quality.status}" if q else "<b>DATA UNAVAILABLE</b>",
        f"H4 {frames['H4'].structure} / H1 {frames['H1'].structure} / "
        f"M15 {frames['M15'].structure} / M5 {frames['M5'].structure}",
        f"RSI M15 {getattr(frames['M15'], 'rsi14', 0):.0f} · ATR M15 {getattr(frames['M15'], 'atr', 0) / 0.0001:.1f} pips",
        f"Сесія: {session.active} ({session.quality})"
        + (" · killzone" if getattr(session, "in_killzone", False) else ""),
        f"Новинний ризик: {news.level}",
    ]

    if levels:
        pick: dict[str, object] = {}
        for z in levels:
            pick.setdefault(z.kind, z)
        parts = []
        for kind, name, attr in (("DAY_HIGH", "PDH", "high"), ("DAY_LOW", "PDL", "low"),
                                 ("WEEK_HIGH", "Тиж. max", "high"), ("WEEK_LOW", "Тиж. min", "low")):
            z = pick.get(kind)
            if z is not None:
                parts.append(f"{name} <b>{_f5(getattr(z, attr))}</b>")
        if parts:
            lines += ["", "<b>Рівні</b>", " · ".join(parts)]

    if high:
        lines += ["", "<b>High-impact сьогодні</b>"]
        lines += [f"• {warsaw_clock(e['time_utc'])} {_esc(e['title'])} ({e['currency']})" for e in high]
    elif medium:
        lines += ["", "<b>Найближчі події</b>"]
        lines += [f"• {warsaw_clock(e['time_utc'])} {_esc(e['title'])} ({e['currency']}, {e['impact']})"
                  for e in medium]
    else:
        lines += ["", "Подій EUR/USD у календарі немає."]

    lines += [
        "",
        "<i>Сигнал надійде лише за підтвердженням ціною. Відсутність сигналу — теж результат.</i>",
        "<i>Команди: /now /bias /calendar /session /setups /stats /history /why</i>",
    ]
    return "\n".join(lines)


def format_stats(signals: list[dict], equity: float, start_equity: float, risk_percent: float) -> str:
    """The report that answers "does this system actually work?".

    Honest by design: it separates signals still open, shows the sample size, and
    refuses to claim an edge from a handful of trades. Percentages on a small sample
    are misleading, so a warning appears until there is enough data to mean something.
    """
    final = [s for s in signals if s.get("resolved_at") and s.get("r_result") is not None]
    # "Open" means "no result yet". Counting by status would double-count TP hits,
    # which stay listed in the open set while already carrying a result.
    open_now = [s for s in signals if not (s.get("resolved_at") and s.get("r_result") is not None)]
    wins = [s for s in final if s["r_result"] > 0]
    losses = [s for s in final if s["r_result"] < 0]
    flat = [s for s in final if s["r_result"] == 0]
    total_r = sum(s["r_result"] for s in final)
    decided = len(wins) + len(losses)
    win_rate = (len(wins) / decided * 100) if decided else 0.0

    def avg(rows: list[dict]) -> float:
        return sum(r["r_result"] for r in rows) / len(rows) if rows else 0.0

    lines = ["<b>📊 Статистика</b>", ""]

    if not signals:
        lines += [
            "Ще немає жодного сигналу.",
            "",
            "<i>Система мовчить, коли ринок не дає підтвердження. "
            "Це не помилка — це і є її правило.</i>",
        ]
        return "\n".join(lines)

    lines += [
        f"Усього сигналів: <b>{len(signals)}</b>",
        f"Завершено: <b>{len(final)}</b> · відкрито: <b>{len(open_now)}</b>",
        "",
        "<b>Результат</b>",
        f"Сумарно: <b>{total_r:+.2f}R</b>",
        f"Капітал: {start_equity} → <b>{equity:.2f} USD</b>",
        f"Виграшів: <b>{len(wins)}</b> · програшів: <b>{len(losses)}</b>"
        + (f" · без результату: {len(flat)}" if flat else ""),
    ]
    if decided:
        lines.append(f"Відсоток виграшів: <b>{win_rate:.0f}%</b> з {decided} вирішених")
        lines += [
            f"Середній виграш: {avg(wins):+.2f}R",
            f"Середній програш: {avg(losses):+.2f}R",
        ]
        if losses and wins:
            lines.append(f"Очікування на угоду: <b>{total_r / decided:+.2f}R</b>")

    by_grade: dict[str, list[dict]] = {}
    for s in final:
        by_grade.setdefault(str(s.get("grade", "?")), []).append(s)
    if by_grade:
        lines += ["", "<b>За рейтингом</b>"]
        for grade in sorted(by_grade, reverse=True):
            rows = by_grade[grade]
            good = len([r for r in rows if r["r_result"] > 0])
            lines.append(f"{grade}: {len(rows)} угод · {good} у плюс · {sum(r['r_result'] for r in rows):+.2f}R")

    by_session: dict[str, list[dict]] = {}
    for s in final:
        by_session.setdefault(str(s.get("session", "—")), []).append(s)
    if len(by_session) > 1:
        lines += ["", "<b>За сесією</b>"]
        for name, rows in sorted(by_session.items(), key=lambda kv: -sum(r["r_result"] for r in kv[1])):
            lines.append(f"{name}: {len(rows)} · {sum(r['r_result'] for r in rows):+.2f}R")

    hit = len([s for s in final
               if (s.get("resolution") or {}).get("status") in ("TP2_HIT", "TP3_HIT", "TP1_HIT")])
    if final:
        lines.append(f"\nДійшли до цілі: <b>{hit}</b> з {len(final)} · стоп: "
                     f"<b>{len([s for s in final if (s.get('resolution') or {}).get('status') == 'SL_HIT'])}</b>")

    if decided < 30:
        lines += [
            "",
            f"⚠️ <i>Лише {decided} вирішених угод. Для висновків потрібно мінімум 30–50. "
            "Поки це спостереження, а не доведена перевага.</i>",
        ]
    lines += ["", "<i>Віртуальний облік. Реальні накази не надсилались.</i>"]
    return "\n".join(lines)


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
