"""One-off live wire check: Telegram transport + real signal template rendering."""
import datetime
import os
import tempfile

os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "wire.db")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

import notify
from datafeed import build_snapshot
from analysis import analyse
from markettime import session_info, news_risk
from signals import Candidate, build_signal
from config import ENV, SYMBOL

# 1) Real HTTP call with a bogus chat id: proves the request is valid and the
#    error path is parsed, without messaging anyone.
ok, err = notify.send(TOKEN, "1", "<b>probe</b>")
print("send() bogus chat ->", ok, "|", err)
print("error path works:", ok is False and err is not None)

# 2) Intercept the transport, build a real signal from live prices, render it.
captured = []
real_send = notify.send
notify.send = lambda tok, chat, text, **kw: (captured.append(text), (True, None))[1]

snap = build_snapshot()
frames = {tf: analyse(tf, snap.candles[tf]) for tf in ("M5", "M15", "H1", "H4")}
now = datetime.datetime.now(datetime.timezone.utc)
sess = session_info(now)
news = news_risk([], now, 20, 15)
price = snap.quote.mid

cand = Candidate(
    side="BUY",
    setup="FVG_PULLBACK",
    entry_low=round(price - 0.0003, 5),
    entry_high=round(price + 0.0003, 5),
    trigger=price,
    stop=round(price - 0.0012, 5),
    tp1=round(price + 0.0024, 5),
    tp2=round(price + 0.0036, 5),
    tp3=None,
    rr=2.0,
    risk_pips=12.0,
    reward_pips=24.0,
    expected_hold_min=60,
    max_hold_min=240,
    context={
        "h4": "H4 range",
        "h1": "H1 bull",
        "m15": "M15 bull",
        "m5": "close above zone",
        "liquidity": "2 вище",
        "levels": "PDH 1.1040",
    },
    why=["Ціна повернулась у FVG M15."],
    cancel_rules=["M5 закрилась за рівнем інвалідації."],
    invalidations=["Закриття M5 нижче рівня."],
    score_parts=[{"label": "Відповідність H4/H1", "points": 20, "max": 25, "note": "ok"}],
    score=78,
    grade="A",
)
sig = build_signal(cand, snap, sess, ENV.risk, now, SYMBOL, news)
text = notify.format_signal(sig)

print("message chars:", len(text))
print("real price present:", f"{price:.5f}" in text)
print("b tags balanced:", text.count("<b>") == text.count("</b>"))
print("spread labelled assumed:", "припущ" in text)
print("under 4096 (Telegram limit):", len(text) < 4096)
print("\n--- excerpt ---")
for line in text.split("\n")[:26]:
    print(line)

notify.send = real_send
print("\ntransport restored")
