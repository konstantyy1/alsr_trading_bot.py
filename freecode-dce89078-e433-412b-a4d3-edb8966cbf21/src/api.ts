import { DEFAULT_RISK, type Env } from "./config";
import * as store from "./db";
import { buildSnapshot } from "./market";
import { newsRisk, sessionInfo, warsawDateTime } from "./time";
import { computeStats, virtualEquity } from "./stats";
import * as tg from "./telegram";
import { runScan, acquireRunLock } from "./scan";
import { realiseCommands } from "./commands";

const API = "https://api.telegram.org";

async function reply(env: Env, chatId: number | string, text: string): Promise<void> {
  const { token } = tg.telegramConfig(env);
  if (!token) return;
  await fetch(`${API}/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}

async function sendTyping(env: Env, chatId: number | string): Promise<void> {
  const { token } = tg.telegramConfig(env);
  if (!token) return;
  await fetch(`${API}/bot${token}/sendChatAction`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, action: "typing" }),
  });
}

/** Verify Telegram's secret header, then process the update. */
export async function handleTelegramUpdate(env: Env, request: Request): Promise<Response> {
  const secret = request.headers.get("x-telegram-bot-api-secret-token");
  const expected = env.SCAN_TOKEN ? `${env.SCAN_TOKEN.slice(0, 12)}tg` : null;
  if (!expected || secret !== expected) return new Response("forbidden", { status: 403 });

  const update = (await request.json()) as any;
  const msg = update?.message ?? update?.edited_message;
  const chatId = msg?.chat?.id;
  const text: string = msg?.text ?? "";
  if (!chatId) return new Response("ok");

  const allowed = env.TELEGRAM_CHAT_ID;
  if (!allowed || String(chatId) !== String(allowed)) {
    await reply(env, chatId, "Доступ до цього бота приватний.");
    return new Response("ok");
  }

  const d1 = env.DB;
  await store.ensureSchema({ DB: d1 });
  await sendTyping(env, chatId);

  try {
    await realiseCommands(env, chatId, text.trim());
  } catch (err) {
    await reply(env, chatId, `Помилка обробки команди: ${(err as Error).message}`);
  }
  return new Response("ok");
}

export { runScan, acquireRunLock };

export function healthJson(env: Env) {
  return {
    ok: true,
    telegram: tg.telegramConfig(env).configured,
    data: env.BIQUOTE_API_KEY ? "BiQuote" : "Synthetic(demo)",
    validator: env.MARKETAPI_API_KEY ? "MarketAPI" : null,
    symbol: env.SYMBOL ?? "EUR_USD",
    time: warsawDateTime(Date.now()),
  };
}

export { DEFAULT_RISK, computeStats, virtualEquity, newsRisk, sessionInfo };
