import json
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "make_demo_db.py"
sys.path.insert(0, str(ROOT / "scripts"))

import make_demo_db  # noqa: E402


def dump(path: Path) -> dict:
    conn = sqlite3.connect(path)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    out = {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in tables}
    conn.close()
    return out


class MakeDemoDbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_script_runs_and_produces_expected_counts(self):
        out = self.dir / "demo.db"
        res = subprocess.run([sys.executable, str(SCRIPT), "--out", str(out)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        conn = sqlite3.connect(out)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0], 60)
        counts = dict(conn.execute("SELECT verdict, COUNT(*) FROM scores GROUP BY verdict"))
        self.assertEqual(counts, {"shortlist": 9, "maybe": 15, "skip": 36})
        for score, verdict, reasons, url in conn.execute(
                "SELECT s.score, s.verdict, s.reasons_json, l.url FROM scores s JOIN listings l USING (job_id)"):
            expected = "shortlist" if score >= 70 else "maybe" if score >= 40 else "skip"
            self.assertEqual(verdict, expected)
            self.assertIn(len(json.loads(reasons)), (2, 3))
            self.assertTrue(url.startswith("https://jobs.example.com/"))
        span = conn.execute("SELECT MAX(first_seen_at) - MIN(first_seen_at) FROM listings").fetchone()[0]
        self.assertLess(span, 30 * 86400 * 1000)
        conn.close()

    def test_reproducible_and_offline(self):
        a, b = self.dir / "a.db", self.dir / "b.db"
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")):
            make_demo_db.build(a)
            make_demo_db.build(b)
        self.assertEqual(dump(a), dump(b))

    def test_refuses_real_database(self):
        with mock.patch.object(make_demo_db, "build") as build:
            self.assertEqual(make_demo_db.main(["--out", str(make_demo_db.db.DB_FILE)]), 2)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
