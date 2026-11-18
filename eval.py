"""Compare stored verdicts against hand labels in labels.csv.

labels.csv columns: job_id, title, company, location, url, label, split.
Only job_id, label and split are read; the rest are there to make labelling
easier. split is "tune" or "holdout"; held-out rows are kept out of prompt
tuning, so their disagreements are never listed.
"""

import csv
import os
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LABELS_FILE = ROOT / "labels.csv"
VERDICTS = ["shortlist", "maybe", "skip"]
SPLITS = ["tune", "holdout"]
COLUMNS = ["job_id", "title", "company", "location", "url", "label", "split"]
HOLDOUT_SIZE = 40
HOLDOUT_SEED = 20261106


def read_labels(path: Path = LABELS_FILE) -> dict[str, str]:
    """job_id -> label for rows with a non-blank label. Raises ValueError on unknown labels."""
    labels = {}
    for line_no, row in enumerate(_read_rows(path), start=2):
        label = (row.get("label") or "").strip().lower()
        if not label:
            continue
        if label not in VERDICTS:
            raise ValueError(f"{path.name} line {line_no}: label {label!r} is not one of {VERDICTS}")
        labels[row["job_id"].strip()] = label
    return labels


def read_splits(path: Path = LABELS_FILE) -> dict[str, str]:
    """job_id -> split for rows with a non-blank split. Raises ValueError on unknown splits."""
    splits = {}
    for line_no, row in enumerate(_read_rows(path), start=2):
        split = (row.get("split") or "").strip().lower()
        if not split:
            continue
        if split not in SPLITS:
            raise ValueError(f"{path.name} line {line_no}: split {split!r} is not one of {SPLITS}")
        splits[row["job_id"].strip()] = split
    return splits


def _read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _write_rows(path: Path, rows: list[dict]) -> None:
    """Write via a temp file and replace, so a failed write never truncates the labels."""
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({c: r.get(c) or "" for c in COLUMNS} for r in rows)
    os.replace(tmp, path)


def write_template(listings, path: Path = LABELS_FILE) -> tuple[int, int]:
    """Add listings not yet in labels.csv with a blank label. Existing rows, labels and
    splits are kept. Returns (added, total)."""
    rows = _read_rows(path)
    known = {r["job_id"] for r in rows}
    added = 0
    for listing in listings:
        if listing["job_id"] not in known:
            rows.append({c: listing[c] for c in COLUMNS[:5]})
            added += 1
    _write_rows(path, rows)
    return added, len(rows)


def assign_split(path: Path = LABELS_FILE, size: int = HOLDOUT_SIZE, seed: int = HOLDOUT_SEED) -> Counter:
    """Give every labelled row without a split one.

    The first time, `size` labelled rows go to holdout, sampled per label in
    proportion to the label counts (fixed seed), and the rest to tune. Once a
    holdout exists it is never reshuffled; rows labelled later go to tune.
    Returns the count of rows per (split, label).
    """
    rows = _read_rows(path)
    labelled = [r for r in rows if (r.get("label") or "").strip()]
    unassigned = [r for r in labelled if not (r.get("split") or "").strip()]
    has_holdout = any((r.get("split") or "").strip().lower() == "holdout" for r in rows)

    holdout_ids: set[str] = set()
    if not has_holdout and unassigned:
        by_label: dict[str, list[str]] = {}
        for r in unassigned:
            by_label.setdefault(r["label"].strip().lower(), []).append(r["job_id"])
        # largest-remainder allocation so the per-label quotas sum to `size`
        total = len(unassigned)
        quotas = {lab: size * len(ids) / total for lab, ids in by_label.items()}
        alloc = {lab: int(q) for lab, q in quotas.items()}
        for lab in sorted(quotas, key=lambda l: (alloc[l] - quotas[l], l))[: size - sum(alloc.values())]:
            alloc[lab] += 1
        rng = random.Random(seed)
        for lab in sorted(by_label):
            holdout_ids.update(rng.sample(sorted(by_label[lab]), min(alloc[lab], len(by_label[lab]))))

    for r in unassigned:
        r["split"] = "holdout" if r["job_id"] in holdout_ids else "tune"
    _write_rows(path, rows)
    return Counter((r["split"].strip().lower(), r["label"].strip().lower()) for r in labelled)


def evaluate(labels: dict[str, str], predictions: dict[str, str]) -> dict:
    """labels and predictions are job_id -> verdict. Only jobs present in both are compared."""
    both = sorted(labels.keys() & predictions.keys())
    confusion = Counter((labels[j], predictions[j]) for j in both)
    agree = sum(confusion[(v, v)] for v in VERDICTS)
    return {
        "compared": len(both),
        "agree": agree,
        "rate": agree / len(both) if both else None,
        "confusion": confusion,
        "disagreements": [(j, labels[j], predictions[j]) for j in both if labels[j] != predictions[j]],
        "unscored": sorted(labels.keys() - predictions.keys()),
    }


RESULTS_FILE = ROOT / "results.csv"
# The tune_* columns hold the metrics for the row's split: tuning rows for split=tune,
# held-out rows for split=holdout.
RESULT_COLUMNS = ["timestamp", "split", "model", "effort", "prompt_sha", "rubric_sha", "note", "tune_compared",
                  "tune_agree", "tune_rate", "skip_baseline", "top15_shortlist", "top15_shortlist_or_maybe",
                  "shortlist_precision", "shortlist_recall", "shortlist_hits", "shortlist_predicted",
                  "shortlist_labelled"]


def shortlist_counts(labels: dict[str, str], predictions: dict[str, str]) -> tuple[int, int, int]:
    """(hits, model shortlists, labelled shortlists) over jobs present in both dicts."""
    both = labels.keys() & predictions.keys()
    predicted = [j for j in both if predictions[j] == "shortlist"]
    actual = [j for j in both if labels[j] == "shortlist"]
    hits = sum(labels[j] == "shortlist" for j in predicted)
    return hits, len(predicted), len(actual)


def shortlist_precision_recall(labels: dict[str, str], predictions: dict[str, str]) -> tuple[float | None, float | None]:
    """Precision: share of model shortlists you labelled shortlist. Recall: share of your
    shortlists the model shortlisted. Only jobs in both dicts count; None when undefined."""
    hits, predicted, actual = shortlist_counts(labels, predictions)
    return (hits / predicted if predicted else None,
            hits / actual if actual else None)


def top_k_precision(labels: dict[str, str], ranked_ids: list[str], k: int = 15) -> tuple[float, float] | None:
    """Of the k best-ranked labelled ids, the share labelled shortlist and the share
    labelled shortlist or maybe. ranked_ids is best first; unlabelled ids are ignored."""
    top = [j for j in ranked_ids if j in labels][:k]
    if not top:
        return None
    return (sum(labels[j] == "shortlist" for j in top) / len(top),
            sum(labels[j] in ("shortlist", "maybe") for j in top) / len(top))


def read_results(path: Path = RESULTS_FILE) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_results(rows: list[dict], path: Path = RESULTS_FILE) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({c: r.get(c) or "" for c in RESULT_COLUMNS} for r in rows)
    os.replace(tmp, path)


def append_result(row: dict, path: Path = RESULTS_FILE) -> None:
    """Append one iteration. A file with an older header is rewritten with the current
    columns first; values for new columns are left blank in old rows."""
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
        if header != RESULT_COLUMNS:
            write_results(read_results(path), path)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULT_COLUMNS, extrasaction="ignore")
        if new:
            writer.writeheader()
        writer.writerow(row)


def format_confusion(confusion: Counter) -> str:
    width = max(len(v) for v in VERDICTS) + 2
    lines = ["label \\ model".ljust(width + 4) + "".join(v.rjust(width) for v in VERDICTS)]
    for actual in VERDICTS:
        lines.append(actual.ljust(width + 4) + "".join(str(confusion[(actual, p)]).rjust(width) for p in VERDICTS))
    return "\n".join(lines)
