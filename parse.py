"""Parse LinkedIn job alert emails into listings.

Works only on the email content. Links are reduced to their job ID and are
never followed.
"""

import logging
import re
from dataclasses import dataclass, field
from email.utils import parseaddr

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

LINKEDIN_SENDERS = {"jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com"}
JOB_ID_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)")
VIEW_JOB_RE = re.compile(r"^View job:\s*(\S+)")
CARD_SEPARATOR = " · "  # "Company · Location"
# LinkedIn emails from the alert senders that carry no jobs.
NOTICE_SUBJECTS = [re.compile(r"want to continue receiving job alerts", re.I)]
# "New jobs similar to X" emails link the job X they are based on in a header,
# whose strings start with this. It is not a recommendation.
REFERENCE_HEADER = "Jobs similar to"


@dataclass(frozen=True)
class Listing:
    job_id: str
    title: str
    company: str
    location: str
    url: str


@dataclass
class EmailResult:
    msg_id: str
    subject: str
    listings: list[Listing] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source: str = ""  # "html" or "text"

    @property
    def failed(self) -> bool:
        return not self.listings


@dataclass
class ParseReport:
    results: list[EmailResult] = field(default_factory=list)
    skipped: list[tuple[str, str, str]] = field(default_factory=list)  # (msg_id, from, subject)
    notices: list[tuple[str, str, str]] = field(default_factory=list)  # LinkedIn emails with no jobs

    @property
    def failed(self) -> list[EmailResult]:
        return [r for r in self.results if r.failed]

    @property
    def listings(self) -> list[Listing]:
        """All listings, deduped on job_id, first occurrence wins."""
        seen: dict[str, Listing] = {}
        for r in self.results:
            for listing in r.listings:
                seen.setdefault(listing.job_id, listing)
        return list(seen.values())


def is_linkedin_alert(sender: str) -> bool:
    return parseaddr(sender)[1].lower() in LINKEDIN_SENDERS


def is_notice(subject: str) -> bool:
    """A LinkedIn alert-sender email with no jobs, such as an alert renewal prompt."""
    return any(p.search(subject) for p in NOTICE_SUBJECTS)


def skip_reason(sender: str, subject: str) -> str | None:
    if not is_linkedin_alert(sender):
        return "not a LinkedIn alert"
    if is_notice(subject):
        return "LinkedIn notice, no jobs"
    return None


def canonical_url(job_id: str) -> str:
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def _job_id(url: str) -> str | None:
    m = JOB_ID_RE.search(url)
    return m.group(1) if m else None


def parse_html(html: str) -> tuple[list[Listing], list[str]]:
    """Job cards are <a href=".../jobs/view/<id>"> whose strings are
    title, "Company · Location", then optional extras."""
    soup = BeautifulSoup(html, "html.parser")
    listings: dict[str, Listing] = {}
    linked_ids: set[str] = set()
    reference_ids: set[str] = set()
    errors: list[str] = []
    for a in soup.find_all("a", href=True):
        job_id = _job_id(a["href"])
        if not job_id:
            continue
        linked_ids.add(job_id)
        strings = list(a.stripped_strings)
        if strings and strings[0] == REFERENCE_HEADER:
            reference_ids.add(job_id)
            continue
        if len(strings) < 2 or job_id in listings:
            continue  # image or title-only link to the same job
        title, meta = strings[0], strings[1]
        if CARD_SEPARATOR not in meta:
            errors.append(f"job {job_id}: no 'Company · Location' line in {meta!r}")
            continue
        company, location = (s.strip() for s in meta.split(CARD_SEPARATOR, 1))
        listings[job_id] = Listing(job_id, title, company, location, canonical_url(job_id))
    for job_id in sorted(linked_ids - listings.keys() - reference_ids):
        if not any(e.startswith(f"job {job_id}:") for e in errors):
            errors.append(f"job {job_id}: linked but no job card found")
    return list(listings.values()), errors


def parse_text(text: str) -> tuple[list[Listing], list[str]]:
    """Plain-text cards are title, company, location, optional extras, then
    "View job: <url>". URL lines and ---- separators start a new block."""
    listings: dict[str, Listing] = {}
    errors: list[str] = []
    block: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = VIEW_JOB_RE.match(line)
        if m:
            job_id = _job_id(m.group(1))
            if not job_id:
                errors.append(f"unrecognised job link {m.group(1)[:60]!r}")
            elif len(block) < 3:
                errors.append(f"job {job_id}: expected title, company, location, got {block!r}")
            elif job_id not in listings:
                title, company, location = block[:3]
                listings[job_id] = Listing(job_id, title, company, location, canonical_url(job_id))
            block = []
        elif "http" in line or line.startswith("---"):
            block = []
        else:
            block.append(line)
    return list(listings.values()), errors


def parse_email(msg: dict) -> EmailResult:
    """msg is a dict as returned by gmail.get_message."""
    result = EmailResult(msg["id"], msg.get("subject", ""))
    if msg.get("html"):
        result.listings, result.errors = parse_html(msg["html"])
        result.source = "html"
    if not result.listings and msg.get("text"):
        text_listings, text_errors = parse_text(msg["text"])
        result.errors += text_errors
        if text_listings:
            result.listings, result.source = text_listings, "text"
    if result.failed:
        log.error("parse failed for %s %r: %s", result.msg_id, result.subject,
                  "; ".join(result.errors) or "no job cards found")
    elif result.errors:
        log.warning("partial parse for %s %r: %s", result.msg_id, result.subject,
                    "; ".join(result.errors))
    return result


def parse_emails(msgs: list[dict]) -> ParseReport:
    report = ParseReport()
    for msg in msgs:
        entry = (msg["id"], msg.get("from", ""), msg.get("subject", ""))
        if not is_linkedin_alert(entry[1]):
            report.skipped.append(entry)
            continue
        if is_notice(entry[2]):
            report.notices.append(entry)
            continue
        report.results.append(parse_email(msg))
    return report
