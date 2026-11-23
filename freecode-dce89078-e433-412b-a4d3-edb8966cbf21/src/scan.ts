import { DEFAULT_DATA, DEFAULT_RISK, type Env } from "./config";
import * as store from "./db";
import { buildSnapshot } from "./market";
import { evaluate, duplicateOfOpen, type SignalCandidate } from "./signals";
import { candidateToSignal } from "./backtest";
import { advanceSignal, applyEvent, positionSize } from "./lifecycle";
import { fetchOfficialNews } from "./news";
import { claimMessage } from "./db";
import { newsRisk, sessionInfo, dayBucket } from "./time";
import type { CalendarEvent, MarketSnapshot, NewsRisk, SessionInfo, Signal } from "./types";

export type ScanOutcome = {
  ranAt: number;
  verdict: string;
  details: string[];
  snapshot: MarketSnapshot | null;
  session: SessionInfo | null;
  news: NewsRisk | null;
  createdSignal: Signal | null;
  updates: { signal: Signal; note: string }[];
  telegram: { sent: number; skipped: number; errors: string[] };
  demo: boolean;
};

async function sendForSignal(env: Env, d1: D1Database, key: string, kind: string, signalId: string, text: string): Promise<{ sent: boolean; error?: string }> {
  const tg = await import("./telegram");
  const claimed = await claimMessage(d1, key, kind, signalId);
  if (!claimed) return { sent: false };
  const { token, chatId } = tg.telegramConfig(env);
  const res = await tg.sendMessage(token, chatId, text);
  if (!res.ok) {
    // Roll back the claim so a later scan can retry the same key.
    await d1.prepare(`DELETE FROM messages WHERE key = ?`).bind(key).run();
    return { sent: false, error: res.error };
  }
  return { sent: true };
}

export async function runScan(env: Env): Promise<ScanOutcome> {
  const d1 = env.DB;
  await store.ensureSchema({ DB: d1 });
  const now = Date.now();
  const state = await store.getScanState(d1);
  const outcome: ScanOutcome = {
    ranAt: now,
    verdict: "NO_TRADE",
    details: [],
    snapshot: null,
    session: null,
    news: null,
    createdSignal: null,
    updates: [],
    telegram: { sent: 0, skipped: 0, errors: [] },
    demo: false,
  };

  const risk = await store.getSetting(d1, "risk", DEFAULT_RISK);
  const dataSettings = await store.getSetting(d1, "data", DEFAULT_DATA);

  try {
    const { snapshot, raw } = await buildSnapshot(env, { allowDemo: true });
    outcome.snapshot = snapshot;
    outcome.demo = snapshot.providerStatus[0]?.provider.startsWith("Synthetic") ?? false;
    const session = sessionInfo(now);
    outcome.session = session;

    // 1) Lifecycle first: resolve what already exists using real closed candles.
    const open = await store.listSignals(d1, { statuses: store.OPEN_STATUSES, limit: 20 });
    for (const s of open) {
      const ev = advanceSignal(s, snapshot.candles.M5, now, risk);
      if (!ev) continue;
      const updated = applyEvent(s, ev, now);
      await store.saveSignal(d1, updated);
      outcome.updates.push({ signal: updated, note: ev.note });
      if (env.NEWS_ENABLED !== "false") {
        const tg = await import("./telegram");
        const r = await sendForSignal(env, d1, `sig:${updated.id}:${updated.status}`, "signal_update", updated.id, tg.formatStatusUpdate(updated, ev.note));
        if (r.sent) outcome.telegram.sent++;
        else outcome.telegram.skipped++;
        if (r.error) outcome.telegram.errors.push(r.error);
      }
    }

    // 2) News: fetch official feeds, persist, compute risk gate.
    let events: CalendarEvent[] = [];
    if (env.NEWS_ENABLED !== "false") {
      try {
        const fetched = await fetchOfficialNews();
        await store.saveEvents(d1, fetched.events);
        events = fetched.events;
        const failed = fetched.status.filter((s) => !s.ok);
        if (failed.length) await store.log(d1, "warn", "news", `${failed.length} фід(ів) недоступні`, failed);
      } catch (err) {
        await store.log(d1, "warn", "news", `Не вдалось отримати новини: ${(err as Error).message}`);
      }
    }
    const storedEvents = await store.listEvents(d1, now - 2 * 86_400_000, now + 7 * 86_400_000);
    const allEvents = storedEvents.length > 0 ? storedEvents : events;
    const news = newsRisk(allEvents, now, dataSettings.newsBlackoutBeforeMin, dataSettings.newsBlackoutAfterMin);
    outcome.news = news;

    // 3) Signal engine
    const day = dayBucket(now);
    const todaySignals = (await store.listSignals(d1, { limit: 80 })).filter((s) => s.createdAt >= day.start);
    const resolvedToday = todaySignals.filter((s) => s.resolvedAt !== null);
    const dailyR = resolvedToday.reduce((a, s) => a + (s.rResult ?? 0), 0);
    const openNow = await store.listSignals(d1, { statuses: ["READY", "ACTIVE", "TP1_HIT", "TP2_HIT"], limit: 10 });
    const levels = (await import("./analysis")).keyLevels(snapshot.candles.H1, now);

    const decision = evaluate(snapshot, levels, session, news, {
      risk,
      data: dataSettings,
      openSignals: openNow,
      signalsToday: todaySignals.length,
      dailyR,
    });
    outcome.verdict = decision.decision.verdict;
    outcome.details = decision.decision.details;
    await store.logDecision(d1, decision.decision);

    const candidate: SignalCandidate | null = decision.candidate;
    const isSendable = candidate && decision.decision.verdict === "SIGNAL" && (candidate.grade === "A" || candidate.grade === "A+");
    if (isSendable && !state.paused) {
      const dupe = duplicateOfOpen(candidate!, [...openNow, ...outcome.updates.map((u) => u.signal)]);
      if (dupe) {
        outcome.details.push(`Схожий сценарій уже існує: ${dupe.id} — повтор не надсилаємо.`);
      } else {
        const sig = candidateToSignal(candidate!, snapshot, session.active, now, snapshot.symbol, risk);
        sig.newsRisk = news.level === "NONE" ? "чисто" : `${news.level}: ${news.reason}`;
        sig.positionSize = positionSize(sig, risk.accountEquity, risk);
        // Persist first so a Telegram failure never duplicates the setup.
        await store.saveSignal(d1, sig);
        outcome.createdSignal = sig;
        if (env.TELEGRAM_ENABLED !== "false") {
          const tg = await import("./telegram");
          const r = await sendForSignal(env, d1, `sig:${sig.id}:READY`, "signal_new", sig.id, tg.formatSignal(sig));
          if (r.sent) outcome.telegram.sent++;
          else outcome.telegram.skipped++;
          if (r.error) outcome.telegram.errors.push(r.error);
        }
      }
    } else if (candidate && (candidate.grade === "B" || candidate.grade === "NO_TRADE")) {
      outcome.details.push(`Спостереження ${candidate.setup} (${candidate.score}/100) — без повідомлення про сигнал.`);
    } else if (state.paused) {
      outcome.details.push("Сигнали призупинено командою /pause.");
    }

    // 4) Persist candles for audit / backtest continuity.
    for (const [tf, candles] of Object.entries(raw)) {
      await store.storeCandles(d1, snapshot.symbol, tf, candles as any, snapshot.quote?.provider ?? "unknown");
    }

    await store.setScanState(d1, { ...state, lastRunAt: now, lastOkAt: now, lastError: null, runsToday: state.runsToday + 1 });
    return outcome;
  } catch (err) {
    const message = (err as Error).message;
    outcome.verdict = "BLOCKED";
    outcome.details.push(message);
    await store.log(d1, "error", "scan", message);
    await store.setScanState(d1, { ...state, lastRunAt: now, lastError: message });
    return outcome;
  }
}

/** Idempotent guard used by the external scheduler to avoid duplicate overlapping runs. */
export async function acquireRunLock(env: Env, ttlSec = 100): Promise<boolean> {
  const key = "scan:lock";
  const now = Date.now();
  const existing = await env.KV.get(key);
  if (existing) {
    const ts = Number(existing);
    if (Number.isFinite(ts) && now - ts < ttlSec * 1000) return false;
  }
  await env.KV.put(key, String(now), { expirationTtl: Math.max(60, ttlSec) });
  return true;
}
