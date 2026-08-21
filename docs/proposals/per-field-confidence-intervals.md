# Proposal: per-field sample size and margin of error in Test Results

## The problem

Test Results shows per-field accuracy as a bare point estimate. A reader cannot tell
100% measured on 3 observations from 100% measured on 300, and the two justify completely
different decisions.

This is the failure mode a golden dataset exists to prevent, one level down. A run's
*overall* accuracy firms up quickly — within roughly the first 100 documents — because
every document contributes to it. A *single field's* accuracy does not, because a field
appearing once per document yields one observation per document. So a field that is
badly broken can sit inside a healthy-looking overall score, and today nothing on the
screen shows that the field's number is built on too little evidence to act on.

Concretely, at a measured 90% accuracy:

| Observations for the field | 95% margin of error |
|---|---|
| 20 | ±13.7 pts |
| 100 | ±6.0 pts |
| 300 | ±3.4 pts |
| 500 | ±2.6 pts |

A field at "90%" on 20 observations lies between 69.9% and 97.2% — the Wilson
bounds. (Subtracting the margin would say 77%, which is the normal-approximation
figure this proposal argues against; the interval is asymmetric near the ends.) Customers are
currently computing this in spreadsheets alongside our reports, which means we hand them
a number and they do the statistics we already have the data for.

## What to add

Two columns in the Test Results per-field table, and the same two values on the API:

- **`observations`** — how many comparisons produced this field's accuracy.
- **`accuracyMarginPct`** — half-width of the 95% interval, in percentage points, plus
  `accuracyLowPct` / `accuracyHighPct` for the bounds.

Rendered as `90.0% ±6.0 (n=100)`, with the interval in the cell popover. Sort remains on
accuracy; a field whose interval is wider than a configurable threshold gets a subdued
"low evidence" hint rather than a warning colour — this is a precision statement, not a
failure.

## Why it is cheap

The counts already exist. `field_metrics` entries carry `tp` / `fp` / `fn` / `tn`
(`lib/idp_common_pkg/idp_common/evaluation/service.py`), from which the accuracy
denominator is `tp + fp + fn + tn`. `cm_accuracy` is already computed from them. Nothing
new needs to be measured, stored, or re-run — this is arithmetic over data we already
persist and already ship to the UI.

## Interval choice: Wilson, not normal

Use the Wilson score interval, not the textbook normal approximation.

The normal approximation is what produces bounds above 100% at small n — the table above
would print `[76.9%, 103.2%]` for n=20 — which is exactly the case this feature exists to
flag, so it must not be the case where our arithmetic looks broken. Wilson stays inside
[0, 1], behaves at p=0 and p=1 (both common per-field results), and needs no special
cases.

```python
def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion.

    Preferred over the normal approximation because per-field results routinely sit at
    p=0 or p=1 with small n, where the normal interval leaves [0, 1] entirely.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))
```

Belongs in `idp_common/evaluation/` next to the confidence-curve maths, with unit tests
covering n=0, p=0, p=1, and the four rows of the table above.

## What this deliberately does not claim

The interval describes **sampling** uncertainty only: how much this field's accuracy
could move if you scored a different sample of the same size from the same population. It
does not account for:

- **Label error.** If the golden labels are themselves wrong some of the time, the true
  accuracy is outside this interval and no amount of sample size fixes it. Worth one
  sentence in the UI copy so the number isn't over-read.
- **Non-representative documents.** A set that doesn't look like production has a tight
  interval around the wrong number.
- **Correlated observations.** Fields appearing many times per document (table rows) have
  observations that are not independent — 300 line items from 10 documents carry less
  information than 300 from 300 documents. The interval will read tighter than it should
  for those fields. Options, in increasing cost: note the caveat in the popover; report
  the document count alongside the observation count so the ratio is visible; or apply a
  cluster correction. **Recommend reporting both counts** — it makes the limitation
  visible without asserting a correction we haven't validated.

## Natural follow-on (not in this proposal)

The inverse question — "how many documents must I collect for ±3 points per field?" — is
the same formula solved for n, and it is the one question customers currently answer by
hand before they ever reach our tooling. Worth doing next, but it is a planning feature
rather than a reporting one, and it belongs wherever set creation is documented rather
than in the results table.

## Scope

- `idp_common/evaluation/` — the interval helper plus tests.
- The aggregation Lambda — emit `observations` and the interval per field alongside
  `cm_accuracy`.
- `TestRun` GraphQL field metrics — three new optional numbers.
- `TestResults.tsx` — two columns, popover copy, low-evidence hint.
- `docs/test-studio.md` §Field-Level Metrics — explain the interval and the caveats above.

No migration. Runs that predate the change simply omit the new values, and the columns
render em-dashes rather than implying `n=0`.
