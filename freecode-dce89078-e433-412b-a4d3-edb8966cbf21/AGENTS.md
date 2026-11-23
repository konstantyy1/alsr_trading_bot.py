# Project notes

freecode reads this file before every change. Add anything it should always remember about this project.

## ALSR EUR/USD terminal

- **Repo:** `konstantyy1/alsr-bot` — public repo contains ONLY `requirements.txt`; no bot source was ever committed. The running Render code is not in Git.
- **Live service:** `https://alsr-bot.onrender.com` (Render free, Werkzeug/Python previously; now Python 3.11 + gunicorn).
- **Bot:** `@ALSR_Konstantyy_Bot`, id `8702666901`.
- **Code lives in `alsr/`** in this workspace. Entry point `alsr/wsgi.py` → `alsr/bot/web.py`.
- **Do not use Flask.** Pure WSGI + stdlib so every route is testable without pip.
- **Data:** Yahoo Finance chart endpoint, free, no key. M5/M15/H1 native, H4 aggregated from H1. ~30–60 s delay, **no bid/ask**, so spread is labelled "припущений" (assumed 1.2 pips). Never claim broker quotes or OANDA parity.
- **News:** free weekly calendar `nfs.faireconomy.media/ff_calendar_thisweek.json` + official ECB/Fed/BLS RSS. The calendar rate-limits with **HTTP 429** when polled too often — cache and cooldown.
- **If the calendar is unreadable, new entries are BLOCKED** (`NewsRisk.level == "UNKNOWN"`). Never treat "no data" as "no news".
- **Sessions use IANA zones** (`Europe/London`, `America/New_York`), display in `Europe/Warsaw`. No static UTC offsets.
- **Score = rule compliance, not win probability.** The message says so explicitly.
- **No broker orders.** Analytics + virtual accounting only, risk 0.35% equity default.
- **Signal stored before Telegram send** so a send failure cannot duplicate it.
- **Telegram token was pasted into chat** → treat as compromised, user must revoke via @BotFather.
- **Sandbox limits:** no `git`, `pip`, `curl` in the Worker shell. Use `compute action=job` for Python/network checks; working dir `/home/ubuntu/project`.
- Run `cd alsr/bot && python3 selftest.py` — must stay at 23/23.
