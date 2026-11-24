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
    min_rr: float = _f("MIN_RR", 1.6)
    # Thresholds raised from 70/85 after the 30-day backtest. Score >= 87 produced 18
    # trades at a 79% win rate and +14.86R, profitable in both halves of the month and
    # at 1.0/1.5/2.0 pips spread. Score 85-86 was already thin, and below 80 the win
    # rate dropped to ~33% in the first half. A+ moved to 92 so the top grade keeps
    # meaning "rarest", not "everything above the floor".
    min_score_ready: float = _f("MIN_SCORE_READY", 87)
    min_score_ap_lus: float = _f("MIN_SCORE_A_PLUS", 92)
    cost_pips: float = _f("COST_PIPS", 1.2)
    account_equity: float = _f("ACCOUNT_EQUITY", 10000)
    # The owner asked for 1-2 trades a day at the highest achievable hit rate, so the
    # daily cap is set to 2: quality over quantity. The backtest average is 0.6/day,
    # which means the cap rarely binds and only stops a clustered burst.
    max_signals_per_day: int = _i("MAX_SIGNALS_PER_DAY", 2)
    # Backtest evidence for the default: over 30 days of real bars the Asia session
    # produced 20 trades and -13.11R, while every other session combined made +9.64R.
    # A 20,000-shuffle test put the chance of that being luck at 0.7%, and it held in
    # both halves of the month. Comma-separated names as shown by markettime.
    blocked_sessions: str = os.environ.get("BLOCKED_SESSIONS", "Asia")
    # Position management, measured on 59 days and then re-tested on 30:
    #   TP1 10% further than the structural target: +0.132R per trade,
    #   5th percentile of the paired bootstrap +0.015 — real, not noise.
    #   Break-even once 0.75R is banked: losers stop turning into full -1R losses.
    # Plain TP1 with a fixed stop scored +15.73R; these two together scored +20.74R
    # and stayed positive in both halves and at 1.0/1.5/2.0 pips spread.
    tp1_scale: float = _f("TP1_SCALE", 1.1)
    break_even_at_r: float = _f("BREAK_EVEN_AT_R", 0.75)
    # Trailing stop since 2.7.0. Chosen by the worst-slice test, not by the highest
    # total: 6 time slices x 3 spread levels on 59 days of EUR/USD M5, 41 trades.
    #   +1.5R start / 1.0xATR  -> +22.60R, dd -2.57R, better in 15 of 18 slices
    #   +1.5R start / 0.5R fixed -> +20.61R, dd -2.68R, better in 9 of 18
    #   +2.0R start / 1.0xATR  -> +24.73R, dd -3.60R, deeper drawdown
    # ATR beats a fixed distance because the median trailing gap lands at 0.42R
    # (10th pct 0.29R, 90th 0.65R) and widens by itself in a volatile session.
    # The floor keeps the gap above 1.5 pips so it can never sit inside the spread.
    trail_start_r: float = _f("TRAIL_START_R", 1.5)
    trail_atr_mult: float = _f("TRAIL_ATR_MULT", 1.0)
    trail_min_pips: float = _f("TRAIL_MIN_PIPS", 1.5)


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
    public_url: str = os.environ.get("PUBLIC_URL", "").rstrip("/")
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

_SETTING_CHAT = "telegram_chat_id"


def chat_id(store) -> str:
    """The chat to deliver to: the dashboard binding first, then the environment.

    Binding from the dashboard keeps the setup to one button and survives restarts
    once a database is attached, without editing service settings.
    """
    try:
        saved = store.get_setting(_SETTING_CHAT, None)
    except Exception:  # noqa: BLE001 - a broken store must not break the dashboard
        saved = None
    return str(saved or ENV.telegram_chat_id or "")


def bind_chat(store, value: str) -> str:
    """Remember the chat id. An empty value unbinds it."""
    store.set_setting(_SETTING_CHAT, value or None)
    return chat_id(store)


def telegram_ready(store) -> bool:
    return bool(ENV.telegram_token and chat_id(store))

PIP = 0.0001
