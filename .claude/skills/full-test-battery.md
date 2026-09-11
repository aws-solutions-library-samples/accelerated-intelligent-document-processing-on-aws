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
installs it via `npm install -g basedpyright`). Compare its output against the
baseline on `develop` rather than reading it absolutely: it reports **4 errors / 51
warnings** on a clean tree (2026-09-11). Note **which files those errors land in
shifts with the installed dependency set** — with `z3-solver` present they sit in
`rule_validation/z3/z3_validator.py` and `calculate_capacity/index.py`, without it
they move — so compare the **totals**, and check that no diagnostic names a file
your change touched.

Per-suite (isolated) — `PP=<checkout>/lib/idp_common_pkg`:

| Suite | Command | Expected (2026-09-11 baseline, all green) |
|-------|---------|-------------------------------------------|
| idp_common unit | `cd lib/idp_common_pkg && PYTHONPATH=$PP pytest tests/unit -q -p no:cacheprovider` | **5682 pass, 13 skip, 0 fail** (~2.5 min) |
| idp_cli | `cd lib/idp_cli_pkg && pytest -q` | 177 pass |
| idp_sdk | `cd lib/idp_sdk && pytest -m "not integration" -q` | 471 pass (~2.5 min) |
| idp_feature_sdk | `cd lib/idp_feature_sdk && pytest -q` | 140 pass (slow, ~75s) |
| feature platform | `cd feature-platform/main-stack-extensions && pytest -q` | 207 pass |
| pii-anonymizer hook | `cd feature-platform/pii-anonymizer/hook && pytest tests -q` | 17 pass |
| config library | `pytest config_library/test_config_library.py -q` | 114 pass |
| pipeline-hooks | `cd lib/idp_common_pkg && pytest tests/unit/lambdas/test_pipeline_hooks_dispatcher.py -q` | 6 pass |
| capacity Lambda | `cd src/lambda/calculate_capacity && pytest -q` | 33 pass |
| chat-with-document | `cd src/lambda/chat_with_document_processor && pytest tests -q` | 17 pass |
| chat-stream | `cd src/lambda/chat_stream_processor && pytest tests -q` | 6 pass |

## There is no standing failure set — green means green

**A correctly installed tree passes every suite above with zero failures**
(verified on `develop` at `3101aeeb`, 2026-09-11). So treat **any** failure as a
real regression until you have proved otherwise.

This section previously listed ~26 "known pre-existing failures — DO NOT treat as
regressions", covering `test_configuration_sync.py`, `test_embedding_service.py`,
`test_discovery_agent.py`, `test_publish.py`, `test_assessment_enabled_property.py`,
`test_pdf_page_extraction.py`, `test_document_compression.py` and
`workflow_tracker/test_notify_circuit_breaker.py`. **All of them now pass**, and the
list has been removed rather than trimmed, because a stale allow-list is worse than
none: it invites you to wave through a genuine regression that happens to land in a
file it names. Two lessons worth keeping:

- Most of that list was an **environment artifact, not a repo state** — the symptom
  of a venv missing the pinned `[test]` extras (see gotcha 1). Reach for
  `pip install -e ".[test]"` before you reach for this section.
- Order-dependent pollution is still a real phenomenon. If a test fails in the full
  run, re-run the file alone (`pytest <file> -q`) before concluding anything.

If you do find a genuine standing failure, add it here **with the date and the
verified cause** — and delete it the moment it stops reproducing.

## How to verify a suspected regression is real (not inherited)

Order matters — check the cheap explanation first:

1. **Read the error.** `ModuleNotFoundError` / `ImportError` on a third-party name
   (`stickler.*`, `z3`, `xlrd`, `sklearn`, `aws_xray_sdk`, `idp_sdk`) is an install
   problem, not a regression. Fix the environment (gotcha 1) and re-run.
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

Active test stack: **IDPUpgradeTest2**, account **912625584728**, region
**us-west-2**, publish bucket basename `idp-accelerator-artifacts-912625584728`.
Use `AWS_PROFILE=default` for all AWS calls (see `.claude/skills/live-eval-and-cost.md`
and the deploy-target memory). Build (clean env, above) → `aws cloudformation
update-stack` with `--parameters UsePreviousValue` → wait. Container-image Lambda
updates rebuild via CodeBuild, so a stack update commonly runs ~15-20 min; the
CLI `wait` may hit its 590s ceiling — re-issue it. PATTERNSTACK reaching
`UPDATE_COMPLETE_CLEANUP_IN_PROGRESS` means the function code is already live.
