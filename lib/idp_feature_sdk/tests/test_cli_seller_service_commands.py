# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The `idp-feature-cli seller-service` command group: `preflight`, `deploy`,
`export-trust-bundle`, `publish-endpoint` and `activations`.

These five commands are the seller side of a paid extension, and four of the five
have the same shape of failure: they succeed, and the thing they configured then
refuses every paying customer. The seller service answers an activation by
looking the product up in a registry baked into its Lambda's environment and
calling `SearchAgreements` as the product's *owner*; a service deployed into the
wrong account, or with a registry that did not survive the deploy, answers every
request with the same "not entitled" body it uses for a genuine non-subscriber.
There is no log line and no failed health check. The whole reason the preflight
and the post-deploy registry read-back exist is that nothing downstream can tell
the difference.

`export-trust-bundle` and `publish-endpoint` fail in the mirror-image way. The
bundle is the material an extension embeds at build time, and its `kid` has to be
byte-identical to what the service puts in the token — a bundle built from the
wrong key or a different ARN spelling verifies nothing, and the extension
fails closed in a customer's account the seller cannot reach. The pointer is
published per-region, one bucket per region, because an extension reads it from
its *own* regional bucket; a pointer written to one region only leaves every
other region's installs on their compiled-in fallback.

So the tests assert the values that carry those guarantees: which account the
preflight resolved and whether ownership was actually verified, the exact argv
the deploy hands to `sam`, the bucket *per region* the pointer landed in and its
key and ACL, and the `kid`/PEM pairing in the bundle. The AWS side is moto where
moto has the service and a recorded fake where it does not (the Marketplace
Catalog API is not mockable).
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import boto3
import pytest
from click.testing import CliRunner
from moto import mock_aws

import idp_feature_sdk.cli as cli_mod
from idp_feature_sdk.cli import main

pytestmark = pytest.mark.unit

_PRODUCT = "prod-a5ee62vs2xa72"
_OTHER_PRODUCT = "prod-b1zz99qq0xb11"
_REGISTRY = json.dumps({_PRODUCT: {"productCode": "abc", "allowFreeTier": True}})
# Every account id below is one of the placeholder ids `scripts/check_account_ids.py`
# allowlists (AWS's canonical examples and hand-typed repeated-digit fixtures). They are
# kept DISTINCT from each other on purpose: the mismatch assertions below would pass for
# the wrong reason if any two collapsed into the same value.
_SELLER_ACCOUNT = "111122223333"
_OTHER_ACCOUNT = "222222222222"


class _Sts:
    def __init__(self, account: str = _SELLER_ACCOUNT) -> None:
        self.account = account

    def get_caller_identity(self):
        return {
            "Account": self.account,
            "Arn": f"arn:aws:sts::{self.account}:assumed-role/Seller/session",
        }


class _Catalog:
    """Marketplace Catalog stand-in; moto does not implement this service."""

    def __init__(self, entities=None, error: Exception | None = None) -> None:
        self._entities = entities if entities is not None else []
        self._error = error
        self.calls = 0

    def list_entities(self, **_kwargs):
        self.calls += 1
        if self._error:
            raise self._error
        return {"EntitySummaryList": self._entities}


def _entity(entity_id=_PRODUCT, name="Auto Optimizer", visibility="Limited"):
    return {"EntityId": entity_id, "Name": name, "Visibility": visibility}


@pytest.fixture
def seller_clients(monkeypatch):
    """Install a (sts, catalog) pair and hand back the catalog so the test can
    assert whether it was consulted at all."""

    def _install(sts=None, catalog=None):
        sts = sts or _Sts()
        catalog = catalog or _Catalog([_entity()])
        monkeypatch.setattr(cli_mod, "_seller_clients", lambda _region: (sts, catalog))
        return catalog

    return _install


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    """Stop Rich hard-wrapping the command output at 80 columns.

    The module-level ``Console()`` in ``cli.py`` recomputes its width per render
    from ``COLUMNS``, and under ``CliRunner`` there is no terminal, so warnings
    and table cells are wrapped or ellipsised mid-token. Asserting against the
    wrapped form would make these tests sensitive to wording length rather than
    to content, and a truncated buyer account id cannot be asserted at all.
    """
    monkeypatch.setenv("COLUMNS", "400")


def _flat(text: str) -> str:
    """Collapse runs of whitespace so an assertion is about the words printed
    rather than where Rich chose to break the line."""
    return " ".join(text.split())


def _run(*args: str):
    return CliRunner().invoke(main, ["seller-service", *args])


# ---------------------------------------------------------------------------
# _seller_clients itself
# ---------------------------------------------------------------------------


def test_the_catalog_client_is_built_in_the_region_it_was_asked_for(
    monkeypatch,
) -> None:
    """The Marketplace Catalog and Agreement APIs exist in us-east-1 only, and
    the `--region` flag is what pins them. A client built in the ambient session
    region would make the preflight's ownership check unreachable from a machine
    configured for anywhere else — and the preflight would then be skipped-by-
    error rather than run."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")

    sts, catalog = cli_mod._seller_clients("us-east-1")
    assert catalog.meta.region_name == "us-east-1"
    assert catalog.meta.service_model.service_name == "marketplace-catalog"
    assert sts.meta.service_model.service_name == "sts"


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_preflight_names_the_account_and_the_owned_product(seller_clients) -> None:
    catalog = seller_clients()
    result = _run("preflight", "--product-registry", _REGISTRY)
    assert result.exit_code == 0, result.output
    assert catalog.calls == 1, "ownership must actually be checked"
    assert _SELLER_ACCOUNT in result.output
    # The product's name and visibility are printed so an operator can see they
    # are pointed at the listing they meant, not merely at "a" seller account.
    assert "Auto Optimizer" in result.output
    assert "Limited" in result.output
    assert "Preflight passed" in result.output


def test_preflight_refuses_an_account_that_does_not_own_the_product(
    seller_clients,
) -> None:
    """The mistake the preflight exists for. Deployed here, every activation is
    refused with a body indistinguishable from "not subscribed"."""
    seller_clients(catalog=_Catalog([_entity(entity_id=_OTHER_PRODUCT)]))
    result = _run("preflight", "--product-registry", _REGISTRY)
    assert result.exit_code == 1
    assert _PRODUCT in result.output
    assert "NOT owned" in result.output


def test_preflight_refuses_an_account_id_mismatch_before_touching_the_catalog(
    seller_clients,
) -> None:
    catalog = seller_clients()
    result = _run(
        "preflight",
        "--product-registry",
        _REGISTRY,
        "--seller-account-id",
        _OTHER_ACCOUNT,
    )
    assert result.exit_code == 1
    assert "Account mismatch" in result.output
    assert catalog.calls == 0


def test_preflight_rejects_a_product_code_passed_as_a_product_id(
    seller_clients,
) -> None:
    """The likeliest data error: the product *code* and the entity id are
    different values for the same listing, and only the entity id filters
    `SearchAgreements`. A code accepted here deploys a registry that matches
    nothing."""
    seller_clients()
    result = _run("preflight", "--product-registry", json.dumps({"abc123": {}}))
    assert result.exit_code == 1
    assert "prod-" in result.output


def test_skip_ownership_check_says_what_is_being_asserted(seller_clients) -> None:
    """The escape hatch has to be loud. Its whole risk is that the resulting
    misconfiguration is silent, so the warning names the consequence rather than
    saying "skipped"."""
    catalog = seller_clients()
    result = _run(
        "preflight", "--product-registry", _REGISTRY, "--skip-ownership-check"
    )
    assert result.exit_code == 0, result.output
    assert catalog.calls == 0, "the catalog must not be consulted at all"
    assert "locked out" in result.output
    assert "asserting" in result.output


def test_skip_ownership_check_still_enforces_the_account_assertion(
    seller_clients,
) -> None:
    """The two flags are independent guards. Skipping the ownership lookup must
    not also switch off `--seller-account-id`, which needs no Catalog call."""
    seller_clients()
    result = _run(
        "preflight",
        "--product-registry",
        _REGISTRY,
        "--skip-ownership-check",
        "--seller-account-id",
        _OTHER_ACCOUNT,
    )
    assert result.exit_code == 1
    assert "Account mismatch" in result.output


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


_SERVICE_TEMPLATE = (
    dedent("""
    AWSTemplateFormatVersion: '2010-09-09'
    Transform: AWS::Serverless-2016-10-31
    Mappings:
      Service:
        Version:
          ServiceVersion:
            Value: '0.4.1'
    Parameters:
      ProductRegistryJson:
        Type: String
      AllowedAccounts:
        Type: String
        Default: ''
      TokenTtlSeconds:
        Type: Number
        Default: 3600
      AgreementRegion:
        Type: String
        Default: us-east-1
    Resources:
      ActivateFunction:
        Type: AWS::Serverless::Function
""").strip()
    + "\n"
)


@pytest.fixture
def service_dir_stub(tmp_path: Path, monkeypatch) -> Path:
    """A stand-in ``seller-entitlement-service`` directory, installed as the
    result of ``find_seller_service_dir``. The real template is exercised by the
    existing argv tests; here the point is the command's own control flow."""
    service = tmp_path / "feature-platform" / "seller-entitlement-service"
    service.mkdir(parents=True)
    (service / "template.yaml").write_text(_SERVICE_TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(cli_mod, "find_seller_service_dir", lambda: service)
    return service


@pytest.fixture
def recorded_commands(monkeypatch) -> list:
    ran: list = []
    monkeypatch.setattr(
        cli_mod, "run_command", lambda cmd, cwd=None: ran.append((cmd, cwd))
    )
    return ran


@pytest.fixture
def verified_registry(monkeypatch) -> list:
    """Substitute the post-deploy read-back and record what it was asked to
    confirm. Returning the expected set is the success case."""
    seen: list = []

    def _verify(*, cfn_client, lambda_client, stack_name, expected_product_ids):
        seen.append(
            {"stack_name": stack_name, "expected_product_ids": expected_product_ids}
        )
        return dict.fromkeys(expected_product_ids, {})

    monkeypatch.setattr(cli_mod, "verify_deployed_registry", _verify)
    return seen


def test_deploy_without_a_checkout_says_a_checkout_is_needed(
    seller_clients, monkeypatch
) -> None:
    """The template and Lambda source ship in the repository, not the wheel. A
    traceback here would send the operator looking for a broken install."""
    seller_clients()
    monkeypatch.setattr(cli_mod, "find_seller_service_dir", lambda: None)
    result = _run("deploy", "--product-registry", _REGISTRY, "--yes")
    assert result.exit_code == 1
    assert "seller-entitlement-service" in result.output


def test_deploy_runs_the_preflight_before_building_anything(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """Ordering is the guard. A `sam build`/`sam deploy` that starts before the
    ownership check has already put the stack in the wrong account by the time
    the check fails."""
    seller_clients(catalog=_Catalog([_entity(entity_id=_OTHER_PRODUCT)]))
    result = _run("deploy", "--product-registry", _REGISTRY, "--yes")
    assert result.exit_code == 1
    assert "NOT owned" in result.output
    assert recorded_commands == []
    assert verified_registry == []


def test_deploy_aborts_when_the_confirmation_is_declined(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    seller_clients()
    result = CliRunner().invoke(
        main,
        ["seller-service", "deploy", "--product-registry", _REGISTRY],
        input="n\n",
    )
    assert result.exit_code == 1
    assert "Aborted." in result.output
    assert recorded_commands == []


def test_the_confirmation_defaults_to_no_on_a_bare_newline(
    seller_clients, service_dir_stub, recorded_commands
) -> None:
    """Pressing Enter must not deploy. This command writes an IAM-capable stack
    into whichever account the credentials resolve to."""
    seller_clients()
    result = CliRunner().invoke(
        main,
        ["seller-service", "deploy", "--product-registry", _REGISTRY],
        input="\n",
    )
    assert result.exit_code == 1
    assert recorded_commands == []


def test_deploy_builds_then_deploys_and_passes_the_compacted_registry(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """Two subprocesses in order, both from the service directory, and the
    registry reaching `sam` single-quoted and compact — SAM re-tokenizes the
    override string, and an unquoted JSON blob is truncated at the first double
    quote, which deploys a registry of exactly `{`."""
    seller_clients()
    result = _run("deploy", "--product-registry", _REGISTRY, "--yes")
    assert result.exit_code == 0, result.output

    assert [cmd[:2] for cmd, _cwd in recorded_commands] == [
        ["sam", "build"],
        ["sam", "deploy"],
    ]
    assert all(cwd == service_dir_stub for _cmd, cwd in recorded_commands)

    deploy_argv = recorded_commands[1][0]
    compact = json.dumps(json.loads(_REGISTRY), separators=(",", ":"), sort_keys=True)
    assert f"ProductRegistryJson='{compact}'" in deploy_argv
    assert "--stack-name" in deploy_argv
    assert "idp-seller-entitlement" in deploy_argv


def test_the_agreement_region_is_not_taken_from_the_deploy_region(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """`--region` places the stack; the Agreement API host is a separate choice
    and resolves in us-east-1 only. Deriving one from the other names a host that
    does not exist, and the failure lands at activation in a buyer's account."""
    seller_clients()
    result = _run(
        "deploy",
        "--product-registry",
        _REGISTRY,
        "--region",
        "us-west-2",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    deploy_argv = recorded_commands[1][0]
    assert "AgreementRegion='us-east-1'" in deploy_argv
    assert "AgreementRegion='us-west-2'" not in deploy_argv
    # The stack itself does go to the requested region.
    assert deploy_argv[deploy_argv.index("--region") + 1] == "us-west-2"


def test_a_non_default_agreement_region_is_warned_about_before_the_deploy(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """Warned, not refused — pinning an allowed list in code would block a region
    AWS may add. But the warning has to precede the deploy, because the failure
    it predicts is remote and silent."""
    seller_clients()
    result = _run(
        "deploy",
        "--product-registry",
        _REGISTRY,
        "--agreement-region",
        "eu-west-1",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "agreement-marketplace.eu-west-1.amazonaws.com will not resolve" in flat
    assert "EVERY activation will fail" in flat
    assert "AgreementRegion='eu-west-1'" in recorded_commands[1][0]


def test_an_allow_listed_account_is_called_out_as_a_free_grant(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """Each entry is an account receiving a paid product without a subscription
    check. That has to be visible in the pre-deploy summary, not only in the
    template parameter."""
    seller_clients()
    result = _run(
        "deploy",
        "--product-registry",
        _REGISTRY,
        "--allowed-accounts",
        f"{_OTHER_ACCOUNT},999988887777",
        "--yes",
    )
    assert result.exit_code == 0, result.output
    assert _OTHER_ACCOUNT in result.output
    assert "skip the subscription check" in _flat(result.output)
    assert f"AllowedAccounts='{_OTHER_ACCOUNT},999988887777'" in recorded_commands[1][0]


def test_no_allow_list_means_the_override_is_not_sent_at_all(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """Absent must not become `AllowedAccounts=''`-by-another-route: the template
    default is the authority, and sending an empty override is how a later
    template change to that default would be silently overridden."""
    seller_clients()
    assert _run("deploy", "--product-registry", _REGISTRY, "--yes").exit_code == 0
    assert not any("AllowedAccounts" in a for a in recorded_commands[1][0])


def test_the_service_version_from_the_template_is_echoed(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    seller_clients()
    result = _run("deploy", "--product-registry", _REGISTRY, "--yes")
    assert result.exit_code == 0, result.output
    assert "0.4.1" in result.output


def test_the_deployed_registry_is_read_back_for_the_products_preflighted(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    """The read-back must check the same product set the preflight verified
    ownership of. Checking a different set (or none) is how a registry that did
    not survive the deploy reaches customers."""
    seller_clients(catalog=_Catalog([_entity(), _entity(entity_id=_OTHER_PRODUCT)]))
    two = json.dumps({_PRODUCT: {}, _OTHER_PRODUCT: {}})
    result = _run("deploy", "--product-registry", two, "--stack-name", "s1", "--yes")
    assert result.exit_code == 0, result.output
    assert verified_registry == [
        {"stack_name": "s1", "expected_product_ids": [_PRODUCT, _OTHER_PRODUCT]}
    ]
    assert "serves 2 product(s)" in result.output


def test_a_registry_that_did_not_survive_the_deploy_fails_the_command(
    seller_clients, service_dir_stub, recorded_commands, monkeypatch
) -> None:
    """Deployed-but-broken must exit non-zero. Exiting 0 with a warning is how an
    endpoint that refuses every customer gets announced."""
    from idp_feature_sdk.seller_service import SellerServiceError

    seller_clients()

    def _boom(**_kw):
        raise SellerServiceError("The product registry did not survive deployment")

    monkeypatch.setattr(cli_mod, "verify_deployed_registry", _boom)
    result = _run("deploy", "--product-registry", _REGISTRY, "--yes")
    assert result.exit_code == 1
    assert "did not survive" in result.output
    assert "Seller Entitlement Service deployed" not in result.output


def test_guided_is_forwarded_to_sam(
    seller_clients, service_dir_stub, recorded_commands, verified_registry
) -> None:
    seller_clients()
    assert (
        _run("deploy", "--product-registry", _REGISTRY, "--guided", "--yes").exit_code
        == 0
    )
    assert "--guided" in recorded_commands[1][0]


# ---------------------------------------------------------------------------
# export-trust-bundle
# ---------------------------------------------------------------------------


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def _seller_stack(cfn, outputs: dict, stack_name: str = "idp-seller-entitlement"):
    """Create a stack whose Outputs are exactly ``outputs``."""
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {"T": {"Type": "AWS::SNS::Topic"}},
        "Outputs": {k: {"Value": v} for k, v in outputs.items()},
    }
    cfn.create_stack(StackName=stack_name, TemplateBody=json.dumps(template))


def test_export_trust_bundle_pairs_the_kid_with_the_key_it_fetched(aws_env) -> None:
    """`kid` must be byte-identical to the signing key ARN the service puts in
    the token, and the PEM must be that key's public half. A bundle whose kid and
    key disagree verifies nothing, and the extension fails closed in a customer
    account the seller cannot reach."""
    with mock_aws():
        kms = boto3.client("kms", region_name="us-east-1")
        key_arn = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")[
            "KeyMetadata"
        ]["Arn"]
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(
            cfn,
            {
                "ActivationEndpoint": "https://abc123.execute-api.us-east-1.amazonaws.com/prod/activate",
                "TokenSigningKeyArn": key_arn,
                "ServiceVersion": "0.4.1",
            },
        )

        result = _run("export-trust-bundle")
        assert result.exit_code == 0, result.output
        bundle = json.loads(result.output)

    assert bundle["kid"] == key_arn
    assert bundle["signingAlgorithm"] == "RSASSA_PSS_SHA_256"
    assert bundle["activationEndpoint"].startswith("https://")
    assert bundle["serviceVersion"] == "0.4.1"
    assert bundle["publicKeyPem"].startswith("-----BEGIN PUBLIC KEY-----")
    assert bundle["exportedAt"].endswith("Z")
    # The pointer's deliberate asymmetry: the key belongs HERE and not in the
    # runtime pointer, so the bundle must actually carry it.
    assert len(bundle["publicKeyPem"].splitlines()) > 3


def test_export_trust_bundle_writes_the_pem_alongside_the_json(
    aws_env, tmp_path: Path
) -> None:
    """Two files, and the PEM file must be exactly the bundle's `publicKeyPem` —
    an extension build that embeds the .pem and a verifier that reads the JSON
    must end up with the same key."""
    with mock_aws():
        kms = boto3.client("kms", region_name="us-east-1")
        key_arn = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")[
            "KeyMetadata"
        ]["Arn"]
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(
            cfn,
            {
                "ActivationEndpoint": "https://abc.execute-api.us-east-1.amazonaws.com/p/a",
                "TokenSigningKeyArn": key_arn,
            },
        )
        out = tmp_path / "trust"
        result = _run("export-trust-bundle", "--output-dir", str(out))
        assert result.exit_code == 0, result.output

    bundle = json.loads((out / "activation-trust.json").read_text(encoding="utf-8"))
    pem = (out / "activation-public-key.pem").read_text(encoding="utf-8")
    assert pem == bundle["publicKeyPem"]
    assert bundle["kid"] == key_arn
    # A stack without a ServiceVersion output still exports; the field is empty
    # rather than the export failing.
    assert bundle["serviceVersion"] == ""
    assert key_arn in result.output


def test_export_trust_bundle_refuses_a_stack_with_no_activation_endpoint(
    aws_env,
) -> None:
    """Unlike ServiceVersion, the endpoint is not optional — a bundle without it
    gives the extension no fallback at all."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"SomethingElse": "x"})
        result = _run("export-trust-bundle")
    assert result.exit_code == 1
    assert "ActivationEndpoint" in result.output


def test_export_trust_bundle_refuses_an_encrypt_only_key(aws_env) -> None:
    """A key whose usage is ENCRYPT_DECRYPT is not the token signing key.
    Exporting its public half produces a bundle that fails every verification —
    and nothing about the export would have looked wrong."""
    with mock_aws():
        kms = boto3.client("kms", region_name="us-east-1")
        key_arn = kms.create_key(KeyUsage="ENCRYPT_DECRYPT", KeySpec="RSA_2048")[
            "KeyMetadata"
        ]["Arn"]
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(
            cfn,
            {
                "ActivationEndpoint": "https://a.execute-api.us-east-1.amazonaws.com/p/a",
                "TokenSigningKeyArn": key_arn,
            },
        )
        result = _run("export-trust-bundle")
    assert result.exit_code == 1
    assert "SIGN_VERIFY" in result.output


def test_export_trust_bundle_reports_a_missing_stack_actionably(aws_env) -> None:
    with mock_aws():
        boto3.client("cloudformation", region_name="us-east-1")
        result = _run("export-trust-bundle", "--stack-name", "not-deployed")
    assert result.exit_code == 1
    assert "seller-service deploy" in result.output


# ---------------------------------------------------------------------------
# publish-endpoint
# ---------------------------------------------------------------------------


def _pointer(s3, bucket: str, key: str) -> dict:
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def _is_public(s3, bucket: str, key: str) -> bool:
    return any(
        g.get("Grantee", {}).get("URI", "").endswith("AllUsers")
        and g.get("Permission") == "READ"
        for g in s3.get_object_acl(Bucket=bucket, Key=key)["Grants"]
    )


_ENDPOINT = "https://abc123.execute-api.us-east-1.amazonaws.com/prod/activate"

#: A KMS key ARN whose account field is the single digit `1`, matching the only other
#: KMS ARN in this repository's tests. The 12-digit AWS documentation accounts
#: (`111122223333` and friends) are allowlisted by `scripts/check_account_ids.py` but a
#: KMS ARN carrying one is refused at push time by the outbound secret scanner, which
#: reads the ARN shape rather than the account. So this is not a realistic-looking
#: placeholder by oversight -- do not "correct" it to a 12-digit account, or the branch
#: becomes unpushable.
_SIGNING_KEY_ARN = "arn:aws:kms:us-east-1:1:key/abc"


def test_the_pointer_is_written_to_one_bucket_per_region(aws_env) -> None:
    """An installed extension reads the pointer from the bucket in *its own*
    region. Publishing to only one region leaves every other region's installs
    on their compiled-in URL, which is the URL this indirection exists to be able
    to change."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        east = boto3.client("s3", region_name="us-east-1")
        west = boto3.client("s3", region_name="us-west-2")
        east.create_bucket(Bucket="artifacts-us-east-1")
        west.create_bucket(
            Bucket="artifacts-us-west-2",
            CreateBucketConfiguration={"LocationConstraint": "us-west-2"},
        )

        result = _run(
            "publish-endpoint",
            "--feature-id",
            "auto-optimizer",
            "--bucket-basename",
            "artifacts",
            "--artifact-regions",
            "us-east-1, us-west-2",
        )
        assert result.exit_code == 0, result.output

        key = "extensions/auto-optimizer/activation.json"
        for client, bucket in (
            (east, "artifacts-us-east-1"),
            (west, "artifacts-us-west-2"),
        ):
            doc = _pointer(client, bucket, key)
            assert doc["activationEndpoint"] == _ENDPOINT
            assert doc["schemaVersion"] == "1.0"
            assert _is_public(client, bucket, key)
            assert bucket in result.output


def test_every_named_feature_gets_its_own_pointer(aws_env) -> None:
    """One endpoint can serve several extensions, and each reads the pointer from
    its own `extensions/<id>/` prefix. A pointer written for only the first
    feature leaves the rest unable to find the new endpoint."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts-us-east-1")

        result = _run(
            "publish-endpoint",
            "--feature-id",
            "auto-optimizer",
            "--feature-id",
            "claims-review",
            "--bucket-basename",
            "artifacts",
            "--artifact-regions",
            "us-east-1",
        )
        assert result.exit_code == 0, result.output
        keys = {
            o["Key"]
            for o in s3.list_objects_v2(Bucket="artifacts-us-east-1")["Contents"]
        }
    assert keys == {
        "extensions/auto-optimizer/activation.json",
        "extensions/claims-review/activation.json",
    }


def test_the_pointer_sits_under_the_catalog_prefix_when_one_is_given(aws_env) -> None:
    """The key has to match the prefix the catalog's `templateKey` uses, or the
    extension reads a 404 and falls back to its compiled-in URL — silently."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts-us-east-1")

        result = _run(
            "publish-endpoint",
            "--feature-id",
            "auto-optimizer",
            "--bucket-basename",
            "artifacts",
            "--artifact-regions",
            "us-east-1",
            "--prefix",
            "/artifacts/genai-idp-mp/",
        )
        assert result.exit_code == 0, result.output
        keys = {
            o["Key"]
            for o in s3.list_objects_v2(Bucket="artifacts-us-east-1")["Contents"]
        }
    assert keys == {"artifacts/genai-idp-mp/extensions/auto-optimizer/activation.json"}
    assert not any("//" in k for k in keys)


def test_the_pointer_carries_the_key_id_as_a_hint_but_no_key_material(
    aws_env,
) -> None:
    """`signingKeyId` is a hint for choosing among keys the extension already
    embeds. Key material here would make the artifact bucket a forgery trust
    root — whoever can write the bucket could substitute both a hostile endpoint
    and the key that validates its tokens."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(
            cfn,
            {
                "ActivationEndpoint": _ENDPOINT,
                "TokenSigningKeyArn": _SIGNING_KEY_ARN,
                "ServiceVersion": "0.4.1",
            },
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts-us-east-1")
        assert (
            _run(
                "publish-endpoint",
                "--feature-id",
                "f",
                "--bucket-basename",
                "artifacts",
                "--artifact-regions",
                "us-east-1",
            ).exit_code
            == 0
        )
        doc = _pointer(s3, "artifacts-us-east-1", "extensions/f/activation.json")
    assert doc["signingKeyId"] == _SIGNING_KEY_ARN
    assert doc["serviceVersion"] == "0.4.1"
    assert "publicKeyPem" not in doc
    assert "publicKey" not in doc
    assert "key" not in doc


def test_private_suppresses_the_public_read_acl(aws_env) -> None:
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts-us-east-1")
        assert (
            _run(
                "publish-endpoint",
                "--feature-id",
                "f",
                "--bucket-basename",
                "artifacts",
                "--artifact-regions",
                "us-east-1",
                "--private",
            ).exit_code
            == 0
        )
        assert not _is_public(s3, "artifacts-us-east-1", "extensions/f/activation.json")


def test_an_empty_artifact_regions_list_is_refused(aws_env) -> None:
    """Exiting 0 having written nothing is the worst outcome: the operator
    believes the new endpoint is live everywhere."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        result = _run(
            "publish-endpoint",
            "--feature-id",
            "f",
            "--bucket-basename",
            "artifacts",
            "--artifact-regions",
            " , ",
        )
    assert result.exit_code == 1
    assert "no regions" in result.output


def test_a_write_failure_in_one_region_fails_the_command(aws_env) -> None:
    """Half-published is a real state and must not read as success — the regions
    that did get the pointer now disagree with the ones that did not."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="artifacts-us-east-1")
        # us-west-2's bucket is deliberately absent.
        result = _run(
            "publish-endpoint",
            "--feature-id",
            "f",
            "--bucket-basename",
            "artifacts",
            "--artifact-regions",
            "us-east-1,us-west-2",
        )
    assert result.exit_code == 1
    assert "artifacts-us-west-2" in result.output


def test_an_http_endpoint_output_is_refused(aws_env) -> None:
    """Extensions send SigV4-signed credentials to this URL. Publishing a plain
    http pointer would have every install send them in clear text."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": "http://insecure.example.com/a"})
        result = _run(
            "publish-endpoint",
            "--feature-id",
            "f",
            "--bucket-basename",
            "artifacts",
            "--artifact-regions",
            "us-east-1",
        )
    assert result.exit_code == 1
    assert "https" in result.output


# ---------------------------------------------------------------------------
# activations
# ---------------------------------------------------------------------------


_ROSTER_TABLE = "idp-seller-activations"


def _make_roster(items: list[dict], region: str = "us-east-1") -> None:
    ddb = boto3.client("dynamodb", region_name=region)
    ddb.create_table(
        TableName=_ROSTER_TABLE,
        KeySchema=[
            {"AttributeName": "buyerAccountId", "KeyType": "HASH"},
            {"AttributeName": "productId", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "buyerAccountId", "AttributeType": "S"},
            {"AttributeName": "productId", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "ProductIndex",
                "KeySchema": [{"AttributeName": "productId", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    table = boto3.resource("dynamodb", region_name=region).Table(_ROSTER_TABLE)
    for item in items:
        table.put_item(Item=item)


def _row(
    buyer: str,
    product: str = _PRODUCT,
    outcome: str = "granted",
    last: str = "2026-08-02T10:00:00Z",
    **extra,
) -> dict:
    return {
        "buyerAccountId": buyer,
        "productId": product,
        "lastOutcome": outcome,
        "attemptCount": 3,
        "grantedCount": 2 if outcome == "granted" else 0,
        "firstAttemptAt": "2026-08-01T09:00:00Z",
        "lastAttemptAt": last,
        **extra,
    }


def test_activations_resolves_the_table_from_the_stack_outputs(aws_env) -> None:
    """The roster table name is a stack output rather than an operator flag so a
    redeploy that renames the table cannot leave the CLI reading an old, frozen
    one and reporting no activity."""
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationsTableName": _ROSTER_TABLE})
        _make_roster([_row("333333333333")])
        result = _run("activations")
    assert result.exit_code == 0, result.output
    assert "333333333333" in result.output
    assert "granted" in result.output


def test_activations_can_read_a_table_directly_without_a_stack(aws_env) -> None:
    with mock_aws():
        boto3.client("cloudformation", region_name="us-east-1")
        _make_roster([_row("333333333333")])
        result = _run("activations", "--table-name", _ROSTER_TABLE)
    assert result.exit_code == 0, result.output
    assert "333333333333" in result.output


def test_activations_reports_an_empty_roster_as_empty_not_as_an_error(
    aws_env,
) -> None:
    """No activations yet and a broken read must not look alike — one is a new
    listing, the other is a service nobody can reach."""
    with mock_aws():
        _make_roster([])
        result = _run("activations", "--table-name", _ROSTER_TABLE)
    assert result.exit_code == 0, result.output
    assert "No activation attempts recorded yet" in result.output


def test_activations_json_output_is_machine_readable(aws_env) -> None:
    """A release script pipes this, so it must be parseable JSON with the field
    names the dataclass declares — not a Rich-rendered table."""
    with mock_aws():
        _make_roster([_row("333333333333", lastFreeTier=True, lastDetail="free tier")])
        result = _run("activations", "--table-name", _ROSTER_TABLE, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == [
        {
            "buyer_account_id": "333333333333",
            "product_id": _PRODUCT,
            "last_outcome": "granted",
            "attempt_count": 3,
            "granted_count": 2,
            "first_attempt_at": "2026-08-01T09:00:00Z",
            "last_attempt_at": "2026-08-02T10:00:00Z",
            "free_tier": True,
            "detail": "free tier",
            "service_version": "",
        }
    ]


def test_activations_surfaces_the_refusal_reasons_for_refused_pairs(
    aws_env,
) -> None:
    """A refused row is the actionable one, and the reason is the only thing that
    distinguishes "not subscribed" from a misconfigured registry. Rendering the
    table without it would hide the diagnosis the roster exists to provide."""
    with mock_aws():
        _make_roster(
            [
                _row("333333333333"),
                _row(
                    "999999999999",
                    outcome="refused",
                    lastDetail="no agreement found for product",
                ),
            ]
        )
        result = _run("activations", "--table-name", _ROSTER_TABLE)
    assert result.exit_code == 0, result.output
    assert "1 account/product pair(s) last refused" in result.output
    assert "no agreement found for product" in result.output


def test_activations_renders_a_refused_row_with_no_detail_without_crashing(
    aws_env,
) -> None:
    with mock_aws():
        _make_roster([_row("999999999999", outcome="refused")])
        result = _run("activations", "--table-name", _ROSTER_TABLE)
    assert result.exit_code == 0, result.output
    assert "(no detail)" in result.output


def test_activations_filters_by_outcome_and_product(aws_env) -> None:
    with mock_aws():
        _make_roster(
            [
                _row("333333333333", outcome="granted"),
                _row("999999999999", outcome="refused"),
            ]
        )
        granted = _run(
            "activations",
            "--table-name",
            _ROSTER_TABLE,
            "--outcome",
            "granted",
            "--json",
        )
        assert granted.exit_code == 0, granted.output
        assert [r["buyer_account_id"] for r in json.loads(granted.output)] == [
            "333333333333"
        ]

        by_product = _run(
            "activations",
            "--table-name",
            _ROSTER_TABLE,
            "--product-id",
            _OTHER_PRODUCT,
            "--json",
        )
        assert by_product.exit_code == 0, by_product.output
        assert json.loads(by_product.output) == []


def test_activations_reports_a_missing_roster_output_as_a_redeploy_hint(
    aws_env,
) -> None:
    with mock_aws():
        cfn = boto3.client("cloudformation", region_name="us-east-1")
        _seller_stack(cfn, {"ActivationEndpoint": _ENDPOINT})
        result = _run("activations")
    assert result.exit_code == 1
    assert "ActivationsTableName" in result.output


def test_activations_reports_an_unreadable_table_rather_than_an_empty_roster(
    aws_env,
) -> None:
    """The distinction that matters operationally: a read failure must not render
    as "no activation attempts recorded yet"."""
    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1")
        result = _run("activations", "--table-name", "no-such-table")
    assert result.exit_code == 1
    assert "Could not read the activation roster" in result.output
    assert "No activation attempts" not in result.output
