import { DEFAULT_RISK, type Env } from "./config";
import * as store from "./db";
import { buildSnapshot } from "./market";
import { evaluate } from "./signals";
import { keyLevels } from "./analysis";
import { newsRisk, sessionInfo, warsawDateTime } from "./time";
import { computeStats, virtualEquity } from "./stats";
import * as tg from "./telegram";
import type { CalendarEvent } from "./types";

export async function realiseCommands(env: Env, chatId: number | string, text: string): Promise<void> {
  const d1 = env.DB;
  const send = (t: string) => tg.sendMessage(tg.telegramConfig(env).token, String(chatId), t);
  const cmd = text.split(/\s+/)[0].toLowerCase().replace(/@.*$/, "");
  const now = Date.now();

  switch (cmd) {
    case "/start": {
      await send(
        [
          "🔒 <b>Приватний EUR/USD термінал</b>",
          "",
          "Команди:",
          "/status — стан системи",
          "/now — поточний аналіз",
          "/bias — напрямок H4/H1",
          "/calendar — найближчі події",
          "/news — останні новини",
          "/session — активна сесія",
          "/setups — активні сценарії",
          "/stats — статистика",
          "/history — історія сигналів",
          "/why — пояснення останнього рішення",
          "/pause, /resume — керування сигналами",
          "",
          "<i>Це аналітика, не наказ брокеру.</i>",
        ].join("\n"),
      );
      return;
    }
    case "/pause": {
      const state = await store.getScanState(d1);
      await store.setScanState(d1, { ...state, paused: true });
      await send("⏸ Сигнали призупинено. Сканування триває, повідомлення про нові входи не надсилаються.");
      return;
    }
    case "/resume": {
      const state = await store.getScanState(d1);
      await store.setScanState(d1, { ...state, paused: false });
      await send("▶️ Сигнали відновлено.");
      return;
    }
    case "/status": {
      const state = await store.getScanState(d1);
      const cfg = tg.telegramConfig(env);
      const logs = await store.listLogs(d1, 5);
      await send(
        [
          "<b>Стан системи</b>",
          `Telegram: ${cfg.configured ? "✅ налаштовано" : "❌ немає токена"}`,
          `Джерело даних: ${env.BIQUOTE_API_KEY ? "✅ BiQuote" : "⚠️ демо (Synthetic)"}`,
          `Валідатор: ${env.MARKETAPI_API_KEY ? "✅ MarketAPI" : "— не налаштований"}`,
          `Сигнали: ${state.paused ? "⏸ пауза" : "▶️ активні"}`,
          `Останній запуск: ${state.lastRunAt ? warsawDateTime(state.lastRunAt) : "—"}`,
          `Останній успіх: ${state.lastOkAt ? warsawDateTime(state.lastOkAt) : "—"}`,
          `Ранів сьогодні: ${state.runsToday}`,
          state.lastError ? `\nОстання помилка: ${state.lastError}` : "",
          logs.length ? `\n<b>Останні логи</b>\n${logs.map((l) => `• ${warsawDateTime(l.at)} ${l.level} ${l.scope}: ${l.message}`).join("\n")}` : "",
        ]
          .filter(Boolean)
          .join("\n"),
      );
      return;
    }
    case "/now":
    case "/bias": {
      const { snapshot } = await buildSnapshot(env, { allowDemo: true }).catch(async (err) => {
        await send(`Немає даних: ${(err as Error).message}`);
        throw err;
      });
      const session = sessionInfo(now);
      const events = await store.listEvents(d1, now - 86_400_000, now + 7 * 86_400_000);
      const news = newsRisk(events, now, 20, 15);
      if (cmd === "/bias") {
        const f = snapshot.frames;
        await send(
          [
            "<b>Bias EUR/USD</b>",
            `H4: <b>${f.H4?.structure ?? "—"}</b> ${f.H4?.lastBos ? `(BOS ${f.H4.lastBos.dir})` : ""}`,
            `H1: <b>${f.H1?.structure ?? "—"}</b> ${f.H1?.lastChoch ? `(CHoCH ${f.H1.lastChoch.dir})` : f.H1?.lastBos ? `(BOS ${f.H1.lastBos.dir})` : ""}`,
            `M15: ${f.M15?.structure ?? "—"} · M5: ${f.M5?.structure ?? "—"}`,
            `ATR M15: ${(snapshot.atr.M15 / 0.0001).toFixed(1)} pips`,
            `Якість даних: ${snapshot.dataQuality}`,
            `Час: ${warsawDateTime(now)}`,
          ].join("\n"),
        );
        return;
      }
      await send(tg.formatMarketNow(snapshot, session, news));
      return;
    }
    case "/session": {
      const s = sessionInfo(now);
      await send(
        [
          "<b>Сесія</b>",
          `Зараз: <b>${s.active}</b>`,
          `Якість: ${s.quality}${s.inKillzone ? ` · ${s.killzoneLabel}` : ""}`,
          `Варшава: ${warsawDateTime(now)}`,
          s.londonOpenInMin !== null ? `Відкриття Лондона через ${s.londonOpenInMin} хв` : "Лондон відкритий",
          s.nyOpenInMin !== null ? `Відкриття NY через ${s.nyOpenInMin} хв` : "NY відкритий",
          s.isWeekend ? "Ринок закритий (вихідні)." : "",
          ...s.notes.map((n) => `• ${n}`),
        ]
          .filter(Boolean)
          .join("\n"),
      );
      return;
    }
    case "/calendar": {
      const events = await store.listEvents(d1, now - 86_400_000, now + 7 * 86_400_000);
      await send(tg.formatCalendar(events, now));
      return;
    }
    case "/news": {
      const events = await store.listEvents(d1, now - 4 * 86_400_000, now + 86_400_000);
      await send(tg.formatNews(events, now));
      return;
    }
    case "/setups": {
      const open = await store.listSignals(d1, { statuses: ["WATCHLIST", "READY", "ACTIVE", "TP1_HIT", "TP2_HIT"], limit: 10 });
      if (open.length === 0) {
        await send("Активних сценаріїв немає. Відсутність сигналу — теж результат.");
        return;
      }
      await send(open.map((s) => tg.formatSignal(s)).join("\n\n———\n\n"));
      return;
    }
    case "/history": {
      const recent = await store.listSignals(d1, { limit: 12 });
      if (recent.length === 0) {
        await send("Історія порожня.");
        return;
      }
      await send(
        ["<b>Останні сигнали</b>", ""]
          .concat(recent.map((s) => `• ${warsawDateTime(s.createdAt)} ${s.side} ${s.grade} ${s.status}${s.rResult !== null ? ` (${s.rResult > 0 ? "+" : ""}${s.rResult}R)` : ""} · <code>${s.id}</code>`))
          .join("\n"),
      );
      return;
    }
    case "/stats": {
      const all = await store.listSignals(d1, { limit: 500 });
      const stats = computeStats(all);
      const risk = await store.getSetting(d1, "risk", DEFAULT_RISK);
      const equity = virtualEquity(all, risk.accountEquity, risk);
      const lastEq = equity.length ? equity[equity.length - 1].equity : risk.accountEquity;
      await send(
        [
          "<b>Статистика (віртуальний облік)</b>",
          `Всього: ${stats.total} · завершено: ${stats.resolved} · відкрито: ${stats.open}`,
          `Win rate: ${stats.winRate !== null ? `${(stats.winRate * 100).toFixed(1)}%` : "—"}`,
          `Expectancy: ${stats.expectancyR !== null ? `${stats.expectancyR}R` : "—"}`,
          `Profit factor: ${stats.profitFactor ?? "—"}`,
          `Сумарно: ${stats.totalR}R · max DD ${stats.maxDrawdownR}R`,
          `Капітал: ${risk.accountEquity} → <b>${lastEq}</b> USD`,
          stats.avgHoldMin !== null ? `Середній час угоди: ${stats.avgHoldMin} хв` : "",
          stats.sampleWarning ? `\n⚠️ ${stats.sampleWarning}` : "",
        ]
          .filter(Boolean)
          .join("\n"),
      );
      return;
    }
    case "/why": {
      const decisions = await store.listDecisions(d1, 1);
      const d = decisions[0];
      if (!d) {
        await send("Ще немає зафіксованих рішень.");
        return;
      }
      await send(
        [
          `<b>Останнє рішення: ${d.verdict}</b>`,
          `${warsawDateTime(d.at)} · сесія ${d.session}`,
          d.snapshotSummary ? `Ціна ${d.snapshotSummary.bid?.toFixed(5) ?? "—"} · spread ${d.snapshotSummary.spread ?? "—"} · H4 ${d.snapshotSummary.h4} / H1 ${d.snapshotSummary.h1}` : "",
          "",
          ...d.details.map((x) => `• ${x}`),
        ]
          .filter(Boolean)
          .join("\n"),
      );
      return;
    }
    default:
      await send("Невідома команда. /start — список команд.");
  }
}

/** Used by the cron endpoint to announce the daily summary and session briefs once. */
export async function maybeSendScheduledBroadcasts(env: Env, now = Date.now()): Promise<string[]> {
  const d1 = env.DB;
  const out: string[] = [];
  const session = sessionInfo(now);
  const state = await store.getScanState(d1);
  const { token, chatId } = tg.telegramConfig(env);
  if (!token || !chatId || state.paused) return out;

  const waw = warsawDateTime(now);
  const [y, m, d] = waw.split(" ")[0].split("-");
  const hhmm = waw.split(" ")[1];
  const dateKey = `${y}${m}${d}`;

  const sendOnce = async (key: string, kind: string, text: string) => {
    const claimed = await store.claimMessage(d1, key, kind);
    if (!claimed) return;
    const res = await tg.sendMessage(token, chatId, text);
    if (!res.ok) {
      await d1.prepare(`DELETE FROM messages WHERE key = ?`).bind(key).run();
      return;
    }
    out.push(key);
  };

  const { snapshot } = await buildSnapshot(env, { allowDemo: true });
  const events = await store.listEvents(d1, now - 86_400_000, now + 2 * 86_400_000);
  const news = newsRisk(events, now, 20, 15);

  // Morning brief at first scan after 08:45 Warsaw.
  if (hhmm >= "08:45" && hhmm < "10:30") {
    await sendOnce(`brief:morning:${dateKey}`, "brief", tg.formatBrief(snapshot, session, news, events, "Ранковий огляд перед Лондоном"));
  }
  // New York brief at first scan after 14:15 Warsaw.
  if (hhmm >= "14:15" && hhmm < "15:45") {
    await sendOnce(`brief:ny:${dateKey}`, "brief", tg.formatBrief(snapshot, session, news, events, "Огляд перед Нью-Йорком"));
  }
  // Daily summary at first scan after 22:05 Warsaw.
  if (hhmm >= "22:05") {
    const risk = await store.getSetting(d1, "risk", DEFAULT_RISK);
    const all = await store.listSignals(d1, { limit: 300 });
    const dayStart = now - (24 * 3_600_000);
    const todays = all.filter((s) => s.createdAt >= dayStart);
    const open = all.filter((s) => s.resolvedAt === null);
    const equity = virtualEquity(all, risk.accountEquity, risk);
    await sendOnce(
      `daily:${dateKey}`,
      "daily",
      tg.formatDailyReport({
        day: `${y}-${m}-${d}`,
        signals: todays,
        open,
        equity: equity.length ? equity[equity.length - 1].equity : risk.accountEquity,
        accountEquity: risk.accountEquity,
        riskPercent: risk.riskPercent,
        rejected: await store.listDecisions(d1, 200).then((ds) => ds.filter((x) => x.verdict !== "SIGNAL")),
      }),
    );
  }

  return out;
}
