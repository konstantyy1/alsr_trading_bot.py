"""WSGI application: JSON API, dashboard and the Telegram webhook.

Deliberately built on the standard library only (no Flask, no Jinja). Gunicorn serves
this as `app:application` on any WSGI host, including the existing Render service, and
every route can be exercised locally without installing anything.

Security model:
  * The whole terminal is private. The dashboard and API are protected by a single
    access token (DASHBOARD_TOKEN, falling back to SCAN_TOKEN) sent as a cookie or a
    `?token=` query once, then stored in a session cookie.
  * /api/scan is the scheduler endpoint and additionally accepts a Bearer token, so
    cron-job.org can call it without a browser session.
  * /telegram/<secret> answers Telegram only when X-Telegram-Bot-Api-Secret-Token
    matches TELEGRAM_WEBHOOK_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.parse
import warnings
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from wsgiref.simple_server import make_server

from config import ENV, bind_chat, chat_id, telegram_ready
from store import Store

import api
import notify
from selftest import run_all

BASE_DIR = Path(__file__).resolve().parent
COOKIE_NAME = "alsr_access"

_STORE = Store(ENV.database_url)
_STORE_LOCK = threading.Lock()
_SELF_TEST_CACHE: dict = {"at": 0.0, "report": None}
_SCANNING = threading.Lock()


def get_store() -> Store:
    return _STORE


# --------------------------------------------------------------------------- auth
def _wire_token() -> str:
    return ENV.dashboard_token or ENV.scan_token


def _parse_cookies(environ) -> dict:
    raw = environ.get("HTTP_COOKIE", "")
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001
        return {}
    return {k: v.value for k, v in jar.items()}


def _secure_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode(), b.encode())


def _bearer(environ) -> str:
    header = environ.get("HTTP_AUTHORIZATION", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _authorized(environ, query: dict) -> bool:
    expected = _wire_token()
    if not expected:
        return False
    candidates = [
        _bearer(environ),
        (query.get("token") or [""])[0],
        _parse_cookies(environ).get(COOKIE_NAME, ""),
        environ.get("HTTP_X_SCAN_TOKEN", ""),
    ]
    return any(_secure_eq(c, expected) for c in candidates)


# --------------------------------------------------------------------------- helpers
def _json(environ, start_response, status_code: int, payload, extra_headers=None):
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    headers = [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
        ("Cache-Control", "no-store"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(f"{status_code} OK" if status_code == 200 else f"{status_code} ERROR", headers)
    return [body]


def _html(start_response, status_code: int, body: str, extra_headers=None):
    data = body.encode("utf-8")
    headers = [
        ("Content-Type", "text/html; charset=utf-8"),
        ("Content-Length", str(len(data))),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(f"{status_code} OK" if status_code == 200 else f"{status_code} ERROR", headers)
    return [data]


def _read_body(environ) -> bytes:
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except (TypeError, ValueError):
        length = 0
    return environ["wsgi.input"].read(length) if length else b""


def _self_test(force: bool = False) -> dict:
    now = time.time()
    if not force and _SELF_TEST_CACHE["report"] and now - _SELF_TEST_CACHE["at"] < 600:
        return _SELF_TEST_CACHE["report"]
    report = run_all()
    _SELF_TEST_CACHE.update({"at": now, "report": report})
    return report


# --------------------------------------------------------------------------- routes
def route_health(environ, start_response):
    store = _STORE
    state = store.state()
    ok = bool(state.get("last_ok_at"))
    return _json(environ, start_response, 200, {
        "ok": True,
        "service": "alsr-terminal",
        "version": api.VERSION,
        "time": datetime.now(timezone.utc).isoformat(),
        "last_ok_at": state.get("last_ok_at"),
        "has_run": ok,
    })


def route_root(environ, start_response):
    page = (BASE_DIR / "template.html").read_text(encoding="utf-8")
    # A one-off ?token= lands in a cookie so the URL can then be forgotten.
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    headers = []
    token = (query.get("token") or [""])[0]
    if _secure_eq(token, _wire_token()):
        headers.append((
            "Set-Cookie",
            f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000; Secure",
        ))
    return _html(start_response, 200, page, headers)


def route_locked(environ, start_response):
    body = """<!doctype html><html lang="uk"><meta charset="utf-8">
<title>Приватний термінал</title>
<style>
:root{color-scheme:dark}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0f14;
color:#e6edf3;font:15px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
.card{max-width:440px;padding:32px;border:1px solid #222d3a;border-radius:14px;background:#111823}
h1{font-size:19px;margin:0 0 10px}
p{color:#93a4b8;margin:0 0 18px}
code{background:#0b0f14;border:1px solid #222d3a;border-radius:6px;padding:2px 6px;color:#7fd1a8}
</style>
<div class="card"><h1>Приватний термінал</h1>
<p>Доступ лише за токеном. Відкрий адресу з параметром <code>?token=…</code> один раз —
далі сесія зберігається в cookie.</p>
<p>Якщо це твій перший запуск: перевір, що змінна <code>DASHBOARD_TOKEN</code> (або
<code>SCAN_TOKEN</code>) задана в налаштуваннях сервісу.</p></div></html>"""
    return _html(start_response, 401, body)


def route_status(environ, start_response):
    return _json(environ, start_response, 200, api.status_payload(_STORE))


def route_market(environ, start_response):
    try:
        return _json(environ, start_response, 200, api.market_payload(_STORE))
    except Exception as exc:  # noqa: BLE001
        _STORE.log("error", "api", f"market: {exc}")
        return _json(environ, start_response, 503, {"error": f"Дані недоступні: {exc}"})


def route_signals(environ, start_response):
    return _json(environ, start_response, 200, api.signals_payload(_STORE))


def route_stats(environ, start_response):
    return _json(environ, start_response, 200, api.stats_payload(_STORE))


def route_calendar(environ, start_response):
    return _json(environ, start_response, 200, api.calendar_payload(_STORE, self_heal=True))


def route_decisions(environ, start_response):
    return _json(environ, start_response, 200, api.decisions_payload(_STORE))


def route_selftest(environ, start_response):
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))
    force = (query.get("force") or ["0"])[0] in ("1", "true", "yes")
    return _json(environ, start_response, 200, _self_test(force))


def route_telegram_discover(environ, start_response):
    """Show which chats have messaged the bot, so the user can copy the real chat id."""
    ok_bot, me, err = notify.get_me(ENV.telegram_token)
    if not ok_bot:
        return _json(environ, start_response, 200, {
            "bot_token_ok": False,
            "error": err,
            "hint": "Перевір TELEGRAM_BOT_TOKEN у налаштуваннях сервісу.",
        })
    ok, chats, err2, webhook_blocking = notify.discover_chats(ENV.telegram_token)
    bound = chat_id(_STORE)
    return _json(environ, start_response, 200, {
        "bot_token_ok": True,
        "bot": {"username": me.get("username"), "name": me.get("first_name"), "id": me.get("id")},
        "chats": chats,
        "chat_bound": bound or None,
        "webhook_blocking_updates": webhook_blocking,
        "hint": (
            "Webhook уже встановлено, тому Telegram не віддає старі повідомлення. "
            "Напиши боту /start і натисни «Перевірити» ще раз."
            if webhook_blocking else
            "Напиши боту будь-яке повідомлення, потім натисни «Перевірити» — chat id зʼявиться тут."
        ),
        "error": err2,
    })


def route_telegram_hello(environ, start_response):
    """Send one confirmation message to the bound chat."""
    target = chat_id(_STORE)
    if not telegram_ready(_STORE):
        return _json(environ, start_response, 200, {"sent": False, "error": "Немає TELEGRAM_BOT_TOKEN або привʼязаного чату."})
    text = notify.format_hello(True, target, _STORE.backend, ENV.poll_interval_min)
    ok, err = notify.send(ENV.telegram_token, target, text)
    _STORE.log("info" if ok else "error", "telegram", "hello sent" if ok else f"hello failed: {err}")
    return _json(environ, start_response, 200, {"sent": ok, "error": err, "chat_id": target})


def route_telegram_bind(environ, start_response):
    """Bind (or clear) the delivery chat without editing service settings."""
    try:
        body = json.loads(_read_body(environ) or b"{}")
    except (ValueError, TypeError):
        return _json(environ, start_response, 400, {"error": "Некоректний JSON."})
    value = str(body.get("chat_id") or "").strip()
    if value and not (value.lstrip("-").isdigit()):
        return _json(environ, start_response, 400, {"error": "chat_id має бути числом."})
    bound = bind_chat(_STORE, value)
    _STORE.log("info", "telegram", f"chat bound: {bound}" if bound else "chat unbound")
    return _json(environ, start_response, 200, {"chat_id": bound or None, "telegram_ready": telegram_ready(_STORE)})


def route_scan(environ, start_response):
    if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
        return _json(environ, start_response, 405, {"error": "Use POST"})
    if not _SCANNING.acquire(blocking=False):
        return _json(environ, start_response, 429, {"error": "Скан уже виконується."})
    try:
        from scan import run_scan

        result = run_scan(_STORE, send=True)
        return _json(environ, start_response, 200, {
            "at": result.at.isoformat(),
            "verdict": result.verdict,
            "data": result.status,
            "created": result.created,
            "updates": result.updates,
            "details": result.details[:12],
            "telegram": {
                "sent": result.telegram_sent,
                "skipped": result.telegram_skipped,
                "errors": result.telegram_errors[:3],
            },
            "duration_ms": result.duration_ms,
        })
    finally:
        _SCANNING.release()


def route_telegram(environ, start_response, secret: str) -> list:
    expected = ENV.webhook_secret
    supplied = environ.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "")
    if not expected or not _secure_eq(secret, expected) or not _secure_eq(supplied, expected):
        return _json(environ, start_response, 403, {"ok": False})
    if environ.get("REQUEST_METHOD", "GET").upper() != "POST":
        return _json(environ, start_response, 405, {"ok": False})

    try:
        update = json.loads(_read_body(environ) or b"{}")
    except json.JSONDecodeError:
        return _json(environ, start_response, 400, {"ok": False})

    message = update.get("message") or update.get("edited_message") or {}
    incoming_chat = str((message.get("chat") or {}).get("id", ""))
    text = (message.get("text") or "").strip()
    bound = chat_id(_STORE)

    if not text:
        return _json(environ, start_response, 200, {"ok": True})
    if incoming_chat and bound and not _secure_eq(incoming_chat, bound):
        _STORE.log("warn", "telegram", f"Відхилено команду з чужого чату {incoming_chat}")
        return _json(environ, start_response, 200, {"ok": True})

    from commands import handle_command

    try:
        reply = handle_command(_STORE, text)
    except Exception as exc:  # noqa: BLE001
        reply = f"Помилка обробки команди: {notify._esc(exc)}"
    ok, err = notify.send(ENV.telegram_token, incoming_chat or bound, reply)
    if not ok:
        _STORE.log("error", "telegram", str(err))
    return _json(environ, start_response, 200, {"ok": True})


# --------------------------------------------------------------------------- dispatch
GET_ROUTES = {
    "/health": route_health,
    "/api/status": route_status,
    "/api/market": route_market,
    "/api/signals": route_signals,
    "/api/stats": route_stats,
    "/api/calendar": route_calendar,
    "/api/decisions": route_decisions,
    "/api/selftest": route_selftest,
}

PUBLIC_ROUTES = {"/health"}


def application(environ, start_response):
    path = environ.get("PATH_INFO", "/") or "/"
    method = environ.get("REQUEST_METHOD", "GET").upper()
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""))

    if path.startswith("/telegram/"):
        return route_telegram(environ, start_response, path[len("/telegram/"):])

    if path == "/":
        if _authorized(environ, query):
            return route_root(environ, start_response)
        return route_locked(environ, start_response)

    if path in PUBLIC_ROUTES:
        return GET_ROUTES[path](environ, start_response)

    if path == "/api/scan":
        if not _authorized(environ, query):
            return _json(environ, start_response, 401, {"error": "Невірний або відсутній токен."})
        return route_scan(environ, start_response)

    POST_ROUTES = {
        "/api/telegram/discover": route_telegram_discover,
        "/api/telegram/hello": route_telegram_hello,
        "/api/telegram/bind": route_telegram_bind,
    }
    if path in POST_ROUTES:
        if not _authorized(environ, query):
            return _json(environ, start_response, 401, {"error": "Невірний або відсутній токен."})
        if method != "POST":
            return _json(environ, start_response, 405, {"error": "Use POST"})
        return POST_ROUTES[path](environ, start_response)

    handler = GET_ROUTES.get(path)
    if handler is None:
        return _json(environ, start_response, 404, {"error": "Not found"})

    if method not in ("GET", "HEAD"):
        return _json(environ, start_response, 405, {"error": "Method not allowed"})
    if not _authorized(environ, query):
        return _json(environ, start_response, 401, {"error": "Невірний або відсутній токен."})
    return handler(environ, start_response)


def init_app() -> None:
    """Create the schema once at boot so the first request never pays for it."""
    try:
        _STORE.init()
    except Exception:  # noqa: BLE001
        pass


init_app()


if __name__ == "__main__":
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        port = ENV.port
        print(f"alsr-terminal on http://0.0.0.0:{port} · db={_STORE.backend}")
        make_server("0.0.0.0", port, application).serve_forever()
