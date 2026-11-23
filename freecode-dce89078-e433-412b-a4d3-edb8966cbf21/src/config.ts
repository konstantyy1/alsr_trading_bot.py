// Central runtime configuration. Values that must not be public live in Worker secrets.

export const INTERNAL_TZ = "Europe/Warsaw";
export const LONDON_TZ = "Europe/London";
export const NY_TZ = "America/New_York";

export type Env = {
  DB: D1Database;
  KV: KVNamespace;
  ASSETS: Fetcher;
  SYMBOL?: string;
  HISTORY_LIMIT?: string;
  NEWS_ENABLED?: string;
  TELEGRAM_ENABLED?: string;
  TELEGRAM_BOT_TOKEN?: string;
  TELEGRAM_CHAT_ID?: string;
  SCAN_TOKEN?: string;
  BIQUOTE_API_KEY?: string;
  MARKETAPI_API_KEY?: string;
};

export type RiskSettings = {
  /** Percent of account per trade. Deliberately conservative by default. */
  riskPercent: number;
  /** Hard stop for the trading day, in R. */
  dailyLossLimitR: number;
  /** Max simultaneously tracked live setups. */
  maxOpenSetups: number;
  /** Max new signals per day regardless of quality. */
  maxSignalsPerDay: number;
  /** Minimum reward:risk accepted. */
  minRr: number;
  /** Score below this is watchlist only, never a trade signal. */
  minScoreReady: number;
  /** Signals above this are A+ (allow up to 3 TP). */
  minScoreAPlus: number;
  /** Assumed round-trip cost in pips (spread + commission + slippage). */
  costPips: number;
  accountEquity: number;
};

export const DEFAULT_RISK: RiskSettings = {
  riskPercent: 0.35,
  dailyLossLimitR: 1.5,
  maxOpenSetups: 3,
  maxSignalsPerDay: 4,
  minRr: 1.6,
  minScoreReady: 70,
  minScoreAPlus: 85,
  costPips: 1.2,
  accountEquity: 10_000,
};

export type DataSettings = {
  /** Max age of a quote before the system refuses to signal. */
  maxQuoteAgeSec: number;
  /** Max allowed difference between primary and validator, in pips. */
  maxCrossDeviationPips: number;
  /** Max spread in pips to allow a new entry. */
  maxSpreadPips: number;
  /** Seconds before/after a high-impact release with no new entries. */
  newsBlackoutBeforeMin: number;
  newsBlackoutAfterMin: number;
  /** Extra ATR ceiling: above this the market is treated as erratic. */
  atrSpikeMultiple: number;
};

export const DEFAULT_DATA: DataSettings = {
  maxQuoteAgeSec: 180,
  maxCrossDeviationPips: 2.5,
  maxSpreadPips: 2.4,
  newsBlackoutBeforeMin: 20,
  newsBlackoutAfterMin: 15,
  atrSpikeMultiple: 3.2,
};

export function symbol(env: Env): string {
  return env.SYMBOL || "EUR_USD";
}

export function pipSize(_sym: string): number {
  return 0.0001;
}

export function historyLimit(env: Env): number {
  const n = Number(env.HISTORY_LIMIT || 1500);
  return Number.isFinite(n) && n > 200 ? Math.min(n, 5000) : 1500;
}
