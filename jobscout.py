"""jobscout CLI."""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import db
import deepdive
import eval as evaluation
import fetch
import gmail
import morning
import parse
import score

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
SAMPLES_DIR = DATA_DIR / "samples"
LOG_FILE = DATA_DIR / "jobscout.log"


def setup_logging() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])
    for noisy in ("httpx", "httpx2", "anthropic", "googleapiclient"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_auth(args) -> None:
    service = gmail.get_service(interactive=True)
    email = gmail.account_email(service)
    db.record_auth(db.connect())  # starts the 7-day countdown the morning digest warns about
    print(f"Authorised read-only for {email}")


def cmd_fetch(args) -> None:
    service = gmail.get_service()
    label = None if args.query else args.label
    ids = gmail.list_message_ids(service, label=label, query=args.query, limit=args.limit)
    source = f'query "{args.query}"' if args.query else f'label "{label}"'
    print(f"{len(ids)} email(s) matching {source}")

    counts = fetch.fetch_new(service, db.connect(), ids, reprocess=args.reprocess)
    print(
        f"\nNew emails: {counts['parsed'] + counts['partial']} parsed "
        f"({counts['partial']} partial), {counts['failed']} failed, {counts['skipped']} skipped. "
        f"{counts['known']} already processed. {counts['new_listings']} new listing(s) saved."
    )
    if counts["failed"]:
        print(f"Failures are logged in {LOG_FILE}")

    if args.dump:
        SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
        for msg_id in ids[: args.dump]:
            msg = gmail.get_message(service, msg_id)
            (SAMPLES_DIR / f"{msg_id}.html").write_text(msg["html"], encoding="utf-8")
            (SAMPLES_DIR / f"{msg_id}.txt").write_text(msg["text"], encoding="utf-8")
            meta = {k: v for k, v in msg.items() if k not in ("html", "text")}
            (SAMPLES_DIR / f"{msg_id}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        print(f"Dumped {min(args.dump, len(ids))} email(s) to {SAMPLES_DIR}")


def load_samples(directory: Path) -> list[dict]:
    msgs = []
    for meta_file in sorted(directory.glob("*.json")):
        msg = json.loads(meta_file.read_text(encoding="utf-8"))
        for key, ext in (("html", ".html"), ("text", ".txt")):
            body = meta_file.with_suffix(ext)
            msg[key] = body.read_text(encoding="utf-8") if body.exists() else ""
        msgs.append(msg)
    return sorted(msgs, key=lambda m: m["internal_date"], reverse=True)


def cmd_parse(args) -> None:
    msgs = load_samples(Path(args.dir))
    report = parse.parse_emails(msgs)
    for r in report.results:
        status = "FAILED" if r.failed else f"{len(r.listings)} listing(s) from {r.source}"
        print(f"\n== {r.subject}\n   [{r.msg_id}] {status}")
        for listing in r.listings:
            print(f"   - {listing.title} | {listing.company} | {listing.location}\n     {listing.url}")
        for err in r.errors:
            print(f"   ! {err}")
    print(
        f"\n{len(msgs)} email(s): {len(report.results)} LinkedIn alert(s), "
        f"{len(report.skipped)} skipped (not LinkedIn), {len(report.notices)} LinkedIn notice(s) with no jobs, "
        f"{len(report.failed)} failed to parse. "
        f"{len(report.listings)} unique listing(s)."
    )


def cmd_score(args) -> None:
    conn = db.connect()
    print(f"Scoring with {score.MODEL}, effort {args.effort} (prompt {score.prompt_sha()[:12]})")
    scored, errors = score.score_all(conn, limit=args.limit, retry_errors=args.retry_errors,
                                     effort=args.effort, rescore=args.rescore)
    print(f"\n{scored} scored, {errors} error(s).")
    if errors:
        print(f"Errors are logged in {LOG_FILE}; re-run with --retry-errors to try them again.")


def cmd_list(args) -> None:
    conn = db.connect()
    rows = db.ranked_listings(conn, score.MODEL, score.prompt_sha(), args.effort)
    if args.verdict:
        rows = [r for r in rows if r["verdict"] == args.verdict]
    for r in rows[: args.limit]:
        if r["score"] is None:
            status = "ERROR" if r["score_error"] else "unscored"
            print(f"\n  -- {status:9s}  {r['title']} | {r['company']} | {r['location']}")
        else:
            print(f"\n{r['score']:4d} {r['verdict']:9s}  {r['title']} | {r['company']} | {r['location']}")
            for reason in json.loads(r["reasons_json"]):
                print(f"               - {reason}")
        print(f"               {r['url']}")
    print(f"\n{len(rows)} listing(s)")


def cmd_eval(args) -> None:
    conn = db.connect()
    rows = db.ranked_listings(conn, score.MODEL, score.prompt_sha(), args.effort)
    if args.template:
        added, total = evaluation.write_template(rows)
        print(f"{evaluation.LABELS_FILE.name}: added {added} listing(s), {total} row(s) in total. "
              f"Fill in the label column with shortlist, maybe or skip.")
        return
    if not evaluation.LABELS_FILE.exists():
        sys.exit(f"error: {evaluation.LABELS_FILE.name} not found. Run `jobscout.py eval --template` first.")
    if args.split:
        counts = evaluation.assign_split()
        for split in evaluation.SPLITS:
            per_label = ", ".join(f"{counts[(split, v)]} {v}" for v in evaluation.VERDICTS)
            print(f"{split:8s} {sum(counts[(split, v)] for v in evaluation.VERDICTS):3d}  ({per_label})")
        return
    try:
        labels = evaluation.read_labels()
        splits = evaluation.read_splits()
    except ValueError as e:
        sys.exit(f"error: {e}")
    no_split = sorted(labels.keys() - splits.keys())
    if no_split:
        print(f"note: {len(no_split)} labelled row(s) have no split and count as tune; "
              f"run `eval --split` to assign them.\n")
    tune = {j: l for j, l in labels.items() if splits.get(j, "tune") == "tune"}
    holdout = {j: l for j, l in labels.items() if splits.get(j) == "holdout"}
    predictions = {r["job_id"]: r["verdict"] for r in rows if r["verdict"]}
    titles = {r["job_id"]: f"{r['title']} | {r['company']}" for r in rows}

    ranked_ids = [r["job_id"] for r in rows if r["score"] is not None]  # best first

    def metrics(split_labels):
        result = evaluation.evaluate(split_labels, predictions)
        if not result["compared"]:
            return None
        scored = {j: l for j, l in split_labels.items() if j in predictions}
        return {
            "result": result,
            "baseline_hits": sum(l == "skip" for l in scored.values()),
            "baseline": sum(l == "skip" for l in scored.values()) / len(scored),
            "top15": evaluation.top_k_precision(scored, ranked_ids, 15),
            "pr": evaluation.shortlist_precision_recall(split_labels, predictions),
            "counts": evaluation.shortlist_counts(split_labels, predictions),
        }

    def report(name, m):
        result, (hits, predicted, labelled) = m["result"], m["counts"]
        fmt = lambda x: "n/a" if x is None else f"{x:.0%}"  # noqa: E731
        print(f"{name} agreement: {result['agree']}/{result['compared']} = {result['rate']:.0%}   "
              f"(skip-everything baseline {m['baseline_hits']}/{result['compared']} = {m['baseline']:.0%})")
        print(f"Shortlist precision {hits}/{predicted} = {fmt(m['pr'][0])}, "
              f"shortlist recall {hits}/{labelled} = {fmt(m['pr'][1])}")
        print(f"Top 15 {name.lower()} listings by score: {m['top15'][0]:.0%} shortlist, "
              f"{m['top15'][1]:.0%} shortlist or maybe\n")
        print(evaluation.format_confusion(result["confusion"]))

    tm = metrics(tune)
    if tm is None:
        sys.exit("error: no tuning listing has both a label and a score for the current prompt.")
    print(f"Effort {args.effort}.")
    report("Tuning", tm)
    if tm["result"]["disagreements"]:
        print("\nTuning disagreements (label -> model):")
        for job_id, label, pred in sorted(tm["result"]["disagreements"], key=lambda d: (d[1], d[2])):
            print(f"  {label:9s} -> {pred:9s}  {titles.get(job_id, job_id)}")

    hm = metrics(holdout)
    held = evaluation.evaluate(holdout, predictions)
    if args.holdout:
        print()
        if hm:
            report("Held-out", hm)  # held-out disagreements are never listed
        else:
            print("Held-out: no scored rows.")
    else:
        print(f"\nHeld-out: {held['compared']} scored row(s), rate hidden (run `eval --holdout` to show).")
    unscored = len(tm["result"]["unscored"]) + len(held["unscored"])
    if unscored:
        print(f"\n{unscored} labelled listing(s) have no score for the current prompt; run `score`.")

    if args.log is not None:
        # with --holdout the held-out row is logged; otherwise the tuning row
        split, m = ("holdout", hm) if args.holdout else ("tune", tm)
        if m is None:
            sys.exit("error: nothing to log for the held-out split.")
        result, (hits, predicted, labelled) = m["result"], m["counts"]
        precision, recall = m["pr"]
        evaluation.append_result({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "split": split,
            "model": score.MODEL,
            "effort": args.effort,
            "prompt_sha": score.prompt_sha()[:12],
            "rubric_sha": db.sha256_text(score.RUBRIC_FILE.read_text(encoding="utf-8"))[:12],
            "note": args.log,
            "tune_compared": result["compared"],
            "tune_agree": result["agree"],
            "tune_rate": f"{result['rate']:.4f}",
            "skip_baseline": f"{m['baseline']:.4f}",
            "top15_shortlist": f"{m['top15'][0]:.4f}",
            "top15_shortlist_or_maybe": f"{m['top15'][1]:.4f}",
            "shortlist_precision": "" if precision is None else f"{precision:.4f}",
            "shortlist_recall": "" if recall is None else f"{recall:.4f}",
            "shortlist_hits": hits,
            "shortlist_predicted": predicted,
            "shortlist_labelled": labelled,
        })
        print(f"\nLogged {split} result to {evaluation.RESULTS_FILE.name}")


def cmd_deepdive(args) -> None:
    if not sys.stdin.isatty():
        sys.exit("error: deepdive needs an interactive terminal for the confirmation and the "
                 "approve/edit/skip choices. Run it in a PowerShell window. No API call was made.")
    try:
        deepdive.run(db.connect(), args.job_id, Path(args.jd))
    except KeyboardInterrupt:
        sys.exit("\nCancelled. Nothing was sent.")
    except EOFError:
        sys.exit("\nNo keyboard input available, so it stopped at the prompt. Run deepdive in a "
                 "PowerShell window. Nothing was sent.")


def cmd_morning(args) -> None:
    # unattended: details go to data/jobscout.log only; stdout gets the one summary line
    root = logging.getLogger()
    for h in list(root.handlers):
        if type(h) is logging.StreamHandler:
            root.removeHandler(h)
    try:
        out = morning.run()
        line = morning.log_line(out)
    except Exception as e:  # e.g. the digest could not be written
        logging.exception("morning run failed before the digest was written")
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} FAILED no digest written: {type(e).__name__}: {e}")
        sys.exit(1)
    print(line)
    if not out.ok:
        sys.exit(1)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    setup_logging()
    parser = argparse.ArgumentParser(prog="jobscout")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("auth", help="run the read-only Gmail OAuth flow")
    p.set_defaults(func=cmd_auth)

    p = sub.add_parser("fetch", help="read alert emails, parse them and save new listings")
    p.add_argument("--label", default="jobscout")
    p.add_argument("--query", help="Gmail search string; used instead of --label")
    p.add_argument("--limit", type=int, help="only the N newest emails")
    p.add_argument("--dump", type=int, metavar="N", help="also save the N newest emails to data/samples/")
    p.add_argument("--reprocess", action="store_true", help="re-parse emails already processed")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("parse", help="parse dumped sample emails offline and print listings")
    p.add_argument("--dir", default=str(SAMPLES_DIR))
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("score", help="score unscored listings against rubric.md")
    p.add_argument("--limit", type=int, help="score at most N listings")
    p.add_argument("--retry-errors", action="store_true", help="also retry listings that failed before")
    p.add_argument("--effort", choices=score.EFFORTS, default=score.EFFORT)
    p.add_argument("--rescore", action="store_true",
                   help="score again even if already scored (adds new score rows; the latest is used)")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("list", help="print listings ranked by score")
    p.add_argument("--verdict", choices=evaluation.VERDICTS)
    p.add_argument("--limit", type=int)
    p.add_argument("--effort", choices=score.EFFORTS, default=score.EFFORT, help="show scores made at this effort")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("eval", help="compare verdicts with labels.csv")
    p.add_argument("--template", action="store_true", help="add unlabelled listings to labels.csv")
    p.add_argument("--split", action="store_true", help="assign tune/holdout to labelled rows (fixed seed)")
    p.add_argument("--holdout", action="store_true", help="also show the held-out agreement rate")
    p.add_argument("--log", metavar="NOTE", help="append this iteration's tuning metrics to results.csv")
    p.add_argument("--effort", choices=score.EFFORTS, default=score.EFFORT, help="evaluate scores made at this effort")
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("deepdive", help="research one listing's employer and draft outreach for approval")
    p.add_argument("job_id")
    p.add_argument("--jd", required=True, metavar="FILE", help="text file with the job description you pasted")
    p.set_defaults(func=cmd_deepdive)

    p = sub.add_parser("morning", help="unattended: fetch, score new listings, write data/digests/<date>.md")
    p.set_defaults(func=cmd_morning)

    args = parser.parse_args()
    try:
        args.func(args)
    except (gmail.GmailError, score.ScoringAborted, deepdive.DeepDiveError) as e:
        sys.exit(f"error: {e}")


if __name__ == "__main__":
    main()
