# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`idp-feature-cli deploy-pack` — the orchestration layer that decides *what
gets published* before a vertical-product pack is deployed.

A pack wrapper is a single CloudFormation template that creates an IDP host
stack and then installs one feature into it. Two URLs have to agree for that to
work: the wrapper's baked `IdpAcceleratorTemplateUrl` (where the host template
lives) and the wrapper's own published location. This command is where they are
chosen, and the interesting failure is a **stale pairing** — a wrapper deployed
against a host template from a previous release, or a republished host with a
wrapper still pointing at the old one. Neither is detectable from the outside:
the stack creates successfully and the feature installs against the wrong host
contract.

That is why the `--build` gate exists at all, and it is what these tests are
mostly about: `accelerator` alone is refused (it would leave the pack pointing at
the old host), `feature` demands an explicit `--host-template-url`, and `all`
derives the URL from the publish it just performed rather than from a flag. The
source-directory auto-discovery is tested to the same standard, because a pack
directory commonly carries its own thin `publish.py` and picking that up would
republish the pack as if it were the accelerator.

The publish and deploy calls themselves are substituted, so what is asserted is
the argument each one receives — the values that decide which artifacts a
customer's stack reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

import idp_feature_sdk.pack as pack_mod
from idp_feature_sdk.cli import main

pytestmark = pytest.mark.unit

_HOST_URL = "https://b.s3.us-east-1.amazonaws.com/host/idp-main.yaml"
_WRAPPER_URL = "https://b.s3.us-east-1.amazonaws.com/extensions/claims/deploy.yaml"


class _Calls:
    """Everything the command handed to the three functions it delegates to."""

    def __init__(self) -> None:
        self.bucket: list[dict] = []
        self.host_publish: list[dict] = []
        self.pack_publish: list[dict] = []
        self.deploy: list[dict] = []


@pytest.fixture
def calls(monkeypatch) -> _Calls:
    """Substitute the bucket resolver, the two publishers and the deploy, and
    record their keyword arguments."""
    recorded = _Calls()

    monkeypatch.setattr(
        pack_mod,
        "ensure_artifacts_bucket",
        lambda **kw: (
            recorded.bucket.append(kw)
            or f"idp-accelerator-artifacts-123456789012-{kw['region']}"
        ),
    )

    def _fake_host_publish(**kw):
        recorded.host_publish.append(kw)
        return _HOST_URL

    monkeypatch.setattr(pack_mod, "publish_host_accelerator", _fake_host_publish)

    class _FakePackPublisher:
        def __init__(self, project_dir, console=None):
            self.project_dir = project_dir

        def publish(self, **kw):
            recorded.pack_publish.append({"project_dir": self.project_dir, **kw})
            return pack_mod.PackPublishResult(
                feature_id="claims",
                version="1.0.0",
                artifact_bucket=kw["artifacts_bucket"],
                artifact_prefix="extensions/claims",
                feature_template_url="https://b/extensions/claims/template.yaml",
                host_template_url=kw["host_template_url"],
                wrapper_template_url=_WRAPPER_URL,
                quick_create_url="https://console/quickcreate",
                deploy_command="idp-feature-cli deploy-pack ...",
            )

    monkeypatch.setattr(pack_mod, "PackPublisher", _FakePackPublisher)

    def _fake_deploy(**kw):
        recorded.deploy.append(kw)
        return "arn:aws:cloudformation:us-east-1:123456789012:stack/claims/abc"

    monkeypatch.setattr(pack_mod, "deploy_pack", _fake_deploy)
    return recorded


def _run(*args: str):
    return CliRunner().invoke(main, ["deploy-pack", *args])


def _required(*extra: str) -> list[str]:
    return ["--stack-name", "claims-stack", "--admin-email", "a@example.com", *extra]


@pytest.fixture
def pack_project(tmp_path: Path) -> Path:
    """A pack source directory nested inside an accelerator-shaped repo root.

    The pack carries its own `publish.py` (as the real ones do) and the repo root
    carries the publish.py + Makefile + template.yaml triple. Only the root is a
    valid `--source-dir`.
    """
    root = tmp_path / "idp-repo"
    (root).mkdir()
    (root / "publish.py").write_text("# host publisher\n", encoding="utf-8")
    (root / "Makefile").write_text("all:\n", encoding="utf-8")
    (root / "template.yaml").write_text("Resources: {}\n", encoding="utf-8")

    pack = root / "feature-platform" / "claims-pack"
    pack.mkdir(parents=True)
    (pack / "publish.py").write_text("# thin pack wrapper\n", encoding="utf-8")
    (pack / "feature.yaml").write_text("featureId: claims\n", encoding="utf-8")
    return pack


# ---------------------------------------------------------------------------
# Source mutex
# ---------------------------------------------------------------------------


def test_wrapper_url_and_from_code_together_are_refused(
    calls, pack_project: Path
) -> None:
    """They answer the same question differently. Silently preferring one would
    publish artifacts the operator then never deploys, or deploy a wrapper the
    operator thinks was just rebuilt."""
    result = _run(
        *_required("--wrapper-url", _WRAPPER_URL, "--from-code", str(pack_project))
    )
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output
    assert calls.deploy == [], "nothing may be deployed after a usage refusal"
    assert calls.pack_publish == []


def test_neither_source_is_refused(calls) -> None:
    result = _run(*_required())
    assert result.exit_code == 1
    assert "either --wrapper-url" in result.output
    assert calls.deploy == []


# ---------------------------------------------------------------------------
# --wrapper-url: deploy a previously published pack
# ---------------------------------------------------------------------------


def test_wrapper_url_deploys_without_publishing_anything(calls) -> None:
    result = _run(*_required("--wrapper-url", _WRAPPER_URL))
    assert result.exit_code == 0, result.output
    assert calls.pack_publish == [], "--wrapper-url must not rebuild the pack"
    assert calls.host_publish == []
    assert calls.bucket == [], (
        "no artifacts bucket is needed to deploy a published pack"
    )
    assert len(calls.deploy) == 1
    assert calls.deploy[0]["wrapper_url"] == _WRAPPER_URL
    assert calls.deploy[0]["stack_name"] == "claims-stack"
    assert calls.deploy[0]["admin_email"] == "a@example.com"
    assert "Stack ARN:" in result.output


def test_extra_parameters_reach_the_deploy_as_a_parsed_mapping(calls) -> None:
    """`--parameters` is how a published pack's baked default is overridden at
    deploy time. A value that arrives unparsed, or split on the wrong comma, is
    submitted to CloudFormation and accepted."""
    result = _run(
        *_required(
            "--wrapper-url",
            _WRAPPER_URL,
            "--parameters",
            "LogLevel=DEBUG,VpcSubnetIds=subnet-a,subnet-b",
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.deploy[0]["extra_parameters"] == {
        "LogLevel": "DEBUG",
        "VpcSubnetIds": "subnet-a,subnet-b",
    }


def test_a_spaced_out_pair_reaches_the_deploy_and_the_tolerance_is_reported(
    calls,
) -> None:
    """`--parameters "LogLevel = DEBUG"` used to reach the deploy as `{}` (#1220).

    The wrapper was then created with every parameter at its publish-time default
    and nothing said so, which is indistinguishable afterwards from the override
    having been applied. The pair is now read as written, and the notice is
    asserted here rather than only on the parser, because printing it is the part
    this call site owns.
    """
    result = _run(
        *_required("--wrapper-url", _WRAPPER_URL, "--parameters", "LogLevel = DEBUG")
    )
    assert result.exit_code == 0, result.output
    assert calls.deploy[0]["extra_parameters"] == {"LogLevel": "DEBUG"}
    assert "whitespace" in result.output


def test_text_that_forms_no_pair_is_reported_and_the_deploy_still_runs(calls) -> None:
    """Not a refusal: exiting non-zero would change what the command accepts.

    What was missing is any way for the operator to tell an ignored `--parameters`
    value from one that worked, so the text is named back and the deploy proceeds
    with the overrides that did parse.
    """
    result = _run(
        *_required("--wrapper-url", _WRAPPER_URL, "--parameters", "JustAKey,A=1")
    )
    assert result.exit_code == 0, result.output
    assert calls.deploy[0]["extra_parameters"] == {"A": "1"}
    assert "JustAKey" in result.output


def test_no_parameters_means_an_empty_mapping_not_none(calls) -> None:
    """The baked publish-time defaults are what a bare deploy is meant to use.
    An empty mapping leaves every one of them in place."""
    result = _run(*_required("--wrapper-url", _WRAPPER_URL))
    assert result.exit_code == 0, result.output
    assert calls.deploy[0]["extra_parameters"] == {}


def test_wait_is_off_unless_asked_for(calls) -> None:
    assert _run(*_required("--wrapper-url", _WRAPPER_URL)).exit_code == 0
    assert calls.deploy[0]["wait"] is False
    assert _run(*_required("--wrapper-url", _WRAPPER_URL, "--wait")).exit_code == 0
    assert calls.deploy[1]["wait"] is True


def test_a_deploy_failure_exits_nonzero_with_the_reason(calls, monkeypatch) -> None:
    def _boom(**_kw):
        raise RuntimeError("Stack claims-stack settled in ROLLBACK_COMPLETE: no perms")

    monkeypatch.setattr(pack_mod, "deploy_pack", _boom)
    result = _run(*_required("--wrapper-url", _WRAPPER_URL))
    assert result.exit_code == 1
    assert "ROLLBACK_COMPLETE" in result.output


# ---------------------------------------------------------------------------
# --from-code --build feature
# ---------------------------------------------------------------------------


def test_build_feature_requires_an_explicit_host_template_url(
    calls, pack_project: Path
) -> None:
    """Publishing only the pack means the host template is whatever is already
    out there. Defaulting that URL to anything would bake a guess into the
    wrapper, and the mismatch surfaces as a feature installed against a host
    whose exports it cannot import."""
    result = _run(*_required("--from-code", str(pack_project)))
    assert result.exit_code == 1
    assert "--host-template-url" in result.output
    assert calls.pack_publish == [], "the pack must not be published without a host URL"


def test_build_feature_publishes_the_pack_and_deploys_the_new_wrapper(
    calls, pack_project: Path
) -> None:
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--host-template-url",
            _HOST_URL,
            "--bucket-basename",
            "my-artifacts",
            "--prefix",
            "artifacts/idp",
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.host_publish == [], "--build feature must not republish the host"

    (published,) = calls.pack_publish
    assert published["project_dir"] == pack_project
    # Explicit basename + region, exactly as `publish-pack` resolves it.
    assert published["artifacts_bucket"] == "my-artifacts-us-east-1"
    assert published["artifacts_prefix"] == "artifacts/idp"
    assert published["host_template_url"] == _HOST_URL
    assert published["make_public"] is False

    # The deploy uses the wrapper URL the publish just produced — not a flag.
    assert calls.deploy[0]["wrapper_url"] == _WRAPPER_URL


def test_the_region_flows_to_the_bucket_the_publish_and_the_deploy(
    calls, pack_project: Path
) -> None:
    """One region, three places. A pack published into us-east-1 and deployed in
    eu-west-1 creates a stack that cannot read its own artifacts."""
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--host-template-url",
            _HOST_URL,
            "--region",
            "eu-west-1",
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.bucket[0]["region"] == "eu-west-1"
    assert calls.pack_publish[0]["region"] == "eu-west-1"
    assert calls.deploy[0]["region"] == "eu-west-1"


def test_public_propagates_to_both_the_bucket_and_the_publish(
    calls, pack_project: Path
) -> None:
    """A cross-account pack deploy needs the wrapper anonymously readable. The
    flag reaching the bucket but not the publish (or the reverse) leaves half the
    artifacts private, and the deploy 403s in the *customer's* account."""
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--host-template-url",
            _HOST_URL,
            "--public",
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.bucket[0]["make_public"] is True
    assert calls.pack_publish[0]["make_public"] is True


def test_a_pack_publish_failure_stops_before_deploying(
    calls, pack_project: Path, monkeypatch
) -> None:
    class _Failing:
        def __init__(self, *_a, **_kw):
            pass

        def publish(self, **_kw):
            raise FileNotFoundError("Wrapper template deploy.yaml not found")

    monkeypatch.setattr(pack_mod, "PackPublisher", _Failing)
    result = _run(
        *_required("--from-code", str(pack_project), "--host-template-url", _HOST_URL)
    )
    assert result.exit_code == 1
    assert "Pack publish failed" in result.output
    assert calls.deploy == []


# ---------------------------------------------------------------------------
# --build accelerator / all
# ---------------------------------------------------------------------------


def test_build_accelerator_alone_is_refused_as_a_stale_pairing(
    calls, pack_project: Path
) -> None:
    """Republishing the host without republishing the pack leaves the pack's
    baked host URL pointing at the previous release. The deploy would succeed."""
    result = _run(
        *_required("--from-code", str(pack_project), "--build", "accelerator")
    )
    assert result.exit_code == 1
    assert "--build all" in result.output
    assert calls.deploy == [], "a refused build must not deploy"
    assert calls.pack_publish == []
    # The host publish DOES happen first — the refusal is about the pack being
    # left behind, so the message must arrive after the host was republished.
    assert len(calls.host_publish) == 1


def test_build_all_derives_the_host_url_from_the_publish_it_just_ran(
    calls, pack_project: Path
) -> None:
    """The point of `all`: the URL baked into the wrapper is the one the host
    publish returned, so the two cannot drift. A `--host-template-url` passed
    alongside is ignored rather than winning."""
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--build",
            "all",
            "--host-template-url",
            "https://stale.example.com/old/idp-main.yaml",
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.pack_publish[0]["host_template_url"] == _HOST_URL
    assert "stale.example.com" not in str(calls.pack_publish)


def test_build_all_publishes_the_host_under_the_host_prefix(
    calls, pack_project: Path
) -> None:
    """Host artifacts and feature artifacts share one bucket and must not share
    a prefix — the public-read bucket policy grants `host/*` and `extensions/*`
    separately, so a host published under the feature prefix is unreadable."""
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--build",
            "all",
            "--prefix",
            "artifacts/idp",
        )
    )
    assert result.exit_code == 0, result.output
    host = calls.host_publish[0]
    assert host["artifacts_prefix"] == "host"
    assert calls.pack_publish[0]["artifacts_prefix"] == "artifacts/idp"
    assert host["artifacts_bucket"] == calls.pack_publish[0]["artifacts_bucket"]


def test_a_custom_host_artifacts_prefix_is_honoured(calls, pack_project: Path) -> None:
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--build",
            "all",
            "--host-artifacts-prefix",
            "host/v2",
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.host_publish[0]["artifacts_prefix"] == "host/v2"


def test_source_dir_is_discovered_by_the_full_repo_signature_not_publish_py(
    calls, pack_project: Path
) -> None:
    """A pack directory has its own thin `publish.py`. Stopping at the first one
    found while walking upward would run the *pack's* publisher as if it were the
    accelerator's, publishing the wrong template to `host/idp-main.yaml` — and
    the wrapper would then create a host stack from a feature template."""
    result = _run(*_required("--from-code", str(pack_project), "--build", "all"))
    assert result.exit_code == 0, result.output
    discovered = calls.host_publish[0]["source_dir"]
    assert discovered == pack_project.parent.parent
    assert (discovered / "Makefile").is_file()
    assert discovered != pack_project


def test_an_explicit_source_dir_skips_discovery(calls, pack_project: Path) -> None:
    """An operator whose checkout is laid out differently must be able to say so;
    the flag is taken as given, without re-deriving it."""
    result = _run(
        *_required(
            "--from-code",
            str(pack_project),
            "--build",
            "all",
            "--source-dir",
            str(pack_project),
        )
    )
    assert result.exit_code == 0, result.output
    assert calls.host_publish[0]["source_dir"] == pack_project


def test_an_undiscoverable_repo_root_is_refused_naming_the_three_files(
    calls, tmp_path: Path
) -> None:
    """Better to refuse than to guess. A guessed root publishes some other
    template under `host/idp-main.yaml`, and the wrapper deploys it."""
    lonely = tmp_path / "standalone-pack"
    lonely.mkdir()
    (lonely / "publish.py").write_text("# only this\n", encoding="utf-8")
    (lonely / "feature.yaml").write_text("featureId: x\n", encoding="utf-8")

    result = _run(*_required("--from-code", str(lonely), "--build", "all"))
    assert result.exit_code == 1
    assert "--source-dir" in result.output
    assert "Makefile" in result.output
    assert calls.host_publish == [], "a lone publish.py must not be taken as the root"
    assert calls.deploy == []


def test_a_host_publish_failure_stops_before_publishing_the_pack(
    calls, pack_project: Path, monkeypatch
) -> None:
    def _boom(**_kw):
        raise RuntimeError("idp-cli publish exited 1")

    monkeypatch.setattr(pack_mod, "publish_host_accelerator", _boom)
    result = _run(*_required("--from-code", str(pack_project), "--build", "all"))
    assert result.exit_code == 1
    assert "Host accelerator publish failed" in result.output
    assert calls.pack_publish == []
    assert calls.deploy == []


def test_an_omitted_bucket_basename_auto_creates_the_per_account_bucket(
    calls, pack_project: Path
) -> None:
    result = _run(
        *_required("--from-code", str(pack_project), "--host-template-url", _HOST_URL)
    )
    assert result.exit_code == 0, result.output
    assert calls.bucket == [
        {"region": "us-east-1", "console": _AnyConsole(), "make_public": False}
    ]
    assert (
        calls.pack_publish[0]["artifacts_bucket"]
        == "idp-accelerator-artifacts-123456789012-us-east-1"
    )


class _AnyConsole:
    """Compares equal to any rich Console, so the recorded kwargs can be
    asserted as a whole dict without pinning the console instance."""

    def __eq__(self, other: object) -> bool:
        from rich.console import Console

        return isinstance(other, Console)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<any Console>"


def test_an_unreadable_from_code_path_is_a_usage_error(calls, tmp_path: Path) -> None:
    """click's `exists=True` — exit 2, before any AWS call."""
    result = _run(*_required("--from-code", str(tmp_path / "missing")))
    assert result.exit_code == 2
    assert calls.bucket == []
