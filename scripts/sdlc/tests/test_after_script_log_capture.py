"""Pin the integration_tests after_script's log capture against two regressions.

Both were the same defect wearing different clothes: a run whose GitLab job
outlived its 1h AWS credentials lost the CodeBuild logs and the deployment
summary, AND the message it posted to Slack blamed the wrong thing.

The 2026-10-11 nightly (job 30872567) is the worked example. Its pipeline went
terminal `Succeeded` at 59m with 12/12 tests green, the job ran 62.6 minutes,
and `after_script` — which inherits the credentials the runner's vendor mints
BEFORE the job starts, hard-capped at 1h for a chained assume — spent all 12
`list-action-executions` attempts re-sending an expired token. Because every
`aws` call there discarded stderr, the artifact and the Slack post said "after
12 attempts (~60s)", which reads as the eventual-consistency race the retry
loop was written for. The three preceding nights ran 58.4-58.5 minutes and
resolved on the first attempt. The job was three minutes slower; nothing else
differed, not even the commit.

So three properties are asserted here, at the YAML level because that is where
they live: the persisted session is sourced before anything calls `aws`, the
resolve loop keeps its stderr and stops rather than retrying a credential
failure, and the credential file is never published.
"""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CI_CONFIG = _REPO_ROOT / ".gitlab-ci.yml"
_CREDS_FILE = "aws_after_script_creds.env"


@pytest.fixture(scope="module")
def ci_text():
    assert _CI_CONFIG.is_file(), f"missing {_CI_CONFIG}"
    return _CI_CONFIG.read_text()


@pytest.fixture(scope="module")
def after_script(ci_text):
    """The integration_tests job's after_script block, as raw text.

    Sliced textually rather than via a YAML parse on purpose: the assertions
    below are about shell source — which redirections it uses, what order the
    statements are in — and a parse would hand back the same strings after a
    round trip that can normalise them.
    """
    start = ci_text.index("\nintegration_tests:")
    body = ci_text[start:]
    # The job is the last one in the file today, but don't depend on that.
    nxt = re.search(r"\n(?=[A-Za-z_][A-Za-z0-9_-]*:\s*\n)", body[1:])
    if nxt:
        body = body[: nxt.start() + 1]
    marker = "\n  after_script:"
    assert marker in body, "integration_tests has no after_script"
    return body[body.index(marker) :]


def test_the_persisted_session_is_sourced_before_any_aws_call(after_script):
    sourced = after_script.index(f". ./{_CREDS_FILE}")
    first_aws = after_script.index("aws ")
    assert sourced < first_aws, (
        "after_script calls `aws` before sourcing the monitor's refreshed "
        "session, so a job running past ~1h is back to using credentials that "
        "have already expired"
    )


def test_sourcing_is_conditional_so_a_short_run_still_works(after_script):
    # The file is absent when the script died before the monitor started, or
    # when the self-assume is denied. That has to fall through to the ambient
    # credentials, not abort the log capture.
    assert f'if [ -f "{_CREDS_FILE}" ]; then' in after_script


def test_the_resolve_loop_keeps_its_stderr(after_script):
    loop = after_script[after_script.index("for attempt in $(seq 1 12)") :]
    loop = loop[: loop.index("\n        done")]
    assert "list-action-executions" in loop
    assert "2>/dev/null" not in loop, (
        "the CodeBuild-id resolve loop is discarding stderr again — that is "
        "what made an expired token indistinguishable from eventual "
        "consistency on job 30872567"
    )
    assert "2>resolve_err.txt" in loop


def test_a_credential_failure_breaks_the_loop_instead_of_retrying(after_script):
    loop = after_script[after_script.index("for attempt in $(seq 1 12)") :]
    loop = loop[: loop.index("\n        done")]
    # Retrying cannot mint a credential, so these must not burn 12 attempts
    # and ~60s before reporting.
    for token in ("ExpiredToken", "RequestExpired", "InvalidClientTokenId"):
        assert token in loop, f"{token} is not recognised as unretryable"
    case_arm = loop[loop.index("case ") :]
    assert "break" in case_arm


def test_the_failure_artifact_names_which_failure_it_was(after_script):
    # "after 12 attempts (~60s)" alone sent triage after an eventual-consistency
    # race for what was a credential expiry; the two need different fixes.
    assert "$RESOLVE_ERR" in after_script
    assert "NOT the eventual-consistency race" in after_script


def test_the_credential_file_is_shredded_on_the_way_out(after_script):
    assert f"rm -f {_CREDS_FILE}" in after_script


def test_the_credential_file_is_not_an_artifact(after_script):
    artifacts = after_script[after_script.index("\n  artifacts:") :]
    assert _CREDS_FILE not in artifacts, (
        "a live AWS session would be published to everyone who can read the pipeline"
    )


def test_the_credential_file_is_gitignored():
    ignore = (_REPO_ROOT / ".gitignore").read_text().splitlines()
    assert _CREDS_FILE in [line.strip() for line in ignore]


def test_slack_separates_a_handoff_from_a_pass(after_script):
    # integration_test_deployment.py exits 0 on the handoff path too, so the
    # job status alone cannot distinguish "12/12 green" from "nobody watched
    # the end". The marker is what does.
    assert "monitor_handoff.txt" in after_script
    assert "UNVERIFIED" in after_script
    # And the handoff note must survive truncation, since it is the part a
    # reader most needs.
    payload = after_script[after_script.index("import json, pathlib, sys") :]
    assert payload.index("elif handed_off:") < payload.index("len(summary) > 2000"), (
        "the summary is truncated before the handoff warning is attached, so a "
        "long summary can push the warning off the end"
    )
