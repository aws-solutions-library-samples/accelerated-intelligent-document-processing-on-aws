"""Carry the templates' scanner suppressions into the generated Terraform.

The CloudFormation templates record accepted findings in two forms, and those
are the only two the tree uses (``tests/test_suppressions.py`` fails on a third):

- a ``# checkov:skip=<id>: <reason>`` comment inside a resource, which checkov
  reads from the lines the resource spans;
- ``Metadata: cfn_nag: rules_to_suppress: [{id, reason}]``, read by cfn_nag.

The generated module is scanned by checkov (``make terraform-scan-diff``), and a
consumer's own checkov reads the same ``#checkov:skip=<id>:<reason>`` comment
inside a Terraform resource block. So a checkov suppression is carried over when,
and only when, :data:`SUPPRESSION_EQUIVALENTS` names the Terraform check that
inspects the same setting with the same failing condition; the comment then goes
into every block of the matching Terraform types emitted for that resource, with
the reason the template gives. cfn_nag has no Terraform rules, and a checkov
check whose Terraform twin fails in cases the CloudFormation one does not
(:data:`DIFFERING_CHECKS`) is not carried: the review the template records did
not cover those cases. Everything not carried is listed, per resource, in
``docs/terraform.md`` (:func:`docs_table`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

#: checkov's own skip-comment pattern (``checkov.common.comment.enum.COMMENT_REGEX``
#: in the pinned 3.4.0). The CloudFormation runner takes group 2 as one id, so a
#: comma-separated list there skips nothing; it is kept as one id here too.
COMMENT = re.compile(
    r"(checkov:skip=|bridgecrew:skip=|cortex:skip=) *([A-Za-z_\d-]+(?:,[A-Za-z_\d-]+)*)?(:[^\n]*)?"
)

_CHECKOV_DOCS = "https://github.com/bridgecrewio/checkov/blob/3.4.0/checkov"


@dataclass(frozen=True)
class Equivalent:
    """A Terraform checkov check that fails exactly when a CloudFormation one does."""

    tf_check: str
    tf_types: tuple[str, ...]
    why: str


#: CloudFormation checkov check id -> the Terraform check it is carried as.
#: One entry per check a template in the translated tree suppresses, each
#: justified against the two check implementations of the pinned checkov
#: (``CHECKOV_VERSION`` in the Makefile), cited by path under
#: ``checkov/cloudformation/checks/resource/aws/`` (cfn) and
#: ``checkov/terraform/checks/`` (tf). "Same failing condition" is read over what
#: a CloudFormation template can express and the translator writes for it.
SUPPRESSION_EQUIVALENTS: dict[str, Equivalent] = {
    # cfn LambdaDLQConfigured.py: fails unless Properties/DeadLetterConfig/TargetArn
    # (SAM: DeadLetterQueue/TargetArn) is set. tf resource/aws/LambdaDLQConfigured.py:
    # fails unless dead_letter_config[0].target_arn is set.
    "CKV_AWS_116": Equivalent(
        "CKV_AWS_116",
        ("aws_lambda_function",),
        "both fail unless the function's dead-letter target is set",
    ),
    # cfn LambdaFunctionLevelConcurrentExecutionLimit.py: fails unless
    # Properties/ReservedConcurrentExecutions is set. tf (same file name): fails
    # when reserved_concurrent_executions is absent or -1, which is the provider's
    # spelling of absent; CloudFormation's minimum is 0, so no template writes -1.
    "CKV_AWS_115": Equivalent(
        "CKV_AWS_115",
        ("aws_lambda_function",),
        "both fail unless reserved concurrency is set",
    ),
    # cfn LambdaInVPC.py: fails unless Properties/VpcConfig is set. tf
    # resource/aws/LambdaInVPC.py: fails unless vpc_config is set.
    "CKV_AWS_117": Equivalent(
        "CKV_AWS_117",
        ("aws_lambda_function",),
        "both fail unless the function has a VPC configuration",
    ),
    # cfn ECRImmutableTags.py: fails unless Properties/ImageTagMutability is
    # IMMUTABLE. tf resource/aws/ECRImmutableTags.py: fails unless
    # image_tag_mutability is IMMUTABLE.
    "CKV_AWS_51": Equivalent(
        "CKV_AWS_51",
        ("aws_ecr_repository",),
        "both fail unless image tags are IMMUTABLE",
    ),
    # cfn ECRImageScanning.py: fails unless
    # Properties/ImageScanningConfiguration/ScanOnPush is true. tf
    # resource/aws/ECRImageScanning.py: fails unless
    # image_scanning_configuration[0].scan_on_push is true.
    "CKV_AWS_163": Equivalent(
        "CKV_AWS_163",
        ("aws_ecr_repository",),
        "both fail unless scan on push is true",
    ),
    # cfn DynamodbRecovery.py: fails unless
    # PointInTimeRecoverySpecification/PointInTimeRecoveryEnabled is true. tf
    # resource/aws/DynamodbRecovery.py: fails unless point_in_time_recovery[0].enabled
    # is true.
    "CKV_AWS_28": Equivalent(
        "CKV_AWS_28",
        ("aws_dynamodb_table",),
        "both fail unless point-in-time recovery is enabled",
    ),
    # cfn DynamoDBTablesEncrypted.py: fails unless SSESpecification has SSEEnabled
    # true and a KMSMasterKeyId. tf resource/aws/DynamoDBTablesEncrypted.py: fails
    # unless server_side_encryption[0] has enabled true and a kms_key_arn.
    "CKV_AWS_119": Equivalent(
        "CKV_AWS_119",
        ("aws_dynamodb_table",),
        "both fail unless server-side encryption is on with a named KMS key",
    ),
    # cfn S3AccessLogs.py: fails unless Properties/LoggingConfiguration is set. tf
    # graph_checks/aws/S3BucketLogging.json: fails unless the aws_s3_bucket has a
    # logging block or an aws_s3_bucket_logging connected to it, which is what the
    # translator writes LoggingConfiguration as.
    "CKV_AWS_18": Equivalent(
        "CKV_AWS_18",
        ("aws_s3_bucket",),
        "both fail unless the bucket has access logging configured",
    ),
    # cfn CloudWatchLogGroupKMSKey.py: fails unless Properties/KmsKeyId is set. tf
    # resource/aws/CloudWatchLogGroupKMSKey.py: fails unless kms_key_id is set.
    "CKV_AWS_158": Equivalent(
        "CKV_AWS_158",
        ("aws_cloudwatch_log_group",),
        "both fail unless the log group names a KMS key",
    ),
    # cfn WAFEnabled.py: fails unless DistributionConfig/WebACLId is set. tf
    # resource/aws/WAFEnabled.py: fails unless web_acl_id is set.
    "CKV_AWS_68": Equivalent(
        "CKV_AWS_68",
        ("aws_cloudfront_distribution",),
        "both fail unless the distribution names a web ACL",
    ),
    # cfn CloudFrontTLS12.py: fails when ViewerCertificate is missing, uses the
    # CloudFront default certificate, or has a MinimumProtocolVersion below TLS 1.2.
    # tf resource/aws/CloudfrontTLS12.py: the same three conditions on
    # viewer_certificate[0], with the same version pattern.
    "CKV_AWS_174": Equivalent(
        "CKV_AWS_174",
        ("aws_cloudfront_distribution",),
        "both fail on a default certificate or a protocol below TLS 1.2",
    ),
    # cfn WAFACLCVE202144228.py: fails unless a rule uses the managed group
    # AWSManagedRulesKnownBadInputsRuleSet without excluding Log4JRCE and with a None
    # override action. tf resource/aws/WAFACLCVE202144228.py: the same rule, group,
    # exclusion and override test on rule/statement/managed_rule_group_statement.
    "CKV_AWS_192": Equivalent(
        "CKV_AWS_192",
        ("aws_wafv2_web_acl",),
        "both fail unless the Log4j managed rule group is in force",
    ),
    # cfn IAMWriteAccess.py (CKV_AWS_111, on AWS::IAM::Role/Policy/ManagedPolicy):
    # fails when cloudsplaining's write_actions_without_constraints is non-empty for
    # any policy document of the resource. tf resource/aws/IAMWriteAccess.py
    # (CKV_AWS_290): the same analysis on each policy resource. checkov numbers the
    # rule separately per framework; CKV_AWS_111 in Terraform is the
    # aws_iam_policy_document data source, which the translator never writes. A
    # role's inline policies become aws_iam_role_policy resources, so the skip goes
    # on each of them.
    "CKV_AWS_111": Equivalent(
        "CKV_AWS_290",
        ("aws_iam_role_policy", "aws_iam_policy"),
        "both run cloudsplaining's write-without-constraints analysis on the "
        "same policy documents",
    ),
}

#: CloudFormation checkov checks a template in the tree suppresses whose
#: Terraform twin fails in cases the CloudFormation check passes, so the review
#: recorded on the template does not cover every Terraform finding. Not carried.
DIFFERING_CHECKS: dict[str, str] = {
    # cfn LambdaEnvironmentEncryptionSettings.py fails only when Environment has
    # Variables and KmsKeyArn is unset; tf (same file name) also fails when
    # kms_key_arn is set and there is no environment block.
    "CKV_AWS_173": "checkov's Terraform rule also fails a function with a KMS key "
    "and no environment variables, which the CloudFormation rule passes",
    # cfn GlueSecurityConfiguration.py passes any mode other than DISABLED; tf
    # (same file name) requires SSE-KMS with a key for CloudWatch and CSE-KMS with a
    # key for job bookmarks.
    "CKV_AWS_99": "checkov's Terraform rule requires KMS modes and keys the "
    "CloudFormation rule does not",
}


@dataclass(frozen=True)
class Suppression:
    tool: str  # checkov | cfn_nag
    rule: str
    reason: str


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _mapping_get(node: yaml.Node, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def checkov_skips(text: str) -> dict[str, list[Suppression]]:
    """``{logical id: [checkov skip]}`` for the comments checkov's CloudFormation
    runner reads: those on the lines from the resource's key to the end of its
    mapping, as ``checkov.cloudformation.context_parser`` slices them."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    resources = _mapping_get(root, "Resources") if root is not None else None
    if not isinstance(resources, yaml.MappingNode):
        return {}
    lines = text.splitlines()
    out: dict[str, list[Suppression]] = {}
    for key, value in resources.value:
        start, end = value.start_mark.line - 1, value.end_mark.line
        span = lines[max(start, 0) : end]
        if end < len(lines) and not text.endswith("\n") and end == len(lines) - 1:
            span.append(lines[end])
        for line in span:
            m = COMMENT.search(line)
            if not m or not m.group(2):
                continue
            reason = _unquote(m.group(3)[1:]) if m.group(3) else ""
            out.setdefault(key.value, []).append(
                Suppression("checkov", m.group(2), reason)
            )
    return out


def cfn_nag_suppressions(template: dict[str, Any]) -> dict[str, list[Suppression]]:
    out: dict[str, list[Suppression]] = {}
    for lid, res in (template.get("Resources") or {}).items():
        meta = (res or {}).get("Metadata") or {}
        nag = meta.get("cfn_nag") if isinstance(meta, dict) else None
        for item in (nag or {}).get("rules_to_suppress") or []:
            if isinstance(item, dict) and item.get("id"):
                out.setdefault(lid, []).append(
                    Suppression("cfn_nag", str(item["id"]), str(item.get("reason", "")))
                )
    return out


@dataclass
class StackSuppressions:
    """One template's suppressions, and its nested stacks' by logical id."""

    source: Path
    resources: dict[str, list[Suppression]]
    children: dict[str, StackSuppressions]


def nested_source(root: Path, url: Any) -> Path | None:
    """A nested stack's source template, by the rule ``cli.local_loader`` uses."""
    if not isinstance(url, str):
        return None
    rel = url.removeprefix("./")
    if not rel.endswith("/.aws-sam/packaged.yaml"):
        return None
    return root / rel.removesuffix("/.aws-sam/packaged.yaml") / "template.yaml"


def read_tree(source: Path) -> StackSuppressions:
    """Suppressions of a source template and of every nested template it names."""
    from .expand import load_template

    return _read(source.resolve(), source.resolve().parent, load_template)


def _read(path: Path, root: Path, load) -> StackSuppressions:
    template = load(path)
    found = checkov_skips(path.read_text())
    for lid, items in cfn_nag_suppressions(template).items():
        found.setdefault(lid, []).extend(items)
    children = {}
    for lid, res in (template.get("Resources") or {}).items():
        if res.get("Type") != "AWS::CloudFormation::Stack":
            continue
        child = nested_source(root, (res.get("Properties") or {}).get("TemplateURL"))
        if child is not None and child.is_file():
            children[lid] = _read(child, root, load)
    return StackSuppressions(path, found, children)


def carried(items: list[Suppression]) -> list[tuple[Equivalent, Suppression]]:
    return [
        (SUPPRESSION_EQUIVALENTS[s.rule], s)
        for s in items
        if s.tool == "checkov" and s.rule in SUPPRESSION_EQUIVALENTS
    ]


def skip_comment(tf_check: str, reason: str) -> str:
    """``#checkov:skip=<id>:<reason>``, on one line as checkov reads it."""
    text = " ".join(reason.split()) or "accepted in the CloudFormation template"
    return f"#checkov:skip={tf_check}:{text}"


_HEADER = re.compile(r'^resource "([a-z0-9_]+)" "[a-z0-9_]+" \{$', re.M)


def annotate(hcl_text: str, items: list[Suppression]) -> str:
    """Insert the carried skips as the first lines of every matching block."""
    pairs = carried(items)
    if not pairs:
        return hcl_text

    def add(m: re.Match[str]) -> str:
        lines = [
            "  " + skip_comment(eq.tf_check, s.reason)
            for eq, s in pairs
            if m.group(1) in eq.tf_types
        ]
        return m.group(0) + "".join("\n" + line for line in lines)

    return _HEADER.sub(add, hcl_text)


def attach(tr: Any, tree: StackSuppressions) -> None:
    """Give a translator tree the suppressions of the matching source tree."""
    tr.suppressions = tree.resources
    for lid, plan in tr.plans.items():
        if plan.kind == "module" and lid in tree.children:
            attach(tr.children[plan.name], tree.children[lid])


def uncarried_rows(
    tree: StackSuppressions, stack: str = ""
) -> list[tuple[str, str, str, str]]:
    """``(resource, tool, rule, reason)`` for every suppression not carried."""
    rows = []
    for lid, items in sorted(tree.resources.items()):
        resource = f"{stack}/{lid}" if stack else lid
        for s in items:
            if s.tool == "checkov" and s.rule in SUPPRESSION_EQUIVALENTS:
                continue
            rows.append((resource, s.tool, s.rule, s.reason))
    for lid, child in sorted(tree.children.items()):
        rows.extend(uncarried_rows(child, f"{stack}/{lid}" if stack else lid))
    return sorted(set(rows))


DOCS_BEGIN = "<!-- uncarried-suppressions:begin (generated; see terraform/translator/idp_tf/suppressions.py) -->"
DOCS_END = "<!-- uncarried-suppressions:end -->"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def docs_table(tree: StackSuppressions) -> str:
    """The generated block of ``docs/terraform.md``, markers included: why each
    checkov rule in :data:`DIFFERING_CHECKS` is not carried, then one row per
    (rule, reason) naming every resource that records it."""
    groups: dict[tuple[str, str, str], list[str]] = {}
    for resource, tool, rule, reason in uncarried_rows(tree):
        groups.setdefault((tool, rule, " ".join(reason.split())), []).append(resource)
    out = [DOCS_BEGIN, ""]
    used = sorted({rule for tool, rule, _ in groups if tool == "checkov"})
    for rule in used:
        # KeyError for a rule in neither table: tests/test_suppressions.py
        # requires every rule the tree suppresses to be in one of them.
        out.append(f"- checkov `{rule}` is not carried: {DIFFERING_CHECKS[rule]}.")
    if used:
        out.append("")
    out += [
        "| Rule | Reason recorded in the template | Resources |",
        "|---|---|---|",
    ]
    for (tool, rule, reason), resources in sorted(groups.items()):
        names = ", ".join(f"`{r}`" for r in sorted(resources))
        out.append(f"| {tool} `{_cell(rule)}` | {_cell(reason) or '-'} | {names} |")
    out += ["", DOCS_END]
    return "\n".join(out)


def splice_docs(doc: str, table: str) -> str:
    """``doc`` with its generated block replaced by ``table``."""
    start, end = doc.index(DOCS_BEGIN), doc.index(DOCS_END) + len(DOCS_END)
    return doc[:start] + table + doc[end:]


REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS_PAGE = REPO_ROOT / "docs" / "terraform.md"


def main(argv: list[str] | None = None) -> int:
    """Regenerate the table in ``docs/terraform.md`` (``--check``: report only)."""
    import argparse

    ap = argparse.ArgumentParser(description=main.__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    doc = DOCS_PAGE.read_text()
    new = splice_docs(doc, docs_table(read_tree(REPO_ROOT / "template.yaml")))
    if new == doc:
        return 0
    if args.check:
        print(f"{DOCS_PAGE} is out of date: run python -m idp_tf.suppressions")
        return 1
    DOCS_PAGE.write_text(new)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
