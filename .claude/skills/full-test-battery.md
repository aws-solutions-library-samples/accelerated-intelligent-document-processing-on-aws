# Full Test Battery — GenAI IDP Accelerator

Use this skill when asked to "run the full battery of tests", validate a branch
before/after a merge, or gate a release. It runs every unit suite `make test`
covers, plus lint/typecheck, and — critically — tells you how to separate a **real
regression** from an **environment problem**, which is what nearly every surprising
failure in this repo turns out to be.

> This is a **read-only verification** skill. It does not deploy or commit. For the
> deploy step (publish + CloudFormation update) see `.claude/skills/infrastructure.md`
> and the "Deploy" note at the bottom.

## Environment setup (do this first — three gotchas)

1. **Install the pinned test extras BEFORE you believe any failure.** This is the
   single biggest source of wasted time on this repo:
   ```bash
   cd lib/idp_common_pkg && pip install -e ".[test]"   # what CI's test-unit-cicd does
   make install-first-party                            # from repo root, for the sibling packages
   ```
   A venv that predates the current `[test]` extra produces **~45 failures and ~17
   collection errors** that look exactly like environment-only "known" failures —
   stale `stickler-eval` (`No module named 'stickler.doc_split'`, `ImportError:
   cannot import name 'aggregate_from_comparisons'`), plus missing `z3`, `xlrd`,
   `sklearn` and `aws_xray_sdk`. They are not repo defects and there is nothing to
   fix in the tests. Check before diagnosing anything:
   ```bash
   python -c "import importlib.metadata as m; print(m.version('stickler-eval'))"  # must match the pin in pyproject [test]
   python -c "import z3, xlrd, sklearn, aws_xray_sdk; print('deps ok')"
   ```
   `idp_cli`'s suite additionally errors with `No module named 'idp_sdk'` until
   `make install-first-party` has run.
2. **Activate the repo venv** — it has all deps (pdfium, PIL, boto3, strands, …):
   ```bash
   source <checkout>/.venv/bin/activate     # e.g. lib/idp_common_pkg/.venv
   ```
   Bare `python3` may import a STALE `idp_common` from another checkout (this repo
   is commonly cloned side by side as `idp1`/`idp2`/`idp3`). When running
   `idp_common` tests directly, force the path:
   ```bash
   export PYTHONPATH=<checkout>/lib/idp_common_pkg
   ```
   Both CIs run **`python:3.13-bookworm`** (`.gitlab-ci.yml` `image:`, and the job
   container in `.github/workflows/developer-tests.yml` and `security-checks.yml`) —
   there is no 3.12/3.13 skew between them. A suite run under a system 3.12 is
   therefore not what CI measures, and "it passes in CI because CI is on a different
   Python" is a claim to check against those three files, not to assume.
3. **`publish.py` must run in a CLEAN env** — the venv on `PATH` breaks SAM's
   OpenSSL (`OPENSSL_3.4.0 not found` / `_sha2`). Build with:
   ```bash
   env -i HOME=$HOME PATH=/usr/local/bin:/usr/bin:/bin AWS_PROFILE=default bash -lc \
     'python3 publish.py <bucket-basename> idp us-west-2 --clean-build'
   ```

## The battery (mirrors `Makefile` `test:` + lint/typecheck)

Run from repo root with the venv active. `make test` runs it all in one go; the
per-suite commands below are for when you want isolated pass/fail (and to apply the
`-p no:cacheprovider` flag that avoids stale cache noise):

```bash
make test          # everything below, canonical
make lint          # ruff-lint + format + ARN check + buildspec + UI lint + codegen
make typecheck     # basedpyright
```

`make typecheck` fails with `make: basedpyright: No such file or directory` if the
tool is absent — it is not in the `[test]` extra. `pip install basedpyright` (CI
installs it via `npm install -g basedpyright`).

**Errors are the gate, and `develop` is at zero, so any error is yours.** That is the
durable half and it needs no comparison: a single error is a regression. The eleven
errors this page once told you to expect were resolvable-import failures, fixed when
`pyrightconfig.json` gained `extraPaths`
([#1109](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1109)).
Check also that `git ls-files '*.py' | wc -l` equals the `filesAnalyzed` the run
prints — the gate reads every tracked `.py` file and that identity is the property to
rely on, rather than any figure for how many there are.

⚠️ **The warning total is a standing set, and it is not stable enough to quote here.**
It has been measured at both 42 and 43 on different machines in the same week, and
nothing in the tree pins it — the set is `reportUnsupportedDunderAll` on several
`__init__.py` re-export lists plus a duplicate import, and which file a diagnostic
lands in shifts with the installed dependency set (`z3-solver` moves two of them
between `rule_validation/z3/__init__.py` and the validator itself). So a number
written here tells you less than a measurement, and it goes stale the way the error
count did. Measure `develop` on the machine you are judging from:

```bash
git worktree add -q /tmp/dev-typecheck github/develop && cd /tmp/dev-typecheck
make typecheck 2>&1 | tail -1     # the "N errors, M warnings, K notes" line
cd - && git worktree remove /tmp/dev-typecheck --force
```

Then compare **totals** against that, not against a literal, and check that no
diagnostic names a file your change touched.

Per-suite (isolated) — `PP=<checkout>/lib/idp_common_pkg`:

Counts are the 2026-09-11 baseline unless a row says otherwise. Two exceptions,
both re-measured later: the three pii-anonymizer rows at `28d2fc33e` (2026-09-18),
and the two chat rows at 2026-09-19.

| Suite | Command | Expected (all green) |
|-------|---------|----------------------|
| idp_common unit | `cd lib/idp_common_pkg && PYTHONPATH=$PP pytest tests/unit -q -p no:cacheprovider` | **5682 pass, 13 skip, 0 fail** (~2.5 min) |
| idp_cli | `cd lib/idp_cli_pkg && pytest -q` | 177 pass |
| idp_sdk | `cd lib/idp_sdk && pytest -m "not integration" -q` | 471 pass (~2.5 min) |
| idp_feature_sdk | `cd lib/idp_feature_sdk && pytest -q` | 140 pass (slow, ~75s) |
| feature platform | `cd feature-platform/main-stack-extensions && pytest -q` | 207 pass |
| pii-anonymizer feature API | `cd feature-platform/pii-anonymizer/feature-api && pytest tests -q` | 14 pass |
| pii-anonymizer hook | `cd feature-platform/pii-anonymizer/hook && pytest tests -q` | 17 pass |
| pii-anonymizer UI deployer | `cd feature-platform/pii-anonymizer/ui-deployer && pytest tests -q` | 10 pass |
| config library | `pytest config_library/test_config_library.py -q` | 114 pass |
| pipeline-hooks | `cd lib/idp_common_pkg && pytest tests/unit/lambdas/test_pipeline_hooks_dispatcher.py -q` | 6 pass |
| capacity Lambda | `cd src/lambda/calculate_capacity && pytest -q` | 33 pass |
| chat-with-document | `cd src/lambda/chat_with_document_processor && pytest tests -q` | 42 pass |
| chat-stream | `cd src/lambda/chat_stream_processor && pytest tests -q` | 32 pass |

## Reading a gate result, and not running the same suite twice

Two ways to mistake one measurement for another. They are together because they are
the same error in opposite directions: the first reads a non-result as a pass, the
second reads one result as two.

⚠️ **A gate result is a count of passed tests plus zero failures. It is not an exit
status.** The **long** suites — `idp_common` unit, `idp_sdk`, `scripts/tests`,
`test-packages-cicd` — outrun the assistant's 120-second Bash timeout and have to be
wrapped: `timeout N ... > log 2>&1` in the background, then the log is read. Several
rows in the table above finish in under four seconds and need none of that.

**Which wrapper you use decides whether the status means anything.** Measured:

| form | exit code when the command is killed |
|---|---|
| `timeout 1 sleep 5` | **124** |
| `timeout 1 sleep 5 > log 2>&1` | **124** |
| `timeout 1 sleep 5 2>&1 \| tail -1` | **0** |

So `timeout` reports a kill reliably, and **in the redirect-only form 124 is a usable
signal — check it.** The misleading 0 comes from the **pipeline**, which returns the
last command's status, so a trailing `| tail` throws the kill away. Prefer redirecting
to a log and reading the file; if you do pipe, the status tells you nothing and only
the summary line does.

Either way the log of a killed run *looks* exactly like a completed one — a column of
dots, no failure section, nothing obviously wrong. Measured: `pytest scripts/tests/`
killed by a 280-second `timeout` at **24%** of the way through, read through a pipe,
reported as exit 0.

The check that actually distinguishes them is the **summary line**:

```
3165 passed, 47 skipped in 505.86s      # a result
........................ [ 24%]        # not a result, whatever the shell said
```

So: read the totals, and treat a run that printed no summary line as not having run.
Budget for the **range** rather than one sample: `scripts/tests/` has been measured
between **5.8 and 11.6 minutes** on this machine (347 s on a quiet one, 696 s loaded),
and `test-packages-cicd` at about 12. A 600-second timeout is therefore too small for
either whenever anything else is running, which on a shared machine is most of the
time.

**`make test-packages-cicd` already runs `pytest scripts/tests/`** as one of its
closing steps. Running both repeats the whole of that suite, and — more misleading than
the cost — two passes of one suite read as independent evidence when they are a single
measurement. If you want a readable failure list, run `scripts/tests/` on its own with
`-q --tb=no -rf`; if you want the gate signal, it is already inside
`test-packages-cicd`. Do not run the two concurrently over one worktree either: two
pytest processes sharing a rootdir contend on its cache, which produces failures
belonging to neither run. (`-p no:cacheprovider` removes that particular contention;
the duplication argument stands regardless.)

Two invocation corrections for the CI-equivalent gates:

- **`make lint-cicd` needs `FORCE=1`** to exercise the UI lint and `npm run
  typecheck`. Without it, an unchanged `src/ui` checksum skips both while the target
  still runs the vite build and still reports success — so a green `lint-cicd` on a
  warm tree does not mean the UI was linted. CI is unaffected: `.checksum` is
  gitignored, so a fresh checkout has none and the lint always runs. See
  [#1152](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1152).
- **Do not reach for `PYTHONPATH` to make `make typecheck` resolve first-party
  imports, and do not conclude from that that the tool ignores the environment.**
  `pyrightconfig.json`'s `extraPaths` names the five first-party roots, so resolution
  is a property of the configuration rather than of the invocation. Measured on the
  current tree the two are identical — **0 errors, 42 warnings, exit 0** both with
  `PYTHONPATH` exported and under `env -u PYTHONPATH`.

  ⚠️ That equivalence is a **consequence of `extraPaths` being populated**, not a
  property of `basedpyright`, which does honour `PYTHONPATH`. Before those entries
  existed the gate resolved nothing first-party and so could not produce a diagnostic
  for any call into the shared library, while reporting zero errors over every file
  (#1109). So what matters is that `extraPaths` **stays** populated, which
  `scripts/tests/test_pyright_config.py` asserts — not which way you invoke the gate.

⚠️ **`PYTHONPATH` for pytest has to name EVERY first-party root, not just
`idp_common_pkg`.** The provenance guard (`scripts/tests/first_party_provenance.py`,
wired into several conftests) checks each first-party package independently, and on a
machine whose editable installs point at another checkout it refuses the run for
whichever one it finds there. Setting only `lib/idp_common_pkg` gets as far as
`idp_sdk resolves OUTSIDE the checkout under test` — which is the guard doing its job,
not a defect, but it is a *refusal* rather than a failure and has to be read as
"did not run":

```bash
W=$(pwd)   # from the worktree root
export PYTHONPATH=$W/lib/idp_common_pkg:$W/lib/idp_sdk:$W/lib/idp_cli_pkg:$W/lib/idp_mcp_connector_pkg:$W/lib/idp_feature_sdk
```

That list is the same set as `FIRST_PARTY_EDITABLES` in the `Makefile`; keep them
together.

## There is no standing failure set — green means green

**Expected standing failures: 0.** A correctly installed tree passes every suite
above with zero failures (verified on `develop` at `3101aeeb`, 2026-09-11; the three
pii-anonymizer rows added and re-verified at `28d2fc33e`, 2026-09-18). So treat
**any** failure as a real regression until you have proved otherwise.

The accepted-failure list is the table below, and it is empty. Keep the count in the
heading and the table in step — `scripts/tests/test_standing_failure_baseline.py`
fails if they disagree, and also fails if `docs/testing.md` or
`release-validation.md` still claim zero while this table is non-empty. That gate
exists because those three documents claimed zero for weeks while
`pii-anonymizer/feature-api/tests/test_handler.py::test_report_list_and_aggregate`
failed on any machine with an assume-role `AWS_PROFILE` (#974).

**`make test` compares this table against what it observed**, in both directions,
and fails the run on either asymmetry: a failure the table does not declare (a
regression, as before), or a declared row whose test passed (a waiver that has
outlived its cause — delete the row and lower the count). So the table is
load-bearing rather than advisory: while it is empty, any failure is red; a row in
it makes exactly that node id acceptable and nothing else. Two consequences worth
knowing before you write a row:

- **Write the Test cell as a pytest node id** — `path/to/test_x.py::test_name`,
  repo-relative, backticks optional. That is the form a run reports, so it is the
  only form the comparison can key on, and a row written as prose fails the gate
  rather than being silently ignored.
- **A run that produces no JUnit entry at all cannot be declared away**, and is
  reported separately: a crash, an internal pytest error, or a failure before
  anything was collected. Note where that boundary actually falls — a module that
  fails to **import** *does* get an entry, under a synthetic name derived from the
  module, so that failure is declarable like any other and a row naming that id
  makes the run green. The undeclarable case is only the one where nothing was
  collected.

Each root's results are written as JUnit XML under `test-reports/`, with a
`test-reports/run_all_tests.json` summary naming the observed, declared,
unexpected and resolved sets. Those are for reading; the verdict is decided in the
run that produced them, so there is no stale-results path through the check.

<!-- STANDING-FAILURES-BEGIN -->
| Suite | Test | Expected failure mode | Verified cause | Date |
|---|---|---|---|---|
<!-- STANDING-FAILURES-END -->

**Keep that table empty unless a failure is verified.** A stale allow-list is worse
than none: it invites you to wave through a genuine regression that happens to land
in a file it names. Two things to check before you add a row:

- Most apparent standing failures are an **environment artifact, not a repo state**
  — the symptom of a venv missing the pinned `[test]` extras (see gotcha 1). Reach
  for `pip install -e ".[test]"` before you reach for this section.
- Order-dependent pollution is a real phenomenon. If a test fails in the full
  run, re-run the file alone (`pytest <file> -q`) before concluding anything —
  but note that re-running the file alone is **not** sufficient to rule order out.
  `test_report_list_and_aggregate` (#974) reproduced perfectly with the file run
  alone, because what mattered was that it was the first DynamoDB test *in the file*
  to run: moto resets its backends when each `mock_aws` starts, so only the first one
  saw the polluted state. Deselect the failing test and watch whether the failure
  moves to its neighbour; if it does, the fault is ordinal, not in the test.

If you do find a genuine standing failure, add it to the table above **with the date
and the verified cause**, bump the count in that heading, and delete the row the
moment it stops reproducing.

## How to verify a suspected regression is real (not inherited)

Order matters — check the cheap explanation first:

1. **Read the error.** `ModuleNotFoundError` / `ImportError` on a third-party name
   (`stickler.*`, `z3`, `xlrd`, `sklearn`, `aws_xray_sdk`, `idp_sdk`) is an install
   problem, not a regression. Fix the environment (gotcha 1) and re-run.
   ⚠️ **A broken install does not always produce an import error.** Several Lambdas
   catch a missing `idp_common` deliberately and degrade — the config-preset handler
   logs `idp_common is unavailable (...)` at ERROR and skips the revision write — so
   the test fails on a plain assertion with no hint of the cause. Before anything
   else, confirm `python3 -c "import idp_common; print(idp_common.__file__)"` resolves
   **inside your own checkout**; an editable install here has been observed pointing
   at another agent's deleted `/tmp` checkout, which fails every suite that needs it
   while looking like a logic bug. Note this is **not** an interpreter-version
   effect, however much it looks like one when two interpreters on the same machine
   disagree: `test_apply_feature_config_preset.py` gives `2 failed, 18 passed`
   whenever `idp_common` is unimportable and `20 passed` whenever it is, identically
   under 3.12 and 3.13. What differs between interpreters is which editable install
   each one carries, not the language.
2. **Re-run the file alone** to rule out order-dependent pollution.
3. **Only then** compare against pristine `develop`:
   ```bash
   git worktree add -q /tmp/dev-check github/develop
   cd /tmp/dev-check/lib/idp_common_pkg
   pip install -e ".[test]"          # the worktree needs the deps too, or you prove nothing
   PYTHONPATH=/tmp/dev-check/lib/idp_common_pkg pytest <the failing test> -q -p no:cacheprovider
   cd - && git worktree remove /tmp/dev-check --force
   ```
   If it fails on pristine develop with a correct install → pre-existing, not yours.
   (Note `/tmp` worktrees may belong to a sibling checkout — verify the git dir
   before running git commands there.)

## Reporting

State totals per suite, then explicitly: "0 failures across N suites" — or name each
failure with its error and which of the three checks above you applied. Two things
never to do: report a raw failure count without saying whether the environment was
correctly installed (a stale venv reads as a broken repo), and describe a failure as
"pre-existing" without having reproduced it on `develop` **with the test extras
installed**.

## Deploy (separate step, when asked to update the stack)

Active test stack: **IDPUpgradeTest2**, region **us-west-2**, publish bucket
basename `idp-accelerator-artifacts-<ACCOUNT_ID>`. Resolve `<ACCOUNT_ID>` at run
time — `AWS_PROFILE=default aws sts get-caller-identity --query Account --output
text` — rather than carrying it in this file; the expected account is recorded in
the `idpagentic-deploy-target` memory.
Use `AWS_PROFILE=default` for all AWS calls (see `.claude/skills/live-eval-and-cost.md`
and the deploy-target memory). Build (clean env, above) → `aws cloudformation
update-stack` with `--parameters UsePreviousValue` → wait. Container-image Lambda
updates rebuild via CodeBuild, so a stack update commonly runs ~15-20 min; the
CLI `wait` may hit its 590s ceiling — re-issue it. PATTERNSTACK reaching
`UPDATE_COMPLETE_CLEANUP_IN_PROGRESS` means the function code is already live.
