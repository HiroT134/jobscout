# Privacy scan

`scripts/scan_history.py` checks the working tree and the full git history for personal
data and secrets before anything is published. It prints only the file, line number and
matched term, never the line itself, so its output can be shared without spreading the
rest of a leaked line.

```
python scripts/scan_history.py [--root DIR]
```

| Exit code | Meaning |
|---|---|
| 0 | No hits |
| 1 | One or more hits (each printed as `path:line: term`) |
| 2 | Cannot scan: the blocked-terms list is missing or empty, or git failed |

## The blocked-terms lists

The terms to look for are real names (employers, clients, people) and personal addresses,
so the lists themselves are sensitive. They live in `.private/`, which is git-ignored and
never published.

Both files have one term per line. Blank lines and lines starting with `#` are ignored.

### Main list: `.private/leaks.txt` (required)

If this file is missing or has no terms, the scan stops with exit code 2. A missing list
must never look like a clean result: a scan that checks nothing would otherwise report
"no hits" and pass.

Terms here match:

- **Case-insensitively:** `Quuxbridge Holdings` finds `QUUXBRIDGE HOLDINGS`.
- **As a substring:** it is found inside longer text, such as a domain
  (`quuxbridgeholdings.example`) or an identifier (`QuuxbridgeHoldingsClient`).
- **Ignoring spaces and separators:** a space in a term matches any run of whitespace,
  `-`, `_` or `.`, or nothing at all. So the same term also finds `quuxbridgeholdings`,
  `quuxbridge-holdings`, `quuxbridge_holdings` and `quuxbridge.holdings`.

Real leaks rarely keep a name's original spelling: email addresses, URLs, file names and
variable names all lower-case it or drop the spaces. Loose matching catches those.

### Exact list: `.private/leaks_exact.txt` (optional)

Some names are also ordinary words, or short enough to appear inside ordinary words. Loose
matching would flag them all over the code (for example, a two-letter abbreviation matches
inside many common words), burying real hits in noise.

Terms in this file match only as whole words and only with the exact capitalisation.
With `Zorbly` on the exact list, `worked at Zorbly.` is a hit, but `zorbly`, `ZorblyX` and
`xZorbly` are not.

If the file does not exist, it is ignored. The main list is still required either way.

Move a term from the main list to the exact list only after checking that its hits are
false positives. Exact matching can miss lower-case or run-together forms of the name.

## Pattern checks

These run on every line, alongside the lists:

- **Anthropic API keys:** any occurrence of the Anthropic key prefix.
- **OAuth client secrets:** the client secret key followed by `:` or `=` (optionally after
  a quote), as in a credentials JSON file or an environment assignment. Plain mentions,
  such as the name of the Google library function that loads a secrets file, are not
  flagged.
- **Email addresses:** every address is a hit unless it is one of these:
  - `example.com` or any subdomain (`careers.example.com`). These are reserved for
    examples and cannot belong to a real person.
  - LinkedIn's alert senders the code reads from (`jobalerts-noreply@linkedin.com`,
    `jobs-noreply@linkedin.com`), plus `messages-noreply@linkedin.com`, which a test uses
    as a sender that must be rejected.
  - GitHub noreply addresses, ending in `@users.noreply.github.com` (in any
    capitalisation). These are GitHub's private commit addresses.

  Lookalikes stay blocked: `example.com.evil.io`, `notexample.com` and
  `users.noreply.github.com.evil.io` are all hits.
- **The current employer's abbreviation:** matched as a case-insensitive substring
  anywhere. It is the term most likely to slip into code and docs, so it is checked even
  if the lists are edited.

## History scan

If the repository has at least one commit, the scanner also reads
`git log --all -p` (every branch and tag) and checks:

- each commit's **author** and **committer** name and email;
- each commit's **message**;
- every **added** and **removed** line in every diff.

Removed lines matter: a name deleted from the current tree is still published in the
history. Hits are reported as `history <sha> path:line`, with `(removed)` for removed
lines and `(author/committer/message line N)` for commit headers. Unchanged context lines
are skipped, since their content was already checked in the commit that added it.

A repository with no commits gets a tree scan only. If `.git` exists but git cannot run,
the scan stops with exit code 2 instead of skipping the history.

## What the tree scan skips

- `.git/` (covered by the history scan), `.private/` (the lists themselves) and `.venv/`.
- `__pycache__/`: compiled bytecode is generated from source files that are already
  scanned, and it is git-ignored. Its binary format also puts type bytes next to strings,
  which made the email check report addresses that are really allowed ones with a stray
  letter attached. A `.pyc` that was committed by mistake is still caught by the history
  scan.

## Not flagging itself

The scanner and its tests must contain the key prefix, the client secret key and the
employer abbreviation in order to check for them. To stop them from matching their own
patterns, they build those strings at run time with `"".join([...])` from pieces.
`"".join` is used rather than `+` because Python folds `"a" + "b"` into one constant in
the compiled `.pyc`.

Tests use only made-up terms, such as `Quuxbridge Holdings` and `Zorbly`, and write a
temporary fake list. They never read the real lists.

## Positive controls before each release

A clean result only means something if the scanner can still find a leak. Before each
release:

1. **Run the tests:** `python -m unittest discover -s tests`. The scanner tests plant fake
   terms, keys, secrets and addresses in a temporary repository, including in commit
   history, and check that each one is reported.
2. **Run one real-list control:** copy one term from `.private/leaks.txt` into a
   temporary file in the repository, run the scan and confirm exit code 1 with that
   file named. Then delete the file.
3. **Run the missing-list control:** rename `.private/leaks.txt`, confirm the scan exits
   with code 2, then rename it back.
4. **Run the real scan** and confirm exit code 0 before publishing.
