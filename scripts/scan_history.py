"""Scan the working tree and full git history for personal data and secrets before publishing.

Blocked terms come from .private/leaks.txt (one per line; blank lines and # comments ignored).
A missing or empty list is an error, never a pass. These match anywhere, ignoring case, and
spaces in a term also match no separator or "-", "_", ".", so "Northwind Traders" also finds
"northwindtraders" and "NORTHWIND-TRADERS".

Optional: .private/leaks_exact.txt, same format, for terms that are also ordinary words.
These match whole words only, case-sensitively. A missing file is ignored.

Also checked everywhere:
- Anthropic API keys (by their key prefix)
- OAuth client secret assignments or JSON keys (not mere mentions such as a loader's name)
- email addresses, except example.com and its subdomains, LinkedIn's noreply senders and
  GitHub noreply addresses (@users.noreply.github.com)
- the employer's short name, case-insensitively

Prints file, line number and the matched term only, never the line itself.
Exit codes: 0 clean, 1 hits found, 2 cannot scan (missing list, git error).

Usage: python scripts/scan_history.py [--root DIR]
"""

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_CLEAN, EXIT_HITS, EXIT_ERROR = 0, 1, 2
# __pycache__ holds bytecode compiled from source scanned here; its binary format glues
# type bytes onto strings, which only produces false email hits. It is gitignored, and a
# committed .pyc would still be caught by the history scan.
SKIP_DIRS = {".git", ".private", ".venv", "__pycache__"}
LEAKS_PATH = Path(".private") / "leaks.txt"
EXACT_PATH = Path(".private") / "leaks_exact.txt"

# Joined at run time (join calls are not constant-folded into the .pyc), so neither this
# file nor its compiled cache matches its own patterns.
KEY_PREFIX = "".join(["sk-", "ant-"])
SECRET_KEY = "".join(["client", "_secret"])
EMPLOYER = "".join(["f", "nz"])

ALLOWED_EMAILS = {"jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com",
                  "messages-noreply@linkedin.com"}
GITHUB_NOREPLY = "@users.noreply.github.com"  # GitHub's private commit addresses
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
FIXED_PATTERNS = [
    (re.compile(re.escape(KEY_PREFIX)), KEY_PREFIX),
    (re.compile(re.escape(SECRET_KEY) + r"""["']?\s*[:=]""", re.I), SECRET_KEY),
    (re.compile(re.escape(EMPLOYER), re.I), EMPLOYER),
]


class ScanError(Exception):
    pass


@dataclass(frozen=True)
class Hit:
    where: str  # "path:line", or "history <sha> path:line"
    term: str

    def __str__(self) -> str:
        return f"{self.where}: {self.term}"


def load_terms(path: Path, required: bool = True) -> list[str]:
    """Terms from a list file. A missing or empty required list is an error; an optional
    one that is missing gives no terms."""
    if not path.is_file():
        if not required:
            return []
        raise ScanError(f"blocked-terms list not found: {path} (refusing to scan without it)")
    terms = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    if not terms and required:
        raise ScanError(f"blocked-terms list is empty: {path} (refusing to scan without it)")
    return terms


def email_allowed(addr: str) -> bool:
    addr = addr.lower()
    domain = addr.rsplit("@", 1)[1]
    return (addr in ALLOWED_EMAILS or domain == "example.com" or domain.endswith(".example.com")
            or addr.endswith(GITHUB_NOREPLY))


class Matcher:
    def __init__(self, terms: list[str], exact_terms: list[str] = ()):
        loose = [(re.compile(r"[\s._-]*".join(map(re.escape, t.split())), re.I), t) for t in terms]
        exact = [(re.compile(rf"(?<![A-Za-z0-9]){re.escape(t)}(?![A-Za-z0-9])"), t) for t in exact_terms]
        self.terms = loose + exact

    def find(self, text: str) -> list[str]:
        found = [label for rx, label in self.terms if rx.search(text)]
        found += [label for rx, label in FIXED_PATTERNS if rx.search(text)]
        found += [m for m in EMAIL_RE.findall(text) if not email_allowed(m)]
        return list(dict.fromkeys(found))


def _lines(data: bytes) -> list[str]:
    return data.decode("utf-8", errors="replace").splitlines()


def scan_tree(root: Path, matcher: Matcher) -> tuple[list[Hit], int]:
    hits, files = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            files += 1
            rel = path.relative_to(root).as_posix()
            for n, line in enumerate(_lines(path.read_bytes()), 1):
                hits += [Hit(f"{rel}:{n}", t) for t in matcher.find(line)]
    return hits, files


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    except FileNotFoundError:
        raise ScanError("git is not installed, but a .git directory exists; cannot scan history")


def has_commits(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    res = _git(root, "rev-list", "--all", "--max-count=1")
    if res.returncode != 0:
        raise ScanError(f"git rev-list failed: {res.stderr.decode(errors='replace').strip()}")
    return bool(res.stdout.strip())


COMMIT_MARK = "\x00commit "
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_history(root: Path, matcher: Matcher) -> tuple[list[Hit], int]:
    """Scan every commit's author, committer, message and added or removed lines."""
    res = _git(root, "log", "--all", "-p", "--text", "--no-color", "--no-ext-diff",
               "--format=%x00commit %H%n%an <%ae>%n%cn <%ce>%n%B")
    if res.returncode != 0:
        raise ScanError(f"git log failed: {res.stderr.decode(errors='replace').strip()}")
    hits, commits = [], 0
    sha, path, state, header_n, old_n, new_n = "", "", "header", 0, 0, 0
    for line in _lines(res.stdout):
        if line.startswith(COMMIT_MARK):
            sha, state, header_n, commits = line[len(COMMIT_MARK):][:10], "header", 0, commits + 1
            continue
        if line.startswith("diff --git "):
            state, path = "file_header", line.rsplit(" b/", 1)[-1]
            continue
        if state == "header":
            header_n += 1
            where = f"history {sha} (author/committer/message line {header_n})"
        elif state == "file_header":
            if line.startswith("+++ b/"):
                path = line[len("+++ b/"):]
            m = HUNK_RE.match(line)
            if m:
                old_n, new_n, state = int(m.group(1)), int(m.group(2)), "hunk"
            continue
        else:
            m = HUNK_RE.match(line)
            if m:
                old_n, new_n = int(m.group(1)), int(m.group(2))
                continue
            if line.startswith("+"):
                where, new_n = f"history {sha} {path}:{new_n}", new_n + 1
            elif line.startswith("-"):
                where, old_n = f"history {sha} {path}:{old_n} (removed)", old_n + 1
            else:
                if line.startswith(" "):
                    old_n, new_n = old_n + 1, new_n + 1
                continue  # context lines repeat content already scanned in another commit
            line = line[1:]
        hits += [Hit(where, t) for t in matcher.find(line)]
    return hits, commits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent,
                    help="repository root (default: this script's repository)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    try:
        matcher = Matcher(load_terms(root / LEAKS_PATH), load_terms(root / EXACT_PATH, required=False))
        hits, files = scan_tree(root, matcher)
        commits = 0
        if has_commits(root):
            history_hits, commits = scan_history(root, matcher)
            hits += history_hits
    except ScanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    for hit in hits:
        print(hit)
    scope = f"{files} files, {commits} commits" if commits else f"{files} files, no git history"
    if hits:
        print(f"FAILED: {len(hits)} hit(s) ({scope}, {len(matcher.terms)} blocked terms)")
        return EXIT_HITS
    print(f"OK: no hits ({scope}, {len(matcher.terms)} blocked terms)")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
