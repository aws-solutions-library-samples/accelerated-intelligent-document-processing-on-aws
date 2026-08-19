# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the Seller Entitlement Service preflight + deploy helpers.

The preflight is a safety guard, and its whole justification is that the failure
it prevents is SILENT: a service deployed into the wrong account comes up healthy
and then refuses every activation, because SearchAgreements(PartyType=Proposer)
returns an empty list rather than an error for a product the caller doesn't own.
So the tests here are mostly about refusing, not about succeeding.
"""

from __future__ import annotations

import json

import pytest

from idp_feature_sdk.seller_service import (
    SellerServiceError,
    build_sam_deploy_command,
    fetch_activations,
    find_seller_service_dir,
    parse_product_registry,
    preflight,
    read_service_version,
    resolve_stack_output,
)

_PRODUCT = "prod-a5ee62vs2xa72"
_REGISTRY = json.dumps({_PRODUCT: {"productCode": "abc", "allowFreeTier": True}})


class _Sts:
    def __init__(self, account="145026617366", arn=None, error=None):
        self._account = account
        self._arn = arn or f"arn:aws:sts::{account}:assumed-role/Admin/x"
        self._error = error

    def get_caller_identity(self):
        if self._error:
            raise self._error
        return {"Account": self._account, "Arn": self._arn}


class _Catalog:
    def __init__(self, entities=None, error=None):
        self._entities = entities if entities is not None else []
        self._error = error

    def list_entities(self, **kwargs):
        assert kwargs["Catalog"] == "AWSMarketplace"
        assert kwargs["EntityType"] == "SaaSProduct"
        if self._error:
            raise self._error
        return {"EntitySummaryList": self._entities}


def _owned(entity_id=_PRODUCT, name="Auto Optimizer", visibility="Limited"):
    return {"EntityId": entity_id, "Name": name, "Visibility": visibility}


# ---------------------------------------------------------------------------
# parse_product_registry — catches the most likely operator mistake.
# ---------------------------------------------------------------------------


def test_parses_product_ids():
    assert parse_product_registry(_REGISTRY) == [_PRODUCT]


def test_rejects_product_code_used_as_product_id():
    """The product CODE and the entity ID are different values for one product.

    Only the entity id works as a `ResourceIdentifier` filter, so passing the
    code would deploy a service that silently matches nothing.
    """
    with pytest.raises(SellerServiceError, match="ENTITY ids"):
        parse_product_registry('{"q0k0s3zuuga46hle6fecx547": {}}')


def test_rejects_malformed_json():
    with pytest.raises(SellerServiceError, match="not valid JSON"):
        parse_product_registry("not json")


def test_rejects_empty_registry():
    with pytest.raises(SellerServiceError, match="non-empty"):
        parse_product_registry("{}")


def test_rejects_non_object_registry():
    with pytest.raises(SellerServiceError, match="non-empty"):
        parse_product_registry('["prod-abc"]')


# ---------------------------------------------------------------------------
# preflight — refusing is the point.
# ---------------------------------------------------------------------------


def test_passes_when_account_owns_the_product():
    result = preflight(
        product_ids=[_PRODUCT],
        sts_client=_Sts(),
        catalog_client=_Catalog([_owned()]),
    )
    assert result.account_id == "145026617366"
    assert result.ownership_verified is True
    assert [p.entity_id for p in result.owned] == [_PRODUCT]


def test_refuses_account_that_owns_no_saas_products():
    """The common mistake: running with buyer/dev credentials."""
    with pytest.raises(SellerServiceError, match="owns no AWS Marketplace SaaS"):
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(account="912625584728"),
            catalog_client=_Catalog([]),
        )


def test_refuses_seller_that_does_not_own_THIS_product():
    """A different seller account is still the wrong account.

    This is why the check is ownership-based rather than "is it a seller account"
    — an id comparison would pass here.
    """
    with pytest.raises(SellerServiceError) as exc:
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(),
            catalog_client=_Catalog([_owned(entity_id="prod-somethingelse")]),
        )
    message = str(exc.value)
    assert "NOT owned" in message
    # Lists what it DOES own, so the operator can see which account they're in.
    assert "prod-somethingelse" in message


def test_refuses_when_only_some_products_are_owned():
    with pytest.raises(SellerServiceError, match="prod-other"):
        preflight(
            product_ids=[_PRODUCT, "prod-other"],
            sts_client=_Sts(),
            catalog_client=_Catalog([_owned()]),
        )


def test_account_assertion_mismatch_refuses():
    with pytest.raises(SellerServiceError, match="Account mismatch"):
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(account="145026617366"),
            catalog_client=_Catalog([_owned()]),
            expected_account_id="111122223333",
        )


def test_account_assertion_match_passes():
    result = preflight(
        product_ids=[_PRODUCT],
        sts_client=_Sts(account="145026617366"),
        catalog_client=_Catalog([_owned()]),
        expected_account_id="145026617366",
    )
    assert result.account_id == "145026617366"


def test_missing_credentials_gives_an_actionable_error():
    with pytest.raises(SellerServiceError, match="Could not resolve AWS credentials"):
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(error=RuntimeError("Unable to locate credentials")),
            catalog_client=_Catalog([_owned()]),
        )


def test_access_denied_on_list_entities_refuses_and_names_the_escape_hatch():
    """AccessDenied almost always means "not a seller account" — the exact mistake
    being guarded against — so it must fail, not warn."""
    with pytest.raises(SellerServiceError) as exc:
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(),
            catalog_client=_Catalog(
                error=RuntimeError("AccessDeniedException: not authorized")
            ),
        )
    message = str(exc.value)
    assert "--skip-ownership-check" in message
    assert "NOT seller-account credentials" in message


def test_other_list_entities_error_is_surfaced():
    with pytest.raises(SellerServiceError, match="Could not list"):
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(),
            catalog_client=_Catalog(error=RuntimeError("Throttling")),
        )


def test_skip_ownership_check_bypasses_the_catalog_entirely():
    """The escape hatch must not call the API it exists to avoid."""

    class _Exploding:
        def list_entities(self, **kwargs):
            raise AssertionError("must not be called when the check is skipped")

    result = preflight(
        product_ids=[_PRODUCT],
        sts_client=_Sts(),
        catalog_client=_Exploding(),
        skip_ownership_check=True,
    )
    assert result.ownership_verified is False


def test_skip_ownership_check_still_honours_the_account_assertion():
    """Skipping ownership must not also skip an explicit account assertion."""
    with pytest.raises(SellerServiceError, match="Account mismatch"):
        preflight(
            product_ids=[_PRODUCT],
            sts_client=_Sts(account="145026617366"),
            catalog_client=_Catalog([]),
            expected_account_id="999988887777",
            skip_ownership_check=True,
        )


# ---------------------------------------------------------------------------
# sam deploy argv
# ---------------------------------------------------------------------------


def test_deploy_command_passes_registry_and_region(tmp_path):
    cmd = build_sam_deploy_command(
        service_dir=tmp_path,
        stack_name="idp-seller-entitlement",
        region="us-east-1",
        product_registry_json=_REGISTRY,
    )
    assert cmd[:2] == ["sam", "deploy"]
    assert "--capabilities" in cmd and "CAPABILITY_IAM" in cmd
    # Don't fail a no-op redeploy — re-running deploy must be safe.
    assert "--no-fail-on-empty-changeset" in cmd
    overrides = cmd[cmd.index("--parameter-overrides") + 1 :]
    assert f"ProductRegistryJson={_REGISTRY}" in overrides
    assert "MarketplaceAgreementRegion=us-east-1" in overrides
    # Omitted options must not appear as empty overrides.
    assert not any(o.startswith("AllowedAccounts=") for o in overrides)
    assert not any(o.startswith("TokenTtlSeconds=") for o in overrides)


def test_deploy_command_includes_optional_overrides(tmp_path):
    cmd = build_sam_deploy_command(
        service_dir=tmp_path,
        stack_name="s",
        region="us-east-1",
        product_registry_json=_REGISTRY,
        allowed_accounts="111122223333",
        token_ttl_seconds=900,
        guided=True,
    )
    overrides = cmd[cmd.index("--parameter-overrides") + 1 :]
    assert "AllowedAccounts=111122223333" in overrides
    assert "TokenTtlSeconds=900" in overrides
    assert "--guided" in cmd


# ---------------------------------------------------------------------------
# Repo asset discovery + version read
# ---------------------------------------------------------------------------


def test_finds_the_service_dir_in_a_repo_layout(tmp_path):
    service = tmp_path / "feature-platform" / "seller-entitlement-service"
    service.mkdir(parents=True)
    (service / "template.yaml").write_text("x", encoding="utf-8")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_seller_service_dir(nested) == service


def test_returns_none_when_not_in_a_repo(tmp_path):
    assert find_seller_service_dir(tmp_path) is None


def test_reads_the_service_version_from_the_template(tmp_path):
    (tmp_path / "template.yaml").write_text(
        "Mappings:\n  ServiceMeta:\n    ServiceVersion:\n      Value: '1.2.3'\n",
        encoding="utf-8",
    )
    assert read_service_version(tmp_path) == "1.2.3"


def test_missing_version_returns_none_rather_than_raising(tmp_path):
    (tmp_path / "template.yaml").write_text("Resources: {}\n", encoding="utf-8")
    assert read_service_version(tmp_path) is None


def test_real_template_carries_a_version():
    """Guards the `make version` sed target: if the shape drifts, this fails."""
    service_dir = find_seller_service_dir()
    if service_dir is None:  # pragma: no cover - only when run outside the repo
        pytest.skip("not running from a repo checkout")
    version = read_service_version(service_dir)
    assert version and version[0].isdigit(), (
        "seller-entitlement-service/template.yaml must carry a literal "
        "ServiceMeta.ServiceVersion.Value that `make version` can stamp"
    )


# ---------------------------------------------------------------------------
# Activation roster reads
# ---------------------------------------------------------------------------


def _item(
    buyer, product=_PRODUCT, outcome="granted", last="2026-08-19T10:00:00Z", **kw
):
    base = {
        "buyerAccountId": buyer,
        "productId": product,
        "lastOutcome": outcome,
        "attemptCount": 3,
        "grantedCount": 3 if outcome == "granted" else 0,
        "firstAttemptAt": "2026-08-01T10:00:00Z",
        "lastAttemptAt": last,
        "lastFreeTier": False,
        "lastDetail": "active agreement agmt-1",
        "lastServiceVersion": "0.6.5.dev1",
    }
    base.update(kw)
    return base


class _Ddb:
    def __init__(self, items, pages=None):
        self._items = items
        self._pages = pages
        self.queries = []
        self.scans = 0

    def Table(self, name):  # noqa: N802 - mirrors the boto3 resource API
        self.table_name = name
        return self

    def query(self, **kwargs):
        self.queries.append(kwargs)
        return {"Items": self._items}

    def scan(self, **kwargs):
        self.scans += 1
        if self._pages:
            page = self._pages.pop(0)
            return page
        return {"Items": self._items}


def test_product_filter_uses_the_gsi_not_a_scan():
    """A per-product roster read must not scan the whole table."""
    ddb = _Ddb([_item("111111111111")])
    records = fetch_activations(
        dynamodb_resource=ddb, table_name="t", product_id=_PRODUCT
    )
    assert len(records) == 1
    assert ddb.scans == 0
    (q,) = ddb.queries
    assert q["IndexName"] == "ProductIndex"
    # Newest first — a seller cares about recent activity.
    assert q["ScanIndexForward"] is False


def test_buyer_filter_uses_the_table_key():
    ddb = _Ddb([_item("111111111111")])
    fetch_activations(
        dynamodb_resource=ddb, table_name="t", buyer_account_id="111111111111"
    )
    (q,) = ddb.queries
    assert "IndexName" not in q
    assert q["ExpressionAttributeValues"] == {":bid": "111111111111"}


def test_unfiltered_read_scans_and_paginates():
    ddb = _Ddb(
        [],
        pages=[
            {"Items": [_item("111111111111")], "LastEvaluatedKey": {"k": 1}},
            {"Items": [_item("222222222222")]},
        ],
    )
    records = fetch_activations(dynamodb_resource=ddb, table_name="t")
    assert {r.buyer_account_id for r in records} == {"111111111111", "222222222222"}
    assert ddb.scans == 2, "must follow LastEvaluatedKey"


def test_results_are_sorted_newest_first():
    ddb = _Ddb(
        [
            _item("111111111111", last="2026-08-01T00:00:00Z"),
            _item("222222222222", last="2026-08-19T00:00:00Z"),
            _item("333333333333", last="2026-08-10T00:00:00Z"),
        ]
    )
    records = fetch_activations(dynamodb_resource=ddb, table_name="t")
    assert [r.buyer_account_id for r in records] == [
        "222222222222",
        "333333333333",
        "111111111111",
    ]


def test_outcome_and_since_filters():
    ddb = _Ddb(
        [
            _item("111111111111", outcome="granted", last="2026-08-19T00:00:00Z"),
            _item("222222222222", outcome="refused", last="2026-08-19T00:00:00Z"),
            _item("333333333333", outcome="refused", last="2026-07-01T00:00:00Z"),
        ]
    )
    refused = fetch_activations(
        dynamodb_resource=ddb, table_name="t", outcome="refused"
    )
    assert [r.buyer_account_id for r in refused] == ["222222222222", "333333333333"]

    recent = fetch_activations(
        dynamodb_resource=ddb, table_name="t", since="2026-08-01"
    )
    assert "333333333333" not in [r.buyer_account_id for r in recent]


def test_decimal_counters_survive_parsing():
    """DynamoDB returns numbers as Decimal; the record must still be int-like."""
    from decimal import Decimal

    ddb = _Ddb(
        [_item("111111111111", attemptCount=Decimal("7"), grantedCount=Decimal("5"))]
    )
    (record,) = fetch_activations(dynamodb_resource=ddb, table_name="t")
    assert record.attempt_count == 7
    assert record.granted_count == 5


def test_missing_or_odd_attributes_do_not_crash():
    ddb = _Ddb([{"buyerAccountId": "1", "productId": "prod-x"}])
    (record,) = fetch_activations(dynamodb_resource=ddb, table_name="t")
    assert record.attempt_count == 0
    assert record.last_outcome == ""


def test_read_failure_is_a_friendly_error():
    class _Broken:
        def Table(self, name):  # noqa: N802
            return self

        def scan(self, **kwargs):
            raise RuntimeError("ResourceNotFoundException")

    with pytest.raises(
        SellerServiceError, match="Could not read the activation roster"
    ):
        fetch_activations(dynamodb_resource=_Broken(), table_name="t")


# ---------------------------------------------------------------------------
# Stack output resolution
# ---------------------------------------------------------------------------


class _Cfn:
    def __init__(self, outputs=None, error=None):
        self._outputs = outputs or []
        self._error = error

    def describe_stacks(self, **kwargs):
        if self._error:
            raise self._error
        return {"Stacks": [{"Outputs": self._outputs}]}


def test_resolves_the_roster_table_from_stack_outputs():
    cfn = _Cfn([{"OutputKey": "ActivationsTableName", "OutputValue": "tbl-1"}])
    assert resolve_stack_output(cfn, "s", "ActivationsTableName") == "tbl-1"


def test_missing_output_suggests_a_redeploy():
    """An older deployment predates the roster — say so rather than 'not found'."""
    with pytest.raises(SellerServiceError, match="redeploy"):
        resolve_stack_output(_Cfn([]), "s", "ActivationsTableName")


def test_missing_stack_suggests_deploying():
    with pytest.raises(SellerServiceError, match="seller-service deploy"):
        resolve_stack_output(
            _Cfn(error=RuntimeError("Stack with id s does not exist")),
            "s",
            "ActivationsTableName",
        )
