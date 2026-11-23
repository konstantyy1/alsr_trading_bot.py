import type { CalendarEvent } from "./types";

type Feed = { source: string; url: string; currency: "EUR" | "USD" | "OTHER"; defaultImpact: CalendarEvent["impact"] };

/**
 * Official, free, automated-accessible feeds. Everything else must be added
 * explicitly after checking its licence and rate limits.
 */
export const OFFICIAL_FEEDS: Feed[] = [
  { source: "ECB", url: "https://www.ecb.europa.eu/rss/press.html", currency: "EUR", defaultImpact: "medium" },
  { source: "Federal Reserve", url: "https://www.federalreserve.gov/feeds/press_all.xml", currency: "USD", defaultImpact: "medium" },
  { source: "BLS", url: "https://www.bls.gov/feed/bls_latest.rss", currency: "USD", defaultImpact: "high" },
  { source: "Eurostat", url: "https://ec.europa.eu/eurostat/api/dissemination/rss/news.xml", currency: "EUR", defaultImpact: "medium" },
];

const HIGH_IMPACT = [
  /cpi|consumer price|inflation/i,
  /pce|personal consumption/i,
  /nonfarm|non-farm|payroll|employment situation/i,
  /unemployment rate|jobless claims/i,
  /gdp|gross domestic product/i,
  /interest rate|rate decision|monetary policy decision/i,
  /fomc|federal open market/i,
  /pmi|purchasing managers/i,
  /retail sales/i,
  /ecb governing council|policy meeting/i,
];

const RELEVANT = [
  /euro area|eurozone|ecb|euro system/i,
  /united states|federal reserve|fomc|u\.?s\. posting/i,
  /cpi|inflation|pce|payroll|unemployment|gdp|pmi|retail sales|interest rate|monetary policy/i,
];

function decodeXml(s: string): string {
  return s
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function tag(block: string, name: string): string | null {
  const re = new RegExp(`<${name}[^>]*>([\\s\\S]*?)</${name}>`, "i");
  const m = block.match(re);
  return m ? decodeXml(m[1]) : null;
}

/** Minimal RSS/Atom parser. Returns published items only — never invents values. */
export function parseFeed(xml: string, feed: Feed): CalendarEvent[] {
  const blocks = xml.match(/<(item|entry)[\s\S]*?<\/(item|entry)>/gi) ?? [];
  const out: CalendarEvent[] = [];
  for (const block of blocks.slice(0, 25)) {
    const title = tag(block, "title");
    if (!title) continue;
    const dateRaw = tag(block, "pubDate") ?? tag(block, "published") ?? tag(block, "updated") ?? tag(block, "dc:date");
    const timeUtc = dateRaw ? Date.parse(dateRaw) : NaN;
    if (!Number.isFinite(timeUtc)) continue;

    const haystack = `${title} ${tag(block, "description") ?? ""} ${tag(block, "summary") ?? ""}`;
    if (!RELEVANT.some((re) => re.test(haystack))) continue;

    const linkMatch = block.match(/<link[^>]*href="([^"]+)"/i) ?? block.match(/<link>([\s\S]*?)<\/link>/i);
    const url = linkMatch ? decodeXml(linkMatch[1]).trim() : null;
    const impact: CalendarEvent["impact"] = HIGH_IMPACT.some((re) => re.test(title)) ? "high" : feed.defaultImpact;

    out.push({
      id: `${feed.source}:${timeUtc}:${title.slice(0, 60).replace(/\W+/g, "-").toLowerCase()}`,
      title,
      currency: feed.currency,
      impact,
      timeUtc,
      actual: null,
      forecast: null,
      previous: null,
      source: feed.source,
      url,
    });
  }
  return out;
}

export type NewsFetchResult = {
  events: CalendarEvent[];
  status: { source: string; ok: boolean; count: number; error: string | null }[];
};

/** Fetch all official feeds in parallel; a failing feed never breaks the rest. */
export async function fetchOfficialNews(): Promise<NewsFetchResult> {
  const results = await Promise.all(
    OFFICIAL_FEEDS.map(async (feed) => {
      try {
        const res = await fetch(feed.url, {
          headers: { "User-Agent": "fx-intel/1.0 (+private research; contact via repo)" , Accept: "application/rss+xml, application/xml, text/xml" },
          cf: { cacheTtl: 600 },
        } as RequestInit);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const xml = await res.text();
        const events = parseFeed(xml, feed);
        return { events, status: { source: feed.source, ok: true, count: events.length, error: null } };
      } catch (err) {
        return { events: [] as CalendarEvent[], status: { source: feed.source, ok: false, count: 0, error: (err as Error).message } };
      }
    }),
  );
  const events = results.flatMap((r) => r.events);
  // Dedupe by id and drop anything older than 7 days.
  const cutoff = Date.now() - 7 * 86_400_000;
  const seen = new Set<string>();
  const unique = events
    .filter((e) => e.timeUtc >= cutoff)
    .filter((e) => (seen.has(e.id) ? false : (seen.add(e.id), true)))
    .sort((a, b) => b.timeUtc - a.timeUtc);
  return { events: unique, status: results.map((r) => r.status) };
}

/**
 * Scheduled macro releases (calendar) that are known in advance.
 * Only used when a verifiable feed supplies them; otherwise the system says so.
 */
export function calendarNote(): string {
  return "Календарні значення (actual/forecast/previous) показуються лише якщо їх надав перевірений фід. Відсутні числа не підставляються.";
}
