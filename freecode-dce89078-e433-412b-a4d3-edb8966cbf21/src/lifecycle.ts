import { DEFAULT_RISK, pipSize, type RiskSettings } from "./config";
import type { Candle, Signal, SignalStatus } from "./types";

export type LifecycleEvent = {
  signal: Signal;
  newStatus: SignalStatus;
  price: number;
  ts: number;
  note: string;
};

/**
 * Advance a signal through its lifecycle using only completed M5 candles in
 * chronological order. Conservative: if one candle touches both SL and TP we
 * cannot know the intrabar order, so SL wins and the note says so.
 */
export function advanceSignal(signal: Signal, m5: Candle[], now: number, settings: RiskSettings = DEFAULT_RISK): LifecycleEvent | null {
  if (!["READY", "ACTIVE", "TP1_HIT", "TP2_HIT"].includes(signal.status)) return null;
  const pip = pipSize(signal.symbol);
  const isBuy = signal.side === "BUY";
  const created = signal.createdAt;
  const relevant = m5.filter((c) => c.t >= created && c.complete !== false);
  if (relevant.length === 0) {
    // Nothing new happened; expire only if it waited too long to be filled.
    if (signal.status === "READY" && now - signal.createdAt > 45 * 60_000) {
      return { signal, newStatus: "EXPIRED", price: signal.trigger, ts: now, note: "Вхід не активовано протягом 45 хв — сценарій знято." };
    }
    return null;
  }

  let status = signal.status;
  let hitTp1 = status === "TP1_HIT" || status === "TP2_HIT";
  let hitTp2 = status === "TP2_HIT";

  for (const c of relevant) {
    const touchedEntry = isBuy ? c.l <= signal.entryHigh : c.h >= signal.entryLow;
    if (status === "READY") {
      if (!touchedEntry) continue;
      status = "ACTIVE";
    }

    const hitStop = isBuy ? c.l <= signal.stop : c.h >= signal.stop;
    const hit1 = isBuy ? c.h >= signal.tp1 : c.l <= signal.tp1;
    const hit2 = isBuy ? c.h >= signal.tp2 : c.l <= signal.tp2;
    const hit3 = signal.tp3 !== null && (isBuy ? c.h >= signal.tp3 : c.l <= signal.tp3);

    if (hitStop && (hit1 || hit2) ) {
      const loss = isBuy ? (signal.stop - signal.entryHigh) / pip : (signal.entryLow - signal.stop) / pip;
      const r = -(Math.abs(loss) / Math.max(signal.riskPips, 0.1));
      return {
        signal,
        newStatus: "SL_HIT",
        price: signal.stop,
        ts: c.t,
        note: `Одна M5 свічка торкнулась і SL, і цілі — внутрішньосвічковий порядок невідомий, тому консервативно зараховано SL (${r.toFixed(2)}R).`,
      };
    }
    if (hitStop) {
      const r = -(Math.abs(isBuy ? signal.entryHigh - signal.stop : signal.stop - signal.entryLow) / pip / Math.max(signal.riskPips, 0.1));
      return { signal, newStatus: "SL_HIT", price: signal.stop, ts: c.t, note: `SL досягнуто: ${signal.stop.toFixed(5)} (${r.toFixed(2)}R).` };
    }
    if (hit3 && signal.tp3 !== null) {
      return { signal, newStatus: "TP3_HIT", price: signal.tp3, ts: c.t, note: "TP3 досягнуто — сценарій закрито повністю." };
    }
    if (hit2 && !hitTp2) {
      if (signal.tp3 === null) {
        return { signal, newStatus: "TP2_HIT", price: signal.tp2, ts: c.t, note: "TP2 досягнуто — фінальна ціль сценарію." };
      }
      hitTp2 = true;
      hitTp1 = true;
      if (status === "ACTIVE" || status === "READY") {
        return { signal, newStatus: "TP2_HIT", price: signal.tp2, ts: c.t, note: "TP2 досягнуто, залишок тримаємо до TP3." };
      }
    }
    if (hit1 && !hitTp1) {
      hitTp1 = true;
      return { signal, newStatus: "TP1_HIT", price: signal.tp1, ts: c.t, note: "TP1 досягнуто — рекомендовано перевести стоп у беззбиток." };
    }
  }

  // Time stop.
  if (now - (signal.resolvedAt ?? signal.createdAt) > signal.maxHoldMin * 60_000 && status !== "READY") {
    const last = relevant[relevant.length - 1]?.c ?? signal.trigger;
    const moved = isBuy ? (last - signal.trigger) / pip : (signal.trigger - last) / pip;
    const r = moved / Math.max(signal.riskPips, 0.1);
    return { signal, newStatus: "EXPIRED", price: last, ts: now, note: `Максимальний час утримання ${signal.maxHoldMin} хв вичерпано, зафіксовано ${r.toFixed(2)}R за останньою ціною.` };
  }
  if (status === "READY" && now - signal.createdAt > 45 * 60_000) {
    return { signal, newStatus: "CANCELLED", price: signal.trigger, ts: now, note: "Вхід не активовано за 45 хв — сценарій скасовано без угоди." };
  }

  return null;
}

/** Realized R for a final status, computed from the actual fill and exit. */
export function realizedR(signal: Signal, exitPrice: number): number {
  const isBuy = signal.side === "BUY";
  const entry = isBuy ? signal.entryHigh : signal.entryLow;
  const risk = Math.abs(entry - signal.stop);
  if (risk <= 0) return 0;
  const move = isBuy ? exitPrice - entry : entry - exitPrice;
  return Number((move / risk).toFixed(2));
}

export function applyEvent(signal: Signal, ev: LifecycleEvent, now: number): Signal {
  const next: Signal = {
    ...signal,
    status: ev.newStatus,
    updatedAt: now,
    resolution: { status: ev.newStatus, price: ev.price, ts: ev.ts, note: ev.note },
  };
  const terminal: SignalStatus[] = ["TP2_HIT", "TP3_HIT", "SL_HIT", "CANCELLED", "EXPIRED", "UNDETERMINED"];
  if (terminal.includes(ev.newStatus)) {
    next.resolvedAt = ev.ts;
    next.rResult = ev.newStatus === "CANCELLED" ? 0 : ev.newStatus === "UNDETERMINED" ? null : realizedR(signal, ev.price);
  }
  return next;
}

export function positionSize(signal: Signal, equity: number, settings: RiskSettings = DEFAULT_RISK): Signal["positionSize"] {
  const riskAmount = equity * (settings.riskPercent / 100);
  // EUR/USD: 1 standard lot = 10 USD per pip. Pip value per lot = 10.
  const lot = signal.riskPips > 0 ? riskAmount / (signal.riskPips * 10) : 0;
  return {
    lots: Number(lot.toFixed(3)),
    riskAmount: Number(riskAmount.toFixed(2)),
    note: `Розрахунок за ${settings.riskPercent}% від ${equity} USD, pip value 10 USD/lot, без урахування комісії брокера.`,
  };
}
