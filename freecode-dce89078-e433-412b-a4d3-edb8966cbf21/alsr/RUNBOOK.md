# Інструкція з експлуатації

## 1. Деплой на Render

Сервіс: `alsr-bot` (існуючий), root directory: `alsr`.

**Build command**
```
pip install --no-cache-dir -r requirements.txt
```

**Start command**
```
gunicorn wsgi:application --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 --log-file -
```

Важливо: `--workers 1`. Кілька воркерів означали б кілька незалежних сканів;
для безкоштовного тарифу й одного користувача одного воркера з потоками достатньо.

**Health check path:** `/health`

## 2. Змінні середовища (Render → Environment)

Обовʼязкові:
| Назва | Що це |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен від @BotFather |
| `TELEGRAM_CHAT_ID` | твій chat id |
| `DASHBOARD_TOKEN` | довгий випадковий рядок для входу в дашборд |
| `SCAN_TOKEN` | довгий випадковий рядок для cron-job.org |

Рекомендовані:
| Назва | Що це |
|---|---|
| `DATABASE_URL` | `postgres://…` (Render Postgres free / Neon / Supabase) |
| `TELEGRAM_WEBHOOK_SECRET` | довгий випадковий рядок |

Решта має безпечні значення за замовчуванням — див. `.env.example`.

Згенерувати випадковий рядок: `openssl rand -hex 24`.

## 3. Знайти chat ID
1. Відкрий дашборд: `https://alsr-bot.onrender.com/?token=<DASHBOARD_TOKEN>`.
2. Напиши боту `@ALSR_Konstantyy_Bot` будь-яке повідомлення (наприклад `/start`).
3. У панелі «Підключення» натисни **«Знайти chat ID»** — зʼявиться номер.
4. Впиши його в `TELEGRAM_CHAT_ID` на Render і перезапусти сервіс.
5. Натисни **«Надіслати тест у Telegram»** — має прийти підтвердження.

Приватний чат має додатний id, група — відʼємний. Якщо в боті вже є webhook,
Telegram не віддає старі повідомлення; напиши боту ще раз після перевірки.

## 4. Розклад кожні 5 хвилин (cron-job.org)
1. Створи безкоштовний акаунт на cron-job.org.
2. Новий cron job:
   - **URL:** `https://alsr-bot.onrender.com/api/scan`
   - **Method:** `POST`
   - **Schedule:** кожні 5 хвилин
   - **Header:** `Authorization: Bearer <SCAN_TOKEN>`
3. Увімкни job і перевір історію виконання: очікується HTTP 200 і JSON із `verdict`.

Без розкладу система нічого не робитиме сама — Render free не тримає фоновий процес.

## 5. Перевірка після деплою
```bash
curl https://alsr-bot.onrender.com/health
curl -X POST -H "Authorization: Bearer <SCAN_TOKEN>" https://alsr-bot.onrender.com/api/scan
curl -H "Authorization: Bearer <DASHBOARD_TOKEN>" https://alsr-bot.onrender.com/api/selftest
```
`/api/selftest` має показати `21/21`.

## 6. Команди бота
`/start`, `/status`, `/now`, `/bias`, `/calendar`, `/news`, `/session`, `/setups`,
`/stats`, `/history`, `/why`, `/pause`, `/resume`.

Webhook (необовʼязково, лише якщо хочеш керувати з Telegram):
```
https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://alsr-bot.onrender.com/telegram/<TELEGRAM_WEBHOOK_SECRET>&secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

## 7. Типові проблеми
| Симптом | Причина | Дія |
|---|---|---|
| `/health` 404 | деплоїться старий код | перевір Start command і root directory |
| `/api/scan` 401 | невірний `SCAN_TOKEN` у cron-job.org | звір заголовок `Authorization` |
| «Знайти chat ID» порожньо | бот не отримував повідомлень | напиши боту `/start` |
| Дані `UNVERIFIED` | котирування старше 10 хв | ринок закритий або збій джерела |
| `sqlite (тимчасове)` на дашборді | немає `DATABASE_URL` | додай Postgres |
| Сигналів немає годинами | новинний фільтр або немає сетапу | це нормально, перевір `/why` |

## 8. Відкат
На Render відкрий Deploys → вибери попередній успішний деплой → **Rollback**.
Render зберігає власні збірки, тому старий код можна повернути без Git.
