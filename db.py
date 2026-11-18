"""SQLite storage for emails, listings and scores."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from parse import Listing

ROOT = Path(__file__).resolve().parent
DB_FILE = ROOT / "data" / "jobscout.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    msg_id        TEXT PRIMARY KEY,
    received_at   INTEGER NOT NULL,      -- Gmail internalDate, ms since epoch
    sender        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL,         -- parsed | partial | failed | skipped
    n_listings    INTEGER NOT NULL DEFAULT 0,
    errors        TEXT,
    processed_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    job_id          TEXT PRIMARY KEY,    -- LinkedIn job ID
    title           TEXT NOT NULL,
    company         TEXT NOT NULL,
    location        TEXT NOT NULL,
    url             TEXT NOT NULL,
    first_seen_msg  TEXT NOT NULL REFERENCES emails(msg_id),
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    score_error     TEXT                 -- set when scoring failed after retry
);

CREATE TABLE IF NOT EXISTS sightings (     -- which emails each listing appeared in
    job_id  TEXT NOT NULL REFERENCES listings(job_id),
    msg_id  TEXT NOT NULL REFERENCES emails(msg_id),
    PRIMARY KEY (job_id, msg_id)
);

CREATE TABLE IF NOT EXISTS scores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        TEXT NOT NULL REFERENCES listings(job_id),
    model         TEXT NOT NULL,
    rubric_sha    TEXT NOT NULL,
    score         INTEGER NOT NULL,
    verdict       TEXT NOT NULL,
    reasons_json  TEXT NOT NULL,
    pay_known     INTEGER NOT NULL,
    scored_at     INTEGER NOT NULL,
    effort        TEXT NOT NULL DEFAULT 'low'
);
CREATE INDEX IF NOT EXISTS scores_job ON scores(job_id, model, rubric_sha);

CREATE TABLE IF NOT EXISTS job_descriptions (   -- pasted by the user, never fetched
    job_id    TEXT PRIMARY KEY REFERENCES listings(job_id),
    jd_text   TEXT NOT NULL,
    jd_sha    TEXT NOT NULL,
    added_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS research_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         TEXT NOT NULL REFERENCES listings(job_id),
    model          TEXT NOT NULL,
    started_at     INTEGER NOT NULL,
    research_text  TEXT,
    brief_json     TEXT,
    status         TEXT NOT NULL,     -- running | ok | error
    error          TEXT,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    web_searches   INTEGER NOT NULL DEFAULT 0,
    cost_usd       REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS searches (      -- every web search the model chose, with its reason
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER NOT NULL REFERENCES research_runs(id),
    seq        INTEGER NOT NULL,
    query      TEXT NOT NULL,
    reason     TEXT,                  -- NULL when the model gave none
    n_results  INTEGER NOT NULL,
    urls_json  TEXT NOT NULL,
    error      TEXT
);

CREATE TABLE IF NOT EXISTS drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES listings(job_id),
    run_id          INTEGER REFERENCES research_runs(id),
    kind            TEXT NOT NULL,     -- cover_letter | outreach
    model           TEXT NOT NULL,
    original_text   TEXT NOT NULL,
    final_text      TEXT,
    sources_json    TEXT NOT NULL,
    facts_sha       TEXT NOT NULL,
    decision        TEXT NOT NULL,     -- approved | edited | skipped | failed_checks
    check_problems  TEXT,
    decided_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_log (      -- each successful interactive Gmail sign-in
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    authed_at  INTEGER NOT NULL
);
"""

# digested_on for listings that existed before the morning digest did
DIGEST_BASELINE = "baseline"


def connect(path: Path = DB_FILE) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    if "effort" not in cols:
        # every score before this column existed was made at effort "low"
        with conn:
            conn.execute("ALTER TABLE scores ADD COLUMN effort TEXT NOT NULL DEFAULT 'low'")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(listings)")}
    if "digested_on" not in cols:
        # listings already here were reviewed by hand, so the first digest must not repeat them
        with conn:
            conn.execute("ALTER TABLE listings ADD COLUMN digested_on TEXT")  # YYYY-MM-DD
            conn.execute("UPDATE listings SET digested_on = ?", (DIGEST_BASELINE,))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def email_status(conn, msg_id: str) -> str | None:
    row = conn.execute("SELECT status FROM emails WHERE msg_id = ?", (msg_id,)).fetchone()
    return row["status"] if row else None


def save_email(conn, msg: dict, status: str, listings: list[Listing], errors: list[str]) -> int:
    """Upsert the email and its listings. Returns the number of new listings."""
    now = int(time.time())
    received = msg["internal_date"]
    with conn:
        conn.execute(
            """INSERT INTO emails (msg_id, received_at, sender, subject, status, n_listings, errors, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(msg_id) DO UPDATE SET status = excluded.status,
                 n_listings = excluded.n_listings, errors = excluded.errors,
                 processed_at = excluded.processed_at""",
            (msg["id"], received, msg.get("from", ""), msg.get("subject", ""), status,
             len(listings), "\n".join(errors) or None, now),
        )
        new = 0
        for listing in listings:
            cur = conn.execute(
                """INSERT INTO listings (job_id, title, company, location, url,
                                         first_seen_msg, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_id) DO NOTHING""",
                (listing.job_id, listing.title, listing.company, listing.location, listing.url,
                 msg["id"], received, received),
            )
            new += cur.rowcount
            conn.execute("INSERT OR IGNORE INTO sightings (job_id, msg_id) VALUES (?, ?)",
                         (listing.job_id, msg["id"]))
            # derived from sightings, so re-processing an email never double counts
            conn.execute(
                """UPDATE listings SET
                     times_seen = (SELECT COUNT(*) FROM sightings WHERE job_id = :j),
                     last_seen_at = (SELECT MAX(e.received_at) FROM sightings s
                                     JOIN emails e ON e.msg_id = s.msg_id WHERE s.job_id = :j)
                   WHERE job_id = :j""",
                {"j": listing.job_id},
            )
    return new


def listings_to_score(conn, model: str, rubric_sha: str, retry_errors: bool = False,
                      effort: str = "low") -> list[sqlite3.Row]:
    """Listings with no score for this model, prompt version and effort."""
    sql = """SELECT l.* FROM listings l
             WHERE NOT EXISTS (SELECT 1 FROM scores s WHERE s.job_id = l.job_id
                               AND s.model = ? AND s.rubric_sha = ? AND s.effort = ?)"""
    if not retry_errors:
        sql += " AND l.score_error IS NULL"
    return conn.execute(sql + " ORDER BY l.first_seen_at DESC", (model, rubric_sha, effort)).fetchall()


def save_score(conn, job_id: str, model: str, rubric_sha: str, result, effort: str = "low") -> None:
    with conn:
        conn.execute(
            """INSERT INTO scores (job_id, model, rubric_sha, score, verdict, reasons_json, pay_known,
                                   scored_at, effort)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_id, model, rubric_sha, result.score, result.verdict,
             json.dumps(result.reasons), int(result.pay_known), int(time.time()), effort),
        )
        conn.execute("UPDATE listings SET score_error = NULL WHERE job_id = ?", (job_id,))


def mark_score_error(conn, job_id: str, error: str) -> None:
    with conn:
        conn.execute("UPDATE listings SET score_error = ? WHERE job_id = ?", (error[:500], job_id))


def record_auth(conn) -> None:
    with conn:
        conn.execute("INSERT INTO auth_log (authed_at) VALUES (?)", (int(time.time()),))


def last_auth(conn) -> int | None:
    """Unix time of the last successful sign-in, or None if none was recorded."""
    return conn.execute("SELECT MAX(authed_at) AS t FROM auth_log").fetchone()["t"]


_NO_SCORE = """NOT EXISTS (SELECT 1 FROM scores s WHERE s.job_id = l.job_id
                         AND s.model = ? AND s.rubric_sha = ? AND s.effort = ?)"""


def undigested_to_score(conn, model: str, rubric_sha: str, effort: str) -> list[sqlite3.Row]:
    """Listings not yet in any digest and with no score for this key (earlier errors included)."""
    return conn.execute(f"SELECT l.* FROM listings l WHERE l.digested_on IS NULL AND {_NO_SCORE}"
                        " ORDER BY l.first_seen_at DESC", (model, rubric_sha, effort)).fetchall()


def mark_digest_ready(conn, model: str, rubric_sha: str, effort: str, day: str) -> int:
    """Mark undigested listings that now have a score for this key as digested on `day`."""
    with conn:
        return conn.execute(
            """UPDATE listings SET digested_on = ? WHERE digested_on IS NULL
               AND EXISTS (SELECT 1 FROM scores s WHERE s.job_id = listings.job_id
                           AND s.model = ? AND s.rubric_sha = ? AND s.effort = ?)""",
            (day, model, rubric_sha, effort)).rowcount


def digested_listings(conn, model: str, rubric_sha: str, effort: str, day: str) -> list[sqlite3.Row]:
    """Listings digested on `day` with their latest score for this key, best first."""
    return [r for r in ranked_listings(conn, model, rubric_sha, effort)
            if r["digested_on"] == day and r["score"] is not None]


def ranked_listings(conn, model: str, rubric_sha: str, effort: str = "low") -> list[sqlite3.Row]:
    """Every listing with its latest score for this model, prompt and effort (NULL if unscored), best first."""
    return conn.execute(
        """SELECT l.*, s.score, s.verdict, s.reasons_json, s.pay_known
           FROM listings l
           LEFT JOIN scores s ON s.id = (
               SELECT id FROM scores WHERE job_id = l.job_id AND model = ? AND rubric_sha = ? AND effort = ?
               ORDER BY scored_at DESC, id DESC LIMIT 1)
           ORDER BY s.score IS NULL, s.score DESC, l.first_seen_at DESC""",
        (model, rubric_sha, effort),
    ).fetchall()
