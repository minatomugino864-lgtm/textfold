Evidence pack for the body-first batch measured for issue #84 (comment of 2026-10-28).

- `experiments/body-first-20261027/report.md` — the write-up the issue links to
- `DESIGN.md` — state machine, metric definitions, preregistered comparison rules
- `manifest.json` — the papers (fresh arXiv HTML, sha16 pinned), unit/word counts, appendix shares
- `run_batch.py` — the harness (prepare / warmup / run M0|M1|M2 / report)
- `raw.jsonl` — every recorded event of the session (timings, stage transitions, reader-visible states)
- `run-M0.json`, `run-M1.json`, `run-M2.json` — per-strategy results
- `run-M2.json-2310.06117` — the mid paper re-measured with the corrected harness (the honest fill numbers)

Not included, and why: the papers' HTML and their layers stay out of the repository (copyright; only hashes
and counts are published); server logs are local; nothing here carries credentials — the model and its key
come from the reader's own settings at run time.

The code these measurements ran on: branches `experiment/body-first-minimal-20261027` and
`feat/appendix-metadata` (PR #79) on this fork.
