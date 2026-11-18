import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eval as evaluation  # noqa: E402


def row(job_id, title="T"):
    return {"job_id": job_id, "title": title, "company": "Co", "location": "Auckland",
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/"}


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "labels.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        with self.path.open(newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    def test_template_columns_and_blank_labels(self):
        self.assertEqual(evaluation.write_template([row("1"), row("2")], self.path), (2, 2))
        rows = self.read()
        self.assertEqual(list(rows[0].keys()), evaluation.COLUMNS)
        self.assertEqual([r["label"] for r in rows], ["", ""])

    def test_template_keeps_existing_labels(self):
        evaluation.write_template([row("1")], self.path)
        text = self.path.read_text(encoding="utf-8").replace("/1/,", "/1/,skip")
        self.path.write_text(text, encoding="utf-8")
        self.assertEqual(evaluation.write_template([row("1"), row("2")], self.path), (1, 2))
        self.assertEqual({r["job_id"]: r["label"] for r in self.read()}, {"1": "skip", "2": ""})

    def test_read_labels_ignores_blanks_and_extra_columns(self):
        self.path.write_text("job_id,label,notes\n1,Shortlist,x\n2,,\n3, skip ,\n", encoding="utf-8")
        self.assertEqual(evaluation.read_labels(self.path), {"1": "shortlist", "3": "skip"})

    def test_read_labels_rejects_unknown_label(self):
        self.path.write_text("job_id,label\n1,yes\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            evaluation.read_labels(self.path)


class SplitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "labels.csv"
        # 100 labelled rows: 60 skip, 25 maybe, 15 shortlist, plus 5 unlabelled
        labels = ["skip"] * 60 + ["maybe"] * 25 + ["shortlist"] * 15 + [""] * 5
        lines = ["job_id,title,company,location,url,label"]
        lines += [f"{1000 + i},T,Co,Auckland,u,{lab}" for i, lab in enumerate(labels)]
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_holdout_size_and_stratified(self):
        counts = evaluation.assign_split(self.path, size=40, seed=1)
        self.assertEqual(sum(counts[("holdout", v)] for v in evaluation.VERDICTS), 40)
        self.assertEqual((counts[("holdout", "skip")], counts[("holdout", "maybe")],
                          counts[("holdout", "shortlist")]), (24, 10, 6))
        splits = evaluation.read_splits(self.path)
        self.assertEqual(len(splits), 100)  # unlabelled rows get no split
        self.assertEqual(evaluation.read_labels(self.path)["1000"], "skip")  # labels untouched

    def test_same_seed_same_split(self):
        evaluation.assign_split(self.path, size=40, seed=7)
        first = evaluation.read_splits(self.path)
        self.tmp.cleanup()
        self.setUp()
        evaluation.assign_split(self.path, size=40, seed=7)
        self.assertEqual(evaluation.read_splits(self.path), first)

    def test_existing_holdout_never_reshuffled(self):
        evaluation.assign_split(self.path, size=40, seed=1)
        before = evaluation.read_splits(self.path)
        text = self.path.read_text(encoding="utf-8-sig").replace(",u,,", ",u,maybe,")  # label the 5 blanks
        self.path.write_text(text, encoding="utf-8")
        evaluation.assign_split(self.path, size=40, seed=999)
        after = evaluation.read_splits(self.path)
        self.assertEqual({j: s for j, s in after.items() if j in before}, before)
        self.assertEqual({s for j, s in after.items() if j not in before}, {"tune"})

    def test_template_keeps_split(self):
        evaluation.assign_split(self.path, size=40, seed=1)
        before = evaluation.read_splits(self.path)
        evaluation.write_template([row("9999")], self.path)
        self.assertEqual(evaluation.read_splits(self.path), before)

    def test_unknown_split_rejected(self):
        self.path.write_text("job_id,label,split\n1,skip,test\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            evaluation.read_splits(self.path)


class EvaluateTests(unittest.TestCase):
    def test_agreement_and_confusion(self):
        labels = {"1": "shortlist", "2": "maybe", "3": "skip", "4": "skip", "5": "maybe"}
        preds = {"1": "shortlist", "2": "skip", "3": "skip", "4": "maybe", "6": "shortlist"}
        r = evaluation.evaluate(labels, preds)
        self.assertEqual((r["compared"], r["agree"]), (4, 2))
        self.assertAlmostEqual(r["rate"], 0.5)
        self.assertEqual(r["confusion"][("maybe", "skip")], 1)
        self.assertEqual(r["confusion"][("skip", "maybe")], 1)
        self.assertEqual(r["unscored"], ["5"])
        self.assertEqual(sorted(d[0] for d in r["disagreements"]), ["2", "4"])

    def test_confusion_table_layout(self):
        r = evaluation.evaluate({"1": "shortlist", "2": "skip"}, {"1": "shortlist", "2": "maybe"})
        lines = evaluation.format_confusion(r["confusion"]).splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[1].split()[1:], ["1", "0", "0"])
        self.assertEqual(lines[3].split()[1:], ["0", "1", "0"])

    def test_top_k_precision(self):
        labels = {"a": "shortlist", "b": "skip", "c": "maybe", "d": "shortlist"}
        ranked = ["x", "a", "b", "y", "c", "d"]  # x, y unlabelled (e.g. held out)
        self.assertEqual(evaluation.top_k_precision(labels, ranked, 2), (0.5, 0.5))
        self.assertEqual(evaluation.top_k_precision(labels, ranked, 3), (1 / 3, 2 / 3))
        self.assertIsNone(evaluation.top_k_precision({}, ranked, 3))

    def test_shortlist_counts(self):
        labels = {"1": "shortlist", "2": "shortlist", "3": "skip", "4": "maybe", "5": "shortlist"}
        preds = {"1": "shortlist", "2": "maybe", "3": "shortlist", "4": "maybe"}
        self.assertEqual(evaluation.shortlist_counts(labels, preds), (1, 2, 2))

    def test_shortlist_precision_recall(self):
        labels = {"1": "shortlist", "2": "shortlist", "3": "skip", "4": "maybe", "5": "shortlist"}
        preds = {"1": "shortlist", "2": "maybe", "3": "shortlist", "4": "maybe"}  # 5 unscored
        self.assertEqual(evaluation.shortlist_precision_recall(labels, preds), (0.5, 0.5))
        self.assertEqual(evaluation.shortlist_precision_recall({"1": "skip"}, {"1": "skip"}), (None, None))

    def test_append_result_upgrades_old_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            path.write_text("timestamp,note,tune_rate\n2026-01-01,it0,0.5\n", encoding="utf-8")
            evaluation.append_result({"note": "it1", "effort": "medium", "shortlist_recall": "0.6"}, path)
            rows = evaluation.read_results(path)
            self.assertEqual(list(rows[0].keys()), evaluation.RESULT_COLUMNS)
            self.assertEqual((rows[0]["note"], rows[0]["tune_rate"], rows[0]["effort"]), ("it0", "0.5", ""))
            self.assertEqual((rows[1]["note"], rows[1]["effort"]), ("it1", "medium"))

    def test_append_result_writes_header_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "results.csv"
            evaluation.append_result({"note": "it0"}, path)
            evaluation.append_result({"note": "it1"}, path)
            with path.open(newline="", encoding="utf-8") as f:
                self.assertEqual([r["note"] for r in csv.DictReader(f)], ["it0", "it1"])

    def test_nothing_to_compare(self):
        self.assertIsNone(evaluation.evaluate({"1": "skip"}, {})["rate"])


class ExampleLabelsTests(unittest.TestCase):
    path = Path(__file__).resolve().parent.parent / "labels.example.csv"

    def test_example_labels_are_valid(self):
        with self.path.open(newline="", encoding="utf-8-sig") as f:
            self.assertEqual(next(csv.reader(f)), evaluation.COLUMNS)
        labels = evaluation.read_labels(self.path)
        self.assertEqual(len(labels), 10)
        self.assertEqual(set(labels.values()), set(evaluation.VERDICTS))
        self.assertEqual(set(evaluation.read_splits(self.path).values()), set(evaluation.SPLITS))


if __name__ == "__main__":
    unittest.main()
