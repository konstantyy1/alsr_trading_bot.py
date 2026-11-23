# Звіт про перевірки

Дата: 2026-11-23 · версія 2.0.0

## Детермінована самоперевірка — 21/21

```
PASS DST offsets (London/NY) — London BST/GMT, NY EDT/EST
PASS Europe/Warsaw display — +1h winter, +2h summer
PASS DST boundary hour — 00:30 GMT → 02:30 BST
PASS Market closure — Sat closed, Fri 20:00 open, Fri 23:00 closed, Sun 21:00 closed, Sun 23:00 open
PASS Session windows — 09:00 London/PRIME, 14:00 Overlap, 03:00 Asia
PASS News blackout gate — blocked_before / elevated / blocked_after / clear / other_currency / low_impact
PASS Lifecycle: SL
PASS Lifecycle: TP1
PASS Lifecycle: ambiguous candle → SL (conservative)
PASS Lifecycle: cancel when entry not triggered
PASS Lifecycle: max hold expiry
PASS Lifecycle: ignore open bar
PASS H4 aggregation — 2 H4 bars from 8 hourly
PASS Candle validation — bad and duplicate dropped
PASS Weekend gap tolerance — 50h closure not a data gap
PASS Previous day = Friday on Monday — PDH 1.1022 == Friday high
PASS Signal template completeness — all sections present
PASS No probability claims
PASS Position sizing — 0.23 lots ⇒ 35.00 USD risked
PASS Store dedupe and state
PASS Score is not a win rate
```

## Перевірки на живому ринку

| Перевірка | Результат |
|---|---|
| EUR/USD котирування | 1.0994, статус `VERIFIED`, вік ~30–60 с |
| Свічки | M5 1237, M15/H1/H4 1500 (H4 агреговано) |
| Структура | H4 range, H1 bull, M15 bull, M5 bear |
| ATR M15 | 4.8 pips |
| Календар EUR/USD | 46 рядків, 42 майбутні, реальні forecast/previous |
| Офіційні RSS | ECB 6, Fed 16, BLS 1 релевантних |
| Повний скан | `verdict: BLOCKED` — «За 1 хв до релізу Flash Manufacturing PMI» |
| Тривалість скану | ~1.7–2.1 с |

## Перевірки HTTP-маршрутів

| Запит | Очікування | Результат |
|---|---|---|
| `GET /` без токена | 401 | ✅ 401 |
| `GET /?token=…` | 200 + cookie | ✅ 200 + `Set-Cookie` |
| `GET /health` | 200 публічно | ✅ 200 |
| `GET /api/status` без токена | 401 | ✅ 401 |
| `GET /api/market` | 200 | ✅ 200, реальні дані |
| `GET /api/selftest` | 21/21 | ✅ |
| `POST /api/scan` | 200 + вердикт | ✅ 200 |
| `POST /api/telegram/discover` | 200 | ✅ 200, bot `@ALSR_Konstantyy_Bot` |
| `POST /api/telegram/discover` без токена | 401 | ✅ 401 |
| `POST /telegram/<невірний secret>` | 403 | ✅ 403 |
| `GET /nope` | 404 | ✅ 404 |

## Перевірки Telegram

| Перевірка | Результат |
|---|---|
| `getMe` з наданим токеном | ✅ токен валідний, бот відповідає (username збігається) |
| `getWebhookInfo` | ✅ webhook не встановлено |
| `getUpdates` | ✅ виклик працює, але **0 повідомлень** — chat ID невідомий |
| Фактична відправка повідомлення | ❌ **не виконано** — без chat ID |
| Отримання команди через webhook | ❌ **не виконано** |

## Не перевірено
- **Postgres.** Тестувалась лише SQLite. Запити написані під обидві бази,
  але на реальному Postgres не запускались.
- **Доставка в Telegram.** Код і шаблон готові, сам факт доставки не підтверджено.
- **Деплой на Render.** Код у Git ще не закомічений, тому на сервісі працює
  попередня версія.
- **Розклад cron-job.org.** Не налаштований.
- **Довгострокова статистика.** Порожня — система щойно запущена. Win rate
  показуватиметься лише після реальних завершених угод.

## Застереження
Джерело даних безкоштовне, з затримкою 30–60 с і без bid/ask. Спред у
повідомленні — припущення 1.2 pips, не брокерське котирування. Score — це
відповідність правилам, а не ймовірність прибутку. Жодного наказу брокеру
система не надсилає.
