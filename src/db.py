"""SQLite schema, migrations and the job state machine (plan.md §4).

`jobs.status` is written ONLY here: at INSERT (initial `new`) and in `transition()`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

# state -> allowed next states. See plan.md §4.
PRE_APPLIED = ("new", "needs_review", "scored", "shortlisted", "tailored")
TRANSITIONS: dict[str, set[str]] = {
    "new": {"filtered_out", "ineligible", "needs_review", "scored", "closed"},
    "filtered_out": {"new"},                 # `new` only via description change (reopen)
    "ineligible": {"new"},
    "needs_review": {"scored", "ineligible", "closed", "new"},
    "scored": {"shortlisted", "skipped", "ineligible", "closed", "new"},
    "skipped": set(),
    "shortlisted": {"tailored", "closed"},
    "tailored": {"applied", "closed"},
    "applied": {"interviewing", "rejected", "ghosted"},
    "interviewing": {"offer", "rejected"},
    "offer": set(),
    "rejected": set(),
    "ghosted": set(),
    "closed": set(),
}
ALL_STATUSES = tuple(TRANSITIONS)


class InvalidTransition(Exception):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL, external_id TEXT NOT NULL,
  company TEXT, company_canonical TEXT, title TEXT, location_raw TEXT, location_norm TEXT,
  url TEXT, description TEXT, description_hash TEXT,
  posted_at TEXT, first_seen TEXT, last_seen TEXT,
  dup_of INTEGER NULL,
  bucket TEXT,
  status TEXT NOT NULL, status_reason TEXT,
  follow_up_on TEXT NULL,
  missed_runs INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE TABLE IF NOT EXISTS scores (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL, stage TEXT NOT NULL,   -- rules | eligibility | evaluate | tailor | drafts
  description_hash TEXT,
  result_json TEXT, decision TEXT,
  model TEXT, prompt_version TEXT, facts_version TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_job ON scores(job_id, stage);
CREATE TABLE IF NOT EXISTS job_events (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL,
  from_status TEXT, to_status TEXT, reason TEXT, at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY, item_id TEXT, type TEXT,   -- fact_bank_edit | watchlist_add | contact_sent
  status TEXT NOT NULL DEFAULT 'pending',            -- pending | approved | rejected
  payload TEXT, created_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, source TEXT,
  jobs_fetched INTEGER DEFAULT 0, jobs_new INTEGER DEFAULT 0, errors TEXT
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, kind TEXT NOT NULL,  -- resume_pdf | report
  path TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_usage (
  id INTEGER PRIMARY KEY, at TEXT, job_id INTEGER, stage TEXT, model TEXT, request_id TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
  cost_usd REAL
);
"""

MIGRATIONS: list[str] = [SCHEMA]  # index i -> migrate to user_version i+1


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version={i + 1}")
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def insert_job(conn: sqlite3.Connection, job: dict) -> int:
    """Insert a new job in status `new` and log the creation event. Returns the job id."""
    ts = now()
    cur = conn.execute(
        """INSERT INTO jobs (source, external_id, company, company_canonical, title, location_raw,
             location_norm, url, description, description_hash, posted_at, first_seen, last_seen,
             status, status_reason)
           VALUES (:source,:external_id,:company,:company_canonical,:title,:location_raw,
             :location_norm,:url,:description,:description_hash,:posted_at,:ts,:ts,'new','ingested')""",
        {**job, "ts": ts},
    )
    job_id = cur.lastrowid
    conn.execute(
        "INSERT INTO job_events (job_id, from_status, to_status, reason, at) VALUES (?,?,?,?,?)",
        (job_id, None, "new", "ingested", ts),
    )
    conn.commit()
    return job_id


def transition(conn: sqlite3.Connection, job_id: int, new_status: str, reason: str) -> None:
    """The only way to change `jobs.status` after insert. Rejects transitions not in TRANSITIONS."""
    row = get_job(conn, job_id)
    if row is None:
        raise KeyError(f"no job {job_id}")
    old = row["status"]
    if new_status not in TRANSITIONS.get(old, set()):
        raise InvalidTransition(f"job {job_id}: {old} -> {new_status} is not allowed")
    if not reason:
        raise ValueError("a transition needs a reason")
    ts = now()
    conn.execute("UPDATE jobs SET status=?, status_reason=? WHERE id=?", (new_status, reason, job_id))
    conn.execute(
        "INSERT INTO job_events (job_id, from_status, to_status, reason, at) VALUES (?,?,?,?,?)",
        (job_id, old, new_status, reason, ts),
    )
    conn.commit()


def log_event(conn: sqlite3.Connection, job_id: int, reason: str) -> None:
    """Record a non-status event (status unchanged)."""
    status = get_job(conn, job_id)["status"]
    conn.execute(
        "INSERT INTO job_events (job_id, from_status, to_status, reason, at) VALUES (?,?,?,?,?)",
        (job_id, status, status, reason, now()),
    )
    conn.commit()


def add_score(conn, job_id, stage, result, decision, *, model=None, prompt_version=None,
              facts_version=None, description_hash=None) -> int:
    cur = conn.execute(
        """INSERT INTO scores (job_id, stage, description_hash, result_json, decision, model,
             prompt_version, facts_version, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (job_id, stage, description_hash, json.dumps(result), decision, model,
         str(prompt_version) if prompt_version is not None else None,
         str(facts_version) if facts_version is not None else None, now()),
    )
    conn.commit()
    return cur.lastrowid


def latest_score(conn, job_id, stage, description_hash=None) -> sqlite3.Row | None:
    sql = "SELECT * FROM scores WHERE job_id=? AND stage=?"
    args: list = [job_id, stage]
    if description_hash is not None:
        sql += " AND description_hash=?"
        args.append(description_hash)
    return conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()


def score_json(row: sqlite3.Row | None) -> dict:
    return json.loads(row["result_json"]) if row and row["result_json"] else {}


def add_artifact(conn, job_id: int, kind: str, path: str | Path) -> None:
    conn.execute("DELETE FROM artifacts WHERE job_id=? AND kind=?", (job_id, kind))
    conn.execute("INSERT INTO artifacts (job_id, kind, path, created_at) VALUES (?,?,?,?)",
                 (job_id, kind, str(path), now()))
    conn.commit()


def get_artifact(conn, job_id: int, kind: str) -> str | None:
    row = conn.execute("SELECT path FROM artifacts WHERE job_id=? AND kind=?", (job_id, kind)).fetchone()
    return row["path"] if row else None


def add_approval(conn, item_id: str, type_: str, payload: dict) -> int:
    exists = conn.execute(
        "SELECT id FROM approvals WHERE item_id=? AND type=? AND status='pending'", (item_id, type_)
    ).fetchone()
    if exists:
        return exists["id"]
    cur = conn.execute("INSERT INTO approvals (item_id, type, status, payload, created_at) VALUES (?,?,?,?,?)",
                       (item_id, type_, "pending", json.dumps(payload), now()))
    conn.commit()
    return cur.lastrowid
