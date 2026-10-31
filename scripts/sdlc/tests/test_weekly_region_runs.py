# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The weekly region runs deploy and test the primary suite outside us-east-1.

A weekly region schedule carries the pipeline variable IDP_TEST_REGION, and
integration_test_deployment.py honours it only on a scheduled pipeline and only for
a region with CI infrastructure. What is pinned here:

* the decision, in both directions, and that an unknown region fails rather than
  quietly testing us-east-1;
* that the GitLab job's script AND after_script select the region, since they are
  separate shells;
* that the build labels its summary, failure email and both Slack posts with the
  region, and leaves the UX review stack and the nightly story jobs alone;
* that the startup sweeps, which reap global IAM, look for a run's stacks in every
  CI region;
* that the analysis models and the uploaded suite configs name models the region
  can call.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_CONFIG = REPO_ROOT / ".gitlab-ci.yml"
ITD_SCRIPT = REPO_ROOT / "scripts" / "sdlc" / "integration_test_deployment.py"
HARNESS = REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py"
SLACK_LABEL = "WEEKLY REGION RUN"


@pytest.fixture
def itd():
    import integration_test_deployment as module

    return module


@pytest.fixture
def agent():
    import failure_agent as module

    return module


# ---------------------------------------------------------------------------
# The trigger rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["eu-central-1", "ca-central-1", " EU-Central-1 "])
def test_a_region_schedule_names_its_region(itd, value):
    environ = {"CI_PIPELINE_SOURCE": "schedule", "IDP_TEST_REGION": value}
    assert itd.region_run_requested(environ) == value.strip().lower()


@pytest.mark.parametrize(
    "environ",
    [
        {"CI_PIPELINE_SOURCE": "schedule"},
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_TEST_REGION": ""},
        {"CI_PIPELINE_SOURCE": "web", "IDP_TEST_REGION": "eu-central-1"},
        {
            "CI_PIPELINE_SOURCE": "merge_request_event",
            "IDP_TEST_REGION": "eu-central-1",
        },
        {"CI_PIPELINE_SOURCE": "push", "IDP_TEST_REGION": "eu-central-1"},
        {"IDP_TEST_REGION": "eu-central-1"},
    ],
    ids=["nightly", "blank", "run-pipeline-form", "mr-button", "push", "no-ci"],
)
def test_nothing_else_is_a_region_run(itd, environ):
    assert itd.region_run_requested(environ) is None


@pytest.mark.parametrize("value", ["us-east-1", "us-west-2", "eu-west-1", "mars"])
def test_a_region_without_ci_infrastructure_is_refused(itd, value):
    with pytest.raises(ValueError, match="IDP_TEST_REGION"):
        itd.region_run_requested(
            {"CI_PIPELINE_SOURCE": "schedule", "IDP_TEST_REGION": value}
        )


def test_the_two_allowlists_agree(itd, cbd):
    """The trigger and the sweeps must name the same set of CI regions."""
    assert itd.PRIMARY_CI_REGION == cbd.PRIMARY_CI_REGION
    assert set(cbd.CI_INFRA_REGIONS) == {itd.PRIMARY_CI_REGION, *itd.REGION_RUN_REGIONS}


def _exports(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        name, _, value = line.removeprefix("export ").partition("=")
        out[name] = value
    return out


def test_a_region_run_exports_its_region_to_every_variable(itd):
    exports = _exports(
        itd.region_env_lines(
            {
                "CI_PIPELINE_SOURCE": "schedule",
                "IDP_TEST_REGION": "ca-central-1",
                "AWS_DEFAULT_REGION": "us-east-1",
            }
        )
    )
    assert exports == {
        "AWS_DEFAULT_REGION": "ca-central-1",
        "AWS_REGION": "ca-central-1",
    }


@pytest.mark.parametrize(
    ("environ", "region"),
    [
        ({"CI_PIPELINE_SOURCE": "schedule"}, "us-east-1"),
        ({"CI_PIPELINE_SOURCE": "web", "AWS_DEFAULT_REGION": "us-west-2"}, "us-west-2"),
    ],
    ids=["nightly", "explicit-override"],
)
def test_every_other_run_keeps_its_region(itd, environ, region):
    exports = _exports(itd.region_env_lines(environ))
    assert exports["AWS_DEFAULT_REGION"] == region
    assert exports["AWS_REGION"] == region


def _region_env(environ: dict) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CI_", "IDP_"))}
    env.update(environ)
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, str(ITD_SCRIPT), "--region-env"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_the_job_entrypoint_prints_the_exports():
    done = _region_env(
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_TEST_REGION": "eu-central-1"}
    )
    assert done.returncode == 0, done.stderr
    assert "export AWS_REGION=eu-central-1" in done.stdout.splitlines()


def test_the_job_entrypoint_fails_on_an_unknown_region():
    done = _region_env(
        {"CI_PIPELINE_SOURCE": "schedule", "IDP_TEST_REGION": "eu-west-9"}
    )
    assert done.returncode != 0
    assert done.stdout == ""
    assert "eu-west-9" in done.stderr


def test_the_run_options_record_the_region(itd, tmp_path):
    options = itd.write_ci_run_options(
        str(tmp_path / "o.json"),
        environ={"CI_PIPELINE_SOURCE": "schedule", "IDP_TEST_REGION": "eu-central-1"},
    )
    assert options["region_run"] == "eu-central-1"
    nightly = itd.write_ci_run_options(
        str(tmp_path / "n.json"), environ={"CI_PIPELINE_SOURCE": "schedule"}
    )
    assert nightly["region_run"] == ""


# ---------------------------------------------------------------------------
# The GitLab job
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def config():
    return yaml.safe_load(CI_CONFIG.read_text(encoding="utf-8"))


def _lines(section) -> list[str]:
    return [line for block in section for line in str(block).splitlines()]


def test_the_script_selects_the_region_before_the_deploy(config):
    script = _lines(config["integration_tests"]["script"])
    select = next(i for i, line in enumerate(script) if "--region-env" in line)
    deploy = next(
        i
        for i, line in enumerate(script)
        if "integration_test_deployment.py" in line and "--region-env" not in line
    )
    assert select < deploy
    assert script[select].startswith("REGION_ENV=$("), (
        'an unknown region must fail the job: `eval "$(...)"` hides the exit status'
    )
    assert not any(
        re.search(r"export AWS_DEFAULT_REGION=\$\{", line) for line in script
    )


def test_after_script_selects_the_same_region_before_its_first_aws_call(config):
    after = _lines(config["integration_tests"]["after_script"])
    select = next(i for i, line in enumerate(after) if "--region-env" in line)
    first_aws = next(
        i
        for i, line in enumerate(after)
        if re.search(r"\baws (s3|codepipeline|logs)\b", line)
    )
    assert select < first_aws


@pytest.mark.parametrize(
    ("helper", "expected"),
    [
        ("exit 2", "us-east-1"),
        ("echo 'export AWS_DEFAULT_REGION=ca-central-1'", "ca-central-1"),
    ],
)
def test_after_script_exports_the_region_its_reads_use(config, helper, expected):
    """A failed helper falls back to us-east-1 for the reads, not just the echo."""
    step = next(
        item
        for item in config["integration_tests"]["after_script"]
        if "--region-env" in item
    )
    script = step.replace(
        "python3 scripts/sdlc/integration_test_deployment.py --region-env",
        f'sh -c "{helper}"',
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    out = subprocess.run(
        ["bash", "-c", script + '\nprintf "%s %s" "$AWS_DEFAULT_REGION" "$AWS_REGION"'],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout
    assert out.splitlines()[-1] == f"{expected} {expected}"


def _region_label_assignment(config, job) -> str:
    """The ``region_run = ...`` statement in ``job``'s Slack heredoc, unparsed."""
    import ast
    import textwrap

    for step in config[job].get("after_script", []) + config[job]["script"]:
        match = re.search(r"<<'PY'\n(.*?)\n\s*PY\s*$", step, re.S | re.M)
        if not match:
            continue
        tree = ast.parse(textwrap.dedent(match.group(1)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "region_run" for t in node.targets
            ):
                return ast.unparse(node)
    raise AssertionError(f"no region_run label in {job}")


def test_both_slack_posts_derive_the_region_label_the_same_way(config):
    """One label, one rule: the schedule's own IDP_TEST_REGION, on a schedule only."""
    posts = {
        job: _region_label_assignment(config, job)
        for job in ("integration_tests", "nightly_notification")
    }
    assert len(set(posts.values())) == 1, posts
    (assignment,) = set(posts.values())
    assert "os.environ.get('IDP_TEST_REGION', '')" in assignment
    assert "os.environ.get('CI_PIPELINE_SOURCE', '') == 'schedule'" in assignment


@pytest.mark.parametrize("job", ["integration_tests", "nightly_notification"])
def test_both_slack_posts_name_the_region(config, job):
    body = "\n".join(
        _lines(config[job].get("after_script", []) + config[job]["script"])
    )
    label_line = next(line for line in body.splitlines() if SLACK_LABEL in line)
    assert label_line.strip().startswith("status ="), (
        "the label goes on `status`, the key the Slack workflow already declares"
    )


def test_region_runs_share_the_deploy_mutex(config):
    """IAM is global, so a region run must not overlap the nightly's sweep."""
    assert config["integration_tests"]["resource_group"] == "integration_deploy"


# ---------------------------------------------------------------------------
# The build
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"AWS_REGION": "eu-central-1"}, "eu-central-1"),
        ({"AWS_DEFAULT_REGION": "ca-central-1"}, "ca-central-1"),
        ({"AWS_REGION": "us-east-1"}, None),
        ({}, None),
    ],
)
def test_the_build_knows_a_region_run_by_its_own_region(cbd, environ, expected):
    assert cbd.region_run(environ) == expected


def test_a_region_run_summary_says_what_run_it_is(cbd):
    labelled = cbd.label_ci_run("TABLE", {"AWS_REGION": "eu-central-1"})
    assert labelled.startswith(f"🌍 {cbd.REGION_RUN_LABEL} · eu-central-1")
    assert "This is not the nightly result." in labelled
    assert labelled.endswith("TABLE")


def test_the_nightly_summary_is_unchanged(cbd):
    assert cbd.label_ci_run("TABLE", {"AWS_REGION": "us-east-1"}) == "TABLE"


@pytest.mark.parametrize(
    ("environ", "subject"),
    [
        ({"AWS_REGION": "us-east-1"}, "IDP CI failure: s"),
        (
            {"AWS_REGION": "eu-central-1"},
            "IDP CI failure (weekly region run · eu-central-1): s",
        ),
        (
            {"AWS_REGION": "ca-central-1", "IDP_RUN_PROBES": "true"},
            "IDP CI failure (weekly variant-probe run; weekly region run · ca-central-1): s",
        ),
    ],
)
def test_the_failure_email_names_the_run(cbd, environ, subject):
    assert cbd.failure_subject("s", environ) == subject


def test_main_uses_the_combined_labels():
    text = HARNESS.read_text(encoding="utf-8")
    assert "label_variant_probe_run(interim)" not in text
    assert "label_variant_probe_run(consolidated)" not in text
    assert "interim = label_ci_run(interim)" in text
    assert "consolidated = label_ci_run(consolidated)" in text
    assert "failure_subject(stack_name)" in text


_SCHEDULED_DEVELOP = {"pipeline_source": "schedule", "ref": "develop"}


def test_a_region_run_leaves_the_ux_review_stack_alone(cbd):
    assert cbd.ux_nightly_update_requested(
        _SCHEDULED_DEVELOP, {"AWS_REGION": "us-east-1"}
    )
    assert not cbd.ux_nightly_update_requested(
        {**_SCHEDULED_DEVELOP, "region_run": "eu-central-1"},
        {"AWS_REGION": "us-east-1"},
    )
    # Either signal suffices: an options file that predates the key still stops
    # an update from a build outside the review stack's region.
    assert not cbd.ux_nightly_update_requested(
        _SCHEDULED_DEVELOP, {"AWS_REGION": "ca-central-1"}
    )


class _Paginator:
    def __init__(self, pages, error=None):
        self._pages, self._error = pages, error

    def paginate(self, **_):
        if self._error:
            raise self._error
        return iter(self._pages)


class _CloudFormation:
    def __init__(self, names, error=None):
        self._names, self._error = names, error

    def get_paginator(self, _):
        return _Paginator(
            [{"StackSummaries": [{"StackName": n} for n in self._names]}], self._error
        )


def test_the_sweeps_protect_a_run_in_any_ci_region(cbd, monkeypatch):
    """A us-east-1 retained stack must protect its global IAM from an EU sweep."""
    stacks = {
        "us-east-1": ["idp-1029-200000"],
        "eu-central-1": ["idp-1030-063000-abc"],
        "ca-central-1": [],
    }
    asked: list[str] = []

    def client(service, region_name=None, **_):
        assert service == "cloudformation"
        asked.append(region_name)
        return _CloudFormation(stacks[region_name])

    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setattr(cbd.boto3, "client", client)

    assert cbd._live_idp_run_prefixes() == {"idp-1029-200000", "idp-1030-063000"}
    assert sorted(asked) == sorted(cbd.CI_INFRA_REGIONS)


def test_an_unread_region_fails_the_enumeration(cbd, monkeypatch):
    """A partial set reads every run in the unread region as over, on a path that deletes."""

    def client(service, region_name=None, **_):
        error = RuntimeError("throttled") if region_name == "ca-central-1" else None
        return _CloudFormation([], error)

    monkeypatch.setattr(cbd.boto3, "client", client)
    with pytest.raises(RuntimeError, match="throttled"):
        cbd._live_idp_run_prefixes()


def test_a_build_outside_the_ci_regions_also_lists_its_own(cbd, monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert cbd._live_run_regions() == [*cbd.CI_INFRA_REGIONS, "us-west-2"]


# ---------------------------------------------------------------------------
# Models the region can call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_id", "region", "expected"),
    [
        (
            "us.anthropic.claude-opus-4-8",
            "eu-central-1",
            "eu.anthropic.claude-opus-4-8",
        ),
        (
            "us.anthropic.claude-opus-4-8",
            "ca-central-1",
            "us.anthropic.claude-opus-4-8",
        ),
        ("us.anthropic.claude-opus-4-8", "us-east-1", "us.anthropic.claude-opus-4-8"),
        # A rescue row: the EU model is a different generation, not a re-prefix.
        (
            "us.anthropic.claude-opus-4-1-20250805-v1:0",
            "eu-central-1",
            "eu.anthropic.claude-opus-4-5-20251101-v1:0",
        ),
        (
            "global.anthropic.claude-opus-4-8",
            "eu-central-1",
            "global.anthropic.claude-opus-4-8",
        ),
    ],
)
def test_the_analysis_model_is_one_the_region_can_call(
    agent, model_id, region, expected
):
    assert agent.regional_model_id(model_id, region) == expected


def test_the_summary_call_is_made_in_the_builds_region(cbd, monkeypatch):
    import io
    import json

    seen = {}

    class _Runtime:
        def invoke_model(self, modelId, body):
            seen["model"] = modelId
            payload = {"content": [{"type": "text", "text": "ok"}]}
            return {"body": io.BytesIO(json.dumps(payload).encode())}

    def client(service, region_name=None, **_):
        # Only the summary call: loading the product's model table for the first
        # time imports a Lambda module that makes its own clients at import.
        if service == "bedrock-runtime":
            seen["region"] = region_name
        return _Runtime()

    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setattr(cbd.boto3, "client", client)

    assert cbd._invoke_bedrock("prompt") == "ok"
    assert seen == {"region": "eu-central-1", "model": "eu.anthropic.claude-opus-4-8"}


NUVEEN = REPO_ROOT / "scripts" / "sdlc" / "config" / "nuveen.yaml"


def _model_ids(path: str) -> set[str]:
    return set(
        re.findall(r"\b(?:us|eu|ca|global)\.[a-z]+\.[\w.:-]+", Path(path).read_text())
    )


def test_an_eu_upload_carries_eu_models(cbd):
    path = cbd.regional_config_file(str(NUVEEN), {"AWS_REGION": "eu-central-1"})
    assert path != str(NUVEEN)
    ids = _model_ids(path)
    assert "eu.anthropic.claude-sonnet-4-6" in ids
    assert not any(i.startswith("us.anthropic.") for i in ids)


def test_a_canada_upload_drops_what_canada_cannot_call(cbd):
    path = cbd.regional_config_file(str(NUVEEN), {"AWS_REGION": "ca-central-1"})
    ids = _model_ids(path)
    assert "us.amazon.nova-pro-v1:0" not in ids
    assert "ca.amazon.nova-lite-v1:0" in ids


#: Every literal configuration the suite uploads through regional_config_file().
SUITE_CONFIGS = [
    NUVEEN,
    REPO_ROOT / "config_library" / "unified" / "lending-package-sample" / "config.yaml",
    REPO_ROOT / "config_library" / "unified" / "rule-validation" / "config.yaml",
]


def test_every_suite_config_is_named_here(cbd):
    source = (REPO_ROOT / "scripts" / "sdlc" / "codebuild_deployment.py").read_text()
    named = set(
        re.findall(r"['\"]((?:scripts|config_library)/[\w/.-]+\.yaml)['\"]", source)
    )
    assert named <= {str(p.relative_to(REPO_ROOT)) for p in SUITE_CONFIGS}, named


@pytest.mark.parametrize("config", SUITE_CONFIGS, ids=lambda p: p.parent.name)
def test_no_suite_config_names_a_model_canada_cannot_call_after_the_swap(cbd, config):
    """The swap does not filter, so an unmapped unavailable id would reach Bedrock."""
    path = cbd.regional_config_file(str(config), {"AWS_REGION": "ca-central-1"})
    unavailable = cbd._update_configuration_module().CA_UNAVAILABLE_MODELS
    text = Path(path).read_text(encoding="utf-8")
    # Substring, not _model_ids(): some unavailable ids carry no geo prefix.
    named = {
        m
        for m in unavailable
        if re.search(rf"(?<![\w.]){re.escape(m)}(?![\w.:-])", text)
    }
    assert not named, named


def test_a_config_is_swapped_once_per_region(cbd):
    first = cbd.regional_config_file(str(NUVEEN), {"AWS_REGION": "eu-central-1"})
    again = cbd.regional_config_file(str(NUVEEN), {"AWS_REGION": "eu-central-1"})
    assert first == again != str(NUVEEN)


def test_a_rewritten_config_is_swapped_again(cbd, tmp_path):
    """The hook configs are rewritten between uploads under the same path."""
    config = tmp_path / "hook.yaml"
    config.write_text("extraction:\n  model: us.amazon.nova-pro-v1:0\n")
    first = cbd.regional_config_file(str(config), {"AWS_REGION": "eu-central-1"})
    config.write_text("extraction:\n  model: us.amazon.nova-lite-v1:0\n")
    second = cbd.regional_config_file(str(config), {"AWS_REGION": "eu-central-1"})
    assert first != second
    assert "eu.amazon.nova-lite-v1:0" in Path(second).read_text()


def test_a_us_upload_is_the_file_as_written(cbd):
    assert cbd.regional_config_file(str(NUVEEN), {"AWS_REGION": "us-east-1"}) == str(
        NUVEEN
    )


def test_every_config_upload_goes_through_the_swap():
    """A new upload site that skips the swap fails its first EU Bedrock call."""
    text = HARNESS.read_text(encoding="utf-8")
    sites = [m.start() for m in re.finditer(r"idp-cli config-upload", text)]
    assert sites
    for start in sites:
        window = text[start : start + 400]
        flag = window.index("--config-file ")
        assert window[flag:].startswith("--config-file {regional_config_file("), text[
            start : start + 200
        ]


def test_the_gitlab_role_may_upload_to_every_region_runs_bucket(itd):
    """Without the grant the region run's upload is denied before anything deploys."""
    template = (
        REPO_ROOT / "scripts" / "sdlc" / "cfn" / "credential-vendor.yml"
    ).read_text(encoding="utf-8")
    for region in itd.REGION_RUN_REGIONS:
        assert (
            f"genaiic-sdlc-sourcecode-${{AWS::AccountId}}-{region}/deploy/code.zip"
            in template
        ), region
