# PR / MR Review Skill

Use this skill when the user asks to **review a pull request or merge request** —
typically with a prompt like:

> `review <MR/PR URL>`  (target develop branch)
>
> Is this a good PR?
> Safe? No regressions?
> Good UX?
> No security issues?
> Well documented?
> Safe to merge?

This skill is for reviewing **someone else's** PR/MR at a URL. It is distinct
from the code-review skill (the pre-commit self-review checklist applied to my
own changes before returning them to the user).

## Ground Rules

1. **Never push, merge, approve, or comment on the PR** from this skill unless
   the user explicitly asks. Produce a written review for the user to act on.
2. **Do not modify files** in the working copy as part of the review. Reviewing
   is read-only.
3. Reference specific **files and line numbers** whenever citing an issue.
4. If the PR targets a branch **other than `develop`**, call this out as the
   first finding. The expected target for this repo is `develop`.
5. Keep findings factual and specific — quote the diff where useful.

## Step 1 — Detect the URL and fetch PR metadata

Identify the host from the URL.

### GitHub PR (`github.com/<owner>/<repo>/pull/<NN>`)

Prefer the `gh` CLI via Bash:

```bash
gh pr view <NN> --repo <owner>/<repo> --json \
  title,body,author,state,mergeable,mergeStateStatus,baseRefName,headRefName,\
  additions,deletions,changedFiles,files,statusCheckRollup,reviews,comments,url

gh pr diff <NN> --repo <owner>/<repo>
gh pr checks <NN> --repo <owner>/<repo>
```

If a GitHub MCP server is configured (e.g. for Cline), use its
`pull_request_read` methods instead of the shell — they are equivalent:
- `method=get` → title, body, author, base/head refs, mergeable state
- `method=get_status` → CI / checks status on the head commit
- `method=get_files` → changed files (paginate if > 100)
- `method=get_diff` → unified diff
- `method=get_reviews` → existing reviews
- `method=get_comments` and `method=get_review_comments` → discussion
- `get_file_contents` on specific paths at `ref=refs/pull/<NN>/head` to read
  full files when the diff lacks enough context

### GitLab MR (`gitlab.aws.dev/<group>/<project>/-/merge_requests/<NN>`)

Use the `glab` CLI:

```bash
glab mr view <NN>   --repo <group>/<project>
glab mr diff <NN>   --repo <group>/<project>
glab mr note list <NN> --repo <group>/<project>
glab ci status      --repo <group>/<project>
```

Fallback — `curl` against the GitLab API:

```bash
PROJECT=$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote_plus(sys.argv[1]))" \
  "<group>/<project>")
TOKEN="${GITLAB_TOKEN:-$(yq -r .token ~/.config/glab-cli/config.yml 2>/dev/null)}"
curl -sH "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.aws.dev/api/v4/projects/$PROJECT/merge_requests/<NN>"
curl -sH "PRIVATE-TOKEN: $TOKEN" \
  "https://gitlab.aws.dev/api/v4/projects/$PROJECT/merge_requests/<NN>/changes"
```

### Collect at minimum

- PR/MR title, description, author
- Source branch → target branch (confirm target is `develop`)
- CI / pipeline status
- List of changed files with insertions / deletions
- The full diff
- Existing reviews and comments
- Linked issues, if any

For larger PRs (> ~500 lines changed or touching unfamiliar modules), read key
changed files in full — not just the diff — using the `Read` tool on paths at
the head ref so surrounding context is captured.

## Step 2 — Evaluate the six review questions

Work through each question. Reuse project coding-standards knowledge from the
other skill files in this directory (backend, frontend, infrastructure,
documentation, testing, code-review).

### 1. Is this a good PR?
- Scope is focused (single feature / fix / refactor; not a grab-bag)
- Title is descriptive and follows convention (e.g. `feat:`, `fix:`, `docs:`)
- Description explains **what** and **why**, and links to an issue when applicable
- Targets `develop` (flag otherwise)
- CI is green (or failures are understood)
- Size is reviewable — very large PRs should be called out

### 2. Safe? No regressions?
- New/changed logic has **unit tests**; assertions are meaningful
- Existing tests are not deleted or weakened without justification
- Backward compatibility maintained for shared interfaces:
  - `Document` model and its serialization
  - Config schemas under `config_library/`
  - GraphQL schema and generated types in `src/ui/src/graphql/generated/`
  - Lambda handler event/response contracts
- Quick Start still works? It is a new user's first experience, and it calls
  surfaces other pages own, so it breaks with no failing test. If the PR changes
  the agent chat route or stream, configuration profile creation or activation,
  the discovery upload resolvers, schema authoring or the class catalog, the
  Welcome page, side navigation or tour, the Test Set Generator extension, the
  groups a role holds, or the stack wiring Quick Start reads (the
  `EnableQuickStartWidget` parameter and the installed-features SSM parameter),
  check in the diff that Quick Start still handles any contract it changes. If it
  renames a page, navigation item or product term, check that the Quick Start
  agent's prompt (`lib/idp_common_pkg/idp_common/agents/quick_start/agent.py`),
  `docs/quick-start.md` and its stories in `scripts/ux_stories.toml` still match.
  A change the diff shows Quick Start's side cannot handle (a field it reads
  renamed or removed, a requirement it does not meet) is 🔴; a backward-compatible
  change needs nothing; one that may break it, or a prompt, page or story that no
  longer matches, is 🟡. Where the PR description or commit messages are available,
  also note whether they say how the Quick Start journey was exercised: open it,
  describe a document, approve the drafted fields, profile created and activated.
  The paths are in `.claude/rules/quick-start.md`
- No obvious race conditions, unbounded loops, or missing pagination
- Error handling uses explicit exceptions (no bare `except:`), with `exc_info=True`
- No removal of observability (X-Ray `patch_all`, structured logging)

#### Terraform parity

Every release also ships a Terraform module generated from the CloudFormation
templates (`terraform/translator/`, documented in `docs/terraform.md` and
`terraform/README.md`), so a template change is a Terraform change too. Check
this whenever the PR touches `template.yaml`, `nested/**`, `patterns/**`
templates, `terraform/**`, the Terraform paths of `idp_sdk`/`idp_cli`, or
runtime code that looks resources up through CloudFormation.

- **The `Terraform Checks` CI context is green.** `make terraform-check`
  translates every template variant offline, validates the module on every
  pinned Terraform and OpenTofu version, then runs `make terraform-scan-diff`
  (below). If it has not run yet, run it on the PR head; it needs no AWS
  credentials. The translator's own checks run in `make test` (the
  `terraform/translator` suite) and fail the PR on any of the cases in the next
  bullets.
- **A new resource type, or a property shape the translator does not handle,**
  fails that gate on purpose. The fix belongs in the translator (a mapping in
  `aws_mappers*.py` or the vendored `awscc` schema subset, refreshed with
  `scripts/refresh_awscc_schema.py`, and the create-only list, refreshed with
  `scripts/refresh_create_only.py`), with a translator test. Flag an entry
  added to `AWSCC_PROPERTY_FIXUPS`, `LIFECYCLE_OVERRIDES`,
  `STABLE_CUSTOM_OUTPUTS`, `PASS_THROUGH_ATTRS` or `TERRAFORM_DIRECTORY_READERS`
  in `translate.py`, to a hand mapper's `ignore_changes` (`SpecMapper` in
  `aws_mappers.py`, or a lifecycle block a mapper class emits itself), or to
  `completeness.NOT_APPLICABLE`, without a written reason and a test: each one
  is a place where Terraform deliberately differs from CloudFormation. A
  mapper's `ignore_changes` is right only for a property the service cannot
  change after creation, or one CloudFormation never updates; anywhere else it
  hides a real change.
- **A property value that does not reach the Terraform.** The completeness check
  (`idp_tf/completeness.py`) fails when a value in the template appears nowhere
  in the HCL emitted for its resource, which is how a hardening setting is lost
  silently. The right fix is almost always the mapper; a `NOT_APPLICABLE` entry
  must say where the value went instead.
- **Upgrade safety.** A Terraform upgrade replaces a resource wherever
  CloudFormation would, and in a few places CloudFormation would not. For a
  stateful resource (every type in `idp_tf/safety.py`'s `STATEFUL_TYPES`:
  tables, buckets, user pools, KMS keys, knowledge bases and data sources, the
  OpenSearch collection, the secret, the vector-store custom resources, and a
  parameter with a second writer) check each of these and flag a 🔴 where the
  PR does not handle it:
  - **a renamed logical ID or nested stack** needs an entry in
    `terraform/translator/renames.yaml` (`from`, `to`, `since`, `reason`), from
    which the release's translation writes a `moved` block. A resource moved
    between nested stacks with its logical ID unchanged is inferred, unless
    the same logical ID and type exist elsewhere; then it needs an entry too;
  - **a changed create-only property** (a name, a key schema, an encryption
    key) replaces the resource under CloudFormation too: ask what happens to the
    data on upgrade;
  - **a create-only property fed by a custom resource's output**, directly or
    through parameters, outputs or another resource. The translator lists every
    such edge (`customOutputEdges` in `idp-terraform.json`, and a
    `custom-output edge:` line in its output) and fails on one into a stateful
    resource unless `STABLE_CUSTOM_OUTPUTS` says why the handler returns the
    same value on every Update. Read that reason against the handler: it is a
    claim about the handler's code.

  `idp-cli deploy --terraform` refuses a plan that deletes or replaces any
  stateful or retained address (`statefulAddresses` and `retainedAddresses`),
  so a missed case is refused at deploy rather than lost; that is a backstop,
  and an upgrade a user has to override with
  `--terraform-allow-replace-stateful` is still a defect in the PR.
- **Softer than CloudFormation.** `make terraform-scan-diff` runs a pinned
  `checkov` over the CloudFormation templates and the generated module and fails
  on a Terraform finding the CloudFormation side does not have. A new entry in
  `scripts/terraform_scan_baseline.json` needs a reason for that one check on
  that one resource; prefer fixing the translation.
- **Runtime lookups through CloudFormation.** Code calling
  `DescribeStacks`/`ListStackResources`/`DescribeStackResource(s)`, or filtering on
  the `aws:cloudformation:stack-name` tag, gets no answer from a Terraform
  deployment unless it goes through `idp_common.utils.terraform_stack`
  (`install_if_terraform()`), its function is listed in
  `TERRAFORM_DIRECTORY_READERS`, and it filters on `idp:stack-name` when
  `IDP_TF_RESOURCE_DIRECTORY` is set (see `src/lambda/data_mart_rollup/`).
- **New stack parameters** become Terraform variables with the same
  constraints and appear read-only on the label stack. `NoEcho` parameters must
  stay `NoEcho`, which keeps them off the label stack.
- **Docs.** If the change makes a Terraform deployment behave differently from a
  CloudFormation one, `docs/terraform.md` says so (its "Differences from
  CloudFormation" section).

### 3. Good UX?
- **Frontend** (`src/ui/`):
  - Cloudscape Design System components (not MUI / AntD / Bootstrap)
  - Loading, empty, and error states handled
  - Arrow-function components; hooks in kebab-case files
  - `ConsoleLogger` used, no stray `console.log`
  - Accessibility: labels on inputs, keyboard nav, focus management
  - `DOMPurify` used for any `dangerouslySetInnerHTML`
  - Responsive behavior reasonable
- **Backend / API**:
  - Actionable error messages; failures surface to the UI (e.g. status on `Document`)
  - Progress / status updated at meaningful checkpoints
  - Sensible defaults; new required config has migration guidance

### 4. No security issues?
- No hardcoded credentials, API keys, or tokens
- No full JWTs logged in plaintext (Talos finding #10)
- No hardcoded `arn:aws:` partitions — must use `${AWS::Partition}`
  (validated by `make check-arn-partitions`)
- No hardcoded `amazonaws.com` — must use `${AWS::URLSuffix}`
- IAM policies scoped — no `Resource: "*"` or wildcard S3 ARNs without
  written justification
- `PermissionsBoundary` conditional present on new IAM roles
- New IAM role or AWS service introduced? → `docs/aws-services-and-roles.md` updated to match
- New Lambdas have a dedicated `AWS::Logs::LogGroup` with KMS encryption
- Input validation on new API surfaces; DOMPurify on HTML render paths
- New Python/npm dependencies are pinned and from trusted sources
- `cfn-nag` / `checkov` suppressions include `reason:` metadata
- No new public S3 buckets, public SNS topics, or open security groups

#### Agent capability

Apply this when the change touches any of: a Strands tool or agent, a system or
user prompt, a model call, an MCP tool or client, a markdown renderer
(`SafeMarkdown`, `safe-markdown-policy.ts`, anything rendering model output), the web
UI content policy, or the IAM grants of a role that runs a model. For each surface
the change touches, answer three questions from the code, not from the PR text:

1. **What can the model read?** Which tools does it hold, what do they reach with
   whose credentials, and which data is private (documents, reporting tables,
   another user's chat, logs, CI credentials)?
2. **What document-derived or external text reaches it?** Document text and page
   images, extracted values, reporting rows, log lines, discovered configuration,
   MCP server responses, a PR's own diff. Any of these can carry instructions.
3. **Where can its output go?** A rendered page that can request remote resources,
   the Code Interpreter sandbox, an MCP client or server, a PR comment, a saved
   configuration that a later model reads.

When all three hold at once (untrusted text in, private data readable, an external
channel out), something in the code must limit each external channel. A system
prompt instruction is not such a limit.

Then check that `scripts/tests/agent_capabilities.yaml` was updated to match: a new
tool or agent has an entry, a changed tool updates its entry's `reads`, `inputs` or
`outputs`, and a removed control is not still named. `test_agent_capabilities.py`
fails on an unregistered surface or a missing symbol, but it checks declarations,
not truth: an entry that understates what a model can read passes it. A manifest
that was not updated when the capability changed is 🟡 at least; a new
combination of all three with no control and no recorded `finding` is 🔴.

#### SRT security scan (`make srt-scan`)

CI runs the `srt_security_review` job on every MR targeting `develop`. Check
the pipeline first: if the job already ran, read its result and the
`srt-issues.json` artifact instead of re-running the scan.

Run the scan locally when the CI job hasn't run yet (draft MR, non-`develop`
target, pipeline still pending) **and** the PR changes code, templates, or
dependencies — skip it for docs-only PRs. SRT scans the **checkout**, not the
diff, so run it on the PR head in a throwaway worktree (this also keeps the
review read-only for your own working copy):

```bash
git fetch origin <headRef> && git worktree add /tmp/pr-<NN> FETCH_HEAD
cd /tmp/pr-<NN> && make srt-setup && make srt-scan   # ~5–10 min
# … read results, then clean up:
cd - && git worktree remove --force /tmp/pr-<NN>
```

Interpreting results:
- Outside CI the scan **exits 0 even with findings** and prints an
  `OPEN HIGH PRIORITY SECURITY ISSUES` table — read the output (or
  `.srt/issues.json`), don't rely on the exit code.
- Attribute a finding to the PR only if it's in a file the PR touches; HIGH
  `Open` findings in changed files are 🔴 blocking.
- New or modified suppressions in `scripts/srt/issues.json` must carry a
  specific justification — flag unexplained bulk suppressions as 🔴.

### 5. Well documented?
- User-facing changes update `CHANGELOG.md`
- Feature docs added/updated under `docs/` with:
  - YAML frontmatter (`title:` required)
  - License header after frontmatter
- Cross-references to related docs added where helpful
- Public functions/classes have docstrings
- Complex logic has inline comments explaining *why*
- If GraphQL schema changed, `make codegen` has been run and generated files
  are included in the PR (or a note explains why not)

### 6. Safe to merge?
- Targets `develop` ✅
- CI / pipeline green ✅
- No unresolved review comments on the latest commit
- No merge conflicts
- `VERSION` / version bump done if required by change scope
- No leftover `TODO` / `FIXME` / debug prints / commented-out code blocks
- Migration notes provided for any breaking change

## Step 3 — Produce the review

Output the review in this exact structure so the user can paste it or act on it:

```markdown
## PR/MR Review: <title> (#<NN>)
**Repo:** <owner/repo>
**Author:** @<author>
**Source → Target:** `<source>` → `<target>`   <!-- ⚠️ if not develop -->
**CI status:** ✅ passing | ❌ failing | ⏳ pending
**Size:** +<insertions> / -<deletions> across <N> files

### Summary
<1–3 sentence plain-English description of what this PR does.>

### Verdict

| Question | Verdict | Notes |
|---|---|---|
| Good PR? | ✅ / ⚠️ / ❌ | … |
| Safe / no regressions? | … | … |
| Good UX? | … / N/A | … |
| No security issues? | … | … |
| Well documented? | … | … |
| **Safe to merge?** | ✅ Approve / ⚠️ Request changes / ❌ Block | … |

### Findings
- 🔴 **Blocking** — <file>:<line> — description + suggested fix
- 🟡 **Should fix** — <file>:<line> — description
- 🟢 **Nice to have** — <file>:<line> — description

### Recommendation
**Approve** / **Request changes** / **Comment**

<one-paragraph rationale>
```

Legend:
- ✅ pass / 🟢 nit — non-blocking
- ⚠️ / 🟡 — should fix before merge
- ❌ / 🔴 — blocking; must fix before merge

## Step 4 — After delivering the review

- Do **not** post the review as a PR/MR comment unless the user explicitly
  said "post this as a review comment" or similar.
- If the user asks follow-up questions, re-use the already-fetched diff /
  metadata rather than re-fetching.
- If the user asks you to implement the requested changes, switch context:
  treat it as a new implementation task following the relevant skill files.

## Quick Reference — Common Red Flags

Flag these immediately when seen in any diff:

- `arn:aws:` literal in a CloudFormation / SAM template
- `amazonaws.com` literal (should be `${AWS::URLSuffix}`)
- `Resource: "*"` in a new IAM policy statement
- `Principal: "*"` in a new resource policy
- `console.log(` in production UI code
- `print(` used for logging in Lambda code
- New Lambda without dedicated LogGroup + KMS
- New IAM role without a `PermissionsBoundary` conditional
- Secrets / access keys / JWTs in code, logs, or tests
- Deleted tests without a replacement
- Schema change in `nested/api-resolvers/src/api/schema.graphql` without
  regenerated files under `src/ui/src/graphql/generated/`
- `Document` model field removed or renamed without a migration path
- Wildcards in S3 bucket/object ARNs without `reason:` metadata
- New/edited SRT suppressions in `scripts/srt/issues.json` without justification
- A template change that renames a logical ID or nested stack of a stateful
  resource without a `terraform/translator/renames.yaml` entry, or changes a
  create-only property of one with nothing said about upgrading existing
  Terraform deployments
- A new `AWSCC_PROPERTY_FIXUPS`, `LIFECYCLE_OVERRIDES`, `STABLE_CUSTOM_OUTPUTS`,
  `PASS_THROUGH_ATTRS` or `TERRAFORM_DIRECTORY_READERS` entry, a hand mapper's
  `ignore_changes`, or a `completeness.NOT_APPLICABLE` or
  `scripts/terraform_scan_baseline.json` entry, without a reason and a test
- Runtime code that walks the stack through CloudFormation APIs or the
  `aws:cloudformation:stack-name` tag without the Terraform resource-directory path
