import type { Candle, FrameAnalysis, Swing, Tf, Zone } from "./types";

export function completed(candles: Candle[]): Candle[] {
  // Drop a trailing candle whose close time is still in the future.
  return candles.filter((c) => c.complete !== false);
}

export function ema(values: number[], period: number): number {
  if (values.length === 0) return 0;
  const k = 2 / (period + 1);
  let out = values[0];
  for (let i = 1; i < values.length; i++) out = values[i] * k + out * (1 - k);
  return out;
}

export function sma(values: number[], period: number): number {
  const slice = values.slice(-period);
  if (slice.length === 0) return 0;
  return slice.reduce((a, b) => a + b, 0) / slice.length;
}

export function atr(candles: Candle[], period = 14): number {
  if (candles.length < 2) return 0;
  const trs: number[] = [];
  for (let i = 1; i < candles.length; i++) {
    const p = candles[i - 1];
    const c = candles[i];
    trs.push(Math.max(c.h - c.l, Math.abs(c.h - p.c), Math.abs(c.l - p.c)));
  }
  return sma(trs, Math.min(period, trs.length));
}

export function rsi(candles: Candle[], period = 14): number {
  if (candles.length < period + 1) return 50;
  let gain = 0;
  let loss = 0;
  for (let i = candles.length - period; i < candles.length; i++) {
    const d = candles[i].c - candles[i - 1].c;
    if (d >= 0) gain += d;
    else loss -= d;
  }
  const avgGain = gain / period;
  const avgLoss = loss / period;
  if (avgLoss === 0) return avgGain === 0 ? 50 : 100;
  const rs = avgGain / avgLoss;
  return 100 - 100 / (1 + rs);
}

/** Fractal swings: a high/low that is the extreme of its 2-bar neighbourhood. */
export function findSwings(candles: Candle[], strength = 2): Swing[] {
  const out: Swing[] = [];
  for (let i = strength; i < candles.length - strength; i++) {
    const c = candles[i];
    let isHigh = true;
    let isLow = true;
    for (let j = 1; j <= strength; j++) {
      if (candles[i - j].h >= c.h || candles[i + j].h >= c.h) isHigh = false;
      if (candles[i - j].l <= c.l || candles[i + j].l <= c.l) isLow = false;
    }
    if (isHigh) out.push({ t: c.t, price: c.h, kind: "high" });
    if (isLow) out.push({ t: c.t, price: c.l, kind: "low" });
  }
  return out.sort((a, b) => a.t - b.t);
}

export function displacement(candles: Candle[], atrValue: number): FrameAnalysis["displacement"] {
  if (candles.length < 3 || atrValue <= 0) return null;
  const c = candles[candles.length - 1];
  const body = Math.abs(c.c - c.o);
  const range = Math.max(c.h - c.l, 1e-9);
  const bodyRatio = body / range;
  const strength = body / atrValue;
  if (strength >= 1.35 && bodyRatio >= 0.62) {
    return { dir: c.c > c.o ? "up" : "down", strength: Number(strength.toFixed(2)) };
  }
  return null;
}

export function fairValueGaps(candles: Candle[], lookback = 30): Zone[] {
  const zones: Zone[] = [];
  const start = Math.max(2, candles.length - lookback);
  for (let i = start; i < candles.length; i++) {
    const a = candles[i - 2];
    const c = candles[i];
    if (c.l > a.h) {
      zones.push({ kind: "FVG", low: a.h, high: c.l, dir: "bull", t: candles[i - 1].t, label: "Bullish FVG" });
    } else if (c.h < a.l) {
      zones.push({ kind: "FVG", low: c.h, high: a.l, dir: "bear", t: candles[i - 1].t, label: "Bearish FVG" });
    }
  }
  return zones.slice(-6);
}

function lastBreak(swings: Swing[], kind: "high" | "low", close: number): number | null {
  const candidates = swings.filter((s) => s.kind === kind);
  if (candidates.length === 0) return null;
  const level = candidates[candidates.length - 1].price;
  if (kind === "high" && close > level) return level;
  if (kind === "low" && close < level) return level;
  return null;
}

export function analyseFrame(tf: Tf, input: Candle[]): FrameAnalysis {
  const candles = completed(input);
  const closes = candles.map((c) => c.c);
  const atrValue = atr(candles, 14);
  const swings = findSwings(candles, tf === "H4" ? 3 : 2);
  const last = candles[candles.length - 1];
  const ema20 = ema(closes.slice(-120), 20);
  const ema50 = ema(closes.slice(-200), 50);
  const rsi14 = rsi(candles, 14);
  const notes: string[] = [];

  if (candles.length < 60) notes.push(`Мало історії для ${tf}: ${candles.length} свічок.`);

  const recentSwings = swings.slice(-12);
  const highs = recentSwings.filter((s) => s.kind === "high");
  const lows = recentSwings.filter((s) => s.kind === "low");

  const bosUpLevel = lastBreak(recentSwings, "high", last?.c ?? 0);
  const bosDownLevel = lastBreak(recentSwings, "low", last?.c ?? 0);

  let structure: FrameAnalysis["structure"] = "range";
  if (highs.length >= 2 && lows.length >= 2) {
    const hh = highs[highs.length - 1].price > highs[highs.length - 2].price;
    const hl = lows[lows.length - 1].price > lows[lows.length - 2].price;
    const lh = highs[highs.length - 1].price < highs[highs.length - 2].price;
    const ll = lows[lows.length - 1].price < lows[lows.length - 2].price;
    if ((hh && hl) || (hh && !ll)) structure = "bull";
    else if ((lh && ll) || (ll && !hh)) structure = "bear";
  }
  if (bosUpLevel !== null) structure = "bull";
  if (bosDownLevel !== null) structure = "bear";

  let lastChoch: FrameAnalysis["lastChoch"] = null;
  if (bosUpLevel !== null && structure === "bear") lastChoch = { dir: "up", level: bosUpLevel, t: last?.t ?? 0 };
  if (bosDownLevel !== null && structure === "bull") lastChoch = { dir: "down", level: bosDownLevel, t: last?.t ?? 0 };

  const rangeWindow = candles.slice(-40);
  const rangeHigh = rangeWindow.length ? Math.max(...rangeWindow.map((c) => c.h)) : null;
  const rangeLow = rangeWindow.length ? Math.min(...rangeWindow.map((c) => c.l)) : null;

  const zones: Zone[] = fairValueGaps(candles, tf === "M5" ? 40 : 60);
  if (bosUpLevel !== null) zones.push({ kind: "ORDER_BLOCK", low: bosUpLevel, high: bosUpLevel, dir: "bull", t: last?.t, label: "Broken swing high (support)" });
  if (bosDownLevel !== null) zones.push({ kind: "ORDER_BLOCK", low: bosDownLevel, high: bosDownLevel, dir: "bear", t: last?.t, label: "Broken swing low (resistance)" });

  return {
    tf,
    structure,
    swings: recentSwings,
    lastBos: bosUpLevel !== null ? { dir: "up", level: bosUpLevel, t: last?.t ?? 0 } : bosDownLevel !== null ? { dir: "down", level: bosDownLevel, t: last?.t ?? 0 } : null,
    lastChoch,
    displacement: displacement(candles, atrValue),
    zones,
    rangeHigh,
    rangeLow,
    atr: atrValue,
    ema20,
    ema50,
    rsi14,
    notes,
  };
}

/** Previous day / week / Asia-session extremes derived from H1 candles, all in Warsaw time. */
export function keyLevels(h1: Candle[], now: number): Zone[] {
  const zones: Zone[] = [];
  const day = 24 * 3_600_000;
  const todayStart = Math.floor(now / day) * day;
  const prev = h1.filter((c) => c.t < todayStart && c.t >= todayStart - 2 * day);
  if (prev.length > 0) {
    zones.push({ kind: "DAY_HIGH", low: Math.max(...prev.map((c) => c.h)), high: Math.max(...prev.map((c) => c.h)), label: "Previous day high" });
    zones.push({ kind: "DAY_LOW", low: Math.min(...prev.map((c) => c.l)), high: Math.min(...prev.map((c) => c.l)), label: "Previous day low" });
  }
  const weekStart = todayStart - 7 * day;
  const week = h1.filter((c) => c.t >= weekStart && c.t < todayStart);
  if (week.length > 0) {
    zones.push({ kind: "WEEK_HIGH", low: Math.max(...week.map((c) => c.h)), high: Math.max(...week.map((c) => c.h)), label: "Previous week high" });
    zones.push({ kind: "WEEK_LOW", low: Math.min(...week.map((c) => c.l)), high: Math.min(...week.map((c) => c.l)), label: "Previous week low" });
  }
  return zones;
}

/** Swing-highs/lows nearest above and below price = resting liquidity. */
export function liquidityTargets(frame: FrameAnalysis, price: number): { above: number[]; below: number[] } {
  const above = frame.swings.filter((s) => s.price > price).map((s) => s.price).sort((a, b) => a - b).slice(0, 3);
  const below = frame.swings.filter((s) => s.price < price).map((s) => s.price).sort((a, b) => b - a).slice(0, 3);
  return { above, below };
}
