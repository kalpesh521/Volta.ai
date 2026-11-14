# 16 — Phase 3 implementation (carry-over gate, part 1)

Phase 3 starts with a gate ([13 §5](./13-progress-tracker.md)): measure answer quality before building agents whose answers cannot be
checked against one number. This document covers what is built so far: **3.5 the L3 judge** (built, not yet run on a real model) and
**3.6 answer prompt v6** (registered, not the default). Items 3.7 to 3.14 and the planning agents are not started.

Nothing here changes what users get today: the default prompt is still v4, and the judge never runs on the request path.

## 1. The L3 judge (3.5)

Design: [05 §5](./05-evals.md). Code: [backend/app/agents_platform/evals/judge/](../../backend/app/agents_platform/evals/judge/).

```
python -m app.agents_platform.evals.judge cases                                            # validate the dataset (free)
python -m app.agents_platform.evals.judge run --max-model-calls 20 --versions 2026-10-04.v4 2026-11-13.v6 --report out.json
python -m app.agents_platform.evals.judge ab --max-model-calls 40 --a 2026-10-04.v4 --b 2026-11-13.v6
python -m app.agents_platform.evals.judge calibrate --max-model-calls 30
```

Add `--dry-run` to any paid command to print the plan and worst-case cost and send nothing.

### 1.1 What it does

| Part | File | What it is |
|---|---|---|
| Frozen cases | `cases.py`, `judge_cases.jsonl` | 15 cases. Each holds exactly what the answer prompt shows the model (facts, analytics, freshness), captured from the real v2 pipeline over a fixed telemetry reading, plus hand-written reviewer notes. Frozen data means a score moves only when the prompt, the model or the judge moves |
| Capture | `tests/capture_judge_fixtures.py` | Regenerates the dataset through the real onboarding, ingest and supervisor with a fake model. Not collected by the normal run |
| Rubric | `rubric.py` | Six criteria with 1/3/5 anchors: faithful, answers_question, actionable, caveats, safe, plain_language. Structured verdict; the failed list is derived in code from the scores. Pinned as `JUDGE_PROMPT_VERSION` |
| Deterministic checks | `deterministic.py` | Run on every answer first and independently of the judge. Hard: ungrounded number, claim of switching a device, unsafe advice, empty field. Soft: raw field name, markdown |
| Scoring run | `runner.py` | Answer per case and prompt version, checks, then a verdict. Per-version means, pass rates (a criterion passes at 4 or more) and hard-failure counts |
| Pairwise A/B | `pairwise.py` | Each pair is judged twice with positions swapped. A version wins only if it wins both orders; disagreement is a tie |
| Calibration | `calibration.py`, `calibration.jsonl` | Judge vs human pass/fail labels, as agreement and Cohen's kappa |
| Gate | `baseline.py` | Against a baseline report: any new hard failure fails; a score may not fall more than 2%. Reports from a different judge prompt, judge model or dataset are refused, not compared |
| Cap | `budget.py` | `ModelCallBudget`, now shared with the parity harness |

### 1.2 Spending is capped in code

A real model costs real money and the OpenRouter balance is small, so these guarantees are tested like production ones.

* Every paid command **requires `--max-model-calls`** and has no default.
* A case (2 calls), a pair (4) or a calibration item (1) is started only if the whole of it fits; the budget is asked before sending.
* A call is charged when it is **sent**, whether or not it succeeded, since a failed request can still be billed.
* Three model errors in a row stop the run, so a wrong key cannot walk through the budget.
* Retries and the fallback model are switched off for these runs, so one counted call is one request on the wire.
* Cases run one at a time, so the cap cannot be raced.
* The judge model must be set (`AI_JUDGE_LLM_MODEL`, new `LLMRole.JUDGE`). It must differ from the answer model unless `--allow-same-model` is passed, because a judge should be stronger than what it judges.

**No real model call was made in this work.** Every test uses a scripted model.

### 1.3 Calibration is not done, and the numbers cannot yet be trusted

Doc 05 asks for kappa of at least 0.7 on 100 human-labelled answers before a rubric gates anything. `calibration.jsonl` has **12** answers,
**labelled by the author of the code, not by a human reviewer**, written to cover each criterion on both sides. The harness will not
call a criterion `trusted` below 100 answers whatever its kappa is (tested: a perfect kappa on 10 answers is still not trusted). So:
the judge can be run, but its scores must not decide a prompt or the v2 switch until the set is reviewed and grown (Y8, item 3.5).

### 1.4 Findings while building

| Finding | Effect |
|---|---|
| The `solar-today` fixture has two different "today" values: the live reading's cumulative figure (4.2 kWh) and the daily summary built from one tick (0.05 kWh) | Both are in the data, so either is a grounded answer, but it is an artefact of one-tick snapshots. Richer snapshots are item 3.10 |
| `describe_models` now lists a `judge` role that shares the answer model when no judge model is set | Matches the other roles; the eval itself refuses to run in that state |

## 2. Answer prompt v6 (3.6)

v6 is v5 plus one rule: *observation must answer the question asked, in its first sentence, with the value that answers it; warnings and
freshness come after, unless stale data changes the answer.* It comes from the real-model run in [15 §9.3](./15-phase2-implementation.md), where the first field opened with
"no warnings, data is fresh" on a battery question. A test pins that v6 is v5 with exactly that rule and nothing else.

It is **registered but not the default** (v4 stays). It becomes the default only after `judge ab` compares it with v4 on a real model and it wins without a new hard failure (Y6).
Select it per deployment with `AI_PROMPT_ANSWER_VERSION=2026-11-13.v6`.

## 3. Tests

| File | What it pins |
|---|---|
| `test_judge_eval.py` (72) | Dataset validation, rubric and prompt (anchors present, answer treated as untrusted data), deterministic checks, the call cap over many budgets, charging on failure, the error-streak stop, the pairwise swap rule, kappa against hand-worked values, the trust threshold, the baseline gate, the command line (cap required, dry run sends nothing, same-model refusal, retries off) |
| `test_platform_registry.py` (+2) | v6 is v5 plus one rule; v6 is registered but not the default |

Test status: **826 pass and 11 skip** (the skips are the opt-in Postgres integration tests; Redis was available). Trajectory evals: 62 / 62 cases. Static check is clean.

Mutation checks were run on 17 guarantees and each was caught: the cap not checked (answer runner, pairwise, calibration), either call not charged, the error streak never resetting, no stop on repeated errors, the pairwise swap ignored, kappa replaced by raw agreement, trust ignoring set size, a hard failure not counting as a regression, incomparable reports being compared, an ungrounded number counted as soft, the judge not seeing reviewer notes, the same-model refusal, retries left on, and a dry run building a model client.

## 4. Known limits

Every row has a tracker item ([13 §8](./13-progress-tracker.md)).

| Limit | Next step |
|---|---|
| The judge has never run on a real model, so no answer-quality number exists yet | Y6 (spend) and Y9 (judge model and key), then item 3.5 |
| Calibration: 12 author-labelled answers, target 100 human-labelled | Y8 (human review), item 3.5 |
| 15 judge cases from one-tick snapshots | Item 3.10 |
| The judge runs at the global temperature (0.1), not 0 | Item 3.5 |
| No CI step for the judge (it needs a key and costs money) | Item 5.4 |
| The grey-zone classifier is still unmeasured | Item 3.5 |
| 3.7 not started. The conversation history endpoint needs a decision first: memory keeps questions, not answers, so a history screen means storing answers, with masking and retention | Decide, then 3.7 |
