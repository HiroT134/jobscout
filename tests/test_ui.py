import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_demo_db  # noqa: E402
from ui import data  # noqa: E402


class UiDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "demo.db"
        make_demo_db.build(cls.path)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.conn = data.connect(self.path)

    def tearDown(self):
        self.conn.close()

    def test_path_from_env(self):
        with mock.patch.dict(os.environ, {"JOBSCOUT_DB": str(self.path)}):
            self.assertEqual(data.db_path(), self.path)
        with mock.patch.dict(os.environ, {"JOBSCOUT_DB": ""}):
            self.assertEqual(data.db_path(), ROOT / "data" / "demo.db")

    def test_missing_file_is_not_created(self):
        missing = Path(self.tmp.name) / "nope.db"
        with self.assertRaises(FileNotFoundError):
            data.connect(missing)
        self.assertFalse(missing.exists())

    def test_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.conn.execute("DELETE FROM listings")

    def test_all_jobs_sorted_by_score(self):
        jobs = data.jobs(self.conn)
        self.assertEqual(len(jobs), 60)
        scores = [j["score"] for j in jobs]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(set(jobs[0]),
                         {"company", "title", "location", "date", "score", "verdict", "url", "job_id"})

    def test_score_and_verdict_filters(self):
        self.assertEqual(len(data.jobs(self.conn, data.Filters(verdicts=["shortlist"]))), 9)
        self.assertEqual(data.jobs(self.conn, data.Filters(verdicts=[])), [])
        jobs = data.jobs(self.conn, data.Filters(min_score=40, max_score=69))
        self.assertEqual(len(jobs), 15)
        self.assertTrue(all(j["verdict"] == "maybe" for j in jobs))

    def test_company_search_ignores_case_and_spaces(self):
        jobs = data.jobs(self.conn, data.Filters(company="  KAURI "))
        self.assertTrue(jobs)
        self.assertTrue(all(j["company"] == "Kauri Analytics" for j in jobs))

    def test_location_filter(self):
        self.assertIn("Wellington", data.locations(self.conn))
        jobs = data.jobs(self.conn, data.Filters(locations=["Wellington"]))
        self.assertTrue(jobs)
        self.assertTrue(all(j["location"] == "Wellington" and j["verdict"] == "skip" for j in jobs))

    def test_date_range(self):
        first, last = data.date_bounds(self.conn)
        self.assertLessEqual((last - first).days, 29)
        self.assertEqual(len(data.jobs(self.conn, data.Filters(date_from=first, date_to=last))), 60)
        one_day = data.jobs(self.conn, data.Filters(date_from=last, date_to=last))
        self.assertTrue(one_day)
        self.assertTrue(all(j["date"] == last for j in one_day))
        self.assertEqual(data.jobs(self.conn, data.Filters(date_from=date(2000, 1, 1),
                                                           date_to=date(2000, 1, 2))), [])


class AppTests(unittest.TestCase):
    def test_app_runs_and_shows_rows(self):
        from streamlit.testing.v1 import AppTest

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.db"
            make_demo_db.build(path)
            with mock.patch.dict(os.environ, {"JOBSCOUT_DB": str(path)}):
                at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
            self.assertFalse(at.exception, at.exception)
            self.assertEqual(len(at.dataframe[0].value), 60)
            self.assertIn("60 of 60 jobs", at.subheader[0].value)
            self.assertIn("fictional", at.info[0].value)


if __name__ == "__main__":
    unittest.main()
