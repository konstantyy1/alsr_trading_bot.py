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
    # Raised from 2 to 3 in 2.7.6 at the owner's request, and confirmed AFTER the R
    # accounting fix. On 59 days: 35 trades and +16.57R at a cap of 2, 40 trades and
    # +19.41R at a cap of 3. On the last 30 days: +7.84R at 2 and +6.84R at 3, so the
    # third slot is not uniformly better - it depends on whether the extra setups land
    # in a trending stretch. It still binds: without a cap the same period offers 51
    # signals, so this is a brake on a clustered day, not a target.
    # The 1.5R daily loss stop keeps the downside in check: it already ends the day
    # after two full losses, so a third setup only matters on a day that starts well.
    max_signals_per_day: int = _i("MAX_SIGNALS_PER_DAY", 3)
    # Backtest evidence for the default: over 30 days of real bars the Asia session
    # produced 20 trades and -13.11R, while every other session combined made +9.64R.
    # A 20,000-shuffle test put the chance of that being luck at 0.7%, and it held in
    # both halves of the month. Comma-separated names as shown by markettime.
    blocked_sessions: str = os.environ.get("BLOCKED_SESSIONS", "Asia")
    # Position management, measured on 59 days and then re-tested on 30:
    #   TP1 10% further than the structural target: +0.132R per trade,
    #   5th percentile of the paired bootstrap +0.015 — real, not noise.
    #   Break-even once 0.75R is banked: losers stop turning into full -1R losses.
    #
    # TP1_SCALE lived here until 2.7.6 and was removed: since 2.7.0 no target closes
    # a trade, so shifting TP1 changed nothing at all. Measured 1.0 / 1.1 / 1.2 on the
    # same bars - byte-identical results (+5.58R each). A setting that looks like it
    # does something but cannot is worse than no setting.
    break_even_at_r: float = _f("BREAK_EVEN_AT_R", 0.75)
    # Trailing stop since 2.7.0; start +2.0R and distance 0.75xATR since 2.7.6.
    #
    # Every number below was re-measured AFTER the R accounting was corrected
    # (2.7.6). Until then the backtest charged the spread a second time on top of a
    # result already expressed in R, which produced -1.18R stop-outs and -0.16R
    # break-evens. Those figures invalidated every earlier comparison, so this is a
    # fresh measurement on 59 days of real EUR/USD M5, 40 trades, with the live
    # lifecycle doing the replay:
    #
    #   rule                total R   30 days   spread 2.0   thirds all up?
    #   +1.5R / 1.0xATR     +16.79      -          -            no
    #   +2.0R / 1.0xATR     +19.41    +6.84      +9.39          no
    #   +2.0R / 0.75xATR    +21.07    +7.76     +10.47          no
    #
    # The third of the period in the middle is negative for every rule tried, so
    # "all thirds positive" is not a bar any variant passes - it is a property of
    # the sample, not a defect of one setting. The choice is made on the spread
    # column instead: 0.75xATR keeps the most when trading costs double.
    #
    # Why 0.75xATR is now accepted, after being rejected in 2.7.0: the earlier
    # rejection rested on numbers produced by the broken accounting, and the D13
    # artefact it was likened to was a 1-pip trail. A median 0.75xATR gap is 7.5
    # pips against a 1.2 pip spread, so a real stop order can sit there. The gain
    # is also not one lucky trade: the largest is +4.11R and the total without it
    # is still +16.96R.
    trail_start_r: float = _f("TRAIL_START_R", 2.0)
    trail_atr_mult: float = _f("TRAIL_ATR_MULT", 0.75)
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
