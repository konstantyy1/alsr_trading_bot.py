import { type Env } from "./config";
import * as store from "./db";
import { buildSnapshot } from "./market";
import { newsRisk, sessionInfo } from "./time";
import { computeStats, virtualEquity, statsFromDecisions } from "./stats";
import { runScan, acquireRunLock } from "./scan";
import { maybeSendScheduledBroadcasts } from "./commands";
import { DEFAULT_RISK, DEFAULT_DATA } from "./config";
import { analyseFrame } from "./analysis";

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });
}

async function authorised(request: Request, env: Env): Promise<boolean> {
  if (!env.SCAN_TOKEN) return false;
  const header = request.headers.get("authorization") ?? "";
  const bearer = header.toLowerCase().startsWith("bearer ") ? header.slice(7).trim() : "";
  const custom = request.headers.get("x-scan-token") ?? "";
  const token = bearer || custom;
  return token.length > 0 && token === env.SCAN_TOKEN;
}

export async function handleApi(request: Request, env: Env, url: URL): Promise<Response> {
  const path = url.pathname;
  const d1 = env.DB;

  if (path === "/api/selftest") {
    const { runSelfTests } = await import("./selftest");
    const res = runSelfTests();
    return json(res, res.failed === 0 ? 200 : 500);
  }

  if (path === "/api/health") {
    return json({
      ok: true,
      data: env.BIQUOTE_API_KEY ? "BiQuote" : "Synthetic(demo)",
      validator: env.MARKETAPI_API_KEY ? "MarketAPI" : null,
      telegram: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
      symbol: env.SYMBOL ?? "EUR_USD",
      now: Date.now(),
    });
  }

  if (path === "/api/seed-demo") {
    const { demoSignalSet } = await import("./demoMode");
    const demo = demoSignalSet(env);
    return json({ inserted: demo.length, note: "Демо-сценарії генеруються в пам'яті та позначені як демо. Не є торговими результатами.", demo: true });
  }

  if (path === "/api/scan" && (request.method === "POST" || request.method === "GET")) {
    if (!(await authorised(request, env))) return json({ error: "unauthorized" }, 401);
    const locked = await acquireRunLock(env, 100);
    if (!locked) return json({ ok: true, skipped: "інший скан уже виконується" });
    await store.ensureSchema({ DB: d1 });
    const outcome = await runScan(env);
    const broadcasts = await maybeSendScheduledBroadcasts(env).catch(() => []);
    return json({
      ok: true,
      verdict: outcome.verdict,
      demo: outcome.demo,
      createdSignal: outcome.createdSignal ? { id: outcome.createdSignal.id, side: outcome.createdSignal.side, grade: outcome.createdSignal.grade } : null,
      updates: outcome.updates.length,
      telegram: outcome.telegram,
      broadcastKeys: broadcasts,
      details: outcome.details.slice(0, 12),
    });
  }

  if (path === "/api/market") {
    try {
      const { snapshot } = await buildSnapshot(env, { allowDemo: true });
      const session = sessionInfo(Date.now());
      const events = await store.listEvents(d1, Date.now() - 86_400_000, Date.now() + 7 * 86_400_000);
      const news = newsRisk(events, Date.now(), DEFAULT_DATA.newsBlackoutBeforeMin, DEFAULT_DATA.newsBlackoutAfterMin);
      const f = snapshot.frames;
      const priceRail = (tf: "M5" | "M15" | "H1" | "H4") => {
        const candles = snapshot.candles[tf].slice(-60).map((c) => ({ t: c.t, o: c.o, h: c.h, l: c.l, c: c.c }));
        return { tf, candles, atr: Number((snapshot.atr[tf] / 0.0001).toFixed(1)) };
      };
      return json({
        symbol: snapshot.symbol,
        quote: snapshot.quote,
        quoteAgeSec: snapshot.quoteAgeSec,
        dataQuality: snapshot.dataQuality,
        qualityNotes: snapshot.qualityNotes,
        crossCheck: { ...snapshot.crossCheck, secondary: snapshot.crossCheck.secondary },
        providerStatus: snapshot.providerStatus,
        session,
        news,
        frames: {
          H4: { structure: f.H4?.structure, bos: f.H4?.lastBos, displacement: f.H4?.displacement, rsi: f.H4?.rsi14, ema20: f.H4?.ema20, ema50: f.H4?.ema50 },
          H1: { structure: f.H1?.structure, bos: f.H1?.lastBos, choch: f.H1?.lastChoch, displacement: f.H1?.displacement, rsi: f.H1?.rsi14 },
          M15: { structure: f.M15?.structure, displacement: f.M15?.displacement, rsi: f.M15?.rsi14, zones: f.M15?.zones.slice(-4) },
          M5: { structure: f.M5?.structure, displacement: f.M5?.displacement, rsi: f.M5?.rsi14 },
        },
        rails: [priceRail("M5"), priceRail("M15"), priceRail("H1")],
        generatedAt: snapshot.generatedAt,
      });
    } catch (err) {
      return json({ error: (err as Error).message, dataQuality: "DATA UNAVAILABLE" }, 200);
    }
  }

  if (path === "/api/signals") {
    const status = url.searchParams.get("status");
    const limit = Math.min(Number(url.searchParams.get("limit") ?? 40), 200);
    const live = await store.listSignals(d1, {
      statuses: status ? (status.split(",") as any) : undefined,
      limit,
    });
    if (live.length > 0) return json({ signals: live, demo: false });
    const { demoSignalSet, demoMode } = await import("./demoMode");
    let demo = demoSignalSet(env).slice(0, limit);
    if (status) {
      const wanted = status.split(",");
      demo = demo.filter((s) => wanted.includes(s.status));
    }
    return json({ signals: demo, demo: demoMode(env) });
  }

  if (path === "/api/stats") {
    const live = await store.listSignals(d1, { limit: 1000 });
    const risk = await store.getSetting(d1, "risk", DEFAULT_RISK);
    const { demoSignalSet, demoMode } = await import("./demoMode");
    const demo = live.length > 0 ? false : demoMode(env);
    const all = live.length > 0 ? live : demoSignalSet(env);
    const stats = computeStats(all);
    const equity = virtualEquity(all, risk.accountEquity, risk);
    const decisions = await store.listDecisions(d1, 200);
    return json({ stats, equity, decisions: statsFromDecisions(decisions), risk, demo });
  }

  if (path === "/api/decisions") {
    return json({ decisions: await store.listDecisions(d1, Math.min(Number(url.searchParams.get("limit") ?? 20), 100)) });
  }

  if (path === "/api/calendar") {
    const events = await store.listEvents(d1, Date.now() - 86_400_000, Date.now() + 7 * 86_400_000);
    return json({ events, now: Date.now() });
  }

  if (path === "/api/news") {
    const events = await store.listEvents(d1, Date.now() - 4 * 86_400_000, Date.now() + 86_400_000);
    return json({ events: events.sort((a, b) => b.timeUtc - a.timeUtc) });
  }

  if (path === "/api/status") {
    await store.ensureSchema({ DB: d1 });
    const state = await store.getScanState(d1);
    const logs = await store.listLogs(d1, 15);
    const risk = await store.getSetting(d1, "risk", DEFAULT_RISK);
    return json({
      state,
      logs,
      risk,
      telegram: Boolean(env.TELEGRAM_BOT_TOKEN && env.TELEGRAM_CHAT_ID),
      dataProvider: env.BIQUOTE_API_KEY ? "BiQuote" : "Synthetic(demo)",
      validator: env.MARKETAPI_API_KEY ? "MarketAPI" : null,
      scheduler: "external cron (cron-job.org / GitHub Actions) → /api/scan",
    });
  }

  if (path === "/api/backtest" && request.method === "POST") {
    if (!(await authorised(request, env))) return json({ error: "unauthorized" }, 401);
    const body = (await request.json().catch(() => ({}))) as { bars?: number };
    try {
      const { runBacktest } = await import("./backtest");
      const { snapshot } = await buildSnapshot(env, { allowDemo: true });
      const m5 = snapshot.candles.M5;
      const result = runBacktest({ m5, symbol: snapshot.symbol, maxBars: body.bars ?? 3000 });
      const stats = computeStats(result.signals);
      return json({ meta: result.meta, stats, sample: result.signals.slice(-5) });
    } catch (err) {
      return json({ error: (err as Error).message }, 400);
    }
  }

  return json({ error: "not found" }, 404);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith("/api/")) {
      try {
        return await handleApi(request, env, url);
      } catch (err) {
        return json({ error: (err as Error).message }, 500);
      }
    }
    if (url.pathname === "/tg") {
      const { handleTelegramUpdate } = await import("./api");
      return handleTelegramUpdate(env, request);
    }
    return env.ASSETS.fetch(request);
  },
};
