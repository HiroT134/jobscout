"""Read-only queries for the demo UI. No Streamlit imports, so this can be tested directly.

The database path comes from the JOBSCOUT_DB environment variable (read on each call),
default data/demo.db. Relative paths are under the repository root. The file is opened
with SQLite's read-only mode and query_only is set, so nothing here can write to it.

The Evaluation and Sample deepdive tabs read static committed files only: results.csv,
ui/eval_summary.json and ui/sample_deepdive.md. Nothing here calls an API.
"""

import csv
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
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


# ---------- evaluation and sample deep dive (static files, no database, no API) ----------

RESULTS_CSV = ROOT / "results.csv"
EVAL_SUMMARY = ROOT / "ui" / "eval_summary.json"
SAMPLE_DEEPDIVE = ROOT / "ui" / "sample_deepdive.md"
ITERATIONS = range(5)  # tuning iterations 0 to 4, as reported in EVALUATION.md
_ITERATION_RE = re.compile(r"iteration (\d+):")


def pct(value: str | float) -> str:
    """'0.8125' -> '81.3%' (half up, as EVALUATION.md rounds); exactly 100 -> '100%'."""
    d = (Decimal(str(value)) * 100).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return "100%" if d == 100 else f"{d}%"


def _run(row: dict) -> dict:
    def count(key):
        return int(row[key]) if row.get(key) else None
    return {"agree": int(row["tune_agree"]), "compared": int(row["tune_compared"]),
            "agreement": float(row["tune_rate"]), "baseline": float(row["skip_baseline"]),
            "precision": float(row["shortlist_precision"]), "recall": float(row["shortlist_recall"]),
            "top15_shortlist": float(row["top15_shortlist"]),
            "shortlist_hits": count("shortlist_hits"), "shortlist_predicted": count("shortlist_predicted"),
            "shortlist_labelled": count("shortlist_labelled"),
            "prompt_sha": row["prompt_sha"], "rubric_sha": row["rubric_sha"]}


def load_evaluation(results_path: Path = RESULTS_CSV, summary_path: Path = EVAL_SUMMARY) -> dict:
    """The tuning iterations 0-4 and the held-out run, numbers from results.csv, labels and
    text from eval_summary.json (only what the CSV does not hold). Raises ValueError if an
    iteration is missing or repeated, if there is not exactly one held-out run, or if a count
    in the summary file disagrees with a rate in the CSV."""
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    with results_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    tuning: dict[int, dict] = {}
    for row in rows:
        m = _ITERATION_RE.match(row["note"])
        if row["split"] == "tune" and m:
            n = int(m.group(1))
            if n in tuning:
                raise ValueError(f"results.csv has iteration {n} more than once")
            tuning[n] = row
    missing = [n for n in ITERATIONS if n not in tuning]
    if missing:
        raise ValueError(f"results.csv is missing iterations {missing}")
    holdouts = [r for r in rows if r["split"] == "holdout"]
    if len(holdouts) != 1:
        raise ValueError(f"expected one held-out run in results.csv, found {len(holdouts)}")

    iterations = []
    for n in ITERATIONS:
        run = _run(tuning[n]) | {"iteration": n}
        extra = summary["iterations"][str(n)]
        run["changed"] = extra["changed"]
        for key in ("shortlist_hits", "shortlist_predicted", "shortlist_labelled"):
            if run[key] is None and key in extra:
                run[key] = extra[key]
        if run["shortlist_hits"] is not None:
            if (round(run["shortlist_hits"] / run["shortlist_predicted"], 4) != round(run["precision"], 4)
                    or round(run["shortlist_hits"] / run["shortlist_labelled"], 4) != round(run["recall"], 4)):
                raise ValueError(f"iteration {n}: shortlist counts do not match results.csv")
        iterations.append(run)
    holdout = _run(holdouts[0]) | summary["holdout"]
    holdout["top15_hits"] = round(holdout["top15_shortlist"] * 15)

    return {"headline": summary["headline"], "summary": summary["summary"],
            "caveats": summary["caveats"], "noise_check": summary["noise_check"],
            "iterations": iterations, "holdout": holdout}


def tuning_table(ev: dict) -> list[dict]:
    """Rows of the tuning table, formatted as in EVALUATION.md."""
    def with_count(rate, hits, total):
        return f"{pct(rate)} ({hits}/{total})" if hits is not None else pct(rate)
    return [{"Iteration": r["iteration"], "What changed": r["changed"],
             "Agreement": f"{pct(r['agreement'])} ({r['agree']}/{r['compared']})",
             "Shortlist precision": with_count(r["precision"], r["shortlist_hits"], r["shortlist_predicted"]),
             "Shortlist recall": with_count(r["recall"], r["shortlist_hits"], r["shortlist_labelled"])}
            for r in ev["iterations"]]


def holdout_table(ev: dict) -> list[dict]:
    h = ev["holdout"]
    return [{"Metric": "Agreement", "Result": f"{pct(h['agreement'])} ({h['agree']}/{h['compared']})"},
            {"Metric": "Shortlist precision",
             "Result": f"{pct(h['precision'])} ({h['shortlist_hits']}/{h['shortlist_predicted']})"},
            {"Metric": "Shortlist recall",
             "Result": f"{pct(h['recall'])} ({h['shortlist_hits']}/{h['shortlist_labelled']})"},
            {"Metric": "Labelled shortlists scored skip", "Result": str(h["labelled_shortlists_scored_skip"])},
            {"Metric": "Top-15 precision (labelled shortlist)", "Result": f"{h['top15_hits']} of 15"}]


def agreement_chart_rows(ev: dict) -> list[dict]:
    """One row per bar: iterations 0-4 then the held-out run, agreement as a percentage."""
    rows = [{"run": f"Iteration {r['iteration']}", "set": "Tuning (126 jobs)",
             "agreement": r["agreement"] * 100, "label": f"{r['agree']}/{r['compared']}",
             "baseline": r["baseline"] * 100} for r in ev["iterations"]]
    h = ev["holdout"]
    rows.append({"run": "Held-out", "set": "Held-out (40 jobs, run once)", "agreement": h["agreement"] * 100,
                 "label": f"{h['agree']}/{h['compared']}", "baseline": h["baseline"] * 100})
    return rows


def sample_deepdive(path: Path = SAMPLE_DEEPDIVE) -> str:
    """The hand-written fictional deep-dive sample, as markdown."""
    return path.read_text(encoding="utf-8")
