# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""S3 access-point policies that confine each tenant's external principals.

A prefix mapping is not a boundary on ingest: whoever holds ``s3:PutObject`` on the
Input bucket chooses the prefix, and with it the tenant. What bounds that is IAM on
the writer. This module turns the stack's tenant declarations into one S3 access
point per tenant and direction:

* **Input** — the tenant's *uploaders* may ``s3:PutObject`` only under the tenant's
  prefixes. Nothing else: an uploader cannot read, list or delete.
* **Output** — the tenant's *consumers* may ``s3:GetObject`` under the tenant's
  prefixes and ``s3:ListBucket`` with an ``s3:prefix`` condition to match.

Why the output subtree is the input subtree: every artefact the pipeline writes to
the Output bucket is keyed ``<input key>/...`` (OCR pages, sections, summaries,
evaluation reports — ``f"{document.input_key}/..."`` throughout ``idp_common``),
so a document under ``clients/acme/`` has every output under ``clients/acme/`` too.
Should a writer ever key outputs another way, this module's output policy would
stop matching it, which is the safe direction.

Access points only take effect when the bucket policy delegates to them, which the
stack's bucket policies do under ``TenantIsolationMode=Strict``. And they only
*confine* a principal that holds no other route to the bucket: a principal also
granted ``s3:PutObject`` on the bucket itself by its own identity policy still
writes anywhere. The generated template says so in its description.

Pure functions only, so the template is deterministic and the same builder answers
the audit's drift question with concrete ARNs.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

DIRECTION_IN = "in"
DIRECTION_OUT = "out"

# S3 access point names: 3-50 characters, lowercase letters, digits and hyphens,
# starting with a letter or digit and not ending with a hyphen. Unique per account
# and region, hence the stack hash.
_AP_MAX = 50
_AP_FIXED = len("idp") + 6 + len("-") + len("-out")
_AP_TENANT_MAX = _AP_MAX - _AP_FIXED

_PRINCIPAL_RE = re.compile(
    r"^arn:aws[a-z-]*:iam::\d{12}:(?:root|role/[\w+=,.@/-]+|user/[\w+=,.@/-]+)$"
)
_KMS_RE = re.compile(r"^arn:aws[a-z-]*:kms:[a-z0-9-]+:\d{12}:key/[A-Za-z0-9-]+$")


class PrincipalsError(ValueError):
    """The principals file is malformed or names a tenant the stack does not declare."""


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:6]


def access_point_name(stack_name: str, tenant_id: str, direction: str) -> str:
    """Deterministic access point name for one tenant and direction.

    ``idp<hash(stack)>-<tenant>-<in|out>``. A tenant ID too long to fit is cut and
    suffixed with its own hash, so two long IDs sharing a head still get distinct
    names.
    """
    if direction not in (DIRECTION_IN, DIRECTION_OUT):
        raise ValueError(f"direction must be {DIRECTION_IN!r} or {DIRECTION_OUT!r}")
    part = tenant_id
    if len(part) > _AP_TENANT_MAX:
        head = part[: _AP_TENANT_MAX - 7].rstrip("-")
        part = f"{head}-{_short_hash(tenant_id)}"
    return f"idp{_short_hash(stack_name)}-{part}-{direction}"


def logical_id(tenant_id: str, direction: str) -> str:
    """A CloudFormation logical ID for one access point (alphanumerics only)."""
    camel = "".join(seg.capitalize() for seg in tenant_id.split("-") if seg)
    return f"Tenant{camel}{_short_hash(tenant_id)}{direction.capitalize()}AccessPoint"


def load_principals(
    raw: Any, declared_tenant_ids: Sequence[str]
) -> Dict[str, Dict[str, Any]]:
    """Validate a principals mapping: ``tenantId -> {uploaders, consumers, kmsKeyArn}``.

    Refuses rather than skips: a tenant ID the stack does not declare is almost
    always a typo, and silently generating nothing for it would read as configured.
    """
    if not isinstance(raw, Mapping):
        raise PrincipalsError(
            "The principals file must be a mapping of tenant ID to "
            "{uploaders: [...], consumers: [...], kmsKeyArn: ...}."
        )
    declared = set(declared_tenant_ids)
    result: Dict[str, Dict[str, Any]] = {}
    for tenant_id, entry in raw.items():
        tenant_id = str(tenant_id)
        if tenant_id not in declared:
            raise PrincipalsError(
                f"Tenant {tenant_id!r} is not declared on this stack. Declared: "
                f"{', '.join(sorted(declared)) or 'none'}."
            )
        if not isinstance(entry, Mapping):
            raise PrincipalsError(f"Tenant {tenant_id!r}: expected a mapping.")
        unknown = set(entry) - {"uploaders", "consumers", "kmsKeyArn"}
        if unknown:
            raise PrincipalsError(
                f"Tenant {tenant_id!r}: unknown key(s) {', '.join(sorted(unknown))}."
            )
        clean: Dict[str, Any] = {"uploaders": [], "consumers": [], "kmsKeyArn": None}
        for role in ("uploaders", "consumers"):
            values = entry.get(role) or []
            if isinstance(values, str):
                values = [values]
            for arn in values:
                if not _PRINCIPAL_RE.match(str(arn)):
                    raise PrincipalsError(
                        f"Tenant {tenant_id!r}: {role} entry {arn!r} is not an IAM "
                        f"role, user or account-root ARN."
                    )
            clean[role] = sorted({str(v) for v in values})
        kms = entry.get("kmsKeyArn")
        if kms is not None:
            if not _KMS_RE.match(str(kms)):
                raise PrincipalsError(
                    f"Tenant {tenant_id!r}: kmsKeyArn {kms!r} is not a KMS key ARN."
                )
            clean["kmsKeyArn"] = str(kms)
        result[tenant_id] = clean
    return result


def _prefix_pattern(prefix: str) -> str:
    # Tenant prefixes are validated to end in "/", so "<prefix>*" cannot reach a
    # sibling folder sharing characters (acme/ vs acmecorp/).
    return f"{prefix}*"


def access_point_policy(
    name: str,
    prefixes: Sequence[str],
    principals: Sequence[str],
    direction: str,
    arn: Callable[[str], Any],
    kms_key_arn: Optional[str] = None,
) -> Dict[str, Any]:
    """The policy document for one access point.

    ``arn(suffix)`` renders ``arn:<partition>:s3:<region>:<account>:accesspoint/
    <name><suffix>`` — a ``Fn::Sub`` for the template, a literal for the audit.
    """

    def principal() -> Dict[str, Any]:
        # A fresh object per statement: a shared one is emitted as a YAML anchor and
        # alias, which CloudFormation accepts but a reviewer reads as a puzzle.
        return {"AWS": list(principals) if len(principals) != 1 else principals[0]}

    objects = [arn(f"/object/{_prefix_pattern(p)}") for p in prefixes]
    statements: List[Dict[str, Any]] = []
    if direction == DIRECTION_IN:
        statements.append(
            {
                "Sid": "TenantUploadersWriteOwnPrefixes",
                "Effect": "Allow",
                "Principal": principal(),
                "Action": "s3:PutObject",
                "Resource": objects,
            }
        )
        if kms_key_arn:
            statements.append(
                {
                    "Sid": "RequireTenantKey",
                    "Effect": "Deny",
                    "Principal": principal(),
                    "Action": "s3:PutObject",
                    "Resource": objects,
                    "Condition": {
                        "StringNotEquals": {
                            "s3:x-amz-server-side-encryption-aws-kms-key-id": kms_key_arn
                        }
                    },
                }
            )
    else:
        statements.append(
            {
                "Sid": "TenantConsumersReadOwnPrefixes",
                "Effect": "Allow",
                "Principal": principal(),
                "Action": "s3:GetObject",
                "Resource": objects,
            }
        )
        statements.append(
            {
                "Sid": "TenantConsumersListOwnPrefixes",
                "Effect": "Allow",
                "Principal": principal(),
                "Action": "s3:ListBucket",
                "Resource": arn(""),
                "Condition": {
                    "StringLike": {"s3:prefix": [_prefix_pattern(p) for p in prefixes]}
                },
            }
        )
    return {"Version": "2012-10-17", "Statement": statements}


def _sub_arn(name: str) -> Callable[[str], Any]:
    def render(suffix: str) -> Any:
        return {
            "Fn::Sub": (
                "arn:${AWS::Partition}:s3:${AWS::Region}:${AWS::AccountId}:"
                f"accesspoint/{name}{suffix}"
            )
        }

    return render


def literal_arn(name: str, partition: str, region: str, account: str):
    def render(suffix: str) -> str:
        return f"arn:{partition}:s3:{region}:{account}:accesspoint/{name}{suffix}"

    return render


def planned_access_points(
    stack_name: str,
    tenants: Sequence[Mapping[str, Any]],
    principals: Mapping[str, Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Every access point the principals file calls for, plus warnings.

    Each entry: ``{tenantId, direction, name, prefixes, principals, kmsKeyArn}``.
    Ordered by tenant then direction, so the template is byte-stable.
    """
    planned: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for tenant in sorted(tenants, key=lambda t: t["tenantId"]):
        tid = tenant["tenantId"]
        entry = principals.get(tid)
        if entry is None:
            warnings.append(
                f"Tenant {tid!r} has no entry in the principals file; no access "
                f"point is generated for it."
            )
            continue
        for direction, role in (
            (DIRECTION_IN, "uploaders"),
            (DIRECTION_OUT, "consumers"),
        ):
            members = list(entry.get(role) or [])
            if not members:
                warnings.append(
                    f"Tenant {tid!r} lists no {role}; no {direction} access point."
                )
                continue
            planned.append(
                {
                    "tenantId": tid,
                    "direction": direction,
                    "name": access_point_name(stack_name, tid, direction),
                    "prefixes": sorted(tenant["prefixes"]),
                    "principals": members,
                    "kmsKeyArn": entry.get("kmsKeyArn")
                    if direction == DIRECTION_IN
                    else None,
                }
            )
    return planned, warnings


def build_template(
    stack_name: str,
    input_bucket: str,
    output_bucket: str,
    tenants: Sequence[Mapping[str, Any]],
    principals: Mapping[str, Mapping[str, Any]],
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """The CloudFormation template, the access point names, and warnings."""
    planned, warnings = planned_access_points(stack_name, tenants, principals)
    resources: Dict[str, Any] = {}
    outputs: Dict[str, Any] = {}
    for ap in planned:
        lid = logical_id(ap["tenantId"], ap["direction"])
        resources[lid] = {
            "Type": "AWS::S3::AccessPoint",
            "Properties": {
                "Bucket": input_bucket
                if ap["direction"] == DIRECTION_IN
                else output_bucket,
                "Name": ap["name"],
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True,
                    "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True,
                    "RestrictPublicBuckets": True,
                },
                "Policy": access_point_policy(
                    ap["name"],
                    ap["prefixes"],
                    ap["principals"],
                    ap["direction"],
                    _sub_arn(ap["name"]),
                    ap["kmsKeyArn"],
                ),
            },
        }
        outputs[f"{lid}Arn"] = {
            "Description": (
                f"Tenant {ap['tenantId']} {'upload' if ap['direction'] == DIRECTION_IN else 'read'} "
                f"access point"
            ),
            "Value": {"Fn::GetAtt": [lid, "Arn"]},
        }
    template: Dict[str, Any] = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": (
            f"Per-tenant S3 access points for IDP stack {stack_name}, generated by "
            f"'idp-cli tenancy generate-policies'. They take effect only when the "
            f"stack runs with TenantIsolationMode=Strict (its bucket policies then "
            f"delegate to access points in this account), and they confine only "
            f"principals with no other S3 grant on these buckets."
        ),
        "Resources": resources,
    }
    if outputs:
        template["Outputs"] = outputs
    return template, [ap["name"] for ap in planned], warnings


def uncovered_tenant_prefixes(
    mappings: Sequence[Mapping[str, Any]], tenants: Sequence[Mapping[str, Any]]
) -> List[Tuple[str, str, Optional[str]]]:
    """Tenant prefixes whose top level no mapping inside the tenant governs.

    Returns ``(tenantId, prefix, governing_mapping_prefix_or_None)``. The probe is a
    key directly under the prefix: if the mapping that wins for it sits outside the
    tenant (an ancestor such as ``clients/``) or there is none, documents dropped
    there process under a profile the tenant does not own — the ingest check refuses
    them in strict mode, and in ``Off`` mode they land in another view.
    """
    from idp_common.config.prefix_mappings import canonical_key, find_match

    results: List[Tuple[str, str, Optional[str]]] = []
    for tenant in sorted(tenants, key=lambda t: t["tenantId"]):
        for prefix in sorted(tenant["prefixes"]):
            root = canonical_key(prefix)
            match = find_match(f"{root}probe.pdf", mappings)
            governing = match["prefix"] if match else None
            if governing is None or not canonical_key(governing).startswith(root):
                results.append((tenant["tenantId"], prefix, governing))
    return results
