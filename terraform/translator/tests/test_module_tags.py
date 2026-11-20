"""The module's ``tags`` input reaches every taggable ``awscc`` resource.

``hashicorp/awscc`` has no ``default_tags``, so a tag a root configuration puts
on its ``aws`` provider stops at the ``aws`` resources. The module therefore has
a ``tags`` variable, and the translator merges it into the tag attribute of
every ``awscc`` resource whose schema has one, in that attribute's own shape.
Which types are taggable, and in which shape, is read from the vendored
provider schema (``AwsccSchema.tag_attribute``); the registry's own tagging
metadata, as ``cfn-lint`` ships it, is the cross-check.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from idp_tf import translate
from idp_tf.awscc_schema import AwsccSchema, awscc_type_name, tag_attribute_of
from idp_tf.cli import TEMPLATE_VARIANTS, VENDORED_SCHEMA, main
from idp_tf.translate import TranslationError, Translator

REPO = Path(__file__).resolve().parents[3]
CREATE_ONLY = VENDORED_SCHEMA.parent / "create_only.json"
RESOURCE = re.compile(
    r'^resource "(awscc_[a-z0-9_]+)" "([a-z0-9_]+)" \{\n(.*?)^\}\n', re.M | re.S
)
MODULE = re.compile(r'^module "([a-z0-9_]+)" \{\n(.*?)^\}\n', re.M | re.S)


@pytest.fixture(scope="module")
def vendored() -> AwsccSchema:
    return AwsccSchema(VENDORED_SCHEMA)


def _vendored_resources() -> dict:
    data = json.loads(VENDORED_SCHEMA.read_text())
    return next(iter(data["provider_schemas"].values()))["resource_schemas"]


# ------------------------------------------------------- the schema derivation
def test_the_vendored_schema_has_taggable_types_in_every_shape():
    """The derivation finds something, in each shape the merge handles: an
    empty result would make every check below pass vacuously."""
    shapes = {}
    for name, spec in _vendored_resources().items():
        found = tag_attribute_of(spec["block"].get("attributes", {}), name)
        if found:
            shapes.setdefault(found[1], set()).add(name)
    assert set(shapes) == {"kv", "map", "json"}, shapes
    assert "awscc_sqs_queue" in shapes["kv"]
    # IdentityPoolTags: a tag property not named Tags.
    assert "awscc_cognito_identity_pool" in shapes["kv"]


def test_every_type_the_registry_calls_taggable_is_found_with_its_tag_property():
    """For every type the templates use that awscc has, the registry's
    ``tagging.tagProperty`` is the attribute the schema derivation picks."""
    pytest.importorskip("cfnlint")
    from cfnlint.schema import PROVIDER_SCHEMA_MANAGER

    schema = AwsccSchema(VENDORED_SCHEMA)
    types = json.loads(CREATE_ONLY.read_text())["types"]
    checked, wrong = [], []
    for cfn_type in sorted(types):
        if not schema.has(cfn_type):
            continue
        registry = PROVIDER_SCHEMA_MANAGER.get_resource_schema("us-east-1", cfn_type)
        tagging = registry.schema.get("tagging") or {}
        if not tagging.get("taggable") or "tagProperty" not in tagging:
            continue
        prop = tagging["tagProperty"].removeprefix("/properties/")
        found = schema.tag_attribute(cfn_type)
        want = schema.hint(cfn_type).attr(prop)
        checked.append(cfn_type)
        if found is None or want is None or found[0] != want[0]:
            wrong.append((cfn_type, prop, found))
    assert checked, "no taggable type checked"
    assert not wrong, wrong


# ------------------------------------------------------- the generated tree
def _translate(out: Path, variant: str | None, mode: str = "hybrid") -> Path:
    args = [str(REPO / "template.yaml"), "--offline", "--out", str(out)]
    args += ["--label-template", str(REPO / "template.yaml"), "--version", "0.0.0"]
    args += ["--mode", mode]
    if variant:
        args += ["--variant", variant]
    assert main(args) == 0
    return out


@pytest.mark.parametrize(
    ("variant", "mode"),
    [(v, "hybrid") for _, v in TEMPLATE_VARIANTS] + [(None, "awscc")],
)
def test_every_taggable_awscc_resource_in_the_tree_takes_the_module_tags(
    variant, mode, vendored, tmp_path
):
    """Scans the generated HCL. A taggable awscc resource whose tag attribute
    is absent, or does not read ``var.tags``, fails here; so does a nested
    module the parent does not pass the tags to. ``--mode awscc`` sends the
    hand-mapped types (functions, roles, buckets, tables) to awscc too."""
    root = _translate(tmp_path / "m", variant, mode)
    tf_to_cfn = {
        awscc_type_name(t): t for t in json.loads(CREATE_ONLY.read_text())["types"]
    }
    merged, missing = 0, []
    for path in sorted(root.rglob("*.tf")):
        text = path.read_text()
        for tf_type, name, body in RESOURCE.findall(text):
            cfn_type = tf_to_cfn.get(tf_type)
            assert cfn_type, f"{tf_type} is not in create_only.json"
            tag = vendored.tag_attribute(cfn_type)
            if tag is None:
                continue
            line = re.search(rf"^  {tag[0]}\s+= (.*)$", body, re.M)
            if line and "var.tags" in line.group(1):
                merged += 1
            else:
                missing.append(f"{path.relative_to(root)}: {tf_type}.{name}")
        for name, body in MODULE.findall(text):
            if not re.search(r"^  tags\s+= var\.tags$", body, re.M):
                missing.append(f"{path.relative_to(root)}: module.{name}")
        if path.name == "variables.tf":
            assert 'variable "tags" {' in text, path
    assert merged, "no taggable awscc resource in the tree"
    assert not missing, missing
    # The root refuses AWS's reserved prefix at plan.
    assert re.search(
        r'^\s*condition\s+= alltrue\(\[for k in keys\(var\.tags\) : '
        r'!startswith\(lower\(k\), "aws:"\)\]\)$',
        (root / "variables.tf").read_text(),
        re.M,
    )


# ------------------------------------------------------- fragments
def _schema(tmp_path: Path, resources: dict) -> AwsccSchema:
    p = tmp_path / "schema.json"
    p.write_text(
        json.dumps(
            {
                "provider_schemas": {
                    "registry.terraform.io/hashicorp/awscc": {
                        "resource_schemas": resources
                    }
                }
            }
        )
    )
    return AwsccSchema(p)


KV = {
    "nested_type": {
        "nesting_mode": "list",
        "attributes": {
            "key": {"type": "string", "optional": True},
            "value": {"type": "string", "optional": True},
        },
    },
    "optional": True,
    "computed": True,
}


def _one(tmp_path, tags_spec, props=None, attr="tags"):
    schema = _schema(
        tmp_path,
        {
            "awscc_sqs_queue": {
                "block": {
                    "attributes": {
                        "queue_name": {"type": "string", "optional": True},
                        attr: tags_spec,
                    }
                }
            }
        },
    )
    t = {
        "Resources": {
            "Q": {"Type": "AWS::SQS::Queue", "Properties": props or {"QueueName": "q"}}
        }
    }
    return Translator(t, schema).render()


def test_a_resource_without_template_tags_takes_the_module_tags_or_null(tmp_path):
    main_tf = _one(tmp_path, KV)["main.tf"]
    assert (
        "  tags = length(var.tags) > 0 ? "
        "[for k, v in var.tags : { key = k, value = v }] : null\n"
    ) in main_tf


def test_template_tags_keep_their_keys_and_the_module_fills_the_rest(tmp_path):
    props = {
        "Tags": [
            {"Key": "Name", "Value": "q"},
            {"Fn::If": ["On", {"Key": "Extra", "Value": "1"}, {"Ref": "AWS::NoValue"}]},
        ]
    }
    t = {
        "Conditions": {"On": {"Fn::Equals": ["a", "a"]}},
        "Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": props}},
    }
    schema = _schema(
        tmp_path, {"awscc_sqs_queue": {"block": {"attributes": {"tags": KV}}}}
    )
    main_tf = Translator(t, schema).render()["main.tf"]
    assert (
        "tags = concat([for k, v in var.tags : { key = k, value = v } "
        'if !contains(["Name", "Extra"], k)], '
    ) in main_tf


def test_a_tag_key_that_is_not_literal_is_refused(tmp_path):
    with pytest.raises(TranslationError, match="literal Key"):
        _one(tmp_path, KV, {"Tags": [{"Key": {"Ref": "AWS::StackName"}, "Value": "v"}]})


def test_map_and_json_shapes(tmp_path):
    as_map = {"type": ["map", "string"], "optional": True}
    assert (
        "  tags = length(var.tags) > 0 ? var.tags : null\n"
        in _one(tmp_path, as_map)["main.tf"]
    )
    assert (
        "  tags = merge(var.tags, {"
        in _one(tmp_path, as_map, {"Tags": {"a": "1"}})["main.tf"]
    )
    as_json = {"type": "string", "optional": True}
    assert (
        "  tags = length(var.tags) > 0 ? jsonencode(var.tags) : null\n"
        in _one(tmp_path, as_json)["main.tf"]
    )
    own = _one(tmp_path, as_json, {"Tags": {"a": "1"}})["main.tf"]
    assert "  tags = jsonencode(merge(var.tags, jsondecode(" in own


def test_a_tag_property_not_named_tags_is_merged(tmp_path):
    out = _one(tmp_path, KV, attr="identity_pool_tags")["main.tf"]
    assert "  identity_pool_tags = length(var.tags) > 0 ? [for k, v" in out


def test_a_computed_only_tags_attribute_is_left_alone(tmp_path):
    out = _one(tmp_path, {**KV, "optional": False, "computed": True})["main.tf"]
    assert "var.tags" not in out


def test_a_tag_attribute_in_an_unknown_shape_fails_the_translation(tmp_path):
    with pytest.raises(TranslationError, match="cannot merge"):
        _one(tmp_path, {"type": ["list", "string"], "optional": True})


def test_create_only_tags_are_set_at_creation_and_kept(vendored):
    """OpenSearch Serverless collections take tags only at creation; a change
    to the module's tags must not replace the collection (and its index)."""
    t = {
        "Resources": {
            "C": {
                "Type": "AWS::OpenSearchServerless::Collection",
                "Properties": {"Name": "c", "Type": "VECTORSEARCH"},
            }
        }
    }
    assert translate.tags_are_create_only(
        "AWS::OpenSearchServerless::Collection", "tags"
    )
    assert not translate.tags_are_create_only("AWS::SQS::Queue", "tags")
    main_tf = Translator(t, vendored).render()["main.tf"]
    assert "ignore_changes = [tags]" in main_tf


def test_nested_modules_declare_and_receive_the_tags(vendored):
    child = {"Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": {}}}}
    parent = {
        "Resources": {
            "Child": {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {"TemplateURL": "child.yaml"},
            }
        }
    }
    tr = Translator(parent, vendored, nested_loader=lambda url: child)
    files = tr.render()
    assert "  tags = var.tags\n" in files["main.tf"]
    child_files = tr.children["child"].render()
    assert (
        'variable "tags" {\n  type = map(string)\n  default = {}'
        in child_files["variables.tf"]
    )
    assert "var.tags" in child_files["main.tf"]


def test_a_parameter_named_tags_does_not_take_the_variable(vendored):
    t = {
        "Parameters": {"Tags": {"Type": "String", "Default": ""}},
        "Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": {}}},
    }
    tr = Translator(t, vendored)
    assert tr.param_vars["Tags"] == "param_tags"


# ------------------------------------------------------- evaluated by Terraform
def _cached_terraform() -> Path | None:
    cache = Path(
        os.environ.get("TERRAFORM_CHECK_CACHE", "~/.cache/idp-terraform-check")
    ).expanduser()
    found = sorted(cache.glob("bin/terraform/*/*/terraform"))
    return found[-1] if found else None


def _expression(main_tf: str, attr: str = "tags") -> str:
    m = re.search(rf"^  {attr} = (.*?)\n(?=  [a-z_]+ = |\}})", main_tf, re.M | re.S)
    assert m, main_tf
    return m.group(1)


def test_the_merge_evaluates_to_the_unmerged_value_when_there_are_no_tags(
    tmp_path, vendored
):
    """With ``var.tags`` empty each generated expression has the value the
    attribute had before the merge existed (absent, or the template's tags), so
    a deployment that passes no tags plans no change. With tags, a template
    key keeps the template's value. Evaluated by the cached Terraform binary
    ``make terraform-check`` leaves; skipped without it."""
    exe = _cached_terraform()
    if exe is None:
        pytest.skip("no cached terraform binary; run `make terraform-check`")
    cases = {
        # (cfn type, props) -> value before the merge, and with tags {Name, Team}
        "kv_none": ("AWS::SQS::Queue", {}, None),
        "kv_own": (
            "AWS::SQS::Queue",
            {"Tags": [{"Key": "Name", "Value": "q"}]},
            [{"key": "Name", "value": "q"}],
        ),
        "map_none": ("AWS::SSM::Parameter", {"Type": "String", "Value": "v"}, None),
        "map_own": (
            "AWS::SSM::Parameter",
            {"Type": "String", "Value": "v", "Tags": {"Name": "p"}},
            {"Name": "p"},
        ),
        "json_none": ("AWS::Glue::Crawler", {"Role": "r", "Targets": {}}, None),
    }
    locals_ = []
    for label, (cfn_type, props, _) in cases.items():
        t = {"Resources": {"R": {"Type": cfn_type, "Properties": props}}}
        main_tf = Translator(t, vendored).render()["main.tf"]
        locals_.append(f"  {label} = {_expression(main_tf)}")
    (tmp_path / "main.tf").write_text(
        'variable "tags" {\n  type = map(string)\n}\n'
        "locals {\n" + "\n".join(locals_) + "\n}\n"
    )
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
    env.pop("TF_CLI_ARGS", None)
    subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(exe), "init", "-backend=false", "-input=false", "-no-color"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=True,
    )

    def evaluate(tags: dict) -> dict:
        query = "jsonencode({" + ", ".join(f"{k} = local.{k}" for k in cases) + "})\n"
        run = subprocess.run(  # nosec B603 - fixed argv, no shell
            [str(exe), "console", "-var", f"tags={json.dumps(tags)}"],
            cwd=tmp_path,
            env=env,
            input=query,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(json.loads(run.stdout.strip()))

    empty = evaluate({})
    assert empty == {k: before for k, (_, _, before) in cases.items()}
    tagged = evaluate({"Name": "module", "Team": "t"})
    assert tagged["kv_none"] == [
        {"key": "Name", "value": "module"},
        {"key": "Team", "value": "t"},
    ]
    assert tagged["kv_own"] == [
        {"key": "Team", "value": "t"},
        {"key": "Name", "value": "q"},
    ]
    assert tagged["map_own"] == {"Name": "p", "Team": "t"}
    assert json.loads(tagged["json_none"]) == {"Name": "module", "Team": "t"}
