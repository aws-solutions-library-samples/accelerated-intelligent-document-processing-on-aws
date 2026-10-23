# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""This suite must not be able to reach a real AWS account, under any invocation.

`make test-packages-cicd` runs it through `HERMETIC_AWS`, which strips the
machine's AWS environment, so the gated path is covered by
`scripts/tests/test_offline_suites_are_hermetic.py`. A bare `pytest` typed in
this directory goes through no wrapper at all, and that is the invocation these
tests are about: `aws_hermetic` applies the same control from inside the suite,
and nothing else checks that it still does.

Two halves, and the second is the one this repository keeps getting wrong — a
control that exists and is never consulted. `test_apply_*` measures the control
on a deliberately polluted environment; the rest assert the post-state during an
ordinary test that requests no AWS fixture whatsoever, which can only be true if
the autouse fixture in `conftest.py` really ran.

No test here resolves a credential it then transmits, and none makes a network
call: reading a resolved access key out of a session is a local operation once
the environment holds one, which after `apply` it does.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import aws_hermetic
import boto3
import pytest

pytestmark = pytest.mark.unit


def test_the_machines_credential_files_and_metadata_service_are_taken_away() -> None:
    """Clearing `AWS_ACCESS_KEY_ID` alone leaves two resolvable sources standing.

    A developer machine's `[default]` profile is a credential with no `AWS_*`
    variable set at all, and no amount of unsetting reaches the file it lives in
    — it has to be pointed elsewhere. The instance metadata service is the other:
    a developer box on EC2 can reach it and a CI runner cannot, which is how a
    suite comes to pass locally for a reason that does not exist in CI.
    """
    assert os.environ["AWS_CONFIG_FILE"] == os.devnull
    assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
    assert os.environ["AWS_EC2_METADATA_DISABLED"] == "true"


def test_no_ambient_profile_or_session_token_survives_into_a_test() -> None:
    """A stale session token beside a placeholder access key does not make a
    request unsendable — it makes it a signed request to a real endpoint that
    happens to be rejected. `AWS_PROFILE` is the same shape of leftover.

    Conditional on the machine, and deliberately so: it can only fail where one
    of these is exported, which is the only machine where it matters. The
    unconditional half is `test_apply_strips_a_polluted_environment_*`, which
    sets them itself rather than hoping to find them.
    """
    for name in ("AWS_PROFILE", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN"):
        assert name not in os.environ, f"{name} reached a test in this suite"


def test_the_credentials_a_test_resolves_are_the_placeholders() -> None:
    """The property the rest of this is for, stated directly: whatever a test in
    this package signs with, it is not the machine's.

    This test requests no AWS fixture, which is the point — the tests that most
    needed the environment stripped were the ones that never asked for it.
    """
    resolved = boto3.Session().get_credentials()
    assert resolved is not None, "no credentials resolved at all"
    assert resolved.access_key == aws_hermetic.FAKE_CREDENTIALS["AWS_ACCESS_KEY_ID"]


def test_no_cached_module_global_session_is_in_force_when_a_test_starts() -> None:
    """`boto3.DEFAULT_SESSION` freezes the credentials resolved when it was
    created, so one built before a fixture is a credential that fixture cannot
    reach. A test starts with none.

    What makes this fire is another test having left one behind, so it is the
    whole-suite run that exercises it rather than this module on its own — which
    is also the shape of the defect, since a leaked session is only ever visible
    to whatever runs next in the same worker.
    """
    assert boto3.DEFAULT_SESSION is None


def test_apply_strips_a_polluted_environment_and_installs_the_placeholders(
    monkeypatch,
) -> None:
    """The control itself, measured rather than assumed.

    The environment is polluted first through every variable-shaped source,
    because a probe built from the ambient environment measures nothing on a
    runner, where there was never anything to strip.
    """
    monkeypatch.setenv("AWS_PROFILE", "some-profile")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "not-the-placeholder")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "not-the-placeholder")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "not-a-real-token")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://127.0.0.1:1/creds")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/config")
    monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)

    aws_hermetic.apply(monkeypatch)

    for name in aws_hermetic.HERMETIC_UNSET:
        if name in aws_hermetic.FAKE_CREDENTIALS:
            assert os.environ[name] == aws_hermetic.FAKE_CREDENTIALS[name]
        else:
            assert name not in os.environ, f"apply() left {name} set"
    for name, value in aws_hermetic.HERMETIC_SET.items():
        assert os.environ[name] == value


def test_apply_discards_a_session_built_before_it_ran(monkeypatch) -> None:
    """The mechanism a fake-credential fixture does not address on its own.

    `boto3.DEFAULT_SESSION` is module-global and is created by the first bare
    `boto3.client(...)` anywhere in the process. Setting fake credentials
    afterwards changes nothing for any later bare client, because the session
    already holds the answer it resolved. Which key a test signs with then
    depends on what ran before it, and under `pytest -n` the partition decides
    that afresh on every run.
    """
    sentinel = object()
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", sentinel, raising=False)
    assert boto3.DEFAULT_SESSION is sentinel

    aws_hermetic.apply(monkeypatch)

    assert boto3.DEFAULT_SESSION is None


def test_reset_default_session_is_usable_on_its_own(monkeypatch) -> None:
    """Separate from `apply` because it is the half that has nothing to do with
    the environment, and the conftest fixture calls it again at teardown so a
    session built under the placeholders does not outlive them."""
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", object(), raising=False)

    aws_hermetic.reset_default_session()

    assert boto3.DEFAULT_SESSION is None


def test_a_request_that_escapes_every_mock_is_refused_rather_than_sent() -> None:
    """The backstop, and the only assertion here that is about the wire.

    Everything above makes the credentials harmless. This makes the request not
    happen, which is the difference between a unit suite that signs a rejected
    request to a public endpoint and one that reaches nothing at all. It is also
    what turns the next instance of this defect into a loud failure naming the
    URL, rather than a call nobody sees.

    This test does not make a network request: the refusal replaces botocore's
    send, so the attempt never leaves the process.
    """
    sts = boto3.client("sts", region_name="us-east-1")

    with pytest.raises(RuntimeError, match=r"attempted a real network request") as err:
        sts.get_caller_identity()

    assert "sts" in str(err.value), (
        f"the refusal should name the endpoint it stopped: {err.value}"
    )


def test_every_fake_credential_in_this_package_is_the_same_fake_credential() -> None:
    """Several fixtures in this package set a placeholder access key, each with
    its own literal — the shared `aws_credentials` in `conftest.py`, a copy that
    shadows it, a handful of `aws_env` copies, and a couple of test bodies that
    set one inline. The set is derived below rather than counted here.

    They all agree today and the autouse fixture makes them redundant, but
    redundant is not the same as harmless: a copy that drifted to a different
    value would still read as "fake credentials are set here" while being the one
    thing the post-state assertions above cannot notice, because those compare
    against `FAKE_CREDENTIALS` rather than against a literal.
    """
    placeholder = aws_hermetic.FAKE_CREDENTIALS["AWS_ACCESS_KEY_ID"]
    assert placeholder == "testing", (
        "the placeholder changed; every fixture literal in this package has to "
        "change with it, and this test is what says which ones"
    )

    pattern = re.compile(
        r"""setenv\(\s*["']AWS_ACCESS_KEY_ID["']\s*,\s*["']([^"']*)["']"""
    )
    here = Path(__file__).parent
    found: dict[str, list[str]] = {}
    for module in sorted(here.glob("*.py")):
        # This file is the exception by construction: the one above sets a
        # deliberately wrong key in order to measure that it gets stripped.
        if module.name == Path(__file__).name:
            continue
        for literal in pattern.findall(module.read_text(encoding="utf-8")):
            found.setdefault(literal, []).append(module.name)

    assert found, "found no placeholder access key anywhere in this package"
    divergent = {k: v for k, v in found.items() if k != placeholder}
    assert not divergent, f"these set a different placeholder: {divergent}"


def test_the_unset_list_covers_every_variable_the_makefile_strips() -> None:
    """`HERMETIC_AWS` in `make/hermetic_aws.mk` is the definition this mirrors.
    Derived from the makefile at test time rather than restated, so a variable
    added there and not here fails rather than quietly leaving a source open.
    """
    # Walked up to rather than counted out, because the depth of this file below
    # the checkout root is not something this test should be asserting.
    makefile = next(
        (
            candidate
            for ancestor in Path(__file__).resolve().parents
            if (candidate := ancestor / "make" / "hermetic_aws.mk").is_file()
        ),
        None,
    )
    assert makefile is not None, "make/hermetic_aws.mk not found above this file"
    text = makefile.read_text(encoding="utf-8")
    body = text.split("HERMETIC_AWS :=", 1)[1].split("\n\n", 1)[0]

    stripped = {
        token for token in body.replace("\\", " ").split() if token.startswith("AWS_")
    }
    unset = {
        tok
        for tok in stripped
        if "=" not in tok and f"-u {tok}" in " ".join(body.split())
    }
    assert unset, "parsed no -u entries out of HERMETIC_AWS; the parse is broken"
    missing = unset - set(aws_hermetic.HERMETIC_UNSET)
    assert not missing, f"HERMETIC_AWS strips {sorted(missing)} and this suite does not"

    pinned = {tok.split("=", 1)[0] for tok in stripped if "=" in tok}
    assert pinned == set(aws_hermetic.HERMETIC_SET), (
        f"HERMETIC_AWS pins {sorted(pinned)}, this suite pins "
        f"{sorted(aws_hermetic.HERMETIC_SET)}"
    )
