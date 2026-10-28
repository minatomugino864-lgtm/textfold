# Body-first real batch — report (issue #84's smaller step)

Date: 2026-10-27/28. Model: the reader's configured one (`mimo-v2.6-pro`, Xiaomi MiMo custom OpenAI-compatible
provider). Machine: MacBook Air M4, macOS 27.0.1. Branch: `experiment/body-first-minimal-20261027`
(upstream/main 256a731 + #79's two commits cherry-picked — **stated dependency**, #79 not merged yet).
Design and preregistered rules: `DESIGN.md` in this folder. Raw events: `raw.jsonl`; per-run files
`run-M0.json`, `run-M1.json`, `run-M2.json`, `run-M2.json-2310.06117`.

## Corpus (manifest.json; fresh arXiv HTML fetches, sha16 pinned)

| paper | units | appendix units | unit share | words | appendix words | word share | verdict |
|---|---|---|---|---|---|---|---|
| 2310.01704 | 52 | 16 | 31% | 4,463 | 947 | 21% | low |
| 2310.06117 | 117 | 52 | 44% | 5,916 | 1,593 | 27% | mid |
| 2308.00352 | 101 | 50 | 50% | 6,733 | 2,617 | 39% | high |

(2304.03442 excluded: 16k words, lowest appendix share, and the call budget — 500 attempts for the session —
was better spent on the paired strategies.)

## How the runs worked (identical start for every strategy)

One **warmup** pass built the en levels (ladder + links) for all three papers once: 94 real calls, ~12 min.
Each strategy then ran in its own data folder **copied from that warmup state**, so every strategy started
with identical levels and the comparison isolates the translation stage; the ladder cost is reported here as
warmup, not inside a strategy's numbers. Each strategy: fresh server with the strategy's env, `zh` generation
per paper, the reader-visible state polled over HTTP (`/api/doc/<id>?lang=zh` — the data the page assembles
from), events to `raw.jsonl`, one paired pass per strategy (no repeats; single-run noise is reported as such).

## Results (per paper, seconds; body readable = every body unit's zh rendering differs from the original,
measured over HTTP the way the page would show it)

| paper | M0 body readable | M1 | M2 | M2 appendix fill (open-equivalent) | calls M0 / M1 / M2 (+fill) |
|---|---|---|---|---|---|
| 2310.01704 (low) | 51.1 | 48.9 | 646.0* | — (harness bug, see below) | 25 / 25 / 20 |
| 2310.06117 (mid) | 77.2 | 77.3 | **64.6** | +51.6 s, 15 calls | 75 / 75 / 61 (+15) |
| 2308.00352 (high) | 80.2 | 82.4 | 86.8 | — (harness bug) | 95 / 95 / 68 |

*The 646 s run: one body part hung ~9 minutes on the model endpoint (the job log reads "1 of 8 still asked …
9 min on"); the strategy cannot be judged on that paper this round.

## Findings

1. **M1 (dispatch order only) buys the reader nothing on 0.10.38.** The `as_answered` refactor (#82's fix)
   assigns translations to units only after every reply is in, so body parts finishing first do not persist
   first: body-readable == pipeline-done for M1 on all three papers (48.9/77.3/82.4). Under today's code,
   reordering alone cannot be observed by the reader. (Upstream could stream persistence for body parts —
   that would be a different, larger change; not part of this minimal step.)
2. **M2 works end to end, exactly as the maintainer sketched.** The skip strategy dispatched only the body's
   parts (translate totals 8/23/20 vs M0's 13/37/47), left the whole appendix as **real gaps** — measured:
   52 untranslated appendix units on 2310.06117 after the build — and the open-equivalent fill (the same POST
   `fillGaps` sends) completed the appendix in **51.6 s / 15 calls**, leaving the stored translation whole
   (all 117 units) with nothing lost.
3. **The wall-clock gain on this batch is small and within model-latency noise.** M2 vs M0: −12.6 s (16%) on
   06117, +6.6 s on 00352 (a slow body part dominated that run; M0's run got a lucky appendix tail), unmeasurable
   on 01704 (the hung part). With `jobs=4` and a fast small model, single-part latency swings (one part ran 9
   minutes) are larger than the ordering effect. **Per the preregistered rules: the speedup is NOT confirmed by
   this batch** — what is confirmed is the mechanism (body persists earlier when the appendix is skipped; the
   fill completes it; no data loss), and the effect should scale with the appendix's share of the text (the
   maintainer's 60–77% is well above this corpus's 21–39%) and with slower or rate-limited endpoints.
4. **Cost of M2**: nothing added up front (fewer calls in the initial generation); the appendix's translation
   is paid on open — measured here as 51.6 s / 15 calls for the mid paper. Total work is the same text either way.

## Harness correction (found during the run, fixed in run_batch.py)

The first M2 pass recorded `gaps_after_build: null` and a 2.1 s "fill" with no calls: the harness read
`meta.gaps` from `/api/doc`, but `/api/doc` (fold.document) does not expose meta.gaps. The harness now counts
untranslated appendix units from the units themselves (the same rendering comparison the page would show), and
the fill measurement polls until every appendix unit is translated. The re-run on 2310.06117
(`run-M2.json-2310.06117`) is the honest measurement; the first M2 pass's fill columns are void (its initial
build's dispatch totals — 8/23/20 body-only vs 13/37/47 — are valid and were the evidence the skip worked).

## What this answers for the maintainer

- "How much sooner is the body readable?" — On this batch: 0–16% per paper, not separable from model-latency
  noise with one paired pass; the mechanism works (body persists while the appendix waits as gaps).
- "What does it cost?" — The appendix's translation is unchanged in size and paid on open (measured: 51.6 s /
  15 calls for the mid paper); nothing is lost, and `gaps`/`fill` do the tracking.
- The change itself is ~20 lines in `translate.py` (the skip + the `_again`/`_titles` scope), behind an
  experiment knob on this branch; the upstream patch would make it the default and drop the knob.

## Reproduce

```bash
.venv/bin/python work/experiments/body-first-20261027/run_batch.py prepare
.venv/bin/python work/experiments/body-first-20261027/run_batch.py warmup
.venv/bin/python work/experiments/body-first-20261027/run_batch.py run M0   # then M1, M2
.venv/bin/python work/experiments/body-first-20261027/run_batch.py report
```
