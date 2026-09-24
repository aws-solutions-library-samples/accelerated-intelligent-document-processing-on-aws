# Repo Quality Review — the whole-repository assessment, repeatable

Use this skill when the user asks for a **holistic review of the repository** rather
than of a change — "review the whole repo", "how healthy is this codebase?", "do a
quality assessment", "run the repo quality review again". It is periodic quality
assurance, not a gate: nothing in CI runs it, and it is meant to be re-run every
release cycle or two so the findings are a trend rather than a one-off.

It is distinct from the two change-scoped review skills. `code-review.md` is the
pre-commit checklist for your own diff; `pr-review.md` reviews someone else's PR at a
URL. This skill reviews the **tree as it stands** — every template, every gate, every
doc — and its output is a ranked findings list, not a merge verdict.

> **Why it exists.** The first pass of this review (2026-09, v0.6.9.dev3) found that
> its most consequential findings were not individual bugs but two *shapes* of bug
> that recur across unrelated subsystems, and that no existing gate detects either.
> Both are mechanically searchable. Sections **Class 1** and **Class 2** below are
> those searches. If you run this skill and only produce a list of nits, you have not
> run it — go back to those two sections.

## Hard constraint — read-only by construction

State this back to the user before starting, then hold to it:

- **No file writes** outside a scratch path and the report itself. Use
  `scratch/repo-review-<date>/` for working notes and write the report where the user
  asks (or to `scratch/` and paste it). Do not edit source, templates, configs or
  docs, even to fix something obvious.
- **No AWS write APIs.** Read-only or least-privilege credentials only
  (`AWS_PROFILE=default` is the deployment account — treat it as production). Prefer
  `describe`/`list`/`get`. Do not create, update, tag, or delete anything. Every
  measurement in this skill is **offline** and needs no credentials at all; live AWS
  is optional colour, not required evidence.
- **Never run `publish.py`**, `make srt-setup`, `make setup`, `npm install`, or
  anything that mutates the working tree or a bucket. `publish.py` writes to S3.
- **No PR, issue, branch or ruleset mutations.** `gh`/`glab` reads only (`view`,
  `list`, `api` with GET). Do not open, label, comment on, close or merge anything —
  even the issues this review's findings deserve. Hand the user the list; they file.
- **No branch pushes**, no commits, no `git stash`.

The review reports; it does not remediate. A finding that takes one line to fix is
still a finding, not a fix. (If the user afterwards asks you to fix things, that is a
new task under `code-review.md`, and each fix gets its own branch and PR.)

## Inputs

| Input | Where it comes from |
|---|---|
| Tree under review | the current checkout. Record `git rev-parse --short HEAD` and `cat VERSION` in the report — a finding without a commit is unreproducible |
| Previous report | the last run's report, if the user has one. Diffing against it is where the trend lives (see **Reporting**) |
| Known non-defects | the list in this file. Read it **before** fanning out, so no reviewer spends a pass re-deriving a withdrawn finding |

Nothing else needs asking. Do not ask which dimensions to cover — all ten are the
deliverable.

## How to run it: fan out one reviewer per dimension, then consolidate

This review is fanned out. One subagent per dimension, each with the dimension's
evidence list and the read-only constraint, all reporting `file:line` findings; then
**you** consolidate.

> **This skill fans out to subagents, so it needs the Agent tool — do not assume it is
> available to you.** Tell the user at the start that the skill needs it and ask them
> to authorize subagents explicitly when they invoke it — e.g. *"run the
> repo-quality-review skill; you may use subagents"* — and do not spawn one before
> they have. If they decline, or say nothing either way, run single-agent and say
> plainly in the report that the review ran that way and that the cross-dimension
> consolidation step (below) is therefore weaker.

**The consolidation step is not a formatting step — it is where the real findings
appear.** In the 2026-09 pass, six of the ten independent reviewers each reported an
instance of Class 1 without any coordination: the observability reviewer found the
unsubscribed alarm topic, the CI reviewer found gates that block nothing, the
security reviewer found the missing DynamoDB index, the code-quality reviewer found
the typecheck `include` pointing at a non-existent path, and so on. Each looked like
a small local defect in its own dimension. Only side by side did the *shape* become
the finding — and the shape is what generalizes into a search. A single-agent pass
tends to report those as six unrelated nits and never abstracts them, because it
never sees them as six.

So the procedure is:

1. **Read the known-non-defects list** and this file's two Class sections yourself
   first, so you brief every reviewer with them.
2. **Fan out ten reviewers**, one per dimension in the table below. Give each: the
   dimension's evidence list, the read-only constraint verbatim, the output contract,
   and the instruction to run the measurement commands relevant to it rather than
   eyeballing.
3. **Run the baseline measurements yourself** (they are cheap and shared) so every
   reviewer argues against the same numbers.
4. **Consolidate.** Lay every finding out in one list and ask explicitly: *which of
   these are the same shape?* Then run the Class 1 and Class 2 searches over the whole
   tree to find the instances the reviewers missed. Promote the shape to a top-level
   finding and demote the instances to examples under it.
5. **Rank by consequence** and write the report.

### What a run costs, roughly

Ten subagents across thirteen measurements is not cheap, so price it before starting.
Treat the figures below as **order-of-magnitude and `inferred`**, because the basis is
thin and stating it precisely would be false precision:

- **Tokens: single-digit millions for a fanned-out run.** The only hard datum in hand is
  a single narrowly-scoped subagent — two facts to verify, 14 tool calls — which cost
  **~78k tokens**. A dimension reviewer is materially broader than that: it greps the
  whole tree, reads templates, and runs its measurement commands. Ten of those plus a
  consolidation pass that has to hold every finding in context at once lands in the
  1–5M range. A single-agent run is cheaper, perhaps 300–600k, and buys a weaker review
  (see the consolidation note above) — that is the actual trade, not speed.
- **Wall clock: under an hour fanned out, because the reviewers run in parallel.** The
  long pole is consolidation, which is serial and cannot start until the last reviewer
  lands. The baseline measurements themselves are seconds to a few minutes each; the
  Class 1 and Class 2 tree-wide searches are the slowest.

Record what the run actually cost in the report. Two or three real numbers replace this
estimate with something worth having, and nobody has recorded one yet.

## The ten dimensions

Each row is mandatory. A dimension you could not assess is reported as **NOT
ASSESSED with the reason**, never dropped.

| # | Dimension | Evidence to gather, and where |
|---|---|---|
| 1 | **Architecture & design** | Resource count per stack vs the 500-resource CloudFormation limit (measurement A) — `template.yaml` is the one to watch. Nested-stack boundaries in `nested/` and `patterns/unified/`; whether `nested/api-resolvers/` is still cohesive or a dumping ground. Coupling: which Lambdas import `idp_common` vs vendor a copy (`**/vendored/`, `**/vendor/`). Whether the unified `use_bda` flag genuinely unified the two modes or just co-located them (`patterns/unified/statemachine/workflow.asl.json` — the BDA branch, the pipeline branch, the shared tail) |
| 2 | **Security** | IAM wildcard census (measurement E) and whether each `Resource: "*"` carries a `reason:` in cfn-nag/checkov metadata. Authorization decision points: `lib/idp_common_pkg/idp_common/config_scope.py` is the canonical fail-closed contract — find every caller and check each honours it. `scripts/tests/test_iam_privilege_escalation.py` and `scripts/tests/test_config_revision_read_grants.py` are the existing structural gates; read what they *don't* cover. Log redaction (Class 2 worked example). SRT suppressions in `scripts/srt/issues.json` and dep-audit triage in `scripts/security/dep_audit_allowlist.json` — each needs a specific justification, not a bulk waiver |
| 3 | **Test strategy & coverage** | Registered vs quarantined test roots (measurement C) and what each quarantine reason costs. Which suites CI actually runs vs which only `make test` runs (measurement G) — a suite outside both `test-cicd` and `test-packages-cicd` runs on no PR. Structural gates in `scripts/tests/` are this repo's strongest asset; inventory them and note which enumerate from the source and which carry a hardcoded list (Class 2). `docs/testing.md` is the published per-method map; check it against reality |
| 4 | **Observability** | Alarm inventory and action wiring (measurement D) — an alarm whose topic has no subscriber is decoration. Lambda-to-LogGroup ratio per template (measurement D2) and whether the gaps are the deliberate custom-resource-only ones `scripts/tests/test_lambda_log_groups.py` enforces. Metric namespace consistency (`scripts/tests/test_metric_namespace_alignment.py`). X-Ray annotation correctness (Class 2 worked example). Whether a failure that matters is *visible*: dead-letter queues with no alarm, `logger.error` on a fail-open path with no metric |
| 5 | **Code quality** | Lint and typecheck coverage (measurement B) — the *coverage* number matters more than the finding count, because an excluded path reports zero. `ruff.toml` skips a named per-file list rather than any directory, and `pyrightconfig.json` `include` covers every tree holding tracked Python, so the coverage question is now "has the debt list grown?" rather than "which trees are dark?" — `python3 scripts/check_lint_debt.py --summary` answers it. Largest files (`find . -name '*.py' | xargs wc -l | sort -rn | head`) — a 6,000-line module that no gate covers is the worst combination. Duplication: identical helper defined in N Lambdas |
| 6 | **CI/CD & automation** | Gate inventory (measurement G): which `make` targets exist, which run on GitHub, which on GitLab, which are advisory (`allow_failure`, `continue-on-error`), and — the one people skip — which are actually **required** on `develop` (measurement G2). `scripts/tests/test_ci_gate_parity.py` enforces GitHub/GitLab symmetry; it cannot enforce branch protection, so check that separately. Workflow triggers: GitHub is `pull_request`-only, so a direct push to `develop` runs nothing there |
| 7 | **Documentation** | Doc-to-template drift, both directions (measurement F): a service shipped and documented nowhere, and a service documented that no template declares. `docs/aws-services-and-roles.md` is the one that must match IAM reality. Both doc tiers per `.claude/skills/documentation.md` — `docs/*.md` and `lib/idp_common_pkg/**/README.md`. Frontmatter/licence header conformance. `CHANGELOG.md` `[Unreleased]` shape. Skill-file inventory vs the `CLAUDE.md` table (measurement F2) |
| 8 | **Frontend / UI** | `src/ui/src` test-file-to-source ratio (measurement H). Cloudscape-only component use; no stray `console.log`; `DOMPurify` on every `dangerouslySetInnerHTML`. Generated GraphQL types in sync (`src/ui/src/graphql/generated/`). Accessibility on new surfaces. Bundle/dependency posture: `src/ui/.npmrc` supply-chain keys and whether the pinned npm honours them (Class 1 worked example) |
| 9 | **Empirical rigor & innovation** | `benchmarks/` — whether claims in `docs/benchmarking/` and `benchmarks/paper/` are backed by a re-runnable matrix with exact ground truth, and whether the metric definitions can hide a regression (recall that counts rows rather than cells scores a fully-null column as perfect). `docs/release-validation/` records: does each tier state measured vs inferred? Cost/accuracy claims: is there a baseline and an A/B, or a single run? |
| 10 | **Engineering-practice signals** | Ownership and bus factor (measurement I) — commit share by author, and whether any subsystem has exactly one author. Issue hygiene (measurement I2): open count, unlabelled count, age distribution, whether issues this review would file already exist. Contribution surface: `CONTRIBUTING.md`, `.github/ISSUE_TEMPLATE/`, presence of a PR template and `CODEOWNERS`, and whether a first-time contributor can run the gates locally from the README alone |

## Baseline measurements

Run these yourself, before and independently of the fan-out, so every reviewer
argues against the same numbers. All are **offline** — no AWS, no network except the
`gh` reads in G2 and I2. Every command below was executed in this repo; every
"last measured" figure was taken at `bbf0dc9c9` / `VERSION 0.6.10.dev3` on
**2026-09-21**, so the whole column is reproducible by checking out that one commit.
Re-measure rather than trusting those figures; the point
of recording them is that a number that moved a lot is itself a finding. When you
re-record, replace the commit label too, and check every figure was actually taken at
the commit you name — a column that mixes states under one label is worthless, because
a reader can no longer treat a difference as signal.

⚠️ **Three of these are not properties of the commit at all**, so the label above does
not bound them. G2 reads a live *repository setting* and I2 reads the live issue and
pull-request lists — both move without any commit, so record a timestamp with them and
do not treat a difference from the last run as a change in the tree. And
**basedpyright's warning count is a property of the working tree**, not of the commit:
in a clean worktree it analyses every tracked `.py` file and reports 91 warnings, and
in a populated checkout — one carrying staged library copies, a `.aws-sam/` build tree
or an editable install resolving elsewhere — the same commit reports 173. Say which
kind of checkout you measured in. The **error** count is the number that must stay at zero and it is
stable across both.

The measurements assume an **existing** dev environment with `ruff` on `PATH`
(measurement B is the only one that needs a binary the shell does not already have —
it resolves from the project `.venv` that `make setup`/`make dev` creates, which the
read-only constraint forbids you from running). Everything else the skill uses
(`python3`, `gh`, `jq`, `comm`, `awk`, `find`, `sed`, `git`) is ambient.

**If you are working in a throwaway git worktree — which `pr-review.md` recommends for
read-only work, and which is the natural way to run this review — `ruff` will not be on
`PATH`.** A worktree has no `.venv` of its own; the virtualenv lives in the main
checkout, so measurement B dies with `command not found`, and B is the measurement this
skill argues matters more than any finding count. Resolve it from the main checkout
instead of asking the user to activate anything — `--git-common-dir` points at the main
checkout's `.git` from inside any worktree, and at a plain `.git` when you are already
in the main checkout, so one form works in both:

```bash
RUFF="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir)")/.venv/bin/ruff"
"$RUFF" --version    # verified from inside a worktree: resolves to the main .venv
```

Use `"$RUFF"` in place of `ruff` in measurement B. If that path does not exist either,
then there genuinely is no environment: ask the user to activate theirs, and do not
create one.

Template and state-machine discovery is **shared with the gates** — always go through
`scripts/discover_templates.sh` rather than a glob, for the reason its header comment
gives (a hardcoded glob missed `nested/`, `samples/`, `notebooks/`, `scripts/` and
`iam-roles/` for months).

```bash
scripts/discover_templates.sh cfn | wc -l    # 30 CloudFormation templates
scripts/discover_templates.sh asl | wc -l    # 4 Step Functions definitions
```

### A. Resources per CloudFormation stack

```bash
for t in $(scripts/discover_templates.sh cfn); do
  printf '%5d  %s\n' \
    "$(awk '/^Resources:/{r=1;next} r&&/^[A-Za-z]/{r=0} r&&/^  [A-Za-z0-9]+:[ \t]*$/{n++} END{print n+0}' "$t")" \
    "$t"
done | sort -rn
```

Last measured: `template.yaml` **314**, `nested/api-resolvers/template.yaml` 86,
`patterns/unified/template.yaml` 57.

⚠️ **That is the count before the SAM transform, and the 500-resource limit binds on
the count after it.** `AWS::Serverless::Function` expands to a Lambda function plus,
unless `Role` is given, a generated IAM role; `AWS::Serverless::Api` expands to a
RestApi plus a Deployment and a Stage; each `Api`, `CloudWatchEvent`, `SQS` or
`DynamoDB` event source adds a Permission or an EventSourceMapping. Reporting only the
pre-transform number overstates the headroom by about seventy resources, so transform
it:

```bash
python3 - <<'PY'
import datetime
from unittest.mock import MagicMock
import cfnlint.decode.cfn_yaml as cfn_yaml
from samtranslator.translator.translator import Translator

URI = {"CodeUri", "ContentUri"}
def plain(o, key=None):
    if isinstance(o, dict):  return {str(k): plain(v, str(k)) for k, v in o.items()}
    if isinstance(o, list):  return [plain(v, key) for v in o]
    if isinstance(o, (datetime.date, datetime.datetime)): return o.isoformat()
    if key in URI and isinstance(o, str): return "s3://bucket/key.zip"
    return o if isinstance(o, (str, int, float, bool)) or o is None else str(o)

loaded = cfn_yaml.load("template.yaml")
tpl = plain(loaded if isinstance(loaded, dict) else loaded[0])
src = len(tpl["Resources"])
for body in tpl["Resources"].values():                  # stub out local artifacts
    if body.get("Type") == "AWS::Serverless::Api":
        body.setdefault("Properties", {}).setdefault(
            "DefinitionBody", {"swagger": "2.0",
                               "info": {"title": "t", "version": "1"}, "paths": {}})
    if body.get("Type") == "AWS::Serverless::StateMachine":
        p = body.setdefault("Properties", {}); p.pop("DefinitionUri", None)
        p.setdefault("Definition", {"StartAt": "s",
                                    "States": {"s": {"Type": "Succeed"}}})
mp = MagicMock(); mp.get_availability_zones.return_value = ["us-east-1a"]
out = Translator(None, mp).translate(sam_template=tpl, parameter_values={})
print(f"source={src} transformed={len(out['Resources'])}")
PY
```

Last measured: **314 source → 384 after the transform**, of a hard 500. So the real
headroom is around **115 resources, not 185**, and that is the number to track release
over release — it is the constraint that forced the nested-stack split and it will
force the next one. Treat 384 as `measured` but approximate to within a few: the
stub API definition body above is not the real one, and a handful of resources sit
behind `Condition:`.

### B. Lint and typecheck coverage

Coverage first, findings second. A path that is excluded — or that does not exist —
reports zero problems, which reads identically to clean.

```bash
# ruff: files it would actually check, against every tracked .py. Take the
# "checked" figure from check_lint_debt.py, NOT from ruff: `ruff check
# --show-files` does not honour `[lint] exclude`, so it over-reports by exactly
# the size of that list. The one authority on whether a given file is linted is
# `python3 scripts/check_lint_debt.py --explain <path>`.
echo "tracked: $(git ls-files '*.py' | wc -l)"
python3 scripts/check_lint_debt.py --summary

# basedpyright: does every include/exclude path in pyrightconfig.json exist?
python3 -c "
import json, os
c = json.load(open('pyrightconfig.json'))
for k in ('include', 'exclude'):
    for p in c.get(k, []):
        base = p.split('*')[0].rstrip('/') or '.'
        print(f'{k:8} {\"OK  \" if os.path.exists(base) else \"MISS\"} {p}')
"
```

Measure the coverage figures rather than quoting them: they grow with the tree, so a
number written here is wrong by the next review. `git ls-files '*.py' | wc -l` is the
tracked total and is exactly what `basedpyright` reports as `filesAnalyzed`; `python3
scripts/check_lint_debt.py --summary` prints how many of them `ruff check` reads and
how many it skips. The figure that is an **invariant** rather than a measurement is
basedpyright's **0 errors** — that is the one to treat as a regression if it moves.
What each gate skips is a named list of individual files, not a tree — files carrying
pre-existing lint findings, files `ruff format` has never run over, plus two scope
entries (the vendored `pii-anonymizer` tree and `**/*.ipynb`); the same `--summary`
prints that split, and `make check-lint-debt` fails if a listed file gains a finding
or has become clean.

⚠️ **basedpyright is not pinned**, unlike `cfn-lint`. `package.json` declares
`"basedpyright": "^1.32.1"`, which is a caret range and in any case is not what gets
run: all three install sites — the `Makefile` hint, `.github/workflows/developer-tests.yml`
and `.gitlab-ci.yml` — run a bare `npm install -g basedpyright`, so the version is
whatever was current that day. Read the version off the run and record it beside the
counts, and treat a warning-count change as possibly-a-release rather than
possibly-a-regression:

```bash
basedpyright --version
basedpyright --outputjson | python3 -c "import json,sys; print(json.load(sys.stdin)['summary'])"
```

The **error** count is the one that must stay at zero, and it is the count that is
stable across versions and across checkout states.

Two things to know before reporting a coverage gap here. The **formatting** debt is
deliberately unpaid and is not a new finding: reformatting that list of files is a
mechanical sweep deferred to its own change (issue #975 closed the exclusions, not the
formatting). And when you demonstrate that a file is excluded, use
`python3 scripts/check_lint_debt.py --explain <path>`. **No ruff invocation answers
this correctly.** A plain `ruff check <path>` bypasses the exclusions, and
`--force-exclude` restores only `exclude`/`extend-exclude` — `[lint] exclude` and
`[format] exclude` filter after discovery, so `ruff check --force-exclude <path>`
prints `All checks passed!` and exits 0 for every lint-excluded file.
`ruff check --show-files` does not honour `[lint] exclude` either.

Pair the coverage number with the largest uncovered files:

```bash
git ls-files '*.py' | xargs wc -l | sort -rn | head -20
```

Last measured: 518,109 tracked Python lines; the top five are
`lib/idp_common_pkg/tests/unit/test_test_set_resolver.py` (8,399),
`lib/idp_common_pkg/idp_common/extraction/service.py` (8,147),
`lib/idp_cli_pkg/idp_cli/cli.py` (6,897), `scripts/sdlc/codebuild_deployment.py`
(6,291) and `nested/api-resolvers/src/lambda/test_set_resolver/index.py` (5,615).
Cross-reference every entry against the two coverage checks above: size alone is a
style opinion, size **plus** no lint and no typecheck is a finding. `cli.py` is no
longer that combination — `pyrightconfig.json`'s `include` now reaches every tree
holding tracked Python, so the type checker sees it (issue #923, PR **#943**).

### C. Registered vs orphaned test roots

```bash
make test-list          # or: python3 scripts/run_all_tests.py --list
```

`scripts/run_all_tests.py` discovers roots by content and **errors on any root that
is in neither registry**, so a new suite cannot be silently skipped — that is the
right shape and worth saying so in the report. What it does not tell you is what the
quarantines cost:

```bash
for d in $(python3 scripts/run_all_tests.py --list | sed -n 's/^  - \([^:]*\):.*/\1/p'); do
  printf '%3d  %s\n' "$(find "$d" -maxdepth 2 -name 'test_*.py' 2>/dev/null | wc -l)" "$d"
done
```

Last measured: **64 RUN roots, 6 QUARANTINE**. Each quarantine carries a written
reason in the script; check the reason is still true (a `cfnresponse`-only quarantine
stops being justified the moment someone adds a stub).

### D. Alarm inventory, and which alarms reach a subscriber

An alarm is a control. Its decision point is the notification path. Measure both.

```bash
for t in $(scripts/discover_templates.sh cfn); do
  n=$(grep -c "AWS::CloudWatch::Alarm" "$t"); [ "$n" -gt 0 ] && printf '%3d  %s\n' "$n" "$t"
done | sort -rn

# where the alarm actions point …
grep -n -A2 'AlarmActions:' template.yaml | grep -oE '!Ref [A-Za-z0-9]+' | sort | uniq -c
# … and which topics have a subscription at all
grep -n -A6 'Type: AWS::SNS::Subscription' template.yaml | grep -E 'TopicArn|Protocol|Endpoint'
```

Last measured: **15 alarms**, all 15 with an `AlarmActions`, and all 15 now reaching a
topic that has a subscriber — 14 point at `AlertsTopic`, which carries an email
subscription to `AdminEmail`, and the fifteenth at `CircuitBreakerTopic`, which has a
Lambda subscription. That closes issue **#922**, where eleven of twelve alarms fired
into a topic with **zero** `AWS::SNS::Subscription` resources (PR **#941**). Keep
running both halves anyway: "15 alarms configured" was never the observability
measurement, the subscription is, and a new topic added without one puts the gap
straight back.

**D2 — Lambda-to-LogGroup ratio:**

```bash
for t in $(scripts/discover_templates.sh cfn); do
  f=$(grep -c "AWS::Serverless::Function\|AWS::Lambda::Function" "$t")
  l=$(grep -c "AWS::Logs::LogGroup" "$t")
  [ "$f" -gt 0 ] && printf 'fn=%-4s lg=%-4s  %s\n' "$f" "$l" "$t"
done | sort -k1 -r
```

Last measured: `template.yaml` 58 functions / 56 log groups;
`nested/api-resolvers/` 31 / 33; `feature-platform/main-stack-extensions/` 9 / 6;
`nested/bedrockkb/` **5 functions / 0 log groups**. The bedrockkb gap is
*deliberate* — those are custom-resource-only Lambdas that keep Lambda's
auto-created group, and `scripts/tests/test_lambda_log_groups.py` asserts exactly
that. Do not report it. It is in the known-non-defects list for that reason.

Across all 30 templates there are **158** `AWS::Logs::LogGroup` resources, of which
**84** declare no `LogGroupName` and so take CloudFormation's generated
`<StackName>-<LogicalId>-<hash>` name. That is the most common shape in the repo and
it has no leading `/`, which matters when anyone goes looking for a function's logs:

```bash
python3 - <<'PY'
import subprocess
import cfnlint.decode.cfn_yaml as cfn_yaml
tot = named = 0
for t in subprocess.run(["scripts/discover_templates.sh", "cfn"],
                        capture_output=True, text=True, check=True).stdout.split():
    loaded = cfn_yaml.load(t)
    tpl = loaded if isinstance(loaded, dict) else loaded[0]
    for v in (tpl.get("Resources") or {}).values():
        if v.get("Type") == "AWS::Logs::LogGroup":
            tot += 1
            named += "LogGroupName" in (v.get("Properties") or {})
print(f"log groups={tot} explicit name={named} generated name={tot - named}")
PY
```

### E. IAM wildcard census

**Parse the templates; do not grep the lines.** A line grep for these is wrong in
four independent ways, and three of them make it *under*-report, which is the direction
that loses findings.

1. **It is quoting-fragile.** The `Resource:[[:space:]]*("\*"|'\*'|\*)` pattern returns
   143 under one shell and 101 under another, because the nested quotes are
   re-interpreted before `grep` ever sees them. A figure you cannot reproduce on
   another machine is not a measurement.
2. **It sees only the spelling it was written for.** `Resource: "*"` on one line
   matches; a scalar with a **trailing comment** does not
   (`nested/bedrockkb/template.yaml:635`, `Resource: '*'   # no resource
   specification applicable`), and neither does the list-item form:

   ```yaml
   Resource:
     - "*"
   ```

   For actions the same anchoring is worse: a pattern requiring the wildcard alone on
   its line cannot see `Action: "sqs:*"`, which is how most of them are written here.
   The action grep finds **41** of the **80** that exist, and the same-line spelling is
   why it misses 39.
3. **It cannot tell a statement from a sentence about one.** `template.yaml:14036` is a
   *comment* — "CloudWatch Logs actions for Step Functions logging require
   `Resource: "*"`" — and the grep counts it as a statement.
4. **It cannot see `Effect`.** This is the one that changes the conclusion rather than
   the count, and the walk below is what makes it visible: a wildcard in a `Deny`
   broadens a denial, which is the safe direction, and **34 of the 80** action
   wildcards are exactly that.

Two things a line grep is *not* wrong about, so do not repeat them as criticisms: it
matches `!If`-wrapped statements perfectly well, because it reads lines and does not
care that the statement is a branch of a condition rather than a member of the
`Statement` list — all six such statements here have plain scalar lines and all six
match. And only one template in the tree contains a `ZipFile:` block at all, with no
wildcard line inside it, so embedded Lambda source is a hazard in principle and not a
present source of error.

```bash
python3 - <<'PY'
import subprocess
import cfnlint.decode.cfn_yaml as cfn_yaml


def statements(node):
    """Every IAM statement anywhere, keyed on SHAPE, not on nesting.

    A mapping carrying `Effect` plus an action key is a statement wherever it
    sits — which is what reaches the ones inside `!If` and inside SAM's
    `Policies:` shorthand.
    """
    if isinstance(node, dict):
        if "Effect" in node and ("Action" in node or "NotAction" in node):
            yield node
        for v in node.values():
            yield from statements(v)
    elif isinstance(node, list):
        for v in node:
            yield from statements(v)


def as_list(v):
    return [] if v is None else (v if isinstance(v, list) else [v])


# Resource types whose policy scopes `Resource` to the resource ITSELF, so `*`
# there means "this key/bucket/queue", not "everything in the account".
SELF_SCOPED = {"AWS::KMS::Key", "AWS::S3::BucketPolicy", "AWS::SQS::QueuePolicy",
               "AWS::SNS::TopicPolicy"}

res, act = {}, {}
for t in subprocess.run(["scripts/discover_templates.sh", "cfn"],
                        capture_output=True, text=True, check=True).stdout.split():
    loaded = cfn_yaml.load(t)
    tpl = loaded if isinstance(loaded, dict) else loaded[0]
    # Iterate RESOURCES, not the whole template, so each statement keeps the type
    # of the resource enclosing it — which is what the self-scoped split needs.
    for body in (tpl.get("Resources") or {}).values():
        rtype = body.get("Type")
        for s in statements(body):
            # A wildcard in a Deny BROADENS the deny. Counting it as a finding is
            # the way the grep misleads that invents work rather than losing it.
            effect = "Allow" if str(s.get("Effect")) == "Allow" else "Deny"
            bucket = (effect, "self-scoped" if rtype in SELF_SCOPED else "account")
            if any(r == "*" for r in as_list(s.get("Resource")) if isinstance(r, str)):
                res.setdefault(bucket, {})
                res[bucket][t] = res[bucket].get(t, 0) + 1
            for a in as_list(s.get("Action")):
                if isinstance(a, str) and a.endswith(":*"):
                    act.setdefault(bucket, {})
                    act[bucket][t] = act[bucket].get(t, 0) + 1

for title, rows in (('Resource: "*"', res), ("action wildcards service:*", act)):
    for bucket in sorted(rows):
        by_t = rows[bucket]
        print(f'--- {title} {bucket} : {sum(by_t.values())} '
              f'across {len(by_t)} template(s) ---')
        for path, n in sorted(by_t.items(), key=lambda kv: -kv[1]):
            print(f"{n:5d}  {path}")
PY
```

Last measured, exactly as the snippet prints it:

| | `Allow`, account-wide | `Allow`, self-scoped | `Deny` | total |
|---|---|---|---|---|
| `Resource: "*"` | **128** across 13 templates | 16 across 4 | 3 across 2 | **147** |
| `service:*` actions | **40** across 4 templates | 6 across 4 | 34 across 5 | **80** |

The account-wide `Allow` column is the one to report. The other two are the reason a
single total misleads, in two different ways:

- **`Deny`.** A wildcard in a denial makes the denial *broader*, which is the safe
  direction. All 34 `Deny` action wildcards are the `EnforceSSLOnly` bucket and queue
  resource policies (`Principal: "*"`, `Condition: aws:SecureTransport: false`), so
  quoting "80 over-broad action grants" describes 34 statements as the opposite of what
  they are.
- **Self-scoped.** In a resource policy, `Resource: "*"` means *this* resource, not
  every resource in the account — that is the only thing it can mean there. All 16 of
  these are `AWS::KMS::Key` key policies, and 6 of the `Allow` action wildcards are
  `kms:*` in the same place. ⚠️ **This is the `Deny` argument one level down**, and it
  is easy to make the `Deny` split and then quote 144 as though every one of them were
  account-wide. The snippet's `SELF_SCOPED` set also covers bucket, queue and topic
  policies so the question gets asked of a new resource policy automatically; only KMS
  keys hit it today.

Within the account-wide column, report the *unjustified* ones — cross-check each against
a cfn-nag/checkov suppression carrying a `reason:`; a wildcard with a written reason is a
decision, one without is a finding. 26 of the 40 account-wide action wildcards are in
`iam-roles/cloudformation-management/`, where a deployment service role legitimately
needs breadth, so that is a decision rather than a finding too.

### F. Documentation-to-template drift, both directions

```bash
# shipped service namespaces with no mention in the services doc
for t in $(scripts/discover_templates.sh cfn); do grep -ohE "AWS::[A-Za-z0-9]+::" "$t"; done \
  | sort -u | sed 's/AWS:://;s/:://' > /tmp/svc.txt
while read -r s; do
  grep -qi "$s" docs/aws-services-and-roles.md || echo "UNDOCUMENTED: AWS::$s"
done < /tmp/svc.txt

# the reverse: a service the docs still describe that no template declares.
# AppSync is the worked example — the UI moved to API Gateway, the docs did not.
grep -rl "AWS::AppSync" $(scripts/discover_templates.sh cfn) || echo "no template declares AppSync"
grep -rl "AppSync" docs/*.md | wc -l
```

Last measured: **27 service namespaces** shipped, of which two —
`AWS::CodePipeline` and `AWS::OpenSearchServerless` — appear nowhere in
`docs/aws-services-and-roles.md`. ⚠️ Note what the reverse grep now returns: two
templates *do* match `AWS::AppSync`, both of them IAM templates
(`iam-roles/cloudformation-management/IDP-Cloudformation-Service-Role.yaml` and
`scripts/sdlc/cfn/codepipeline-s3.yml`) naming the service in an `Action:` rather than
declaring a resource of that type. So `grep -rl` answers a different question from
"does any template declare an AppSync resource" — read the hits before concluding
anything, or parse for `Type: AWS::AppSync::`. No template declares an
AppSync resource, and when this review was first run **28 files under `docs/`**
described AppSync as the UI-to-backend API while `CLAUDE.md` listed it under "Key AWS
Services Used". That is now closed: issue #929 fixed the prose, `CLAUDE.md` keeps only
a parenthetical saying the nested stack was *historically* named `APPSYNCSTACK`, and
the 13 remaining `docs/` mentions are each either explicitly historical or a retained
GraphQL-schema identifier, triaged one at a time in
`scripts/sdlc/retired_services.json`. The reverse direction is therefore no longer a
manual search here — `make check-retired-services` enforces it on every push and MR,
so run this section to look for the *next* retired service, not for AppSync.
Substitute the current name for a removed service each run — the search only works if
you know what was removed, so read `CHANGELOG.md`'s `### Changed`/`### Removed`
entries since the last review to get the candidate list.

**F2 — skill-file inventory vs the `CLAUDE.md` table** (this repo documents its own
skills, so the table is a control that can drift):

```bash
ls .claude/skills/*.md | sed 's|.*/||' | sort > /tmp/skills.txt
grep -oE '\.claude/skills/[a-z0-9-]+\.md' CLAUDE.md | sed 's|.*/||' | sort -u > /tmp/tabled.txt
comm -23 /tmp/skills.txt /tmp/tabled.txt      # skill files with no CLAUDE.md row
find .cline/skills -maxdepth 1 -type f -name '*.md'   # any output = a COPY, not a symlink

# the third direction: a .claude skill Cline cannot see at all
for c in .cline/skills/*.md; do basename "$(readlink -f "$c")"; done | sort -u > /tmp/linked.txt
comm -23 /tmp/skills.txt /tmp/linked.txt      # .claude skills with no .cline symlink
```

Last measured: **28 skill files, 28 `CLAUDE.md` rows** — `comm -23` returns nothing,
which is the expected state. All `.cline/skills` entries are symlinks (the `find -type
f` returns nothing), which is the required state per
`.claude/skills/documentation.md`. `scripts/tests/test_repo_quality_review_skill.py`
asserts both for every skill file rather than for one, so this drift cannot recur
silently — which makes this measurement a check on the *test*, not a hunt for a known
gap.

The third `comm` is the one worth actually reading, because it is the direction the
first version of that test left open. Skill visibility is a **triangle** — a `.claude`
file, a `CLAUDE.md` row, a `.cline` symlink — and closing two sides can leave the third
wide: registering `sync-pii-anonymizer.md` in the table did nothing to make Cline able
to read it. That case is what the direction was added for. It is an offline
vendored-code resync with no live-stack step, so the live-tier rationale that covers the
other absences did not cover it, and the owner resolved it by adding the symlink.

Last measured: **28 `.claude` skills and 23 `.cline` entries** — five absences, all five
the deliberately Claude-only live-stack tiers (`full-test-battery.md`,
`run-benchmarks.md`, `run-stack-tests.md`, `test-upgrade.md`,
`transform-deploy-test.md`), which is a legitimate reason to have no symlink. Their
reasons are recorded per entry in the test's `CLINE_EXEMPT` table, so an absence has to
be stated rather than merely observed.

Note what that sequence does and does not license. The rule is still: do not close a gap
in this direction by creating symlinks to make the test green. Whether a live-stack skill
should be visible to Cline is a judgement about that assistant's capabilities, and a
symlink added to satisfy a test inverts the decision. The `sync-pii-anonymizer.md` case
went the other way for a reason that had nothing to do with the test — the *stated
rationale for the exemption class did not apply to it*, so the honest options were a
different reason or a symlink, and the owner chose the symlink. Report the unexplained
ones with the reason each one fails to fit, and let the owner choose.

### G. Gate inventory — exists / GitHub / GitLab / blocking

```bash
grep -oE '^[a-zA-Z][a-zA-Z0-9_.-]*:' Makefile | tr -d ':' | sort -u > /tmp/all_targets.txt
grep -rhoE 'make [a-zA-Z][a-zA-Z0-9_-]*' .github/workflows/ | sed 's/^make //' | sort -u > /tmp/gh.txt
grep -hoE  'make [a-zA-Z][a-zA-Z0-9_-]*' .gitlab-ci.yml    | sed 's/^make //' | sort -u > /tmp/gl.txt

echo "make targets: $(wc -l < /tmp/all_targets.txt)"
echo '--- in GitHub CI ---';  comm -12 /tmp/gh.txt /tmp/all_targets.txt
echo '--- in GitLab CI ---';  comm -12 /tmp/gl.txt /tmp/all_targets.txt
echo '--- GitLab only ---';   comm -13 /tmp/gh.txt /tmp/gl.txt | grep -Fxf /tmp/all_targets.txt
echo '--- GitHub only ---';   comm -23 /tmp/gh.txt /tmp/gl.txt | grep -Fxf /tmp/all_targets.txt

echo '--- advisory, not blocking ---'
grep -n 'allow_failure' .gitlab-ci.yml
grep -rn 'continue-on-error' .github/workflows/
```

Two traps in reading that output. First, `grep 'make …'` also matches English prose
("make the", "make curl"), which is why every list is intersected against the real
target list with `comm`/`grep -Fxf`. Second, a target invoked as
`make test-cicd -C lib/idp_common_pkg` lives in a **sub-Makefile** and will not appear
in `/tmp/all_targets.txt` — check for it by name before concluding it runs nowhere.

Last measured: **85 root `make` targets**; GitHub CI invokes 9 of them, GitLab 6 plus
`test-cicd` in the sub-Makefile; the GitHub-only three (`dep-manifest`, `docs-deploy`,
`install-first-party`) are publish/scaffold steps, not gates, and nothing is
GitLab-only, so the *gate* sets match — which is what
`scripts/tests/test_ci_gate_parity.py` exists to keep true. Two `allow_failure` entries
appear in `.gitlab-ci.yml` and three GitHub steps in `developer-tests.yml` are
explicitly `continue-on-error: false`.

**G2 — which checks are actually required on `develop` *and* on `main`.** This is the
step that turns "the gate runs" into "the gate blocks", and it is the one the parity
test cannot cover.

**Read both branches.** `develop` is what pull requests target, but `main` is the
repository's **default** branch and the one releases are cut from, and it carries its
own independent protection setting. Checking only `develop` is why `main` being equally
unprotected went unnoticed. Read the two endpoints that answer at ordinary permission
levels, and do **not** infer protection state from the protection endpoint alone:

```bash
R=aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws

gh api "repos/$R" -q '.default_branch'   # main — note which branch that is

# 1. Classic branch protection — take it from the BRANCH object. The `protected`
#    field is returned at `pull` level and settles the question either way. Per
#    branch: one read answers for one branch.
for b in develop main; do
  echo "$b: $(gh api "repos/$R/branches/$b" -q '.protected')"
  gh api "repos/$R/branches/$b" -q '.protection.required_status_checks'
done

# 2. Rulesets, which is the other way a check can be required. Includes
#    org- and enterprise-INHERITED rulesets, and also needs no admin.
gh api "repos/$R/rulesets" -q '.[] | "\(.target)  \(.source_type)  \(.name)"'

# 3. The dedicated protection endpoint is the trap. It requires **admin** and
#    returns 404 — not 403 — when admin is absent, deliberately, so that it does
#    not disclose whether protection exists. A 404 is therefore equally consistent
#    with "not protected" and with "protected, invisible to this token".
gh api "repos/$R/branches/develop/protection"
gh api "repos/$R" -q '.permissions'   # admin:false => that 404 told you nothing
```

The repository ships this as a tool, which is the better way to run it — it also
derives the required-check list the workflows *should* produce and diffs it against the
live one, which the raw reads above do not:

```bash
make check-branch-protection                                      # develop
make check-branch-protection BRANCH_PROTECTION_ARGS=--branch=main  # main
```

Last measured **2026-09-21** with
`{"admin":false,"maintain":true,"pull":true,"push":true,"triage":true}`: the default
branch is `main`; `branches/develop` **and** `branches/main` both report
**`protected: false`** with `required_status_checks` of
`{checks: [], contexts: [], enforcement_level: "off"}`; and all five rulesets are
enterprise-inherited with **no branch target** — four `target=repository` (block
internal visibility, block private visibility, block repository deletion, only
enterprise owners can transfer) and one `target=tag` (`block-untagged`). All of that is
**measured** at this permission level. The protection endpoint did return 404, but that
reading was discarded as uninformative: it is the branch objects and the ruleset list
that establish the result. So no required status check exists on either long-lived
branch — every gate in G is visible and none is blocking, and a red PR can be merged.

**Report this as a known, accepted residual, not as a new finding, and do not re-file
it.** The decision is recorded in closed issue **#933** (`NOT_PLANNED`): enabling
classic protection needs repository **admin**, which no contributor and no CI token
here has, so it cannot be done from the tree or from tooling. Cite the issue as the
decision record. The condition under which it stops being a residual is a repository
**setting** changing, by one of two routes that are not the same permission — somebody
with repository admin enabling protection, or an organization or enterprise owner
publishing a **branch ruleset** targeting these branches, which needs no repository
admin at all and is demonstrably available here given the five inherited rulesets.
Report the residual with its **structural** limit stated: nothing in the repository can
substitute, because enforcement is server-side and a merge taken through GitHub's own
Merge button runs no code from the tree. That is why this is the one Class 1 instance
whose decision point cannot be brought inside the repository.

It is still the flagship Class 1 instance — a control that exists and is consulted by
nothing — and still worth leading the report with, as a statement of exposure rather
than as an action item.

Two rules for re-running this, because the conclusion is security-relevant and the
skill is meant to be re-run:

- **Never label "nothing blocks" `measured` on the strength of a 404.** If step 1 is
  available you do not need to: it is dispositive. If even step 1 is unavailable to
  you, you cannot establish protection state at all — report the conclusion as
  **`unverified`** per the output contract's rule 3, and say which read you were denied.
  The rulesets half stays `measured` regardless, since it needs no admin.
- **Re-read it every run rather than carrying the finding forward, even though it is a
  recorded residual.** A maintainer or an organization owner turning protection on is
  the single most likely way this changes, and neither announces itself in the tree —
  so a stale "nothing blocks" is precisely the Class 1 error this skill exists to
  catch: a decision made as though a control's state had been consulted when it never
  was. "It is recorded in #933" is a reason not to re-file the issue, never a reason
  not to re-measure.

### H. Frontend test ratio

```bash
echo "UI test files: $(find src/ui/src \( -name '*.test.*' -o -name '*.spec.*' \) | wc -l)"
echo "UI sources:    $(find src/ui/src \( -name '*.tsx' -o -name '*.ts' -o -name '*.jsx' -o -name '*.js' \) | grep -v generated | wc -l)"
```

Last measured: **99 test files against 408 sources**. Treat the ratio as a prompt to
ask *which* surfaces are untested (auth, upload, config editing) rather than as a
score.

### I. Ownership and bus factor

```bash
git shortlog -sn --since="6 months ago" HEAD            # commit share by author
git shortlog -sn --since="6 months ago" HEAD -- src/ui   # per subsystem: swap the path
git rev-list --count --since="6 months ago" HEAD         # the denominator
git log --since="6 months ago" --format=%ad --date=short HEAD | tail -1   # window opens
```

The explicit `HEAD` is load-bearing, not decorative. Given **no revision argument**
`git shortlog` reads its commit list from **stdin**, and in a non-interactive shell
stdin is empty — so it prints nothing and exits **0**, which reads exactly like "no
commits in this window". Do not respond to that by swapping in a commit-count window
such as `-200`, which is a different measurement wearing the same label: at this commit
those 200 commits span **four days** (2026-09-18 to 2026-09-21), so the window's width
varies silently with commit rate, and it distorts the answer badly — `-200` reports a
**100%** single-author share against the six-month **73%**, and it drops every other
contributor from the list entirely, so a bus-factor measurement loses the very thing it
measures. Keep `-sn` and not
`-sne`: `-e` splits one author here across three email addresses and re-fragments the
number being measured. `-sn` groups by author *name*, which has the mirror-image
problem — one contributor under two spellings is undercounted — so scan the list for
near-duplicate names before quoting a share.

Last measured over **six months to 2026-09-21** (window opens 2026-03-21; **2,917
commits** by **31** distinct author names): **2,132 Bob Strahan, 296 Jeremy Feldman,
221 Taniya Mathur, 100 dependabot** — a **73% single-author share**. `src/ui` over the
same window: 283 / 192 / 45 / 34. Two of those names are the same person
("Taniya Mathur" 221 and "Taniya [C] Mathur" 18). Always record the window alongside
the numbers, so the next run compares like with like. Report it as a risk statement
with the number, not as a criticism; then name the subsystems where the count is
exactly one.

**I2 — issue hygiene and contribution surface:**

```bash
R=aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws
gh issue list -R "$R" --state open --limit 200 --json number,labels -q 'length'
gh issue list -R "$R" --state open --limit 200 --json number,labels \
  -q '[.[]|select(.labels|length==0)]|length'
gh pr list    -R "$R" --state open --limit 200 --json number -q 'length'
ls .github/ISSUE_TEMPLATE/
find . -name CODEOWNERS -not -path '*/node_modules/*'   # no output = there is none
```

Last measured **2026-09-21**: **22 open issues** (10 unlabelled), **5 open PRs**, three
issue templates present (`bug_report.yml`, `feature_request.yml`, `config.yml`),
`.github/CODEOWNERS` present, and **no PR template**. ⚠️ These four counts are a live
read of the issue and pull-request lists, not a property of the commit, so they move
between runs with no change to the tree — record the date beside them and do not read a
difference as a trend. Before filing anything
from this review, search the open issues — the 2026-09 pass found several of its own
findings already filed, and re-filing them is noise.

## Class 1 — a control that exists as an artifact but is never consulted at the decision point

**The search.** For every control the repository claims — a gate, a flag, a policy, a
checksum, an alarm, a config key, an index, a lint rule — find the **code path that
makes the decision the control is supposed to influence**, and confirm the control is
read *there*. Existence of the artifact proves nothing. The artifact is almost always
present, well-named, documented and tested; what is missing is the read.

Ask, per control, in this order: *Who reads this? On which line? What happens if it
says no?* If you cannot name the file and line that reads it, that is the finding.

Where these hide, and how to find them: an artifact and its consumer are usually in
different languages or different layers, so no single-file review sees both. Grep for
the artifact's name across the **whole tree** and look at the *shape* of the hits —
if every hit is a definition, a doc, or a test of the definition, and none is a
consumer, you have one.

**Worked examples, all verified in this tree.** These are the shapes to recognize:
**illustrative and dated (2026-09), not a to-do list** — re-derive each one before
citing it, and when an instance gets fixed move it to a "closed" list with the fixing
PR rather than deleting it, so the class keeps its evidence without implying the
instance is still open.

**Open instances.**

| Control | Decision point that should read it | What is actually there |
|---|---|---|
| CI gates on GitHub and GitLab | branch protection / rulesets on `develop` **and** `main` | Nothing requires any check — measurement G2 reads `protected: false` on both branch objects and finds no branch-targeted ruleset. Every gate is visible; none blocks a merge. The archetype of the class, and the one instance here that is **not fixable from the tree**: it needs repository admin or an org/enterprise branch ruleset. Recorded as an accepted residual in closed issue **#933** — report the exposure, do not re-file it, and re-measure it every run |
| Published SHA-256 for a feature bundle | the loader that installs the bundle | No loader reads it, so the digest cannot reject a tampered artifact |
| `src/ui/.npmrc` `min-release-age=7` | the npm client resolving a new dependency | Honoured only by npm >= 11.10; the pinned `engines.npm` still allows 10.x, which ignores the key with a warning. The comment in the file says so — read it, then check what npm the build actually runs |

**Closed instances — evidence for the class, not work to do.** Each was fixed and
verified fixed; they stay here because the class keeps its evidence that way. Do not
re-report one, and do not re-derive it as though it were open: run the generalized
searches below instead, which are what would have found them.

| Control | Decision point that should have read it | What was there, and the fix |
|---|---|---|
| Pipeline hook `onError: fail` (#919) | the state machine's error routing | PR **#953**. `pipeline_hooks_function/index.py` raised when `onError == "fail"` — and the ASL `Catch` on `States.ALL` routed **forward** to the next step at **six** of the seven hook points in `patterns/unified/statemachine/workflow.asl.json` — only `PreprocessingHook` caught to a `Fail` state — so `onError: fail` could not fail the workflow. The routing now aborts the document |
| DynamoDB `SubIndex`, granted in IAM | the Chat-with-Document processor queried it to resolve the caller's config-version scope | #970, PR **#1020**. No template ever declared it, so every query raised `ValidationException` and the `except` logged and returned `None` — fail-**open**. Config-version scoping on the chat path had never restricted anything. Two things make this the archetype: the gap was *documented* in the function's own docstring, which is honest and was still a finding; and the unit suite stubbed `table.query` without validating `IndexName`, so it passed on a query the service would reject. **PR #1020** repointed the lookup at the `EmailIndex` that `template.yaml` actually declares, made any lookup failure deny rather than allow, and added a unit check tying the index the code names to the declared one. A later change (PR **#1038**) keyed the lookup on the immutable Cognito `sub` through a pointer item, because an email address that stops matching its record silently lifts the restriction; the `sub` route and the email join are deliberately **not** a fallback chain — the implementation's own docstring says so — so do not describe them as one |
| 12 CloudWatch alarms (#922) | an SNS subscriber | PR **#941**. 11 of 12 published to `AlertsTopic`, which had zero subscriptions. `AlertsTopic` now carries an email subscription to `AdminEmail`; measurement D reads 15 alarms all reaching a subscribed topic |
| `pyrightconfig.json` `include: idp_cli/idp_cli` (#923) | basedpyright's file walk | PR **#943**. The path did not exist, so a 6,800-line module was type-checked by nothing. `include` now covers every tree holding tracked Python and `test_pyright_config.py` derives that closure from `git ls-files` |
| X-Ray `put_annotation` document id (#925) | the annotation value X-Ray records | PR **#943**. Written as a set literal `{document.id}` in four pipeline Lambdas, so the value was unusable in exactly the four places it mattered most |

**The generalized searches to run**, beyond re-checking the eight above:

```bash
# every DynamoDB index a runtime query names, vs every index a template declares
grep -rhoE 'IndexName["]?[=:][^,}]*"[A-Za-z0-9_-]+"' --include='*.py' . \
  | grep -oE '"[A-Za-z0-9_-]+"$' | tr -d '"' | sort -u > /tmp/idx_used.txt
grep -rhoE 'IndexName: [A-Za-z0-9_"-]+' $(scripts/discover_templates.sh cfn) \
  | sed 's/IndexName: //' | tr -d '"' | sort -u > /tmp/idx_declared.txt
comm -23 /tmp/idx_used.txt /tmp/idx_declared.txt     # queried but never created

# every States.ALL catch and where it routes: a Next that goes FORWARD is fail-open
for f in $(scripts/discover_templates.sh asl); do
  echo "--- $f"; grep -n -A4 '"States.ALL"' "$f" | grep '"Next"'
done

# fail-open exception handlers: a broad except that returns a permissive default
grep -rn -A6 'except Exception' --include='*.py' src/lambda nested/*/src \
  | grep -E 'return (None|True|\[\]|\{\})'
```

For each hit, the question is not "is this except too broad" — it is **"what
authorization or correctness decision is downstream of this return, and does the
permissive default grant something?"**

## Class 2 — the instance was fixed and the class was not

**The search.** For every fix in the repository — every closed bug, every guard, every
`# noqa` with a story, every principle written in a docstring — grep for the *shape*
of the bug across the whole tree and count how many places have it. Then check what
prevents the next one: is the guard a **test that enumerates from the source**, or a
**list someone has to remember to update**?

A hand-maintained inventory in a repo that has already built content-discovery **four**
times (`scripts/discover_templates.sh` for templates and state machines,
`scripts/run_all_tests.py` for test roots, `scripts/tests/repo_files.py`'s
`tracked_paths` for tracked files of a given glob, and
`scripts/tests/gate_premises.py`'s `tracked_files` for git pathspecs) is itself a
Class 2 finding — the pattern for closing the class exists and was not reused. Four
helpers with overlapping jobs may be worth a finding of its own, but that is a
different one from a gate that reuses none of them.

Be fair about the trade-off, though: `scripts/tests/test_state_machine_provisioning_retry.py`
hardcodes its path list **on purpose**, and its header argues that "a discovery engine
is its own source of bugs, and a new state machine added without being listed here is
exactly the kind of change that should have to touch this file". That is a defensible
position. Report a hardcoded inventory as a finding only when you can say what it
currently misses — measure, do not assume.

**Worked examples, all verified in this tree** — as in Class 1, illustrative rather than
an open work list. Re-derive before citing.

**Open instances.**

| Fix that was applied | The class it left open |
|---|---|
| Deterministic-timeout / provisioning retry (#917) | Still uneven. Measure with the Task-vs-Retry counts below; `patterns/unified/statemachine/workflow.asl.json` has 24 Task states and 25 `Retry` blocks, but `src/lambda/finetuning_state_machine/definition.json` has 10 Tasks and **7** Retries and `src/lambda/multi_doc_discovery/statemachine.asl.json` 7 and **5** |
| Fail-closed scope contract articulated in `lib/idp_common_pkg/idp_common/config_scope.py` | the circuit breaker and several API resolvers still fail open. Find every caller and check each one, rather than trusting the module's own tests |

**Closed instances — evidence for the class, not work to do.**

| Fix that was applied | The class it left open, and how it was closed |
|---|---|
| Canonical log redactor `lib/idp_common_pkg/idp_common/utils/log_sanitizer.py` (#921) | PR **#945**. `_sanitize_for_log` had been hand-copied into **10** resolvers under `nested/api-resolvers/src/lambda/`, each carrying a 10-key denylist against the canonical 18, so eight keys were redacted by the library and not by the copies. The copies are gone; the resolvers call the canonical redactor. ⚠️ The `comm -23` search below now returns the **entire** canonical key set, because there is no local set left to subtract — an empty `/tmp/local.txt` makes the set difference look like a total divergence. Check that the local grep matched anything before reading its output |
| X-Ray `put_annotation` document id (#925) | PR **#943**. Written as a set literal `{document.id}` in four pipeline Lambdas and correctly as `document.id` in two others, so the value was unusable in exactly the four places it mattered most. ⚠️ The shape grep below now returns **2** hits, both of them prose inside `scripts/tests/test_xray_annotations.py`, which is the guard that closed the class — so a non-zero count here is not a finding until you have read the hits |

**The generalized searches:**

```bash
# retry coverage per state machine: Tasks vs Retry vs Catch
for f in $(scripts/discover_templates.sh asl); do
  printf 'Task=%-3s Retry=%-3s Catch=%-3s  %s\n' \
    "$(grep -c '"Type": *"Task"' "$f")" "$(grep -c '"Retry"' "$f")" \
    "$(grep -c '"Catch"' "$f")" "$f"
done

# the redactor divergence, as a set difference rather than by eye
sed -n '/_DEFAULT_DENY_KEY_SUBSTRINGS/,/^}/p' \
  lib/idp_common_pkg/idp_common/utils/log_sanitizer.py \
  | grep -oE '"[a-z_x-]+"' | tr -d '"' | sort -u > /tmp/canon.txt
grep -rn -A12 '_LOG_SENSITIVE_KEYS = ' --include='index.py' nested/api-resolvers/src/lambda/ \
  | grep -oE '"[a-z_x-]+"' | tr -d '"' | sort -u > /tmp/local.txt
comm -23 /tmp/canon.txt /tmp/local.txt      # keys the canonical set has, the copies don't

# the set-literal annotation bug, as a shape
grep -rn 'put_annotation([^)]*, *{' --include='*.py' .

# every hand-maintained inventory of tree contents that a content walk could
# replace. Keyed on the SHAPE of the list, not on a comment admitting to it.
python3 - <<'PY'
import ast, pathlib, re
ITEM = re.compile(r"^(?:[\w.*/-]+\.(?:py|ya?ml|json|ts|tsx|txt|sh|ipynb)"
                  r"|[\w.*/-]*/[\w.*/-]*|make [\w-]+)$")
# The content-discovery helpers this repo has TODAY, plus the raw walk primitives
# and `ls-files` for a gate that shells out to git itself. Keep this current: a
# helper missing from here mislabels its callers `ONLY RECORD`, a false positive in
# the expensive direction, since it sends a reviewer to audit a list that a
# universe-closure test already covers. Re-derive the list each run rather than
# trusting it — `grep -rln "def .*tracked\|discover" scripts/tests scripts/sdlc`
# is a reasonable start.
WALKS = re.compile(r"discover_templates|run_all_tests|tracked_paths|tracked_files"
                   r"|repo_files|gate_premises|ls-files|ls_files"
                   r"|rglob|\.glob\(|iterdir|os\.walk")
for f in sorted(p for r in ("scripts/tests", "scripts/sdlc")
                for p in pathlib.Path(r).rglob("*.py")):
    src = f.read_text(encoding="utf-8")
    kind = "cross-checked" if WALKS.search(src) else "ONLY RECORD"
    for n in ast.parse(src).body:                       # module level only
        if not isinstance(n, (ast.Assign, ast.AnnAssign)):
            continue
        tg = n.targets if isinstance(n, ast.Assign) else [n.target]
        names = [t.id for t in tg if isinstance(t, ast.Name)]
        v = n.value
        if isinstance(v, ast.Call) and v.args:          # frozenset({...}), tuple([...])
            v = v.args[0]
        if not names or not isinstance(v, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            continue
        elts = v.values if isinstance(v, ast.Dict) else v.elts
        items = [c.value for e in elts for c in ast.walk(e)
                 if isinstance(c, ast.Constant) and isinstance(c.value, str)
                 and ITEM.match(c.value)]
        if len(items) >= 2 and len(items) >= 0.6 * len(elts):
            print(f"{kind:13} {f}:{n.lineno}  {names[0]} ({len(elts)})")
PY
```

Three things about that search, because the version it replaces was itself a Class 2
instance and the lesson is the point.

**Why it is shaped this way.** The obvious search is
`grep -rn -i 'hard.coded' --include='*.py' scripts/tests/ scripts/sdlc/`. Do not use
it. It returns **two hits** at this commit, of which only one is an inventory —
`scripts/tests/test_state_machine_provisioning_retry.py:45`, the one the "be fair about
the trade-off" paragraph above already excuses — because it is keyed on an author having
*confessed* in a comment. A search that can only return inventories whose authors already flagged them
finds nothing you did not already know. The replacement matches module-level
list/tuple/set/dict literals whose elements are path-like or `make`-target-like
strings, which is what a hardcoded inventory actually looks like regardless of whether
anyone commented on it. It also has to reach inside `REPO_ROOT / "path"` expressions
and `frozenset({...})` wrappers, which is why it walks the AST rather than matching
text — the excused example is a dict of `Path` expressions and a naive literal match
misses it.

**The discriminator is the second column, and it is the whole value of the output.**
`ONLY RECORD` means nothing in that file enumerates from the tree, so the list *is* the
record of what should be there and goes stale silently. `cross-checked` means the file
also walks the tree somewhere, so a stale entry has a decent chance of being caught.
Read the `ONLY RECORD` rows; skim the rest. This is a heuristic on the file, not the
assignment, so confirm by reading before you report.

**Last measured at `bbf0dc9c9`: 30 inventories, 6 of them `ONLY RECORD`.** The
previous run recorded 15 at `fac1c120b`; the doubling is tree drift over three days of
heavy gate work rather than a change in the search, and it is not reconstructable from
here — if a jump like that matters to you, check out the older commit and re-run.

`scripts/tests/test_ci_gate_parity.py` `SHARED_GATES` is still hand-authored, and still
the only record of *which specific invocations* must appear in both CI configurations —
deliberately, because deciding which lines of a CI config are gate invocations is not
derivable. But do **not** lead a review with "adding a gate to the `Makefile` and to
neither CI passes": the file derives a gate universe from the `Makefile`
(`gate_universe()`) and fails when a target in it is absent from both CIs *and* from
`GATES_DELIBERATELY_OUT_OF_CI`. Verify rather than assume — add a `check-*` target to one
of the scanned sections, run the suite, and watch
`test_every_gate_shaped_target_is_in_both_cis_or_registered` fail.

What is still worth probing there is the universe's own edge. It is every target in the
five `##@` sections that are entirely checks, plus any **check-shaped-by-name** target in
any other section, minus the live-stack section. A check elsewhere whose *name* does not
look like one is outside it. That residual is registered with its ratchet in
`scripts/tests/gate_exemptions.json`, so the question to ask is whether the registered
reason still matches the tree — not whether the gap exists.

`scripts/tests/test_state_machine_provisioning_retry.py:50` `ASL_JSON_PATHS` also comes
back `ONLY RECORD`, correctly: it is deliberately the only record, and the paragraph
above is why that is defensible. The remaining four are
`scripts/sdlc/tests/test_config_schema_order.py:31` `_TEMPLATES`,
`scripts/sdlc/tests/test_retired_models_not_offered.py` `_OFFERING_SURFACES` and
`_UI_SURFACES`, and `scripts/tests/test_config_revision_read_grants.py:35` `TEMPLATES`.

Three known limits of the search, so you do not over-read a clean run. It scans dict
*values* and not keys — including keys found nothing extra and, by doubling the element
count, pushed the excused example below the 0.6 ratio threshold, so values-only is
strictly better here but an inventory keyed by path would be missed. Two files carry
paths inside dict values that are justification *prose*; the element pattern rejects
strings containing spaces to keep those out, but re-check any hit whose entries read
like sentences. And the second column is a **heuristic on the file**, so keep `WALKS`
current: with `tracked_paths`/`repo_files` missing from it the search reported 8
`ONLY RECORD` rather than 6, the two false positives being
`scripts/tests/test_lambda_log_groups.py` and
`scripts/tests/test_log_group_encryption.py`. Each discovers its templates through a
**single** `tracked_paths` call (`:792` and `:514`) and each carries a real
universe-closure test, so both were the opposite of an unchecked record. One call is all
it takes, which is the trap: `grep -c tracked_paths` returns 3 and 4 for those files
because it also counts the import and the comments, so do not read a grep count as a
number of discovery sites.

Then, for each fix landed since the last review (read `CHANGELOG.md`'s `### Fixed`
entries and the PR numbers in them), do the same by hand: take the shape of that bug
and grep for it tree-wide. This is the highest-yield twenty minutes in the whole
review.

## Known non-defects — do not re-report (delete a row when it stops being true)

A withdrawal is a finding too: recording *why* something is not a defect is what
stops the next run spending a reviewer on it. The entries below were each reported by
the pass in the date column and then withdrawn after investigation.

| Withdrawn | Suspected finding | Why it is not a defect |
|---|---|---|
| 2026-09 | Byte-identical vendored module copies under `src/lambda/chat_stream_processor/vendored/` | A deliberate SAM packaging workaround: the two Lambdas cannot share a directory at build time. It is **guarded** — a vendored-in-sync test fails if the copy drifts from the original — and the sync is asserted in CI. Copying without a guard would be a Class 2 finding; this one has the guard |
| 2026-09 | `reportUnsupportedDunderAll` warnings against `lib/idp_common_pkg/idp_common/__init__.py` | The module is a deliberate **PEP 562 lazy loader** (`__getattr__`), which is the documented pattern for keeping Lambda package size down — `__all__` names attributes that exist only on access. The warning is the type checker not modelling the pattern, not a defect |
| 2026-09 | `nested/bedrockkb/` declaring 5 Lambda functions and 0 `AWS::Logs::LogGroup` resources | Deliberate: those are custom-resource-only Lambdas that run during a stack operation and keep Lambda's auto-created log group, an accepted retention cost. `scripts/tests/test_lambda_log_groups.py` asserts exactly this shape |
| 2026-09 | `last_exception` in `lib/idp_common_pkg/idp_common/bedrock/client.py` looks like a swallowed error | It is dead but harmless, and it is a **Python semantics trap a fresh reviewer will re-derive from scratch** — which is why it is here rather than left to be rediscovered. The three sites (`:1554`, `:2058`, `:2603`) are *function parameters*, not local variables, on the recursive retry helpers `_invoke_with_retry`, `_generate_embedding_with_retry` and `_invoke_lambda_hook_with_retry`; each is threaded down the recursion at the `last_exception=e` call sites and **never loaded** (verified by AST: zero `Name`-in-`Load` occurrences). Nothing is swallowed because every exhaustion path ends in a **bare `raise`** (`:1706` and `:1792` in the first helper, `:2165`, `:2746` and `:2768`), which re-raises the exception currently being handled in that frame — i.e. the most recent attempt's — which is what the parameter was presumably meant to supply. Dead code worth deleting; not an error-handling defect |
| 2026-09 | "108 of 109 log groups are encrypted", i.e. one unencrypted log group | The **figure** is withdrawn as a conflation of two different statistics over the same population, and it carries a **scope trap** worth recording: 106 of 109 declare `KmsKeyId` and 108 of 109 take `RetentionInDays` from a parameter, and the denominator 109 only reproduces if you restrict to `template.yaml` (56), `patterns/unified/template.yaml` (20) and `nested/api-resolvers/template.yaml` (33). Against `scripts/discover_templates.sh cfn`'s **30** templates it is **158** log groups, 131 with `KmsKeyId` and 145 parameterised — so quoting "109" without naming the three-template scope is not reproducible. ⚠️ **Only the statistic is withdrawn, not the gap.** `HttpApiDispatcherLogGroup` (`nested/api-resolvers/template.yaml:2876`) is the sole exception on retention (hardcoded `30`) and one of *three* on encryption, and unlike the other two (`StacknameCheckFunctionLogGroup`, `ReadPreviousIDPPatternFunctionLogGroup`, which each carry a `cfn_nag` W84 suppression and a `checkov:skip` with a reason) it carries no suppression, comment or test saying the deviation is deliberate. That is a live finding, addressed by PR **#973** |
| 2026-09 | `TEMPLATES` in `scripts/tests/test_lambda_log_groups.py:113` and `CMK_TEMPLATES` in `scripts/tests/test_log_group_encryption.py:143` are hardcoded template inventories with no cross-check | Both files **do** enumerate from the tree — each through one `scripts/tests/repo_files.py` `tracked_paths` call, at `:792` and `:514` — and both carry universe-closure tests, so a template added to the repo and not to the list fails rather than being silently uncovered. The lists are a second filter on an already-derived set, not the record of what should be there. They were reported because the Class 2 inventory search's `WALKS` pattern did not know about `tracked_paths`, which is the repo's third content-discovery helper after `discover_templates.sh` and `run_all_tests.py`; the pattern now names it. If you see either of these come back `ONLY RECORD` again, the regex has gone stale, not the tests |
| 2026-09 | `scripts/tests/test_cfn_export_ownership.py` needs an append-only snapshot of host export names, so that withdrawing or moving one forces a deliberate acknowledgement | The property is already enforced, twice, and `PINNED_PRODUCERS` *is* that snapshot: `test_every_host_export_is_pinned` compares the produced suffixes against it **in both directions**, so a withdrawn name fails, and `test_host_exports_keep_their_pinned_producer` compares each name's producing template against the pin, so a move fails. Verified by probe — renaming one `Export.Name` in `feature-platform/main-stack-extensions/template.yaml` fails **three** tests. A second artifact would duplicate a passing check and add a third place to keep in step. ⚠️ **The gap the review actually found is a different one, and a snapshot does not close it:** `MARKETPLACE_EXTENSION_HOST_IMPORTS` records which *closed-source* extension imports which name, is a documented **lower bound** rather than a census, and is used only to cross-check manifest `featureId`s and to enrich a failure message — never to assert a template property. Those templates live in another repository, so nothing readable from here can enumerate them; an artifact that looked authoritative about consumers would be trusted more than it deserves. Leave the lower bound, labelled as one |

When you withdraw a finding, **add a row here in the same PR as the report**, dated,
with the reason in one sentence. When you keep a finding that looks like one of these,
say explicitly why this instance differs.

**This register expires.** At the start of each run, re-check every row and **delete
the ones whose justification no longer holds** — the vendoring guard removed, the lazy
loader rewritten, the log-group assertion dropped. A row is a licence to skip a check,
so a stale row suppresses a real finding, which is worse than having no register at
all. `full-test-battery.md` retired its entire 26-entry list on exactly that argument;
this table should stay short for the same reason. If a row survives several runs
unchanged, prefer moving its justification into a test that fails when it stops being
true, and delete the row.

## Limitations — what this review cannot establish

Every measurement here is **static and offline**. That is a deliberate design choice —
it makes the review re-runnable by anyone, with no credentials and no deployed stack —
but it draws a hard boundary that the report must respect rather than blur.

Nothing offline can establish **runtime behaviour**. Reading the template does not tell
you whether an X-Ray annotation actually arrives at the service, whether a deployed
authorizer actually denies the request, whether an alarm actually fires and delivers,
whether a log group is actually encrypted at rest, or which npm version the build host
actually runs. Those are all things the *code shape* strongly implies and only a live
check can confirm.

This matters because several of this skill's own worked examples are exactly that:
runtime claims reasoned out from static structure. The `onError: fail` routing, the
`SubIndex` query raising `ValidationException`, the `min-release-age` key being ignored
by the pinned npm, and the set-literal X-Ray annotation being unusable are all
**`inferred`**, not `measured`, however confident the reasoning looks. The `SubIndex`
one makes the point twice over: it was fixed on the strength of the inference alone,
so whether the deny path it now takes ever ran against a real DynamoDB endpoint is
*still* unmeasured. Label them that
way per the output contract's rule 3, and where a live check would settle it, say which
check. Do not run it — say it.

## Output contract

The report is a ranked findings list. Four rules, all non-negotiable:

1. **Rank by consequence, not by severity label.** "What breaks, for whom, and how
   would we find out?" A P3-looking typo in a `pyrightconfig.json` path that removes
   6,791 lines from type checking outranks a genuine but contained `Resource: "*"`.
   Do not sort by a CVSS-shaped adjective; sort by blast radius and detectability, and
   say which is which. Put the two defect **classes** above their instances — the
   class is the finding, the instances are its evidence.
2. **Every finding carries `file:line` evidence.** A path alone is not evidence; a
   line number is. For an absence (a control nothing reads) cite the line that *should*
   read it and the search that found nothing — "the artifact is at X:12; the decision
   is made at Y:275 and does not reference it; `grep -rn <name>` returns only
   definitions and tests".
3. **Label every claim `measured`, `inferred`, or `unverified`.** Measured means you
   ran the command and are quoting its output. Inferred means you read the code and
   reasoned. Unverified means you believe it and could not confirm — which is a
   perfectly acceptable thing to ship, *labelled*. Never let an inference inherit the
   authority of a measurement, and never quietly drop an unverified item because it
   was awkward to caveat.
4. **State the withdrawals.** Findings you investigated and dropped go in the report
   with the reason, and into the known-non-defects table above. A report that only
   contains confirmed defects hides the work and guarantees the next run repeats it.

Suggested report shape:

```markdown
# Repo Quality Review — <commit> / VERSION <x.y.z> — <date>

## Baseline measurements
<the measurement table, with the delta against the previous report where one exists>

## Findings, ranked by consequence
### 1. <Class 1> Controls that exist but are never consulted     [measured]
<the shape, then each instance with file:line>
### 2. <Class 2> Fixes applied to the instance, not the class    [measured]
...
### N. <single finding>                                          [inferred]

## Investigated and withdrawn
| Suspected | Verdict | Why |

## Not assessed
| Dimension | Why |
```

## Reporting back to the user

Lead with the two classes and the count of instances of each, then the baseline
numbers that moved since the last run, then the ranked list. Say plainly how the
review was run (how many subagents, or single-agent) because that determines how much
weight the cross-dimension conclusions carry.

Two things never to do. Do not report a measurement without saying which command
produced it — a number nobody can re-derive is a claim, not a measurement. And do not
close the report with fixes: this skill's deliverable is the list, and the user
decides what gets filed and in what order. Offer to file the issues; do not file them.
