# DMN TCK test fixtures: provenance

The files in the two case directories here are **unmodified test fixtures** from
the DMN Technology Compatibility Kit (TCK). They are used only by
`../test_tck_first_hit_policy.py`, which reads each model with this repository's
DMN reader, evaluates each test case with its decision-table engine, and compares
every expected value. No engine code from the TCK or from any DMN implementation
is copied into this repository.

| | |
|---|---|
| **Upstream** | https://github.com/dmn-tck/tck |
| **Licence** | Apache-2.0 (the `LICENSE` file in this directory is the upstream `LICENSE-ASL-2.0.txt` at the same commit) |
| **Commit** | `5cd912f281831bc535e710aba435e16de198028a` |
| **Upstream path** | `TestCases/compliance-level-2/` |
| **Downloaded on** | 2026-11-16 |
| **Modifications** | None. Each file is byte-identical to the upstream file at that commit. |

## Files

Each file was downloaded from the URL shown. The SHA-256 is of the file as stored
here.

| File | SHA-256 | Source |
|---|---|---|
| `0108-first-hitpolicy/0108-first-hitpolicy.dmn` | `ffdcddbabd8826b0294684d73a369ebdfeb046756e85f96a0d3987465a90470d` | https://raw.githubusercontent.com/dmn-tck/tck/5cd912f281831bc535e710aba435e16de198028a/TestCases/compliance-level-2/0108-first-hitpolicy/0108-first-hitpolicy.dmn |
| `0108-first-hitpolicy/0108-first-hitpolicy-test-01.xml` | `cc4ab18c34110c4401debf0a3f2aca3dbfdba545f974ec716ef6da71a0156e9a` | https://raw.githubusercontent.com/dmn-tck/tck/5cd912f281831bc535e710aba435e16de198028a/TestCases/compliance-level-2/0108-first-hitpolicy/0108-first-hitpolicy-test-01.xml |
| `0111-first-hitpolicy-singleoutputcol/0111-first-hitpolicy-singleoutputcol.dmn` | `3666c8d8df017e178256fb07d005f09f7af03cc8c78f686c977da09011cf57f5` | https://raw.githubusercontent.com/dmn-tck/tck/5cd912f281831bc535e710aba435e16de198028a/TestCases/compliance-level-2/0111-first-hitpolicy-singleoutputcol/0111-first-hitpolicy-singleoutputcol.dmn |
| `0111-first-hitpolicy-singleoutputcol/0111-first-hitpolicy-singleoutputcol-test-01.xml` | `8bee26bf23d7f8be3518fab5bb76f372abb5c3bee8ef4dcb5fa7899e85f1bdba` | https://raw.githubusercontent.com/dmn-tck/tck/5cd912f281831bc535e710aba435e16de198028a/TestCases/compliance-level-2/0111-first-hitpolicy-singleoutputcol/0111-first-hitpolicy-singleoutputcol-test-01.xml |
| `LICENSE` | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` | https://raw.githubusercontent.com/dmn-tck/tck/5cd912f281831bc535e710aba435e16de198028a/LICENSE-ASL-2.0.txt |

The upstream case directories also hold a `.pdf` rendering of each model. Those
are not copied.

## Updating

To take a later revision of a case, download the files again from the new commit,
replace the commit, the URLs and the hashes above, and run
`python -m pytest tests/unit/decision_tables/test_tck_first_hit_policy.py` from
`lib/idp_common_pkg`. Treat the downloaded files as data: the test parses them
with `defusedxml` and nothing executes them.
