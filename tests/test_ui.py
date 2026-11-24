import html
import json
import os
import re
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

    def test_filter_default_is_all_verdicts(self):
        self.assertEqual(data.Filters().verdicts, ["shortlist", "maybe", "skip"])
        self.assertEqual(len(data.jobs(self.conn, data.Filters())), 60)

    def test_job_detail(self):
        for job in data.jobs(self.conn):
            detail = data.job_detail(self.conn, job["job_id"])
            for key in ("company", "title", "location", "date", "score", "verdict", "url"):
                self.assertEqual(detail[key], job[key], key)
            self.assertTrue(2 <= len(detail["reasons"]) <= 4, detail["reasons"])
            self.assertTrue(all(isinstance(r, str) and r for r in detail["reasons"]))
        self.assertIsNone(data.job_detail(self.conn, "no-such-job"))

    def test_job_detail_uses_latest_score(self):
        job_id = data.jobs(self.conn)[0]["job_id"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "copy.db"
            path.write_bytes(self.path.read_bytes())
            rw = sqlite3.connect(path)
            rw.execute("""INSERT INTO scores (job_id, model, rubric_sha, score, verdict, reasons_json,
                                              pay_known, scored_at, effort)
                          VALUES (?, 'm', 'r', 12, 'skip', ?, 0, 9999999999, 'low')""",
                       (job_id, '["a", "b", "c", "d", "e"]'))
            rw.commit()
            rw.close()
            conn = data.connect(path)
            detail = data.job_detail(conn, job_id)
            conn.close()
        self.assertEqual((detail["score"], detail["verdict"]), (12, "skip"))
        self.assertEqual(detail["reasons"], ["a", "b", "c", "d"])  # capped at four

    def test_date_range(self):
        first, last = data.date_bounds(self.conn)
        self.assertLessEqual((last - first).days, 29)
        self.assertEqual(len(data.jobs(self.conn, data.Filters(date_from=first, date_to=last))), 60)
        one_day = data.jobs(self.conn, data.Filters(date_from=last, date_to=last))
        self.assertTrue(one_day)
        self.assertTrue(all(j["date"] == last for j in one_day))
        self.assertEqual(data.jobs(self.conn, data.Filters(date_from=date(2000, 1, 1),
                                                           date_to=date(2000, 1, 2))), [])


class SelectionLookupTests(unittest.TestCase):
    IDS = ["j1", "j2", "j3"]

    def test_job_id_for_row_uses_the_ids_the_table_was_given(self):
        self.assertEqual(data.job_id_for_row(self.IDS, [0]), "j1")
        self.assertEqual(data.job_id_for_row(self.IDS, [2]), "j3")
        # same position, different (filtered or reordered) data gives that data's job
        self.assertEqual(data.job_id_for_row(["j3", "j1"], [0]), "j3")

    def test_job_id_for_row_without_a_valid_selection(self):
        for rows in ([], [3], [-1], ["0"]):
            self.assertIsNone(data.job_id_for_row(self.IDS, rows), rows)
        self.assertIsNone(data.job_id_for_row([], [0]))

    def test_visible_selection_drops_a_filtered_out_job(self):
        self.assertEqual(data.visible_selection("j2", self.IDS), "j2")
        self.assertIsNone(data.visible_selection("j9", self.IDS))
        self.assertIsNone(data.visible_selection(None, self.IDS))
        self.assertIsNone(data.visible_selection("j2", []))


class EvaluationDataTests(unittest.TestCase):
    """The Evaluation tab's loader, against the committed results.csv and eval_summary.json."""

    def setUp(self):
        self.ev = data.load_evaluation()

    def test_iterations_and_holdout_match_evaluation_md(self):
        self.assertEqual([r["iteration"] for r in self.ev["iterations"]], [0, 1, 2, 3, 4])
        self.assertEqual([(r["agree"], r["compared"]) for r in self.ev["iterations"]],
                         [(69, 126), (81, 126), (81, 126), (81, 126), (96, 126)])
        h = self.ev["holdout"]
        self.assertEqual((h["agree"], h["compared"]), (24, 40))
        self.assertEqual((h["shortlist_hits"], h["shortlist_predicted"], h["shortlist_labelled"]), (3, 5, 7))
        self.assertEqual(h["labelled_shortlists_scored_skip"], 0)
        self.assertEqual(h["top15_hits"], 7)
        # evaluated with the iteration 4 rubric
        self.assertEqual((h["prompt_sha"], h["rubric_sha"]), ("776782997ac5", "0a468fc03475"))

    def test_noise_check_row_is_not_an_iteration(self):
        self.assertEqual(self.ev["iterations"][4]["agree"], 96)  # not the 94 of the re-run

    def test_tables_formatted_as_in_evaluation_md(self):
        tuning = data.tuning_table(self.ev)
        self.assertEqual(tuning[0], {"Iteration": 0, "What changed": "Original rubric",
                                     "Agreement": "54.8% (69/126)", "Shortlist precision": "100%",
                                     "Shortlist recall": "30.4%"})
        self.assertEqual(tuning[3]["Shortlist precision"], "81.3%")  # 0.8125 rounds half up
        self.assertEqual(tuning[4]["Agreement"], "76.2% (96/126)")
        self.assertEqual(tuning[4]["Shortlist precision"], "87.5% (14/16)")
        self.assertEqual(tuning[4]["Shortlist recall"], "60.9% (14/23)")
        self.assertEqual({r["Metric"]: r["Result"] for r in data.holdout_table(self.ev)}, {
            "Agreement": "60.0% (24/40)", "Shortlist precision": "60.0% (3/5)",
            "Shortlist recall": "42.9% (3/7)", "Labelled shortlists scored skip": "0",
            "Top-15 precision (labelled shortlist)": "7 of 15"})

    def test_chart_rows(self):
        rows = data.agreement_chart_rows(self.ev)
        self.assertEqual([r["run"] for r in rows], [f"Iteration {n}" for n in range(5)] + ["Held-out"])
        self.assertAlmostEqual(rows[-1]["agreement"], 60.0)
        self.assertTrue(all(64.9 < r["baseline"] < 65.2 for r in rows))

    def test_text_comes_from_evaluation_md(self):
        doc = (ROOT / "EVALUATION.md").read_text(encoding="utf-8")
        for line in [self.ev["headline"], self.ev["noise_check"], *self.ev["summary"], *self.ev["caveats"]]:
            self.assertIn(line, doc)

    def test_missing_or_repeated_runs_are_errors(self):
        lines = data.RESULTS_CSV.read_text(encoding="utf-8").splitlines()
        iteration2 = next(l for l in lines if ",iteration 2:" in l)
        holdout = next(l for l in lines if ",holdout," in l)
        cases = [([l for l in lines if ",iteration 2:" not in l], "missing iterations"),
                 (lines + [iteration2], "more than once"),
                 ([l for l in lines if ",holdout," not in l], "found 0"),
                 (lines + [holdout], "found 2")]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            for rows, message in cases:
                path.write_text("\n".join(rows) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    data.load_evaluation(path)

    def test_counts_that_disagree_with_the_csv_are_errors(self):
        summary = json.loads(data.EVAL_SUMMARY.read_text(encoding="utf-8"))
        summary["iterations"]["4"]["shortlist_hits"] = 13
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.json"
            path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "do not match"):
                data.load_evaluation(summary_path=path)

    def test_pct(self):
        self.assertEqual(data.pct("0.8125"), "81.3%")
        self.assertEqual(data.pct("1.0000"), "100%")
        self.assertEqual(data.pct(0.6), "60.0%")


class SampleDeepdiveTests(unittest.TestCase):
    def setUp(self):
        self.text = data.sample_deepdive()

    def test_has_every_section_and_is_labelled_fictional(self):
        self.assertTrue(self.text.startswith("# FICTIONAL"))
        for heading in ("## Employer brief", "## Draft cover letter", "## Draft outreach message",
                        "## Sources (fictional)"):
            self.assertIn(heading, self.text)

    def test_job_is_in_the_demo_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "demo.db"
            make_demo_db.build(path)
            conn = data.connect(path)
            job = data.job_detail(conn, "4354797302")
            conn.close()
        self.assertEqual((job["company"], job["title"]), ("Matai Assurance", "Product Owner - Claims Platform"))
        self.assertIn(job["company"], make_demo_db.COMPANIES)
        self.assertIn(f"{job['title']} at {job['company']}", self.text)

    def test_sources_are_reserved_example_domains(self):
        urls = re.findall(r"https?://[^\s)]+", self.text)
        self.assertEqual(len(urls), 3)
        for url in urls:
            host = re.match(r"https?://([^/]+)", url).group(1)
            self.assertRegex(host, r"(^|\.)example\.(com|org|net)$", url)
        sources = self.text.split("## Sources (fictional)")[1]
        self.assertEqual(sources.count("(fictional"), 3)

    def test_no_email_addresses(self):
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", self.text))

    def test_sample_is_outside_the_ignored_data_dir(self):
        # data/ is gitignored, so a sample there could not be committed
        self.assertEqual(data.SAMPLE_DEEPDIVE.parent, ROOT / "ui")


HINT ="Click a row to see why it got its verdict."


class AppTests(unittest.TestCase):
    """Drives app.py headlessly. AppTest cannot click a dataframe row, so a click is
    simulated by writing the table's selection state, which is what the browser sends."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "demo.db"
        make_demo_db.build(cls.path)
        conn = data.connect(cls.path)
        cls.jobs = data.jobs(conn)
        cls.conn = conn

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        cls.tmp.cleanup()

    def setUp(self):
        from streamlit.testing.v1 import AppTest

        env = mock.patch.dict(os.environ, {"JOBSCOUT_DB": str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        self.at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
        self.assertNoException()

    def assertNoException(self):
        self.assertFalse(self.at.exception, self.at.exception)

    def click_row(self, i):
        key = self.at.dataframe[0].key
        self.at.session_state[key] = {"selection": {"rows": [i], "columns": [], "cells": []}}
        self.at.run()
        self.assertNoException()

    def text(self):
        return "\n".join(m.value for m in self.at.markdown)

    def panel_open(self):
        return "**Why this verdict**" in self.text()

    def test_app_runs_and_shows_rows(self):
        self.assertEqual(len(self.at.dataframe[0].value), 60)
        self.assertIn("60 of 60 jobs", self.at.subheader[0].value)
        self.assertIn("fictional", self.at.info[0].value)
        self.assertEqual(len(self.at.selectbox), 0)  # the old selectbox is gone

    def test_verdict_filter_defaults_to_all(self):
        verdict = next(w for w in self.at.multiselect if w.label == "Verdict")
        self.assertEqual(verdict.value, ["shortlist", "maybe", "skip"])

    def test_no_selection_shows_hint_and_full_width_table(self):
        self.assertIn(HINT, [c.value for c in self.at.caption])
        self.assertEqual(len(self.at.columns), 5)  # just the filter row
        self.assertFalse(self.panel_open())

    def test_row_click_opens_detail_panel(self):
        job = self.jobs[2]
        self.click_row(2)
        self.assertEqual(self.at.session_state["selected_job"], job["job_id"])
        self.assertEqual(len(self.at.columns), 7)  # filter row plus table and panel
        self.assertNotIn(HINT, [c.value for c in self.at.caption])
        text = self.text()
        self.assertIn(html.escape(job["title"]), text)
        self.assertIn(html.escape(job["company"]), text)
        for reason in data.job_detail(self.conn, job["job_id"])["reasons"]:
            self.assertIn(f"- {html.escape(reason)}", text)
        self.assertEqual(self.at.get("link_button")[0].proto.url, job["url"])

    def test_row_maps_to_job_by_id_under_filters(self):
        next(w for w in self.at.multiselect if w.label == "Verdict").set_value(["maybe"]).run()
        self.click_row(0)
        first_maybe = next(j for j in self.jobs if j["verdict"] == "maybe")
        self.assertEqual(self.at.session_state["selected_job"], first_maybe["job_id"])
        self.assertNotEqual(first_maybe["job_id"], self.jobs[0]["job_id"])

    def test_selected_job_filtered_out(self):
        self.click_row(0)  # top job, a shortlist
        self.assertEqual(self.jobs[0]["verdict"], "shortlist")
        self.assertTrue(self.panel_open())
        verdict = next(w for w in self.at.multiselect if w.label == "Verdict")
        verdict.set_value(["skip"]).run()
        self.assertNoException()
        self.assertFalse(self.panel_open())
        self.assertIn(HINT, [c.value for c in self.at.caption])
        self.assertEqual(len(self.at.columns), 5)
        self.assertIsNone(self.at.session_state["selected_job"])
        # widening the filters again does not bring back the panel or a stale row selection
        next(w for w in self.at.multiselect if w.label == "Verdict").set_value(list(data.VERDICTS)).run()
        self.assertNoException()
        self.assertFalse(self.panel_open())

    def test_selected_job_still_shown_keeps_panel(self):
        self.click_row(0)
        next(w for w in self.at.multiselect if w.label == "Verdict").set_value(["shortlist"]).run()
        self.assertNoException()
        self.assertEqual(self.at.session_state["selected_job"], self.jobs[0]["job_id"])
        self.assertTrue(self.panel_open())

    def test_close_clears_selection(self):
        self.click_row(1)
        old_key = self.at.dataframe[0].key
        next(b for b in self.at.button if b.label == "Close").click().run()
        self.assertNoException()
        self.assertFalse(self.panel_open())
        self.assertIsNone(self.at.session_state["selected_job"])
        self.assertNotEqual(self.at.dataframe[0].key, old_key)  # row highlight cleared too

    def test_tabs(self):
        self.assertEqual([t.label for t in self.at.tabs], ["Jobs", "Evaluation", "Sample deepdive"])

    def test_evaluation_tab(self):
        text = self.text()
        self.assertIn("high-recall filter", text)
        self.assertIn("What this does and does not show", text)
        self.assertEqual(len(self.at.table), 2)
        self.assertIn("76.2% (96/126)", self.at.table[0].value.to_string())
        self.assertIn("60.0% (24/40)", self.at.table[1].value.to_string())
        self.assertTrue(any("no live API call" in c.value for c in self.at.caption))

    def test_sample_tab(self):
        banner = self.at.warning[0].value
        self.assertIn("FICTIONAL", banner)
        self.assertIn("Nothing here is a real employer, person or application", banner)
        self.assertIn("No live API call", banner)
        self.assertIn(data.sample_deepdive().strip(), self.text())

    def test_other_tabs_render_without_the_database(self):
        from streamlit.testing.v1 import AppTest

        missing = Path(self.tmp.name) / "nope.db"
        with mock.patch.dict(os.environ, {"JOBSCOUT_DB": str(missing)}):
            at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
        self.assertFalse(at.exception)
        self.assertTrue(at.error)
        self.assertEqual(len(at.table), 2)
        self.assertTrue(at.warning)
        self.assertFalse(missing.exists())

    def test_clicking_selected_row_again_deselects(self):
        self.click_row(1)
        key = self.at.dataframe[0].key
        self.at.session_state[key] = {"selection": {"rows": [], "columns": [], "cells": []}}
        self.at.run()
        self.assertNoException()
        self.assertFalse(self.panel_open())


if __name__ == "__main__":
    unittest.main()
