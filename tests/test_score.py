"""score.py tests with a fake client; no API calls are made."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["JOBSCOUT_RUBRIC"] = str(ROOT / "rubric.example.md")  # before importing score
os.environ["JOBSCOUT_FACTS"] = str(ROOT / "facts.example.md")

import db  # noqa: E402
import score  # noqa: E402
from parse import Listing  # noqa: E402
from pydantic import ValidationError  # noqa: E402

GOOD = '{"score": 82, "verdict": "shortlist", "reasons": ["Senior BA title", "Bank", "Pay unknown"], "pay_known": false}'
MISMATCH = '{"score": 82, "verdict": "maybe", "reasons": ["a", "b"], "pay_known": false}'
OUT_OF_RANGE = '{"score": 140, "verdict": "shortlist", "reasons": ["a", "b"], "pay_known": false}'
ONE_REASON = '{"score": 20, "verdict": "skip", "reasons": ["a"], "pay_known": false}'
USAGE = SimpleNamespace(input_tokens=1, cache_read_input_tokens=0, cache_creation_input_tokens=0, output_tokens=1)


class FakeClient:
    """Returns the given JSON outputs in turn, validating them like messages.parse does."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = score.ScoreResult.model_validate_json(self.outputs.pop(0))
        return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=USAGE)


LISTING = {"job_id": "1", "title": "Senior Business Analyst", "company": "Example Bank", "location": "Auckland"}


class ScoreResultTests(unittest.TestCase):
    def test_valid(self):
        r = score.ScoreResult.model_validate_json(GOOD)
        self.assertEqual((r.score, r.verdict), (82, "shortlist"))

    def test_invalid_outputs_rejected(self):
        for bad in (MISMATCH, OUT_OF_RANGE, ONE_REASON, "not json"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                score.ScoreResult.model_validate_json(bad)

    def test_band_edges(self):
        for s, v in ((70, "shortlist"), (69, "maybe"), (40, "maybe"), (39, "skip")):
            score.ScoreResult(score=s, verdict=v, reasons=["a", "b"], pay_known=False)


class ScoreListingTests(unittest.TestCase):
    def test_request_shape(self):
        client = FakeClient(GOOD)
        score.score_listing(client, "SYSTEM", LISTING)
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual(call["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIs(call["output_format"], score.ScoreResult)
        user = call["messages"][0]["content"]
        self.assertIn("Example Bank", user)
        self.assertNotIn("url", user)

    def test_retries_once_then_succeeds(self):
        client = FakeClient(MISMATCH, GOOD)
        self.assertEqual(score.score_listing(client, "S", LISTING).score, 82)
        self.assertEqual(len(client.calls), 2)

    def test_gives_up_after_one_retry(self):
        client = FakeClient(MISMATCH, OUT_OF_RANGE, GOOD)
        with self.assertRaises(ValueError):
            score.score_listing(client, "S", LISTING)
        self.assertEqual(len(client.calls), 2)

    def test_score_all_marks_error_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            msg = {"id": "m", "internal_date": 1, "from": "x", "subject": "s"}
            db.save_email(conn, msg, "parsed", [
                Listing("1", "A", "Co", "Auckland", "u1"), Listing("2", "B", "Co", "Auckland", "u2")], [])
            client = FakeClient(MISMATCH, MISMATCH, GOOD)  # first listing fails twice, second succeeds
            with mock.patch.object(score, "make_client", return_value=client), \
                 mock.patch("builtins.print"), self.assertLogs("score", "ERROR"):
                scored, errors = score.score_all(conn)
            self.assertEqual((scored, errors), (1, 1))
            failed = [r for r in conn.execute("SELECT * FROM listings") if r["score_error"]]
            self.assertEqual(len(failed), 1)
            conn.close()

    def test_rescore_scores_already_scored_listings(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            msg = {"id": "m", "internal_date": 1, "from": "x", "subject": "s"}
            db.save_email(conn, msg, "parsed", [Listing("1", "A", "Co", "Auckland", "u1")], [])
            with mock.patch.object(score, "make_client", return_value=FakeClient(GOOD, GOOD, GOOD)), \
                 mock.patch("builtins.print"):
                self.assertEqual(score.score_all(conn), (1, 0))
                self.assertEqual(score.score_all(conn), (0, 0))  # already scored
                self.assertEqual(score.score_all(conn, rescore=True), (1, 0))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0], 2)
            conn.close()

    def test_prompt_sha_tracks_rubric(self):
        self.assertIn(score.RUBRIC_FILE.read_text(encoding="utf-8").strip(), score.system_prompt())
        self.assertEqual(score.prompt_sha(), db.sha256_text(score.system_prompt()))


class PathFromEnvTests(unittest.TestCase):
    def test_tests_use_the_example_rubric(self):
        self.assertEqual(score.RUBRIC_FILE, ROOT / "rubric.example.md")

    def test_default_when_unset_or_blank(self):
        for env in ({}, {"X_PATH": ""}):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(score.path_from_env("X_PATH", "rubric.md"), score.ROOT / "rubric.md")

    def test_relative_is_under_root_and_absolute_is_kept(self):
        with mock.patch.dict(os.environ, {"X_PATH": "sub/r.md"}):
            self.assertEqual(score.path_from_env("X_PATH", "rubric.md"), score.ROOT / "sub" / "r.md")
        absolute = str(Path(tempfile.gettempdir()) / "r.md")
        with mock.patch.dict(os.environ, {"X_PATH": absolute}):
            self.assertEqual(score.path_from_env("X_PATH", "rubric.md"), Path(absolute))


if __name__ == "__main__":
    unittest.main()
