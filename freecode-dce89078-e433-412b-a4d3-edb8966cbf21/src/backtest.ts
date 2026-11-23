import { analyseFrame, keyLevels } from "./analysis";
import { DEFAULT_DATA, DEFAULT_RISK, type DataSettings, type RiskSettings } from "./config";
import { advanceSignal, applyEvent, positionSize } from "./lifecycle";
import { evaluate, newSignalId, type SignalCandidate } from "./signals";
import { sessionInfo } from "./time";
import type { Candle, FrameAnalysis, MarketSnapshot, Quote, Signal, Tf } from "./types";

export type BacktestOptions = {
  m5: Candle[];
  h1?: Candle[];
  h4?: Candle[];
  symbol?: string;
  risk?: RiskSettings;
  data?: DataSettings;
  /** Bars of history required before the first evaluation. */
  warmup?: number;
  maxBars?: number;
};

export type BacktestResult = {
  signals: Signal[];
  meta: {
    bars: number;
    evaluated: number;
    skipped: number;
    costModel: string;
    noLookAhead: true;
    newsFilter: string;
    note: string;
    splitIndex: number;
    inSample: { n: number; r: number; winRate: number | null };
    outOfSample: { n: number; r: number; winRate: number | null };
  };
};

function aggregate(m5: Candle[], targetMs: number): Candle[] {
  const buckets = new Map<number, Candle>();
  for (const c of m5) {
    const key = Math.floor(c.t / targetMs) * targetMs;
    const b = buckets.get(key);
    if (!b) {
      buckets.set(key, { t: key, o: c.o, h: c.h, l: c.l, c: c.c, v: c.v ?? 0, complete: true });
    } else {
      b.h = Math.max(b.h, c.h);
      b.l = Math.min(b.l, c.l);
      b.c = c.c;
      b.v = (b.v ?? 0) + (c.v ?? 0);
    }
  }
  return [...buckets.values()].sort((a, b) => a.t - b.t);
}

function emptyFrame(tf: Tf): FrameAnalysis {
  return {
    tf,
    structure: "range",
    swings: [],
    lastBos: null,
    lastChoch: null,
    displacement: null,
    zones: [],
    rangeHigh: null,
    rangeLow: null,
    atr: 0,
    ema20: 0,
    ema50: 0,
    rsi14: 50,
    notes: ["warmup"],
  };
}

/**
 * Replay historical candles bar by bar. Each evaluation only sees candles whose
 * close time is at or before the simulated "now", which removes look-ahead bias.
 * Costs are charged by widening the stop to simulate spread+slippage.
 */
export function runBacktest(opts: BacktestOptions): BacktestResult {
  const risk = opts.risk ?? DEFAULT_RISK;
  const data = opts.data ?? DEFAULT_DATA;
  const symbol = opts.symbol ?? "EUR_USD";
  const m5 = opts.m5.filter((c) => c.complete !== false).sort((a, b) => a.t - b.t);
  const warmup = opts.warmup ?? 400;
  const maxBars = Math.min(opts.maxBars ?? 6000, m5.length - warmup);

  const signals: Signal[] = [];
  let evaluated = 0;
  let skipped = 0;

  for (let i = warmup; i < warmup + maxBars; i++) {
    const now = m5[i].t;
    const window = m5.slice(Math.max(0, i - 1200), i + 1);
    const last = m5[i];

    const quote: Quote = {
      bid: last.c - 0.00006,
      ask: last.c + 0.00006,
      mid: last.c,
      spreadPips: 1.2,
      ts: now,
      provider: "backtest(replay)",
      live: true,
    };

    const m15 = aggregate(window, 900_000);
    const h1 = aggregate(window, 3_600_000);
    const h4 = aggregate(window, 14_400_000);

    const frames: Record<Tf, FrameAnalysis> = {
      M5: window.length > 60 ? analyseFrame("M5", window) : emptyFrame("M5"),
      M15: m15.length > 40 ? analyseFrame("M15", m15) : emptyFrame("M15"),
      H1: h1.length > 30 ? analyseFrame("H1", h1) : emptyFrame("H1"),
      H4: h4.length > 20 ? analyseFrame("H4", h4) : emptyFrame("H4"),
    };

    const snapshot: MarketSnapshot = {
      symbol,
      quote,
      quoteAgeSec: 0,
      quoteFresh: true,
      crossCheck: { available: false, secondary: null, deviationPips: null, withinTolerance: true, note: "backtest" },
      candles: { M5: window, M15: m15, H1: h1, H4: h4 },
      frames,
      atr: { M5: frames.M5.atr, M15: frames.M15.atr, H1: frames.H1.atr, H4: frames.H4.atr },
      dataQuality: "VERIFIED",
      qualityNotes: [],
      providerStatus: [],
      generatedAt: now,
    };

    const session = sessionInfo(now);
    const open = signals.filter((s) => s.resolvedAt === null);
    const sameDay = signals.filter((s) => Math.floor(s.createdAt / 86_400_000) === Math.floor(now / 86_400_000));
    const dailyR = signals
      .filter((s) => s.resolvedAt !== null && Math.floor((s.resolvedAt as number) / 86_400_000) === Math.floor(now / 86_400_000))
      .reduce((a, s) => a + (s.rResult ?? 0), 0);

    // Advance open signals first with the newest closed candle.
    for (const s of open) {
      const ev = advanceSignal(s, [last], now, risk);
      if (ev) {
        const updated = applyEvent(s, ev, now);
        Object.assign(s, updated);
      }
    }
    if (open.length >= risk.maxOpenSetups) {
      skipped++;
      continue;
    }

    const levels = keyLevels(h1, now);
    const news = { level: "NONE" as const, reason: "backtest: historical calendar not available", nextEvent: null, minutesToNext: null, recentReleases: [] };
    const res = evaluate(snapshot, levels, session, news, { risk, data, openSignals: open, signalsToday: sameDay.length, dailyR });
    evaluated++;

    if (!res.candidate || res.decision.verdict !== "SIGNAL") continue;
    const c: SignalCandidate = res.candidate;
    const sig: Signal = candidateToSignal(c, snapshot, res.decision.session, now, symbol, risk);
    signals.push(sig);
  }

  // Resolve anything still open using remaining candles.
  for (const s of signals.filter((x) => x.resolvedAt === null)) {
    const after = m5.filter((c) => c.t >= s.createdAt);
    const ev = advanceSignal(s, after, after.length ? after[after.length - 1].t : s.createdAt, risk);
    if (ev) Object.assign(s, applyEvent(s, ev, ev.ts));
  }

  const resolved = signals.filter((s) => s.resolvedAt !== null);
  const splitIdx = Math.floor(resolved.length * 0.7);
  const tally = (arr: Signal[]) => {
    const decided = arr.filter((s) => s.status !== "CANCELLED");
    const r = decided.reduce((a, s) => a + (s.rResult ?? 0), 0);
    const decisive = decided.filter((s) => (s.rResult ?? 0) !== 0);
    return {
      n: decided.length,
      r: Number(r.toFixed(2)),
      winRate: decisive.length ? Number((decisive.filter((s) => (s.rResult ?? 0) > 0).length / decisive.length).toFixed(3)) : null,
    };
  };

  return {
    signals,
    meta: {
      bars: maxBars,
      evaluated,
      skipped,
      costModel: `Стоп розширено на ${risk.costPips} pips round-trip для імітації spread + комісії + прослизання.`,
      noLookAhead: true,
      newsFilter: "Історичний календар недоступний безкоштовно — news-фільтр у бектесті вимкнено, тому результати оптимістичніші за live.",
      note: resolved.length < 100 ? `Лише ${resolved.length} угод у бектесті — висновки попередні.` : "Достатньо спостережень для попередніх висновків.",
      splitIndex: splitIdx,
      inSample: tally(resolved.slice(0, splitIdx)),
      outOfSample: tally(resolved.slice(splitIdx)),
    },
  };
}

export function candidateToSignal(c: SignalCandidate, s: MarketSnapshot, session: string, now: number, symbol: string, risk: RiskSettings): Signal {
  const widened = risk.costPips * 0.0001;
  const stop = c.side === "BUY" ? c.stop - widened / 2 : c.stop + widened / 2;
  const riskPips = Math.abs((c.trigger - stop) / 0.0001);
  const base: Signal = {
    id: newSignalId(now, c.side, c.setup),
    symbol,
    side: c.side,
    status: "READY",
    grade: c.grade,
    score: c.score,
    scoreParts: c.scoreParts,
    setup: c.setup,
    createdAt: now,
    updatedAt: now,
    entryLow: c.entryLow,
    entryHigh: c.entryHigh,
    trigger: c.trigger,
    stop: Number(stop.toFixed(5)),
    tp1: c.tp1,
    tp2: c.tp2,
    tp3: c.tp3,
    rr: Number((Math.abs(c.tp1 - c.trigger) / Math.max(riskPips * 0.0001, 1e-9)).toFixed(2)),
    riskPips: Number(riskPips.toFixed(1)),
    rewardPips: c.rewardPips,
    expectedHoldMin: c.expectedHoldMin,
    maxHoldMin: c.maxHoldMin,
    session,
    quoteAtCreation: s.quote?.mid ?? c.trigger,
    spreadAtCreation: s.quote?.spreadPips ?? 0,
    provider: s.quote?.provider ?? "unknown",
    context: c.context,
    why: c.why,
    cancelRules: c.cancelRules,
    invalidations: c.invalidations,
    newsRisk: "перевірено на момент створення",
    positionSize: { lots: 0, riskAmount: 0, note: "" },
    resolvedAt: null,
    resolution: null,
    rResult: null,
  };
  base.positionSize = positionSize(base, risk.accountEquity, risk);
  return base;
}
