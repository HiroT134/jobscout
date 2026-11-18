"""Unattended daily run: fetch alerts, score new listings, write a digest.

No prompts, never opens a browser (Gmail is used non-interactively), never sends
anything. The digest's first line always states the outcome.
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from google.auth.exceptions import RefreshError

import db
import fetch
import gmail
import score

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent
DIGEST_DIR = ROOT / "data" / "digests"
QUERY = "from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)"
AUTH_LIFETIME_DAYS = 7   # Testing-mode Google apps expire refresh tokens after 7 days
WARN_FROM_DAYS = 5
EXPIRED_LINE = "Gmail sign-in expired. Run: python jobscout.py auth"
NOT_SIGNED_IN_LINE = "FAILED: Gmail is not signed in. Run: python jobscout.py auth"


@dataclass
class Outcome:
    day: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scored: int = 0
    usage: score.Usage = field(default_factory=score.Usage)
    rows: list = field(default_factory=list)   # listings digested today, with scores
    digest: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.failures


def auth_note(last_auth: int | None, today: date) -> str | None:
    if last_auth is None:
        return ("Gmail sign-in date unknown. Run: python jobscout.py auth "
                f"to start the {AUTH_LIFETIME_DAYS}-day countdown.")
    signed = date.fromtimestamp(last_auth)
    if (today - signed).days >= WARN_FROM_DAYS:
        expires = signed + timedelta(days=AUTH_LIFETIME_DAYS)
        return (f"Gmail sign-in expires soon (signed in {signed.isoformat()}, expires around "
                f"{expires.isoformat()}). Run: python jobscout.py auth")
    return None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def counts_text(rows) -> str:
    by = {v: sum(r["verdict"] == v for r in rows) for v in ("shortlist", "maybe", "skip")}
    return (f"{_plural(len(rows), 'new listing')} ({by['shortlist']} shortlist, "
            f"{by['maybe']} maybe, {by['skip']} skipped)")


def status_lines(out: Outcome) -> list[str]:
    if out.failures:
        lines = list(out.failures)
        if out.rows:
            lines.append(f"Digested today despite the failure: {counts_text(out.rows)}")
    else:
        lines = [f"OK: {counts_text(out.rows)}" if out.rows else "OK: no new listings"]
    if EXPIRED_LINE not in out.failures:
        lines += out.notes
    return lines


def render(out: Outcome) -> str:
    parts = [line + "\n" for line in status_lines(out)]  # blank lines keep them separate in Markdown
    parts.append(f"# jobscout digest {out.day}\n")
    for verdict, heading in (("shortlist", "Shortlist"), ("maybe", "Maybe")):
        group = [r for r in out.rows if r["verdict"] == verdict]  # already best first
        if not group:
            continue
        parts.append(f"## {heading} ({len(group)})\n")
        for r in group:
            reasons = "\n".join(f"- {x}" for x in json.loads(r["reasons_json"]))
            parts.append(f"### {r['score']} · {r['title']} · {r['company']} · {r['location']}\n\n"
                         f"{reasons}\n\n{r['url']}\n")
    skipped = sum(r["verdict"] == "skip" for r in out.rows)
    if skipped:
        parts.append(f"{_plural(skipped, 'skipped listing')} not shown (`python jobscout.py list --verdict skip`).\n")
    return "\n".join(parts)


def write_digest(out: Outcome, digest_dir: Path) -> Path:
    digest_dir.mkdir(parents=True, exist_ok=True)
    path = digest_dir / f"{out.day}.md"
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render(out), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _work(conn, out: Outcome, get_service, list_ids, fetch_new, score_rows) -> None:
    service = get_service(interactive=False)  # never opens a browser; expired sign-in raises
    counts = fetch_new(service, conn, list_ids(service, query=QUERY), echo=None)
    if counts["failed"]:
        out.failures.append(f"FAILED: {_plural(counts['failed'], 'alert email')} could not be parsed "
                            "(retried next run); see data/jobscout.log")
    if counts["partial"]:
        out.failures.append(f"FAILED: {_plural(counts['partial'], 'alert email')} only partly parsed, "
                            "so some listings may be missing; see data/jobscout.log")
    sha = score.prompt_sha()
    todo = db.undigested_to_score(conn, score.MODEL, sha, score.EFFORT)
    if todo:
        out.scored, errors = score_rows(conn, todo, score.EFFORT, usage=out.usage, echo=None)
        if errors:
            out.failures.append(f"FAILED: {_plural(errors, 'listing')} could not be scored "
                                "(retried next run); see data/jobscout.log")
    db.mark_digest_ready(conn, score.MODEL, sha, score.EFFORT, out.day)


def run(today: date | None = None, connect=db.connect, get_service=gmail.get_service,
        list_ids=gmail.list_message_ids, fetch_new=fetch.fetch_new, score_rows=score.score_rows,
        digest_dir: Path = DIGEST_DIR) -> Outcome:
    today = today or date.today()
    out = Outcome(day=today.isoformat())
    conn = None
    try:
        conn = connect()
        note = auth_note(db.last_auth(conn), today)
        if note:
            out.notes.append(note)
        _work(conn, out, get_service, list_ids, fetch_new, score_rows)
    except (gmail.SignInExpired, RefreshError):
        out.failures.insert(0, EXPIRED_LINE)
    except gmail.NotSignedIn:
        out.failures.insert(0, NOT_SIGNED_IN_LINE)
    except gmail.GmailError as e:
        out.failures.insert(0, f"FAILED: Gmail: {e}")
    except score.ScoringAborted as e:
        out.failures.insert(0, f"FAILED: scoring stopped: {e}")
    except Exception as e:  # anything else still has to reach the digest's first line
        log.exception("morning run failed")
        out.failures.insert(0, f"FAILED: {type(e).__name__}: {e}"[:300])
    if conn is not None:
        try:
            out.rows = db.digested_listings(conn, score.MODEL, score.prompt_sha(), score.EFFORT, out.day)
        except Exception as e:
            log.exception("could not read today's listings")
            out.failures.append(f"FAILED: could not read today's listings: {type(e).__name__}: {e}"[:300])
    out.digest = write_digest(out, digest_dir)
    return out


def log_line(out: Outcome, now: datetime | None = None) -> str:
    """The one line per run that goes to data/morning.log."""
    now = now or datetime.now()
    u = out.usage
    digest = out.digest.relative_to(ROOT).as_posix() if out.digest and out.digest.is_relative_to(ROOT) else out.digest
    return (f"{now:%Y-%m-%d %H:%M:%S} {'OK' if out.ok else 'FAILED'} scored={out.scored} "
            f"tokens in={u.input_tokens} cache_read={u.cache_read} cache_write={u.cache_write} "
            f"out={u.output_tokens} cost~${u.cost:.4f} | {status_lines(out)[0]} | {digest}")
