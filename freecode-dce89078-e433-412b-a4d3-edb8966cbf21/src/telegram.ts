import { warsawClock, warsawDateTime } from "./time";
import type { CalendarEvent, Decision, MarketSnapshot, NewsRisk, SessionInfo, Signal } from "./types";

const API = "https://api.telegram.org";

export type TgResult = { ok: boolean; error?: string; skipped?: boolean };

export async function sendMessage(token: string | undefined, chatId: string | undefined, text: string, opts: { disablePreview?: boolean } = {}): Promise<TgResult> {
  if (!token || !chatId) return { ok: false, skipped: true, error: "Telegram не налаштовано" };
  if (text.length > 4000) text = text.slice(0, 3950) + "\n…";
  try {
    const res = await fetch(`${API}/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text,
        parse_mode: "HTML",
        disable_web_page_preview: opts.disablePreview ?? true,
      }),
    });
    const data = (await res.json()) as any;
    if (!data.ok) return { ok: false, error: String(data.description ?? "Telegram error") };
    return { ok: true };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const f5 = (n: number): string => n.toFixed(5);

export function formatSignal(s: Signal): string {
  const ready = s.status === "READY" || s.status === "ACTIVE";
  const lines = [
    `<b>${s.symbol.replace("_", "/")} | ${s.side}</b>`,
    "",
    `Статус: <b>${s.status}</b>`,
    `Рейтинг: <b>${s.grade}</b>`,
    `Score: <b>${s.score}/100</b> <i>(відповідність правилам, не ймовірність)</i>`,
    "",
    `Час: ${warsawClock(s.createdAt)} Europe/Warsaw`,
    `Сесія: ${esc(s.session)}`,
    `ID: <code>${s.id}</code>`,
    "",
    `<b>Entry zone:</b> ${f5(s.entryLow)} – ${f5(s.entryHigh)}`,
    `<b>Entry trigger:</b> ${f5(s.trigger)}`,
    `<b>Stop Loss:</b> ${f5(s.stop)} <i>(${s.riskPips} pips)</i>`,
    `<b>TP1:</b> ${f5(s.tp1)}`,
    `<b>TP2:</b> ${f5(s.tp2)}`,
    `<b>TP3:</b> ${s.tp3 !== null ? f5(s.tp3) : "— (сценарій із двома цілями)"}`,
    "",
    `Risk/Reward: <b>1:${s.rr}</b> (до TP1)`,
    `Орієнтовний час угоди: ${s.expectedHoldMin}–${s.maxHoldMin} хв`,
    `Максимальний час утримання: ${s.maxHoldMin} хв`,
    "",
    `<b>Ринковий контекст</b>`,
    `Структура H4: ${esc(s.context.h4)}`,
    `Структура H1: ${esc(s.context.h1)}`,
    `Підтвердження M15: ${esc(s.context.m15)}`,
    `Тригер M5: ${esc(s.context.m5)}`,
    `Ліквідність: ${esc(s.context.liquidity)}`,
    `Ключові рівні: ${esc(s.context.levels)}`,
    "",
    `<b>Новинний ризик:</b> ${esc(s.newsRisk)}`,
    `<b>Spread:</b> ${s.spreadAtCreation} pips`,
    `<b>Джерело ціни:</b> ${esc(s.provider)}`,
    `<b>Ціна на момент створення:</b> ${f5(s.quoteAtCreation)}`,
    "",
    `<b>Чому саме цей сигнал:</b>`,
    ...s.why.map((w) => `• ${esc(w)}`),
    "",
    `<b>Що скасує сценарій:</b>`,
    ...s.cancelRules.map((w) => `• ${esc(w)}`),
    "",
    `<b>За яких умов не відкривати:</b>`,
    ...s.invalidations.map((w) => `• ${esc(w)}`),
    "",
    `Розмір позиції (${s.positionSize.note}): <b>${s.positionSize.lots}</b> lot, ризик ${s.positionSize.riskAmount} USD`,
    "",
    `<i>Це аналітика, не наказ брокеру. Виконання — вручну.</i>`,
  ];
  if (!ready) lines.splice(3, 0, `<i>Спостереження: ${s.status === "WATCHLIST" ? "умови ще не підтверджені" : s.status}</i>`);
  return lines.join("\n");
}

export function formatStatusUpdate(s: Signal, note: string): string {
  return [
    `<b>Оновлення сценарію ${s.symbol.replace("_", "/")} ${s.side}</b>`,
    `ID: <code>${s.id}</code>`,
    `Статус: <b>${s.status}</b>`,
    s.rResult !== null ? `Результат: <b>${s.rResult > 0 ? "+" : ""}${s.rResult}R</b>` : "",
    "",
    esc(note),
  ]
    .filter(Boolean)
    .join("\n");
}

export function formatMarketNow(s: MarketSnapshot, session: SessionInfo, news: NewsRisk): string {
  const q = s.quote;
  const frames = s.frames;
  const rows = [
    `<b>EUR/USD — стан ринку</b>`,
    `Час: ${warsawDateTime(s.generatedAt)} (Europe/Warsaw)`,
    `Сесія: <b>${session.active}</b>${session.inKillzone ? ` · ${session.killzoneLabel}` : ""} · якість ${session.quality}`,
    "",
    q ? `Ціна: <b>${f5(q.mid)}</b>` : `<b>DATA UNAVAILABLE</b>`,
    q ? `Bid / Ask: ${f5(q.bid)} / ${f5(q.ask)} · spread ${q.spreadPips} pips` : "",
    q ? `Джерело: ${esc(q.provider)} · оновлено ${s.quoteAgeSec} с тому` : "",
    `Якість даних: <b>${s.dataQuality}</b>`,
    "",
    `<b>Структура</b>`,
    `H4: ${frames.H4?.structure ?? "—"} · H1: ${frames.H1?.structure ?? "—"} · M15: ${frames.M15?.structure ?? "—"} · M5: ${frames.M5?.structure ?? "—"}`,
    `ATR M15: ${(s.atr.M15 / 0.0001).toFixed(1)} pips · ATR H1: ${(s.atr.H1 / 0.0001).toFixed(1)} pips`,
    `RSI(14) H1: ${frames.H1?.rsi14.toFixed(0) ?? "—"} · M15: ${frames.M15?.rsi14.toFixed(0) ?? "—"}`,
    "",
    `<b>Новини:</b> ${news.level} — ${esc(news.reason)}`,
    session.isWeekend ? "\nРинок закритий (вихідні)." : "",
    s.qualityNotes.length ? `\n<b>Зауваження до даних:</b>\n${s.qualityNotes.map((n) => `• ${esc(n)}`).join("\n")}` : "",
  ];
  return rows.filter(Boolean).join("\n");
}

export function formatCalendar(events: CalendarEvent[], now: number): string {
  const upcoming = events.filter((e) => e.timeUtc >= now).sort((a, b) => a.timeUtc - b.timeUtc).slice(0, 12);
  if (upcoming.length === 0) return "Найближчих подій EUR/USD у перевірених фідах немає.";
  const lines = [`<b>Найближчі події EUR/USD</b>`, ""];
  for (const e of upcoming) {
    const min = Math.round((e.timeUtc - now) / 60000);
    const when = min < 120 ? `через ${min} хв` : `через ${(min / 60).toFixed(1)} год`;
    const badge = e.impact === "high" ? "🔴" : e.impact === "medium" ? "🟠" : "⚪";
    lines.push(`${badge} <b>${esc(e.title)}</b>`);
    lines.push(`${warsawDateTime(e.timeUtc)} · ${when} · ${e.source} · ${e.currency}`);
    if (e.actual || e.forecast || e.previous) {
      lines.push(`actual ${e.actual ?? "—"} / forecast ${e.forecast ?? "—"} / previous ${e.previous ?? "—"}`);
    } else {
      lines.push(`<i>Числові значення не надані джерелом — не підставляються.</i>`);
    }
    if (e.url) lines.push(e.url);
    lines.push("");
  }
  return lines.join("\n");
}

export function formatNews(events: CalendarEvent[], now: number): string {
  const published = events.filter((e) => e.timeUtc <= now).sort((a, b) => b.timeUtc - a.timeUtc).slice(0, 10);
  if (published.length === 0) return "Нових релізів у перевірених фідах немає.";
  const lines = [`<b>Останні новини EUR/USD</b>`, ""];
  for (const e of published) {
    lines.push(`${e.impact === "high" ? "🔴" : "🟠"} <b>${esc(e.title)}</b>`);
    lines.push(`${warsawDateTime(e.timeUtc)} · ${e.source}`);
    if (e.url) lines.push(e.url);
    lines.push("");
  }
  return lines.join("\n");
}

export function formatBrief(s: MarketSnapshot, session: SessionInfo, news: NewsRisk, events: CalendarEvent[], label: string): string {
  const q = s.quote;
  const high = events.filter((e) => e.timeUtc >= s.generatedAt && e.impact === "high").slice(0, 5);
  return [
    `<b>${label}</b>`,
    `${warsawDateTime(s.generatedAt)} · Europe/Warsaw`,
    "",
    q ? `EUR/USD <b>${f5(q.mid)}</b> · spread ${q.spreadPips} pips · ${s.dataQuality}` : `<b>DATA UNAVAILABLE</b>`,
    `H4 ${s.frames.H4?.structure ?? "—"} / H1 ${s.frames.H1?.structure ?? "—"}`,
    `Сесія: ${session.active} (${session.quality})`,
    `Новинний ризик: ${news.level}`,
    high.length ? `\n<b>High-impact сьогодні:</b>\n${high.map((e) => `• ${warsawClock(e.timeUtc)} ${esc(e.title)}`).join("\n")}` : "\nHigh-impact подій у календарі немає.",
    "",
    `<i>Сигнал надійде лише за підтвердження ціною. Відсутність сигналу — теж результат.</i>`,
  ].join("\n");
}

export function formatDailyReport(params: {
  day: string;
  signals: Signal[];
  open: Signal[];
  equity: number;
  accountEquity: number;
  riskPercent: number;
  rejected: Decision[];
}): string {
  const final = params.signals.filter((s) => s.resolvedAt !== null);
  const wins = final.filter((s) => (s.rResult ?? 0) > 0);
  const losses = final.filter((s) => (s.rResult ?? 0) < 0);
  const r = final.reduce((a, s) => a + (s.rResult ?? 0), 0);
  const active = params.open.filter((s) => s.status === "ACTIVE" || s.status === "TP1_HIT");
  const waiting = params.open.filter((s) => s.status === "READY");
  return [
    `<b>Підсумок дня ${params.day}</b>`,
    "",
    `Завершено: ${final.length} · TP: ${wins.length} · SL: ${losses.length}`,
    `Сумарний результат: <b>${r > 0 ? "+" : ""}${r.toFixed(2)}R</b>`,
    `Активних зараз: ${active.length} · очікують входу: ${waiting.length}`,
    `Рішення NO TRADE / BLOCKED: ${params.rejected.length}`,
    "",
    `<b>Капітал</b>`,
    `Початковий: ${params.accountEquity} USD · поточний віртуальний: <b>${params.equity.toFixed(2)} USD</b>`,
    `Ризик на угоду: ${params.riskPercent}%`,
    "",
    final.length
      ? `<b>Угоди</b>\n${final
          .map((s) => `• ${s.symbol.replace("_", "/")} ${s.side} ${s.grade} → ${s.status} (${(s.rResult ?? 0).toFixed(2)}R)`)
          .join("\n")}`
      : "Завершених угод сьогодні немає.",
    "",
    `<i>Віртуальний облік. Реальні накази не надсилались.</i>`,
  ].join("\n");
}

export function telegramConfig(env: { TELEGRAM_BOT_TOKEN?: string; TELEGRAM_CHAT_ID?: string }): { token?: string; chatId?: string; configured: boolean } {
  const configured = Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID);
  return { token: env.TELEGRAM_BOT_TOKEN, chatId: env.TELEGRAM_CHAT_ID, configured };
}
