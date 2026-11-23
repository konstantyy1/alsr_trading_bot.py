"""Runtime configuration. Secrets come from environment variables only."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

INTERNAL_TZ = "Europe/Warsaw"
LONDON_TZ = "Europe/London"
NY_TZ = "America/New_York"

SYMBOL = os.environ.get("SYMBOL", "EURUSD=X")


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Risk:
    """Conservative defaults. This is an analytics system, not an executor."""
    risk_percent: float = _f("RISK_PERCENT", 0.35)
    daily_loss_limit_r: float = _f("DAILY_LOSS_LIMIT_R", 1.5)
    max_open_setups: int = _i("MAX_OPEN_SETUPS", 3)
    max_signals_per_day: int = _i("MAX_SIGNALS_PER_DAY", 4)
    min_rr: float = _f("MIN_RR", 1.6)
    min_score_ready: float = _f("MIN_SCORE_READY", 70)
    min_score_ap_lus: float = _f("MIN_SCORE_A_PLUS", 85)
    cost_pips: float = _f("COST_PIPS", 1.2)
    account_equity: float = _f("ACCOUNT_EQUITY", 10000)


@dataclass(frozen=True)
class DataCfg:
    max_quote_age_sec: int = _i("MAX_QUOTE_AGE_SEC", 240)
    max_cross_deviation_pips: float = _f("MAX_CROSS_DEVIATION_PIPS", 12.0)
    max_spread_pips: float = _f("MAX_SPREAD_PIPS", 2.4)
    assumed_spread_pips: float = _f("ASSUMED_SPREAD_PIPS", 1.2)
    news_blackout_before_min: int = _i("NEWS_BLACKOUT_BEFORE_MIN", 20)
    news_blackout_after_min: int = _i("NEWS_BLACKOUT_AFTER_MIN", 15)
    atr_spike_multiple: float = _f("ATR_SPIKE_MULTIPLE", 3.2)
    max_atr_m15_pips: float = _f("MAX_ATR_M15_PIPS", 45.0)
    min_risk_pips: float = _f("MIN_RISK_PIPS", 5.0)
    max_risk_pips: float = _f("MAX_RISK_PIPS", 35.0)


@dataclass(frozen=True)
class Env:
    telegram_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")
    database_url: str = os.environ.get("DATABASE_URL", "")
    scan_token: str = os.environ.get("SCAN_TOKEN", "")
    dashboard_token: str = os.environ.get("DASHBOARD_TOKEN", "")
    webhook_secret: str = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    port: int = _i("PORT", 10000)
    poll_interval_min: int = _i("SCAN_INTERVAL_MIN", 5)
    enable_signals: bool = os.environ.get("ENABLE_SIGNALS", "true").lower() == "true"
    enable_news: bool = os.environ.get("ENABLE_NEWS", "true").lower() == "true"
    risk: Risk = field(default_factory=Risk)
    data: DataCfg = field(default_factory=DataCfg)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)


ENV = Env()

PIP = 0.0001
