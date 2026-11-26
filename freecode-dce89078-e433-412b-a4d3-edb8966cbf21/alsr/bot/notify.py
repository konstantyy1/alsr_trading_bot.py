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


def _g(v, default: str = "—") -> str:
    """Short number for a rule description: 1.0 -> "1", 0.75 -> "0.75"."""
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return default


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


def _exit_plan_lines(sig: dict) -> list[str]:
    """The exit rule, written from the signal's own settings.

    Read from the signal instead of hardcoded text, so a signal stored by 2.6.x —
    which has no trailing fields — shows the rule it was actually issued under rather
    than a "—" placeholder it never had.
    """
    be = sig.get("break_even_at_r")
    start = sig.get("trail_start_r")
    mult = sig.get("trail_atr_mult")
    if start is None or mult is None:
        return [f"<b>План виходу:</b> стоп на вхід (беззбиток), коли ціна дійде до "
                f"+{_g(be, '0.75')}R. TP1 закриває позицію."]
    return [
        f"<b>План виходу:</b> коли ціна дійде до +{_g(be, '0.75')}R — стоп на вхід "
        f"(беззбиток, не в мінус). Далі трейлінг {_g(mult, '1')}×ATR з +{_g(start, '1.5')}R.",
        "TP1–TP3 — <b>орієнтири</b>, не команда закривати. Повну позицію тримаємо до стопу або трейлінгу.",
    ]


def _entry_wait_lines(sig: dict) -> list[str]:
    """Say plainly that this is a limit order and whether the entry is still ahead.

    The live case that caused the confusion: the card showed a zone, the price sat
    2.3 pips above it, and "Вхід: 1.09373 – 1.09457" read like "buy somewhere in here
    now". Nothing said the order only fills if price comes BACK down. The reader went
    in immediately at market, the planned limit never filled, and the bot closed a
    signal the owner thought was his open trade.
    """
    started = sig.get("quote_at_creation")
    low, high = sig.get("entry_low"), sig.get("entry_high")
    if started is None or low is None or high is None:
        return ["<i>Це лімітна заявка: вхід відкриється, коли ціна дійде до зони.</i>"]
    started = float(started)
    out = []
    if low <= started <= high:
        out.append("<i>Ціна вже в зоні — вхід можна відкривати зараз.</i>")
    else:
        away = abs(started - (high if started > high else low)) / 0.0001
        where = "вище" if started > high else "нижче"
        edge = high if started > high else low
        out.append(f"<i>⚠️ Це лімітна заявка, а не «купити зараз». Ціна на момент сигналу "
                   f"{_f5(started)} — {away:.1f} pips {where} зони. Вхід відкриється лише "
                   f"тоді, коли ціна повернеться до {_f5(edge)}. Зайшов раніше за цією "
                   f"ціною — це інша угода, і тобі слід сказати про це боту.</i>")
    return out


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
        *(_entry_wait_lines(sig)),
        *(_exit_plan_lines(sig)),
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
        f"Спред: {sig['spread_at_creation']} pips (припущення) · ціна на момент сигналу {_f5(sig['quote_at_creation'])}",
        f"Джерело: {_esc(sig['provider'])} · <code>{sig['id']}</code>",
    ]

    cancel = [*sig.get("cancel_rules", []), *sig.get("invalidations", [])]
    if cancel:
        lines += ["", "<b>Скасовує сценарій</b>", *[f"• {_esc(w)}" for w in cancel]]

    lines += ["", "<i>Аналітика, не наказ брокеру. Виконання вручну.</i>"]
    return "\n".join(lines)


def fill_note(sig: dict) -> str:
    """The message that says the trade now exists, and where the stop goes next.

    Before this, a filled trade was never announced: the card stayed "готовий" in the
    chat while the position was open, so the owner had to work out from the price alone
    whether he was in. This is the one message worth sending immediately.

    Plain text, no markup: format_update escapes the whole note as one string, so any
    tag written here would reach the reader as "&lt;b&gt;".
    """
    side = str(sig.get("side", "")).upper()
    is_buy = side == "BUY"
    entry = _fill_of(sig)
    stop = float(sig["stop"])
    risk_pips = abs(entry - stop) / 0.0001
    be_r = float(sig.get("break_even_at_r") or 0)
    be_level = entry + (1 if is_buy else -1) * be_r * abs(entry - stop) if be_r > 0 else None
    trail_r = sig.get("trail_start_r")
    lines = [
        f"Вхід виконано на {_f5(entry)} — угода відкрита.",
        f"Стоп {_f5(stop)} · ризик {risk_pips:.1f} pips.",
    ]
    if be_level is not None:
        lines.append(f"Коли ціна дійде до {_f5(be_level)} (+{be_r:.2f}R) — "
                     f"переведи стоп на вхід {_f5(entry)} (беззбиток).")
    if trail_r:
        tp = entry + (1 if is_buy else -1) * float(trail_r) * abs(entry - stop)
        lines.append(f"З {_f5(tp)} (+{float(trail_r):.1f}R) стоп рухається за ціною.")
    tp1 = sig.get("tp1")
    if tp1 is not None:
        lines.append(f"TP1 {_f5(tp1)} — орієнтир, угоду не закриває.")
    return "\n".join(lines)

def stop_note(sig: dict, stop_now: float, kind: str) -> str:
    """Tell the owner exactly what to do with the stop, in plain words.

    "Пересунь стоп на 1.09385" is a price, not an instruction: on a SELL that number is
    below the entry, and nothing said it was break-even rather than a loss. The kind is
    known here, so say which of the three cases this is and what it protects.

    Plain text, no markup - see fill_note for why.
    """
    is_buy = str(sig.get("side", "")).upper() == "BUY"
    entry = _fill_of(sig)
    risk = abs(entry - float(sig["stop"]))
    r_now = ((stop_now - entry) if is_buy else (entry - stop_now)) / risk if risk > 0 else 0.0
    best = float(sig.get("best_r") or 0)
    if kind == "BE":
        head = (f"Переведи стоп на беззбиток: {_f5(stop_now)}\n"
                f"Ціна дійшла до +{float(sig.get('break_even_at_r') or 0.75):.2f}R. "
                f"Стоп став на ціну входу — тепер угода не може дати збиток.")
    else:
        # The icon lives in the message headline, so it is not repeated here.
        where = (f"стоп іде за нею на +{r_now:.2f}R"
                 if r_now > 0.001 else "стоп іде за нею в бік прибутку")
        head = (f"Пересунь стоп на {_f5(stop_now)}\n"
                f"Трейлінг: ціна пройшла +{best:.2f}R, {where}.")
    lines = [head]
    # No TP line here on purpose. Since 2.7.0 a target cannot close the trade, and the
    # owner is mid-position being told where to move the stop - repeating "TP1 is only
    # a reference" on every break-even and every trail step was pure noise. It read as
    # if the bot still managed three targets. The signal card keeps one reference line,
    # which is where it belongs.
    lines.append("Стоп рухається тільки в бік прибутку і ніколи назад.")
    return "\n".join(lines)


def _fill_of(sig: dict) -> float:
    """The price the position was opened at, as the lifecycle computes it."""
    if sig.get("fill_price") is not None:
        return float(sig["fill_price"])
    return float(sig["entry_high"] if str(sig.get("side", "")).upper() == "BUY" else sig["entry_low"])


def format_update(sig: dict, note: str, label: str | None = None) -> str:
    """A state change on an open signal, short enough to read as a notification.

    `label` replaces the status headline. A stop move is not a status change - the
    trade is still ACTIVE - so without it every break-even and trailing message
    opened with "вхід виконано", which reads as if the entry had just happened.
    """
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
    # A break-even exit is recorded as SL_HIT because that is where the stop sat, but
    # a red "stop" icon on a zero-result trade reads as a loss. Say what happened.
    if status == "SL_HIT" and str((sig.get("resolution") or {}).get("exit_kind")) == "BE":
        icon = "➖ беззбиток"
    elif status == "SL_HIT" and str((sig.get("resolution") or {}).get("exit_kind")) == "TRAIL":
        icon = "✅ трейлінг закрив у плюс"
    if label:
        icon = label

    # A cancelled signal that never filled is not "cancelled for a reason" - it simply
    # never became a trade. Say that, because "скасовано" reads like a decision.
    res_now = sig.get("resolution") or {}
    if status == "CANCELLED" and (str(res_now.get("exit_kind")) == "NO_ENTRY"
                                  # Signals stored before 2.7.1 carry no exit_kind and
                                  # only this sentence in the note. Recognise them too,
                                  # so the live trade 20261124-1733 reads correctly
                                  # without rewriting its history in the database.
                                  or "Вхід не активовано" in str(res_now.get("note") or "")):
        icon = "⚪ входу не було"

    r_line = ""
    if r is not None and status != "CANCELLED":
        r_line = f"Результат: <b>{r:+.2f}R</b>" if r else "Результат: <b>0.00R</b>"
    elif status == "CANCELLED":
        r_line = "Угода не відкривалась."

    # Where the stop stands now. This is the one number the owner has to act on when
    # a trade is open, so it is shown on every update rather than only at the start.
    res = sig.get("resolution") or {}
    kind = str(res.get("exit_kind") or "")
    stop_now = sig.get("trail_stop")
    closed = status in ("SL_HIT", "CANCELLED", "EXPIRED", "UNDETERMINED", "TP1_HIT", "TP2_HIT", "TP3_HIT")
    plan = []
    if stop_now is not None and not closed:
        # Where the stop is now, and how far it moved. Plain text, not markup: these
        # lines are escaped as a whole, so an inner tag would arrive as "&lt;b&gt;".
        if sig.get("stop") is not None:
            move = abs(float(stop_now) - float(sig["stop"])) / 0.0001
            if move >= 0.05:
                plan.append(f"Стоп зараз: {_f5(stop_now)} "
                            f"({move:.1f} pips від початкового {_f5(sig['stop'])}).")
            else:
                plan.append(f"Стоп зараз: {_f5(stop_now)}.")
    # "Закрив трейлінг" only holds when the trade really is over. While it is open the
    # same exit_kind just means where the stop currently sits.
    if closed:
        if kind == "TRAIL":
            plan.append("Закрив трейлінг — прибуток зафіксовано.")
        elif kind == "BE":
            plan.append("Закрив беззбиток — нульовий результат.")
        elif kind == "NO_ENTRY":
            plan.append("Ціна не зайшла в зону входу — сигнал згорів, нічого не відкривалось.")
    elif status == "ACTIVE" and sig.get("trail_start_r") is not None:
        # Direction-neutral on purpose: for a SELL the stop travels downwards, so any
        # wording built on "above"/"below" would be wrong half the time. Skipped when
        # the note already explains the stop, otherwise the same sentence arrives twice.
        if "стоп" not in note.lower():
            plan.append("Стоп рухається за ціною в бік прибутку і ніколи не назад.")

    lines = [
        f"<b>{_esc(_pair(sig['symbol']))} {side}</b>",
        f"{icon}",
        r_line,
        "",
        _esc(note),
        *[f"<i>{_esc(p)}</i>" for p in plan],
        "",
        f"<sub>{warsaw_clock(_dt(sig['updated_at'] or sig['created_at']))} · <code>{sig['id']}</code></sub>",
    ]
    return "\n".join([line for line in lines if line])


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
    final = [s for s in signals if s.get("resolved_at") and s.get("r_result") is not None
             and s.get("status") != "CANCELLED"]
    cancelled = [s for s in signals if s.get("status") == "CANCELLED"]
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
        f"Завершено: <b>{len(final)}</b> · відкрито: <b>{len(open_now)}</b>"
        + (f" · скасовано до входу: {len(cancelled)}" if cancelled else ""),
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


def format_daily(day: str, signals: list[dict], equity: float, start_equity: float,
                 risk_percent: float, cancelled: int = 0) -> str:
    final = [s for s in signals if s.get("resolved_at")]
    wins = [s for s in final if (s.get("r_result") or 0) > 0]
    losses = [s for s in final if (s.get("r_result") or 0) < 0]
    total_r = sum(s.get("r_result") or 0 for s in final)
    lines = [
        f"<b>Підсумок дня {day}</b>",
        "",
        f"Завершено: {len(final)} · TP: {len(wins)} · SL: {len(losses)}",
        f"Сумарний результат: <b>{'+' if total_r > 0 else ''}{total_r:.2f}R</b>",
        f"Капітал: {start_equity} → <b>{equity:.2f} USD</b> (ризик {risk_percent}% на угоду)",
        "",
        ("<b>Угоди</b>\n" + "\n".join(f"• {s['side']} {s['grade']} → {s['status']} ({s.get('r_result') or 0:+.2f}R)" for s in final))
        if final else "Завершених угод сьогодні немає.",
    ]
    # A setup the price never reached is neither a win nor a loss, but leaving it out
    # silently would make a busy day look empty. One line, no R value, no percentage.
    if cancelled:
        lines += ["", f"Без входу: {cancelled} — ціна не зайшла в зону, угода не відкривалась."]
    lines += ["", "<i>Віртуальний облік. Реальні накази не надсилались.</i>"]
    return "\n".join(lines)


def _dt(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
