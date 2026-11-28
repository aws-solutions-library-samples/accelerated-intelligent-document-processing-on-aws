# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Agentic root-cause analysis for a failed SDLC pipeline run.

WHY THIS EXISTS
---------------
`generate_deployment_summary` in codebuild_deployment.py makes ONE Bedrock call
with a fixed evidence window: the Step Functions failure list plus the last 150
lines of the build log. That window is often pointed at the wrong thing. In the
2026-09-08 run (job 28666687) the real cause — Step 11's
`IDPProcessingError: Timeout waiting for test run ... after 300s` — sat ~1,090
lines from the end of a 3,741-line log, so the 150-line tail contained only the
concurrent stack teardown's bucket/log-group inventory. The model correctly
reported "root cause not captured", because it genuinely had nothing.

Widening that window does not fix the class of failure. Two reasons:

1. Roughly 45% of a build log is mechanical noise (820 lines of pip output, 759
   lines of teardown/resource-discovery chatter, 118 lines of Rich box-drawing
   in that same run), and because Steps 3-10/13-14 run concurrently against one
   shared stack and write to one CloudWatch stream, the log is not linearly
   readable — two steps' sentences interleave mid-word. More raw text amplifies
   that rather than clarifying it.
2. Some root causes are not in the build log AT ALL. The Step 11 timeout needed
   the corroborating fact that Step 7's run against the same test set was also
   unfinished at ~305s, plus `git log -S` to establish that the 300s budget
   predated the commit that parallelized the step. That is evidence GATHERING,
   which a single-shot summarizer structurally cannot do.

So this module runs Claude Code headless (`claude -p`) against Bedrock, hands it
a deterministic brief plus the full log ON DISK, and lets it grep the log, read
the repo, and query live AWS. Feeding the log as prompt tokens instead would
cost ~110-125K tokens (~$0.55-0.65/failure) to make the model read
`Collecting botocore` at full rate; a file it greps costs almost nothing.

SAFETY MODEL
------------
The CodeBuild role (`genaiic-sdlc-role`) already carries `PowerUserAccess` plus
a broad IAM-write policy — see scripts/sdlc/cfn/sdlc-iam-role.yml, whose own
description says "TODO: Refine this role to be least privilege". So the agent
does not NEED a permission grant; it inherits effectively-admin credentials. The
risk runs the other way, and it is not hypothetical: the CI account is SHARED
with concurrent pipelines. That is why the stale-resource reapers in
codebuild_deployment.py are age-gated (APIGW_VPC_STALE_AGE_SECONDS = 2h,
IDP_STACK_STALE_AGE_SECONDS = 3h) — "so a concurrent pipeline's in-flight
buckets are never touched". An agent with unconstrained write and no such
discipline could delete another running pipeline's stack.

Defence in depth, therefore:

* IAM (the real boundary): the agent runs with credentials from
  `sts:AssumeRole` into a read-only role, `IDP_FAILURE_AGENT_ROLE_ARN`. Nothing
  it does can mutate the account regardless of what it decides to try. When the
  variable is unset or the assume fails, the agent does not run at all and the
  caller uses its single-shot summary; it never runs on the job's own
  credentials (`_readonly_credentials`).
* Environment: the child gets an allowlisted environment (`child_env`): PATH, a
  private HOME, the locale, a proxy if one is set, the region, the assumed
  role's three values and the CLI's own settings. Nothing else in the job's
  environment is passed, the container-credential variables CodeBuild sets
  (`AWS_CONTAINER_CREDENTIALS_*`) included, so the job's role is not reachable
  through the SDK's credential chain.
* Harness: `claude_command` carries the same session-scope flags as the AI
  review (`SESSION_SCOPE_FLAGS`, `--strict-mcp-config`, `--permission-mode
  manual`), so no settings, CLAUDE.md, hooks or MCP servers load from the
  checkout and the file tools stay inside the working directory and the log
  directory. `--disallowedTools` strips the write tools, and `--allowedTools`
  lists only read-only `aws` and `git` verbs. Those Bash rules are prefix
  matches, so options that turn a read into a write or a request elsewhere are
  refused by deny rules (`_DISALLOWED_TOOLS`): `--output` for git, which writes a
  file, and `--endpoint-url` for aws. On the pinned CLI a command chained,
  piped, redirected or substituted after an allowed prefix is refused as well;
  `scripts/sdlc/tests/test_failure_agent_session_scope.py` measures both. The
  allowlist still reduces noise more than it bounds anything; the read-only role
  is what bounds what a command can change.
* Output: the report is passed through the repository's redactor
  (`redact_report`) before it is returned, because the caller publishes it to
  S3, the failure email and the team's chat channel.
* Blast radius on failure: this module never raises and never decides pass/fail.
  It returns a string or None. Python owns the verdict — the prompt-based
  routing that misclassified failures and leaked its scratchpad into the summary
  is recorded at codebuild_deployment.py's `generate_deployment_summary`
  docstring, and this must not reintroduce it.

Opt-in via IDP_FAILURE_AGENT=1 for now, so a slow or flaky agent cannot
destabilise a pipeline that gates merges. With it off, the caller keeps today's
single-shot behaviour, still improved by `build_evidence_brief` below.
"""

from __future__ import annotations

import functools
import json
import os
import pathlib
import shutil
import subprocess  # nosec B404 - fixed argv, no shell, no user input
import sys
import time
from collections.abc import Mapping

# The redactor lives in scripts/ux_runner. Appended, so a name in scripts/ can
# never stand in for a standard-library module.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.append(str(_SCRIPTS_DIR))

from ux_runner import redact  # noqa: E402

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Opt-in. Set IDP_FAILURE_AGENT=1 in the pipeline env to enable.
AGENT_ENABLED = os.environ.get("IDP_FAILURE_AGENT", "0").lower() in ("1", "true")

# Pinned so a surprise npm release cannot change CI behaviour mid-week, and kept
# equal to the AI review's CLAUDE_CODE_VERSION (asserted in
# scripts/sdlc/tests/test_failure_agent.py), so both harnesses run the version
# whose session-scope flags are measured. Every flag claude_command passes
# exists there; scripts/sdlc/tests/test_failure_agent_session_scope.py measures
# what the argv admits and refuses on it. --permission-prompts is load-bearing:
# without it a tool call outside the allowlist waits on a prompt nobody can
# answer, and the agent hangs until the wall clock.
CLAUDE_CODE_NPM_SPEC = os.environ.get(
    "IDP_FAILURE_AGENT_NPM_SPEC", "@anthropic-ai/claude-code@2.1.281"
)

# Same model the existing single-shot summary uses (_invoke_bedrock), so this
# introduces no new Bedrock model-access requirement. On Bedrock, background
# tasks follow the primary model unless a Haiku model is pinned, hence the
# explicit ANTHROPIC_DEFAULT_HAIKU_MODEL below — otherwise session-title
# generation and similar chores would also bill at Opus rates.
AGENT_MODEL = os.environ.get("IDP_FAILURE_AGENT_MODEL", "us.anthropic.claude-opus-4-8")
AGENT_HAIKU_MODEL = os.environ.get(
    "IDP_FAILURE_AGENT_HAIKU_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)


_UPDATE_CONFIGURATION_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "lambda"
    / "update_configuration"
)


@functools.cache
def _update_configuration_module():
    """The UpdateConfiguration Lambda's module, loaded from source.

    The module sets the root logger's level on import, which is put back.
    """
    import importlib.util
    import logging

    root_level = logging.getLogger().level
    added = str(_UPDATE_CONFIGURATION_DIR) not in sys.path
    if added:
        # Its own cfnresponse.py sits beside it.
        sys.path.insert(0, str(_UPDATE_CONFIGURATION_DIR))
    try:
        spec = importlib.util.spec_from_file_location(
            "_ci_update_configuration", _UPDATE_CONFIGURATION_DIR / "index.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {_UPDATE_CONFIGURATION_DIR / 'index.py'}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(str(_UPDATE_CONFIGURATION_DIR))
        logging.getLogger().setLevel(root_level)
    return module


def regional_model_id(model_id: str, region: str | None = None) -> str:
    """`model_id`, or the model the product maps it to for the build's region.

    The CI analysis models are written as `us.` profiles, which Bedrock refuses
    as invalid from an EU region. The mapping is the product's own
    (get_model_mapping in the UpdateConfiguration Lambda), so the harness calls
    what a stack deployed in that region would: in the EU that is sometimes a
    different model rather than the same one re-prefixed. ca-central-1 calls the
    `us.` Claude profiles directly (docs/canada-region-model-support.md), and the
    Canada table leaves them as written. Any other region keeps the id.
    """
    region = (
        region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    )
    region_type = (
        "eu" if region.startswith("eu-") else "ca" if region.startswith("ca-") else None
    )
    if region_type is None:
        return model_id
    return _update_configuration_module().get_model_mapping(model_id, region_type)


# Three independent caps. --max-turns and --max-budget-usd are enforced by the
# CLI (print mode only); the wall clock is ours, because a single turn can stall
# on a slow AWS call and neither CLI cap would fire.
AGENT_MAX_TURNS = int(os.environ.get("IDP_FAILURE_AGENT_MAX_TURNS", "40"))
AGENT_MAX_BUDGET_USD = os.environ.get("IDP_FAILURE_AGENT_BUDGET_USD", "5.00")
AGENT_WALL_CLOCK_SECONDS = int(os.environ.get("IDP_FAILURE_AGENT_TIMEOUT", "900"))

# Read-only role for the agent to assume. When unset, or when the assume fails,
# the agent is skipped and the caller falls back to its single-shot summary: it
# never runs on the job's own credentials.
AGENT_ROLE_ARN = os.environ.get("IDP_FAILURE_AGENT_ROLE_ARN", "")

WORKDIR = "/tmp/idp-failure-agent"  # nosec B108 - CodeBuild container, ephemeral
AGENT_HOME = "/tmp/idp-failure-agent-home"  # nosec B108 - as above

# Read-only AWS verbs the agent may run unprompted. Deliberately narrow: these
# are the services that actually hold IDP failure evidence.
_ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(aws logs describe-log-groups*)",
    "Bash(aws logs describe-log-streams*)",
    "Bash(aws logs get-log-events*)",
    "Bash(aws logs filter-log-events*)",
    "Bash(aws logs start-query*)",
    "Bash(aws logs get-query-results*)",
    "Bash(aws stepfunctions list-executions*)",
    "Bash(aws stepfunctions describe-execution*)",
    "Bash(aws stepfunctions get-execution-history*)",
    "Bash(aws cloudformation describe-stacks*)",
    "Bash(aws cloudformation describe-stack-events*)",
    "Bash(aws cloudformation describe-stack-resources*)",
    "Bash(aws dynamodb query*)",
    "Bash(aws dynamodb scan*)",
    "Bash(aws dynamodb describe-table*)",
    "Bash(aws s3 ls*)",
    "Bash(aws s3api list-objects-v2*)",
    "Bash(aws lambda get-function-configuration*)",
    "Bash(aws sqs get-queue-attributes*)",
    "Bash(aws sts get-caller-identity*)",
    "Bash(git log*)",
    "Bash(git show*)",
    "Bash(git diff*)",
    "Bash(git blame*)",
]

# The built-in tools the session has at all (`--tools`). `--restricted` removes
# Bash unless it is named here, and the agent needs it for `aws` and `git`.
_SESSION_TOOLS = ("Read", "Grep", "Glob", "Bash")

# Bare names remove the tool from the agent's context entirely. The Bash rules
# refuse options the prefix rules above would otherwise admit, and a deny rule
# wins over an allow rule:
#   * `--output` / `--output=<path>` is a diff option `git log`, `git show` and
#     `git diff` all accept, and it writes the output to that path;
#   * `--endpoint-url` sends an `aws` request to a host other than the service's.
_DISALLOWED_TOOLS = [
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebSearch",  # unavailable on Bedrock anyway
    "WebFetch",
    "Task",
    "Bash(git *--output*)",
    "Bash(aws *--endpoint-url*)",
]

# Flags that confine the session, the same pair the AI review uses
# (scripts/sdlc/ai_mr_review.py::SESSION_SCOPE_FLAGS). `--restricted` confines
# Read, Grep and Glob to the working directories and ignores the user, project
# and local settings files; `--safe-mode` stops CLAUDE.md, rules, skills,
# commands, agents and hooks from loading. Both exist on the pinned CLI above,
# and scripts/sdlc/tests/test_failure_agent_session_scope.py runs this argv on it.
SESSION_SCOPE_FLAGS = ("--restricted", "--safe-mode")

# Named on the command line so that a settings file's `defaultMode` cannot widen
# the allowlist; with `--permission-prompts none` anything outside it is denied.
PERMISSION_MODE = "manual"

# The only variables of the job's environment the child receives, by name. The
# credentials and the CLI's own settings are set by `child_env` itself.
_PASSED_ENV_KEYS = (
    "PATH",
    "LANG",
    "LC_ALL",
    "TZ",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

# Job variables whose values may never appear in the published report.
_REDACTED_ENV_KEYS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "IDP_ADMIN_EMAIL",
    "SLACK_WEBHOOK_URL",
)

# Environment values naming the GitLab instance, when the caller runs in a
# GitLab job: host names as they are, URLs by their host.
_HOST_ENV_KEYS = ("CI_SERVER_HOST", "CI_SERVER_FQDN")
_URL_ENV_KEYS = ("CI_API_V4_URL", "CI_SERVER_URL")

# Noise classes that earn no tokens. Applied only to the excerpt handed to the
# model — the full log always reaches disk unfiltered, so the agent can still
# grep anything this drops.
_NOISE_MARKERS = (
    "Collecting ",
    "Downloading ",
    "Using cached ",
    "Requirement already",
    "Installing collected",
    "Preparing metadata",
    "Building editable",
    "finished with status",
    "Deleted log group",
    "Deleted S3 bucket",
    "Discovering resources for stack",
    "Initialized batch processor",
)

# Signals that mark where a failure is actually described. Ordered most- to
# least-specific; the brief keeps context around each hit.
_FAILURE_SIGNALS = (
    "Traceback (most recent call last)",
    "IDPProcessingError",
    "❌ Test suite failed at",
    "❌ Step",
    "Command failed with exit code",
    "✗ Error:",
    "ROLLBACK_",
    "_FAILED",
)


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    # Rich table borders carry no information once the text is gone.
    if stripped[0] in "│┃┡└├╭╰━┏┗┌┐┤┴┬┼╇╡":
        return True
    return any(marker in line for marker in _NOISE_MARKERS)


# ---------------------------------------------------------------------------
# Deterministic evidence brief
# ---------------------------------------------------------------------------


#: The transcript `codebuild_deployment.install_suite_transcript` tees the
#: suite's own stdout/stderr into. Kept as a literal rather than imported
#: because this module is also driven from tests that never import that one;
#: the two values are asserted equal in `scripts/sdlc/tests/`.
SUITE_TRANSCRIPT_PATH = os.environ.get(
    "IDP_SUITE_TRANSCRIPT", "/tmp/idp-suite-transcript.log"
)  # nosec B108 - isolated CodeBuild environment


def read_suite_transcript(path: str = "") -> str:
    """Return the suite's own transcript of this build, or "" if there is none.

    Args:
        path: Transcript to read; defaults to `SUITE_TRANSCRIPT_PATH`.

    Returns:
        The transcript's contents, or "" when it is absent or unreadable.
    """
    try:
        with open(
            path or SUITE_TRANSCRIPT_PATH, encoding="utf-8", errors="replace"
        ) as f:
            return f.read()
    except OSError:
        return ""


def fetch_full_build_log() -> str:
    """Return this build's log, preferring the suite's own transcript.

    ⚠️ **The CloudWatch stream is not a reliable source for a build analysing
    itself**, which is why the local transcript is tried first. CodeBuild batches
    stdout to CloudWatch and the suite's parallel steps interleave, so a walk
    performed while the build is still running can return a prefix that is
    missing lines already written. Measured: one nightly run's analysis captured
    1942 lines and stopped ~50 short of the three that identified the failure,
    concluded "root cause not fully determined", and hypothesised a timeout that
    had not occurred. Read again after the build, the same stream held all of
    it. The transcript is written by this process, line-buffered, in the order
    this process emitted it, so it cannot be behind.

    The CloudWatch walk remains as the fallback, for the cases the transcript
    cannot cover: a failure before the deployment script installed the tee, and
    any caller running outside that script. It still paginates on
    `nextForwardToken` — `get_codebuild_logs`' single `get_log_events` call caps
    at ~1MB / 10K events and so silently returns only the tail of a long build —
    but it keeps the race described above.
    """
    local = read_suite_transcript()
    if local:
        return local

    build_id = os.environ.get("CODEBUILD_BUILD_ID", "")
    if not build_id:
        return ""

    import boto3  # local: keeps this module importable outside CodeBuild

    log_group = f"/aws/codebuild/{build_id.split(':')[0]}"
    log_stream = build_id.split(":")[-1]
    client = boto3.client("logs")

    messages: list[str] = []
    token = None
    # 200 pages matches the after_script's own ceiling; a runaway stream must
    # not turn diagnostics into an infinite loop.
    for _ in range(200):
        kwargs = {
            "logGroupName": log_group,
            "logStreamName": log_stream,
            "startFromHead": True,
        }
        if token:
            kwargs["nextToken"] = token
        try:
            page = client.get_log_events(**kwargs)
        except Exception as e:  # noqa: BLE001 - diagnostics must never raise
            messages.append(f"[failure_agent] log fetch stopped: {e}")
            break
        messages.extend(ev["message"].rstrip("\n") for ev in page.get("events", []))
        next_token = page.get("nextForwardToken")
        # get_log_events returns the SAME token at the end of the stream.
        if not next_token or next_token == token:
            break
        token = next_token

    return "\n".join(messages)


def extract_failure_excerpts(log_text: str, context_lines: int = 40) -> str:
    """Pull the parts of the log that describe a failure, with context.

    This replaces the blind `[-150:]` tail. It is what the single-shot summary
    should have been given, and it doubles as the agent's opening pointer so the
    agent does not spend turns rediscovering which step failed.
    """
    if not log_text:
        return "(no build log available)"

    lines = log_text.split("\n")
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if any(sig in line for sig in _FAILURE_SIGNALS):
            keep.update(range(max(0, i - 5), min(len(lines), i + context_lines)))

    if not keep:
        # No recognised signal: fall back to a tail, but a de-noised one so the
        # window is not spent on teardown inventory.
        meaningful = [ln for ln in lines if not _is_noise(ln)]
        return "\n".join(meaningful[-150:])

    out: list[str] = []
    previous = -1
    for i in sorted(keep):
        if previous >= 0 and i > previous + 1:
            out.append(f"... [{i - previous - 1} lines omitted] ...")
        if not _is_noise(lines[i]):
            out.append(lines[i])
        previous = i
    return "\n".join(out)


def build_evidence_brief(
    stack_name: str,
    error_text: str,
    workflow_failures: list | None,
    log_text: str,
    log_path: str | None = None,
) -> str:
    """Assemble the deterministic evidence bundle.

    Used twice: as the agent's opening brief, and as the evidence block for the
    single-shot fallback. One implementation so the two paths cannot drift.
    """
    region = os.environ.get("AWS_DEFAULT_REGION") or os.environ.get(
        "AWS_REGION", "us-east-1"
    )
    parts = [
        f"Stack name: {stack_name}",
        f"Region: {region}",
        f"CodeBuild build: {os.environ.get('CODEBUILD_BUILD_ID', 'n/a')}",
        "",
        "Reported test error (often a GENERIC wrapper — not necessarily the "
        "root cause):",
        error_text or "(none recorded)",
        "",
        "Step Functions execution failures (authoritative when present; the "
        "`cause` field holds the real Lambda traceback). An EMPTY list means no "
        "document workflow errored — which for a test failure usually points at "
        "a timeout, an assertion, or a harness problem rather than a product "
        "bug:",
        json.dumps(workflow_failures or [], indent=2),
        "",
    ]
    if log_path:
        parts += [
            f"FULL build log on disk: {log_path} "
            f"({len(log_text.splitlines())} lines). Grep it — do not read it "
            "whole. Note that Steps 3-10/13-14 run CONCURRENTLY against one "
            "shared stack and write to one log stream, so their output "
            "interleaves; correlate by timestamp, not by adjacency.",
            "",
        ]
    parts += [
        "Failure excerpts extracted from the full log (noise filtered):",
        extract_failure_excerpts(log_text),
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

_AGENT_TASK = """\
You are diagnosing a failed CI run of the GenAIIDP accelerator's SDLC pipeline.
Your job is to find the ROOT CAUSE and say what to change. You are advisory
only — the pass/fail verdict is already decided and is not yours to revisit.

You have READ-ONLY access. You cannot and must not mutate anything: no stack
deletion, no config writes, no file edits. The account is SHARED with other
pipelines running right now, so do not touch resources belonging to stacks other
than the one named in the brief.

Evidence available to you:
  * The brief below (reported error, Step Functions failures, log excerpts).
  * The FULL build log on disk — grep it; do not read it whole.
  * Live AWS: CloudWatch Logs (including the stack's Lambda log groups), Step
    Functions execution histories, CloudFormation stack events, DynamoDB
    tracking tables. The stack still exists while you run.
  * This git repository, including history. `git log -S<string>` and
    `git log -1 --format=%ad <sha>` are often decisive: a test that fails on a
    timeout or threshold is frequently a budget that was tuned under different
    conditions and never revisited when the surrounding code changed.

Method:
  1. Establish WHICH step failed and what it asserts. The suite orchestration is
     in scripts/sdlc/codebuild_deployment.py.
  2. Separate the real failure from collateral. When one parallel test fails the
     suite kills the rest, so `exit code -9` / SIGKILL lines are fail-fast
     collateral, NOT independent failures. Never report them as causes.
  3. Follow the evidence to where the cause actually lives. If the build log
     dead-ends, go to the Lambda logs or the Step Functions `cause` field.
  4. Distinguish a product regression from a harness/timeout/environment
     problem. Check whether the changed code could even affect the failing path.
  5. Quote exact strings with `file:line` or a log timestamp. Never paraphrase an
     error you have not seen.

Grounding rules — these override any instinct to be helpful:
  * If you cannot establish the cause from evidence, SAY SO and list what
     evidence would settle it. A confident wrong cause is worse than "not
     determined" — it sends someone down a dead end.
  * Distinguish what you MEASURED from what you INFER. Mark inferences as such.
  * Do not guess at IAM/quota/region/config causes without direct evidence.

Output plain text in exactly this shape, no preamble:

ROOT CAUSE
  <2-4 sentences. What failed, and why. Quote the decisive evidence.>

EVIDENCE
  <bullets; each cites a file:line, a log timestamp, or a command's output>

PRODUCT REGRESSION OR CI/HARNESS ISSUE
  <one line, and why — this decides whether the MR author needs to act at all>

RECOMMENDED FIX
  <bullets; concrete, with file:line where applicable>

CONFIDENCE
  <high | medium | low, and what would raise it>

--- EVIDENCE BRIEF ---
{brief}
"""


def _readonly_credentials() -> dict[str, str] | None:
    """Assume the read-only role and return its credentials as env vars.

    None when the role is not configured or cannot be assumed; the caller then
    skips the agent rather than run it on the job's own credentials.
    """
    if not AGENT_ROLE_ARN:
        print(
            "⚠️  failure-agent: IDP_FAILURE_AGENT_ROLE_ARN unset — skipping the "
            "agent; the single-shot summary is used instead"
        )
        return None
    import boto3

    try:
        session = boto3.client("sts").assume_role(
            RoleArn=AGENT_ROLE_ARN,
            RoleSessionName="idp-failure-agent",
            # Bounded to the agent's own wall clock plus slack, so a leaked
            # credential is useless almost immediately.
            # 3600 is the role's MaxSessionDuration (sdlc-iam-role.yml); STS
            # refuses a longer request, which would skip the agent.
            DurationSeconds=min(3600, max(900, AGENT_WALL_CLOCK_SECONDS + 300)),
        )["Credentials"]
    except Exception as e:  # noqa: BLE001
        print(
            f"⚠️  failure-agent: could not assume the read-only role "
            f"({redact_report(str(e))}) — skipping the agent; the single-shot "
            "summary is used instead"
        )
        return None
    return {
        "AWS_ACCESS_KEY_ID": session["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": session["SecretAccessKey"],
        "AWS_SESSION_TOKEN": session["SessionToken"],
    }


def child_env(
    credentials: Mapping[str, str],
    home: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment the agent's CLI runs in, built from an allowlist.

    Only the names in `_PASSED_ENV_KEYS` are copied from the job's environment.
    The assumed role's credentials, a private HOME and the CLI's settings are set
    here, so no other credential source the job holds (the container-credential
    endpoint CodeBuild exports, a shared credentials file under the job's HOME,
    a token variable) is visible to the child or to the commands it runs.
    """
    environ = os.environ if environ is None else environ
    env = {key: environ[key] for key in _PASSED_ENV_KEYS if environ.get(key)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("AWS_REGION", env.get("AWS_DEFAULT_REGION", "us-east-1"))
    env.setdefault("AWS_DEFAULT_REGION", env["AWS_REGION"])
    env.update(
        {
            "HOME": home,
            # The SDK's other credential sources, closed explicitly: the files
            # under the private HOME do not exist, and instance metadata is off.
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_EC2_METADATA_DISABLED": "true",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_MODEL": regional_model_id(AGENT_MODEL, env["AWS_REGION"]),
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": regional_model_id(
                AGENT_HAIKU_MODEL, env["AWS_REGION"]
            ),
            # Never let CI wait on a TTY that does not exist.
            "CI": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
    )
    env.update(credentials)
    return env


def claude_command(binary: str, prompt: str, model: str, log_dir: str) -> list[str]:
    """The exact argv the agent runs."""
    return [
        binary,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--max-turns",
        str(AGENT_MAX_TURNS),
        "--max-budget-usd",
        str(AGENT_MAX_BUDGET_USD),
        # Confinement and instruction loading; see SESSION_SCOPE_FLAGS.
        *SESSION_SCOPE_FLAGS,
        # The build log is outside the checkout; `--restricted` confines the file
        # tools to the working directories, so the log directory is added.
        "--add-dir",
        log_dir,
        "--strict-mcp-config",
        "--permission-mode",
        PERMISSION_MODE,
        # Anything outside the allowlist is DENIED rather than queued for a
        # prompt nobody can answer. Without this the agent hangs.
        "--permission-prompts",
        "none",
        "--no-session-persistence",
        "--tools",
        ",".join(_SESSION_TOOLS),
        "--allowedTools",
        *_ALLOWED_TOOLS,
        "--disallowedTools",
        *_DISALLOWED_TOOLS,
    ]


def _redactor(
    environ: Mapping[str, str] | None = None, extra: tuple[str, ...] = ()
) -> redact.Redactor:
    """A redactor holding the job's credential values and the forge's host."""
    import urllib.parse

    environ = os.environ if environ is None else environ
    redactor = redact.Redactor(extra)
    redactor.add(*(environ.get(key, "") for key in _REDACTED_ENV_KEYS))
    redactor.add(*(environ.get(key, "") for key in _HOST_ENV_KEYS))
    for key in _URL_ENV_KEYS:
        try:
            redactor.add(urllib.parse.urlsplit(environ.get(key, "")).hostname or "")
        except ValueError:
            continue
    return redactor


def redact_report(
    text: str,
    environ: Mapping[str, str] | None = None,
    extra: tuple[str, ...] = (),
) -> str:
    """`text` with credentials, account ids, addresses and forge hosts replaced.

    Applied to every model-written summary before it leaves the job: the caller
    puts it in S3, the failure email, a GitLab artifact and the team's chat
    channel. `scripts/ux_runner/redact.py` holds the shapes (account ids, so ARNs
    that carry one, access keys, session and bearer tokens, JWTs, email
    addresses, presigned-URL signatures); the values registered here are the
    job's own credentials and endpoints, plus `extra`.
    """
    return _redactor(environ, extra)(text)


def ensure_claude_code() -> str | None:
    """Install the pinned Claude Code CLI if absent; return its path or None."""
    existing = shutil.which("claude")
    if existing:
        return existing
    print(f"📦 failure-agent: installing {CLAUDE_CODE_NPM_SPEC}...")
    try:
        result = subprocess.run(  # nosec B603 B607 - fixed argv, no shell
            ["npm", "install", "-g", "--no-fund", "--no-audit", CLAUDE_CODE_NPM_SPEC],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception as e:  # noqa: BLE001
        print(f"⚠️  failure-agent: npm install failed: {e}")
        return None
    if result.returncode != 0:
        print(
            f"⚠️  failure-agent: npm install exit {result.returncode}: {result.stderr[-500:]}"
        )
        return None
    return shutil.which("claude")


def run_failure_agent(
    stack_name: str,
    error_text: str,
    workflow_failures: list | None,
) -> str | None:
    """Diagnose a pipeline failure with Claude Code headless.

    Returns the agent's report, or None if the agent is disabled or could not
    complete — in which case the caller MUST fall back to its single-shot
    summary. This function never raises.
    """
    if not AGENT_ENABLED:
        return None

    started = time.time()
    try:
        # First, so that without the read-only role nothing else is done.
        creds = _readonly_credentials()
        if not creds:
            return None
        secrets = tuple(creds.values())

        os.makedirs(WORKDIR, exist_ok=True)

        log_text = fetch_full_build_log()
        log_path = os.path.join(WORKDIR, "build.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(log_text)
        print(
            f"🔍 failure-agent: captured {len(log_text.splitlines())} log lines "
            f"to {log_path}"
        )

        brief = build_evidence_brief(
            stack_name, error_text, workflow_failures, log_text, log_path
        )

        claude = ensure_claude_code()
        if not claude:
            return None

        # Outside WORKDIR, which the file tools may read: the CLI writes its own
        # state under HOME.
        os.makedirs(AGENT_HOME, exist_ok=True)
        env = child_env(creds, AGENT_HOME)
        print("🔒 failure-agent: running on read-only assumed-role credentials")

        model = env["ANTHROPIC_MODEL"]
        cmd = claude_command(claude, _AGENT_TASK.format(brief=brief), model, WORKDIR)

        print(
            f"🤖 failure-agent: starting (model={model}, "
            f"max_turns={AGENT_MAX_TURNS}, budget=${AGENT_MAX_BUDGET_USD}, "
            f"wall_clock={AGENT_WALL_CLOCK_SECONDS}s)"
        )
        result = subprocess.run(  # nosec B603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=AGENT_WALL_CLOCK_SECONDS,
            check=False,
            env=env,
            cwd=os.getcwd(),
        )
        elapsed = time.time() - started

        if result.returncode != 0:
            print(
                f"⚠️  failure-agent: exit {result.returncode} after {elapsed:.0f}s "
                f"— falling back to the single-shot summary. "
                f"stderr: {redact_report(result.stderr[-500:], extra=secrets)}"
            )
            return None

        report, cost = _parse_agent_output(result.stdout)
        if not report:
            print("⚠️  failure-agent: produced no text — falling back")
            return None
        report = redact_report(_strip_preamble(report), extra=secrets)
        cost_note = f", cost ${cost:.2f}" if cost is not None else ""
        print(f"✅ failure-agent: completed in {elapsed:.0f}s{cost_note}")
        return report

    except subprocess.TimeoutExpired:
        print(
            f"⚠️  failure-agent: exceeded its {AGENT_WALL_CLOCK_SECONDS}s wall "
            "clock — falling back to the single-shot summary"
        )
        return None
    except Exception as e:  # noqa: BLE001 - diagnostics must never fail a build
        print(
            f"⚠️  failure-agent: {type(e).__name__}: {redact_report(str(e))} "
            "— falling back"
        )
        return None


def _strip_preamble(report: str) -> str:
    """Drop anything the model emitted before the ROOT CAUSE header.

    The prompt says "no preamble", and the first live run still opened with
    "I have enough to establish the root cause decisively... Let me write up the
    finding." — narration that belongs in a transcript, not in a CI summary a
    human skims at 2am. Enforcing the contract in code is more reliable than
    another round of prompt-tuning. If the header is absent (the model ignored
    the format entirely) the text is returned untouched rather than discarded.
    """
    marker = "ROOT CAUSE"
    idx = report.find(marker)
    return report[idx:].strip() if idx > 0 else report.strip()


def _parse_agent_output(stdout: str) -> tuple[str | None, float | None]:
    """Pull the report text and cost out of `--output-format json`."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        # Not JSON: treat non-empty stdout as the report rather than losing it.
        text = (stdout or "").strip()
        return (text or None), None
    if isinstance(payload, dict):
        text = payload.get("result") or payload.get("text")
        cost = payload.get("total_cost_usd")
        return (text.strip() if isinstance(text, str) and text.strip() else None), (
            cost if isinstance(cost, (int, float)) else None
        )
    return None, None
