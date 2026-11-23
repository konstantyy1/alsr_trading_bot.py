import type { Candle, ProviderStatus, Quote, Tf } from "./types";

export interface DataProvider {
  name: string;
  fetchQuote(symbol: string): Promise<Quote>;
  fetchCandles(symbol: string, tf: Tf, limit: number): Promise<Candle[]>;
}

const TF_MS: Record<Tf, number> = { M5: 300_000, M15: 900_000, H1: 3_600_000, H4: 14_400_000 };

export function tfMs(tf: Tf): number {
  return TF_MS[tf];
}

export function decodeSymbol(sym: string): { base: string; quote: string } {
  const clean = sym.replace(/[^A-Za-z]/g, "").toUpperCase();
  return { base: clean.slice(0, 3), quote: clean.slice(3, 6) };
}

/** BiQuote-style primary feed. Requires BIQUOTE_API_KEY. */
export class BiQuoteProvider implements DataProvider {
  name = "BiQuote";
  constructor(private key: string | undefined, private base = "https://api.biquote.com/v1") {}

  private headers(): Record<string, string> {
    if (!this.key) throw new Error("BIQUOTE_API_KEY не налаштовано");
    return { Authorization: `Bearer ${this.key}`, Accept: "application/json" };
  }

  async fetchQuote(symbol: string): Promise<Quote> {
    const { base, quote } = decodeSymbol(symbol);
    const res = await fetch(`${this.base}/quotes?symbol=${base}${quote}`, { headers: this.headers() });
    if (!res.ok) throw new Error(`BiQuote quote HTTP ${res.status}`);
    const raw = (await res.json()) as any;
    const bid = Number(raw?.bid ?? raw?.data?.bid);
    const ask = Number(raw?.ask ?? raw?.data?.ask);
    if (!Number.isFinite(bid) || !Number.isFinite(ask) || bid <= 0 || ask <= 0) throw new Error("BiQuote: некоректні bid/ask");
    const tsRaw = raw?.timestamp ?? raw?.time ?? raw?.data?.timestamp;
    const ts = typeof tsRaw === "string" ? Date.parse(tsRaw) : Number(tsRaw);
    return {
      bid,
      ask,
      mid: (bid + ask) / 2,
      spreadPips: Number(((ask - bid) / 0.0001).toFixed(2)),
      ts: Number.isFinite(ts) && ts > 0 ? ts : Date.now(),
      provider: this.name,
      live: raw?.status ? String(raw.status).toUpperCase() === "LIVE" : true,
    };
  }

  async fetchCandles(symbol: string, tf: Tf, limit: number): Promise<Candle[]> {
    const { base, quote } = decodeSymbol(symbol);
    const interval: Record<Tf, string> = { M5: "5min", M15: "15min", H1: "1h", H4: "4h" };
    const res = await fetch(`${this.base}/ohlc?symbol=${base}${quote}&interval=${interval[tf]}&limit=${limit}`, { headers: this.headers() });
    if (!res.ok) throw new Error(`BiQuote ohlc HTTP ${res.status}`);
    const raw = (await res.json()) as any;
    const rows: any[] = Array.isArray(raw) ? raw : raw?.data ?? raw?.candles ?? [];
    return rows
      .map((r) => {
        if (Array.isArray(r)) {
          return { t: Number(r[0]), o: Number(r[1]), h: Number(r[2]), l: Number(r[3]), c: Number(r[4]), v: Number(r[5] ?? 0) };
        }
        const tRaw = r.time ?? r.t ?? r.timestamp;
        const t = typeof tRaw === "string" ? Date.parse(tRaw) : Number(tRaw);
        return {
          t: Number.isFinite(t) ? t : NaN,
          o: Number(r.open ?? r.o),
          h: Number(r.high ?? r.h),
          l: Number(r.low ?? r.l),
          c: Number(r.close ?? r.c),
          v: Number(r.volume ?? r.v ?? 0),
        };
      })
      .filter((c) => Number.isFinite(c.t) && Number.isFinite(c.c));
  }
}

/**
 * MarketAPI-style validator. Only used to confirm the primary feed,
 * never mixed into the signal feed. Requires MARKETAPI_API_KEY.
 */
export class MarketApiProvider implements DataProvider {
  name = "MarketAPI";
  constructor(private key: string | undefined, private base = "https://api.marketdata.app/v1") {}

  private headers(): Record<string, string> {
    if (!this.key) throw new Error("MARKETAPI_API_KEY не налаштовано");
    return { Authorization: `Token ${this.key}`, Accept: "application/json" };
  }

  async fetchQuote(symbol: string): Promise<Quote> {
    const { base, quote } = decodeSymbol(symbol);
    const res = await fetch(`${this.base}/quotes/?symbols=${base}/${quote}`, { headers: this.headers() });
    if (!res.ok) throw new Error(`MarketAPI quote HTTP ${res.status}`);
    const raw = (await res.json()) as any;
    const bid = Number(first(raw?.bid, raw?.data?.bid));
    const ask = Number(first(raw?.ask, raw?.data?.ask));
    const midRaw = Number(first(raw?.mid, raw?.last, raw?.data?.mid));
    const mid = Number.isFinite(midRaw) && midRaw > 0 ? midRaw : (bid + ask) / 2;
    if (!Number.isFinite(mid) || mid <= 0) throw new Error("MarketAPI: немає ціни");
    const status = String(first(raw?.status, raw?.data?.status) ?? "LIVE").toUpperCase();
    const bidV = Number.isFinite(bid) && bid > 0 ? bid : mid - 0.00005;
    const askV = Number.isFinite(ask) && ask > 0 ? ask : mid + 0.00005;
    return {
      bid: bidV,
      ask: askV,
      mid,
      spreadPips: Number(((askV - bidV) / 0.0001).toFixed(2)),
      ts: Number(first(raw?.updated, raw?.data?.updated)) * (String(first(raw?.updated)).length > 11 ? 1000 : 1) || Date.now(),
      provider: this.name,
      live: status === "LIVE" || status === "OK",
    };
  }

  async fetchCandles(_symbol: string, _tf: Tf, _limit: number): Promise<Candle[]> {
    throw new Error("MarketAPI використовується лише для крос-валідації ціни");
  }
}

function first(...vals: any[]): any {
  for (const v of vals) {
    if (v === undefined || v === null) continue;
    const arr = Array.isArray(v) ? v[0] : v;
    if (arr !== undefined && arr !== null && !(typeof arr === "number" && Number.isNaN(arr))) return arr;
  }
  return undefined;
}

/** Deterministic synthetic feed for local preview only. Never labelled as live. */
export class SyntheticProvider implements DataProvider {
  name = "Synthetic(demo)";
  private seed: number;
  constructor(seed = 20261123) {
    this.seed = seed;
  }

  private rnd(): number {
    // xorshift-ish, stable per process
    this.seed ^= this.seed << 13;
    this.seed ^= this.seed >>> 17;
    this.seed ^= this.seed << 5;
    return ((this.seed >>> 0) % 100000) / 100000;
  }

  async fetchQuote(): Promise<Quote> {
    const now = Date.now();
    const phase = (now % (6 * 3_600_000)) / (6 * 3_600_000);
    const trend = Math.sin(phase * Math.PI * 2) * 0.0016;
    const mid = 1.1592 + trend + (this.rnd() - 0.5) * 0.00035;
    const spread = 0.00009 + this.rnd() * 0.00006;
    return {
      bid: Number((mid - spread / 2).toFixed(5)),
      ask: Number((mid + spread / 2).toFixed(5)),
      mid: Number(mid.toFixed(5)),
      spreadPips: Number((spread / 0.0001).toFixed(2)),
      ts: now,
      provider: this.name,
      live: false,
    };
  }

  async fetchCandles(_symbol: string, tf: Tf, limit: number): Promise<Candle[]> {
    const step = tfMs(tf);
    const now = Math.floor(Date.now() / step) * step;
    const out: Candle[] = [];
    let price = 1.1592 - Math.sin(((now - limit * step) % (6 * 3_600_000)) / (6 * 3_600_000) * Math.PI * 2) * 0.0012;
    for (let i = limit; i >= 0; i--) {
      const t = now - i * step;
      const phase = (t % (6 * 3_600_000)) / (6 * 3_600_000);
      const drift = Math.sin(phase * Math.PI * 2) * 0.00012;
      const vol = 0.00016 * (1 + Math.abs(Math.sin(phase * Math.PI)));
      const o = price;
      const c = Math.max(0.8, o + drift + (this.rnd() - 0.5) * vol);
      const h = Math.max(o, c) + this.rnd() * vol * 0.7;
      const l = Math.min(o, c) - this.rnd() * vol * 0.7;
      out.push({
        t,
        o: Number(o.toFixed(5)),
        h: Number(h.toFixed(5)),
        l: Number(l.toFixed(5)),
        c: Number(c.toFixed(5)),
        v: Math.round(400 + this.rnd() * 900),
        complete: i > 0,
      });
      price = c;
    }
    return out;
  }
}

/** Validates candle series: freshness, gaps, duplicates, bad values, future candles. */
export function validateCandles(tf: Tf, candles: Candle[], now: number): { ok: boolean; notes: string[]; clean: Candle[] } {
  const notes: string[] = [];
  const step = TF_MS[tf];
  const seen = new Set<number>();
  const clean: Candle[] = [];
  let gaps = 0;
  let bad = 0;

  const sorted = [...candles].sort((a, b) => a.t - b.t);
  for (const c of sorted) {
    if (!Number.isFinite(c.o) || !Number.isFinite(c.h) || !Number.isFinite(c.l) || !Number.isFinite(c.c)) {
      bad++;
      continue;
    }
    if (c.h < c.l || c.c > c.h * 1.0005 || c.c < c.l * 0.9995) {
      bad++;
      continue;
    }
    if (c.t > now + step) {
      bad++;
      continue;
    }
    if (seen.has(c.t)) continue;
    seen.add(c.t);
    clean.push(c);
  }

  for (let i = 1; i < clean.length; i++) {
    const delta = clean[i].t - clean[i - 1].t;
    if (delta > step * 1.5) gaps++;
  }

  const last = clean[clean.length - 1];
  if (last) {
    const ageMs = now - last.t;
    if (ageMs > step * 3) notes.push(`${tf}: остання свічка віком ${Math.round(ageMs / 60000)} хв — дані застарілі.`);
  } else {
    notes.push(`${tf}: свічок немає.`);
  }
  if (gaps > 0) notes.push(`${tf}: ${gaps} пропуск(ів) у свічках.`);
  if (bad > 0) notes.push(`${tf}: ${bad} некоректна(их) свічка(ок) відкинуто.`);

  // Mark the currently forming candle as incomplete.
  if (last) {
    const closeTime = last.t + step;
    if (closeTime > now) last.complete = false;
    else last.complete = true;
  }

  const ok = clean.length >= 60 && gaps <= Math.max(3, Math.floor(clean.length * 0.02)) && (last ? now - last.t <= step * 3 : false);
  return { ok, notes, clean };
}

export async function withStatus<T>(provider: DataProvider, fn: (p: DataProvider) => Promise<T>): Promise<{ result: T | null; status: ProviderStatus }> {
  const started = Date.now();
  try {
    const result = await fn(provider);
    return {
      result,
      status: { provider: provider.name, ok: true, latencyMs: Date.now() - started, lastQuote: null, error: null, checkedAt: Date.now() },
    };
  } catch (err) {
    return {
      result: null,
      status: { provider: provider.name, ok: false, latencyMs: Date.now() - started, lastQuote: null, error: (err as Error).message, checkedAt: Date.now() },
    };
  }
}
