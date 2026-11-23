import { sessionInfo, zonedTimeToUtc, newsRisk, isMarketClosed, inZone } from "./time";
import { computeStats } from "./stats";
import { advanceSignal, applyEvent } from "./lifecycle";
import { validateCandles } from "./providers";
import { analyseFrame } from "./analysis";
import { DEFAULT_RISK } from "./config";
import type { Candle, Signal } from "./types";

export type TestCase = { name: string; pass: boolean; detail: string };

function assert(name: string, cond: boolean, detail = ""): TestCase {
  return { name, pass: cond, detail };
}

function candle(t: number, o: number, h: number, l: number, c: number): Candle {
  return { t, o, h, l, c, v: 100, complete: true };
}

function baseSignal(over: Partial<Signal> = {}): Signal {
  return {
    id: "T-1",
    symbol: "EUR_USD",
    side: "BUY",
    status: "ACTIVE",
    grade: "A",
    score: 80,
    scoreParts: [],
    setup: "LIQUIDITY_SWEEP_MSS",
    createdAt: 0,
    updatedAt: 0,
    entryLow: 1.1,
    entryHigh: 1.1,
    trigger: 1.1,
    stop: 1.098,
    tp1: 1.104,
    tp2: 1.108,
    tp3: null,
    rr: 2,
    riskPips: 20,
    rewardPips: 40,
    expectedHoldMin: 45,
    maxHoldMin: 240,
    session: "London",
    quoteAtCreation: 1.1,
    spreadAtCreation: 1.2,
    provider: "test",
    context: { h4: "", h1: "", m15: "", m5: "", liquidity: "", levels: "" },
    why: [],
    cancelRules: [],
    invalidations: [],
    newsRisk: "",
    positionSize: { lots: 0, riskAmount: 0, note: "" },
    resolvedAt: null,
    resolution: null,
    rResult: null,
    ...over,
  };
}

export function runSelfTests(): { passed: number; failed: number; cases: TestCase[] } {
  const cases: TestCase[] = [];

  // --- DST correctness -------------------------------------------------------
  // London is UTC+0 in January and UTC+1 in July. An 08:00 London open must
  // therefore land on 08:00Z in winter and 07:00Z in summer.
  const winter = zonedTimeToUtc("Europe/London", 2026, 1, 15, 8, 0);
  const summer = zonedTimeToUtc("Europe/London", 2026, 7, 15, 8, 0);
  cases.push(assert("London 08:00 у січні = 08:00Z", inZone(winter, "UTC").hh === 8, `отримано ${inZone(winter, "UTC").hh}:00Z`));
  cases.push(assert("London 08:00 у липні = 07:00Z", inZone(summer, "UTC").hh === 7, `отримано ${inZone(summer, "UTC").hh}:00Z`));

  // New York: UTC-5 winter, UTC-4 summer.
  const nyWinter = zonedTimeToUtc("America/New_York", 2026, 1, 15, 8, 0);
  const nySummer = zonedTimeToUtc("America/New_York", 2026, 7, 15, 8, 0);
  cases.push(assert("NY 08:00 у січні = 13:00Z", inZone(nyWinter, "UTC").hh === 13, `отримано ${inZone(nyWinter, "UTC").hh}:00Z`));
  cases.push(assert("NY 08:00 у липні = 12:00Z", inZone(nySummer, "UTC").hh === 12, `отримано ${inZone(nySummer, "UTC").hh}:00Z`));

  // Warsaw follows CET/CEST; 12:00 local must be 11:00Z in winter, 10:00Z in summer.
  const wawWinter = zonedTimeToUtc("Europe/Warsaw", 2026, 1, 15, 12, 0);
  const wawSummer = zonedTimeToUtc("Europe/Warsaw", 2026, 7, 15, 12, 0);
  cases.push(assert("Варшава 12:00 у січні = 11:00Z", inZone(wawWinter, "UTC").hh === 11, `отримано ${inZone(wawWinter, "UTC").hh}:00Z`));
  cases.push(assert("Варшава 12:00 у липні = 10:00Z", inZone(wawSummer, "UTC").hh === 10, `отримано ${inZone(wawSummer, "UTC").hh}:00Z`));

  // --- Session detection -----------------------------------------------------
  const lonMorning = zonedTimeToUtc("Europe/London", 2026, 3, 10, 9, 0); // 09:00 London, Tuesday
  const s1 = sessionInfo(lonMorning);
  cases.push(assert("09:00 London → London killzone", s1.active === "London" && s1.inKillzone && s1.quality === "PRIME", `${s1.active} / ${s1.quality}`));

  const overlap = zonedTimeToUtc("Europe/London", 2026, 3, 10, 14, 0); // 14:00 London = 10:00 NY
  const s2 = sessionInfo(overlap);
  cases.push(assert("14:00 London → Overlap", s2.active === "Overlap", s2.active));

  const sat = zonedTimeToUtc("Europe/London", 2026, 3, 14, 12, 0); // Saturday
  cases.push(assert("Субота → ринок закритий", isMarketClosed(sat) && sessionInfo(sat).isWeekend, "weekend flag"));
  const sunLate = zonedTimeToUtc("Europe/London", 2026, 3, 15, 23, 0); // Sunday 23:00 London, market reopens
  cases.push(assert("Неділя 23:00 London → ринок відкритий", !isMarketClosed(sunLate), "reopen after 22:00"));

  // --- News blackout ---------------------------------------------------------
  const now = 1_700_000_000_000;
  const inTen = now + 10 * 60_000;
  const high = [{ id: "e1", title: "US CPI", currency: "USD" as const, impact: "high" as const, timeUtc: inTen, actual: null, forecast: null, previous: null, source: "BLS", url: null }];
  const blocked = newsRisk(high, now, 20, 15);
  cases.push(assert("High-impact за 10 хв → BLOCKED", blocked.level === "BLOCKED", blocked.reason));

  const far = now + 120 * 60_000;
  const elevated = newsRisk([{ ...high[0], timeUtc: far }], now, 20, 15);
  cases.push(assert("High-impact за 2 год → ELEVATED", elevated.level === "ELEVATED", elevated.reason));

  const justReleased = newsRisk([{ ...high[0], timeUtc: now - 5 * 60_000 }], now, 20, 15);
  cases.push(assert("Реліз 5 хв тому → BLOCKED (стабілізація)", justReleased.level === "BLOCKED", justReleased.reason));

  const mediumOnly = newsRisk([{ ...high[0], impact: "medium", timeUtc: inTen }], now, 20, 15);
  cases.push(assert("Medium-impact не блокує", mediumOnly.level === "NONE", mediumOnly.level));

  // --- Lifecycle: TP then SL order ------------------------------------------
  const sig = baseSignal();
  const tpCandle = candle(60_000, 1.1, 1.1045, 1.0995, 1.104);
  const tpEv = advanceSignal({ ...sig, status: "READY" }, [tpCandle], 120_000, DEFAULT_RISK);
  cases.push(assert("TP1 досягнуто за реальною свічкою", tpEv?.newStatus === "TP1_HIT", tpEv?.newStatus ?? "null"));

  const slCandle = candle(60_000, 1.1, 1.1005, 1.096, 1.0975);
  const slEv = advanceSignal({ ...sig, status: "READY" }, [slCandle], 120_000, DEFAULT_RISK);
  cases.push(assert("SL досягнуто → від'ємний R", slEv?.newStatus === "SL_HIT" && (applyEvent(sig, slEv!, 120_000).rResult ?? 0) < 0, slEv?.newStatus ?? "null"));

  const both = candle(60_000, 1.1, 1.1045, 1.096, 1.1);
  const bothEv = advanceSignal({ ...sig, status: "READY" }, [both], 120_000, DEFAULT_RISK);
  cases.push(assert("SL і TP в одній свічці → консервативно SL", bothEv?.newStatus === "SL_HIT", bothEv?.newStatus ?? "null"));

  // --- Stats maths -----------------------------------------------------------
  const closed: Signal[] = [
    baseSignal({ id: "a", status: "TP1_HIT", resolvedAt: 1, rResult: 2 }),
    baseSignal({ id: "b", status: "SL_HIT", resolvedAt: 2, rResult: -1 }),
    baseSignal({ id: "c", status: "TP1_HIT", resolvedAt: 3, rResult: 1.8 }),
    baseSignal({ id: "d", status: "CANCELLED", resolvedAt: 4, rResult: 0 }),
  ];
  const stats = computeStats(closed);
  cases.push(assert("Win rate 2/3 без скасованих", stats.winRate !== null && Math.abs(stats.winRate - 2 / 3) < 0.001, String(stats.winRate)));
  cases.push(assert("Expectancy = 0.933R", stats.expectancyR !== null && Math.abs(stats.expectancyR - 0.933) < 0.01, String(stats.expectancyR)));
  cases.push(assert("Profit factor 3.8", stats.profitFactor === 3.8, String(stats.profitFactor)));
  cases.push(assert("Max drawdown 1R", Math.abs(stats.maxDrawdownR - 1) < 0.001, String(stats.maxDrawdownR)));

  // --- Data validation -------------------------------------------------------
  const step = 300_000;
  const good: Candle[] = Array.from({ length: 100 }, (_, i) => candle(i * step, 1.1, 1.1005, 1.0995, 1.1 + (i % 3) * 0.0001));
  const vGood = validateCandles("M5", good, 99 * step + 60_000);
  cases.push(assert("Валідні свічки проходять перевірку", vGood.ok, vGood.notes.join("; ") || "ok"));

  const withGaps = good.filter((_, i) => i % 10 !== 0);
  const vGaps = validateCandles("M5", withGaps, 99 * step + 60_000);
  cases.push(assert("Пропуски фіксуються", vGaps.notes.some((n) => n.includes("пропуск")), vGaps.notes.join("; ")));

  const stale = validateCandles("M5", good, 99 * step + 3_600_000);
  cases.push(assert("Застарілі дані не проходять", !stale.ok && stale.notes.some((n) => n.includes("застарілі")), stale.notes.join("; ")));

  const future = [...good, candle(200 * step, 1.1, 1.2, 1.0, 1.1)];
  const vFuture = validateCandles("M5", future, 99 * step + 60_000);
  cases.push(assert("Майбутні свічки відкидаються", vFuture.clean.length === good.length, `clean=${vFuture.clean.length}`));

  // --- Structure engine ------------------------------------------------------
  const uptrend: Candle[] = [];
  let p = 1.1;
  for (let i = 0; i < 120; i++) {
    const next = p + 0.00035 + Math.sin(i / 3) * 0.0002;
    uptrend.push(candle(i * step, p, Math.max(p, next) + 0.00008, Math.min(p, next) - 0.00008, next));
    p = next;
  }
  const frame = analyseFrame("M15", uptrend);
  cases.push(assert("Висхідний ряд → структура bull", frame.structure === "bull", frame.structure));
  cases.push(assert("ATR обчислено", frame.atr > 0, frame.atr.toFixed(6)));

  const passed = cases.filter((c) => c.pass).length;
  return { passed, failed: cases.length - passed, cases };
}
