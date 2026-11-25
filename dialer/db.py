import sqlite3
import threading
from contextlib import contextmanager

from .models import SQL_ACTIVE

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
  current_call_id TEXT, state_since REAL, wrap_until REAL, pause_requested INTEGER NOT NULL DEFAULT 0,
  last_heartbeat REAL, idle_since REAL);
CREATE INDEX IF NOT EXISTS ix_agents_state ON agents(campaign_id, state, idle_since);

CREATE TABLE IF NOT EXISTS borrowers(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, phone TEXT NOT NULL, priority INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL DEFAULT 4,
  next_attempt_at REAL NOT NULL DEFAULT 0, dnc INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_borrowers_pick ON borrowers(campaign_id, state, next_attempt_at);

CREATE TABLE IF NOT EXISTS calls(
  id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, borrower_id TEXT NOT NULL, agent_id TEXT,
  provider TEXT, provider_ref TEXT, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
  bound INTEGER NOT NULL, owner TEXT, lease_until REAL, created_at REAL, state_since REAL,
  initiated_at REAL, answered_at REAL, outcome TEXT, outcome_at REAL, ended_at REAL, end_reason TEXT,
  hold_deadline REAL, abandoned INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_calls_state ON calls(campaign_id, state);
CREATE INDEX IF NOT EXISTS ix_calls_outcome ON calls(campaign_id, outcome_at);
CREATE INDEX IF NOT EXISTS ix_calls_lease ON calls(state, lease_until);
-- DB-level backstops: even if application logic is wrong, these cannot be violated.
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_call_per_borrower ON calls(borrower_id) WHERE state IN {SQL_ACTIVE};
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_call_per_agent ON calls(agent_id)
  WHERE agent_id IS NOT NULL AND state IN {SQL_ACTIVE};

CREATE TABLE IF NOT EXISTS call_transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT, from_state TEXT, to_state TEXT, at REAL, cause TEXT);
CREATE INDEX IF NOT EXISTS ix_ct_call ON call_transitions(call_id);
CREATE TABLE IF NOT EXISTS agent_transitions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT, from_state TEXT, to_state TEXT, at REAL, cause TEXT, call_id TEXT);
-- Dedup ledger. One row per (call, event type): a provider event can only ever be applied once.
CREATE TABLE IF NOT EXISTS provider_events(
  call_id TEXT NOT NULL, event_type TEXT NOT NULL, provider TEXT, received_at REAL, outcome TEXT,
  PRIMARY KEY(call_id, event_type));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, campaign_id TEXT, engine TEXT, requested INTEGER,
  approved INTEGER, action TEXT, reasons TEXT, explain TEXT);
CREATE TABLE IF NOT EXISTS campaigns(id TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 1);
"""


class Database:
    """One sqlite file, one connection per thread. All writes go through `txn()` (BEGIN IMMEDIATE)."""

    def __init__(self, path: str, fast: bool = False):
        self.path = path
        self.fast = fast
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, isolation_level=None, timeout=60, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=" + ("OFF" if self.fast else "NORMAL"))
            c.execute("PRAGMA busy_timeout=60000")
            self._local.c = c
        return c

    @contextmanager
    def txn(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:      # includes SimulatedCrash: a crashed worker's txn simply never happened
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")
