export type Candle = {
  t: number; // open time, epoch ms (UTC)
  o: number;
  h: number;
  l: number;
  c: number;
  v?: number;
  complete?: boolean;
};

export type Quote = {
  bid: number;
  ask: number;
  mid: number;
  spreadPips: number;
  ts: number; // epoch ms when sourced
  provider: string;
  live: boolean;
};

export type ProviderStatus = {
  provider: string;
  ok: boolean;
  latencyMs: number;
  lastQuote: Quote | null;
  error: string | null;
  checkedAt: number;
};

export type MarketSnapshot = {
  symbol: string;
  quote: Quote | null;
  quoteAgeSec: number;
  quoteFresh: boolean;
  crossCheck: {
    available: boolean;
    secondary: Quote | null;
    deviationPips: number | null;
    withinTolerance: boolean;
    note: string;
  };
  candles: Record<Tf, Candle[]>;
  frames: Record<Tf, FrameAnalysis>;
  atr: Record<Tf, number>;
  dataQuality: "VERIFIED" | "UNVERIFIED" | "DATA UNAVAILABLE";
  qualityNotes: string[];
  providerStatus: ProviderStatus[];
  generatedAt: number;
};

export type Tf = "M5" | "M15" | "H1" | "H4";

export type StructureDir = "bull" | "bear" | "range";

export type Swing = { t: number; price: number; kind: "high" | "low" };

export type Zone = {
  kind: "FVG" | "ORDER_BLOCK" | "SUPPORT" | "RESISTANCE" | "DAY_HIGH" | "DAY_LOW" | "WEEK_HIGH" | "WEEK_LOW" | "ASIA_HIGH" | "ASIA_LOW";
  low: number;
  high: number;
  dir?: "bull" | "bear";
  t?: number;
  label: string;
};

export type FrameAnalysis = {
  tf: Tf;
  structure: StructureDir;
  swings: Swing[];
  lastBos: { dir: "up" | "down"; level: number; t: number } | null;
  lastChoch: { dir: "up" | "down"; level: number; t: number } | null;
  displacement: { dir: "up" | "down"; strength: number } | null;
  zones: Zone[];
  rangeHigh: number | null;
  rangeLow: number | null;
  atr: number;
  ema20: number;
  ema50: number;
  rsi14: number;
  notes: string[];
};

export type SessionName = "Asia" | "London" | "Overlap" | "New York" | "Off-session";

export type SessionInfo = {
  active: SessionName;
  warsawTime: string;
  warsawDate: string;
  londonOpenInMin: number | null;
  nyOpenInMin: number | null;
  overlapInMin: number | null;
  isWeekend: boolean;
  inKillzone: boolean;
  killzoneLabel: string | null;
  quality: "PRIME" | "GOOD" | "LOW";
  notes: string[];
};

export type CalendarEvent = {
  id: string;
  title: string;
  currency: "EUR" | "USD" | "OTHER";
  impact: "high" | "medium" | "low";
  timeUtc: number;
  actual: string | null;
  forecast: string | null;
  previous: string | null;
  source: string;
  url: string | null;
};

export type NewsRisk = {
  level: "NONE" | "ELEVATED" | "BLOCKED";
  reason: string;
  nextEvent: CalendarEvent | null;
  minutesToNext: number | null;
  recentReleases: CalendarEvent[];
};

export type SetupKind =
  | "LIQUIDITY_SWEEP_MSS"
  | "BREAKOUT_RETEST"
  | "TREND_CONTINUATION"
  | "FVG_PULLBACK"
  | "REVERSAL_DAY_LEVEL";

export type SignalStatus =
  | "WATCHLIST"
  | "READY"
  | "ACTIVE"
  | "TP1_HIT"
  | "TP2_HIT"
  | "TP3_HIT"
  | "SL_HIT"
  | "CANCELLED"
  | "EXPIRED"
  | "UNDETERMINED";

export type Signal = {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  status: SignalStatus;
  grade: "A+" | "A" | "B" | "NO_TRADE";
  score: number;
  scoreParts: { label: string; points: number; max: number; note: string }[];
  setup: SetupKind;
  createdAt: number;
  updatedAt: number;
  entryLow: number;
  entryHigh: number;
  trigger: number;
  stop: number;
  tp1: number;
  tp2: number;
  tp3: number | null;
  rr: number;
  riskPips: number;
  rewardPips: number;
  expectedHoldMin: number;
  maxHoldMin: number;
  session: string;
  quoteAtCreation: number;
  spreadAtCreation: number;
  provider: string;
  context: {
    h4: string;
    h1: string;
    m15: string;
    m5: string;
    liquidity: string;
    levels: string;
  };
  why: string[];
  cancelRules: string[];
  invalidations: string[];
  newsRisk: string;
  positionSize: { lots: number; riskAmount: number; note: string };
  resolvedAt: number | null;
  resolution: { status: SignalStatus; price: number; ts: number; note: string } | null;
  rResult: number | null;
};

export type ScanState = {
  lastRunAt: number | null;
  lastOkAt: number | null;
  lastError: string | null;
  lastMessageAt: number | null;
  lastMessageKey: string | null;
  paused: boolean;
  runsToday: number;
  updatedAt: number;
};

export type Decision = {
  at: number;
  verdict: "SIGNAL" | "NO_TRADE" | "BLOCKED";
  symbol: string;
  session: string;
  details: string[];
  snapshotSummary: {
    bid: number | null;
    ask: number | null;
    spread: number | null;
    h4: string;
    h1: string;
    atrM15: number | null;
  } | null;
};
