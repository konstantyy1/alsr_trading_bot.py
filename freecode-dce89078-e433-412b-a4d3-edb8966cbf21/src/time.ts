import { INTERNAL_TZ, LONDON_TZ, NY_TZ } from "./config";
import type { CalendarEvent, NewsRisk, SessionInfo, SessionName } from "./types";

/** Format a UTC instant in a named IANA zone. Never uses a fixed UTC offset. */
export function inZone(ts: number, tz: string): { y: number; m: number; d: number; hh: number; mm: number; weekday: number; text: string } {
  const dtf = new Intl.DateTimeFormat("en-GB", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    weekday: "short",
  });
  const parts: Record<string, string> = {};
  for (const p of dtf.formatToParts(new Date(ts))) parts[p.type] = p.value;
  const weekdayMap: Record<string, number> = { Sun: 0, Mon: 1, Tue: 2, Wed: 3, Thu: 4, Fri: 5, Sat: 6 };
  return {
    y: Number(parts.year),
    m: Number(parts.month),
    d: Number(parts.day),
    hh: Number(parts.hour) % 24,
    mm: Number(parts.minute),
    weekday: weekdayMap[parts.weekday] ?? 0,
    text: `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`,
  };
}

function zoneOffsetMinutes(ts: number, tz: string): number {
  const z = inZone(ts, tz);
  const asUtc = Date.UTC(z.y, z.m - 1, z.d, z.hh, z.mm);
  const floored = Math.floor(ts / 60000) * 60000;
  return Math.round((asUtc - floored) / 60000);
}

/**
 * Resolve a wall-clock time in a zone to a UTC instant, tolerant to DST gaps/overlaps
 * (two-pass offset resolution, standard technique).
 */
export function zonedTimeToUtc(tz: string, y: number, m: number, d: number, hh: number, mm = 0): number {
  const guess = Date.UTC(y, m - 1, d, hh, mm);
  const o1 = zoneOffsetMinutes(guess, tz);
  const first = guess - o1 * 60000;
  const o2 = zoneOffsetMinutes(first, tz);
  return guess - o2 * 60000;
}

export function warsawClock(ts: number): string {
  const z = inZone(ts, INTERNAL_TZ);
  return `${String(z.hh).padStart(2, "0")}:${String(z.mm).padStart(2, "0")}`;
}

export function warsawDateTime(ts: number): string {
  const z = inZone(ts, INTERNAL_TZ);
  return `${z.y}-${String(z.m).padStart(2, "0")}-${String(z.d).padStart(2, "0")} ${String(z.hh).padStart(2, "0")}:${String(z.mm).padStart(2, "0")}`;
}

function minutesOfDay(tz: string, ts: number): number {
  const z = inZone(ts, tz);
  return z.hh * 60 + z.mm;
}

/** Next instant (>= now) whose London wall clock reaches hh:mm. */
function nextZonedTime(tz: string, now: number, hh: number, mm: number, weekdayOnly = false): number {
  for (let dayOffset = 0; dayOffset < 8; dayOffset++) {
    const z = inZone(now + dayOffset * 86_400_000, tz);
    let target = zonedTimeToUtc(tz, z.y, z.m, z.d, hh, mm);
    if (target <= now) target += 86_400_000;
    if (weekdayOnly) {
      const wd = inZone(target, tz).weekday;
      if (wd === 0) target += 86_400_000;
      if (wd === 6) target += 2 * 86_400_000;
    }
    if (target > now) return target;
  }
  return now + 86_400_000;
}

export function sessionInfo(now = Date.now()): SessionInfo {
  const lon = inZone(now, LONDON_TZ);
  const ny = inZone(now, NY_TZ);
  const waw = inZone(now, INTERNAL_TZ);
  const notes: string[] = [];

  // Weekend: FX closed from Fri 22:00 London to Sun 22:00 London.
  const londonMin = lon.hh * 60 + lon.mm;
  const isWeekend = lon.weekday === 6 || (lon.weekday === 0 && londonMin < 22 * 60) || (lon.weekday === 5 && londonMin >= 22 * 60);
  if (isWeekend) notes.push("Ринок закритий на вихідні або вже після rollover п'ятниці.");

  // London 08:00-17:00 local, New York 08:00-17:00 local, both DST-aware.
  const lonOpen = 8 * 60;
  const lonClose = 17 * 60;
  const nyOpen = 8 * 60;
  const nyClose = 17 * 60;
  const nyMin = ny.hh * 60 + ny.mm;

  const inLondon = londonMin >= lonOpen && londonMin < lonClose;
  const inNy = nyMin >= nyOpen && nyMin < nyClose;

  let active: SessionName = "Off-session";
  if (inLondon && inNy) active = "Overlap";
  else if (inLondon) active = "London";
  else if (inNy) active = "New York";
  else if (londonMin >= 0 && londonMin < lonOpen) active = "Asia";

  const lonOpenTs = nextZonedTime(LONDON_TZ, now, 8, 0, true);
  const nyOpenTs = nextZonedTime(NY_TZ, now, 8, 0, true);
  const toMin = (ts: number) => Math.max(0, Math.round((ts - now) / 60000));

  // Killzones: London 08:00-11:00, NY 13:00-16:00 London time (overlap window).
  let killzoneLabel: string | null = null;
  let inKillzone = false;
  if (londonMin >= 8 * 60 && londonMin < 11 * 60) {
    inKillzone = true;
    killzoneLabel = "London killzone (08:00-11:00)";
  } else if (londonMin >= 13 * 60 && londonMin < 16 * 60) {
    inKillzone = true;
    killzoneLabel = "NY / overlap killzone (13:00-16:00)";
  }

  let quality: SessionInfo["quality"] = "LOW";
  if (inKillzone) quality = "PRIME";
  else if (active === "London" || active === "New York" || active === "Overlap") quality = "GOOD";
  if (waw.weekday === 5 && londonMin >= 16 * 60) {
    notes.push("Пізня п'ятниця: ліквідність тоншає перед закриттям тижня.");
    if (quality === "PRIME") quality = "GOOD";
  }

  return {
    active,
    warsawTime: warsawClock(now),
    warsawDate: `${waw.y}-${String(waw.m).padStart(2, "0")}-${String(waw.d).padStart(2, "0")}`,
    londonOpenInMin: inLondon ? null : toMin(lonOpenTs),
    nyOpenInMin: inNy ? null : toMin(nyOpenTs),
    overlapInMin: inLondon && inNy ? null : toMin(lonOpenTs) < toMin(nyOpenTs) ? 0 : null,
    isWeekend,
    inKillzone,
    killzoneLabel,
    quality,
    notes,
  };
}

/** True if the given instant falls in the FX weekend window. */
export function isMarketClosed(ts: number): boolean {
  const lon = inZone(ts, LONDON_TZ);
  const min = lon.hh * 60 + lon.mm;
  if (lon.weekday === 6) return true;
  if (lon.weekday === 0 && min < 22 * 60) return true;
  if (lon.weekday === 5 && min >= 22 * 60) return true;
  return false;
}

/**
 * News risk gate. Conservative: blocks new entries around high-impact EUR/USD events.
 */
export function newsRisk(events: CalendarEvent[], now: number, beforeMin: number, afterMin: number): NewsRisk {
  const relevant = events
    .filter((e) => (e.currency === "EUR" || e.currency === "USD") && e.impact === "high")
    .sort((a, b) => a.timeUtc - b.timeUtc);

  const upcoming = relevant.find((e) => e.timeUtc >= now);
  const minutesToNext = upcoming ? Math.round((upcoming.timeUtc - now) / 60000) : null;
  const recent = relevant.filter((e) => e.timeUtc < now && now - e.timeUtc <= afterMin * 60000);

  if (recent.length > 0) {
    return {
      level: "BLOCKED",
      reason: `Щойно вийшов реліз «${recent[recent.length - 1].title}» — потрібна стабілізація волатильності.`,
      nextEvent: upcoming ?? null,
      minutesToNext,
      recentReleases: recent,
    };
  }
  if (upcoming && minutesToNext !== null && minutesToNext <= beforeMin) {
    return {
      level: "BLOCKED",
      reason: `За ${minutesToNext} хв до релізу «${upcoming.title}» — нові входи заборонені.`,
      nextEvent: upcoming,
      minutesToNext,
      recentReleases: [],
    };
  }
  if (upcoming && minutesToNext !== null && minutesToNext <= beforeMin * 3) {
    return {
      level: "ELEVATED",
      reason: `Попереду «${upcoming.title}» через ${minutesToNext} хв — тримай ризик нижче.`,
      nextEvent: upcoming,
      minutesToNext,
      recentReleases: [],
    };
  }
  return { level: "NONE", reason: "Найближчих high-impact подій EUR/USD немає.", nextEvent: upcoming ?? null, minutesToNext, recentReleases: [] };
}

/** Daily / weekly candle bucket boundaries in the internal Warsaw zone. */
export function dayBucket(ts: number): { start: number; end: number; key: string } {
  const z = inZone(ts, INTERNAL_TZ);
  const start = zonedTimeToUtc(INTERNAL_TZ, z.y, z.m, z.d, 0, 0);
  return { start, end: start + 86_400_000, key: `${z.y}-${String(z.m).padStart(2, "0")}-${String(z.d).padStart(2, "0")}` };
}

export function weekBucket(ts: number): { start: number; end: number; key: string } {
  const z = inZone(ts, INTERNAL_TZ);
  const dow = z.weekday === 0 ? 7 : z.weekday; // Monday = 1
  const mondayTs = dayBucket(ts).start - (dow - 1) * 86_400_000;
  const monday = inZone(mondayTs, INTERNAL_TZ);
  const start = zonedTimeToUtc(INTERNAL_TZ, monday.y, monday.m, monday.d, 0, 0);
  const end = start + 7 * 86_400_000;
  return { start, end, key: `${monday.y}-W${String(Math.ceil(((start - Date.UTC(monday.y, 0, 1)) / 86_400_000 + 1) / 7)).padStart(2, "0")}` };
}
