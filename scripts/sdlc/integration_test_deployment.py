#!/usr/bin/env python3
"""
Integration Test Deployment Script

Handles code packaging, S3 upload, and pipeline monitoring for integration tests.
"""

import json
import os
import pathlib
import subprocess
import sys
import time

import boto3
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn


def run_command(cmd, check=True):
    """Run shell command and return result"""
    print(f"Running: {cmd}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)  # nosec B602 nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true - hardcoded commands, no user input
    if check and result.returncode != 0:
        print(f"Error: {result.stderr}")
        sys.exit(1)
    return result


def get_env_var(name, default=None):
    """Get environment variable with optional default"""
    value = os.environ.get(name, default)
    if value is None:
        print(f"Error: Environment variable {name} is required")
        sys.exit(1)
    return value


def create_deployment_package():
    """Create deployment zip package"""
    print("Creating deployment package...")

    # Create dist directory
    os.makedirs("./dist", exist_ok=True)

    # Remove existing zip
    if os.path.exists("./dist/code.zip"):
        os.remove("./dist/code.zip")

    # Create zip with exclusions
    excludes = [
        "*.git/*",
        "*.git/**",
        "*__pycache__/*",
        ".gitlab-ci.yml",
        "*.delete/*",
        "*.sav/*",
        "*.venv/*",
        "*.vscode/*",
        "*cdk.out/*",
        "*dist/*",
        "*.DS_Store",
        "*.pyc",
        "*.pyo",
        "*.pyd",
        "*.so",
        "**/.env",
        "*.docker/*",
        "*.aws-sam/*",
        # Live credentials the monitor persists for after_script. This package
        # is uploaded to S3 and unpacked by CodeBuild, and a GitLab runner can
        # reuse a workspace between jobs, so a file left by a PREVIOUS run
        # would otherwise be shipped. after_script removes it too; this is the
        # belt to that braces.
        f"*{AFTER_SCRIPT_CREDS_FILE}",
    ]

    exclude_args = " ".join([f'-x "{pattern}"' for pattern in excludes])
    cmd = f"zip -r ./dist/code.zip ./ {exclude_args}"

    run_command(cmd)
    print("✅ Deployment package created")


def upload_to_s3(bucket_name):
    """Upload code package to S3 and return version ID"""
    print(f"Uploading to S3 bucket: {bucket_name}")

    s3_client = boto3.client("s3")

    try:
        # Get GitLab user email to pass to CodeBuild
        gitlab_user_email = os.environ.get("GITLAB_USER_EMAIL", "")
        
        # Add metadata to pass email to CodeBuild
        metadata = {}
        if gitlab_user_email:
            metadata["gitlab-user-email"] = gitlab_user_email
            print(f"Adding GitLab user email to metadata: {gitlab_user_email}")

        response = s3_client.put_object(
            Bucket=bucket_name,
            Key="deploy/code.zip",
            Body=open("./dist/code.zip", "rb"),
            Metadata=metadata,
        )
        version_id = response.get("VersionId", "unknown")
        print(f"✅ Uploaded with version ID: {version_id}")
        return version_id
    except Exception as e:
        print(f"❌ Upload failed: {e}")
        sys.exit(1)


def find_pipeline_execution_by_version(pipeline_name, version_id, max_wait=300):
    """Find pipeline execution that corresponds to specific S3 version ID"""
    console = Console()
    console.print(f"[cyan]Finding pipeline execution for version:[/cyan] {version_id}")
    
    codepipeline = boto3.client("codepipeline")
    start_time = time.time()
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
        
        task = progress.add_task("[yellow]Searching for pipeline execution...", total=None)
        
        while time.time() - start_time < max_wait:
            try:
                response = codepipeline.list_pipeline_executions(
                    pipelineName=pipeline_name, maxResults=10
                )
                
                for execution in response["pipelineExecutionSummaries"]:
                    execution_id = execution["pipelineExecutionId"]
                    
                    # Get execution details to check source version
                    details = codepipeline.get_pipeline_execution(
                        pipelineName=pipeline_name,
                        pipelineExecutionId=execution_id
                    )
                    
                    # Check if this execution matches our version ID
                    for artifact in details["pipelineExecution"].get("artifactRevisions", []):
                        if artifact.get("revisionId") == version_id:
                            progress.update(task, description="[green]✅ Found matching execution!")
                            console.print(f"[green]✅ Found matching execution:[/green] {execution_id}")
                            return execution_id
                
                elapsed = int(time.time() - start_time)
                progress.update(task, description=f"[yellow]Waiting for pipeline trigger ({elapsed}s)...")
                        
            except Exception as e:
                progress.update(task, description=f"[red]Error: {str(e)[:50]}...")
                console.print(f"[red]Error finding execution: {e}[/red]")
                
            time.sleep(10)
        
        progress.update(task, description="[red]❌ No matching execution found")
        console.print(f"[red]❌ Could not find pipeline execution for version {version_id}[/red]")
    return None


# CodeBuild streams every codebuild_deployment.py print to this log group.
CODEBUILD_LOG_GROUP = "/aws/codebuild/app-sdlc"

# Substrings that mark a meaningful step boundary or result. Surfacing only
# these keeps the monitor readable (the raw log is tens of thousands of lines
# of boto3/idp_sdk chatter) while still showing what the pipeline is doing.
MILESTONE_MARKERS = (
    "Step ",
    "Running tests",
    "Deploying stack",
    "Deployment completed",
    "Publishing templates",
    "Template published",
    "API Gateway Web UI hosting",
    "All tests passed",
    "Test suite failed",
    "✅",
    "❌",
    "🎉",
    "💥",
    "🧹",
    "📦",
)


def resolve_codebuild_log_stream(pipeline_name, execution_id):
    """Find the CodeBuild log stream backing this pipeline execution.

    Mirrors the after_script: the BuildAction's externalExecutionId is
    '<project>:<streamName>'. Returns the stream name, or None if the build
    hasn't started/registered yet (caller retries on later polls).
    """
    try:
        cp = boto3.client("codepipeline")
        actions = cp.list_action_executions(
            pipelineName=pipeline_name,
            filter={"pipelineExecutionId": execution_id},
        ).get("actionExecutionDetails", [])
        for a in actions:
            if a.get("actionName") != "BuildAction":
                continue
            ext = (
                a.get("output", {})
                .get("executionResult", {})
                .get("externalExecutionId", "")
            )
            if ":" in ext:
                return ext.split(":", 1)[1]
    except Exception:
        # Non-fatal: status polling continues without the log stream.
        pass
    return None


def fetch_summary_verdict(log_stream):
    """Read the build's S3 summary and return a pass/fail verdict, if available.

    The build uploads its summary to a deterministic S3 key derived from the
    CodeBuild stream id. The primary suite writes a progressive/interim summary
    there as steps complete (well before the whole suite finishes), so this
    verdict is usually available before the monitor's handoff deadline.

    Returns:
      True  — summary present and shows OVERALL: PASS.
      False — summary present and shows OVERALL: FAIL.
      None  — no summary yet / unreadable / no OVERALL line (undecided).

    This lets the handoff path fail the GitLab job on a real failure instead of
    always exiting neutral — the gap that let a failed run show as a green job.

    Limitation: at handoff the summary is usually the INTERIM one (primary suite
    done, probes may still be running). So a PASS here means "nothing has failed
    YET" — a probe that fails after handoff still only surfaces via SNS/S3, since
    the 1h role-chained creds can't watch the run to completion. This reliably
    catches primary-suite / early-deploy failures (the common case), not late
    probe failures.
    """
    if not log_stream:
        return None
    account = os.environ.get("IDP_ACCOUNT_ID", "020432867916")
    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    bucket = f"genaiic-sdlc-sourcecode-{account}-{region}"
    key = f"deploy/summaries/{log_stream}.txt"
    try:
        body = (
            boto3.client("s3")
            .get_object(Bucket=bucket, Key=key)["Body"]
            .read()
            .decode("utf-8", "replace")
        )
    except Exception:
        return None  # not uploaded yet / no access — undecided
    if "OVERALL: FAIL" in body:
        return False
    if "OVERALL: PASS" in body:
        return True
    return None


def stream_new_milestones(logs_client, log_stream, next_token, console, max_pages=20):
    """Print new milestone log lines since next_token; return updated token.

    Returns (token, saw_activity): saw_activity is True when ANY new events
    arrived (even non-milestone), so the caller can show a real "last activity"
    heartbeat and distinguish a working build from a genuinely stalled one.

    Drains up to max_pages of get_log_events per call: a single page caps at
    ~1MB/10k events, and a busy build easily produces more between 30s polls,
    so paging fully keeps the milestone stream from falling behind. The cap
    bounds a single poll's work (a fresh monitor attaching to a long build).
    """
    saw_activity = False
    try:
        for _ in range(max_pages):
            kwargs = {
                "logGroupName": CODEBUILD_LOG_GROUP,
                "logStreamName": log_stream,
                "startFromHead": True,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            resp = logs_client.get_log_events(**kwargs)
            events = resp.get("events", [])
            if events:
                saw_activity = True
            for e in events:
                msg = e["message"].rstrip()
                if any(marker in msg for marker in MILESTONE_MARKERS):
                    # Trim to keep one line per milestone; the log prefix (bare
                    # timestamps) adds no value here.
                    console.print(f"  [dim]│[/dim] {msg[:160]}")
            new_token = resp.get("nextForwardToken", next_token)
            # get_log_events returns the SAME nextForwardToken at the stream
            # head — that's the "no more pages" signal; stop draining.
            if new_token == next_token:
                next_token = new_token
                break
            next_token = new_token
    except Exception:
        # Log group/stream may not exist yet, or a transient error — the
        # status poller remains the source of truth for completion.
        pass
    return next_token, saw_activity


# The GitLab runner's AWS credentials are vended by assuming idp-sdlc-GitLab
# FROM the already-assumed gitlab-runners-prod role. That is STS role chaining,
# which AWS hard-caps EACH session at 1 hour regardless of the role's
# MaxSessionDuration (confirmed empirically: STS rejects DurationSeconds>3600
# for a chained assume). The CodeBuild pipeline itself is NOT capped (its own
# service role, ~64-68 min observed, completes fine) — only the monitor's creds
# expire ~60 min in.
#
# To watch a long run to completion, the monitor REFRESHES its credentials
# before they expire: it re-assumes idp-sdlc-GitLab again (a fresh 1h session —
# role chaining allows repeated re-assumes, each capped at 1h) and rebuilds its
# boto3 clients. So MONITOR_HANDOFF_SECONDS can now exceed the 1h cap. It is set
# below the GitLab job timeout (2h30m) with headroom for before/after_script.
# If refresh ever fails (e.g. the self-assume permission is missing), the
# monitor still hands off gracefully to the S3 summary + SNS email.
MONITOR_HANDOFF_SECONDS = 6600  # 110 min
# Re-assume this far before the ~1h chained-session expiry.
CREDENTIAL_REFRESH_SECONDS = 3000  # 50 min
GITLAB_ROLE_NAME = "idp-sdlc-GitLab"

# ⚠️ after_script's shell `aws` calls do NOT share this process's refreshed
# credentials. The runner's credential vendor mints the job's credentials BEFORE
# the job starts ("AWS Credential Vendor -- Success -- Assumed role
# .../idp-sdlc-GitLab"), and a chained assume is hard-capped at 1h, so a run
# that outlives the hour leaves after_script holding a dead session. Every
# `aws` call there discarded stderr, which made that failure indistinguishable
# from the eventual-consistency race the CodeBuild-id retry loop exists for:
# the 2026-10-11 nightly (job 30872567, 62.6 min wall clock, pipeline terminal
# at 59m) burned all 12 attempts against an expired token and posted "no logs
# were fetched" to Slack, while the three preceding nights (58.4-58.5 min)
# resolved on the first attempt. The job was 3 minutes slower, nothing else.
#
# after_script also cannot fix this itself: re-assuming the role needs valid
# credentials to call STS with, and by then it has none. So the MONITOR, which
# does hold a live session, writes each freshly-minted one here and
# after_script sources it. Written on every successful refresh INCLUDING one at
# startup, so the file is never older than CREDENTIAL_REFRESH_SECONDS and
# always has >=10 min of life left when after_script runs.
#
# ⚠️ NEVER add this path to the job's `artifacts:` — it holds live credentials,
# and this repository's pipelines are readable by everyone in the group.
AFTER_SCRIPT_CREDS_FILE = "aws_after_script_creds.env"

# Written when the monitor exits on the HANDOFF path, where the job exits 0
# without the pipeline having reached a terminal state. The job is green and
# the suite's result is unknown, and those must not look the same in Slack.
MONITOR_HANDOFF_MARKER = "monitor_handoff.txt"

#: Written at the root of the deployment zip and read by codebuild_deployment.py,
#: which has no other way to tell the nightly schedule from a button press. A
#: correctness guard, not a security control: an MR-button run executes the MR's
#: own harness, which can write or ignore this file however it likes.
CI_RUN_OPTIONS_FILE = "ci_run_options.json"


def write_ci_run_options(
    path: str = CI_RUN_OPTIONS_FILE, environ: dict | None = None
) -> dict:
    """Record which GitLab pipeline source and ref started this run, for CodeBuild.

    codebuild_deployment.py updates the long-lived UX review stack in place only on
    the nightly schedule of develop, and CodeBuild sees only the zip, not the GitLab
    job's environment, so the two facts travel inside it. This is a correctness
    guard, not a security control: an MR-button run executes the MR's own
    codebuild_deployment.py, which reads or ignores this file however its author
    wrote it. What it does prevent is the ordinary mistake, a button press moving
    the shared review stack onto an unmerged template.

    Written with pathlib rather than through run_command, so the zip stays
    create_deployment_package's first command. Best effort: a write that fails
    leaves no file, because main() removes any previous one first, and the reader
    takes no file to mean no update. Returns the options whether or not they were
    written.
    """
    env = os.environ if environ is None else environ
    options = {
        "version": 1,
        "pipeline_source": env.get("CI_PIPELINE_SOURCE", ""),
        "ref": env.get("CI_COMMIT_REF_NAME", ""),
        "pipeline_id": env.get("CI_PIPELINE_ID", ""),
    }
    try:
        pathlib.Path(path).write_text(
            json.dumps(options, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as e:
        print(f"⚠️ Could not write {path}, so the review stack is left as it is: {e}")
    return options


class _CredentialRefresher:
    """Keep the monitor's AWS creds alive past the 1h role-chaining cap.

    The GitLab job's creds are a chained assume of idp-sdlc-GitLab, hard-capped
    at 1h. Re-assuming the SAME role (chaining again) mints a fresh 1h session,
    so calling refresh() every <1h keeps the monitor alive for the whole run.

    build_clients() returns freshly-credentialed (codepipeline, logs) clients.
    On the first call it uses the ambient job creds; subsequent refreshes use
    the newly-vended session. If the re-assume fails (missing self-assume
    permission, expired base creds), it returns None so the caller falls back to
    the graceful S3/SNS handoff rather than crashing.

    Each successful refresh is also persisted to AFTER_SCRIPT_CREDS_FILE, which
    is the only way after_script's shell `aws` calls can outlive the job's own
    1h credentials — see the comment on that constant.
    """

    def __init__(self, region):
        self._region = region
        self._session_kwargs = {}  # empty = ambient creds
        self._role_arn = self._resolve_role_arn()

    def _resolve_role_arn(self):
        # The job identity is an assumed-role ARN:
        #   arn:aws:sts::<acct>:assumed-role/idp-sdlc-GitLab/<session>
        # Convert to the role ARN we re-assume:
        #   arn:aws:iam::<acct>:role/idp-sdlc-GitLab
        try:
            acct = boto3.client("sts").get_caller_identity()["Account"]
            return f"arn:aws:iam::{acct}:role/{GITLAB_ROLE_NAME}"
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ Could not resolve GitLab role ARN for refresh: {e}")
            return None

    def refresh(self):
        """Re-assume the role for a fresh 1h session. Returns True on success."""
        if not self._role_arn:
            return False
        try:
            # Use whatever creds we currently hold (ambient or last-refreshed).
            sts = boto3.client("sts", **self._session_kwargs)
            creds = sts.assume_role(
                RoleArn=self._role_arn,
                RoleSessionName="idp-monitor-refresh",
                # 1h is the max for a chained assume; request it explicitly.
                DurationSeconds=3600,
            )["Credentials"]
            self._session_kwargs = {
                "aws_access_key_id": creds["AccessKeyId"],
                "aws_secret_access_key": creds["SecretAccessKey"],
                "aws_session_token": creds["SessionToken"],
            }
            self._persist_for_after_script(creds)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ Credential refresh failed ({e}); will hand off at deadline")
            return False

    @staticmethod
    def _persist_for_after_script(creds):
        """Write the fresh session where after_script's `aws` calls can source it.

        Best effort: a write failure costs the log/summary artifacts on a long
        run, which is strictly better than failing a deploy that worked.
        """
        try:
            path = pathlib.Path(AFTER_SCRIPT_CREDS_FILE)
            path.write_text(
                f"export AWS_ACCESS_KEY_ID={creds['AccessKeyId']}\n"
                f"export AWS_SECRET_ACCESS_KEY={creds['SecretAccessKey']}\n"
                f"export AWS_SESSION_TOKEN={creds['SessionToken']}\n"
            )
            # The runner workspace is shared with after_script and nothing else
            # needs to read a live credential out of it.
            path.chmod(0o600)
            return True
        except OSError as e:
            print(f"⚠️ Could not persist credentials for after_script: {e}")
            return False

    def build_clients(self):
        """Return (codepipeline, logs) clients on the current credentials."""
        cp = boto3.client("codepipeline", region_name=self._region, **self._session_kwargs)
        logs = boto3.client("logs", region_name=self._region, **self._session_kwargs)
        return cp, logs


def monitor_pipeline_execution(
    pipeline_name, execution_id, max_wait=8100, handoff_after=MONITOR_HANDOFF_SECONDS
):
    """Monitor a pipeline execution with live progress; hand off if it outlives creds.

    Returns:
      True  — pipeline reached a terminal Succeeded state.
      False — pipeline reached a terminal failure state, or polling failed hard.
      None  — still running when handoff_after elapsed: a graceful HANDOFF, not
              a failure. The caller exits neutrally and the run's real result
              surfaces via the S3 summary + SNS.

    max_wait (135 min) is a hard backstop below the GitLab job's 2h30m ceiling;
    handoff_after (110 min) is the normal exit for a still-running pipeline.
    The monitor refreshes its 1h-capped creds every ~50 min (see
    _CredentialRefresher), so it can watch past the single-credential lifetime.
    """
    console = Console()
    console.print(f"[cyan]Monitoring pipeline execution:[/cyan] {execution_id}")

    region = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    refresher = _CredentialRefresher(region)
    # Mint a session up front, not only at the 50-min mark. The job's OWN
    # credentials are already part-used when this process starts (the vendor
    # mints them pre-job, and before_script installs the AWS CLI first), so a
    # run that finishes just over the hour would otherwise leave after_script
    # with nothing to source. On failure we keep the ambient creds and behave
    # exactly as before.
    if refresher.refresh():
        console.print(
            "[dim]🔑 Minted a session for after_script's log capture.[/dim]"
        )
    codepipeline, logs_client = refresher.build_clients()
    # Re-assume before the 1h chained-session cap; recompute clients on refresh.
    next_refresh_at = CREDENTIAL_REFRESH_SECONDS
    poll_interval = 30
    # A persistent get_pipeline_execution failure (throttling, expired creds,
    # wrong id) must NOT masquerade as a 2h hang: the old handler logged each
    # error and kept polling to the deadline. Bail after this many consecutive
    # errors so the real problem is visible in minutes, not hours.
    max_consecutive_errors = 10
    consecutive_errors = 0

    # Live milestone stream from the CodeBuild log so the monitor shows what
    # the tests are actually doing (which step, pass/fail) instead of an opaque
    # spinner. Resolving the stream may lag the execution start, so retry until
    # it appears. Track "last activity" to expose a real stall heartbeat.
    log_stream = None
    log_token = None
    wait_time_at_last_activity = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:

        task = progress.add_task("[yellow]Pipeline executing...", total=None)

        wait_time = 0
        while wait_time < max_wait:
            # Refresh creds before the ~1h chained-session cap so the monitor
            # can watch a long run to completion. On success, rebuild the
            # clients on the new session; on failure, keep the old clients and
            # let the graceful handoff take over when they expire.
            if wait_time >= next_refresh_at:
                if refresher.refresh():
                    codepipeline, logs_client = refresher.build_clients()
                    console.print(
                        f"[dim]🔑 Refreshed monitor credentials at "
                        f"{wait_time // 60}m (fresh 1h session).[/dim]"
                    )
                # Schedule the next refresh regardless; a failed refresh will
                # just retry at the next interval until the handoff/deadline.
                next_refresh_at += CREDENTIAL_REFRESH_SECONDS

            try:
                response = codepipeline.get_pipeline_execution(
                    pipelineName=pipeline_name,
                    pipelineExecutionId=execution_id
                )

                consecutive_errors = 0
                status = response["pipelineExecution"]["status"]
                elapsed_mins = wait_time // 60

                # Surface new CodeBuild milestone lines (best effort). Resolve
                # the log stream lazily once the BuildAction has started.
                if log_stream is None:
                    log_stream = resolve_codebuild_log_stream(
                        pipeline_name, execution_id
                    )
                if log_stream is not None:
                    log_token, saw_activity = stream_new_milestones(
                        logs_client, log_stream, log_token, console
                    )
                    if saw_activity:
                        wait_time_at_last_activity = wait_time

                # Terminal states: return IMMEDIATELY and log explicitly so a
                # detection miss can never be confused with a timeout.
                if status == "Succeeded":
                    progress.update(task, description="[green]✅ Pipeline completed successfully!")
                    console.print(
                        f"[green]✅ Pipeline reached terminal state 'Succeeded' "
                        f"after {elapsed_mins}m — monitor exiting.[/green]"
                    )
                    return True
                elif status in ["Failed", "Cancelled", "Superseded", "Stopped"]:
                    progress.update(task, description=f"[red]❌ Pipeline failed: {status}")
                    console.print(
                        f"[red]❌ Pipeline reached terminal state '{status}' "
                        f"after {elapsed_mins}m — monitor exiting.[/red]"
                    )
                    return False
                elif status == "InProgress":
                    # Graceful handoff: the pipeline is healthy but will outlive
                    # our 1h role-chained credentials. Stop before they expire
                    # (which would otherwise spew ExpiredToken errors and burn
                    # the job slot) and point at the authoritative result.
                    if wait_time >= handoff_after:
                        progress.update(
                            task, description="[cyan]↪ Handing off (still running)"
                        )
                        # Before handing off neutral, check the S3 summary: the
                        # primary suite uploads its result (INTERIM) as soon as
                        # it finishes, well within this window. If it already
                        # shows OVERALL: FAIL, FAIL the job now rather than
                        # exiting green — the gap that let a failed run pass.
                        verdict = fetch_summary_verdict(log_stream)
                        if verdict is False:
                            console.print(
                                f"[red]✗ Pipeline still running after "
                                f"{elapsed_mins}m, but the deploy summary already "
                                f"reports OVERALL: FAIL — failing the job.[/red]"
                            )
                            console.print(f"[red]  Execution: {execution_id}[/red]")
                            return False
                        console.print(
                            f"[cyan]↪ Pipeline still running after {elapsed_mins}m "
                            f"(monitor deadline reached). Handing off; the run "
                            f"continues and its result lands in S3 + SNS email.[/cyan]"
                        )
                        console.print(
                            f"[cyan]  Execution: {execution_id}[/cyan]"
                        )
                        if verdict is True:
                            console.print(
                                "[cyan]  Primary suite passed so far; probes may "
                                "still be running. Final result in S3 + SNS.[/cyan]"
                            )
                        else:
                            console.print(
                                "[cyan]  No summary yet — result will be published "
                                "to the S3 deploy summary + SNS on finish.[/cyan]"
                            )
                        return None
                    # Heartbeat: show minutes since the last log activity so a
                    # genuine stall (build hung) is visually distinct from a
                    # healthy long-running step.
                    idle_mins = (wait_time - wait_time_at_last_activity) // 60
                    idle_note = (
                        f", no log activity for {idle_mins}m" if idle_mins >= 3 else ""
                    )
                    stream_note = "" if log_stream else ", awaiting build logs"
                    progress.update(
                        task,
                        description=(
                            f"[yellow]⏳ Pipeline running "
                            f"({elapsed_mins}m elapsed{idle_note}{stream_note})..."
                        ),
                    )

            except Exception as e:
                consecutive_errors += 1
                # Expired creds are NOT a pipeline failure: if the refresh path
                # ever fails (e.g. missing sts:TagSession on the self-assume) the
                # monitor's 1h role-chained session expires while a HEALTHY run is
                # still going, and boto3 raises ExpiredTokenException every poll.
                # Treat that like the handoff: consult the authoritative S3
                # verdict, and only fail on a real OVERALL: FAIL. Otherwise exit
                # neutral so a SUCCESSFUL deploy is never painted red by our own
                # credential lifetime. (This is what turned a fully-passing run
                # into a red job before the trust-policy TagSession fix.)
                if "ExpiredToken" in type(e).__name__ or "ExpiredToken" in str(e):
                    console.print(
                        "[cyan]↪ Monitor credentials expired while the pipeline "
                        "is still running — cannot refresh. Handing off to the S3 "
                        "summary + SNS instead of failing a healthy run.[/cyan]"
                    )
                    verdict = fetch_summary_verdict(log_stream)
                    if verdict is False:
                        console.print(
                            "[red]✗ Deploy summary already reports OVERALL: FAIL "
                            "— failing the job.[/red]"
                        )
                        console.print(f"[red]  Execution: {execution_id}[/red]")
                        return False
                    console.print(
                        f"[cyan]  Execution: {execution_id} — final result in "
                        f"S3 + SNS email.[/cyan]"
                    )
                    return None
                progress.update(task, description=f"[red]Error: {str(e)[:50]}...")
                console.print(
                    f"[red]Error checking pipeline status "
                    f"({consecutive_errors}/{max_consecutive_errors}): {e}[/red]"
                )
                if consecutive_errors >= max_consecutive_errors:
                    console.print(
                        f"[red]❌ Aborting: {max_consecutive_errors} consecutive "
                        f"errors querying pipeline status (not a timeout).[/red]"
                    )
                    return False

            time.sleep(poll_interval)
            wait_time += poll_interval

        progress.update(task, description=f"[red]❌ Timeout after {max_wait//60} minutes")
        console.print(f"[red]❌ Pipeline monitoring timed out after {max_wait} seconds[/red]")
        return False


def monitor_pipeline(pipeline_name, version_id, max_wait=8100):
    """Monitor pipeline using version-based tracking"""
    # First find the execution that matches our version
    execution_id = find_pipeline_execution_by_version(pipeline_name, version_id)
    
    if not execution_id:
        return False
    
    # Write execution ID to file for GitLab CI to use
    with open("pipeline_execution_id.txt", "w") as f:
        f.write(execution_id)
    print(f"Pipeline execution ID written to file: {execution_id}")
        
    # Then monitor that specific execution
    return monitor_pipeline_execution(pipeline_name, execution_id, max_wait)


def main():
    """Main execution function.

    This run's state files are cleared before anything else. A GitLab runner can
    reuse a workspace between jobs, and each of these is read later: a stale
    pipeline_execution_id.txt makes after_script fetch and publish the PREVIOUS
    run's CodeBuild logs as if they were this one's, a stale handoff marker
    makes Slack report a verified pass as UNVERIFIED, and a stale run-options
    file would ship a previous run's pipeline source if this run's could not be
    written. Each produces a confident wrong answer, which is worse than a
    missing one.
    """
    print("Starting integration test deployment...")

    for stale in (
        MONITOR_HANDOFF_MARKER,
        "pipeline_execution_id.txt",
        CI_RUN_OPTIONS_FILE,
    ):
        pathlib.Path(stale).unlink(missing_ok=True)

    # Get configuration from environment
    account_id = get_env_var("IDP_ACCOUNT_ID", "020432867916")
    region = get_env_var("AWS_DEFAULT_REGION", "us-east-1")
    bucket_name = f"genaiic-sdlc-sourcecode-{account_id}-{region}"
    pipeline_name = get_env_var("IDP_PIPELINE_NAME", "genaiic-sdlc-deploy-pipeline")

    print(f"Account ID: {account_id}")
    print(f"Region: {region}")
    print(f"Bucket: {bucket_name}")
    print(f"Pipeline: {pipeline_name}")

    # Execute deployment steps
    write_ci_run_options()
    create_deployment_package()
    version_id = upload_to_s3(bucket_name)

    result = monitor_pipeline(pipeline_name, version_id)

    if result is True:
        print("🎉 Integration test deployment completed successfully!")
        sys.exit(0)
    elif result is None:
        # Handoff: pipeline still running when the credential window closed.
        # Not a failure — the real result lands in the S3 summary + SNS. Exit 0
        # so a healthy long run doesn't show as a red job; monitoring it to
        # completion is impossible on 1h role-chained creds.
        #
        # Leave a marker so the Slack post can say UNVERIFIED instead of
        # PASSED: exit 0 here means "nobody watched the end", and a reader
        # cannot tell that apart from a 12/12 green run otherwise.
        try:
            pathlib.Path(MONITOR_HANDOFF_MARKER).write_text(
                "The monitor exited before the pipeline reached a terminal "
                "state, so this job's green status does NOT mean the suite "
                "passed. The authoritative result is the S3 deploy summary "
                "and the SNS email.\n"
            )
        except OSError as e:  # noqa: BLE001
            print(f"⚠️ Could not write the handoff marker: {e}")
        print(
            "↪ Integration test pipeline still running at handoff; "
            "monitor exited cleanly. See the S3 deploy summary / SNS for the "
            "final result."
        )
        sys.exit(0)
    else:
        print("💥 Integration test deployment failed!")
        sys.exit(1)


if __name__ == "__main__":
    main()
