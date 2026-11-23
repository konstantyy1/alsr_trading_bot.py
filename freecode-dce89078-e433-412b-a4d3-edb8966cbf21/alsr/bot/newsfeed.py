"""News intelligence from official, free, machine-readable feeds.

Only what the feed actually publishes is stored. If a feed does not provide
actual/forecast/previous, those fields stay empty instead of being invented.
"""
from __future__ import annotations

import re
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


def fetch_calendar(timeout: int = 15) -> tuple[list[dict], FeedStatus]:
    """Free weekly economic calendar with real actual/forecast/previous values.

    Only EUR and USD rows are kept. Empty strings stay empty — never coerced to 0.
    """
    url = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    try:
        rows = http_json(url, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return [], FeedStatus("Economic calendar", False, 0, str(exc))

    if not isinstance(rows, list):
        return [], FeedStatus("Economic calendar", False, 0, "некоректний формат")

    out: list[dict] = []
    for r in rows:
        country = str(r.get("country", "")).upper()
        if country not in ("EUR", "USD"):
            continue
        raw_date = r.get("date")
        ts = _parse_date(str(raw_date)) if raw_date else None
        if ts is None:
            continue
        title = str(r.get("title", "")).strip()
        if not title:
            continue
        impact_raw = str(r.get("impact", "")).lower()
        impact = "high" if impact_raw == "high" else "medium" if impact_raw == "medium" else "low"
        out.append({
            "id": f"cal:{country}:{int(ts.timestamp())}:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:40]}",
            "title": title,
            "currency": country,
            "impact": impact,
            "time_utc": ts,
            "source": "Economic calendar (free weekly feed)",
            "url": None,
            "actual": (r.get("actual") or "").strip() or None,
            "forecast": (r.get("forecast") or "").strip() or None,
            "previous": (r.get("previous") or "").strip() or None,
        })
    return out, FeedStatus("Economic calendar", True, len(out))


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
