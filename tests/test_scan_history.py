"""scan_history.py tests: a temporary repository with a fake leaks file and a fake blocked term."""

import contextlib
import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
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
# A two-letter exact-list term, the kind that matches compressed image bytes by chance.
# Joined at run time so this file does not contain it as a whole word.
SHORT_TERM = "".join(["E", "Y"])


def chunk(ctype: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body))


def make_png(pixels: bytes = b"\x00\x00\x00\x00", extra: list[bytes] = ()) -> bytes:
    """A 1-pixel-high greyscale PNG whose row is `pixels` (filter byte first). The image
    data is zlib level 0, so the pixel bytes appear verbatim in the file."""
    ihdr = struct.pack(">IIBBBBB", len(pixels) - 1, 1, 8, 0, 0, 0, 0)
    return (sh.PNG_SIGNATURE + chunk(b"IHDR", ihdr) + b"".join(extra)
            + chunk(b"IDAT", zlib.compress(pixels, 0)) + chunk(b"IEND", b""))


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


class BinaryFileTests(ScanTestCase):
    def write_bytes(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def review(self, *pngs: bytes):
        lines = ["# Reviewed by eye: test images"]
        lines += [f"{hashlib.sha256(p).hexdigest()}  img{i}.png" for i, p in enumerate(pngs)]
        self.write("docs/images/REVIEWED.txt", "\n".join(lines) + "\n")

    def test_blocked_term_in_text_chunk_fails(self):  # (a)
        png = make_png(extra=[chunk(b"tEXt", b"Author\x00" + FAKE_TERM.encode())])
        self.write_bytes("docs/images/a.png", png)
        self.review(png)  # reviewed pixels do not excuse a leak in the metadata
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn(f"docs/images/a.png [tEXt chunk line 2]: {FAKE_TERM}", out)

    def test_blocked_term_in_compressed_and_international_text_chunks_fails(self):
        cases = {
            "z.png": chunk(b"zTXt", b"Comment\x00\x00" + zlib.compress(FAKE_TERM.encode())),
            "i.png": chunk(b"iTXt", b"Title\x00\x01\x00en\x00\x00" + zlib.compress(FAKE_TERM.encode())),
            "x.png": chunk(b"eXIf", b"MM\x00*" + FAKE_TERM.encode()),
        }
        pngs = {name: make_png(extra=[c]) for name, c in cases.items()}
        for name, png in pngs.items():
            self.write_bytes(f"docs/images/{name}", png)
        self.review(*pngs.values())
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        for name, ctype in (("z.png", "zTXt"), ("i.png", "iTXt"), ("x.png", "eXIf")):
            self.assertRegex(out, rf"docs/images/{name} \[{ctype} chunk line \d+\]: {FAKE_TERM}")

    def test_blocked_term_in_png_file_name_fails(self):
        png = make_png()
        self.write_bytes("docs/images/quuxbridge-holdings.png", png)
        self.review(png)
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn(f"docs/images/quuxbridge-holdings.png (file name): {FAKE_TERM}", out)

    def test_unreviewed_png_fails(self):  # (b)
        self.write_bytes("docs/images/a.png", make_png())
        self.review(make_png(b"\x00\x01\x02\x03"))  # a different image
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn("docs/images/a.png: PNG not listed in docs/images/REVIEWED.txt: "
                      "needs human review of its pixels", out)

    def test_reviewed_png_with_term_in_compressed_bytes_passes(self):  # (c)
        self.write(".private/leaks_exact.txt", f"{SHORT_TERM}\n")
        png = make_png(b"\x00 " + SHORT_TERM.encode() + b" ")
        self.assertIn(f" {SHORT_TERM} ".encode(), png)  # the raw-text scan used to hit this
        self.write_bytes("docs/images/a.png", png)
        self.review(png)
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN, out)
        # REVIEWED.txt itself is text and still scanned; a hit there would show up
        self.write("notes.txt", f"see {SHORT_TERM}\n")
        self.assertEqual(self.run_scan()[0], sh.EXIT_HITS)

    def test_missing_reviewed_list_with_png_fails(self):  # (d)
        self.write_bytes("docs/images/a.png", make_png())
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn("docs/images/a.png: PNG present but docs/images/REVIEWED.txt is missing: "
                      "needs human review of its pixels", out)

    def test_missing_reviewed_list_without_png_passes(self):
        self.write("a.txt", "x\n")
        self.assertEqual(self.run_scan()[0], sh.EXIT_CLEAN)

    def test_malformed_reviewed_list_is_an_error(self):
        self.write("docs/images/REVIEWED.txt", "# header\nnot-a-hash  a.png\n")
        code, _, err = self.run_scan()
        self.assertEqual(code, sh.EXIT_ERROR)
        self.assertIn("expected a SHA-256 hash", err)

    def test_malformed_png_fails_even_if_reviewed(self):
        good = make_png()
        cases = {"crc.png": good[:-1] + bytes([good[-1] ^ 1]),
                 "trailing.png": good + FAKE_TERM.encode(),
                 "truncated.png": good[:-6]}
        for name, data in cases.items():
            self.write_bytes(f"docs/images/{name}", data)
        self.review(*cases.values())
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        for name in cases:
            self.assertRegex(out, rf"docs/images/{name}: malformed PNG \(.+\), review manually")
        self.assertNotIn(FAKE_TERM, out)  # never prints the content

    def test_unsupported_binary_types_fail(self):  # (e)
        cases = {"photo.jpg": b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
                 "anim.gif": b"GIF89a\x01\x00\x01\x00\x00\x00\x00;",
                 "doc.pdf": b"%PDF-1.4\n1 0 obj << >> endobj\n%%EOF\n",  # all ASCII, still binary
                 "bundle.zip": b"PK\x03\x04\x14\x00\x00\x00",
                 "blob.dat": b"\x00\x01\x02text",
                 "latin1.txt": "caf\xe9\n".encode("latin-1"),
                 "fake.png": b"not really a png\n"}
        for name, data in cases.items():
            self.write_bytes(name, data)
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        for name in cases:
            self.assertIn(f"{name}: unsupported binary, review manually", out)


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

    def commit_png(self, rel, png):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        self.git("add", rel)
        self.git("commit", "-q", "-m", "add image")

    def test_unreviewed_png_in_history_fails_after_deletion(self):
        self.write("docs/images/REVIEWED.txt", "# none reviewed\n")
        self.commit_png("docs/images/a.png", make_png())
        self.git("rm", "-q", "docs/images/a.png")
        self.git("commit", "-q", "-m", "remove image")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertRegex(out, r"history docs/images/a\.png \(blob [0-9a-f]+\): PNG not listed in "
                              r"docs/images/REVIEWED\.txt: needs human review of its pixels")

    def test_reviewed_png_in_history_has_no_false_hits(self):
        self.write(".private/leaks_exact.txt", f"{SHORT_TERM}\n")
        png = make_png(b"\x00\n+ " + SHORT_TERM.encode() + b" \n")  # a diff line, if read as text
        self.write("docs/images/REVIEWED.txt", f"{hashlib.sha256(png).hexdigest()}  a.png\n")
        self.git("add", "docs/images/REVIEWED.txt")
        self.commit_png("docs/images/a.png", png)
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN, out)

    def test_text_chunk_in_history_png_fails(self):
        png = make_png(extra=[chunk(b"tEXt", b"Author\x00" + FAKE_TERM.encode())])
        self.write("docs/images/REVIEWED.txt", f"{hashlib.sha256(png).hexdigest()}  a.png\n")
        self.commit_png("docs/images/a.png", png)
        (self.root / "docs/images/a.png").unlink()
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertRegex(out, rf"history docs/images/a\.png \(blob [0-9a-f]+\) \[tEXt chunk line 2\]: {FAKE_TERM}")

    def test_unsupported_binary_in_history_fails(self):
        self.commit_png("files/report.pdf", b"%PDF-1.4\n%%EOF\n")
        (self.root / "files/report.pdf").unlink()
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertRegex(out, r"history files/report\.pdf \(blob [0-9a-f]+\): unsupported binary, review manually")

    def test_text_file_marked_binary_by_gitattributes_is_still_scanned(self):
        self.write(".gitattributes", "*.md -diff\n")
        self.write("notes.md", f"{FAKE_TERM}\n")
        self.git("add", ".gitattributes", "notes.md")
        self.git("commit", "-q", "-m", "notes")
        self.write("notes.md", "clean\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertRegex(out, rf"history [0-9a-f]+ notes\.md:1: {FAKE_TERM}")

    def test_ignored_binary_is_skipped_but_force_added_is_not(self):
        self.write(".gitignore", "data/\n")
        (self.root / "data").mkdir()
        (self.root / "data/demo.db").write_bytes(b"SQLite format 3\x00")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_CLEAN, out)
        self.assertIn("1 git-ignored binary file(s) skipped", out)
        self.git("add", "-f", "data/demo.db")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn("data/demo.db: unsupported binary, review manually", out)

    def test_ignored_text_file_is_still_scanned(self):
        self.write(".gitignore", "notes/\n")
        self.write("notes/a.txt", f"{FAKE_TERM}\n")
        code, out, _ = self.run_scan()
        self.assertEqual(code, sh.EXIT_HITS)
        self.assertIn(f"notes/a.txt:1: {FAKE_TERM}", out)


if __name__ == "__main__":
    unittest.main()
