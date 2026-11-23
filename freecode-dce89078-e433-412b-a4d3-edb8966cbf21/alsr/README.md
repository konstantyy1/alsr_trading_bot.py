# ALSR — приватний EUR/USD термінал

Аналітика EUR/USD для інтрадей-торгівлі з доставкою в Telegram і приватним
вебдашбордом. Безкоштовний стек: Render free + Yahoo Finance + відкритий
економічний календар.

**Це не радник і не наказ брокеру.** Реальні ордери не надсилаються.

## Швидкий старт

```
pip install -r requirements.txt
gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
```

Локально без gunicorn:

```
cd bot && SCAN_TOKEN=test DASHBOARD_TOKEN=test python3 web.py
```

## Змінні середовища

Обовʼязкові: `TELEGRAM_BOT_TOKEN`, `DASHBOARD_TOKEN`, `SCAN_TOKEN`.
Рекомендовані: `TELEGRAM_CHAT_ID`, `DATABASE_URL`, `TELEGRAM_WEBHOOK_SECRET`.
Повний список — у `.env.example`.

## Перевірка

```
cd bot && python3 selftest.py     # 23/23
curl https://<service>/health
curl -H "Authorization: Bearer <DASHBOARD_TOKEN>" https://<service>/api/selftest
```

## Документація

| Файл | Про що |
|---|---|
| `RUNBOOK.md` | деплой, cron-job.org, типові проблеми |
| `ARCHITECTURE.md` | як влаштовано |
| `DECISIONS.md` | чому саме так |
| `PROJECT_STATE.md` | що працює, а що ні |
| `TEST_REPORT.md` | результати перевірок |
| `CHANGELOG.md` | історія змін |

## Обмеження, про які треба знати

- Джерело ціни безкоштовне: затримка ~30–60 с, **немає bid/ask**. Спред у
  повідомленні позначений як припущений (1.2 pips), не як брокерський.
- Score — відповідність правилам, **не ймовірність прибутку**.
- Без `DATABASE_URL` історія не переживе рестарт Render.
