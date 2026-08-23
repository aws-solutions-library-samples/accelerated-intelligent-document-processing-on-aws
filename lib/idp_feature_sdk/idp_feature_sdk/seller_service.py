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
import subprocess  # nosec B404 - fixed argv, no shell; see run_command()
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


def _sam_override(key: str, value: str) -> str:
    """One ``Key=Value`` element for ``sam deploy --parameter-overrides``.

    SAM does **not** take each argv element literally. It re-tokenizes the string
    with its own quote-aware parser, and an *unquoted* value is truncated at the
    first double quote. Passing raw JSON therefore delivered a product registry of
    exactly ``{`` — and nothing surfaced it: the endpoint deployed clean, every
    activation was refused as "unknown product", and the service deliberately makes
    "unknown product" byte-identical to "not entitled" so as not to leak an
    existence oracle. A fully non-functional deployment looked perfectly healthy,
    including to the live security test.

    Wrapping in single quotes makes SAM take the value verbatim (verified against
    SAM CLI 1.142.1 by reading back the parsed overrides). The equally-effective
    alternative — escaping every inner ``"`` — is not used because it corrupts any
    value that legitimately contains an escaped quote.
    """
    if "'" in value:
        raise SellerServiceError(
            f"{key} contains a single quote, which cannot be passed through "
            "`sam deploy --parameter-overrides` safely. Remove it.\n"
            f"  value: {value}"
        )
    return f"{key}='{value}'"


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
    # Compact the registry before quoting it. Two reasons: SAM's override parser
    # splits on whitespace, so a pretty-printed / multi-line JSON blob (an obvious
    # thing for an operator to paste when registering a second product) would be
    # shredded; and canonicalising here means the deployed value is byte-comparable
    # with what we read back after deploy.
    try:
        compact_registry = json.dumps(
            json.loads(product_registry_json), separators=(",", ":"), sort_keys=True
        )
    except ValueError as exc:
        raise SellerServiceError(
            f"--product-registry is not valid JSON: {exc}"
        ) from exc

    overrides = [_sam_override("ProductRegistryJson", compact_registry)]
    if allowed_accounts:
        overrides.append(_sam_override("AllowedAccounts", allowed_accounts))
    if token_ttl_seconds is not None:
        overrides.append(_sam_override("TokenTtlSeconds", str(token_ttl_seconds)))
    overrides.append(_sam_override("MarketplaceAgreementRegion", region))

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
        # nosec B603 - argv is a fixed list built by build_sam_deploy_command()
        # with no shell=True, so there is no shell to inject into; each operator
        # value (stack name, region, product registry) is a single argv element
        # and cannot split into extra arguments. Inputs are the operator's own
        # CLI flags, not remote input.
        subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)  # nosec B603
    except FileNotFoundError as exc:
        raise SellerServiceError(
            f"`{cmd[0]}` not found. The AWS SAM CLI is required to deploy the "
            "seller service (same prerequisite as `idp-feature-cli publish`)."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise SellerServiceError(
            f"`{' '.join(cmd[:2])}` failed with exit code {exc.returncode}."
        ) from exc


# ---------------------------------------------------------------------------
# Activation endpoint pointer (indirection)
# ---------------------------------------------------------------------------
#
# The activation endpoint URL contains the API Gateway REST API id, which AWS
# assigns. If the API is ever replaced — a stack rebuild, a region move, an
# account migration — the id changes, and every already-installed copy of the
# extension is running in a CUSTOMER's account where the seller cannot reach it to
# update a baked-in URL. That makes the URL permanent in the worst way.
#
# So the endpoint is published as a small pointer object next to `latest.json`,
# and the extension reads it at activation time rather than trusting only what was
# compiled in. Same pattern, and the same regional layout, as `latest.json`.
#
# SECURITY: the pointer carries NO key material, deliberately.
# --------------------------------------------------------
# The public verification key stays embedded in the published extension. If the
# pointer file also carried the key, then whoever can write to the artifact bucket
# could substitute both a hostile endpoint AND the key that validates its tokens —
# making the bucket a forgery trust root and the entitlement gate worthless.
#
# With key material excluded, a tampered pointer can only redirect the request to
# somewhere that cannot produce a signature verifying against the embedded key. The
# activation then fails closed, and the buyer-side grace period on the
# last-known-good token absorbs it. Worst case is denial of service, not free access.
#
# `signingKeyId` is therefore a HINT ONLY — for choosing among keys the extension
# already embeds during a rotation. It must never be treated as a key, or fetched
# from.

ACTIVATION_POINTER_FILENAME = "activation.json"
ACTIVATION_POINTER_SCHEMA_VERSION = "1.0"


def utc_now_iso() -> str:
    """Same format publisher._now_iso() writes into latest.json, so the two
    pointer objects are comparable by eye."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def activation_pointer_key(feature_id: str, s3_prefix: str = "") -> str:
    """S3 key for a feature's activation pointer, mirroring `latest.json`'s layout.

    Version-free and at the extension base, because it is a pointer: readers must
    resolve the *current* endpoint, not one pinned to a release.
    """
    base = f"extensions/{feature_id}/{ACTIVATION_POINTER_FILENAME}"
    prefix = s3_prefix.strip("/")
    return f"{prefix}/{base}" if prefix else base


def build_activation_pointer(
    *,
    activation_endpoint: str,
    signing_key_id: str = "",
    service_version: str = "",
    published_at: str = "",
) -> dict:
    """The pointer document. Pure, so its shape is a tested contract."""
    if not activation_endpoint.startswith("https://"):
        raise SellerServiceError(
            "activation endpoint must be an https URL, got: "
            f"{activation_endpoint!r}. Extensions send SigV4-signed credentials "
            "to it; plaintext http would expose them."
        )
    document = {
        "schemaVersion": ACTIVATION_POINTER_SCHEMA_VERSION,
        "activationEndpoint": activation_endpoint,
    }
    # Hint only — never key material. See the note above.
    if signing_key_id:
        document["signingKeyId"] = signing_key_id
    if service_version:
        document["serviceVersion"] = service_version
    if published_at:
        document["publishedAt"] = published_at
    return document


def publish_activation_pointer(
    *,
    s3_client: Any,
    bucket: str,
    feature_ids: list[str],
    document: dict,
    s3_prefix: str = "",
    make_public: bool = True,
) -> list[str]:
    """Write the pointer for each feature into one regional bucket.

    Returns the keys written. Public-read by default: the reader is an extension
    running in an arbitrary buyer account, exactly like the template and
    `latest.json` it sits beside.
    """
    if "publicKey" in document or "publicKeyPem" in document or "key" in document:
        raise SellerServiceError(
            "refusing to publish key material in the activation pointer — that "
            "would make the artifact bucket a forgery trust root. The public "
            "verification key belongs embedded in the extension."
        )
    body = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
    extra: dict[str, Any] = {"ContentType": "application/json"}
    if make_public:
        extra["ACL"] = "public-read"

    written = []
    for feature_id in feature_ids:
        key = activation_pointer_key(feature_id, s3_prefix)
        try:
            s3_client.put_object(Bucket=bucket, Key=key, Body=body, **extra)
        except Exception as exc:  # noqa: BLE001
            raise SellerServiceError(
                f"could not write s3://{bucket}/{key}: {exc}"
            ) from exc
        written.append(key)
    return written


# ---------------------------------------------------------------------------
# Build-time trust material
# ---------------------------------------------------------------------------
#
# What an extension author needs, once per release, to bake into the bundle:
#
#   * the public verification key — EMBEDDED, so tokens are verified against
#     something an attacker cannot swap;
#   * `kid` — the value the service actually puts in the token's `kid` claim, so
#     the verifier can map a claim to one of the keys it embeds during a rotation;
#   * the activation endpoint — as the compiled-in fallback for when the runtime
#     pointer (`activation.json`) is unreachable.
#
# Note the deliberate asymmetry with the pointer file: the public key belongs
# HERE, in build-time material that ends up inside the published bundle, and NOT
# in the runtime pointer. Runtime-fetched key material would make the artifact
# bucket a forgery trust root; build-time embedded key material is exactly what
# stops it being one.

TRUST_BUNDLE_SCHEMA_VERSION = "1.0"
# nosec B105 - "RSASSA_PSS_SHA_256" is a KMS signing-algorithm NAME, not a
# credential. Bandit's hardcoded-password heuristic fires only because the
# identifier contains the substring "TOKEN". Nothing secret is expressible here:
# the value is echoed to buyers in every activation response, and the signing key
# never leaves KMS.
TOKEN_SIGNING_ALGORITHM = "RSASSA_PSS_SHA_256"  # nosec B105


def der_to_pem(der: bytes, label: str = "PUBLIC KEY") -> str:
    """Wrap DER bytes as PEM. KMS returns SubjectPublicKeyInfo DER; most verifier
    libraries want PEM, and doing the conversion here keeps a base64/openssl
    incantation out of every extension's release process."""
    import base64 as _b64
    import textwrap

    body = _b64.b64encode(der).decode("ascii")
    lines = textwrap.wrap(body, 64)
    return (
        f"-----BEGIN {label}-----\n" + "\n".join(lines) + f"\n-----END {label}-----\n"
    )


def fetch_signing_public_key(kms_client: Any, key_arn: str) -> bytes:
    """The SubjectPublicKeyInfo DER for the token signing key.

    Verifies the key is actually a signing key: exporting an ENCRYPT_DECRYPT key's
    public half here would produce a bundle that silently fails every
    verification.
    """
    try:
        resp = kms_client.get_public_key(KeyId=key_arn)
    except Exception as exc:  # noqa: BLE001
        raise SellerServiceError(
            f"Could not fetch the public key for {key_arn}: {exc}\n"
            "The caller needs kms:GetPublicKey on the signing key. Note the "
            "activation Lambda deliberately does NOT hold that permission — run "
            "this with your own seller credentials."
        ) from exc

    usage = str(resp.get("KeyUsage", ""))
    if usage != "SIGN_VERIFY":
        raise SellerServiceError(
            f"{key_arn} has KeyUsage {usage!r}, not SIGN_VERIFY. That is not the "
            "token signing key — a bundle built from it would fail every "
            "verification."
        )
    der = resp.get("PublicKey")
    if not der:
        raise SellerServiceError(f"KMS returned no public key for {key_arn}")
    return bytes(der)


def build_trust_bundle(
    *,
    activation_endpoint: str,
    kid: str,
    public_key_der: bytes,
    service_version: str = "",
    exported_at: str = "",
) -> dict:
    """Build-time trust material for one seller service. Pure, so its shape is a
    tested contract that the extension build can rely on."""
    if not kid:
        raise SellerServiceError(
            "kid is empty. It must be the signing key ARN, byte-identical to the "
            "`kid` claim the service puts in its tokens, or a verifier cannot map "
            "a token to the key that signed it."
        )
    return {
        "schemaVersion": TRUST_BUNDLE_SCHEMA_VERSION,
        "activationEndpoint": activation_endpoint,
        # Byte-identical to the token's `kid` claim (the Lambda uses the key ARN).
        "kid": kid,
        "signingAlgorithm": TOKEN_SIGNING_ALGORITHM,
        "publicKeyPem": der_to_pem(public_key_der),
        "serviceVersion": service_version,
        "exportedAt": exported_at,
    }


def verify_deployed_registry(
    *,
    cfn_client: Any,
    lambda_client: Any,
    stack_name: str,
    expected_product_ids: list[str],
) -> dict:
    """Read the registry back off the DEPLOYED function and confirm it survived.

    This exists because a mangled registry is invisible from the outside. The
    endpoint deploys clean, answers every request, and refuses every activation
    with the same body it uses for a genuine non-subscriber — so neither a smoke
    test nor the live security test can tell a correctly-configured service from
    one serving no products at all. The only reliable check is to look at what the
    function actually received. Raises rather than warns: shipping an endpoint that
    refuses all paying customers is worse than a failed deploy.
    """
    try:
        detail = cfn_client.describe_stack_resource(
            StackName=stack_name, LogicalResourceId="ActivateFunction"
        )
        function_name = detail["StackResourceDetail"]["PhysicalResourceId"]
        config = lambda_client.get_function_configuration(FunctionName=function_name)
    except Exception as exc:  # noqa: BLE001
        raise SellerServiceError(
            "Deployed, but could not read the activation function back to verify "
            f"the product registry: {exc}\n"
            "Check it by hand before announcing the endpoint: the registry must "
            "list every product you expect to serve."
        ) from exc

    raw = (
        (config.get("Environment") or {})
        .get("Variables", {})
        .get("PRODUCT_REGISTRY_JSON", "")
    )
    try:
        deployed = json.loads(raw)
        if not isinstance(deployed, dict):
            raise ValueError(f"expected a JSON object, got {type(deployed).__name__}")
    except ValueError as exc:
        raise SellerServiceError(
            f"The product registry did not survive deployment: {exc}\n"
            f"  the function received: {raw!r}\n"
            "Every activation will be refused as an unknown product — which is "
            "indistinguishable from 'not subscribed' from the caller's side, so "
            "this would not show up in testing. Do not announce this endpoint."
        ) from exc

    missing = [p for p in expected_product_ids if p not in deployed]
    if missing:
        raise SellerServiceError(
            f"These products are not in the DEPLOYED registry: {', '.join(missing)}\n"
            f"  the function received: {raw}\n"
            "Activation for them will be refused as an unknown product."
        )
    return deployed


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


# ---------------------------------------------------------------------------
# Activation roster reads
# ---------------------------------------------------------------------------


@dataclass
class ActivationRecord:
    buyer_account_id: str
    product_id: str
    last_outcome: str
    attempt_count: int
    granted_count: int
    first_attempt_at: str
    last_attempt_at: str
    free_tier: bool = False
    detail: str = ""
    service_version: str = ""

    @classmethod
    def from_item(cls, item: dict) -> "ActivationRecord":
        def _int(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        return cls(
            buyer_account_id=str(item.get("buyerAccountId", "")),
            product_id=str(item.get("productId", "")),
            last_outcome=str(item.get("lastOutcome", "")),
            attempt_count=_int(item.get("attemptCount")),
            granted_count=_int(item.get("grantedCount")),
            first_attempt_at=str(item.get("firstAttemptAt", "")),
            last_attempt_at=str(item.get("lastAttemptAt", "")),
            free_tier=bool(item.get("lastFreeTier", False)),
            detail=str(item.get("lastDetail", "")),
            service_version=str(item.get("lastServiceVersion", "")),
        )


def resolve_stack_output(cfn_client: Any, stack_name: str, key: str) -> str:
    """Read one Output off the deployed seller-service stack.

    Lets the CLI find the roster table without the operator passing its name.
    """
    try:
        resp = cfn_client.describe_stacks(StackName=stack_name)
    except Exception as exc:  # noqa: BLE001
        raise SellerServiceError(
            f"Could not describe stack '{stack_name}': {exc}\n"
            "Is the Seller Entitlement Service deployed in this account/region? "
            "Deploy it with `idp-feature-cli seller-service deploy`."
        ) from exc
    for stack in resp.get("Stacks") or []:
        for output in stack.get("Outputs") or []:
            if output.get("OutputKey") == key:
                return str(output.get("OutputValue", ""))
    raise SellerServiceError(
        f"Stack '{stack_name}' has no output named '{key}'. It may predate the "
        "activation roster — redeploy with a current template."
    )


def fetch_activations(
    *,
    dynamodb_resource: Any,
    table_name: str,
    product_id: Optional[str] = None,
    buyer_account_id: Optional[str] = None,
    outcome: Optional[str] = None,
    since: Optional[str] = None,
) -> list[ActivationRecord]:
    """Read the activation roster, newest attempt first.

    Uses the ProductIndex GSI when filtering by product, and the table's own key
    when filtering by buyer — a Scan only when neither is given, which is the
    "show me everything" case and is fine for a roster of customers.
    """
    table = dynamodb_resource.Table(table_name)
    try:
        if product_id:
            resp = table.query(
                IndexName="ProductIndex",
                KeyConditionExpression="productId = :pid",
                ExpressionAttributeValues={":pid": product_id},
                ScanIndexForward=False,
            )
            items = resp.get("Items", [])
        elif buyer_account_id:
            resp = table.query(
                KeyConditionExpression="buyerAccountId = :bid",
                ExpressionAttributeValues={":bid": buyer_account_id},
            )
            items = resp.get("Items", [])
        else:
            items = []
            kwargs: dict = {}
            while True:
                resp = table.scan(**kwargs)
                items.extend(resp.get("Items", []))
                if "LastEvaluatedKey" not in resp:
                    break
                kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    except Exception as exc:  # noqa: BLE001
        raise SellerServiceError(
            f"Could not read the activation roster: {exc}"
        ) from exc

    records = [ActivationRecord.from_item(i) for i in items]
    if outcome:
        records = [r for r in records if r.last_outcome == outcome]
    if since:
        records = [r for r in records if r.last_attempt_at >= since]
    records.sort(key=lambda r: r.last_attempt_at, reverse=True)
    return records
