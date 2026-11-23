import { DEFAULT_RISK, type RiskSettings } from "./config";
import type { Decision, Signal } from "./types";

export type Stats = {
  total: number;
  resolved: number;
  open: number;
  wins: number;
  losses: number;
  winRate: number | null;
  expectancyR: number | null;
  totalR: number;
  profitFactor: number | null;
  avgWinR: number | null;
  avgLossR: number | null;
  avgHoldMin: number | null;
  tp1Rate: number | null;
  tp2Rate: number | null;
  tp3Rate: number | null;
  bySession: Record<string, { n: number; r: number; winRate: number | null }>;
  bySetup: Record<string, { n: number; r: number; winRate: number | null }>;
  byGrade: Record<string, { n: number; r: number; winRate: number | null }>;
  equityCurve: { t: number; r: number }[];
  maxDrawdownR: number;
  sampleWarning: string | null;
};

function empty(): Stats {
  return {
    total: 0,
    resolved: 0,
    open: 0,
    wins: 0,
    losses: 0,
    winRate: null,
    expectancyR: null,
    totalR: 0,
    profitFactor: null,
    avgWinR: null,
    avgLossR: null,
    avgHoldMin: null,
    tp1Rate: null,
    tp2Rate: null,
    tp3Rate: null,
    bySession: {},
    bySetup: {},
    byGrade: {},
    equityCurve: [],
    maxDrawdownR: 0,
    sampleWarning: null,
  };
}

function group(signals: Signal[], key: (s: Signal) => string): Record<string, { n: number; r: number; winRate: number | null }> {
  const out: Record<string, { n: number; r: number; winRate: number | null }> = {};
  for (const s of signals) {
    const k = key(s) || "—";
    if (!out[k]) out[k] = { n: 0, r: 0, winRate: null };
    out[k].n++;
    out[k].r += s.rResult ?? 0;
  }
  for (const k of Object.keys(out)) {
    const group_ = signals.filter((s) => (key(s) || "—") === k);
    const decided = group_.filter((s) => s.rResult !== null && s.rResult !== 0);
    out[k].winRate = decided.length ? decided.filter((s) => (s.rResult ?? 0) > 0).length / decided.length : null;
    out[k].r = Number(out[k].r.toFixed(2));
  }
  return out;
}

export function computeStats(all: Signal[]): Stats {
  const stats = empty();
  stats.total = all.length;
  const resolved = all.filter((s) => s.resolvedAt !== null && s.rResult !== null);
  const open = all.filter((s) => s.resolvedAt === null);
  stats.resolved = resolved.length;
  stats.open = open.length;

  // Cancelled with no fill are excluded from expectancy but counted in total.
  const traded = resolved.filter((s) => s.status !== "CANCELLED");
  stats.wins = traded.filter((s) => (s.rResult ?? 0) > 0).length;
  stats.losses = traded.filter((s) => (s.rResult ?? 0) < 0).length;

  const rValues = traded.map((s) => s.rResult ?? 0);
  stats.totalR = Number(rValues.reduce((a, b) => a + b, 0).toFixed(2));

  const decisive = stats.wins + stats.losses;
  stats.winRate = decisive > 0 ? Number((stats.wins / decisive).toFixed(3)) : null;
  stats.expectancyR = traded.length > 0 ? Number((stats.totalR / traded.length).toFixed(3)) : null;

  const grossWin = traded.filter((s) => (s.rResult ?? 0) > 0).reduce((a, s) => a + (s.rResult ?? 0), 0);
  const grossLoss = Math.abs(traded.filter((s) => (s.rResult ?? 0) < 0).reduce((a, s) => a + (s.rResult ?? 0), 0));
  stats.profitFactor = grossLoss > 0 ? Number((grossWin / grossLoss).toFixed(2)) : grossWin > 0 ? null : null;
  stats.avgWinR = stats.wins ? Number((grossWin / stats.wins).toFixed(2)) : null;
  stats.avgLossR = stats.losses ? Number((-grossLoss / stats.losses).toFixed(2)) : null;

  const holds = traded.filter((s) => s.resolvedAt && s.createdAt).map((s) => ((s.resolvedAt as number) - s.createdAt) / 60000);
  stats.avgHoldMin = holds.length ? Number((holds.reduce((a, b) => a + b, 0) / holds.length).toFixed(0)) : null;

  const tp1 = all.filter((s) => ["TP1_HIT", "TP2_HIT", "TP3_HIT"].includes(s.status)).length;
  const tp2 = all.filter((s) => ["TP2_HIT", "TP3_HIT"].includes(s.status)).length;
  const tp3 = all.filter((s) => s.status === "TP3_HIT").length;
  stats.tp1Rate = all.length ? Number((tp1 / all.length).toFixed(3)) : null;
  stats.tp2Rate = all.length ? Number((tp2 / all.length).toFixed(3)) : null;
  stats.tp3Rate = all.length ? Number((tp3 / all.length).toFixed(3)) : null;

  stats.bySession = group(all, (s) => s.session);
  stats.bySetup = group(all, (s) => s.setup);
  stats.byGrade = group(all, (s) => s.grade);

  let cumulative = 0;
  let peak = 0;
  let maxDd = 0;
  const sorted = [...resolved].sort((a, b) => (a.resolvedAt ?? 0) - (b.resolvedAt ?? 0));
  stats.equityCurve = sorted.map((s) => {
    cumulative += s.rResult ?? 0;
    peak = Math.max(peak, cumulative);
    maxDd = Math.max(maxDd, peak - cumulative);
    return { t: s.resolvedAt ?? 0, r: Number(cumulative.toFixed(2)) };
  });
  stats.maxDrawdownR = Number(maxDd.toFixed(2));

  if (traded.length < 100) {
    stats.sampleWarning = `Лише ${traded.length} завершених угод. Для статистично значущих висновків потрібно ≥100, бажано 200+ out-of-sample спостережень.`;
  }
  return stats;
}

export type EquityPoint = { t: number; equity: number; r: number };

/** Virtual account equity from realized R and a fixed risk percent. */
export function virtualEquity(signals: Signal[], startEquity: number, settings: RiskSettings = DEFAULT_RISK): EquityPoint[] {
  let equity = startEquity;
  const out: EquityPoint[] = [];
  const resolved = signals.filter((s) => s.resolvedAt !== null && s.rResult !== null).sort((a, b) => (a.resolvedAt ?? 0) - (b.resolvedAt ?? 0));
  for (const s of resolved) {
    equity += equity * (settings.riskPercent / 100) * (s.rResult ?? 0);
    out.push({ t: s.resolvedAt as number, equity: Number(equity.toFixed(2)), r: s.rResult ?? 0 });
  }
  return out;
}

export function statsFromDecisions(decisions: Decision[]): { noTrade: number; blocked: number; signals: number } {
  return {
    noTrade: decisions.filter((d) => d.verdict === "NO_TRADE").length,
    blocked: decisions.filter((d) => d.verdict === "BLOCKED").length,
    signals: decisions.filter((d) => d.verdict === "SIGNAL").length,
  };
}
