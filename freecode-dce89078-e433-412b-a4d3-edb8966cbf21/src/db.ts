import type { CalendarEvent, Decision, ScanState, Signal, SignalStatus } from "./types";

let schemaReady: Promise<void> | null = null;

export function db(env: { DB: D1Database }): D1Database {
  return env.DB;
}

export async function ensureSchema(env: { DB: D1Database }): Promise<void> {
  if (schemaReady) return schemaReady;
  schemaReady = (async () => {
    await env.DB.batch([
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        status TEXT NOT NULL,
        grade TEXT NOT NULL,
        score INTEGER NOT NULL,
        setup TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        entry_low REAL NOT NULL,
        entry_high REAL NOT NULL,
        trigger REAL NOT NULL,
        stop REAL NOT NULL,
        tp1 REAL NOT NULL,
        tp2 REAL NOT NULL,
        tp3 REAL,
        rr REAL NOT NULL,
        risk_pips REAL NOT NULL,
        reward_pips REAL NOT NULL,
        expected_hold_min INTEGER NOT NULL,
        max_hold_min INTEGER NOT NULL,
        session TEXT NOT NULL,
        quote_at_creation REAL NOT NULL,
        spread_at_creation REAL NOT NULL,
        provider TEXT NOT NULL,
        payload TEXT NOT NULL,
        resolved_at INTEGER,
        resolution TEXT,
        r_result REAL,
        origin TEXT NOT NULL DEFAULT 'live'
      )`),
      env.DB.prepare(`CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(status)`),
      env.DB.prepare(`CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at DESC)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        currency TEXT NOT NULL,
        impact TEXT NOT NULL,
        time_utc INTEGER NOT NULL,
        actual TEXT,
        forecast TEXT,
        previous TEXT,
        source TEXT NOT NULL,
        url TEXT,
        fetched_at INTEGER NOT NULL
      )`),
      env.DB.prepare(`CREATE INDEX IF NOT EXISTS idx_events_time ON events(time_utc)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        verdict TEXT NOT NULL,
        symbol TEXT NOT NULL,
        session TEXT NOT NULL,
        details TEXT NOT NULL,
        summary TEXT
      )`),
      env.DB.prepare(`CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions(at DESC)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at INTEGER NOT NULL,
        level TEXT NOT NULL,
        scope TEXT NOT NULL,
        message TEXT NOT NULL,
        meta TEXT
      )`),
      env.DB.prepare(`CREATE INDEX IF NOT EXISTS idx_logs_at ON logs(at DESC)`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL
      )`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS messages (
        key TEXT PRIMARY KEY,
        sent_at INTEGER NOT NULL,
        signal_id TEXT,
        kind TEXT NOT NULL
      )`),
      env.DB.prepare(`CREATE TABLE IF NOT EXISTS candles (
        symbol TEXT NOT NULL,
        tf TEXT NOT NULL,
        t INTEGER NOT NULL,
        o REAL NOT NULL,
        h REAL NOT NULL,
        l REAL NOT NULL,
        c REAL NOT NULL,
        v REAL,
        provider TEXT NOT NULL,
        PRIMARY KEY (symbol, tf, t)
      )`),
    ]);
  })();
  return schemaReady;
}

export async function saveSignal(d1: D1Database, s: Signal, origin: "live" | "backtest" = "live"): Promise<void> {
  await d1
    .prepare(
      `INSERT INTO signals (id,symbol,side,status,grade,score,setup,created_at,updated_at,entry_low,entry_high,trigger,stop,tp1,tp2,tp3,rr,risk_pips,reward_pips,expected_hold_min,max_hold_min,session,quote_at_creation,spread_at_creation,provider,payload,resolved_at,resolution,r_result,origin)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
       ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, payload=excluded.payload, resolved_at=excluded.resolved_at, resolution=excluded.resolution, r_result=excluded.r_result`,
    )
    .bind(
      s.id,
      s.symbol,
      s.side,
      s.status,
      s.grade,
      s.score,
      s.setup,
      s.createdAt,
      s.updatedAt,
      s.entryLow,
      s.entryHigh,
      s.trigger,
      s.stop,
      s.tp1,
      s.tp2,
      s.tp3,
      s.rr,
      s.riskPips,
      s.rewardPips,
      s.expectedHoldMin,
      s.maxHoldMin,
      s.session,
      s.quoteAtCreation,
      s.spreadAtCreation,
      s.provider,
      JSON.stringify(s),
      s.resolvedAt,
      s.resolution ? JSON.stringify(s.resolution) : null,
      s.rResult,
      origin,
    )
    .run();
}

export async function listSignals(d1: D1Database, opts: { statuses?: SignalStatus[]; limit?: number; origin?: string } = {}): Promise<Signal[]> {
  const limit = opts.limit ?? 50;
  let rows: any;
  if (opts.statuses && opts.statuses.length > 0) {
    const marks = opts.statuses.map(() => "?").join(",");
    rows = await d1
      .prepare(`SELECT payload FROM signals WHERE status IN (${marks}) AND origin = ? ORDER BY created_at DESC LIMIT ?`)
      .bind(...opts.statuses, opts.origin ?? "live", limit)
      .all();
  } else {
    rows = await d1
      .prepare(`SELECT payload FROM signals WHERE origin = ? ORDER BY created_at DESC LIMIT ?`)
      .bind(opts.origin ?? "live", limit)
      .all();
  }
  return (rows.results as any[]).map((r) => JSON.parse(r.payload) as Signal);
}

export async function getSignal(d1: D1Database, id: string): Promise<Signal | null> {
  const row = await d1.prepare(`SELECT payload FROM signals WHERE id = ?`).bind(id).first<{ payload: string }>();
  return row ? (JSON.parse(row.payload) as Signal) : null;
}

export const OPEN_STATUSES: SignalStatus[] = ["WATCHLIST", "READY", "ACTIVE", "TP1_HIT", "TP2_HIT"];

export async function saveEvents(d1: D1Database, events: CalendarEvent[]): Promise<void> {
  if (events.length === 0) return;
  const now = Date.now();
  const stmts = events.map((e) =>
    d1
      .prepare(
        `INSERT INTO events (id,title,currency,impact,time_utc,actual,forecast,previous,source,url,fetched_at)
         VALUES (?,?,?,?,?,?,?,?,?,?,?)
         ON CONFLICT(id) DO UPDATE SET actual=excluded.actual, forecast=excluded.forecast, previous=excluded.previous, fetched_at=excluded.fetched_at`,
      )
      .bind(e.id, e.title, e.currency, e.impact, e.timeUtc, e.actual, e.forecast, e.previous, e.source, e.url, now),
  );
  await d1.batch(stmts.slice(0, 100));
}

export async function listEvents(d1: D1Database, from: number, to: number): Promise<CalendarEvent[]> {
  const rows = await d1.prepare(`SELECT * FROM events WHERE time_utc >= ? AND time_utc <= ? ORDER BY time_utc ASC LIMIT 200`).bind(from, to).all();
  return (rows.results as any[]).map((r) => ({
    id: r.id,
    title: r.title,
    currency: r.currency,
    impact: r.impact,
    timeUtc: r.time_utc,
    actual: r.actual,
    forecast: r.forecast,
    previous: r.previous,
    source: r.source,
    url: r.url,
  }));
}

export async function logDecision(d1: D1Database, d: Decision): Promise<void> {
  await d1
    .prepare(`INSERT INTO decisions (at,verdict,symbol,session,details,summary) VALUES (?,?,?,?,?,?)`)
    .bind(d.at, d.verdict, d.symbol, d.session, JSON.stringify(d.details), JSON.stringify(d.snapshotSummary))
    .run();
}

export async function listDecisions(d1: D1Database, limit = 20): Promise<Decision[]> {
  const rows = await d1.prepare(`SELECT * FROM decisions ORDER BY at DESC LIMIT ?`).bind(limit).all();
  return (rows.results as any[]).map((r) => ({
    at: r.at,
    verdict: r.verdict,
    symbol: r.symbol,
    session: r.session,
    details: JSON.parse(r.details || "[]"),
    snapshotSummary: r.summary ? JSON.parse(r.summary) : null,
  }));
}

export async function log(d1: D1Database, level: "info" | "warn" | "error", scope: string, message: string, meta?: unknown): Promise<void> {
  try {
    await d1.prepare(`INSERT INTO logs (at,level,scope,message,meta) VALUES (?,?,?,?,?)`).bind(Date.now(), level, scope, message, meta ? JSON.stringify(meta) : null).run();
  } catch {
    // Logging must never break the scan.
  }
}

export async function listLogs(d1: D1Database, limit = 40): Promise<{ at: number; level: string; scope: string; message: string }[]> {
  const rows = await d1.prepare(`SELECT at,level,scope,message FROM logs ORDER BY at DESC LIMIT ?`).bind(limit).all();
  return rows.results as any[];
}

export async function getSetting<T>(d1: D1Database, key: string, fallback: T): Promise<T> {
  const row = await d1.prepare(`SELECT value FROM settings WHERE key = ?`).bind(key).first<{ value: string }>();
  if (!row) return fallback;
  try {
    return { ...fallback, ...JSON.parse(row.value) } as T;
  } catch {
    return fallback;
  }
}

export async function setSetting(d1: D1Database, key: string, value: unknown): Promise<void> {
  await d1
    .prepare(`INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at`)
    .bind(key, JSON.stringify(value), Date.now())
    .run();
}

export async function getScanState(d1: D1Database): Promise<ScanState> {
  return getSetting<ScanState>(d1, "scan_state", {
    lastRunAt: null,
    lastOkAt: null,
    lastError: null,
    lastMessageAt: null,
    lastMessageKey: null,
    paused: false,
    runsToday: 0,
    updatedAt: Date.now(),
  });
}

export async function setScanState(d1: D1Database, state: ScanState): Promise<void> {
  state.updatedAt = Date.now();
  await setSetting(d1, "scan_state", state);
}

/** Dedup guard: returns true if this message key was never sent before. */
export async function claimMessage(d1: D1Database, key: string, kind: string, signalId?: string): Promise<boolean> {
  const existing = await d1.prepare(`SELECT key FROM messages WHERE key = ?`).bind(key).first();
  if (existing) return false;
  await d1.prepare(`INSERT INTO messages (key,sent_at,signal_id,kind) VALUES (?,?,?,?)`).bind(key, Date.now(), signalId ?? null, kind).run();
  return true;
}

export async function storeCandles(d1: D1Database, symbol: string, tf: string, candles: { t: number; o: number; h: number; l: number; c: number; v?: number }[], provider: string): Promise<void> {
  if (candles.length === 0) return;
  const keep = candles.slice(-300);
  const stmts = keep.map((c) =>
    d1
      .prepare(`INSERT INTO candles (symbol,tf,t,o,h,l,c,v,provider) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol,tf,t) DO NOTHING`)
      .bind(symbol, tf, c.t, c.o, c.h, c.l, c.c, c.v ?? 0, provider),
  );
  for (let i = 0; i < stmts.length; i += 25) await d1.batch(stmts.slice(i, i + 25));
}

export async function listCandles(d1: D1Database, symbol: string, tf: string, from: number, to: number): Promise<{ t: number; o: number; h: number; l: number; c: number }[]> {
  const rows = await d1
    .prepare(`SELECT t,o,h,l,c FROM candles WHERE symbol = ? AND tf = ? AND t >= ? AND t <= ? ORDER BY t ASC`)
    .bind(symbol, tf, from, to)
    .all();
  return rows.results as any[];
}
