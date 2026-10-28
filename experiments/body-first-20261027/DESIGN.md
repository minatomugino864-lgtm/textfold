# Body-First Minimal — design, state machine, metrics, preregistration

Date: 2026-10-27. Base: upstream/main `256a731` (0.10.38) + #79's two commits cherry-picked
(`1462519` flag, `200e460` ancestry fix) as a **stated temporary dependency** — #79 is not merged yet, and
chunk["app"] is what tells the pipeline where a paper's body ends. Branch:
`experiment/body-first-minimal-20261027`.

## 1. The real state machine (read from 0.10.38's code, not memory)

One generation of paper P into language L runs, in `pipeline.generate`:

```
resolve → fetch/parse → [en exists?] ladder.build (all sections, ThreadPool 4)
                       → links.build (one call, whole paper)
                       → store.save_en
       → [L ≠ text language] translate.build(L)   ← the only stage this work touches
                       → store.save_tr
```

`translate.build(L)` (0.10.38):
1. `tasks` = every chunk's parts (a part ≈ 5,000 chars); `_ask` runs them through `ThreadPoolExecutor(jobs)`,
   replies read in paper order (`llm.as_answered`, #70/#82's fix).
2. `_again(L)` = whatever `gaps(L)` still misses — **including parts whose call just failed** — asked once more
   (`again=True`). This is why "just reorder" cannot leave the appendix behind: the same build refills it.
3. `_titles(L)`: headings without text of their own, and the paper title.

The reader's road back (verified in `web/app.js`, `fillGaps`): a stored translation with missing paragraphs
sets `meta.gaps[L]` in `store.assemble`; opening `/p/<id>?lang=L` runs `fillGaps` once per gap-count, which
submits a full generation; its pipeline branch (en exists, tr exists, base matches) runs `apply_tr` +
`translate.gaps` → `translate.fill` → `save_tr`. **The appendix's levels already exist** (ladder covered every
section); the fill only translates the missing text. This is the mechanism the maintainer points at.

Facts that bound the design:
- Reordering alone (M1) changes dispatch order only; with `jobs=4` body and appendix parts still interleave in
  completion, and `_again`+`_titles` would refill the appendix inside the same build. It cannot satisfy
  "appendix translation left as gaps" by itself.
- Skipping the appendix's translation but not `_again`/`_titles` fails the same way.
- Ladder and links still cover the appendix in every strategy — the maintainer named this limitation and asked
  for the measurement anyway.

## 2. Strategies

| id | what runs | what it answers |
|---|---|---|
| **M0** | upstream behavior, untouched | the baseline: when does the reader get the body today |
| **M1** | same calls, body parts dispatched before appendix parts | how much the dispatch order alone contributes |
| **M2** | the maintainer's minimal step: initial translation of body chunks only; appendix left as real `gaps`; `_again`/`_titles` in the same build restricted to the body; the appendix filled later by the existing open→fillGaps road | the candidate: what the reader gains, and what the later fill costs |

No API scope, no queue tiers, no new layer field, no reader UI. M1/M2 are behind an experiment-only env knob
(`TEXTFOLD_BODY_FIRST=order|skip`) read inside `translate.build`; the final upstream patch (if any) makes the
behavior the default and drops the knob.

## 3. Metrics (monotonic clock; JSONL events; server HTTP where the reader would be)

- `T_ladder_done`, `T_links_done` — job stage transitions (`/api/jobs`).
- `T_body_translation_persisted` — first poll instant at which `GET /api/doc/<id>?lang=L` shows **every
  body unit** (`app is not True`) carrying an L translation. This is the reader-visible state (the page
  assembles from these layers); internal function completion is never reported as this.
- `T_pipeline_done` — job status `done`.
- `T_appendix_fill_done` (M2) — first later instant `meta.gaps[L]` is empty after the open-triggered fill.
- calls: attempted / successful / cached per run (from the job's log lines and the llm-cache file count);
  failures and retries; no cost estimation beyond call counts (the model is the reader's configured one).
- body/appendix share per paper: units and words, both reported.

## 4. Preregistered comparison rules

- Same `source.html` per paper across strategies (hash in manifest.json); same model/provider (the reader's
  configured one — recorded from the provider name and model id, proxy labels noted), same prompts (prompt
  text depends only on the corpus), same target language (zh), same `jobs=4`, same machine, same session.
- **One warmup pass** builds en (ladder+links) for all papers once; each strategy's data folder starts from a
  copy of that state, so the strategies isolate the translation stage and the ladder cost is reported
  separately as warmup (~100 calls), never mixed into the per-strategy numbers.
- Each strategy runs in its own `TEXTFOLD_DATA` (no shared layers), translate calls real (per-run cache).
- One paired pass M0→M1→M2 over the same papers, then the M2 fill-on-open measurement. Repeats only if the
  budget allows; no significance claims from one pass — paired differences and per-paper spreads are reported
  as observations.
- A strategy's run is invalid (and re-run once) if the final HTTP state contradicts the strategy (e.g. M2 with
  zero gaps) or a generation failed; model 401/403/429/502 twice in a row stops the session.
- Budget: ≤500 real call attempts this session (failures counted); wall clock ≤4h.

## 5. What would falsify M2

- `T_body_translation_persisted` not earlier than M0's (then the bottleneck is ladder/links, and the answer to
  the maintainer is "the gain is before translation" — M3 exploratory only).
- The open-triggered fill costing more than the initial skip saved, or leaving the translation corrupt
  (then the minimal step does not pay for itself).
- Hard failures of the no-appendix / checklist cases (regression) — then the implementation is wrong, not the
  idea.
