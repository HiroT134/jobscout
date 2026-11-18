"""scan_history.py tests: a temporary repository with a fake leaks file and a fake blocked term."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scan_history as sh  # noqa: E402

FAKE_TERM = "Quuxbridge Holdings"
# Joined at run time so neither this file nor its .pyc matches the scanner's own patterns.
KEY = "".join(["sk-", "ant-", "abc123"])
SECRET = "".join(["client", "_secret"])
EMPLOYER = "".join(["F", "nZ"])


def addr(local, domain):
    return "@".join([local, domain])


REAL_EMAIL = addr("real.person", "gmail.com")


def git_env():
    """Isolate test commits from the user's git config (signing, hooks, templates)."""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.com",
               GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.com")
    return env


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.write(".private/leaks.txt", f"# fake list\n\n{FAKE_TERM}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def run_scan(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sh.main(["--root", str(self.root)])
        return code, out.getvalue(), err.getvalue()


class LeaksFileTests(ScanTestCase):
    def test_missing_list_is_an_error_not_a_pass(self):
        (self.root / ".private" / "leaks.txt").unlink()
        self.write("clean.py", "x = 1\n")
        code, out, err = self.run_scan()
        self.assertEqual(code, sh.EXIT_ERROR)
        self.assertIn("not found", err)
        self.assertNotIn("OK", out)

    def test_empty_list_is_an_error_not_a_pass(self):
        for content in ("", "\n  \n", "# only a comment\n"):
            with self.subTest(content=content):
                self.write(".private/leaks.txt", content)
                code, out, err = self.run_scan()
                self.assertEqual(code, sh.EXIT_ERROR)
                self.assertIn("empty", err)


class TreeTests(ScanTestCase):
    def test_clean_tree_passes(self):
        self.write("app.py", 'SENDER = "jobalerts-noreply@linkedin.com"\nprint("hello")\n')
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN)
        self.assertIn("OK: no hits", out)

    def test_blocked_term_reports_file_line_and_term_but_not_the_line(self):
        self.write("notes/a.md", f"line one\nWorked at {FAKE_TERM} on the private-detail project\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn(f"notes/a.md:2: {FAKE_TERM}", out)
        self.assertNotIn("private-detail", out)

    def test_main_list_ignores_case_spacing_and_word_boundaries(self):
        variants = ["quuxbridgeholdings", "QUUXBRIDGE HOLDINGS", "xQuuxbridge-HoldingsX",
                    "quuxbridge_holdings", "quuxbridge.holdings", "Quuxbridge  Holdings"]
        self.write("a.txt", "\n".join(variants) + "\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        for n in range(1, len(variants) + 1):
            self.assertIn(f"a.txt:{n}: {FAKE_TERM}", out)

    def test_main_list_does_not_match_partial_term(self):
        self.write("a.txt", "Quuxbridge Partners\n")
        self.assertEqual(self.run_scan()[0], sh.EXIT_CLEAN)

    def test_exact_list_is_whole_word_and_case_sensitive(self):
        self.write(".private/leaks_exact.txt", "# ordinary words\nZorbly\n")
        self.write("a.txt", "zorbly\nZorblyX\nxZorbly\nworked at Zorbly.\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertEqual(out.count("Zorbly"), 1)
        self.assertIn("a.txt:4: Zorbly", out)

    def test_missing_or_empty_exact_list_is_ignored(self):
        self.write("a.txt", "x = 1\n")
        self.assertEqual(self.run_scan()[0], sh.EXIT_CLEAN)
        self.write(".private/leaks_exact.txt", "")
        self.assertEqual(self.run_scan()[0], sh.EXIT_CLEAN)

    def test_exact_list_does_not_replace_required_main_list(self):
        (self.root / ".private" / "leaks.txt").unlink()
        self.write(".private/leaks_exact.txt", "Zorbly\n")
        code, _, err = self.run_scan()
        self.assertEqual(code, sh.EXIT_ERROR)
        self.assertIn("not found", err)

    def test_fixed_patterns(self):
        cases = {
            "key.txt": (f"token = '{KEY}'\n", sh.KEY_PREFIX),
            "creds.json": (f'{{"{SECRET}": "abc"}}\n', SECRET),
            "env.txt": (f"{SECRET.upper()}=abc\n", SECRET),
            "employer.txt": (f"worked at {EMPLOYER} Japan\n", EMPLOYER.lower()),
            "mail.txt": (f"contact {REAL_EMAIL}\n", REAL_EMAIL),
        }
        for name, (content, term) in cases.items():
            with self.subTest(name=name):
                self.write(name, content)
                code, out, _ = self.run_scan()
                self.assertEqual(code, sh.EXIT_HITS)
                self.assertIn(f"{name}:1: {term}", out)
                (self.root / name).unlink()

    def test_allowed_emails_and_secrets_file_loader_do_not_hit(self):
        self.write("ok.py", "a = 'sam@example.com'\nb = 'x@careers.example.com'\n"
                            "c = 'JOBS-NOREPLY@linkedin.com'\n"
                            f"flow = InstalledAppFlow.from_{SECRET}s_file(path)\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN, out)

    def test_github_noreply_addresses_are_allowed(self):
        self.write("a.txt", addr("12345+someone", "users.noreply.github.com") + "\n"
                            + addr("Someone", "Users.NoReply.GitHub.com") + "\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN, out)

    def test_github_noreply_lookalikes_are_not_allowed(self):
        lookalikes = [addr("x", "users.noreply.github.com.evil.io"), addr("y", "noreply.github.com"),
                      addr("z", "evilusers.noreply.github.com")]
        self.write("a.txt", "\n".join(lookalikes) + "\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        for a in lookalikes:
            self.assertIn(a, out)

    def test_lookalike_domains_are_not_allowed(self):
        lookalikes = [addr("x", "example.com.evil.io"), addr("y", "notexample.com")]
        self.write("a.txt", "\n".join(lookalikes) + "\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        for a in lookalikes:
            self.assertIn(a, out)

    def test_excluded_directories_are_not_scanned(self):
        for d in (".git", ".private", ".venv", "sub/.venv", "pkg/__pycache__"):
            self.write(f"{d}/leak.txt", f"{FAKE_TERM}\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN, out)


@unittest.skipUnless(shutil.which("git"), "git not installed")
class HistoryTests(ScanTestCase):
    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, env=git_env())

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self._env = dict(os.environ)
        os.environ.update(git_env())

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        super().tearDown()

    def test_repo_without_commits_scans_tree_only(self):
        self.write("a.py", "x = 1\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN)
        self.assertIn("no git history", out)

    def test_term_removed_from_tree_is_still_found_in_history(self):
        self.write("a.py", "x = 1\ny = 2\n")
        self.git("add", "a.py")
        self.git("commit", "-q", "-m", "first")
        self.write("a.py", f"x = 1\ny = '{FAKE_TERM} secret-detail'\n")
        self.git("commit", "-q", "-am", "second")
        self.write("a.py", "x = 1\ny = 2\n")
        self.git("commit", "-q", "-am", "third")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertRegex(out, rf"history [0-9a-f]+ a\.py:2: {FAKE_TERM}")
        self.assertRegex(out, rf"history [0-9a-f]+ a\.py:2 \(removed\): {FAKE_TERM}")
        self.assertNotIn("secret-detail", out)
        self.assertIn("3 commits", out)

    def test_commit_message_and_author_are_scanned(self):
        self.write("a.py", "x = 1\n")
        self.git("add", "a.py")
        subprocess.run(["git", "-C", str(self.root), "commit", "-q", "-m", f"fix for {FAKE_TERM}"],
                       check=True, capture_output=True,
                       env=dict(git_env(), GIT_AUTHOR_EMAIL=REAL_EMAIL))
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn(FAKE_TERM, out)
        self.assertIn(REAL_EMAIL, out)
        self.assertIn("author/committer/message", out)


if __name__ == "__main__":
    unittest.main()
