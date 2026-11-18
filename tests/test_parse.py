"""Tests for parse.py.

The fixtures below are synthetic but copy the structure of real LinkedIn
alerts (Oct 2026). Real samples hold personal data and stay in the
gitignored data/samples/; SampleEmailTests runs against them when present.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import parse  # noqa: E402

SAMPLES_DIR = ROOT / "data" / "samples"
TRACKING = "?trackingId=abc%3D%3D&refId=x&trk=eml-email_job_alert&otpToken=SECRET123"


def card_html(job_id, title, meta, extra="Actively recruiting"):
    href = f"https://www.linkedin.com/comm/jobs/view/{job_id}/{TRACKING}"
    return f"""
    <tr><td><a href="{href}"><img src="logo.png" alt=""></a></td></tr>
    <tr><td><a href="{href}">
      <table><tr><td class="pb-0">{title}</td></tr>
      <tr><td class="pb-0"><p class="text-xs">{meta}</p></td></tr>
      <tr><td><p class="job-card-flavor__detail">{extra}</p></td></tr></table>
    </a></td></tr>
    <tr><td><a href="{href}">{title}</a></td></tr>
    """


def email(html="", text="", sender="LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
          msg_id="m1", subject="s"):
    return {"id": msg_id, "from": sender, "subject": subject, "internal_date": 0,
            "html": html, "text": text}


SINGLE_JOB_HTML = (
    '<html><body><a href="https://www.linkedin.com/comm/jobs/alerts?x=1">Manage alerts</a><table>'
    + card_html("111", "Product Manager", "Example Software · Auckland (Hybrid)")
    + "</table></body></html>"
)

DIGEST_HTML = (
    '<html><body><a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=x">Fintech jobs</a><table>'
    + card_html("222", "Senior Technical Business Analyst", "Example Recruiting · Auckland")
    + card_html("333", "Senior Risk Transformation Analyst", "Example Bank · Auckland (Hybrid)", "2 connections")
    + card_html("444", "Transformation Design &amp; Delivery - Manager",
                "Example Consulting · Auckland, New Zealand (Hybrid)")
    + card_html("222", "Senior Technical Business Analyst", "Example Recruiting · Auckland")
    + "</table></body></html>"
)

DIGEST_TEXT = f"""Your job alert for Senior Business Analyst in AucklandManage your job alerts:  https://www.linkedin.com/comm/jobs/alerts?x=1

Senior Technical Business Analyst

Example Recruiting

Auckland



This company is actively hiring

View job: https://www.linkedin.com/comm/jobs/view/222/{TRACKING}

---------------------------------------------------------

Fintech jobs

https://www.linkedin.com/comm/jobs/search-results/?keywords=x

Client Implementation Consultant

Employer A

Ellerslie

View job: https://www.linkedin.com/comm/jobs/view/555/{TRACKING}

---------------------------------------------------------
"""


class SenderTests(unittest.TestCase):
    def test_linkedin_senders_accepted(self):
        self.assertTrue(parse.is_linkedin_alert("LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>"))
        self.assertTrue(parse.is_linkedin_alert("LinkedIn <JOBS-NOREPLY@linkedin.com>"))

    def test_other_senders_rejected(self):
        self.assertFalse(parse.is_linkedin_alert("Example Careers <no-reply@careers.example.com>"))
        self.assertFalse(parse.is_linkedin_alert("Sam Example <sam@example.com>"))
        self.assertFalse(parse.is_linkedin_alert("messages-noreply@linkedin.com"))

    def test_display_name_spoof_rejected(self):
        self.assertFalse(parse.is_linkedin_alert('"jobalerts-noreply@linkedin.com" <attacker@example.com>'))

    def test_non_linkedin_emails_skipped_and_counted(self):
        report = parse.parse_emails([
            email(SINGLE_JOB_HTML, msg_id="a"),
            email(SINGLE_JOB_HTML, sender="Me <sam@example.com>", msg_id="fwd", subject="Fwd: alert"),
            email("<p>Thanks for applying</p>", sender="careers@employer-b.example.com", msg_id="b"),
        ])
        self.assertEqual([r.msg_id for r in report.results], ["a"])
        self.assertEqual([s[0] for s in report.skipped], ["fwd", "b"])
        self.assertEqual(report.failed, [])


class HtmlTests(unittest.TestCase):
    def test_single_job_email(self):
        listings, errors = parse.parse_html(SINGLE_JOB_HTML)
        self.assertEqual(errors, [])
        self.assertEqual(listings, [parse.Listing(
            "111", "Product Manager", "Example Software", "Auckland (Hybrid)",
            "https://www.linkedin.com/jobs/view/111/")])

    def test_digest_email_dedupes_within_email(self):
        listings, errors = parse.parse_html(DIGEST_HTML)
        self.assertEqual(errors, [])
        self.assertEqual([l.job_id for l in listings], ["222", "333", "444"])
        self.assertEqual(listings[1].location, "Auckland (Hybrid)")
        self.assertEqual(listings[2].title, "Transformation Design & Delivery - Manager")
        self.assertEqual(listings[2].location, "Auckland, New Zealand (Hybrid)")

    def test_urls_are_canonical_without_tracking(self):
        listings, _ = parse.parse_html(DIGEST_HTML)
        for listing in listings:
            self.assertEqual(listing.url, f"https://www.linkedin.com/jobs/view/{listing.job_id}/")
            self.assertNotIn("otpToken", listing.url)

    def test_card_without_company_location_is_an_error(self):
        html = card_html("666", "Mystery Role", "no separator here") + card_html("777", "Ok", "Co · Auckland")
        listings, errors = parse.parse_html(html)
        self.assertEqual([l.job_id for l in listings], ["777"])
        self.assertEqual(len(errors), 1)
        self.assertIn("666", errors[0])

    def test_job_link_without_card_is_an_error(self):
        html = '<a href="https://www.linkedin.com/comm/jobs/view/888/?x=1">Some title</a>'
        listings, errors = parse.parse_html(html)
        self.assertEqual(listings, [])
        self.assertIn("888", errors[0])


class TextTests(unittest.TestCase):
    def test_text_digest(self):
        listings, errors = parse.parse_text(DIGEST_TEXT)
        self.assertEqual(errors, [])
        self.assertEqual(
            [(l.job_id, l.title, l.company, l.location) for l in listings],
            [("222", "Senior Technical Business Analyst", "Example Recruiting", "Auckland"),
             ("555", "Client Implementation Consultant", "Employer A", "Ellerslie")])

    def test_text_used_when_html_has_no_cards(self):
        result = parse.parse_email(email(html="<p>nothing</p>", text=DIGEST_TEXT))
        self.assertEqual(result.source, "text")
        self.assertEqual(len(result.listings), 2)

    def test_html_preferred_when_it_has_cards(self):
        result = parse.parse_email(email(html=SINGLE_JOB_HTML, text=DIGEST_TEXT))
        self.assertEqual(result.source, "html")
        self.assertEqual([l.job_id for l in result.listings], ["111"])


SIMILAR_TO_HTML = (
    '<table><tr><td><a href="https://www.linkedin.com/comm/jobs/view/999/?x=1">'
    "<p>Jobs similar to</p><p>Strategic Sales Director, APAC</p><p>at</p><p>Employer C</p></a></td></tr>"
    + card_html("222", "NZ &amp; Pacific Sales Owner", "Employer D Limited · New Zealand (Remote)")
    + "</table>"
)


class SimilarJobsAndNoticeTests(unittest.TestCase):
    def test_similar_to_header_is_skipped_without_error(self):
        listings, errors = parse.parse_html(SIMILAR_TO_HTML)
        self.assertEqual(errors, [])
        self.assertEqual([(l.job_id, l.company) for l in listings], [("222", "Employer D Limited")])

    def test_renewal_notice_is_a_notice_not_a_failure(self):
        subject = "Sam, want to continue receiving job alerts for Product Owner in Auckland, New Zealand?"
        report = parse.parse_emails([email("<p>Do you still want job alerts?</p>", subject=subject, msg_id="n")])
        self.assertEqual(report.results, [])
        self.assertEqual([n[0] for n in report.notices], ["n"])
        self.assertEqual(parse.skip_reason("jobalerts-noreply@linkedin.com", subject), "LinkedIn notice, no jobs")

    def test_skip_reason(self):
        self.assertEqual(parse.skip_reason("someone@example.com", "x"), "not a LinkedIn alert")
        self.assertIsNone(parse.skip_reason("jobalerts-noreply@linkedin.com", "Employer E is hiring"))


class FailureTests(unittest.TestCase):
    def test_unparseable_alert_is_counted_and_logged(self):
        with self.assertLogs("parse", level="ERROR") as logs:
            report = parse.parse_emails([email("<p>LinkedIn changed its layout</p>", "no cards", msg_id="bad")])
        self.assertEqual([r.msg_id for r in report.failed], ["bad"])
        self.assertIn("bad", logs.output[0])

    def test_partial_parse_is_logged_as_warning(self):
        html = card_html("666", "Mystery Role", "no separator") + SINGLE_JOB_HTML
        with self.assertLogs("parse", level="WARNING") as logs:
            result = parse.parse_email(email(html))
        self.assertFalse(result.failed)
        self.assertIn("partial parse", logs.output[0])

    def test_report_dedupes_across_emails(self):
        report = parse.parse_emails([email(DIGEST_HTML, msg_id="a"), email(SINGLE_JOB_HTML + DIGEST_HTML, msg_id="b")])
        self.assertEqual(sorted(l.job_id for l in report.listings), ["111", "222", "333", "444"])


def _load_samples():
    msgs = []
    for meta_file in sorted(SAMPLES_DIR.glob("*.json")):
        msg = json.loads(meta_file.read_text(encoding="utf-8"))
        msg["html"] = meta_file.with_suffix(".html").read_text(encoding="utf-8")
        msg["text"] = meta_file.with_suffix(".txt").read_text(encoding="utf-8")
        msgs.append(msg)
    return msgs


@unittest.skipUnless(list(SAMPLES_DIR.glob("*.json")), "no real samples in data/samples")
class SampleEmailTests(unittest.TestCase):
    def test_every_linkedin_sample_parses_cleanly(self):
        report = parse.parse_emails(_load_samples())
        self.assertTrue(report.results, "no LinkedIn alerts among samples")
        for r in report.results:
            with self.subTest(subject=r.subject):
                self.assertEqual(r.source, "html")
                self.assertTrue(r.listings)
                self.assertEqual(r.errors, [])
                for listing in r.listings:
                    self.assertTrue(listing.title and listing.company and listing.location)

    def test_html_and_text_agree_on_job_ids(self):
        for msg in _load_samples():
            if not parse.is_linkedin_alert(msg["from"]) or not msg["text"]:
                continue
            with self.subTest(subject=msg["subject"]):
                html_ids = [l.job_id for l in parse.parse_html(msg["html"])[0]]
                text_ids = [l.job_id for l in parse.parse_text(msg["text"])[0]]
                self.assertEqual(html_ids, text_ids)


if __name__ == "__main__":
    unittest.main()
