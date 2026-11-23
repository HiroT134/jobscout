"""Build data/demo.db: a reproducible demo database of fictional listings and scores.

Uses the same tables as db.py (it calls db.connect, so the schema and migrations are
identical). Every company, title and link is made up; links point at jobs.example.com.
No API or network calls are made. A fixed seed and a fixed end date make the output the
same on every run; the listings span the 30 days before the end date.

Scores are stored under score.MODEL and the prompt hash for rubric.example.md, so the
demo shows up in jobscout.py when JOBSCOUT_RUBRIC=rubric.example.md.

Usage: python scripts/make_demo_db.py [--out PATH] [--end-date YYYY-MM-DD]
"""

import argparse
import json
import os
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# read at import by score.py, so set before importing it
os.environ["JOBSCOUT_RUBRIC"] = str(ROOT / "rubric.example.md")

import db  # noqa: E402
import score  # noqa: E402  (imported for MODEL, prompt_sha and ScoreResult only; no client is made)

SEED = 20261123
DEFAULT_OUT = ROOT / "data" / "demo.db"
DEFAULT_END = date(2026, 11, 23)
DAYS = 30
N_LISTINGS = 60
# 15% shortlist, 25% maybe, 60% skip of 60 listings
COUNTS = {"shortlist": 9, "maybe": 15, "skip": 36}
SCORE_RANGES = {"shortlist": (70, 95), "maybe": (40, 69), "skip": (5, 39)}
SENDER = "alerts@jobs.example.com"
NZ = timezone(timedelta(hours=13))  # NZDT, fixed so the dates do not depend on the machine

# fictional companies and the kind of business each one is
COMPANIES = {
    "Kauri Analytics": "software product company",
    "Rimu Freight": "logistics company",
    "Totara Mutual": "insurer",
    "Pohutukawa Health": "health provider",
    "Tui Ledger Software": "software product company",
    "Harakeke Energy": "energy retailer",
    "Matai Assurance": "insurer",
    "Kowhai Labs": "software product company",
    "Ruru Advisory": "consulting firm",
    "Kahikatea Savings Bank": "bank",
    "Pukeko Payments": "fintech product company",
    "Titoki Telecom": "telco",
    "Karaka Partners": "consulting firm",
    "Tawa Digital": "digital agency",
    "Hinau Retail Group": "retailer",
    "Mahoe Council Services": "local government shared service",
}
GOOD_COMPANIES = [c for c, kind in COMPANIES.items()
                  if kind in ("software product company", "insurer", "fintech product company", "bank")]

AUCKLAND = ["Auckland", "Auckland CBD", "Auckland (Hybrid)", "North Shore, Auckland", "Remote, New Zealand"]
OTHER_NZ = ["Wellington", "Christchurch", "Hamilton", "Tauranga", "Dunedin"]

SHORTLIST_TITLES = ["Senior Business Analyst", "Product Owner", "Senior Product Owner",
                    "Lead Business Analyst", "Business Analyst - Digital Products",
                    "Product Owner - Claims Platform", "Senior Business Analyst - Payments"]
MAYBE_TITLES = ["Business Analyst", "Technical Business Analyst", "Business Analyst (12 month contract)",
                "Product Analyst", "Business Process Analyst", "Associate Product Owner",
                "Business Analyst - Data"]
SKIP_TITLES = ["Director of Business Analysis", "Pre-sales Business Analyst", "Junior Business Analyst",
               "Business Analyst - Expression of Interest", "Graduate Business Analyst",
               "Head of Product", "Business Analyst Intern", "Product Marketing Manager"]

SHORTLIST_REASONS = ["{title} at a {kind} is a strong-fit target role.",
                     "Location suits: {location}.",
                     "Seniority in the title matches the target level.",
                     "Product delivery work in a domain the rubric favours."]
MAYBE_REASONS = ["{title} is an acceptable fit but the scope is unclear from the alert.",
                 "{company} is a {kind}, which the rubric treats as a middle case.",
                 "Seniority cannot be judged from the title alone.",
                 "Contract or analyst-level title could go either way."]
# per skip cause: the main reason, then follow-ups that hold for any listing with that cause
SKIP_REASONS = {
    "location": ["{location} is outside Auckland and the role is not listed as remote.",
                 "Relocation is outside the scope of the search.",
                 "Hybrid or remote work is not mentioned in the alert."],
    "title": ["{title} matches a poor-fit rule in the rubric.",
              "The role type is away from the BA and PO roles being targeted.",
              "Company and location alone cannot lift a poor-fit title."],
    "kind": ["{company} is a {kind}, which is outside the target industries.",
             "The title alone is not enough to outweigh the industry rule.",
             "Pay and scope are unknown, so there is nothing to offset it."],
}


def _pick_reasons(rng: random.Random, verdict: str, skip_cause: str, fields: dict) -> list[str]:
    if verdict == "skip":
        first, *rest = SKIP_REASONS[skip_cause]
        chosen = [first] + rng.sample(rest, rng.randint(1, 2))
    else:
        pool = SHORTLIST_REASONS if verdict == "shortlist" else MAYBE_REASONS
        chosen = [pool[0]] + rng.sample(pool[1:], rng.randint(1, 2))
    return [r.format(**fields) for r in chosen]


def _listing(rng: random.Random, verdict: str) -> dict:
    skip_cause = ""
    if verdict == "shortlist":
        title, company, location = rng.choice(SHORTLIST_TITLES), rng.choice(GOOD_COMPANIES), rng.choice(AUCKLAND)
    elif verdict == "maybe":
        title, company, location = rng.choice(MAYBE_TITLES), rng.choice(list(COMPANIES)), rng.choice(AUCKLAND)
    else:
        skip_cause = rng.choice(["location", "title", "title", "kind"])
        title = rng.choice(SKIP_TITLES if skip_cause == "title" else SHORTLIST_TITLES + MAYBE_TITLES)
        company = rng.choice([c for c, k in COMPANIES.items() if k not in
                              ("software product company", "insurer", "fintech product company")]
                             if skip_cause == "kind" else list(COMPANIES))
        location = rng.choice(OTHER_NZ if skip_cause == "location" else AUCKLAND[:4])
    fields = {"title": title, "company": company, "kind": COMPANIES[company], "location": location}
    lo, hi = SCORE_RANGES[verdict]
    return {**fields, "verdict": verdict, "score": rng.randint(lo, hi),
            "reasons": _pick_reasons(rng, verdict, skip_cause, fields),
            "pay_known": rng.random() < 0.1}


def build(out: Path, end: date = DEFAULT_END) -> dict:
    """Write the demo database to `out`, replacing any file there. Returns verdict counts."""
    rng = random.Random(SEED)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    conn = db.connect(out)
    model, sha = score.MODEL, score.prompt_sha()

    # one alert email a day, around 7am
    emails = []
    for d in range(DAYS):
        day = end - timedelta(days=DAYS - 1 - d)
        sent = int(datetime(day.year, day.month, day.day, 7, rng.randint(0, 59), tzinfo=NZ).timestamp())
        emails.append((f"demo-msg-{d + 1:03d}", sent, day.isoformat()))

    verdicts = [v for v, n in COUNTS.items() for _ in range(n)]
    rng.shuffle(verdicts)
    job_ids = rng.sample(range(4_000_000_000, 4_999_999_999), N_LISTINGS)

    with conn:
        for msg_id, sent, _ in emails:
            conn.execute(
                """INSERT INTO emails (msg_id, received_at, sender, subject, status, n_listings,
                                       errors, processed_at) VALUES (?, ?, ?, ?, 'parsed', 0, NULL, ?)""",
                (msg_id, sent * 1000, SENDER, "[Demo] job alert: new roles for you", sent + 60))
        for job_id, verdict in zip(job_ids, verdicts):
            item = _listing(rng, verdict)
            jid = str(job_id)
            first = rng.randrange(len(emails))
            seen = sorted({first, *rng.sample(range(first, len(emails)), min(rng.randint(0, 2), len(emails) - first))})
            first_msg, first_sent, first_day = emails[seen[0]]
            last_sent = emails[seen[-1]][1]
            conn.execute(
                """INSERT INTO listings (job_id, title, company, location, url, first_seen_msg,
                                         first_seen_at, last_seen_at, times_seen, score_error, digested_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                (jid, item["title"], item["company"], item["location"], f"https://jobs.example.com/{jid}",
                 first_msg, first_sent * 1000, last_sent * 1000, len(seen), first_day))
            for i in seen:
                conn.execute("INSERT INTO sightings (job_id, msg_id) VALUES (?, ?)", (jid, emails[i][0]))
            result = score.ScoreResult(score=item["score"], verdict=item["verdict"],
                                       reasons=item["reasons"], pay_known=item["pay_known"])
            conn.execute(
                """INSERT INTO scores (job_id, model, rubric_sha, score, verdict, reasons_json, pay_known,
                                       scored_at, effort) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'low')""",
                (jid, model, sha, result.score, result.verdict, json.dumps(result.reasons),
                 int(result.pay_known), first_sent + 120))
        conn.execute("""UPDATE emails SET n_listings =
                          (SELECT COUNT(*) FROM sightings s WHERE s.msg_id = emails.msg_id)""")
    counts = {v: n for v, n in conn.execute("SELECT verdict, COUNT(*) FROM scores GROUP BY verdict")}
    conn.close()
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output file (default: {DEFAULT_OUT})")
    ap.add_argument("--end-date", type=date.fromisoformat, default=DEFAULT_END,
                    help=f"last day of the 30-day window (default: {DEFAULT_END})")
    args = ap.parse_args(argv)
    out = args.out.resolve()
    if out == db.DB_FILE.resolve():
        print(f"ERROR: refusing to overwrite the real database {db.DB_FILE}", file=sys.stderr)
        return 2
    counts = build(out, args.end_date)
    total = sum(counts.values())
    print(f"Wrote {out}: {total} listings, "
          + ", ".join(f"{v} {counts.get(v, 0)}" for v in COUNTS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
