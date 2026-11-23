"""News intelligence from official, free, machine-readable feeds.

Only what the feed actually publishes is stored. If a feed does not provide
actual/forecast/previous, those fields stay empty instead of being invented.
"""
from __future__ import annotations

import re
import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

from datafeed import HEADERS, FeedError, http_json

OFFICIAL_FEEDS = [
    ("ECB", "https://www.ecb.europa.eu/rss/press.html", "EUR"),
    ("Federal Reserve", "https://www.federalreserve.gov/feeds/press_all.xml", "USD"),
    ("BLS", "https://www.bls.gov/feed/bls_latest.rss", "USD"),
]

RELEVANT = re.compile(
    r"euro area|eurozone|ecb|eurosystem|federal reserve|fomc|united states|u\.?s\.|"
    r"cpi|inflation|pce|payroll|employment|unemployment|gdp|pmi|retail sales|"
    r"interest rate|monetary policy|rate decision",
    re.IGNORECASE,
)

HIGH = re.compile(
    r"cpi|consumer price|pce|payroll|employment situation|unemployment|gdp|"
    r"interest rate|rate decision|fomc|monetary policy decision|pmi|retail sales|"
    r"ecb governing council|policy meeting",
    re.IGNORECASE,
)


@dataclass
class FeedStatus:
    source: str
    ok: bool
    count: int
    error: str | None = None


def _text(node) -> str:
    return " ".join("".join(node.itertext()).split())


def parse_feed(xml_text: str, source: str, currency: str) -> list[dict]:
    out: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out

    items = [el for el in root.iter() if el.tag.split("}")[-1] in ("item", "entry")]
    for item in items[:40]:
        fields: dict[str, str] = {}
        link = ""
        for child in item:
            tag = child.tag.split("}")[-1]
            if tag == "link":
                link = child.attrib.get("href") or child.text or ""
            else:
                fields.setdefault(tag, _text(child))

        title = fields.get("title", "").strip()
        if not title:
            continue
        haystack = f"{title} {fields.get('description', '')} {fields.get('summary', '')}"
        if not RELEVANT.search(haystack):
            continue

        raw_date = fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date")
        if not raw_date:
            continue
        ts = _parse_date(raw_date)
        if ts is None:
            continue

        out.append({
            "id": f"{source}:{int(ts.timestamp())}:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:50]}",
            "title": title,
            "currency": currency,
            "impact": "high" if HIGH.search(title) else "medium",
            "time_utc": ts,
            "source": source,
            "url": link.strip() or None,
            "actual": None,
            "forecast": None,
            "previous": None,
        })
    return out


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


CAL_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CAL_XML = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
BROWSER_HEADERS = {
    **HEADERS,
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def _cal_row(title: str, country: str, ts: datetime, impact_raw: str,
             actual, forecast, previous, url=None) -> dict | None:
    title = (title or "").strip()
    if not title or ts is None:
        return None
    impact_raw = (impact_raw or "").lower()
    impact = "high" if impact_raw == "high" else "medium" if impact_raw == "medium" else "low"
    return {
        "id": f"cal:{country}:{int(ts.timestamp())}:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:40]}",
        "title": title,
        "currency": country,
        "impact": impact,
        "time_utc": ts,
        "source": "Economic calendar (free weekly feed)",
        "url": url,
        "actual": (actual or "").strip() or None,
        "forecast": (forecast or "").strip() or None,
        "previous": (previous or "").strip() or None,
    }


def _fetch_calendar_json(timeout: int) -> tuple[list[dict], str | None]:
    req = urllib.request.Request(CAL_JSON, headers={**BROWSER_HEADERS, "Accept": "application/json, text/plain, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        rows = json.loads(res.read().decode("utf-8", errors="replace"))
    if not isinstance(rows, list):
        raise ValueError("некоректний формат JSON")
    out = []
    for r in rows:
        country = str(r.get("country", "")).upper()
        if country not in ("EUR", "USD"):
            continue
        raw_date = r.get("date")
        if not raw_date:
            continue
        row = _cal_row(r.get("title", ""), country, _parse_date(str(raw_date)), r.get("impact", ""),
                       r.get("actual"), r.get("forecast"), r.get("previous"))
        if row:
            out.append(row)
    return out, None


def _fetch_calendar_xml(timeout: int) -> tuple[list[dict], str | None]:
    """Same weekly feed in XML. The host throttles the JSON path harder, and the
    XML file carries identical events, so this is the fallback that keeps the
    news gate working instead of blocking every entry for days."""
    req = urllib.request.Request(CAL_XML, headers={**BROWSER_HEADERS, "Accept": "application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read()
    text = raw.decode("windows-1252", errors="replace")
    root = ET.fromstring(text)
    out = []
    for ev in root.findall("event"):
        country = (ev.findtext("country") or "").strip().upper()
        if country not in ("EUR", "USD"):
            continue
        ts = _parse_xml_time(ev.findtext("date") or "", ev.findtext("time") or "")
        row = _cal_row(ev.findtext("title") or "", country, ts, ev.findtext("impact") or "",
                       ev.findtext("actual"), ev.findtext("forecast"), ev.findtext("previous"),
                       (ev.findtext("url") or "").strip() or None)
        if row:
            out.append(row)
    return out, None


def _parse_xml_time(date_s: str, time_s: str) -> datetime | None:
    """XML dates look like `11-22-2026` + `9:45pm`, both in UTC (verified against
    the JSON feed for every EUR/USD row of the same week)."""
    m = re.match(r"(\d{1,2})-(\d{1,2})-(\d{4})", date_s.strip())
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    tm = re.match(r"(\d{1,2}):(\d{2})\s*([ap])m", time_s.strip(), re.IGNORECASE)
    if not tm:
        return None
    hour, minute, half = int(tm.group(1)), int(tm.group(2)), tm.group(3).lower()
    if half == "p" and hour != 12:
        hour += 12
    elif half == "a" and hour == 12:
        hour = 0
    try:
        return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        return None


def fetch_calendar(timeout: int = 15) -> tuple[list[dict], FeedStatus]:
    """Free weekly economic calendar with real actual/forecast/previous values.

    Only EUR and USD rows are kept. Empty strings stay empty — never coerced to 0.
    The host rate-limits by IP and blocks whole ranges, so callers must cache the
    result; a 429 is reported honestly instead of being retried in a loop. JSON is
    tried first, then the identical XML feed, which survives throttling longer.
    """
    problems: list[str] = []
    for fetcher in (_fetch_calendar_json, _fetch_calendar_xml):
        try:
            events, _ = fetcher(timeout)
            return events, FeedStatus("Economic calendar", True, len(events))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                problems.append("HTTP 429 (тимчасовий ліміт запитів до безкоштовного фіду)")
            else:
                problems.append(f"HTTP {exc.code}")
        except ET.ParseError:
            problems.append("некоректний XML")
        except Exception as exc:  # noqa: BLE001
            problems.append(str(exc)[:80])

    detail = problems[0] if problems else "невідома помилка"
    if len(problems) > 1:
        detail += " (обидва формати фіду недоступні)"
    return [], FeedStatus("Economic calendar", False, 0, detail)


def fetch_news(timeout: int = 12) -> tuple[list[dict], list[FeedStatus]]:
    import urllib.request

    events: list[dict] = []
    statuses: list[FeedStatus] = []
    for source, url, currency in OFFICIAL_FEEDS:
        try:
            req = urllib.request.Request(url, headers={**HEADERS, "Accept": "application/rss+xml, application/xml, text/xml"})
            with urllib.request.urlopen(req, timeout=timeout) as res:
                xml_text = res.read().decode("utf-8", errors="replace")
            parsed = parse_feed(xml_text, source, currency)
            events.extend(parsed)
            statuses.append(FeedStatus(source, True, len(parsed)))
        except Exception as exc:  # noqa: BLE001
            statuses.append(FeedStatus(source, False, 0, str(exc)))

    cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
    seen: set[str] = set()
    unique = []
    for e in sorted(events, key=lambda x: x["time_utc"], reverse=True):
        if e["time_utc"].timestamp() < cutoff or e["id"] in seen:
            continue
        seen.add(e["id"])
        unique.append(e)
    return unique, statuses


def _safe_json(url: str) -> dict | None:
    try:
        return http_json(url)
    except FeedError:
        return None
