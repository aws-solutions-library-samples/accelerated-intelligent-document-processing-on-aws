# Enforcement A/B — what coercion and validation actually change

Measured on the live IDP1 stack (us-west-2, account 912625584728), 2026-08-31.
Two experiments; the second exists because the first was uninformative and the
reason it was uninformative is itself the finding.

## Experiment 1 — benchmark `enforcement` suite

`enforce-off` vs `enforce-warn` on one stack with byte-identical code, so a delta
is attributable to the feature. 2 cells x 2 synthetic docs.

| cell | success | recall | accuracy | cost | wall |
|---|---|---|---|---|---|
| enforce-off  | 2/2 | 1.0 | 0.75 | $0.0954 | 52.1s |
| enforce-warn | 2/2 | 1.0 | 0.75 | $0.0880 | 33.5s |

Accuracy and recall **identical**. Cost/latency are **not reportable** at n=2 —
the harness warns cost CV ~0.96-0.99, i.e. the two documents differ from each
other far more than the arms do.

The run was NOT vacuous — verified after an initial probe wrongly reported zero
activity (it was reading the wrong tracking key):

- `enforce-warn` applied **81** coercions, all `date_normalized`
- and **26** refusals: 24 `ambiguous_date`, 2 `type_family_mismatch`
- 4/4 sections validated; one document failed validation under `warn` and still
  completed, as designed
- `enforce-off` shows 0 of everything

So coercion fired heavily and moved accuracy by zero. The suite's date fields use
the `DATE` comparator, which is **format-tolerant** — it scores `03/15/2024` and
`2024-03-15` as equal. Measuring coercion by that metric measures the wrong thing.

## Experiment 2 — purpose-built, format-STRICT

A single-page invoice engineered to contain every coercion case, scored by the
pipeline's own evaluation against a typed ground-truth baseline, with
`EXACT` / `NUMERIC_EXACT` comparators so format differences cannot be absorbed:

| document text | field type | ground truth |
|---|---|---|
| `Amount Due: $1,234.00` | `number` | `1234.00` |
| `Tax Amount: 1.234,56` (European) | `number` | `1234.56` |
| `Discount: 12.5%` | `number` | `12.5` |
| `Invoice Date: 03/15/2024` | `format: date` | `2024-03-15` |
| `Paid In Full: Yes` | `boolean` | `true` |
| `Shipment Date: 03/04/1985` (AMBIGUOUS) | `format: date` | `1985-04-03` |

Run with coercion off vs on, on **two model tiers**:

| model | coercion | eval accuracy | coercions applied |
|---|---|---|---|
| `claude-sonnet-4-6` | off | 0.8571 (6/7) | — |
| `claude-sonnet-4-6` | **on** | 0.8571 (6/7) | **0 — metadata ABSENT** |
| `nova-lite-v1:0` | off | 6/7 exact | — |
| `nova-lite-v1:0` | **on** | 6/7 exact | **0 — metadata ABSENT** |

**Both models already returned every value correctly typed** — `1234.0` as a
float from `$1,234.00`, `1234.56` from the European `1.234,56`, `12.5` from
`12.5%`, `true` from `Yes`, ISO from `03/15/2024`. Coercion never saw a string
to repair, so it fired zero times and the arms are identical for a second,
different reason than in experiment 1.

## Conclusions

1. **Coercion is a safety net, not an accuracy improver.** On well-formed model
   output it is a no-op. Do not expect it to move an accuracy metric.
2. **It fires on long repetitive list rows, not on scalar header fields** — 81
   coercions across 100-row transaction lists vs 0 on a 7-field invoice. Model
   output drifts at scale; it is reliable on a handful of scalars.
3. **Its value accrues to consumers that are NOT format-tolerant** — Athena
   column typing, rule validation, the public SDK's `fields` contract, HITL
   display — rather than to the evaluator, which already tolerates the
   difference.
4. **The ambiguous-date protection is narrower than it looks.** Both models
   resolved `03/04/1985` to `1985-03-04` themselves and emitted ISO, so coercion
   had nothing to refuse and nothing was flagged. The guarantee is "coercion
   never guesses", not "the pipeline never guesses". Filed as #717.

## Reproducing

Experiment 1: `make_configs.py --suite enforcement` then
`run_matrix.py --stack IDP1 --suite enforcement`, then `aggregate.py`.
Experiment 2 was a targeted e2e (document generator + typed baseline + two
config profiles); the harness has no format-strict scalar class, which is why it
was done outside the matrix.
