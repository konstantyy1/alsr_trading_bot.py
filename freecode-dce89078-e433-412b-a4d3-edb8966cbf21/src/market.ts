import { DEFAULT_DATA, historyLimit, pipSize, type DataSettings, type Env, symbol } from "./config";
import { analyseFrame, keyLevels } from "./analysis";
import { BiQuoteProvider, MarketApiProvider, SyntheticProvider, validateCandles, type DataProvider } from "./providers";
import type { Candle, FrameAnalysis, MarketSnapshot, ProviderStatus, Quote, Tf } from "./types";

const TFS: Tf[] = ["M5", "M15", "H1", "H4"];

export type SnapshotResult = {
  snapshot: MarketSnapshot;
  raw: Record<Tf, Candle[]>;
  levels: ReturnType<typeof keyLevels>;
};

export function buildProviders(env: Env): { primary: DataProvider; validator: DataProvider | null; demo: boolean } {
  if (env.BIQUOTE_API_KEY) {
    return {
      primary: new BiQuoteProvider(env.BIQUOTE_API_KEY),
      validator: env.MARKETAPI_API_KEY ? new MarketApiProvider(env.MARKETAPI_API_KEY) : null,
      demo: false,
    };
  }
  return { primary: new SyntheticProvider(), validator: null, demo: true };
}

export async function buildSnapshot(env: Env, opts: { allowDemo?: boolean } = {}): Promise<SnapshotResult> {
  const sym = symbol(env);
  const pip = pipSize(sym);
  const settings: DataSettings = DEFAULT_DATA;
  const now = Date.now();
  const { primary, validator, demo } = buildProviders(env);
  if (demo && !opts.allowDemo) {
    throw new Error("Немає налаштованого джерела ринкових даних (BIQUOTE_API_KEY).");
  }

  const providerStatus: ProviderStatus[] = [];
  const qualityNotes: string[] = [];

  // Primary quote
  let quote: Quote | null = null;
  const t0 = Date.now();
  try {
    quote = await primary.fetchQuote(sym);
    providerStatus.push({ provider: primary.name, ok: true, latencyMs: Date.now() - t0, lastQuote: quote, error: null, checkedAt: now });
  } catch (err) {
    providerStatus.push({ provider: primary.name, ok: false, latencyMs: Date.now() - t0, lastQuote: null, error: (err as Error).message, checkedAt: now });
  }

  // Secondary cross-check (never merged into the signal feed)
  let secondary: Quote | null = null;
  let crossNote = validator ? "Валідатор не відповів." : "Друге джерело не налаштоване.";
  if (validator) {
    const t1 = Date.now();
    try {
      secondary = await validator.fetchQuote(sym);
      providerStatus.push({ provider: validator.name, ok: true, latencyMs: Date.now() - t1, lastQuote: secondary, error: null, checkedAt: now });
      crossNote = "Крос-перевірку виконано.";
    } catch (err) {
      providerStatus.push({ provider: validator.name, ok: false, latencyMs: Date.now() - t1, lastQuote: null, error: (err as Error).message, checkedAt: now });
    }
  }

  const deviationPips =
    quote && secondary ? Number((Math.abs(quote.mid - secondary.mid) / pip).toFixed(2)) : null;
  const withinTolerance = deviationPips !== null && deviationPips <= settings.maxCrossDeviationPips;
  if (deviationPips !== null && !withinTolerance) {
    qualityNotes.push(`Розбіжність джерел ${deviationPips} pips перевищує допуск ${settings.maxCrossDeviationPips}.`);
  }

  // Candles per timeframe
  const candles: Record<Tf, Candle[]> = { M5: [], M15: [], H1: [], H4: [] };
  const raw: Record<Tf, Candle[]> = { M5: [], M15: [], H1: [], H4: [] };
  const frames: Record<Tf, FrameAnalysis> = {} as Record<Tf, FrameAnalysis>;
  const limit = historyLimit(env);

  for (const tf of TFS) {
    try {
      const got = await primary.fetchCandles(sym, tf, limit);
      raw[tf] = got;
      const v = validateCandles(tf, got, now);
      candles[tf] = v.clean;
      qualityNotes.push(...v.notes);
    } catch (err) {
      qualityNotes.push(`${tf}: помилка завантаження — ${(err as Error).message}`);
    }
    frames[tf] = analyseFrame(tf, candles[tf]);
  }

  const quoteAgeSec = quote ? Math.max(0, Math.round((now - quote.ts) / 1000)) : Number.POSITIVE_INFINITY;
  const quoteFresh = quote ? quoteAgeSec <= settings.maxQuoteAgeSec && quote.live : false;
  if (quote && quoteAgeSec > settings.maxQuoteAgeSec) qualityNotes.push(`Котирування застаріле: ${quoteAgeSec} с (ліміт ${settings.maxQuoteAgeSec} с).`);

  let dataQuality: MarketSnapshot["dataQuality"] = "VERIFIED";
  if (!quote) dataQuality = "DATA UNAVAILABLE";
  else if (quoteAgeSec > settings.maxQuoteAgeSec) {
    dataQuality = "UNVERIFIED";
    qualityNotes.push(`Котирування застаріле: ${quoteAgeSec} с (ліміт ${settings.maxQuoteAgeSec} с).`);
  } else if (!quote.live) {
    dataQuality = "UNVERIFIED";
    qualityNotes.push(`Провайдер ${quote.provider} не підтвердив статус LIVE — ціна не верифікована.`);
  } else if (deviationPips !== null && !withinTolerance) {
    dataQuality = "UNVERIFIED";
  } else if (candles.M15.length < 60 || !frames.M15) {
    dataQuality = "UNVERIFIED";
    qualityNotes.push("Недостатньо свічок M15 для надійного аналізу.");
  }
  if (demo) {
    dataQuality = "UNVERIFIED";
    qualityNotes.push("Демо-потік (Synthetic): реальні сигнали вимкнено до підключення джерела.");
  }

  const levels = keyLevels(candles.H1, now);

  const snapshot: MarketSnapshot = {
    symbol: sym,
    quote,
    quoteAgeSec,
    quoteFresh,
    crossCheck: {
      available: Boolean(secondary),
      secondary,
      deviationPips,
      withinTolerance,
      note: crossNote,
    },
    candles,
    frames,
    atr: { M5: frames.M5?.atr ?? 0, M15: frames.M15?.atr ?? 0, H1: frames.H1?.atr ?? 0, H4: frames.H4?.atr ?? 0 },
    dataQuality,
    qualityNotes: [...new Set(qualityNotes)],
    providerStatus,
    generatedAt: now,
  };

  return { snapshot, raw, levels };
}
