"""Scanner suppressions carried from the templates into the generated module."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from idp_tf import suppressions as sup
from idp_tf.awscc_schema import AwsccSchema
from idp_tf.cli import main
from idp_tf.translate import Translator

REPO = Path(__file__).resolve().parents[3]
TREE = sup.read_tree(REPO / "template.yaml")


def _walk(tree: sup.StackSuppressions, stack: str = ""):
    for lid, items in tree.resources.items():
        for item in items:
            yield stack, lid, item
    for lid, child in tree.children.items():
        yield from _walk(child, f"{stack}/{lid}" if stack else lid)


def _sources(tree: sup.StackSuppressions) -> list[Path]:
    return [tree.source] + [p for c in tree.children.values() for p in _sources(c)]


@pytest.fixture(scope="module")
def module(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("sup") / "m"
    assert main([str(REPO / "template.yaml"), "--offline", "--out", str(out)]) == 0
    return out


def _block(module: Path, address: str) -> str:
    """The text of one resource block, by module-qualified address."""
    parts = address.split(".")
    directory = module
    while parts[0] == "module":
        directory = directory / "modules" / parts[1]
        parts = parts[2:]
    rtype, name = parts
    text = "\n".join(p.read_text() for p in sorted(directory.glob("*.tf")))
    start = text.index(f'resource "{rtype}" "{name}" {{\n')
    return text[start : text.index("\n}\n", start)]


# ------------------------------------------------------------ the universe
def test_the_tree_uses_only_the_suppression_forms_this_module_reads():
    """A third form (checkov or bridgecrew Metadata, cfn-guard SuppressedRules, a
    trivy or tfsec comment) would be neither carried nor listed in the docs."""
    for path in _sources(TREE):
        text = path.read_text()
        assert not re.search(r"(trivy|tfsec):ignore", text), path
        template = yaml.compose(text, Loader=yaml.SafeLoader)
        resources = sup._mapping_get(template, "Resources")
        for _, res in resources.value:
            meta = sup._mapping_get(res, "Metadata")
            keys = {k.value for k, _ in meta.value} if meta is not None else set()
            assert not keys & {"checkov", "bridgecrew", "guard"}, (path, keys)


def test_every_checkov_rule_the_tree_suppresses_is_decided_once():
    """Universe closure and non-vacuity: each rule is either carried or named as
    differing, never both, and neither table holds a rule nothing suppresses."""
    # A rule in neither table is unaccounted for and fails here.
    used = {s.rule for _, _, s in _walk(TREE) if s.tool == "checkov"}
    carried, differing = set(sup.SUPPRESSION_EQUIVALENTS), set(sup.DIFFERING_CHECKS)
    assert not carried & differing
    assert used - carried - differing == set(), "decide these rules"
    assert carried - used == set(), "stale SUPPRESSION_EQUIVALENTS entries"
    assert differing - used == set(), "stale DIFFERING_CHECKS entries"


def test_skips_are_read_from_the_lines_checkov_reads():
    """checkov's CloudFormation runner reads a resource's lines from its key to
    the end of its mapping, so a comment above a key belongs to the resource
    before it, and one above the first resource to none."""
    text = (
        "Resources:\n"
        "  # checkov:skip=CKV_AWS_1: above the first key\n"
        "  A:\n"
        "    Type: AWS::SNS::Topic\n"
        '    # checkov:skip=CKV_AWS_2: "inside A"\n'
        "    Properties:\n"
        "      TopicName: !Sub '${AWS::StackName}-a'\n"
        "  # checkov:skip=CKV_AWS_3:between A and B\n"
        "  B:  # checkov:skip=CKV_AWS_5: on the key line\n"
        "    Type: AWS::SNS::Topic\n"
        "    # checkov:skip=CKV_AWS_4\n"
    )
    found = sup.checkov_skips(text)
    assert found == {
        "A": [
            sup.Suppression("checkov", "CKV_AWS_2", "inside A"),
            sup.Suppression("checkov", "CKV_AWS_3", "between A and B"),
        ],
        "B": [
            sup.Suppression("checkov", "CKV_AWS_5", "on the key line"),
            sup.Suppression("checkov", "CKV_AWS_4", ""),
        ],
    }


def test_a_skip_on_the_last_line_of_a_file_without_a_newline_is_read():
    """checkov appends that line to the last resource (measured with the pinned
    checkov: it reports CKV_AWS_158 skipped on this template)."""
    text = (
        "Resources:\n"
        "  A:\n"
        "    Type: AWS::Logs::LogGroup\n"
        "    Properties:\n"
        "      RetentionInDays: 7  # checkov:skip=CKV_AWS_158: last line"
    )
    assert sup.checkov_skips(text) == {
        "A": [sup.Suppression("checkov", "CKV_AWS_158", "last line")]
    }


# ------------------------------------------------- each mapping, end to end
@pytest.mark.parametrize("rule", sorted(sup.SUPPRESSION_EQUIVALENTS))
def test_each_carried_rule_reaches_every_matching_block(rule, module):
    """For every resource in the standard tree that suppresses ``rule``, every
    block of the mapped Terraform types generated from it carries the skip with
    the template's reason, and at least one such block exists."""
    eq = sup.SUPPRESSION_EQUIVALENTS[rule]
    manifest = json.loads((module / "idp-terraform.json").read_text())
    addresses = {
        (e["stack"], e["logicalId"]): e["addresses"] for e in manifest["resources"]
    }
    checked = 0
    for stack, lid, s in _walk(TREE):
        if s.tool != "checkov" or s.rule != rule:
            continue
        for address in addresses.get((stack, lid), []):
            if address.split(".")[-2] not in eq.tf_types:
                continue
            assert sup.skip_comment(eq.tf_check, s.reason) in _block(module, address)
            checked += 1
    assert checked, f"{rule}: no {eq.tf_types} block carries it"


def test_the_module_carries_nothing_else(module):
    """The number of skip comments is exactly what the tree's carried
    suppressions produce, and no uncarried rule appears as a skip."""
    manifest = json.loads((module / "idp-terraform.json").read_text())
    addresses = {
        (e["stack"], e["logicalId"]): e["addresses"] for e in manifest["resources"]
    }
    expected = 0
    for stack, lid, s in _walk(TREE):
        eq = sup.SUPPRESSION_EQUIVALENTS.get(s.rule) if s.tool == "checkov" else None
        if eq is None:
            continue
        expected += sum(
            a.split(".")[-2] in eq.tf_types for a in addresses.get((stack, lid), [])
        )
    text = "\n".join(p.read_text() for p in sorted(module.rglob("*.tf")))
    found = re.findall(r"#checkov:skip=([A-Z0-9_]+):", text)
    assert len(found) == expected
    assert set(found) == {e.tf_check for e in sup.SUPPRESSION_EQUIVALENTS.values()}


# ------------------------------------------------------ one resource, by hand
FRAGMENT = """\
Resources:
  Fn:
    Type: AWS::Lambda::Function
    Metadata:
      cfn_nag:
        rules_to_suppress:
          - id: W92
            reason: concurrency scales on demand
    # checkov:skip=CKV_AWS_116: "invoked synchronously, nothing for a DLQ to hold"
    # checkov:skip=CKV_AWS_173: "no sensitive variables"
    Properties:
      Role: arn:x
      Code: {S3Bucket: b, S3Key: k}
      Environment:
        Variables: {A: b}
  R:
    Type: AWS::IAM::Role
    # checkov:skip=CKV_AWS_111:write access is scoped by the boundary
    Properties:
      AssumeRolePolicyDocument: {Version: "2012-10-17"}
      Policies:
        - PolicyName: p
          PolicyDocument: {Statement: []}
        - PolicyName: q
          PolicyDocument: {Statement: []}
  Logs:
    Type: AWS::Logs::LogGroup
    Metadata:
      cfn_nag:
        rules_to_suppress:
          - id: CKV_AWS_158
            reason: checkov does not read cfn_nag metadata
"""


@pytest.fixture
def fragment(tmp_path: Path) -> sup.StackSuppressions:
    path = tmp_path / "template.yaml"
    path.write_text(FRAGMENT)
    return sup.read_tree(path)


def _render(tmp_path: Path, tree: sup.StackSuppressions) -> tuple[Translator, str]:
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "provider_schemas": {
                    "registry.terraform.io/hashicorp/awscc": {"resource_schemas": {}}
                }
            }
        )
    )
    tr = Translator(yaml.safe_load(FRAGMENT), AwsccSchema(schema))
    sup.attach(tr, tree)
    return tr, tr.render()["main.tf"]


def test_a_mapped_suppression_is_carried_and_an_unmapped_one_is_listed(
    tmp_path, fragment
):
    tr, out = _render(tmp_path, fragment)
    fn = out[out.index('resource "aws_lambda_function" "fn" {') :]
    fn = fn[: fn.index("\n}\n")]
    assert (
        "  #checkov:skip=CKV_AWS_116:invoked synchronously, nothing for a DLQ to hold"
        in fn.splitlines()
    )
    assert "CKV_AWS_173" not in out and "W92" not in out
    # Only checkov's own comment is carried, whatever id a cfn_nag entry names.
    assert "CKV_AWS_158" not in out
    # checkov numbers the IAM rule separately per framework, and a role's
    # inline policies are separate Terraform resources: each gets the skip.
    for name in ("r_p", "r_q"):
        block = out[out.index(f'resource "aws_iam_role_policy" "{name}" {{') :]
        assert block.splitlines()[1] == (
            "  #checkov:skip=CKV_AWS_290:write access is scoped by the boundary"
        )
    role = out[out.index('resource "aws_iam_role" "r" {') :]
    assert "#checkov" not in role[: role.index("\n}\n")]
    # The completeness check reads ``emitted``: the reason text stays out of it.
    assert "#checkov" not in "".join(tr.emitted.values())
    rows = sup.uncarried_rows(fragment)
    assert rows == [
        ("Fn", "cfn_nag", "W92", "concurrency scales on demand"),
        ("Fn", "checkov", "CKV_AWS_173", "no sensitive variables"),
        ("Logs", "cfn_nag", "CKV_AWS_158", "checkov does not read cfn_nag metadata"),
    ]
    table = sup.docs_table(fragment)
    assert "| checkov `CKV_AWS_173` | no sensitive variables | `Fn` |" in table
    assert "| cfn_nag `W92` | concurrency scales on demand | `Fn` |" in table
    assert "CKV_AWS_116" not in table


def test_the_docs_table_is_current():
    """``docs/terraform.md`` lists exactly what the templates record and the
    module does not carry. Regenerate with ``python -m idp_tf.suppressions``."""
    doc = sup.DOCS_PAGE.read_text()
    assert sup.splice_docs(doc, sup.docs_table(TREE)) == doc
    assert sup.main(["--check"]) == 0
