import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JOBSCOUT_RUBRIC"] = str(ROOT / "rubric.example.md")  # before importing score
os.environ["JOBSCOUT_FACTS"] = str(ROOT / "facts.example.md")

import db  # noqa: E402
from parse import Listing  # noqa: E402
from score import ScoreResult  # noqa: E402


def msg(msg_id, when=1000):
    return {"id": msg_id, "internal_date": when, "from": "jobalerts-noreply@linkedin.com", "subject": "s"}


def listing(job_id, title="Senior BA"):
    return Listing(job_id, title, "Example Bank", "Auckland", f"https://www.linkedin.com/jobs/view/{job_id}/")


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_dedupe_by_job_id_across_emails(self):
        self.assertEqual(db.save_email(self.conn, msg("a", 1000), "parsed", [listing("1"), listing("2")], []), 2)
        self.assertEqual(db.save_email(self.conn, msg("b", 2000), "parsed", [listing("2"), listing("3")], []), 1)
        rows = {r["job_id"]: r for r in self.conn.execute("SELECT * FROM listings")}
        self.assertEqual(sorted(rows), ["1", "2", "3"])
        self.assertEqual(rows["2"]["times_seen"], 2)
        self.assertEqual(rows["2"]["first_seen_msg"], "a")
        self.assertEqual(rows["2"]["last_seen_at"], 2000)

    def test_reprocessing_an_email_does_not_double_count(self):
        db.save_email(self.conn, msg("a", 1000), "parsed", [listing("1")], [])
        db.save_email(self.conn, msg("a", 1000), "parsed", [listing("1")], [])
        db.save_email(self.conn, msg("b", 3000), "parsed", [listing("1")], [])
        row = self.conn.execute("SELECT times_seen, last_seen_at FROM listings WHERE job_id = '1'").fetchone()
        self.assertEqual((row["times_seen"], row["last_seen_at"]), (2, 3000))

    def test_email_status_and_reprocessing(self):
        self.assertIsNone(db.email_status(self.conn, "a"))
        db.save_email(self.conn, msg("a"), "failed", [], ["no cards"])
        self.assertEqual(db.email_status(self.conn, "a"), "failed")
        db.save_email(self.conn, msg("a"), "parsed", [listing("1")], [])
        self.assertEqual(db.email_status(self.conn, "a"), "parsed")

    def test_scores_keyed_on_model_and_rubric(self):
        db.save_email(self.conn, msg("a"), "parsed", [listing("1"), listing("2")], [])
        result = ScoreResult(score=80, verdict="shortlist", reasons=["a", "b"], pay_known=False)
        db.save_score(self.conn, "1", "m", "sha1", result)
        self.assertEqual([r["job_id"] for r in db.listings_to_score(self.conn, "m", "sha1")], ["2"])
        # a rubric change makes everything due again
        self.assertEqual(len(db.listings_to_score(self.conn, "m", "sha2")), 2)

    def test_scores_keyed_on_effort(self):
        db.save_email(self.conn, msg("a"), "parsed", [listing("1")], [])
        result = ScoreResult(score=80, verdict="shortlist", reasons=["a", "b"], pay_known=False)
        db.save_score(self.conn, "1", "m", "sha", result)  # default effort low
        self.assertEqual(db.listings_to_score(self.conn, "m", "sha"), [])
        self.assertEqual(len(db.listings_to_score(self.conn, "m", "sha", effort="medium")), 1)
        self.assertIsNone(db.ranked_listings(self.conn, "m", "sha", "medium")[0]["score"])
        self.assertEqual(db.ranked_listings(self.conn, "m", "sha", "low")[0]["score"], 80)

    def test_migration_adds_effort_as_low(self):
        path = Path(self.tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.executescript(db.SCHEMA.replace(",\n    effort        TEXT NOT NULL DEFAULT 'low'", ""))
        old.execute("INSERT INTO emails VALUES ('a', 1, 'f', 's', 'parsed', 1, NULL, 1)")
        old.execute("INSERT INTO listings (job_id, title, company, location, url, first_seen_msg, "
                    "first_seen_at, last_seen_at) VALUES ('1', 't', 'c', 'l', 'u', 'a', 1, 1)")
        old.execute("INSERT INTO scores (job_id, model, rubric_sha, score, verdict, reasons_json, pay_known, "
                    "scored_at) VALUES ('1', 'm', 'sha', 50, 'maybe', '[]', 0, 1)")
        old.commit()
        old.close()
        conn = db.connect(path)
        self.assertEqual(conn.execute("SELECT effort FROM scores").fetchone()[0], "low")
        conn.close()

    def test_errors_skipped_unless_retried(self):
        db.save_email(self.conn, msg("a"), "parsed", [listing("1")], [])
        db.mark_score_error(self.conn, "1", "bad output")
        self.assertEqual(db.listings_to_score(self.conn, "m", "sha"), [])
        self.assertEqual(len(db.listings_to_score(self.conn, "m", "sha", retry_errors=True)), 1)

    def test_ranked_uses_latest_score_for_current_rubric(self):
        db.save_email(self.conn, msg("a"), "parsed", [listing("1"), listing("2"), listing("3")], [])
        db.save_score(self.conn, "1", "m", "sha", ScoreResult(score=30, verdict="skip", reasons=["a", "b"], pay_known=False))
        db.save_score(self.conn, "1", "m", "sha", ScoreResult(score=75, verdict="shortlist", reasons=["a", "b"], pay_known=False))
        db.save_score(self.conn, "2", "m", "sha", ScoreResult(score=50, verdict="maybe", reasons=["a", "b"], pay_known=False))
        db.save_score(self.conn, "3", "m", "old", ScoreResult(score=99, verdict="shortlist", reasons=["a", "b"], pay_known=False))
        rows = db.ranked_listings(self.conn, "m", "sha")
        self.assertEqual([(r["job_id"], r["score"]) for r in rows], [("1", 75), ("2", 50), ("3", None)])


if __name__ == "__main__":
    unittest.main()
