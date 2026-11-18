"""morning.py tests with fake Gmail and a fake scorer; no network or API calls."""

import os
import sys
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JOBSCOUT_RUBRIC"] = str(ROOT / "rubric.example.md")  # before importing score
os.environ["JOBSCOUT_FACTS"] = str(ROOT / "facts.example.md")

import db  # noqa: E402
import gmail  # noqa: E402
import morning  # noqa: E402
import score  # noqa: E402
from parse import Listing  # noqa: E402

TODAY = date(2026, 11, 8)
DAY = 86400


def listing(job_id, title="Senior Business Analyst"):
    return Listing(job_id, title, "Example Bank", "Auckland", f"https://www.linkedin.com/jobs/view/{job_id}/")


def save(conn, msg_id, *listings):
    db.save_email(conn, {"id": msg_id, "internal_date": 1_700_000_000_000, "from": "x", "subject": "s"},
                  "parsed", list(listings), [])


class FakeScorer:
    """Scores by job ID from a table; IDs in `fail` count as errors, like score_rows."""

    def __init__(self, table, fail=()):
        self.table, self.fail, self.calls = table, set(fail), []

    def __call__(self, conn, todo, effort, usage=None, echo=None):
        self.calls.append([r["job_id"] for r in todo])
        scored = errors = 0
        for r in todo:
            if r["job_id"] in self.fail:
                db.mark_score_error(conn, r["job_id"], "boom")
                errors += 1
                continue
            s = self.table[r["job_id"]]
            verdict = "shortlist" if s >= 70 else "maybe" if s >= 40 else "skip"
            db.save_score(conn, r["job_id"], score.MODEL, score.prompt_sha(),
                          SimpleNamespace(score=s, verdict=verdict, reasons=[f"r{s}a", f"r{s}b"],
                                          pay_known=False), effort)
            usage.add(SimpleNamespace(input_tokens=100, output_tokens=20,
                                      cache_read_input_tokens=1000, cache_creation_input_tokens=0))
            scored += 1
        return scored, errors


class MorningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.conn = db.connect(self.dir / "t.db")
        self.new_emails = []      # (msg_id, listings) the fake fetch will "find"
        self.fetch_counts = {}

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def fake_fetch(self, service, conn, ids, echo=None):
        for msg_id, listings in self.new_emails:
            save(conn, msg_id, *listings)
        self.new_emails = []
        return {"known": 0, "skipped": 0, "parsed": 1, "partial": 0, "failed": 0, "new_listings": 0,
                **self.fetch_counts}

    def run_morning(self, scorer=None, get_service=None, today=TODAY):
        self.services = []

        def service(interactive=False):
            self.services.append(interactive)
            return object()

        return morning.run(today=today, connect=lambda: self.conn, get_service=get_service or service,
                           list_ids=lambda s, query: [], fetch_new=self.fake_fetch,
                           score_rows=scorer or FakeScorer({}), digest_dir=self.dir / "digests")

    def lines(self, out):
        return out.digest.read_text(encoding="utf-8").splitlines()

    def test_expired_sign_in_is_first_line_and_nothing_scored(self):
        def expired(interactive=False):
            raise gmail.SignInExpired("expired")
        scorer = FakeScorer({})
        out = self.run_morning(scorer, get_service=expired)
        self.assertEqual(self.lines(out)[0], "Gmail sign-in expired. Run: python jobscout.py auth")
        self.assertEqual(scorer.calls, [])
        self.assertFalse(out.ok)
        self.assertNotIn("unknown", out.digest.read_text(encoding="utf-8"))  # no redundant note

    def test_gmail_is_never_interactive(self):
        self.run_morning()
        self.assertEqual(self.services, [False])

    def test_quiet_day(self):
        db.record_auth(self.conn)
        out = self.run_morning()
        self.assertEqual(self.lines(out)[0], "OK: no new listings")
        self.assertTrue(out.ok)

    def test_ok_line_order_and_skips_hidden(self):
        db.record_auth(self.conn)
        self.new_emails = [("m1", [listing("1", "Maybe role"), listing("2", "Top role"),
                                   listing("3", "Skip role"), listing("4", "Second shortlist")])]
        out = self.run_morning(FakeScorer({"1": 55, "2": 90, "3": 10, "4": 75}))
        text = out.digest.read_text(encoding="utf-8")
        self.assertEqual(self.lines(out)[0], "OK: 4 new listings (2 shortlist, 1 maybe, 1 skipped)")
        self.assertLess(text.index("Top role"), text.index("Second shortlist"))
        self.assertLess(text.index("Second shortlist"), text.index("Maybe role"))
        self.assertNotIn("Skip role", text)
        self.assertIn("- r90a", text)
        self.assertIn("https://www.linkedin.com/jobs/view/2/", text)
        self.assertEqual(out.scored, 4)

    def test_existing_listings_are_baseline_and_never_digested(self):
        path = self.dir / "old.db"
        old = db.connect(path)
        save(old, "m0", listing("9"))
        old.execute("UPDATE listings SET digested_on = NULL")  # simulate a pre-digest database
        old.execute("ALTER TABLE listings DROP COLUMN digested_on")
        old.commit()
        old.close()
        self.conn.close()
        self.conn = db.connect(path)  # migration marks the old listing as baseline
        scorer = FakeScorer({"9": 90})
        out = self.run_morning(scorer)
        self.assertEqual(scorer.calls, [])
        self.assertEqual(self.lines(out)[0], "OK: no new listings")

    def test_listing_digested_once_and_same_day_rerun_keeps_it(self):
        self.new_emails = [("m1", [listing("1")])]
        scorer = FakeScorer({"1": 80})
        self.run_morning(scorer)
        out = self.run_morning(scorer)  # same day: no rescoring, digest still lists it
        self.assertEqual(scorer.calls, [["1"]])
        self.assertIn("OK: 1 new listing (1 shortlist, 0 maybe, 0 skipped)", self.lines(out)[0])
        tomorrow = self.run_morning(scorer, today=date(2026, 11, 9))
        self.assertEqual(self.lines(tomorrow)[0], "OK: no new listings")

    def test_score_failure_first_line_then_retried_next_day(self):
        self.new_emails = [("m1", [listing("1"), listing("2")])]
        out = self.run_morning(FakeScorer({"2": 80}, fail={"1"}))
        lines = self.lines(out)
        self.assertTrue(lines[0].startswith("FAILED: 1 listing could not be scored"))
        self.assertIn("1 new listing (1 shortlist", out.digest.read_text(encoding="utf-8"))
        retry = FakeScorer({"1": 50})
        self.run_morning(retry, today=date(2026, 11, 9))
        self.assertEqual(retry.calls, [["1"]])

    def test_parse_failure_is_first_line(self):
        self.fetch_counts = {"failed": 2}
        out = self.run_morning()
        self.assertTrue(self.lines(out)[0].startswith("FAILED: 2 alert emails could not be parsed"))

    def test_unexpected_error_is_first_line(self):
        def broken(interactive=False):
            raise OSError("network down")
        with self.assertLogs("morning", "ERROR"):
            out = self.run_morning(get_service=broken)
        self.assertEqual(self.lines(out)[0], "FAILED: OSError: network down")

    def test_scoring_aborted_is_first_line(self):
        def aborted(*a, **k):
            raise score.ScoringAborted("AuthenticationError (status 401); check .env and model name")
        self.new_emails = [("m1", [listing("1")])]
        out = self.run_morning(aborted)
        self.assertTrue(self.lines(out)[0].startswith("FAILED: scoring stopped: AuthenticationError"))

    def test_not_signed_in(self):
        def none(interactive=False):
            raise gmail.NotSignedIn("Not authorised.")
        out = self.run_morning(get_service=none)
        self.assertEqual(self.lines(out)[0], morning.NOT_SIGNED_IN_LINE)

    def test_log_line(self):
        self.new_emails = [("m1", [listing("1")])]
        out = self.run_morning(FakeScorer({"1": 80}))
        line = morning.log_line(out, datetime(2026, 11, 8, 7, 30))
        self.assertTrue(line.startswith("2026-11-08 07:30:00 OK scored=1 tokens in=100 cache_read=1000 "
                                        "cache_write=0 out=20 cost~$0.0000 | OK: 1 new listing"))
        self.assertNotIn("\n", line)


class AuthNoteTests(unittest.TestCase):
    def at(self, y, m, d):
        return int(time.mktime(date(y, m, d).timetuple())) + 3600

    def test_no_note_before_day_five(self):
        self.assertIsNone(morning.auth_note(self.at(2026, 11, 4), TODAY))  # 4 days

    def test_warns_from_day_five(self):
        note = morning.auth_note(self.at(2026, 11, 3), TODAY)  # 5 days
        self.assertEqual(note, "Gmail sign-in expires soon (signed in 2026-11-03, expires around "
                               "2026-11-10). Run: python jobscout.py auth")

    def test_unknown_date(self):
        self.assertIn("date unknown", morning.auth_note(None, TODAY))

    def test_warning_is_second_line_after_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            conn.execute("INSERT INTO auth_log (authed_at) VALUES (?)", (self.at(2026, 11, 2),))
            conn.commit()
            out = morning.run(today=TODAY, connect=lambda: conn, get_service=lambda interactive: object(),
                              list_ids=lambda s, query: [],
                              fetch_new=lambda *a, **k: {"failed": 0, "partial": 0},
                              score_rows=FakeScorer({}), digest_dir=Path(tmp) / "d")
            lines = out.digest.read_text(encoding="utf-8").splitlines()
            conn.close()
        self.assertEqual(lines[0], "OK: no new listings")
        self.assertTrue(lines[2].startswith("Gmail sign-in expires soon (signed in 2026-11-02"))


class UsageTests(unittest.TestCase):
    def test_cost(self):
        u = score.Usage()
        u.add(SimpleNamespace(input_tokens=1_000_000, output_tokens=1_000_000,
                              cache_read_input_tokens=1_000_000, cache_creation_input_tokens=1_000_000))
        self.assertAlmostEqual(u.cost, 0.10 + 0.50 + 0.01 + 0.125)


if __name__ == "__main__":
    unittest.main()
