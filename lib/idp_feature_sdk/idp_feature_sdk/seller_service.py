# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deploy the Seller Entitlement Service into an AWS Marketplace seller account.

The service is the only place a paid extension's entitlement can actually be
checked: ``SearchAgreements`` with ``PartyType=Proposer`` answers only for the
account that **owns** the product. See
``feature-platform/seller-entitlement-service/README.md``.

Why there is a preflight
------------------------
Deployed into the wrong account, the service still comes up looking healthy —
``SearchAgreements`` returns an *empty list* rather than an error — so every
activation is refused, every customer is locked out, and nothing in the logs says
why. The failure is silent, remote, and hits paying customers, which is worth
spending a preflight on.

The check verifies **ownership**, not merely "some seller account": comparing an
account id would pass for any seller, including one that does not sell this
product.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# AWS Marketplace lives in us-east-1; its catalog/agreement APIs are not
# available in every region.
DEFAULT_MARKETPLACE_REGION = "us-east-1"


class SellerServiceError(Exception):
    """Raised for any preflight or deploy failure. Message is user-facing."""


@dataclass
class OwnedProduct:
    entity_id: str
    name: str
    visibility: str


@dataclass
class PreflightResult:
    account_id: str
    caller_arn: str
    product_ids: list[str]
    owned: list[OwnedProduct] = field(default_factory=list)
    ownership_verified: bool = True


def find_seller_service_dir(start: Optional[Path] = None) -> Optional[Path]:
    """Locate ``feature-platform/seller-entitlement-service/`` in a repo checkout.

    Mirrors ``scaffold.find_feature_template``: walk up from the working
    directory so the CLI works from any subdirectory. The template and Lambda
    source live in the repository rather than inside the installed package, so a
    checkout is required — same constraint as ``idp-feature-cli init``.
    """
    relative_candidates = (
        Path("feature-platform") / "seller-entitlement-service",
        Path("subscription-features")
        / "feature-platform"
        / "seller-entitlement-service",
    )
    root = (start or Path.cwd()).resolve()
    for candidate_root in (root, *root.parents):
        for relative in relative_candidates:
            candidate = candidate_root / relative
            if (candidate / "template.yaml").is_file():
                return candidate
    return None


def parse_product_registry(registry_json: str) -> list[str]:
    """Extract product ids from a PRODUCT_REGISTRY_JSON string.

    Validates the `prod-` prefix, because the most likely mistake is passing the
    product *code* instead of the entity id — they are different values for the
    same product, and only the entity id works as a `ResourceIdentifier` filter.
    """
    try:
        registry = json.loads(registry_json)
    except ValueError as exc:
        raise SellerServiceError(
            f"--product-registry is not valid JSON: {exc}"
        ) from exc
    if not isinstance(registry, dict) or not registry:
        raise SellerServiceError(
            "--product-registry must be a non-empty JSON object keyed by productId, "
            'e.g. \'{"prod-abc123":{"productCode":"xyz","allowFreeTier":true}}\''
        )

    product_ids = [str(k) for k in registry]
    bad = [p for p in product_ids if not p.startswith("prod-")]
    if bad:
        raise SellerServiceError(
            f"These do not look like SaaS product ENTITY ids: {', '.join(bad)}.\n"
            "They must start with 'prod-'. NOTE this is not the product code — "
            "SearchAgreements matches on the entity id. Find it with:\n"
            "  aws marketplace-discovery get-listing --listing-id prodview-XXXX "
            "--region us-east-1 \\\n"
            "    --query 'associatedEntities[0].product.productId' --output text"
        )
    return product_ids


def _list_owned_saas_products(catalog_client: Any) -> list[OwnedProduct]:
    try:
        resp = catalog_client.list_entities(
            Catalog="AWSMarketplace", EntityType="SaaSProduct"
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a friendly error below
        message = str(exc)
        if "AccessDenied" in message or "not authorized" in message:
            raise SellerServiceError(
                "This account cannot list AWS Marketplace SaaS products "
                "(aws-marketplace:ListEntities denied), so product ownership "
                "cannot be verified.\n\n"
                "That usually means these are NOT seller-account credentials — "
                "the mistake this preflight exists to catch. If you are certain "
                "the account is right and the role merely lacks ListEntities, "
                "re-run with --skip-ownership-check.\n"
                f"Details: {message}"
            ) from exc
        raise SellerServiceError(
            f"Could not list AWS Marketplace SaaS products: {message}"
        ) from exc

    return [
        OwnedProduct(
            entity_id=str(e.get("EntityId", "")),
            name=str(e.get("Name", "")),
            visibility=str(e.get("Visibility", "")),
        )
        for e in resp.get("EntitySummaryList") or []
    ]


def preflight(
    *,
    product_ids: list[str],
    sts_client: Any,
    catalog_client: Any,
    expected_account_id: Optional[str] = None,
    skip_ownership_check: bool = False,
) -> PreflightResult:
    """Confirm the caller is the seller that owns every registered product.

    Raises ``SellerServiceError`` with an actionable message on any failure.
    """
    try:
        identity = sts_client.get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        raise SellerServiceError(
            "Could not resolve AWS credentials. Configure credentials for your "
            f"AWS Marketplace SELLER account before deploying.\nDetails: {exc}"
        ) from exc

    account_id = str(identity.get("Account", ""))
    caller_arn = str(identity.get("Arn", ""))

    if expected_account_id and expected_account_id != account_id:
        raise SellerServiceError(
            f"Account mismatch: credentials are for {account_id}, but "
            f"--seller-account-id {expected_account_id} was requested.\n"
            "Switch credentials, or correct --seller-account-id."
        )

    result = PreflightResult(
        account_id=account_id, caller_arn=caller_arn, product_ids=product_ids
    )

    if skip_ownership_check:
        result.ownership_verified = False
        return result

    owned = _list_owned_saas_products(catalog_client)
    result.owned = owned

    if not owned:
        raise SellerServiceError(
            f"Account {account_id} owns no AWS Marketplace SaaS products.\n"
            "These are almost certainly not seller-account credentials. The "
            "Seller Entitlement Service must be deployed in the account that "
            "OWNS the listing — deployed anywhere else it refuses every "
            "activation, silently."
        )

    owned_ids = {p.entity_id for p in owned}
    missing = [p for p in product_ids if p not in owned_ids]
    if missing:
        inventory = "\n".join(
            f"    {p.entity_id}  {p.name} ({p.visibility})" for p in owned
        )
        raise SellerServiceError(
            f"Products NOT owned by {account_id}: {', '.join(missing)}\n\n"
            f"  SaaS products this account does own:\n{inventory}\n\n"
            "Refusing to deploy. SearchAgreements(PartyType=Proposer) only "
            "answers for the product's OWNER, so a service deployed here would "
            "refuse every activation for the unowned product(s) — returning an "
            "empty result rather than an error, and therefore failing silently."
        )

    return result


def build_sam_deploy_command(
    *,
    service_dir: Path,
    stack_name: str,
    region: str,
    product_registry_json: str,
    allowed_accounts: str = "",
    token_ttl_seconds: Optional[int] = None,
    guided: bool = False,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    """The `sam deploy` argv. Separated out so tests can assert it without AWS."""
    overrides = [f"ProductRegistryJson={product_registry_json}"]
    if allowed_accounts:
        overrides.append(f"AllowedAccounts={allowed_accounts}")
    if token_ttl_seconds is not None:
        overrides.append(f"TokenTtlSeconds={token_ttl_seconds}")
    overrides.append(f"MarketplaceAgreementRegion={region}")

    cmd = [
        "sam",
        "deploy",
        "--template-file",
        str(service_dir / "template.yaml"),
        "--stack-name",
        stack_name,
        "--region",
        region,
        "--capabilities",
        "CAPABILITY_IAM",
        # The service has no user-facing bucket of its own; let SAM manage one.
        "--resolve-s3",
        "--no-fail-on-empty-changeset",
        "--parameter-overrides",
        *overrides,
    ]
    if guided:
        cmd.append("--guided")
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def run_command(cmd: list[str], cwd: Optional[Path] = None) -> None:
    """Run a subprocess, raising SellerServiceError on failure."""
    try:
        subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)
    except FileNotFoundError as exc:
        raise SellerServiceError(
            f"`{cmd[0]}` not found. The AWS SAM CLI is required to deploy the "
            "seller service (same prerequisite as `idp-feature-cli publish`)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SellerServiceError(
            f"`{' '.join(cmd[:2])}` failed with exit code {exc.returncode}."
        ) from exc


def read_service_version(service_dir: Path) -> Optional[str]:
    """Read the ServiceVersion mapping value out of the template, if present.

    Deliberately a small text scan rather than a YAML parse: this runs before
    deploy purely to echo the version, and pulling in a YAML dependency (or
    tolerating CloudFormation's custom `!Ref`-style tags) for a cosmetic line
    would be a poor trade. Returns None if the shape isn't found.
    """
    try:
        lines = (service_dir / "template.yaml").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for i, line in enumerate(lines):
        if line.strip().startswith("ServiceVersion:"):
            for follow in lines[i + 1 : i + 4]:
                stripped = follow.strip()
                if stripped.startswith("Value:"):
                    return stripped.split("Value:", 1)[1].strip().strip("'\"")
    return None
