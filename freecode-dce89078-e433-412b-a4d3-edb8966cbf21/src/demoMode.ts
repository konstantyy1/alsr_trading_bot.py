import type { Env } from "./config";
import { demoSignals } from "./demo";

/**
 * Demo rows are generated in memory and clearly labelled. They are only used when no
 * live feed is configured, so the interface and signal cards can be inspected without
 * pretending to be live trading results.
 */
export function demoMode(env: Env): boolean {
  return !env.BIQUOTE_API_KEY;
}

export function demoSignalSet(env: Env) {
  return demoMode(env) ? demoSignals() : [];
}
