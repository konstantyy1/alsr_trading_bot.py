"""Signal engine: six explicit setups, transparent 0-100 rule score, risk filters.

A score measures compliance with the written rules. It is NOT a probability of profit
and is never presented as one.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from analysis import Frame, Zone, liquidity_targets
from config import PIP, Risk
from datafeed import Snapshot
from markettime import NewsRisk, SessionInfo, day_bounds, warsaw_str

SETUP_NAMES = {
    "LIQUIDITY_SWEEP_MSS": "Liquidity sweep + зміна структури",
    "BREAKOUT_RETEST": "Breakout + retest",
    "TREND_CONTINUATION": "Продовження після displacement",
    "FVG_PULLBACK": "Повернення у FVG",
    "REVERSAL_DAY_LEVEL": "Розворот від денного рівня",
}
EXECUTABLE_SETUPS = tuple(SETUP_NAMES)


def pips(a: float, b: float) -> float:
    return abs(a - b) / PIP


@dataclass
class Candidate:
    side: str
    setup: str
    entry_low: float
    entry_high: float
    trigger: float
    stop: float
    tp1: float
    tp2: float
    tp3: float | None
    rr: float
    risk_pips: float
    reward_pips: float
    expected_hold_min: int
    max_hold_min: int
    context: dict
    why: list[str]
    cancel_rules: list[str]
    invalidations: list[str]
    score_parts: list[dict] = field(default_factory=list)
    score: int = 0
    grade: str = "NO_TRADE"


@dataclass
class Decision:
    verdict: str                     # SIGNAL | NO_TRADE | BLOCKED
    details: list[str]
    candidate: Candidate | None
    session: SessionInfo
    news: NewsRisk
    summary: dict


def _fmt(price: float) -> str:
    return f"{price:.5f}"


def _common_rules(side: str, stop: float) -> tuple[list[str], list[str]]:
    cancel = [
        "M5 закрилась за рівнем інвалідації до активації входу.",
        "Минув максимальний час очікування входу.",
        "Spread або якість даних перестали відповідати вимогам.",
        "Вийшла high-impact новина EUR/USD до входу.",
    ]
    invalid = [
        f"Закриття M5 {'нижче' if side == 'BUY' else 'вище'} {_fmt(stop)} скасовує сценарій.",
        "Втрата структури H1 проти напрямку угоди.",
        "Spike понад 2.5× ATR без повернення — рух вважається випадковим.",
    ]
    return cancel, invalid


def _base_context(frames: dict[str, Frame], note: str, levels_text: str, liq_text: str) -> dict:
    h4, h1, m15 = frames["H4"], frames["H1"], frames["M15"]
    return {
        "h4": f"H4 {h4.structure}" + (f" · BOS {h4.bos['dir']} @ {_fmt(h4.bos['level'])}" if h4.bos else ""),
        "h1": f"H1 {h1.structure}" + (f" · CHoCH {h1.choch['dir']}" if h1.choch else f" · BOS {h1.bos['dir']}" if h1.bos else " · консолідація"),
        "m15": f"M15 {m15.structure}" + (f" · displacement {m15.displacement['dir']} ×{m15.displacement['strength']}" if m15.displacement else ""),
        "m5": note,
        "liquidity": liq_text,
        "levels": levels_text,
    }


def detect_setups(snap: Snapshot, frames: dict[str, Frame], levels: list[Zone], min_risk_pips: float = 5.0, max_risk_pips: float = 35.0) -> list[Candidate]:
    q = snap.quote
    m5, m15 = snap.candles["M5"], snap.candles["M15"]
    if q is None or len(m5) < 40 or len(m15) < 30:
        return []

    f5, f15, f1, f4 = frames["M5"], frames["M15"], frames["H1"], frames["H4"]
    price = q.mid
    atr5 = f5.atr or 0.0004
    atr15 = f15.atr or 0.0006
    liq_above, liq_below = liquidity_targets(f15, price)

    day_high = next((z.high for z in levels if z.kind == "DAY_HIGH"), None)
    day_low = next((z.low for z in levels if z.kind == "DAY_LOW"), None)
    day_range = (day_high - day_low) if day_high and day_low else atr15 * 20

    levels_text = " · ".join(
        filter(
            None,
            [
                f"PDH {_fmt(day_high)}" if day_high else None,
                f"PDL {_fmt(day_low)}" if day_low else None,
                f"ліквідність вище {_fmt(liq_above[0])}" if liq_above else None,
                f"ліквідність нижче {_fmt(liq_below[0])}" if liq_below else None,
            ],
        )
    ) or "рівні не визначені"
    liq_text = f"{len(liq_above)} цілі вище, {len(liq_below)} нижче"

    out: list[Candidate] = []
    last, prev = m5[-1], m5[-2]
    recent = m5[-12:]
    prior = recent[:-1]

    # 1) Liquidity sweep + reclaim on M5, aligned with M15.
    if prior:
        swept_low = prev.l < min(c.l for c in prior)
        if swept_low and last.c > prev.h and f5.structure != "bear" and f15.structure != "bear":
            swing_low = min(c.l for c in recent)
            stop = swing_low - atr5 * 0.35
            risk = price - stop
            if atr5 * 0.5 < risk < atr15 * 2.2:
                tp1 = liq_above[0] if liq_above else price + risk * 2
                tp2 = liq_above[1] if len(liq_above) > 1 else price + risk * 3
                if pips(tp1, price) / max(pips(price, stop), 0.1) >= 1.2:
                    cancel, invalid = _common_rules("BUY", stop)
                    out.append(Candidate(
                        side="BUY", setup="LIQUIDITY_SWEEP_MSS",
                        entry_low=round(price - atr5 * 0.2, 5), entry_high=round(price + atr5 * 0.1, 5),
                        trigger=price, stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(tp2, 5), tp3=round(tp2 + risk, 5),
                        rr=round(pips(tp1, price) / pips(price, stop), 2),
                        risk_pips=round(pips(price, stop), 1), reward_pips=round(pips(tp1, price), 1),
                        expected_hold_min=45, max_hold_min=240,
                        context=_base_context(frames, f"sweep {_fmt(swing_low)} → reclaim, close {_fmt(last.c)}", levels_text, liq_text),
                        why=["M5 зняв ліквідність під локальним мінімумом і повернувся вище діапазону свічки зняття.",
                             f"Структура M15 ({f15.structure}) не суперечить лонгу.",
                             "Тригер — закриття M5 вище тіла свічки, що зняла ліквідність."],
                        cancel_rules=cancel, invalidations=invalid,
                    ))

        swept_high = prev.h > max(c.h for c in prior)
        if swept_high and last.c < prev.l and f5.structure != "bull" and f15.structure != "bull":
            swing_high = max(c.h for c in recent)
            stop = swing_high + atr5 * 0.35
            risk = stop - price
            if atr5 * 0.5 < risk < atr15 * 2.2:
                tp1 = liq_below[0] if liq_below else price - risk * 2
                tp2 = liq_below[1] if len(liq_below) > 1 else price - risk * 3
                if pips(tp1, price) / max(pips(stop, price), 0.1) >= 1.2:
                    cancel, invalid = _common_rules("SELL", stop)
                    out.append(Candidate(
                        side="SELL", setup="LIQUIDITY_SWEEP_MSS",
                        entry_low=round(price - atr5 * 0.1, 5), entry_high=round(price + atr5 * 0.2, 5),
                        trigger=price, stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(tp2, 5), tp3=round(tp2 - risk, 5),
                        rr=round(pips(tp1, price) / pips(stop, price), 2),
                        risk_pips=round(pips(stop, price), 1), reward_pips=round(pips(tp1, price), 1),
                        expected_hold_min=45, max_hold_min=240,
                        context=_base_context(frames, f"sweep {_fmt(swing_high)} → rejection, close {_fmt(last.c)}", levels_text, liq_text),
                        why=["M5 зняв ліквідність над локальним максимумом і закрився назад під рівнем.",
                             f"Структура M15 ({f15.structure}) не суперечить шорту.",
                             "Тригер — закриття M5 нижче тіла свічки, що зняла ліквідність."],
                        cancel_rules=cancel, invalidations=invalid,
                    ))

    # 2) Breakout + retest of the M15 range edge.
    rh, rl = f15.range_high, f15.range_low
    if rh and rl and (rh - rl) > atr15 * 3:
        broke_up = last.c > rh and prev.c <= rh
        retest_up = any(c.l <= rh * 1.0002 and c.c > rh for c in m5[-3:])
        if (broke_up or retest_up) and f1.structure != "bear":
            risk = max(price - (rh - atr5 * 0.8), atr5 * 0.6)
            stop = price - risk
            tp1, tp2 = price + risk * 2, price + risk * 3.2
            cancel, invalid = _common_rules("BUY", stop)
            out.append(Candidate(
                side="BUY", setup="BREAKOUT_RETEST",
                entry_low=round(rh, 5), entry_high=round(rh + atr5 * 0.3, 5), trigger=rh,
                stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(tp2, 5), tp3=None,
                rr=2.0, risk_pips=round(risk / PIP, 1), reward_pips=round(risk * 2 / PIP, 1),
                expected_hold_min=60, max_hold_min=240,
                context=_base_context(frames, f"пробій {_fmt(rh)} з ретестом", levels_text, liq_text),
                why=[f"Ціна пробила верхню межу діапазону M15 ({_fmt(rh)}).",
                     "Ретест утримався і M5 закрився над рівнем.",
                     "Ціль — наступна ліквідність у напрямку."],
                cancel_rules=cancel, invalidations=invalid,
            ))

        broke_down = last.c < rl and prev.c >= rl
        retest_down = any(c.h >= rl * 0.9998 and c.c < rl for c in m5[-3:])
        if (broke_down or retest_down) and f1.structure != "bull":
            risk = max((rl + atr5 * 0.8) - price, atr5 * 0.6)
            stop = price + risk
            tp1, tp2 = price - risk * 2, price - risk * 3.2
            cancel, invalid = _common_rules("SELL", stop)
            out.append(Candidate(
                side="SELL", setup="BREAKOUT_RETEST",
                entry_low=round(rl - atr5 * 0.3, 5), entry_high=round(rl, 5), trigger=rl,
                stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(tp2, 5), tp3=None,
                rr=2.0, risk_pips=round(risk / PIP, 1), reward_pips=round(risk * 2 / PIP, 1),
                expected_hold_min=60, max_hold_min=240,
                context=_base_context(frames, f"пробій {_fmt(rl)} з ретестом", levels_text, liq_text),
                why=[f"Ціна пробила нижню межу діапазону M15 ({_fmt(rl)}).",
                     "Ретест утримався і M5 закрився під рівнем.",
                     "Ціль — наступна ліквідність у напрямку."],
                cancel_rules=cancel, invalidations=invalid,
            ))

    # 3) Trend continuation after M15 displacement.
    disp = f15.displacement
    if disp and f5.structure != "range":
        up = disp["dir"] == "up"
        aligned = f1.structure != ("bear" if up else "bull")
        pullback = any(c.c < prev.o for c in m5[-4:]) and last.c > prev.c if up else any(c.c > prev.o for c in m5[-4:]) and last.c < prev.c
        if aligned and pullback:
            tail = m5[-6:]
            stop = (min(c.l for c in tail) - atr5 * 0.3) if up else (max(c.h for c in tail) + atr5 * 0.3)
            risk = abs(price - stop)
            if atr5 * 0.5 < risk < atr15 * 1.8:
                tp1 = price + risk * 1.8 if up else price - risk * 1.8
                tp2 = price + risk * 2.8 if up else price - risk * 2.8
                cancel, invalid = _common_rules("BUY" if up else "SELL", stop)
                out.append(Candidate(
                    side="BUY" if up else "SELL", setup="TREND_CONTINUATION",
                    entry_low=round(price - atr5 * 0.2 if up else price - atr5 * 0.1, 5),
                    entry_high=round(price + atr5 * 0.1 if up else price + atr5 * 0.2, 5),
                    trigger=price, stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(tp2, 5), tp3=None,
                    rr=1.8, risk_pips=round(risk / PIP, 1), reward_pips=round(risk * 1.8 / PIP, 1),
                    expected_hold_min=90, max_hold_min=240,
                    context=_base_context(frames, f"displacement {disp['dir']} ×{disp['strength']}, pullback завершено", levels_text, liq_text),
                    why=[f"M15 показав displacement {disp['dir']} силою {disp['strength']}×ATR.",
                         "M5 відкотився і закрився в напрямку імпульсу.",
                         f"Структура H1 ({f1.structure}) не суперечить напрямку."],
                    cancel_rules=cancel, invalidations=invalid,
                ))

    # 4) Pullback into a fresh M15 FVG.
    for fvg in [z for z in f15.zones if z.kind == "FVG"][-3:]:
        touched = any(c.l <= fvg.high and c.h >= fvg.low for c in m5[-6:])
        if not touched:
            continue
        up = fvg.direction == "bull"
        reacts = (last.c > fvg.low and last.c < fvg.high + atr5) if up else (last.c < fvg.high and last.c > fvg.low - atr5)
        aligned = f1.structure != ("bear" if up else "bull")
        if not (reacts and aligned):
            continue
        stop = fvg.low - atr5 * 0.4 if up else fvg.high + atr5 * 0.4
        risk = abs(price - stop)
        if not (atr5 * 0.4 < risk < atr15 * 2):
            continue
        tp1 = price + risk * 2 if up else price - risk * 2
        tp2 = price + risk * 3 if up else price - risk * 3
        cancel, invalid = _common_rules("BUY" if up else "SELL", stop)
        out.append(Candidate(
            side="BUY" if up else "SELL", setup="FVG_PULLBACK",
            entry_low=round(fvg.low, 5), entry_high=round(fvg.high, 5), trigger=price,
            stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(tp2, 5), tp3=None,
            rr=2.0, risk_pips=round(risk / PIP, 1), reward_pips=round(risk * 2 / PIP, 1),
            expected_hold_min=60, max_hold_min=210,
            context=_base_context(frames, f"FVG {fvg.direction} {_fmt(fvg.low)}–{_fmt(fvg.high)}", levels_text, liq_text),
            why=[f"Незаповнений FVG {fvg.direction} на M15 ({_fmt(fvg.low)}–{_fmt(fvg.high)}).",
                 "Ціна повернулась у зону і показала реакцію на M5.",
                 "Стоп за межами імбілансу, цілі за імпульсом."],
            cancel_rules=cancel, invalidations=invalid,
        ))
        break

    # 5) Reversal from the previous day extreme, confirmed on M5.
    if day_high and abs(price - day_high) < atr15 * 0.6 and last.c < prev.o and f5.structure == "bear":
        stop = max(day_high + atr5 * 0.5, last.h + atr5 * 0.3)
        risk = stop - price
        if risk > atr5 * 0.4:
            tp1 = price - max(risk * 1.8, day_range * 0.35)
            if pips(tp1, price) / risk >= 1.4:
                cancel, invalid = _common_rules("SELL", stop)
                out.append(Candidate(
                    side="SELL", setup="REVERSAL_DAY_LEVEL",
                    entry_low=round(price - atr5 * 0.1, 5), entry_high=round(price + atr5 * 0.2, 5), trigger=price,
                    stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(price - max(risk * 2.6, day_range * 0.5), 5), tp3=None,
                    rr=round(max(risk * 1.8, day_range * 0.35) / risk, 2),
                    risk_pips=round(risk / PIP, 1), reward_pips=round(max(risk * 1.8, day_range * 0.35) / PIP, 1),
                    expected_hold_min=75, max_hold_min=240,
                    context=_base_context(frames, f"реакція від PDH {_fmt(day_high)}", levels_text, liq_text),
                    why=[f"Ціна підійшла до Previous Day High ({_fmt(day_high)}).",
                         "M5 показав відмову від рівня.",
                         "Стоп за екстремумом дня, ціль — середина денного діапазону."],
                    cancel_rules=cancel, invalidations=invalid,
                ))
    elif day_low and abs(price - day_low) < atr15 * 0.6 and last.c > prev.o and f5.structure == "bull":
        stop = min(day_low - atr5 * 0.5, last.l - atr5 * 0.3)
        risk = price - stop
        if risk > atr5 * 0.4:
            tp1 = price + max(risk * 1.8, day_range * 0.35)
            if pips(tp1, price) / risk >= 1.4:
                cancel, invalid = _common_rules("BUY", stop)
                out.append(Candidate(
                    side="BUY", setup="REVERSAL_DAY_LEVEL",
                    entry_low=round(price - atr5 * 0.2, 5), entry_high=round(price + atr5 * 0.1, 5), trigger=price,
                    stop=round(stop, 5), tp1=round(tp1, 5), tp2=round(price + max(risk * 2.6, day_range * 0.5), 5), tp3=None,
                    rr=round(max(risk * 1.8, day_range * 0.35) / risk, 2),
                    risk_pips=round(risk / PIP, 1), reward_pips=round(max(risk * 1.8, day_range * 0.35) / PIP, 1),
                    expected_hold_min=75, max_hold_min=240,
                    context=_base_context(frames, f"реакція від PDL {_fmt(day_low)}", levels_text, liq_text),
                    why=[f"Ціна підійшла до Previous Day Low ({_fmt(day_low)}).",
                         "M5 показав відмову і повернення вгору.",
                         "Стоп за екстремумом дня, ціль — середина денного діапазону."],
                    cancel_rules=cancel, invalidations=invalid,
                ))

    # Reject structurally valid but economically meaningless setups. Measured from
    # the price the order actually fills at, not from the trigger price.
    # The reward is checked here too: on a limit entry the fill sits at one edge of the
    # zone while the target was computed from the current mid, so a passing trigger
    # price can still leave almost no room to the target. Two trades in the 59-day
    # sample reached TP1 within 0.3 and 3.0 pips of the fill while the filter thought
    # they were 1:2 — this check removes that whole class.
    kept: list[Candidate] = []
    for c in out:
        if _targets_are_far_enough(c, min_risk_pips, max_risk_pips):
            kept.append(c)
    return kept


def _targets_are_far_enough(c: Candidate, min_risk_pips: float, max_risk_pips: float) -> bool:
    """Is there real room between the fill, the stop and the first target?

    A limit entry fills at one edge of the zone, while the targets were computed from
    the current mid. A setup can therefore look like 1:2 on the trigger price and still
    leave almost nothing to TP1 once filled. Two trades in the 59-day sample did exactly
    that (0.3 and 3.0 pips of room), so the check is made from the fill price only.
    """
    fill = entry_price(c.side, c.entry_low, c.entry_high)
    risk = pips(fill, c.stop)
    reward = pips(fill, c.tp1)
    if not (min_risk_pips <= risk <= max_risk_pips):
        return False
    if risk <= 0 or reward < risk:
        return False
    return True


def score_candidate(c: Candidate, frames: dict[str, Frame], snap: Snapshot, session: SessionInfo, news: NewsRisk, cfg) -> list[dict]:
    f4, f1, f5 = frames["H4"], frames["H1"], frames["M5"]
    bull = c.side == "BUY"
    parts: list[dict] = []

    align = 0
    if (bull and f4.structure == "bull") or (not bull and f4.structure == "bear"):
        align += 12
    if (bull and f1.structure == "bull") or (not bull and f1.structure == "bear"):
        align += 13
    elif f1.structure == "range":
        align += 6
    parts.append({"label": "Відповідність H4/H1", "points": align, "max": 25, "note": f"H4 {f4.structure}, H1 {f1.structure}"})

    above, below = liquidity_targets(f1, c.trigger)
    targets = len(above if bull else below)
    parts.append({"label": "Очевидна ліквідність", "points": 15 if targets >= 2 else 10 if targets == 1 else 0, "max": 15,
                  "note": f"{targets} структурних цілей у напрямку"})

    conf = 0
    disp = frames["M15"].displacement
    if disp and ((bull and disp["dir"] == "up") or (not bull and disp["dir"] == "down")):
        conf += 12
    if f5.structure == ("bull" if bull else "bear"):
        conf += 8
    parts.append({"label": "Підтвердження ціною", "points": conf, "max": 20,
                  "note": f"M15 displacement {disp['dir']}" if disp else "без displacement"})

    atr15 = frames["M15"].atr
    vol = 10 if 0.00045 <= atr15 <= 0.0022 else 6 if atr15 > 0.0003 else 0
    parts.append({"label": "Волатильність", "points": vol, "max": 10, "note": f"ATR M15 {atr15 / PIP:.1f} pips"})

    last = snap.candles["M5"][-1]
    spike = f5.atr > 0 and (last.h - last.l) > f5.atr * cfg.atr_spike_multiple
    parts.append({"label": "Не spike", "points": 0 if spike else 10, "max": 10,
                  "note": "аномальна свічка" if spike else "нормальна амплітуда"})

    parts.append({"label": "Новинний ризик", "points": 10 if news.level == "NONE" else 5 if news.level == "ELEVATED" else 0,
                  "max": 10, "note": news.level})

    spread = snap.quote.spread_pips if snap.quote else 99
    parts.append({"label": "Spread", "points": 5 if spread <= 1.2 else 3 if spread <= cfg.max_spread_pips else 0,
                  "max": 5, "note": f"{spread} pips (припущений)"})

    obstacles = below if bull else above
    room = abs(c.tp1 - c.trigger)
    blocked = any(0.0001 < abs(lvl - c.trigger) < room * 0.6 for lvl in obstacles)
    parts.append({"label": "Простір до перешкоди", "points": 2 if blocked else 10, "max": 10,
                  "note": "перешкода ближче за TP1" if blocked else "чистий шлях"})

    parts.append({"label": "Сесія", "points": 5 if session.quality == "PRIME" else 3 if session.quality == "GOOD" else 0,
                  "max": 5, "note": f"{session.active}{' / killzone' if session.in_killzone else ''}"})

    covers = c.risk_pips > 0 and c.reward_pips / c.risk_pips >= 1.6 and c.risk_pips >= cfg.max_spread_pips * 2
    parts.append({"label": "Покриття витрат", "points": 5 if covers else 0, "max": 5, "note": f"R:R {c.rr}"})
    return parts


def evaluate(
    snap: Snapshot,
    frames: dict[str, Frame],
    levels: list[Zone],
    session: SessionInfo,
    news: NewsRisk,
    *,
    open_count: int,
    signals_today: int,
    daily_r: float,
    risk: Risk,
    data_cfg,
) -> Decision:
    details: list[str] = []
    summary = {
        "bid": snap.quote.bid if snap.quote else None,
        "ask": snap.quote.ask if snap.quote else None,
        "spread": snap.quote.spread_pips if snap.quote else None,
        "h4": frames["H4"].structure if frames.get("H4") else "?",
        "h1": frames["H1"].structure if frames.get("H1") else "?",
        "atr_m15": round(frames["M15"].atr / PIP, 1) if frames.get("M15") else None,
    }

    def stop(verdict: str, reason: str) -> Decision:
        details.insert(0, reason)
        return Decision(verdict, details, None, session, news, summary)

    if snap.quality.status != "VERIFIED":
        details.extend(snap.quality.notes[:4])
        return stop("BLOCKED", f"Дані {snap.quality.status}: нові сигнали заборонені.")
    if session.is_weekend:
        return stop("BLOCKED", "Ринок закритий (вихідні).")
    blocked = {s.strip().lower() for s in str(getattr(risk, "blocked_sessions", "")).split(",") if s.strip()}
    if session.active.lower() in blocked:
        return stop("BLOCKED", f"Сесія {session.active} у списку слабких: бектест за місяць показав тут "
                               f"від'ємний результат. Вхід заблоковано навмисно.")
    if news.level == "BLOCKED":
        return stop("BLOCKED", news.reason)
    if news.level == "UNKNOWN":
        return stop("BLOCKED", news.reason + " Це захист, а не збій: торгувати наосліп ми не будемо.")
    if (snap.quote.spread_pips if snap.quote else 99) > data_cfg.max_spread_pips:
        return stop("BLOCKED", f"Spread {snap.quote.spread_pips} pips перевищує ліміт {data_cfg.max_spread_pips}.")
    if open_count >= risk.max_open_setups:
        return stop("BLOCKED", f"Уже {open_count} активних сценарії(в) — ліміт {risk.max_open_setups}.")
    if signals_today >= risk.max_signals_per_day:
        return stop("BLOCKED", f"Денний ліміт сигналів {risk.max_signals_per_day} вичерпано.")
    if daily_r <= -risk.daily_loss_limit_r:
        return stop("BLOCKED", f"Денний ліміт збитку {risk.daily_loss_limit_r}R досягнуто.")
    if frames["M15"].atr / PIP > data_cfg.max_atr_m15_pips:
        return stop("BLOCKED", "Волатильність надмірна (ATR M15 вище ліміту).")

    raw = detect_setups(snap, frames, levels, data_cfg.min_risk_pips, data_cfg.max_risk_pips)
    if not raw:
        return stop("NO_TRADE", "Жоден із п'яти сетапів не має підтвердження на M15/M5.")

    # Trade only with the higher-timeframe trend. Over 59 days of real bars, setups
    # that fought H4 lost 34 trades at a 28% win rate for -13.49R, while aligned setups
    # with score >= 87 made +15.02R at 57% and stayed positive in both halves of the
    # period and at 1.0 / 1.5 / 2.0 pips spread. A setup against H4 is kept as
    # observation only, because the score alone does not separate them well enough.
    h4_structure = frames["H4"].structure if frames.get("H4") else "range"
    aligned = [c for c in raw
               if h4_structure == "range" or h4_structure == ("bull" if c.side == "BUY" else "bear")]
    if not aligned:
        return stop("NO_TRADE", f"Усі сетапи проти тренду H4 ({h4_structure}). Бектест показав, що "
                               f"контртрендові входи збиткові, тому вони не пропонуються.")

    for cand in aligned:
        cand.score_parts = score_candidate(cand, frames, snap, session, news, data_cfg)
        cand.score = max(0, min(100, sum(p["points"] for p in cand.score_parts)))
    raw = aligned
    raw.sort(key=lambda c: c.score, reverse=True)
    best = raw[0]

    details.extend(f"{p['label']}: {p['points']}/{p['max']} ({p['note']})" for p in best.score_parts)
    best.grade = "A+" if best.score >= risk.min_score_ap_lus else "A" if best.score >= risk.min_score_ready else "B" if best.score >= 55 else "NO_TRADE"

    if best.grade in ("B", "NO_TRADE"):
        return stop("NO_TRADE", f"Найкращий сетап {best.setup} отримав {best.score}/100 — це спостереження, не сигнал.")
    if best.rr < risk.min_rr:
        best.grade = "B"
        return stop("NO_TRADE", f"R:R {best.rr} нижче мінімуму {risk.min_rr}.")
    moved = pips(snap.quote.mid, best.trigger)
    if moved > max(6, best.risk_pips * 0.45):
        best.grade = "B"
        return stop("NO_TRADE", f"Ціна відійшла на {moved:.1f} pips від тригера — не переслідуємо вхід.")

    return Decision("SIGNAL", details, best, session, news, summary)


def new_signal_id(now) -> str:
    day = now.strftime("%Y%m%d")
    return f"{day}-{random.randint(0x1000, 0xFFFF):04X}"


def entry_price(side: str, entry_low: float, entry_high: float) -> float:
    """The price a filled order is accounted at.

    A sell limit fills at the lower edge of the zone and a buy limit at the upper
    edge, which is also what the lifecycle uses when it resolves the trade. Risk,
    reward and position size must all be measured from this same price, otherwise
    the reported pips and the realised R disagree.
    """
    return entry_high if side == "BUY" else entry_low


def build_signal(cand: Candidate, snap: Snapshot, session: SessionInfo, risk: Risk, now, symbol: str, news: NewsRisk) -> dict:
    widened = risk.cost_pips * PIP
    stop = cand.stop - widened / 2 if cand.side == "BUY" else cand.stop + widened / 2
    fill = entry_price(cand.side, cand.entry_low, cand.entry_high)

    # TP1 is pushed slightly past the structural target. On 59 days the raw target
    # scored +15.73R and the same target 10% further scored +17.76R, because price
    # that reaches a visible level usually trades a little beyond it. The scale is
    # measured, not guessed, and stays at 1.0 if the setting is changed.
    tp1 = cand.tp1
    if risk.tp1_scale and risk.tp1_scale != 1.0:
        raw_r = pips(fill, cand.tp1) / pips(fill, stop) if pips(fill, stop) > 0 else 0
        shifted = fill + (pips(fill, stop) * raw_r * risk.tp1_scale) * (1 if cand.side == "BUY" else -1) * PIP
        # Never move a target through TP2 — that would make the ladder inconsistent.
        tp2_far = pips(fill, cand.tp2)
        shifted_far = pips(fill, shifted)
        if 0 < shifted_far < tp2_far:
            tp1 = round(shifted, 5)

    risk_pips = pips(fill, stop)
    reward_pips = pips(fill, tp1)
    rr = (reward_pips / risk_pips) if risk_pips > 0 else 0.0
    lots = (risk.account_equity * risk.risk_percent / 100) / (risk_pips * 10) if risk_pips > 0 else 0
    return {
        "id": new_signal_id(now),
        "symbol": symbol,
        "side": cand.side,
        "setup": cand.setup,
        "grade": cand.grade,
        "score": cand.score,
        "score_parts": cand.score_parts,
        "status": "READY",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "entry_low": cand.entry_low,
        "entry_high": cand.entry_high,
        "trigger": round(fill, 5),
        "stop": round(stop, 5),
        "tp1": tp1,
        "tp2": cand.tp2,
        "tp3": cand.tp3,
        "rr": round(rr, 1),
        "risk_pips": round(risk_pips, 1),
        "reward_pips": round(reward_pips, 1),
        "expected_hold_min": cand.expected_hold_min,
        "max_hold_min": cand.max_hold_min,
        # Lifecycle reads this to move the stop to entry once 0.75R is banked.
        "break_even_at_r": risk.break_even_at_r,
        "session": session.active,
        "quote_at_creation": snap.quote.mid if snap.quote else cand.trigger,
        "spread_at_creation": snap.quote.spread_pips if snap.quote else 0,
        "provider": snap.quote.provider if snap.quote else "unknown",
        "context": cand.context,
        "why": cand.why,
        "cancel_rules": cand.cancel_rules,
        "invalidations": cand.invalidations,
        "news_risk": f"{news.level}: {news.reason}",
        "position_size": {
            "lots": round(lots, 3),
            "risk_amount": round(risk.account_equity * risk.risk_percent / 100, 2),
            "note": f"{risk.risk_percent}% від {risk.account_equity} USD, 10 USD/pip за лот",
        },
        "resolved_at": None,
        "resolution": None,
        "r_result": None,
        "origin": "live",
    }
