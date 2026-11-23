"""Read-only queries for the demo UI. No Streamlit imports, so this can be tested directly.

The database path comes from the JOBSCOUT_DB environment variable (read on each call),
default data/demo.db. Relative paths are under the repository root. The file is opened
with SQLite's read-only mode and query_only is set, so nothing here can write to it.
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = Path("data") / "demo.db"
VERDICTS = ["shortlist", "maybe", "skip"]
TZ = ZoneInfo("Pacific/Auckland")  # listing dates are shown as NZ calendar days


def db_path() -> Path:
    path = Path(os.environ.get("JOBSCOUT_DB") or DEFAULT_DB)
    return path if path.is_absolute() else ROOT / path


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the database read-only. Raises FileNotFoundError if it does not exist
    (read-only mode never creates a file)."""
    path = (path or db_path()).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"database not found: {path}")
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


@dataclass
class Filters:
    min_score: int = 0
    max_score: int = 100
    verdicts: list[str] = field(default_factory=lambda: list(VERDICTS))  # empty matches nothing
    company: str = ""                                   # case-insensitive substring; blank matches all
    locations: list[str] = field(default_factory=list)  # empty matches all
    date_from: date | None = None                       # inclusive, NZ date of first sighting
    date_to: date | None = None                         # inclusive


# each listing with its most recent score; unscored listings are left out
_JOBS_SQL = """
SELECT l.job_id, l.company, l.title, l.location, l.url, l.first_seen_at, s.score, s.verdict
FROM listings l
JOIN scores s ON s.id = (SELECT id FROM scores WHERE job_id = l.job_id
                         ORDER BY scored_at DESC, id DESC LIMIT 1)
ORDER BY s.score DESC, l.first_seen_at DESC, l.job_id
"""


def _day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, TZ).date()


def all_jobs(conn) -> list[dict]:
    """Every scored listing, best score first (ties: newest first)."""
    return [{"company": r["company"], "title": r["title"], "location": r["location"],
             "date": _day(r["first_seen_at"]), "score": r["score"], "verdict": r["verdict"],
             "url": r["url"], "job_id": r["job_id"]} for r in conn.execute(_JOBS_SQL)]


def filter_jobs(jobs: list[dict], f: Filters) -> list[dict]:
    """Jobs matching every filter, in the order given."""
    needle = f.company.strip().casefold()
    return [j for j in jobs
            if f.min_score <= j["score"] <= f.max_score
            and j["verdict"] in f.verdicts
            and (not needle or needle in j["company"].casefold())
            and (not f.locations or j["location"] in f.locations)
            and (f.date_from is None or j["date"] >= f.date_from)
            and (f.date_to is None or j["date"] <= f.date_to)]


def jobs(conn, f: Filters | None = None) -> list[dict]:
    return filter_jobs(all_jobs(conn), f or Filters())


def job_id_for_row(ids: list[str], rows: list[int]) -> str | None:
    """The job id behind a table selection. `ids` are the job ids in the order the table
    was given them; `rows` are the selected row positions in that data, which the table
    reports in data order even when the viewer has sorted it by a column. None if nothing
    is selected or the position is out of range."""
    if not rows:
        return None
    i = rows[0]
    return ids[i] if isinstance(i, int) and 0 <= i < len(ids) else None


def visible_selection(selected: str | None, ids: list[str]) -> str | None:
    """`selected` if that job is still in the table, else None (filtered out or gone)."""
    return selected if selected in ids else None


MAX_REASONS = 4


def job_detail(conn, job_id: str) -> dict | None:
    """One listing with its most recent score and reasons (at most MAX_REASONS), or None
    if there is no scored listing with that id."""
    row = conn.execute(
        """SELECT l.job_id, l.company, l.title, l.location, l.url, l.first_seen_at,
                  s.score, s.verdict, s.reasons_json
           FROM listings l
           JOIN scores s ON s.id = (SELECT id FROM scores WHERE job_id = l.job_id
                                    ORDER BY scored_at DESC, id DESC LIMIT 1)
           WHERE l.job_id = ?""", (job_id,)).fetchone()
    if row is None:
        return None
    try:
        reasons = [str(r) for r in json.loads(row["reasons_json"])][:MAX_REASONS]
    except (TypeError, ValueError):
        reasons = []
    return {"job_id": row["job_id"], "company": row["company"], "title": row["title"],
            "location": row["location"], "date": _day(row["first_seen_at"]),
            "score": row["score"], "verdict": row["verdict"], "url": row["url"], "reasons": reasons}


def locations(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT location FROM listings ORDER BY location")]


def date_bounds(conn) -> tuple[date, date] | None:
    """First and last NZ date a listing was first seen, or None if there are no listings."""
    row = conn.execute("SELECT MIN(first_seen_at), MAX(first_seen_at) FROM listings").fetchone()
    return None if row[0] is None else (_day(row[0]), _day(row[1]))
