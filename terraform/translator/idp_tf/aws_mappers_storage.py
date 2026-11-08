"""Hand-written hashicorp/aws mappings for S3, DynamoDB and Lambda wiring.

These types are in the generated awscc schema, but awscc compares what the
service returns literally: S3 and DynamoDB return lists sorted, bucket policies
come back re-encoded, and Lambda stores a function name as an ARN and an
account principal as a root ARN. Each shows a change on every plan, and some
force a replacement. The hashicorp/aws resources normalise all of these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import hcl
from .aws_mappers import (
    DEFAULTS,
    SpecMapper,
    _if_leaves,
    default_name,
    render_spec,
)
from .translate import NO_VALUE, STACK_NAME_VAR, TranslationError, is_intrinsic

if TYPE_CHECKING:
    from .aws_mappers import Spec
    from .translate import Plan, Translator


GENERATED_NAME_LIFECYCLE = "lifecycle {{\n  ignore_changes = [{attrs}]\n}}"


def _kms_arn(tr: Translator, v: Any) -> str:
    """A key reference as an ARN: the provider takes only ARNs here."""
    if isinstance(v, dict) and set(v) == {"Ref"}:
        target = tr.plans.get(v["Ref"])
        if target is not None and target.cfn_type == "AWS::KMS::Key":
            return tr.getatt(v["Ref"], "Arn")
    return tr.expr(v)


def _guarded(tr: Translator, plan: Plan, value: Any) -> list[tuple[str, Any]]:
    """(count line or "", value) per branch of a possibly conditional value."""
    out = []
    for guard, leaf in _if_leaves(tr, value, []):
        guards = [g for g in (guard,) if g != "true"]
        if plan.condition:
            guards.insert(0, tr.cond_ref(plan.condition))
        count = f"count = ({' && '.join(guards)}) ? 1 : 0" if guards else ""
        out.append((count, leaf))
    return out


# ----------------------------------------------------------------- S3 bucket
S3_SUBRESOURCES: dict[str, tuple[str, str, Spec]] = {
    # cfn property -> (tf type, label suffix, spec of the body under the bucket)
    "BucketEncryption": (
        "aws_s3_bucket_server_side_encryption_configuration",
        "encryption",
        {
            "ServerSideEncryptionConfiguration": (
                "blocks",
                "rule",
                {
                    "BucketKeyEnabled": ("attr", "bucket_key_enabled"),
                    "ServerSideEncryptionByDefault": (
                        "block",
                        "apply_server_side_encryption_by_default",
                        {
                            "SSEAlgorithm": ("attr", "sse_algorithm"),
                            "KMSMasterKeyID": ("kms_arn", "kms_master_key_id"),
                        },
                    ),
                },
            ),
        },
    ),
    "CorsConfiguration": (
        "aws_s3_bucket_cors_configuration",
        "cors",
        {
            "CorsRules": (
                "blocks",
                "cors_rule",
                {
                    "Id": ("attr", "id"),
                    "AllowedHeaders": ("attr", "allowed_headers"),
                    "AllowedMethods": ("attr", "allowed_methods"),
                    "AllowedOrigins": ("attr", "allowed_origins"),
                    "ExposedHeaders": ("attr", "expose_headers"),
                    "MaxAge": ("attr", "max_age_seconds"),
                },
            ),
        },
    ),
    "LifecycleConfiguration": (
        "aws_s3_bucket_lifecycle_configuration",
        "lifecycle",
        {
            "Rules": (
                "blocks",
                "rule",
                {
                    "Id": ("attr", "id"),
                    "Status": ("attr", "status"),
                    "Prefix": ("block_attr", "filter", "prefix"),
                    "ExpirationInDays": ("block_attr", "expiration", "days"),
                    "NoncurrentVersionExpiration": (
                        "block",
                        "noncurrent_version_expiration",
                        {"NoncurrentDays": ("attr", "noncurrent_days")},
                    ),
                    "AbortIncompleteMultipartUpload": (
                        "block",
                        "abort_incomplete_multipart_upload",
                        {"DaysAfterInitiation": ("attr", "days_after_initiation")},
                    ),
                    # The provider wants a filter on every rule; CloudFormation's
                    # rule without Prefix applies to the whole bucket.
                    DEFAULTS: ("defaults_block", {"filter": ""}),
                },
            ),
        },
    ),
    "LoggingConfiguration": (
        "aws_s3_bucket_logging",
        "logging",
        {
            "DestinationBucketName": ("attr", "target_bucket"),
            "LogFilePrefix": ("attr", "target_prefix"),
            DEFAULTS: ("defaults", {"target_prefix": '""'}),
        },
    ),
    "NotificationConfiguration": (
        "aws_s3_bucket_notification",
        "notification",
        {
            "EventBridgeConfiguration": (
                "flatten",
                {"EventBridgeEnabled": ("attr", "eventbridge")},
            ),
        },
    ),
    "OwnershipControls": (
        "aws_s3_bucket_ownership_controls",
        "ownership",
        {
            "Rules": (
                "blocks",
                "rule",
                {"ObjectOwnership": ("attr", "object_ownership")},
            ),
        },
    ),
    "PublicAccessBlockConfiguration": (
        "aws_s3_bucket_public_access_block",
        "public_access",
        {
            "BlockPublicAcls": ("attr", "block_public_acls"),
            "BlockPublicPolicy": ("attr", "block_public_policy"),
            "IgnorePublicAcls": ("attr", "ignore_public_acls"),
            "RestrictPublicBuckets": ("attr", "restrict_public_buckets"),
        },
    ),
    "VersioningConfiguration": (
        "aws_s3_bucket_versioning",
        "versioning",
        {
            "Status": (
                "block_attr",
                "versioning_configuration",
                "status",
            ),
        },
    ),
    "WebsiteConfiguration": (
        "aws_s3_bucket_website_configuration",
        "website",
        {
            "IndexDocument": ("block_attr", "index_document", "suffix"),
            "ErrorDocument": ("block_attr", "error_document", "key"),
        },
    ),
}


class S3BucketMapper:
    """One bucket becomes ``aws_s3_bucket`` and one resource per configured
    feature. Anything referencing the bucket waits for all of them, which is
    what CloudFormation's single resource gives a dependant."""

    tf_type = "aws_s3_bucket"
    ref_attr = "id"

    def getatt(self, attr: str) -> Any:
        return {
            "Arn": "arn",
            "DomainName": "bucket_domain_name",
            "RegionalDomainName": "bucket_regional_domain_name",
            "WebsiteURL": ("website_endpoint", '"http://${%s}"'),
        }[attr]

    def plan(self, tr: Translator, plan: Plan) -> None:
        # Exactly the resources emit() writes: one per branch that has a value.
        # A property that resolves to AWS::NoValue (the GovCloud transform sets
        # WebsiteConfiguration to it) has no leaves and so writes nothing; a
        # dependency on it would name an undeclared resource.
        for prop, (tf_type, suffix, _) in S3_SUBRESOURCES.items():
            if prop in plan.props:
                n = len(_if_leaves(tr, plan.props[prop], []))
                for j in range(n):
                    label = f"{plan.name}_{suffix}" + (f"_{j}" if n > 1 else "")
                    plan.extra_deps.append(f"{tf_type}.{label}")

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        acl = props.pop("AccessControl", "Private")
        if acl != "Private":
            raise TranslationError(f"{plan.logical_id}: AccessControl {acl!r}")
        lines = []
        generated_name = False
        if "BucketName" in props:
            lines.append(f"bucket = {tr.expr(props.pop('BucketName'))}")
        else:
            # Bucket names are global: let the provider add a random suffix, as
            # CloudFormation does, after the stack-name prefix.
            prefix = f"${{var.{STACK_NAME_VAR}}}-{plan.logical_id}"
            lines.append(f'bucket_prefix = lower(substr("{prefix}", 0, 36))')
            generated_name = True
        if "Tags" in props:
            from .aws_mappers import _tags

            lines.append(f"tags = {_tags(tr, props.pop('Tags'))}")
        sub = {k: props.pop(k) for k in list(props) if k in S3_SUBRESOURCES}
        if props:
            raise TranslationError(f"{plan.logical_id}: unmapped {set(props)}")
        head, tail = tr.meta_lines(plan, res)
        if generated_name:
            # As for tables: keep the name a bucket was created with if a later
            # version derives the prefix differently, rather than replacing it.
            tail = tail + [GENERATED_NAME_LIFECYCLE.format(attrs="bucket_prefix")]
        out = [
            hcl.block(f'resource "aws_s3_bucket" "{plan.name}"', head + lines + tail)
        ]
        bucket = f"aws_s3_bucket.{plan.name}{'[0]' if plan.condition else ''}.id"
        for prop, value in sub.items():
            tf_type, suffix, spec = S3_SUBRESOURCES[prop]
            leaves = _guarded(tr, plan, value)
            for j, (count, leaf) in enumerate(leaves):
                label = f"{plan.name}_{suffix}" + (f"_{j}" if len(leaves) > 1 else "")
                body = render_storage_spec(tr, spec, leaf, f"{plan.logical_id}.{prop}")
                out.append(
                    hcl.block(
                        f'resource "{tf_type}" "{label}"',
                        ([count] if count else []) + [f"bucket = {bucket}"] + body,
                    )
                )
        return "\n".join(out)


def render_storage_spec(tr: Translator, spec: Spec, v: Any, where: str) -> list[str]:
    """render_spec plus the shapes these resources need: an attribute moved
    into its own one-attribute block, and default empty blocks."""
    if is_intrinsic(v):
        raise TranslationError(f"{where}: must be literal")
    lines: list[str] = []
    plain: dict[str, Any] = {}
    plain_spec: Spec = {}
    for k, value in v.items():
        entry = spec.get(k)
        if entry is None:
            raise TranslationError(f"{where}: unmapped property {k!r}")
        if entry[0] == "block_attr":
            lines.append(
                hcl.block(entry[1], [f"{entry[2]} = {tr.expr(value)}"]).rstrip()
            )
        elif entry[0] == "kms_arn":
            lines.append(f"{entry[1]} = {_kms_arn(tr, value)}")
        elif entry[0] in ("blocks", "block"):
            name, sub = entry[1], entry[2]
            items = value if entry[0] == "blocks" else [value]
            if is_intrinsic(items):
                raise TranslationError(f"{where}.{k}: must be literal")
            for i, item in enumerate(items):
                for guard, leaf in _if_leaves(tr, item, []):
                    body = render_storage_spec(tr, sub, leaf, f"{where}.{k}[{i}]")
                    if guard == "true":
                        lines.append(hcl.block(name, body).rstrip())
                    else:
                        lines.append(
                            hcl.block(
                                f'dynamic "{name}"',
                                [
                                    f"for_each = ({guard}) ? [1] : []",
                                    hcl.block("content", body).rstrip(),
                                ],
                            ).rstrip()
                        )
        else:
            plain[k] = value
            plain_spec[k] = entry
    if plain:
        lines.extend(render_spec(tr, plain_spec, plain, where))
    defaults = spec.get(DEFAULTS)
    if defaults is not None:
        kind, values = defaults
        set_names = {spec[k][1] for k in v if k in spec and spec[k][0] != "flatten"}
        for name, value in values.items():
            if name in set_names:
                continue
            if kind == "defaults_block":
                lines.append(f"{name} {{}}")
            else:
                lines.append(f"{name} = {value}")
    return lines


# ------------------------------------------------------------- DynamoDB table
class DynamoDbTableMapper:
    tf_type = "aws_dynamodb_table"
    ref_attr = "name"
    spec: Spec = {
        "BillingMode": ("attr", "billing_mode"),
        "DeletionProtectionEnabled": ("attr", "deletion_protection_enabled"),
        "TableClass": ("attr", "table_class"),
    }

    def getatt(self, attr: str) -> Any:
        return {"Arn": "arn", "StreamArn": "stream_arn"}[attr]

    def plan(self, tr: Translator, plan: Plan) -> None:
        return None

    @staticmethod
    def _keys(tr: Translator, schema: Any, where: str) -> list[str]:
        if is_intrinsic(schema):
            raise TranslationError(f"{where}: KeySchema must be literal")
        out = []
        for k in schema:
            kind = {"HASH": "hash_key", "RANGE": "range_key"}[k["KeyType"]]
            out.append(f"{kind} = {tr.expr(k['AttributeName'])}")
        return out

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        where = plan.logical_id
        lines = []
        name = props.pop("TableName", None)
        suffix_blocks: list[str] = []
        if name is not None:
            lines.append(f"name = {tr.expr(name)}")
        elif plan.logical_id in tr.retained:
            # A delete keeps this table, and CloudFormation would give the next
            # deployment of the same name a new random name. A deterministic
            # name would collide with the kept table on redeploy, so add a
            # suffix fixed when the table is first created.
            stamp = f"{plan.name}_name_suffix"
            count = (
                [f"count = {tr.cond_ref(plan.condition)} ? 1 : 0"]
                if plan.condition
                else []
            )
            suffix_blocks.append(hcl.block(f'resource "time_static" "{stamp}"', count))
            idx = "[0]" if plan.condition else ""
            lines.append(
                f'name = "${{substr("${{var.{STACK_NAME_VAR}}}-{where}", 0, 240)}}-'
                f'${{time_static.{stamp}{idx}.unix}}"'
            )
        else:
            lines.append(f"name = {default_name(where, 255)}")
        lines += self._keys(tr, props.pop("KeySchema"), where)
        for a in props.pop("AttributeDefinitions", []):
            lines.append(
                hcl.block(
                    "attribute",
                    [
                        f"name = {tr.expr(a['AttributeName'])}",
                        f"type = {tr.expr(a['AttributeType'])}",
                    ],
                ).rstrip()
            )
        for i, gsi in enumerate(props.pop("GlobalSecondaryIndexes", []) or []):
            for guard, leaf in _if_leaves(tr, gsi, []):
                extra = set(leaf) - {"IndexName", "KeySchema", "Projection"}
                if extra:
                    raise TranslationError(
                        f"{where}.GlobalSecondaryIndexes[{i}]: {extra}"
                    )
                body = [f"name = {tr.expr(leaf['IndexName'])}"]
                body += self._keys(tr, leaf["KeySchema"], where)
                proj = leaf.get("Projection", {})
                body.append(
                    f"projection_type = {tr.expr(proj.get('ProjectionType', 'ALL'))}"
                )
                if "NonKeyAttributes" in proj:
                    body.append(
                        f"non_key_attributes = {tr.expr(proj['NonKeyAttributes'])}"
                    )
                block = hcl.block("global_secondary_index", body).rstrip()
                if guard != "true":
                    block = hcl.block(
                        'dynamic "global_secondary_index"',
                        [
                            f"for_each = ({guard}) ? [1] : []",
                            hcl.block("content", body).rstrip(),
                        ],
                    ).rstrip()
                lines.append(block)
        pitr = props.pop("PointInTimeRecoverySpecification", None)
        if pitr is not None:
            lines.append(
                hcl.block(
                    "point_in_time_recovery",
                    [
                        f"enabled = {tr.expr(pitr.get('PointInTimeRecoveryEnabled', False))}"
                    ],
                ).rstrip()
            )
        sse = props.pop("SSESpecification", None)
        if sse is not None:
            body = [f"enabled = {tr.expr(sse.get('SSEEnabled', False))}"]
            if "KMSMasterKeyId" in sse:
                body.append(f"kms_key_arn = {_kms_arn(tr, sse['KMSMasterKeyId'])}")
            lines.append(hcl.block("server_side_encryption", body).rstrip())
        stream = props.pop("StreamSpecification", None)
        if stream is not None:
            lines.append("stream_enabled = true")
            lines.append(f"stream_view_type = {tr.expr(stream['StreamViewType'])}")
        ttl = props.pop("TimeToLiveSpecification", None)
        if ttl is not None:
            lines.append(
                hcl.block(
                    "ttl",
                    [
                        f"attribute_name = {tr.expr(ttl['AttributeName'])}",
                        f"enabled = {tr.expr(ttl.get('Enabled', False))}",
                    ],
                ).rstrip()
            )
        if "Tags" in props:
            from .aws_mappers import _tags

            lines.append(f"tags = {_tags(tr, props.pop('Tags'))}")
        lines += render_spec(tr, self.spec, props, where)
        head, tail = tr.meta_lines(plan, res)
        if name is None:
            # The translator chose this name, and a later version may choose it
            # differently. A table's name forces replacement, which would delete
            # an existing deployment's data, so keep the name it was created with.
            tail = tail + [GENERATED_NAME_LIFECYCLE.format(attrs="name")]
        table = hcl.block(
            f'resource "aws_dynamodb_table" "{plan.name}"', head + lines + tail
        )
        return "\n".join(suffix_blocks + [table])


BUCKET_POLICY = SpecMapper(
    tf_type="aws_s3_bucket_policy",
    ref_attr="id",
    attrs={},
    spec={"Bucket": ("attr", "bucket"), "PolicyDocument": ("json", "policy")},
)


class LambdaPermissionMapper(SpecMapper):
    """Lambda stores an account principal as ``arn:<partition>:iam::<id>:root``
    and the provider compares literally, forcing a replacement every plan, so
    an account id is written in the stored form (an equivalent principal)."""

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        principal = plan.props.get("Principal")
        if principal == {"Ref": "AWS::AccountId"} or (
            isinstance(principal, str) and principal.isdigit() and len(principal) == 12
        ):
            account = "${AWS::AccountId}" if isinstance(principal, dict) else principal
            original = plan.props
            plan.props = dict(
                original,
                Principal={"Fn::Sub": f"arn:${{AWS::Partition}}:iam::{account}:root"},
            )
            try:
                return super().emit(tr, plan, res)
            finally:
                plan.props = original
        return super().emit(tr, plan, res)


LAMBDA_PERMISSION = LambdaPermissionMapper(
    tf_type="aws_lambda_permission",
    ref_attr="id",
    attrs={},
    spec={
        "Action": ("attr", "action"),
        "FunctionName": ("attr", "function_name"),
        "Principal": ("attr", "principal"),
        "SourceAccount": ("attr", "source_account"),
        "SourceArn": ("attr", "source_arn"),
        "FunctionUrlAuthType": ("attr", "function_url_auth_type"),
        "PrincipalOrgID": ("attr", "principal_org_id"),
        "EventSourceToken": ("attr", "event_source_token"),
    },
)

EVENT_SOURCE_MAPPING = SpecMapper(
    tf_type="aws_lambda_event_source_mapping",
    ref_attr="uuid",
    attrs={"Id": "uuid"},
    spec={
        "BatchSize": ("attr", "batch_size"),
        "EventSourceArn": ("attr", "event_source_arn"),
        "FunctionName": ("attr", "function_name"),
        "FunctionResponseTypes": ("attr", "function_response_types"),
        "MaximumBatchingWindowInSeconds": (
            "attr",
            "maximum_batching_window_in_seconds",
        ),
        "StartingPosition": ("attr", "starting_position"),
        "Enabled": ("attr", "enabled"),
        "MaximumRetryAttempts": ("attr", "maximum_retry_attempts"),
        "BisectBatchOnFunctionError": ("attr", "bisect_batch_on_function_error"),
        "ParallelizationFactor": ("attr", "parallelization_factor"),
    },
)

DASHBOARD = SpecMapper(
    tf_type="aws_cloudwatch_dashboard",
    ref_attr="dashboard_name",
    attrs={},
    required_name=("DashboardName", "dashboard_name", 255),
    spec={
        "DashboardName": ("attr", "dashboard_name"),
        # Compared as JSON by the provider; awscc compares the text.
        "DashboardBody": ("attr", "dashboard_body"),
    },
)


class CognitoUserMapper:
    tf_type = "aws_cognito_user"
    ref_attr = "username"

    def getatt(self, attr: str) -> Any:
        raise TranslationError(f"UserPoolUser has no attribute {attr}")

    def plan(self, tr: Translator, plan: Plan) -> None:
        return None

    def emit(self, tr: Translator, plan: Plan, res: dict[str, Any]) -> str:
        props = dict(plan.props)
        lines = [
            f"user_pool_id = {tr.expr(props.pop('UserPoolId'))}",
            f"username = {tr.expr(props.pop('Username'))}",
        ]
        attrs = props.pop("UserAttributes", [])
        if attrs:
            if is_intrinsic(attrs):
                raise TranslationError(
                    f"{plan.logical_id}: UserAttributes literal only"
                )
            items = [(a["Name"], tr.expr(a["Value"])) for a in attrs]
            lines.append("attributes = " + hcl.obj(items, quote_keys=True))
        spec: Spec = {
            "DesiredDeliveryMediums": ("attr", "desired_delivery_mediums"),
            "MessageAction": ("attr", "message_action"),
            "ForceAliasCreation": ("attr", "force_alias_creation"),
            "ClientMetadata": ("map", "client_metadata"),
        }
        lines += render_spec(tr, spec, props, plan.logical_id)
        head, tail = tr.meta_lines(plan, res)
        return hcl.block(
            f'resource "aws_cognito_user" "{plan.name}"', head + lines + tail
        )


USER_IN_GROUP = SpecMapper(
    tf_type="aws_cognito_user_in_group",
    ref_attr="id",
    attrs={},
    spec={
        "UserPoolId": ("attr", "user_pool_id"),
        "GroupName": ("attr", "group_name"),
        "Username": ("attr", "username"),
    },
)

STORAGE_MAPPERS: dict[str, Any] = {
    "AWS::CloudWatch::Dashboard": DASHBOARD,
    "AWS::Cognito::UserPoolUser": CognitoUserMapper(),
    "AWS::Cognito::UserPoolUserToGroupAttachment": USER_IN_GROUP,
    "AWS::S3::Bucket": S3BucketMapper(),
    "AWS::S3::BucketPolicy": BUCKET_POLICY,
    "AWS::DynamoDB::Table": DynamoDbTableMapper(),
    "AWS::Lambda::Permission": LAMBDA_PERMISSION,
    "AWS::Lambda::EventSourceMapping": EVENT_SOURCE_MAPPING,
}

__all__ = ["NO_VALUE", "STORAGE_MAPPERS"]
