"""Read alert emails (read-only), parse them and save new listings. Shared by fetch and morning."""

from datetime import datetime

import db
import gmail
import parse


def fetch_new(service, conn, ids: list[str], reprocess: bool = False, echo=print) -> dict:
    """Process the given message IDs. Returns counts: known, skipped, parsed, partial,
    failed and new_listings. echo=None prints nothing."""
    say = echo or (lambda *_: None)
    counts = {"known": 0, "skipped": 0, "parsed": 0, "partial": 0, "failed": 0, "new_listings": 0}
    for msg_id in ids:
        if not reprocess and db.email_status(conn, msg_id) in ("parsed", "partial", "skipped"):
            counts["known"] += 1
            continue  # failed emails are retried in case the parser has been fixed
        h = gmail.get_headers(service, msg_id)
        when = datetime.fromtimestamp(h["internal_date"] / 1000).strftime("%Y-%m-%d %H:%M")
        line = f"{when}  {h['from']}  |  {h['subject']}"
        reason = parse.skip_reason(h["from"], h["subject"])
        if reason:
            db.save_email(conn, h, "skipped", [], [reason])
            counts["skipped"] += 1
            say(f"{line}\n    skipped ({reason})")
            continue
        result = parse.parse_email(gmail.get_message(service, msg_id))
        status = "failed" if result.failed else "partial" if result.errors else "parsed"
        new = db.save_email(conn, h, status, result.listings, result.errors)
        counts[status] += 1
        counts["new_listings"] += new
        say(f"{line}\n    {status}: {len(result.listings)} listing(s), {new} new")
    return counts
