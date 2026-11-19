# Evaluation

## What this shows

Job Scout is a high-recall filter, not a classifier that beats a trivial baseline.

- On the data used to tune the rubric, agreement with my own labels reached 76.2% against a 65.1% skip-everything baseline.
- On a held-out set that was run once, agreement was 60.0% (24 of 40) against a 65.0% baseline. With 40 jobs this is statistically indistinguishable from the baseline (95% interval about 45% to 74%).
- No job I had labelled "shortlist" was scored "skip" in either set. The tool's value is that it rarely hides a role I would have wanted, so I read a short list instead of every alert.

I have not measured time saved. That is a separate two-week trial (see Next).

## Method

- **Labels.** I labelled 166 jobs from real LinkedIn alert emails as shortlist, maybe or skip.
- **Split.** The labelled jobs were split into a tuning set (126) and a held-out set (40) with a fixed random seed (20261106). I tuned the rubric on the tuning set only.
- **Baseline.** Predicting "skip" for every job is right 65% of the time, because most alerts are not relevant. A scorer has to beat that to claim anything.
- **Model.** claude-haiku-5-5 at low effort scores each job against a written rubric and returns a score, a verdict and short reasons, validated against a schema.
- **Versioning.** Every result row records the prompt hash and rubric hash, so a number can always be traced to the exact instructions that produced it.
- All results here are for the iteration 4 rubric (rubric hash 0a468fc03475, prompt hash 776782997ac5). The rubric has changed slightly since, a location clause among other edits, and that version has not been evaluated.
- **Held-out discipline.** The held-out set was scored once, after the final rubric was fixed. I did not change the rubric afterwards.

## Results

Tuning set, 126 jobs, skip-everything baseline 65.1%:

| Iteration | What changed | Agreement | Shortlist precision | Shortlist recall |
| --- | --- | --- | --- | --- |
| 0 | Original rubric | 54.8% (69/126) | 100% | 30.4% |
| 1 | General rules by role family | 64.3% (81/126) | 85.7% | 26.1% |
| 2 | Platform and product-company rules | 64.3% (81/126) | 87.5% | 60.9% |
| 3 | Same rules, medium effort | 64.3% (81/126) | 81.3% | 56.5% |
| 4 | Consulting and platform-consultant section | 76.2% (96/126) | 87.5% (14/16) | 60.9% (14/23) |

Held-out set, 40 jobs, baseline 65.0%, final rubric, run once:

| Metric | Result |
| --- | --- |
| Agreement | 60.0% (24/40) |
| Shortlist precision | 60.0% (3/5) |
| Shortlist recall | 42.9% (3/7) |
| Labelled shortlists scored skip | 0 |
| Top-15 precision (labelled shortlist) | 7 of 15 |

Noise check: scoring the tuning set again with the same rubric and prompt moved agreement from 96 to 94 of 126 (76.2% to 74.6%). The model is not fully repeatable, so differences of one or two jobs mean nothing.

## What this does and does not show

- The tuning result is optimistic. I wrote the rubric rules from the cases the earlier iterations got wrong, so the tuning set is partly fitted.
- The held-out result is the honest number. It is below the baseline, but 40 jobs and five predicted shortlists are too few to say it is worse, only that there is no evidence it is better.
- Shortlist recall is modest (3 of 7 on held-out). The "high recall" claim is the weaker one: every labelled shortlist scored shortlist or maybe, none skip. It does not mean every shortlist was surfaced as a shortlist.
- Pay is rarely in alert emails, so the scorer is told not to guess it. Roles where pay decides the answer are scored on title and employer alone.

## Limitations

- One labeller (me), no second opinion on the labels.
- 166 labelled jobs from one inbox over a short period.
- A small held-out set, run once.
- Model output varies slightly between runs.
- No measurement yet of time saved or of any role it surfaced that I would otherwise have missed.
- The rubric in daily use is newer than the evaluated one. Its effect on agreement has not been measured.

## Next

1. Label 60 to 100 more jobs from the daily digests without touching the rubric.
2. Draw a new held-out split with a new seed, record the prompt and rubric hashes, and run it once. Report the result here whatever it shows.
3. Run a two-week trial logging daily triage minutes and any role the tool surfaced. Report time saved only from that log.

## Reproduce

Result rows live in `results.csv`, one per run, with the prompt and rubric hashes. See the README for the commands to score and evaluate, using your own labels.
