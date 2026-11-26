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

Binary files, in the tree and in every blob reachable from git history:
- PNG files are not read as text (their compressed pixels match short terms by chance).
  Their text and other ancillary metadata chunks and their file name are scanned instead,
  and each PNG's SHA-256 must be listed in docs/images/REVIEWED.txt, which records that a
  person looked at its pixels. An unlisted PNG fails, and so does every PNG if that file
  is missing. A malformed PNG fails.
- Any other binary file (not UTF-8, contains a NUL byte, or has a binary extension such
  as .jpg, .gif, .pdf or .zip) fails as unsupported, to be reviewed manually.
- Binary files that git ignores in the working tree are skipped and counted, since they
  cannot be published without being force-added, and a force-added file is not ignored.

Prints file, line number and the matched term only, never the line itself.
Exit codes: 0 clean, 1 hits found, 2 cannot scan (missing list, git error, bad REVIEWED.txt).

Usage: python scripts/scan_history.py [--root DIR]
"""

import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

EXIT_CLEAN, EXIT_HITS, EXIT_ERROR = 0, 1, 2
# __pycache__ holds bytecode compiled from source scanned here; its binary format glues
# type bytes onto strings, which only produces false email hits. It is gitignored, and a
# committed .pyc would still be caught by the history scan.
SKIP_DIRS = {".git", ".private", ".venv", "__pycache__"}
LEAKS_PATH = Path(".private") / "leaks.txt"
EXACT_PATH = Path(".private") / "leaks_exact.txt"
REVIEWED_PATH = Path("docs") / "images" / "REVIEWED.txt"

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_CRITICAL = {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
MAX_INFLATE = 16 * 1024 * 1024  # cap on a decompressed metadata chunk
# always treated as binary, even if the bytes happen to decode as text (an all-ASCII PDF)
BINARY_EXTENSIONS = {
    ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".ico", ".heic", ".avif", ".psd",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar", ".jar", ".whl",
    ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt", ".odt", ".ods", ".odp",
    ".db", ".sqlite", ".sqlite3", ".pyc", ".exe", ".dll", ".so", ".dylib", ".bin",
    ".mp3", ".mp4", ".mov", ".wav", ".avi", ".mkv", ".woff", ".woff2", ".ttf", ".otf",
}
NEEDS_REVIEW = "PNG not listed in docs/images/REVIEWED.txt: needs human review of its pixels"
REVIEWED_MISSING = ("PNG present but docs/images/REVIEWED.txt is missing: "
                    "needs human review of its pixels")
UNSUPPORTED = "unsupported binary, review manually"

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


# ---------- binary files ----------

def kind(name: str, data: bytes) -> str:
    """'png', 'binary' or 'text'. Text means valid UTF-8 with no NUL byte and no binary
    extension. A file named .png that is not a PNG counts as binary."""
    if data.startswith(PNG_SIGNATURE):
        return "png"
    ext = os.path.splitext(name)[1].lower()
    if ext == ".png" or ext in BINARY_EXTENSIONS or b"\x00" in data:
        return "binary"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"
    return "text"


def load_reviewed(path: Path) -> set[str] | None:
    """SHA-256 hashes from REVIEWED.txt, or None if the file does not exist. Each line is
    a hash, then optionally the file name; blank lines and # comments are ignored. Any
    other line is an error, so a typo cannot silently drop a review."""
    if not path.is_file():
        return None
    hashes = set()
    for n, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest = line.split()[0].lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ScanError(f"{REVIEWED_PATH.as_posix()}:{n}: expected a SHA-256 hash")
        hashes.add(digest)
    return hashes


class PngError(Exception):
    pass


def _inflate(data: bytes) -> bytes:
    d = zlib.decompressobj()
    try:
        out = d.decompress(data, MAX_INFLATE)
    except zlib.error as e:
        raise PngError(f"bad compressed data ({e})")
    if d.unconsumed_tail:
        raise PngError("compressed metadata too large")
    return out


def _chunk_text(ctype: bytes, body: bytes) -> str:
    """The searchable text of one ancillary chunk. Text chunks are decoded by their PNG
    encoding (tEXt and zTXt Latin-1, iTXt UTF-8); any other chunk is searched as raw bytes."""
    if ctype == b"tEXt":
        return body.replace(b"\x00", b"\n").decode("latin-1")
    if ctype == b"zTXt":
        key, _, rest = body.partition(b"\x00")
        return key.decode("latin-1") + "\n" + _inflate(rest[1:]).decode("latin-1")
    if ctype == b"iTXt":
        key, _, rest = body.partition(b"\x00")
        if len(rest) < 2:
            raise PngError("truncated iTXt chunk")
        compressed, rest = rest[0], rest[2:]
        lang, _, rest = rest.partition(b"\x00")
        translated, _, text = rest.partition(b"\x00")
        text = _inflate(text) if compressed else text
        return "\n".join([key.decode("latin-1"), lang.decode("latin-1", errors="replace"),
                          translated.decode("utf-8", errors="replace"), text.decode("utf-8", errors="replace")])
    if ctype in (b"iCCP",):
        name, _, rest = body.partition(b"\x00")
        return name.decode("latin-1") + "\n" + _inflate(rest[1:]).decode("utf-8", errors="replace")
    return body.decode("utf-8", errors="replace")


def png_metadata(data: bytes) -> list[tuple[str, str]]:
    """(chunk type, text) for every ancillary chunk. Raises PngError if the file is not a
    well-formed PNG: bad CRC, truncated, unknown critical chunk or bytes after IEND."""
    if not data.startswith(PNG_SIGNATURE):
        raise PngError("missing PNG signature")
    out, i, end = [], len(PNG_SIGNATURE), False
    while i < len(data):
        if end:
            raise PngError("data after IEND")
        if i + 12 > len(data):
            raise PngError("truncated chunk")
        length, ctype = struct.unpack(">I4s", data[i:i + 8])
        body = data[i + 8:i + 8 + length]
        if len(body) != length or i + 12 + length > len(data):
            raise PngError("truncated chunk")
        crc = struct.unpack(">I", data[i + 8 + length:i + 12 + length])[0]
        if zlib.crc32(ctype + body) != crc:
            raise PngError(f"bad CRC in {ctype.decode('latin-1')} chunk")
        if ctype[0] & 0x20:  # lower-case first letter: ancillary
            out.append((ctype.decode("latin-1"), _chunk_text(ctype, body)))
        elif ctype not in PNG_CRITICAL:
            raise PngError(f"unknown critical chunk {ctype.decode('latin-1')}")
        end = ctype == b"IEND"
        i += 12 + length
    if not end:
        raise PngError("no IEND chunk")
    return out


def check_binary(where: str, name: str, data: bytes, kind_: str, matcher: Matcher,
                 reviewed: set[str] | None) -> list[Hit]:
    """Hits for one non-text file: its name, and for a PNG its metadata and review status."""
    hits = [Hit(f"{where} (file name)", t) for t in matcher.find(name)]
    if kind_ != "png":
        return hits + [Hit(where, UNSUPPORTED)]
    try:
        for ctype, text in png_metadata(data):
            for n, line in enumerate(text.splitlines(), 1):
                hits += [Hit(f"{where} [{ctype} chunk line {n}]", t) for t in matcher.find(line)]
    except PngError as e:
        hits.append(Hit(where, f"malformed PNG ({e}), review manually"))
    if reviewed is None:
        hits.append(Hit(where, REVIEWED_MISSING))
    elif hashlib.sha256(data).hexdigest() not in reviewed:
        hits.append(Hit(where, NEEDS_REVIEW))
    return hits


# ---------- working tree ----------

def ignored_files(root: Path) -> set[str]:
    """Untracked files git ignores, as repository-relative paths. Empty without a .git."""
    if not (root / ".git").exists():
        return set()
    res = _git(root, "ls-files", "-z", "--others", "--ignored", "--exclude-standard")
    if res.returncode != 0:
        raise ScanError(f"git ls-files failed: {res.stderr.decode(errors='replace').strip()}")
    return {p.decode("utf-8", errors="replace") for p in res.stdout.split(b"\x00") if p}


def scan_tree(root: Path, matcher: Matcher, reviewed: set[str] | None = None) -> tuple[list[Hit], int, int]:
    """Hits, files scanned and ignored binary files skipped."""
    hits, files, skipped = [], 0, 0
    ignored = ignored_files(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            data = path.read_bytes()
            k = kind(name, data)
            if k != "text":
                if rel in ignored:
                    skipped += 1
                    continue
                files += 1
                hits += check_binary(rel, rel, data, k, matcher, reviewed)
                continue
            files += 1
            for n, line in enumerate(_lines(data), 1):
                hits += [Hit(f"{rel}:{n}", t) for t in matcher.find(line)]
    return hits, files, skipped


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


INDEX_RE = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")


def scan_history_blobs(root: Path, matcher: Matcher, reviewed: set[str] | None) -> tuple[list[Hit], set[str]]:
    """Check every blob reachable from any ref with the binary rules. Returns the hits and
    the git ids of the non-text blobs, whose lines the diff scan then skips. Text blobs
    are left to the diff scan."""
    res = _git(root, "rev-list", "--all", "--objects")
    if res.returncode != 0:
        raise ScanError(f"git rev-list failed: {res.stderr.decode(errors='replace').strip()}")
    paths: dict[str, str] = {}
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        oid, _, path = line.partition(" ")
        if path:  # commits have no path; trees and blobs do
            paths.setdefault(oid, path)
    try:
        res = subprocess.run(["git", "-C", str(root), "cat-file", "--batch"],
                             input="".join(f"{oid}\n" for oid in paths).encode(), capture_output=True)
    except FileNotFoundError:
        raise ScanError("git is not installed, but a .git directory exists; cannot scan history")
    if res.returncode != 0:
        raise ScanError(f"git cat-file failed: {res.stderr.decode(errors='replace').strip()}")
    hits, binary, out, i = [], set(), res.stdout, 0
    while i < len(out):
        nl = out.index(b"\n", i)
        oid, otype, size = out[i:nl].decode().split()
        size, i = int(size), nl + 1
        data, i = out[i:i + size], i + size + 1  # content, then a newline
        if otype != "blob":
            continue
        path = paths[oid]
        k = kind(os.path.basename(path), data)
        if k != "text":
            binary.add(oid)
            hits += check_binary(f"history {path} (blob {oid[:10]})", path, data, k, matcher, reviewed)
    return hits, binary


def scan_history(root: Path, matcher: Matcher, reviewed: set[str] | None = None) -> tuple[list[Hit], int]:
    """Scan every commit's author, committer, message and added or removed lines, and every
    binary blob. --text keeps text files visible even if .gitattributes marks them binary;
    the lines of real binary blobs are skipped here because the blob scan covers them."""
    hits, binary = scan_history_blobs(root, matcher, reviewed)
    res = _git(root, "log", "--all", "-p", "--text", "--full-index", "--no-color", "--no-ext-diff",
               "--no-textconv", "--format=%x00commit %H%n%an <%ae>%n%cn <%ce>%n%B")
    if res.returncode != 0:
        raise ScanError(f"git log failed: {res.stderr.decode(errors='replace').strip()}")
    commits = 0
    sha, path, state, header_n, old_n, new_n = "", "", "header", 0, 0, 0
    old_binary = new_binary = False
    # split on "\n" only, as git counts lines: str.splitlines would also split on bytes such
    # as \x1c or \x85 inside a diff line, making fragments that lose their +/- prefix
    for line in res.stdout.decode("utf-8", errors="replace").split("\n"):
        if line.startswith(COMMIT_MARK):
            sha, state, header_n, commits = line[len(COMMIT_MARK):][:10], "header", 0, commits + 1
            continue
        if line.startswith("diff --git "):
            state, path = "file_header", line.rsplit(" b/", 1)[-1]
            old_binary = new_binary = False
            continue
        if state == "header":
            header_n += 1
            where = f"history {sha} (author/committer/message line {header_n})"
        elif state == "file_header":
            if line.startswith("+++ b/"):
                path = line[len("+++ b/"):]
            m = INDEX_RE.match(line)
            if m:
                old_binary, new_binary = m.group(1) in binary, m.group(2) in binary
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
                if new_binary:
                    continue
            elif line.startswith("-"):
                where, old_n = f"history {sha} {path}:{old_n} (removed)", old_n + 1
                if old_binary:
                    continue
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
        reviewed = load_reviewed(root / REVIEWED_PATH)
        hits, files, skipped = scan_tree(root, matcher, reviewed)
        commits = 0
        if has_commits(root):
            history_hits, commits = scan_history(root, matcher, reviewed)
            hits += history_hits
    except ScanError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_ERROR
    for hit in hits:
        print(hit)
    scope = f"{files} files, {commits} commits" if commits else f"{files} files, no git history"
    if skipped:
        scope += f", {skipped} git-ignored binary file(s) skipped"
    if hits:
        print(f"FAILED: {len(hits)} hit(s) ({scope}, {len(matcher.terms)} blocked terms)")
        return EXIT_HITS
    print(f"OK: no hits ({scope}, {len(matcher.terms)} blocked terms)")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
