import type { Signal, SignalStatus } from "./types";

/**
 * Deterministic demo scenarios for UI verification only.
 * Clearly labelled, generated from a fixed pseudo-random walk — never presented as live results.
 */
function rng(seed: number) {
  let s = seed;
  return () => {
    s ^= s << 13;
    s ^= s >>> 17;
    s ^= s << 5;
    return ((s >>> 0) % 1_000_000) / 1_000_000;
  };
}

const SETUPS = ["LIQUIDITY_SWEEP_MSS", "BREAKOUT_RETEST", "TREND_CONTINUATION", "FVG_PULLBACK", "REVERSAL_DAY_LEVEL"] as const;

export function demoSignals(now = Date.now()): Signal[] {
  const rand = rng(20261123);
  const out: Signal[] = [];
  const day = 86_400_000;
  const statuses: SignalStatus[] = ["TP2_HIT", "SL_HIT", "TP1_HIT", "CANCELLED", "TP3_HIT", "SL_HIT", "TP2_HIT", "EXPIRED", "TP1_HIT", "SL_HIT", "TP3_HIT", "SL_HIT", "READY", "ACTIVE"];

  for (let i = 0; i < statuses.length; i++) {
    const status = statuses[i];
    const side = rand() > 0.45 ? "BUY" : "SELL";
    const dir = side === "BUY" ? 1 : -1;
    const ageDays = statuses.length - 1 - i;
    const createdAt = now - ageDays * day - Math.floor(rand() * 6) * 3_600_000;
    const entry = 1.16 + (rand() - 0.5) * 0.004;
    const riskPips = 9 + Math.floor(rand() * 12);
    const stop = entry - dir * riskPips * 0.0001;
    const tp1 = entry + dir * riskPips * 1.8 * 0.0001;
    const tp2 = entry + dir * riskPips * 3 * 0.0001;
    const tp3 = rand() > 0.5 ? entry + dir * riskPips * 4.2 * 0.0001 : null;
    const score = status === "READY" || status === "ACTIVE" ? 72 + Math.floor(rand() * 24) : 70 + Math.floor(rand() * 26);
    const grade = score >= 85 ? "A+" : "A";
    const setup = SETUPS[i % SETUPS.length];
    const rMap: Record<string, number | null> = {
      TP2_HIT: 3,
      TP3_HIT: 4.2,
      TP1_HIT: 1.8,
      SL_HIT: -1,
      EXPIRED: 0.35,
      CANCELLED: 0,
      READY: null,
      ACTIVE: null,
    };
    const resolved = !["READY", "ACTIVE"].includes(status);
    out.push({
      id: `DEMO-${String(i + 1).padStart(3, "0")}`,
      symbol: "EUR_USD",
      side,
      status,
      grade,
      score,
      scoreParts: [
        { label: "Відповідність H4/H1", points: 19, max: 25, note: "demo" },
        { label: "Ліквідність", points: 15, max: 15, note: "demo" },
        { label: "Підтвердження", points: 20, max: 20, note: "demo" },
        { label: "Волатильність", points: 8, max: 10, note: "demo" },
        { label: "Не spike", points: 10, max: 10, note: "demo" },
        { label: "Новинний ризик", points: 10, max: 10, note: "demo" },
        { label: "Spread", points: 5, max: 5, note: "demo" },
        { label: "Простір", points: 10, max: 10, note: "demo" },
        { label: "Сесія", points: 4, max: 5, note: "demo" },
        { label: "Покриття витрат", points: 5, max: 5, note: "demo" },
      ],
      setup,
      createdAt,
      updatedAt: createdAt + 40 * 60_000,
      entryLow: Number((entry - dir * 0.0001).toFixed(5)),
      entryHigh: Number((entry + dir * 0.0001).toFixed(5)),
      trigger: Number(entry.toFixed(5)),
      stop: Number(stop.toFixed(5)),
      tp1: Number(tp1.toFixed(5)),
      tp2: Number(tp2.toFixed(5)),
      tp3: tp3 !== null ? Number(tp3.toFixed(5)) : null,
      rr: 1.8,
      riskPips,
      rewardPips: Number((riskPips * 1.8).toFixed(1)),
      expectedHoldMin: 45,
      maxHoldMin: 240,
      session: rand() > 0.5 ? "London" : "Overlap",
      quoteAtCreation: Number(entry.toFixed(5)),
      spreadAtCreation: 1.2,
      provider: "DEMO — синтетичні дані",
      context: {
        h4: "H4 bull · BOS up",
        h1: "H1 bull · CHoCH up",
        m15: "M15 bull · displacement up x1.6",
        m5: "M5 reclaim + close",
        liquidity: "2 цілі вище, 1 нижче",
        levels: "PDH 1.16420 · PDL 1.15610",
      },
      why: [
        "Демонстраційний сценарій для перевірки інтерфейсу.",
        "Правила ті самі, що й у живому рушії, але ціни синтетичні.",
      ],
      cancelRules: ["Ціна закрила M5 за рівнем інвалідації до активації.", "Вийшла high-impact новина до входу."],
      invalidations: [`Закриття M5 ${side === "BUY" ? "нижче" : "вище"} ${stop.toFixed(5)} скасовує сценарій.`],
      newsRisk: "демо",
      positionSize: { lots: 0.3, riskAmount: 35, note: "демо-розрахунок" },
      resolvedAt: resolved ? createdAt + 95 * 60_000 : null,
      resolution: resolved ? { status, price: tp1, ts: createdAt + 95 * 60_000, note: "демо" } : null,
      rResult: rMap[status] ?? null,
    });
  }
  return out.reverse();
}
