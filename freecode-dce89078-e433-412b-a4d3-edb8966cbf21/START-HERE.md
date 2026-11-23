# ЗВІДСИ ПОЧИНАЙ

Цей архів містить робочу систему EUR/USD для Render.

## Що завантажувати в GitHub
Тільки папку **`alsr`**. Решта папок у цьому архіві — старі чернетки, їх можна ігнорувати.

## Порядок
1. Розпакуй архів.
2. Відкрий свій репозиторій на GitHub.
3. Натисни **Add file → Upload files**.
4. Перетягни папку `alsr` у вікно браузера.
5. Натисни **Commit changes**.

## Налаштування Render
- **Root Directory:** `alsr`
- **Build Command:** `pip install --no-cache-dir -r requirements.txt`
- **Start Command:** `gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --log-file -`
- **Health Check Path:** `/health`

## Змінні середовища (Environment)
`TELEGRAM_BOT_TOKEN`, `DASHBOARD_TOKEN`, `SCAN_TOKEN` — обовʼязково.
`TELEGRAM_CHAT_ID`, `DATABASE_URL` — після першого входу в дашборд.

Деталі — у файлі `alsr/RUNBOOK.md`.
