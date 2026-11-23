import { DEFAULT_DATA, DEFAULT_RISK, pipSize, type RiskSettings, type DataSettings } from "./config";
import { liquidityTargets } from "./analysis";
import { dayBucket } from "./time";
import type { Candle, Decision, FrameAnalysis, MarketSnapshot, NewsRisk, SessionInfo, SetupKind, Signal, Zone } from "./types";

export type SignalCandidate = {
  side: "BUY" | "SELL";
  setup: SetupKind;
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
  context: Signal["context"];
  why: string[];
  cancelRules: string[];
  invalidations: string[];
  scoreParts: Signal["scoreParts"];
  score: number;
  grade: Signal["grade"];
};

export type EvaluateResult = {
  decision: Decision;
  candidate: SignalCandidate | null;
};

const pips = (a: number, b: number) => Math.abs(a - b) / 0.0001;

function fmt(price: number): string {
  return price.toFixed(5);
}

/**
 * Six minute-level setup detectors. Each returns a candidate (without score) or null.
 * Rules are explicit so they can be backtested and audited.
 */
function detectSetups(s: MarketSnapshot, levels: Zone[]): Omit<SignalCandidate, "score" | "grade" | "scoreParts">[] {
  const out: Omit<SignalCandidate, "score" | "grade" | "scoreParts">[] = [];
  const q = s.quote;
  const m5 = s.candles.M5;
  const m15 = s.candles.M15;
  if (!q || m5.length < 40 || m15.length < 30) return out;

  const fM5 = s.frames.M5;
  const fM15 = s.frames.M15;
  const fH1 = s.frames.H1;
  const fH4 = s.frames.H4;
  const last = m5[m5.length - 1];
  const prev = m5[m5.length - 2];
  const atrM5 = fM5.atr || 0.0004;
  const atrM15 = fM15.atr || 0.0006;
  const price = q.mid;
  const liq = liquidityTargets(fM15, price);

  const dayHigh = levels.find((z) => z.kind === "DAY_HIGH")?.high ?? null;
  const dayLow = levels.find((z) => z.kind === "DAY_LOW")?.low ?? null;
  const dayRange = dayHigh !== null && dayLow !== null ? dayHigh - dayLow : atrM15 * 20;

  const levelsText = [
    dayHigh !== null ? `PDH ${fmt(dayHigh)}` : null,
    dayLow !== null ? `PDL ${fmt(dayLow)}` : null,
    liq.above[0] ? `ліквідність вище ${fmt(liq.above[0])}` : null,
    liq.below[0] ? `ліквідність нижче ${fmt(liq.below[0])}` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  const baseContext = (extra: string): Signal["context"] => ({
    h4: `H4 ${fH4.structure} · ${fH4.lastBos ? `BOS ${fH4.lastBos.dir} @ ${fmt(fH4.lastBos.level)}` : "без свіжого BOS"}`,
    h1: `H1 ${fH1.structure} · ${fH1.lastChoch ? `CHoCH ${fH1.lastChoch.dir} @ ${fmt(fH1.lastChoch.level)}` : fH1.lastBos ? `BOS ${fH1.lastBos.dir}` : "консолідація"}`,
    m15: `M15 ${fM15.structure} · ${fM15.displacement ? `displacement ${fM15.displacement.dir} x${fM15.displacement.strength}` : "без displacement"}`,
    m5: extra,
    liquidity: `${liq.above.length} цілі вище, ${liq.below.length} нижче`,
    levels: levelsText || "рівні не визначені",
  });

  const commonRules = (side: "BUY" | "SELL", stop: number) => ({
    cancelRules: [
      "Ціна закрила M5 свічку за рівнем інвалідації до активації.",
      "Сигнал старший за максимальний час очікування входу.",
      "Spread перевищив ліміт або джерело даних стало UNVERIFIED.",
      "Вийшла high-impact новина EUR/USD до входу.",
    ],
    invalidations: [
      `Закриття M5 ${side === "BUY" ? "нижче" : "вище"} ${fmt(stop)} скасовує сценарій.`,
      "Втрата структури H1 проти напрямку угоди.",
      "Різкий spike > 2.5× ATR без повернення — рух вважається випадковим.",
    ],
  });

  // 1) Liquidity sweep + market structure shift on M5, aligned with M15.
  {
    const sweepLow = prev.l < Math.min(...m5.slice(-12, -1).map((c: Candle) => c.l));
    const reclaim = last.c > prev.h;
    if (sweepLow && reclaim && fM5.structure !== "bear" && fM15.structure !== "bear") {
      const swingLow = Math.min(...m5.slice(-12).map((c) => c.l));
      const stop = swingLow - atrM5 * 0.35;
      const entry = price;
      const risk = entry - stop;
      const target1 = liq.above[0] ?? entry + risk * 2;
      const target2 = liq.above[1] ?? entry + risk * 3;
      if (risk > atrM5 * 0.5 && risk < atrM15 * 2.2) {
        out.push({
          side: "BUY",
          setup: "LIQUIDITY_SWEEP_MSS",
          entryLow: Number((entry - atrM5 * 0.2).toFixed(5)),
          entryHigh: Number((entry + atrM5 * 0.1).toFixed(5)),
          trigger: Number(entry.toFixed(5)),
          stop: Number(stop.toFixed(5)),
          tp1: Number(target1.toFixed(5)),
          tp2: Number(target2.toFixed(5)),
          tp3: Number((target2 + risk).toFixed(5)),
          rr: Number(pips(target1, entry) / pips(entry, stop)) || 0,
          riskPips: Number(pips(entry, stop).toFixed(1)),
          rewardPips: Number(pips(target1, entry).toFixed(1)),
          expectedHoldMin: 45,
          maxHoldMin: 240,
          context: baseContext(`sweep ${fmt(swingLow)} → reclaim, close ${fmt(last.c)}`),
          why: [
            "M5 зняв ліквідність під локальним мінімумом і повернувся над діапазоном свічки.",
            `Структура M15 ${fM15.structure} не суперечить лонгу.`,
            "Тригер — закриття M5 вище тіла свічки зняття.",
          ],
          ...commonRules("BUY", stop),
        });
      }
    }
    const sweepHigh = prev.h > Math.max(...m5.slice(-12, -1).map((c: Candle) => c.h));
    const reject = last.c < prev.l;
    if (sweepHigh && reject && fM5.structure !== "bull" && fM15.structure !== "bull") {
      const swingHigh = Math.max(...m5.slice(-12).map((c) => c.h));
      const stop = swingHigh + atrM5 * 0.35;
      const entry = price;
      const risk = stop - entry;
      const target1 = liq.below[0] ?? entry - risk * 2;
      const target2 = liq.below[1] ?? entry - risk * 3;
      if (risk > atrM5 * 0.5 && risk < atrM15 * 2.2) {
        out.push({
          side: "SELL",
          setup: "LIQUIDITY_SWEEP_MSS",
          entryLow: Number((entry - atrM5 * 0.1).toFixed(5)),
          entryHigh: Number((entry + atrM5 * 0.2).toFixed(5)),
          trigger: Number(entry.toFixed(5)),
          stop: Number(stop.toFixed(5)),
          tp1: Number(target1.toFixed(5)),
          tp2: Number(target2.toFixed(5)),
          tp3: Number((target2 - risk).toFixed(5)),
          rr: Number(pips(entry, target1) / pips(stop, entry)) || 0,
          riskPips: Number(pips(stop, entry).toFixed(1)),
          rewardPips: Number(pips(entry, target1).toFixed(1)),
          expectedHoldMin: 45,
          maxHoldMin: 240,
          context: baseContext(`sweep ${fmt(swingHigh)} → rejection, close ${fmt(last.c)}`),
          why: [
            "M5 зняв ліквідність над локальним максимумом і закрився назад під рівнем.",
            `Структура M15 ${fM15.structure} не суперечить шорту.`,
            "Тригер — закриття M5 нижче тіла свічки зняття.",
          ],
          ...commonRules("SELL", stop),
        });
      }
    }
  }

  // 2) Breakout + retest of an H1/M15 range edge.
  {
    const rangeHigh = fM15.rangeHigh;
    const rangeLow = fM15.rangeLow;
    if (rangeHigh && rangeLow && rangeHigh - rangeLow > atrM15 * 3) {
      const brokeUp = last.c > rangeHigh && prev.c <= rangeHigh;
      const retestUp = m5.slice(-3).some((c) => c.l <= rangeHigh * 1.0002 && c.c > rangeHigh);
      if ((brokeUp || retestUp) && fH1.structure !== "bear") {
        const stop = rangeHigh - atrM5 * 0.8;
        const entry = price;
        const risk = Math.max(entry - stop, atrM5 * 0.6);
        const target1 = entry + risk * 2;
        const target2 = entry + risk * 3.2;
        if (rrOk(entry, stop, target1)) {
          out.push({
            side: "BUY",
            setup: "BREAKOUT_RETEST",
            entryLow: Number(rangeHigh.toFixed(5)),
            entryHigh: Number((rangeHigh + atrM5 * 0.3).toFixed(5)),
            trigger: Number(rangeHigh.toFixed(5)),
            stop: Number((rangeHigh - risk).toFixed(5)),
            tp1: Number(target1.toFixed(5)),
            tp2: Number(target2.toFixed(5)),
            tp3: null,
            rr: Number((risk * 2 / risk).toFixed(2)),
            riskPips: Number(pips(entry, rangeHigh - risk).toFixed(1)),
            rewardPips: Number((risk * 2 / 0.0001).toFixed(1)),
            expectedHoldMin: 60,
            maxHoldMin: 240,
            context: baseContext(`пробій ${fmt(rangeHigh)} з ретестом`),
            why: [
              `Ціна пробила верхню межу діапазону M15 (${fmt(rangeHigh)}).`,
              "Ретест утримався, M5 закрився над рівнем.",
              "Ціль — продовження руху до наступної ліквідності.",
            ],
            ...commonRules("BUY", rangeHigh - risk),
          });
        }
      }
      const brokeDown = last.c < rangeLow && prev.c >= rangeLow;
      const retestDown = m5.slice(-3).some((c) => c.h >= rangeLow * 0.9998 && c.c < rangeLow);
      if ((brokeDown || retestDown) && fH1.structure !== "bull") {
        const stop = rangeLow + atrM5 * 0.8;
        const entry = price;
        const risk = Math.max(stop - entry, atrM5 * 0.6);
        const target1 = entry - risk * 2;
        const target2 = entry - risk * 3.2;
        if (rrOk(stop, entry, target1)) {
          out.push({
            side: "SELL",
            setup: "BREAKOUT_RETEST",
            entryLow: Number((rangeLow - atrM5 * 0.3).toFixed(5)),
            entryHigh: Number(rangeLow.toFixed(5)),
            trigger: Number(rangeLow.toFixed(5)),
            stop: Number((rangeLow + risk).toFixed(5)),
            tp1: Number(target1.toFixed(5)),
            tp2: Number(target2.toFixed(5)),
            tp3: null,
            rr: 2,
            riskPips: Number(pips(rangeLow + risk, entry).toFixed(1)),
            rewardPips: Number((risk * 2 / 0.0001).toFixed(1)),
            expectedHoldMin: 60,
            maxHoldMin: 240,
            context: baseContext(`пробій ${fmt(rangeLow)} з ретестом`),
            why: [
              `Ціна пробила нижню межу діапазону M15 (${fmt(rangeLow)}).`,
              "Ретест утримався, M5 закрився під рівнем.",
              "Ціль — продовження руху до наступної ліквідності.",
            ],
            ...commonRules("SELL", rangeLow + risk),
          });
        }
      }
    }
  }

  // 3) Trend continuation after displacement (M15 displacement + M5 pullback).
  {
    const disp = fM15.displacement;
    if (disp && fM5.structure !== "range") {
      const dirUp = disp.dir === "up";
      const aligned = dirUp ? fH1.structure !== "bear" : fH1.structure !== "bull";
      const pullback = dirUp
        ? m5.slice(-4).some((c) => c.c < prev.o) && last.c > prev.c
        : m5.slice(-4).some((c) => c.c > prev.o) && last.c < prev.c;
      if (aligned && pullback) {
        const entry = price;
        const stop = dirUp ? Math.min(...m5.slice(-6).map((c) => c.l)) - atrM5 * 0.3 : Math.max(...m5.slice(-6).map((c) => c.h)) + atrM5 * 0.3;
        const risk = Math.abs(entry - stop);
        const target1 = dirUp ? entry + risk * 1.8 : entry - risk * 1.8;
        const target2 = dirUp ? entry + risk * 2.8 : entry - risk * 2.8;
        if (risk > atrM5 * 0.5 && risk < atrM15 * 1.8) {
          out.push({
            side: dirUp ? "BUY" : "SELL",
            setup: "TREND_CONTINUATION",
            entryLow: Number((dirUp ? entry - atrM5 * 0.2 : entry - atrM5 * 0.1).toFixed(5)),
            entryHigh: Number((dirUp ? entry + atrM5 * 0.1 : entry + atrM5 * 0.2).toFixed(5)),
            trigger: Number(entry.toFixed(5)),
            stop: Number(stop.toFixed(5)),
            tp1: Number(target1.toFixed(5)),
            tp2: Number(target2.toFixed(5)),
            tp3: null,
            rr: 1.8,
            riskPips: Number((risk / 0.0001).toFixed(1)),
            rewardPips: Number((risk * 1.8 / 0.0001).toFixed(1)),
            expectedHoldMin: 90,
            maxHoldMin: 240,
            context: baseContext(`displacement ${disp.dir} x${disp.strength}, pullback завершено`),
            why: [
              `M15 показав displacement ${disp.dir} силою ${disp.strength}×ATR.`,
              "M5 відкотився і закрився в напрямку імпульсу.",
              `H1 ${fH1.structure} не суперечить напрямку.`,
            ],
            ...commonRules(dirUp ? "BUY" : "SELL", stop),
          });
        }
      }
    }
  }

  // 4) Pullback into a fresh M15 FVG, still valid (not fully filled before).
  {
    const fvgs = fM15.zones.filter((z) => z.kind === "FVG");
    for (const fvg of fvgs.slice(-3)) {
      const touched = m5.slice(-6).some((c) => c.l <= fvg.high && c.h >= fvg.low);
      if (!touched) continue;
      const dirUp = fvg.dir === "bull";
      const react = dirUp ? last.c > fvg.low && last.c < fvg.high + atrM5 : last.c < fvg.high && last.c > fvg.low - atrM5;
      const aligned = dirUp ? fH1.structure !== "bear" : fH1.structure !== "bull";
      if (react && aligned) {
        const entry = price;
        const stop = dirUp ? fvg.low - atrM5 * 0.4 : fvg.high + atrM5 * 0.4;
        const risk = Math.abs(entry - stop);
        const target1 = dirUp ? entry + risk * 2 : entry - risk * 2;
        const target2 = dirUp ? entry + risk * 3 : entry - risk * 3;
        if (risk > atrM5 * 0.4 && risk < atrM15 * 2) {
          out.push({
            side: dirUp ? "BUY" : "SELL",
            setup: "FVG_PULLBACK",
            entryLow: Number(fvg.low.toFixed(5)),
            entryHigh: Number(fvg.high.toFixed(5)),
            trigger: Number(entry.toFixed(5)),
            stop: Number(stop.toFixed(5)),
            tp1: Number(target1.toFixed(5)),
            tp2: Number(target2.toFixed(5)),
            tp3: null,
            rr: 2,
            riskPips: Number((risk / 0.0001).toFixed(1)),
            rewardPips: Number((risk * 2 / 0.0001).toFixed(1)),
            expectedHoldMin: 60,
            maxHoldMin: 210,
            context: baseContext(`FVG ${fvg.dir} ${fmt(fvg.low)}–${fmt(fvg.high)}`),
            why: [
              `Незаповнений FVG ${fvg.dir} на M15 (${fmt(fvg.low)}–${fmt(fvg.high)}).`,
              "Ціна повернулась у зону і показала реакцію на M5.",
              "Стоп за межами імбілансу, цілі за імпульсом.",
            ],
            ...commonRules(dirUp ? "BUY" : "SELL", stop),
          });
        }
      }
    }
  }

  // 5) Reversal from previous day extreme (only with M5 confirmation).
  {
    if (dayHigh !== null && Math.abs(price - dayHigh) < atrM15 * 0.6 && last.c < prev.o && fM5.structure === "bear") {
      const stop = Math.max(dayHigh + atrM5 * 0.5, last.h + atrM5 * 0.3);
      const entry = price;
      const risk = stop - entry;
      const target1 = entry - Math.max(risk * 1.8, dayRange * 0.35);
      if (risk > atrM5 * 0.4 && rrOk(stop, entry, target1)) {
        out.push({
          side: "SELL",
          setup: "REVERSAL_DAY_LEVEL",
          entryLow: Number((entry - atrM5 * 0.1).toFixed(5)),
          entryHigh: Number((entry + atrM5 * 0.2).toFixed(5)),
          trigger: Number(entry.toFixed(5)),
          stop: Number(stop.toFixed(5)),
          tp1: Number(target1.toFixed(5)),
          tp2: Number((entry - Math.max(risk * 2.6, dayRange * 0.5)).toFixed(5)),
          tp3: null,
          rr: Number((Math.max(risk * 1.8, dayRange * 0.35) / risk).toFixed(2)),
          riskPips: Number((risk / 0.0001).toFixed(1)),
          rewardPips: Number((Math.max(risk * 1.8, dayRange * 0.35) / 0.0001).toFixed(1)),
          expectedHoldMin: 75,
          maxHoldMin: 240,
          context: baseContext(`реакція від PDH ${fmt(dayHigh)}`),
          why: [
            `Ціна підійшла до Previous Day High (${fmt(dayHigh)}).`,
            "M5 показав відмову: закриття нижче тіла попередньої свічки.",
            "Стоп за екстремумом дня, ціль — середина денного діапазону.",
          ],
          ...commonRules("SELL", stop),
        });
      }
    }
    if (dayLow !== null && Math.abs(price - dayLow) < atrM15 * 0.6 && last.c > prev.o && fM5.structure === "bull") {
      const stop = Math.min(dayLow - atrM5 * 0.5, last.l - atrM5 * 0.3);
      const entry = price;
      const risk = entry - stop;
      const target1 = entry + Math.max(risk * 1.8, dayRange * 0.35);
      if (risk > atrM5 * 0.4 && rrOk(entry, stop, target1)) {
        out.push({
          side: "BUY",
          setup: "REVERSAL_DAY_LEVEL",
          entryLow: Number((entry - atrM5 * 0.2).toFixed(5)),
          entryHigh: Number((entry + atrM5 * 0.1).toFixed(5)),
          trigger: Number(entry.toFixed(5)),
          stop: Number(stop.toFixed(5)),
          tp1: Number(target1.toFixed(5)),
          tp2: Number((entry + Math.max(risk * 2.6, dayRange * 0.5)).toFixed(5)),
          tp3: null,
          rr: Number((Math.max(risk * 1.8, dayRange * 0.35) / risk).toFixed(2)),
          riskPips: Number((risk / 0.0001).toFixed(1)),
          rewardPips: Number((Math.max(risk * 1.8, dayRange * 0.35) / 0.0001).toFixed(1)),
          expectedHoldMin: 75,
          maxHoldMin: 240,
          context: baseContext(`реакція від PDL ${fmt(dayLow)}`),
          why: [
            `Ціна підійшла до Previous Day Low (${fmt(dayLow)}).`,
            "M5 показав відмову від рівня та повернення вгору.",
            "Стоп за екстремумом дня, ціль — середина денного діапазону.",
          ],
          ...commonRules("BUY", stop),
        });
      }
    }
  }

  return out;
}

function rrOk(stopBuy: number, entry: number, target: number): boolean {
  // For BUY: stop < entry < target. For SELL callers swap so target < entry < stop.
  const risk = Math.abs(entry - stopBuy);
  const reward = Math.abs(target - entry);
  return risk > 0 && reward / risk >= 1.4;
}

/** Transparent 0-100 score. Not a probability. */
export function scoreCandidate(
  c: Omit<SignalCandidate, "score" | "grade" | "scoreParts">,
  s: MarketSnapshot,
  session: SessionInfo,
  news: NewsRisk,
  settings: DataSettings,
): SignalCandidate["scoreParts"] {
  const parts: SignalCandidate["scoreParts"] = [];
  const fH4 = s.frames.H4;
  const fH1 = s.frames.H1;
  const fM5 = s.frames.M5;
  const wantBull = c.side === "BUY";

  // 1. Higher-timeframe alignment (25)
  let align = 0;
  if ((wantBull && fH4.structure === "bull") || (!wantBull && fH4.structure === "bear")) align += 12;
  if ((wantBull && fH1.structure === "bull") || (!wantBull && fH1.structure === "bear")) align += 13;
  else if (fH1.structure === "range") align += 6;
  parts.push({ label: "Відповідність H4/H1", points: align, max: 25, note: `H4 ${fH4.structure}, H1 ${fH1.structure}` });

  // 2. Liquidity present (15)
  const liq = liquidityTargets(fH1, s.quote?.mid ?? c.trigger);
  const liqTargets = wantBull ? liq.above.length : liq.below.length;
  const liqPts = liqTargets >= 2 ? 15 : liqTargets === 1 ? 10 : 0;
  parts.push({ label: "Очевидна ліквідність", points: liqPts, max: 15, note: `${liqTargets} структурна ціль у напрямку` });

  // 3. Confirmation (20)
  let conf = 0;
  const disp = s.frames.M15.displacement;
  if (disp && ((wantBull && disp.dir === "up") || (!wantBull && disp.dir === "down"))) conf += 12;
  if (fM5.structure === (wantBull ? "bull" : "bear")) conf += 8;
  parts.push({ label: "Підтвердження ціною", points: conf, max: 20, note: disp ? `M15 displacement ${disp.dir}` : "без displacement" });

  // 4. Volatility fit (10)
  const atrM15 = s.atr.M15;
  const atrM5 = s.atr.M5;
  const ratio = atrM5 > 0 && atrM15 > 0 ? atrM15 / atrM5 : 0;
  let volPts = 0;
  if (atrM15 >= 0.00045 && atrM15 <= 0.0022 && ratio > 0.9) volPts = 10;
  else if (atrM15 > 0.0003) volPts = 6;
  parts.push({ label: "Волатильність", points: volPts, max: 10, note: `ATR M15 ${(atrM15 / 0.0001).toFixed(1)} pips` });

  // 5. Not a random spike (10)
  let spikePts = 10;
  const lastCandles = s.candles.M5.slice(-1);
  const last = lastCandles[0];
  if (last && atrM5 > 0 && (last.h - last.l) > atrM5 * settings.atrSpikeMultiple) spikePts = 0;
  parts.push({ label: "Не spike", points: spikePts, max: 10, note: spikePts === 0 ? "аномальна свічка" : "нормальна амплітуда" });

  // 6. News risk (10)
  const newsPts = news.level === "NONE" ? 10 : news.level === "ELEVATED" ? 5 : 0;
  parts.push({ label: "Новинний ризик", points: newsPts, max: 10, note: news.level });

  // 7. Spread (5)
  const spread = s.quote?.spreadPips ?? 99;
  const spreadPts = spread <= 1.2 ? 5 : spread <= settings.maxSpreadPips ? 3 : 0;
  parts.push({ label: "Spread", points: spreadPts, max: 5, note: `${spread} pips` });

  // 8. Room to next obstacle (10)
  const obstacles = wantBull ? liq.below : liq.above;
  const room = Math.abs(c.tp1 - c.trigger);
  const blocking = obstacles.some((lvl) => Math.abs(lvl - c.trigger) < room * 0.6 && Math.abs(lvl - c.trigger) > 0.0001);
  parts.push({ label: "Простір до перешкоди", points: blocking ? 2 : 10, max: 10, note: blocking ? "перешкода ближче за TP1" : "чистий шлях" });

  // 9. Session fit (5)
  const sessionPts = session.quality === "PRIME" ? 5 : session.quality === "GOOD" ? 3 : 0;
  parts.push({ label: "Сесія", points: sessionPts, max: 5, note: `${session.active}${session.inKillzone ? " / killzone" : ""}` });

  // 10. Costs covered (5)
  const costCovered = c.riskPips > 0 && c.rewardPips / c.riskPips >= 1.6 && c.riskPips >= settings.maxSpreadPips * 2;
  parts.push({ label: "Покриття витрат", points: costCovered ? 5 : 0, max: 5, note: `R:R ${c.rr}` });

  return parts;
}

export function evaluate(
  s: MarketSnapshot,
  levels: Zone[],
  session: SessionInfo,
  news: NewsRisk,
  opts: { risk?: RiskSettings; data?: DataSettings; openSignals: Signal[]; signalsToday: number; dailyR: number },
): EvaluateResult {
  const risk = opts.risk ?? DEFAULT_RISK;
  const data = opts.data ?? DEFAULT_DATA;
  const details: string[] = [];
  const now = Date.now();

  const snapshotSummary = {
    bid: s.quote?.bid ?? null,
    ask: s.quote?.ask ?? null,
    spread: s.quote?.spreadPips ?? null,
    h4: s.frames.H4?.structure ?? "?",
    h1: s.frames.H1?.structure ?? "?",
    atrM15: s.atr.M15 ? Number((s.atr.M15 / 0.0001).toFixed(1)) : null,
  };

  const blocked = (reason: string): EvaluateResult => ({
    decision: { at: now, verdict: "BLOCKED", symbol: s.symbol, session: session.active, details: [reason, ...details], snapshotSummary },
    candidate: null,
  });

  if (s.dataQuality !== "VERIFIED") {
    details.push(...s.qualityNotes);
    return blocked(`Дані ${s.dataQuality}: сигнали заборонено.`);
  }
  if (session.isWeekend) return blocked("Ринок закритий (вихідні).");
  if (news.level === "BLOCKED") return blocked(news.reason);
  if ((s.quote?.spreadPips ?? 99) > data.maxSpreadPips) return blocked(`Spread ${s.quote?.spreadPips} pips перевищує ліміт ${data.maxSpreadPips}.`);
  if (opts.openSignals.length >= risk.maxOpenSetups) return blocked(`Уже ${opts.openSignals.length} активних сценарії(в) — ліміт ${risk.maxOpenSetups}.`);
  if (opts.signalsToday >= risk.maxSignalsPerDay) return blocked(`Денний ліміт сигналів ${risk.maxSignalsPerDay} вичерпано.`);
  if (opts.dailyR <= -risk.dailyLossLimitR) return blocked(`Денний ліміт збитку ${risk.dailyLossLimitR}R досягнуто.`);
  if (s.atr.M15 > 0 && s.atr.M15 / 0.0001 > 45) return blocked("Волатильність надмірна (ATR M15 > 45 pips) — ринок нестабільний.");

  const raw = detectSetups(s, levels);
  if (raw.length === 0) {
    details.push("Жоден із шести сетапів не має підтвердження на M15/M5.");
    return { decision: { at: now, verdict: "NO_TRADE", symbol: s.symbol, session: session.active, details, snapshotSummary }, candidate: null };
  }

  // Score every candidate, pick the best.
  const scored = raw
    .map((c) => {
      const parts = scoreCandidate(c, s, session, news, data);
      const score = Math.max(0, Math.min(100, parts.reduce((a, p) => a + p.points, 0)));
      return { ...c, scoreParts: parts, score };
    })
    .sort((a, b) => b.score - a.score);

  const best = scored[0];
  const grade: Signal["grade"] = best.score >= risk.minScoreAPlus ? "A+" : best.score >= risk.minScoreReady ? "A" : best.score >= 55 ? "B" : "NO_TRADE";
  const candidate: SignalCandidate = { ...best, grade };

  details.push(...best.scoreParts.map((p) => `${p.label}: ${p.points}/${p.max} (${p.note})`));

  if (grade === "B" || grade === "NO_TRADE") {
    details.push(`Найкращий сетап ${best.setup} отримав ${best.score}/100 — це лише спостереження, не сигнал.`);
    return { decision: { at: now, verdict: "NO_TRADE", symbol: s.symbol, session: session.active, details, snapshotSummary }, candidate };
  }

  if (best.rr < risk.minRr) {
    details.push(`R:R ${best.rr} нижче мінімуму ${risk.minRr}.`);
    return { decision: { at: now, verdict: "NO_TRADE", symbol: s.symbol, session: session.active, details, snapshotSummary }, candidate: { ...candidate, grade: "B" } };
  }

  // Don't chase: entry must still be reachable and price must not have run past TP1.
  const movedAway = Math.abs((s.quote?.mid ?? 0) - best.trigger) / 0.0001;
  if (movedAway > Math.max(6, best.riskPips * 0.45)) {
    details.push(`Ціна вже відійшла на ${movedAway.toFixed(1)} pips від тригера — не переслідуємо.`);
    return { decision: { at: now, verdict: "NO_TRADE", symbol: s.symbol, session: session.active, details, snapshotSummary }, candidate: { ...candidate, grade: "B" } };
  }

  return {
    decision: { at: now, verdict: "SIGNAL", symbol: s.symbol, session: session.active, details, snapshotSummary },
    candidate,
  };
}

export function newSignalId(now: number, side: string, setup: string): string {
  const bucket = dayBucket(now).key.replace(/-/g, "");
  const rand = Math.random().toString(36).slice(2, 6).toUpperCase();
  return `${bucket}-${side === "BUY" ? "L" : "S"}-${setup.slice(0, 4)}-${rand}`;
}

/** Keep the newest candidate per direction only if it is genuinely new. */
export function duplicateOfOpen(candidate: SignalCandidate, open: Signal[]): Signal | null {
  const match = open.find(
    (s) =>
      s.side === candidate.side &&
      (s.status === "READY" || s.status === "ACTIVE" || s.status === "WATCHLIST") &&
      Math.abs(s.trigger - candidate.trigger) / 0.0001 < 5,
  );
  return match ?? null;
}
