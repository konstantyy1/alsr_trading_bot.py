"""Time, session and news-window logic.

Sessions are computed from real IANA zones so DST is handled by the system, never
with hardcoded UTC offsets.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import INTERNAL_TZ, LONDON_TZ, NY_TZ

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

WAW = ZoneInfo(INTERNAL_TZ)
LON = ZoneInfo(LONDON_TZ)
NY = ZoneInfo(NY_TZ)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SessionInfo:
    active: str
    warsaw_time: str
    warsaw_date: str
    london_open_in_min: int | None
    ny_open_in_min: int | None
    is_weekend: bool
    in_killzone: bool
    killzone_label: str | None
    quality: str
    notes: tuple[str, ...] = ()


def warsaw(ts: datetime | None = None) -> datetime:
    return (ts or utcnow()).astimezone(WAW)


def warsaw_str(ts: datetime | None = None) -> str:
    return warsaw(ts).strftime("%Y-%m-%d %H:%M")


def warsaw_clock(ts: datetime | None = None) -> str:
    return warsaw(ts).strftime("%H:%M")


def market_closed(ts: datetime | None = None) -> bool:
    """FX closes Fri 22:00 London, reopens Sun 22:00 London."""
    t = (ts or utcnow()).astimezone(LON)
    wd, minute = t.weekday(), t.hour * 60 + t.minute  # Monday=0
    if wd == 5:                      # Saturday
        return True
    if wd == 6 and minute < 22 * 60:  # Sunday before reopen
        return True
    if wd == 4 and minute >= 22 * 60:  # Friday after close
        return True
    return False


def _next_local(zone: ZoneInfo, now: datetime, hour: int, minute: int = 0) -> datetime:
    local = now.astimezone(zone)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def session_info(now: datetime | None = None) -> SessionInfo:
    now = now or utcnow()
    lon = now.astimezone(LON)
    ny = now.astimezone(NY)
    waw = now.astimezone(WAW)
    notes: list[str] = []

    lon_min = lon.hour * 60 + lon.minute
    ny_min = ny.hour * 60 + ny.minute
    in_london = 8 * 60 <= lon_min < 17 * 60
    in_ny = 8 * 60 <= ny_min < 17 * 60

    if in_london and in_ny:
        active = "Overlap"
    elif in_london:
        active = "London"
    elif in_ny:
        active = "New York"
    elif 0 <= lon_min < 8 * 60:
        active = "Asia"
    else:
        active = "Off-session"

    closed = market_closed(now)
    if closed:
        notes.append("Ринок закритий: вихідні або rollover п'ятниці.")

    kill_label = None
    in_kill = False
    if 8 * 60 <= lon_min < 11 * 60:
        in_kill, kill_label = True, "London killzone 08:00–11:00"
    elif 13 * 60 <= lon_min < 16 * 60:
        in_kill, kill_label = True, "NY / overlap killzone 13:00–16:00"

    quality = "LOW"
    if in_kill:
        quality = "PRIME"
    elif active in ("London", "New York", "Overlap"):
        quality = "GOOD"
    if lon.weekday() == 4 and lon_min >= 16 * 60:
        notes.append("Пізня п'ятниця: ліквідність тоншає.")
        if quality == "PRIME":
            quality = "GOOD"

    lon_open = _next_local(LON, now, 8)
    ny_open = _next_local(NY, now, 8)
    return SessionInfo(
        active=active,
        warsaw_time=waw.strftime("%H:%M"),
        warsaw_date=waw.strftime("%Y-%m-%d"),
        london_open_in_min=None if in_london else max(0, int((lon_open - now).total_seconds() // 60)),
        ny_open_in_min=None if in_ny else max(0, int((ny_open - now).total_seconds() // 60)),
        is_weekend=closed,
        in_killzone=in_kill,
        killzone_label=kill_label,
        quality=quality,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class NewsRisk:
    level: str          # NONE | ELEVATED | BLOCKED | UNKNOWN
    reason: str
    minutes_to_next: int | None = None
    next_event: dict | None = None


HIGH_IMPACT_WORDS = (
    "cpi", "consumer price", "pce", "payroll", "employment situation", "unemployment",
    "gdp", "interest rate", "rate decision", "fomc", "monetary policy decision",
    "pmi", "retail sales", "ecb governing council",
)


def is_high_impact(title: str) -> bool:
    low = title.lower()
    return any(w in low for w in HIGH_IMPACT_WORDS)


def news_risk(events: list[dict], now: datetime, before_min: int, after_min: int, feed_ok: bool = True) -> NewsRisk:
    """Conservative gate: blocks new entries around high-impact EUR/USD events.

    If the calendar could not be read at all, the level is UNKNOWN and new entries
    are blocked: not knowing whether a release is imminent must never be treated as
    "no news".
    """
    if not feed_ok:
        return NewsRisk(
            "UNKNOWN",
            "Календар новин недоступний, тому новинний ризик невідомий. Нові входи заборонені, доки фід не відновиться.",
        )

    relevant = [e for e in events if e.get("currency") in ("EUR", "USD") and is_high_impact(e.get("title", ""))]
    relevant.sort(key=lambda e: e["time_utc"])
    future = [e for e in relevant if e["time_utc"] >= now]
    recent = [e for e in relevant if e["time_utc"] < now and (now - e["time_utc"]).total_seconds() <= after_min * 60]

    if recent:
        return NewsRisk("BLOCKED", f"Щойно вийшов реліз «{recent[-1]['title']}» — потрібна стабілізація волатильності.")
    if future:
        nxt = future[0]
        mins = int((nxt["time_utc"] - now).total_seconds() // 60)
        if mins <= before_min:
            return NewsRisk("BLOCKED", f"За {mins} хв до релізу «{nxt['title']}» — нові входи заборонені.", mins, nxt)
        if mins <= before_min * 3:
            return NewsRisk("ELEVATED", f"Попереду «{nxt['title']}» через {mins} хв — тримай ризик нижче.", mins, nxt)
        return NewsRisk("NONE", "Найближчих high-impact подій EUR/USD немає.", mins, nxt)
    return NewsRisk("NONE", "High-impact подій EUR/USD у перевірених фідах немає.")


def day_bounds(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    now = now or utcnow()
    w = now.astimezone(WAW)
    start = w.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1), w.strftime("%Y-%m-%d")
