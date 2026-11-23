"""Persistence layer.

Production uses Postgres (Render free Postgres or Neon/Supabase free tier) via
DATABASE_URL. If no database is configured the service falls back to a local SQLite
file so it still runs, and the dashboard reports that state honestly. Render's
ephemeral disk must never hold the only copy of live history.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS signals (
  id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  status TEXT NOT NULL,
  grade TEXT NOT NULL,
  score INTEGER NOT NULL,
  setup TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  origin TEXT NOT NULL DEFAULT 'live',
  payload JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_signals_status ON signals (status);

CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  currency TEXT NOT NULL,
  impact TEXT NOT NULL,
  time_utc TIMESTAMPTZ NOT NULL,
  source TEXT NOT NULL,
  url TEXT,
  actual TEXT,
  forecast TEXT,
  previous TEXT,
  fetched_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (time_utc);

CREATE TABLE IF NOT EXISTS decisions (
  id SERIAL PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL,
  verdict TEXT NOT NULL,
  session TEXT NOT NULL,
  details JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_decisions_at ON decisions (at DESC);

CREATE TABLE IF NOT EXISTS logs (
  id SERIAL PRIMARY KEY,
  at TIMESTAMPTZ NOT NULL,
  level TEXT NOT NULL,
  scope TEXT NOT NULL,
  message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  key TEXT PRIMARY KEY,
  sent_at TIMESTAMPTZ NOT NULL,
  kind TEXT NOT NULL,
  signal_id TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL
);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS signals (id TEXT PRIMARY KEY, symbol TEXT, side TEXT, status TEXT, grade TEXT,
  score INTEGER, setup TEXT, created_at TEXT, updated_at TEXT, origin TEXT, payload TEXT);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals (created_at DESC);
CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, title TEXT, currency TEXT, impact TEXT,
  time_utc TEXT, source TEXT, url TEXT, actual TEXT, forecast TEXT, previous TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, verdict TEXT, session TEXT, details TEXT);
CREATE TABLE IF NOT EXISTS logs (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, scope TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS messages (key TEXT PRIMARY KEY, sent_at TEXT, kind TEXT, signal_id TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
"""

_LOOP = threading.Lock()


class Store:
    """Small storage facade. Both backends expose the same methods."""

    def __init__(self, database_url: str = "") -> None:
        self.url = database_url
        self.backend = "postgres" if database_url.startswith(("postgres://", "postgresql://")) else "sqlite"
        self._sqlite_path = os.environ.get("SQLITE_PATH", "/tmp/alsr_state.db")
        self._ready = False
        self._lock = _LOOP

    # ---------- connections ----------
    @contextmanager
    def _conn(self):
        if self.backend == "postgres":
            import psycopg
            from psycopg.rows import dict_row

            with psycopg.connect(self.url, row_factory=dict_row) as conn:
                yield conn
                conn.commit()
        else:
            conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def init(self) -> None:
        with self._lock:
            if self._ready:
                return
            with self._conn() as conn:
                cur = conn.cursor()
                for stmt in (SCHEMA_PG if self.backend == "postgres" else SCHEMA_SQLITE).split(";"):
                    if stmt.strip():
                        cur.execute(stmt)
                # Older databases were created without the real calendar values.
                # Add the columns in place so an existing deployment keeps its data.
                for col in ("actual", "forecast", "previous"):
                    try:
                        if self.backend == "postgres":
                            cur.execute(f"ALTER TABLE events ADD COLUMN IF NOT EXISTS {col} TEXT")
                        else:
                            cur.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
                    except Exception:  # noqa: BLE001 - column already exists
                        pass
            self._ready = True

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    # ---------- signals ----------
    def save_signal(self, signal: dict) -> None:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            payload = json.dumps(signal, ensure_ascii=False)
            if self.backend == "postgres":
                cur.execute(
                    """INSERT INTO signals (id,symbol,side,status,grade,score,setup,created_at,updated_at,origin,payload)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, updated_at=EXCLUDED.updated_at, payload=EXCLUDED.payload""",
                    (signal["id"], signal["symbol"], signal["side"], signal["status"], signal["grade"], signal["score"],
                     signal["setup"], signal["created_at"], signal["updated_at"], signal.get("origin", "live"), payload),
                )
            else:
                cur.execute(
                    """INSERT INTO signals (id,symbol,side,status,grade,score,setup,created_at,updated_at,origin,payload)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at, payload=excluded.payload""",
                    (signal["id"], signal["symbol"], signal["side"], signal["status"], signal["grade"], signal["score"],
                     signal["setup"], signal["created_at"], signal["updated_at"], signal.get("origin", "live"), payload),
                )

    def list_signals(self, statuses: list[str] | None = None, limit: int = 50) -> list[dict]:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            if statuses:
                marks = ",".join(["%s"] * len(statuses)) if self.backend == "postgres" else ",".join(["?"] * len(statuses))
                cur.execute(f"SELECT payload FROM signals WHERE status IN ({marks}) ORDER BY created_at DESC LIMIT {int(limit)}", tuple(statuses))
            else:
                cur.execute(f"SELECT payload FROM signals ORDER BY created_at DESC LIMIT {int(limit)}")
            rows = cur.fetchall()
        return [r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]) for r in rows]

    def get_signal(self, signal_id: str) -> dict | None:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT payload FROM signals WHERE id = %s" if self.backend == "postgres" else "SELECT payload FROM signals WHERE id = ?", (signal_id,))
            row = cur.fetchone()
        if not row:
            return None
        return row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"])

    # ---------- events ----------
    def save_events(self, events: list[dict]) -> None:
        if not events:
            return
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            for e in events:
                if self.backend == "postgres":
                    cur.execute(
                        """INSERT INTO events (id,title,currency,impact,time_utc,source,url,actual,forecast,previous,fetched_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (id) DO UPDATE SET actual=EXCLUDED.actual, forecast=EXCLUDED.forecast,
                             previous=EXCLUDED.previous, fetched_at=EXCLUDED.fetched_at""",
                        (e["id"], e["title"], e["currency"], e["impact"], e["time_utc"], e["source"], e.get("url"),
                         e.get("actual"), e.get("forecast"), e.get("previous"), self._now()),
                    )
                else:
                    cur.execute(
                        """INSERT INTO events (id,title,currency,impact,time_utc,source,url,actual,forecast,previous,fetched_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(id) DO UPDATE SET actual=excluded.actual, forecast=excluded.forecast,
                             previous=excluded.previous, fetched_at=excluded.fetched_at""",
                        (e["id"], e["title"], e["currency"], e["impact"], e["time_utc"], e["source"], e.get("url"),
                         e.get("actual"), e.get("forecast"), e.get("previous"), self._now().isoformat()),
                    )

    def list_events(self, start: datetime, end: datetime) -> list[dict]:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            if self.backend == "postgres":
                cur.execute("SELECT * FROM events WHERE time_utc >= %s AND time_utc <= %s ORDER BY time_utc LIMIT 300", (start, end))
                rows = cur.fetchall()
            else:
                # SQLite stores timestamps as text, so a string comparison breaks as
                # soon as one row carries an offset ("03:15:00-05:00"). Compare real
                # datetimes in Python instead; the table only holds a few hundred rows.
                cur.execute("SELECT * FROM events ORDER BY time_utc DESC LIMIT 800")
                rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            t = d["time_utc"]
            try:
                d["time_utc"] = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
            except (ValueError, TypeError):
                continue
            if d["time_utc"].tzinfo is None:
                d["time_utc"] = d["time_utc"].replace(tzinfo=timezone.utc)
            if start <= d["time_utc"] <= end:
                out.append(d)
        out.sort(key=lambda e: e["time_utc"])
        return out

    # ---------- decisions & logs ----------
    def log_decision(self, decision: dict) -> None:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            payload = json.dumps(decision["details"], ensure_ascii=False)
            if self.backend == "postgres":
                cur.execute("INSERT INTO decisions (at,verdict,session,details) VALUES (%s,%s,%s,%s)",
                            (decision["at"], decision["verdict"], decision["session"], payload))
            else:
                cur.execute("INSERT INTO decisions (at,verdict,session,details) VALUES (?,?,?,?)",
                            (decision["at"].isoformat(), decision["verdict"], decision["session"], payload))

    def list_decisions(self, limit: int = 20) -> list[dict]:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM decisions ORDER BY at DESC LIMIT {int(limit)}")
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            details = d.get("details")
            d["details"] = details if isinstance(details, list) else json.loads(details or "[]")
            t = d.get("at")
            d["at"] = t if isinstance(t, datetime) else datetime.fromisoformat(t) if t else None
            out.append(d)
        return out

    def log(self, level: str, scope: str, message: str) -> None:
        try:
            self.init()
            with self._conn() as conn:
                cur = conn.cursor()
                if self.backend == "postgres":
                    cur.execute("INSERT INTO logs (at,level,scope,message) VALUES (%s,%s,%s,%s)", (self._now(), level, scope, message))
                else:
                    cur.execute("INSERT INTO logs (at,level,scope,message) VALUES (?,?,?,?)", (self._now().isoformat(), level, scope, message))
        except Exception:  # noqa: BLE001 - logging must never break a scan
            pass

    def list_logs(self, limit: int = 30) -> list[dict]:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM logs ORDER BY at DESC LIMIT {int(limit)}")
            rows = cur.fetchall()
        out = []
        for r in rows:
            d = dict(r)
            t = d.get("at")
            d["at"] = t if isinstance(t, datetime) else datetime.fromisoformat(t) if t else None
            out.append(d)
        return out

    # ---------- message dedupe ----------
    def claim_message(self, key: str, kind: str, signal_id: str | None = None) -> bool:
        """Atomically claim a message key. False means it was already sent."""
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            try:
                if self.backend == "postgres":
                    cur.execute("INSERT INTO messages (key,sent_at,kind,signal_id) VALUES (%s,%s,%s,%s)",
                                (key, self._now(), kind, signal_id))
                else:
                    cur.execute("INSERT INTO messages (key,sent_at,kind,signal_id) VALUES (?,?,?,?)",
                                (key, self._now().isoformat(), kind, signal_id))
                return True
            except Exception:  # unique violation => already claimed
                return False

    def release_message(self, key: str) -> None:
        """Roll a claim back so a failed send can be retried."""
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM messages WHERE key = %s" if self.backend == "postgres" else "DELETE FROM messages WHERE key = ?", (key,))

    # ---------- settings ----------
    def get_setting(self, key: str, default):
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = %s" if self.backend == "postgres" else "SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
        if not row:
            return default
        value = row["value"]
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    def set_setting(self, key: str, value) -> None:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            payload = json.dumps(value, ensure_ascii=False)
            if self.backend == "postgres":
                cur.execute(
                    "INSERT INTO settings (key,value,updated_at) VALUES (%s,%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
                    (key, payload, self._now()),
                )
            else:
                cur.execute(
                    "INSERT INTO settings (key,value,updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    (key, payload, self._now().isoformat()),
                )

    # ---------- state helpers ----------
    DEFAULT_STATE = {
        "last_run_at": None,
        "last_ok_at": None,
        "last_error": None,
        "paused": False,
        "runs_today": 0,
        "runs_day": None,
    }

    def state(self) -> dict:
        state = dict(self.DEFAULT_STATE)
        state.update(self.get_setting("scan_state", {}) or {})
        return state

    def set_state(self, **changes) -> dict:
        state = self.state()
        state.update(changes)
        self.set_setting("scan_state", state)
        return state

    def message_exists(self, key: str) -> bool:
        self.init()
        with self._conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT key FROM messages WHERE key = %s" if self.backend == "postgres" else "SELECT key FROM messages WHERE key = ?", (key,))
            return cur.fetchone() is not None
