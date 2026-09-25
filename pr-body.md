Fixes the seven defects reported in #1235, one commit each so any of them reverts alone.

## 1. `DocumentState` was half-unnamed, and the other half fell through to "queued"

`idp-cli`'s progress display bucketed documents with an `if/elif` chain that named eleven `DocumentState` members and sent the other twelve to `queued` by falling off the end. Two consequences a user acts on:

- `ABORTED` and `REDACTED_SUPERSEDED` are **terminal**, so a finished batch containing one counted zero failures — `status` printed "ALL COMPLETED" and exited 0 for a batch that had aborted its work, and a single aborted document reported "IN PROGRESS" and exit 2 forever, so `status --wait` never terminated.
- `PREPROCESSING` is set for *every* document whenever a preprocessing hook is registered, so on a PII-anonymization stack the running count read 0 for the whole run while everything was being processed. `OCR`, `STARTED`, `IN_PROGRESS`, `POSTPROCESSING` and `RULE_VALIDATION_POLICY_CLASSIFICATION` read as Queued the same way.

The four buckets are now **one authority** in `idp_sdk.models.base`, declared as a partition of `DocumentState` that the SDK's progress monitor and the CLI both read, so their counts cannot disagree about whether a state is terminal. Neither has a fallback branch. The bucket assignments preserve `ProgressMonitor._categorize_document`'s existing behaviour exactly; what changes is that its `else: running` default and the CLI's `else: queued` default are both gone.

**A new member fails loudly rather than being absorbed.** `document_state_partition_faults` is a pure function over a state set and a bucket mapping; an import-time check runs it over `set(DocumentState)` and raises `RuntimeError` naming any member in no bucket. The check is an explicit `if`, not an `assert`, because `assert` is stripped under `python -O`. `lib/idp_sdk/tests/unit/test_document_state_buckets.py` asserts the partition offline, parametrises over the enum rather than over a typed-out list, and measures the *future* case by driving the real fault detector with a state name the codebase does not define — the only way to observe what happens to a member an `Enum` cannot be extended to hold.

## 2. A mixed batch crashed the table

`doc.end_time or ""` passed a `datetime` through when present and substituted `str` when not, putting two types under one key. Both things the display layer does with that key fail on the mix rather than degrade: the recent-completions table sorts by it, so `status` printed `'<' not supported between instances of 'datetime.datetime' and 'str'` and exited 1, and `--monitor` abandoned the watch. `status --format json` on a single completed document with an end time raised `Object of type datetime is not JSON serializable` from the same cause — a second live consequence, found while fixing this one.

Both timestamps are rendered as ISO 8601 in every case. Lexicographic order over ISO 8601 is chronological order, so the sort still means what it was written to mean. `create_recent_completions_table`'s key is coerced as well, so a caller that builds `status_data` by hand cannot crash it either.

## 3. `test-compare` can now show configuration differences

`configs` was the literal `[]` with a `TODO`, so the difference table was dead code and every comparison printed "No configuration differences to display" — including for two runs on different models, which is the most useful thing to know when two runs score differently, and a claim about the configurations that nothing had checked.

**Decided in favour of populating it rather than saying "not implemented", because the data needed no new call.** Each run records the configuration it ran under, and `getTestRun` — which the SDK already invokes once per run — returns it under `config`; the processor was discarding it when narrowing to its eleven-key metrics projection. So `compare_test_runs` diffs those and `TestComparisonResult` carries the result. No new resolver field, no new IAM, and the diff is a pure function that is fully testable offline.

Three outcomes stay distinct rather than collapsing into one empty table:

| `configs` | Message |
|---|---|
| non-empty list | the difference table, with each run's value and `<missing>` where a run has no such setting |
| `[]` | "Configurations are identical across the compared runs" |
| `None` | "Configurations not compared: fewer than two of these runs recorded the configuration they ran under" |

`None` and `[]` are different answers and the model keeps them apart. Save timestamps and class definitions are excluded from the comparison — the same set the Test Studio comparison view in the web UI hides, so the two agree about what "no differences" means.

## 4. The missing-dependency remedy did not fix the problem — and two more of the same

Rich's markup parser treats any `[...]` as a tag and drops one it cannot resolve as a style, with no error and no warning. `idp-cli chat` without the optional agents dependencies therefore told the user to run `pip install -e 'lib/idp_common_pkg'`, which fixes nothing: `idp_common` is already installed in the situation that produces the message and the missing piece is the `[agents]` extra, so the user runs it, watches it succeed, retries and gets the identical error.

Scanning for the class found two more, both confirmed by rendering them through Rich: `config-upload`'s "this will update the default `[system default]` config profile" warning was losing the profile name, and the test-set overwrite prompt was losing its `[y/N]`, leaving a question with no answers offered. All three escape the bracket now.

A new offline check walks the package's `console.print` calls and asks **Rich itself** — `Style.parse`, the same code the renderer uses — whether each bracketed run resolves as a style. Asking Rich rather than comparing against a list of style names is what keeps it from going stale as Rich's styles change.

## 5. Two metering-count defects in `search_tracking_table.py`

"Documents with metering" tested `any(metering_data.values())` — the accumulator, not the document in hand — so once one document had produced a reading, every later document carrying a `Metering` attribute was counted whether it contributed or not, and the figure was order-dependent: the same documents gave a different count depending on which arrived first.

Separately, a `<Stage>/lambda/duration` entry stored as a bare number raised `AttributeError` into the timestamp handler's broad `except`, which incremented `missing_data_count` for a document that had already incremented `valid_count`. One document appeared in both totals, so they no longer added up to the search count, and every later stage for that document was abandoned, dropping readings that were fine.

The count is per document now; the metering parse sits outside the timestamp handler, because a reading this code cannot read says nothing about whether the timestamps were there; and an unreadable reading is skipped with a warning naming the document and stage rather than dropped at debug level.

## 6. Dead guard after `split(",")` — at both sites

`str.split` never returns an empty list. `"".split(",")` is `[""]`, so `abort-test-run`'s `if not test_run_id_list` guard and its "No test run IDs provided" message were unreachable code and `--test-run-ids ""` went on to request an abort of a run whose id is the empty string. `"run-a,".split(",")` is `["run-a", ""]`, so `test-compare`'s "at least 2 test run IDs" check passed with one real id and then rendered a blank column of `N/A` as a successful comparison with exit 0.

One helper parses the option for both commands, dropping blank segments, which is what makes the two existing guards mean what they say rather than adding a third check beside them. The issue named only the `abort-test-run` site; fixing it alone would have left its twin three hundred lines away with the identical mechanism. The six other `split(",")` call sites in `cli.py` parse different options (document ids, file types, regions, features) with different semantics and are deliberately untouched.

## 7. `initiated_only` was unsatisfiable

`delete` told the two outcomes apart with `not result.success and result.status == "INITIATED"`, but `StackOperation.delete` computes `success = result.get("success", status == "INITIATED")` and the underlying no-wait path sets no `success` key, so an initiated deletion arrives as `success=True, status="INITIATED"` and that conjunction could never hold. Without `--wait` the user was told "✓ Stack deleted successfully!" with "Status: INITIATED" as the only hint, then a note about a retained logging bucket that reads as the post-mortem of a finished deletion, and never saw the branch naming the console path and the exact `--force --wait` command — which was unreachable.

The status decides now and is checked first, so the branch is reachable from the input a user actually produces, and the retained-bucket note waits for a completed deletion. The test that used to reach the branch by hand is parametrised over both spellings of `success`, since building only `success=False` — as it did — passes against a condition no user input can satisfy.

## Reachability, for the two dead-guard items

Items 6 and 7 are unreachable branches, so there was no behaviour to invert in the "before" direction and a green suite after removing the dead condition would prove nothing. The safety argument is the analysis:

- **Item 6.** `str.split` returns at least one element for every input, and every element of `"".split(",")`, `",".split(",")`, `"  ".split(",")` and `" , ".split(",")` is falsy-or-blank while the *list* is truthy. So `if not <list>` after a bare split is satisfiable by no input at all. Measured directly rather than asserted.
- **Item 7.** The only two producers of a `DELETE` result are the no-wait path (`status="INITIATED"`, no `success` key, so `success` defaults to `True`) and `_wait_for_deletion` (`DELETE_COMPLETE`/`success=True` or `DELETE_FAILED`/`success=False`). Neither can produce `success=False` with `status="INITIATED"`.

Where a dead branch was made *live* — item 7's console-path guidance — the new behaviour is asserted through the real no-wait path under moto, and reverting the reorder reddens it.

## Verification

Every production change in this PR was reverted and a test observed to go red; 19 mutations, all measured. One came back **green on the first attempt** — dropping `configs=result.get("configs")` from the SDK operation layer, which the processor's and the CLI's own tests both missed — and that gap is closed by `test_the_configuration_differences_reach_the_result_model`, after which the same mutation reddens.

Gates on the merge result:

| Gate | Result |
|---|---|
| `PYTEST_PARALLEL="-n 4" SKIP_INSTALL=1 make test-packages-cicd` | exit 0 |
| `pytest lib/idp_cli_pkg/tests` | 1245 passed (base: 1201) |
| `pytest lib/idp_sdk/tests/unit -n 4` | 2643 passed (base: 2559) |
| `ruff check .` / `ruff format --check .` | clean / 1268 files formatted |
| `scripts/check_lint_debt.py` | exit 0 |
| `make typecheck` (whole tree) | 0 errors, 44 warnings |
| `make check-markdown-links` | exit 0, 349 files |

`github/backlog/staging` was merged in immediately before opening this PR and the full battery re-run on the merge result. The 11 `lib/idp_sdk/tests/integration` failures present at the base SHA are unchanged and require AWS credentials.
