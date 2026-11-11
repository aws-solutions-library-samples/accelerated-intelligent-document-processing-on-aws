"""Upgrade safety and completeness: the stateful address list, create-only
properties fed by custom resources, ``moved`` blocks, and every property value
reaching the Terraform it became."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from idp_tf import completeness, moved, safety, translate
from idp_tf.awscc_schema import AwsccSchema
from idp_tf.cli import VENDORED_SCHEMA, main, render_tree, tree_checks, tree_resources
from idp_tf.translate import Translator

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def schema() -> AwsccSchema:
    return AwsccSchema(VENDORED_SCHEMA)


def translated(template: dict, schema: AwsccSchema) -> Translator:
    tr = Translator(template, schema)
    tr.render()
    render_tree(tr)
    return tr


FN = {
    "Type": "AWS::Lambda::Function",
    "Properties": {
        "Role": "arn:x",
        "Code": {"S3Bucket": "b", "S3Key": "k"},
        "Handler": "index.handler",
        "Runtime": "python3.12",
    },
}


def kb_fragment() -> dict:
    """The shape measured live: a custom resource creates the knowledge base, an
    SSM parameter passes its id on, and the data source takes it as the
    create-only KnowledgeBaseId."""
    return {
        "Resources": {
            "Fn": FN,
            "KbCustom": {
                "Type": "Custom::S3VectorsKnowledgeBase",
                "Properties": {
                    "ServiceToken": {"Fn::GetAtt": ["Fn", "Arn"]},
                    "RoleArn": "arn:role",
                },
            },
            "KnowledgeBase": {
                "Type": "AWS::SSM::Parameter",
                "Properties": {
                    "Type": "String",
                    "Value": {"Fn::GetAtt": ["KbCustom", "KnowledgeBaseId"]},
                },
            },
            "S3DataSource": {
                "Type": "AWS::Bedrock::DataSource",
                "Properties": {
                    "Name": "ds",
                    "KnowledgeBaseId": {"Fn::GetAtt": ["KnowledgeBase", "Value"]},
                    "DataSourceConfiguration": {
                        "Type": "S3",
                        "S3Configuration": {"BucketArn": "arn:aws:s3:::b"},
                    },
                },
            },
        }
    }


# ------------------------------------------------------------------ A: stateful
def test_stateful_addresses_are_computed_from_the_type_list(schema):
    t = {
        "Resources": {
            "B": {"Type": "AWS::S3::Bucket", "Properties": {}},
            "Q": {"Type": "AWS::SQS::Queue", "Properties": {}},
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "Condition": "C",
                "Properties": {
                    "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
                    "AttributeDefinitions": [
                        {"AttributeName": "k", "AttributeType": "S"}
                    ],
                },
            },
        },
        "Conditions": {"C": {"Fn::Equals": ["a", "a"]}},
    }
    tr = translated(t, schema)
    assert safety.stateful_addresses(tr) == [
        "aws_dynamodb_table.t",
        "aws_s3_bucket.b",
    ]


def test_the_stateful_and_not_stateful_lists_do_not_overlap():
    assert not set(safety.STATEFUL_TYPES) & set(safety.NOT_STATEFUL)


def test_a_parameter_with_a_second_writer_is_stateful(schema):
    t = {
        "Resources": {
            "SettingsParameter": {
                "Type": "AWS::SSM::Parameter",
                "Properties": {"Type": "String", "Value": "{}"},
            },
            "Other": {
                "Type": "AWS::SSM::Parameter",
                "Properties": {"Type": "String", "Value": "x"},
            },
        }
    }
    tr = translated(t, schema)
    assert safety.stateful_addresses(tr) == ["awscc_ssm_parameter.settings_parameter"]


# --------------------------------------------- B: create-only from custom output
def test_a_custom_output_reaching_a_stateful_create_only_property_fails(
    schema, monkeypatch
):
    monkeypatch.setattr(translate, "STABLE_CUSTOM_OUTPUTS", {})
    tr = translated(kb_fragment(), schema)
    errors, _, edges = tree_checks(tr)
    (edge,) = [e for e in edges if e.logical_id == "S3DataSource"]
    assert edge.prop == "KnowledgeBaseId"
    assert edge.sources == ("KbCustom",)
    assert edge.stateful and not edge.handled
    assert any("STABLE_CUSTOM_OUTPUTS" in e and "S3DataSource" in e for e in errors)


def test_a_stable_output_is_ignored_and_replaced_only_by_its_other_inputs(
    schema,
):
    tr = translated(kb_fragment(), schema)
    errors, stale, edges = tree_checks(tr)
    assert not errors, errors
    assert [e.handled for e in edges if e.logical_id == "S3DataSource"] == [True]
    main_tf = tr.emitted["S3DataSource"]
    assert "ignore_changes = [knowledge_base_id]" in main_tf
    assert (
        "replace_triggered_by = [terraform_data.s3_data_source_knowledge_base_id_key]"
        in main_tf
    )
    key = main_tf[: main_tf.index('resource "awscc_bedrock_data_source"')]
    # The key reads the custom resource's name, not its unknown output, and
    # goes through the SSM parameter to the value it carries.
    assert 'input = "custom-resource:KbCustom.KnowledgeBaseId"' in key
    assert "aws_lambda_invocation" not in key and "awscc_ssm_parameter" not in key


def test_a_not_stateful_edge_is_reported_without_failing(schema):
    t = {
        "Resources": {
            "Fn": FN,
            "Lower": {
                "Type": "Custom::GetLowercase",
                "Properties": {
                    "ServiceToken": {"Fn::GetAtt": ["Fn", "Arn"]},
                    "InputString": "ABC",
                },
            },
            "Group": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {"LogGroupName": {"Fn::GetAtt": ["Lower", "Out"]}},
            },
        }
    }
    tr = translated(t, schema)
    errors, _, edges = tree_checks(tr)
    assert not errors, errors
    assert [(e.logical_id, e.prop, e.stateful) for e in edges] == [
        ("Group", "LogGroupName", False)
    ]


def test_a_value_that_is_not_create_only_is_no_edge(schema):
    t = kb_fragment()
    ds = t["Resources"]["S3DataSource"]["Properties"]
    ds["KnowledgeBaseId"] = "fixed"
    ds["Description"] = {"Fn::GetAtt": ["KbCustom", "KnowledgeBaseId"]}
    tr = translated(t, schema)
    assert [e for e in safety.custom_output_edges(tr)] == []


# ------------------------------------------------------- whole standard template
@pytest.fixture(scope="module")
def standard(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("std") / "m"
    args = [
        str(REPO / "template.yaml"),
        "--offline",
        "--out",
        str(out),
        "--label-template",
        str(REPO / "template.yaml"),
        "--version",
        "0.0.0",
    ]
    assert main(args) == 0
    return out


def test_the_manifest_lists_stateful_addresses_beyond_the_retained_ones(standard):
    meta = json.loads((standard / "idp-terraform.json").read_text())
    stateful = set(meta["statefulAddresses"])
    assert "aws_cognito_user_pool.user_pool" in stateful
    assert "aws_dynamodb_table.tracking_table" in stateful
    # The resource the live upgrade replaced; no DeletionPolicy retains it.
    ds = "module.documentkb.awscc_bedrock_data_source.s3_data_source"
    assert ds in stateful and ds not in meta["retainedAddresses"]


def test_every_stateful_edge_in_the_standard_template_is_handled(standard):
    meta = json.loads((standard / "idp-terraform.json").read_text())
    edges = meta["customOutputEdges"]
    assert edges, "no custom-output edge left; drop the STABLE_CUSTOM_OUTPUTS rows"
    assert all(e["handled"] for e in edges if e["stateful"]), edges
    # Propagation crosses module boundaries: a root resource reads a nested
    # stack's output that is a custom resource's.
    assert any("PATTERNSTACK:BDAOCRProject" in e["sources"] for e in edges)
    text = (standard / "modules" / "documentkb" / "main.tf").read_text()
    assert "ignore_changes = [knowledge_base_id]" in text


def test_no_declaration_is_stale_for_the_standard_template(standard):
    """Each declaration names something in the standard template; a variant may
    remove what one describes, so only this template's run is held to it."""
    from idp_tf.cli import local_loader
    from idp_tf.expand import expand, load_template, offline_placeholders

    t = expand(offline_placeholders(load_template(REPO / "template.yaml")))
    tr = Translator(t, AwsccSchema(VENDORED_SCHEMA), nested_loader=local_loader(REPO))
    tr.directory_readers = True
    tr.render()
    render_tree(tr)
    errors, stale, _ = tree_checks(tr)
    assert not errors and not stale, (errors, stale)


def test_the_vendored_create_only_list_matches_cfn_lint_when_installed():
    pytest.importorskip("cfnlint")
    from cfnlint.schema import PROVIDER_SCHEMA_MANAGER

    data = json.loads((VENDORED_SCHEMA.parent / "create_only.json").read_text())
    differing = []
    for cfn_type, paths in data["types"].items():
        schema = PROVIDER_SCHEMA_MANAGER.get_resource_schema("us-east-1", cfn_type)
        live = sorted(
            p.removeprefix("/properties/")
            for p in schema.schema.get("createOnlyProperties", [])
        )
        if live != paths:
            differing.append(cfn_type)
    if differing:
        pytest.skip(
            f"installed cfn-lint differs for {differing}; the vendored list records "
            f"{data['source']}. Run scripts/refresh_create_only.py with the pinned one"
        )


# ------------------------------------------------------------------ C: moved
def entry(stack, lid, cfn_type, addresses):
    return {
        "stack": stack,
        "logicalId": lid,
        "cfnType": cfn_type,
        "address": addresses[0],
        "addresses": addresses,
    }


BUCKET_OLD = entry(
    "PATTERNSTACK",
    "InputBucket",
    "AWS::S3::Bucket",
    [
        "module.patternstack.aws_s3_bucket.input_bucket",
        "module.patternstack.aws_s3_bucket_versioning.input_bucket_versioning",
    ],
)
BUCKET_NEW = entry(
    "",
    "InputBucket",
    "AWS::S3::Bucket",
    ["aws_s3_bucket.input_bucket", "aws_s3_bucket_versioning.input_bucket_versioning"],
)


def test_a_move_between_nested_stacks_is_inferred_with_its_companions():
    result = moved.plan_moves({"resources": [BUCKET_OLD]}, [BUCKET_NEW], {})
    assert result.moves == [
        (
            "module.patternstack.aws_s3_bucket.input_bucket",
            "aws_s3_bucket.input_bucket",
        ),
        (
            "module.patternstack.aws_s3_bucket_versioning.input_bucket_versioning",
            "aws_s3_bucket_versioning.input_bucket_versioning",
        ),
    ]


def test_an_ambiguous_move_is_not_inferred():
    other = entry(
        "OTHER", "InputBucket", "AWS::S3::Bucket", ["module.other.x.input_bucket"]
    )
    result = moved.plan_moves({"resources": [BUCKET_OLD]}, [BUCKET_NEW, other], {})
    assert result.moves == []


def test_a_declared_rename_moves_the_resource_and_renames_its_companions():
    old = entry(
        "",
        "Tracking",
        "AWS::DynamoDB::Table",
        ["aws_dynamodb_table.tracking", "time_static.tracking_name_suffix"],
    )
    new = entry(
        "",
        "TrackingTable",
        "AWS::DynamoDB::Table",
        ["aws_dynamodb_table.tracking_table", "time_static.tracking_table_name_suffix"],
    )
    renames = {"Tracking": {"from": "Tracking", "to": "TrackingTable", "reason": "r"}}
    result = moved.plan_moves({"resources": [old]}, [new], renames)
    assert result.moves == [
        ("aws_dynamodb_table.tracking", "aws_dynamodb_table.tracking_table"),
        ("time_static.tracking_name_suffix", "time_static.tracking_table_name_suffix"),
    ]
    # Without the table it is a delete and a create, which the plan guard refuses.
    assert moved.plan_moves({"resources": [old]}, [new], {}).moves == []


def test_a_renamed_nested_stack_moves_as_one_module():
    old = [
        entry("KB", "KB", "AWS::CloudFormation::Stack", ["module.kb"]),
        entry("KB", "T", "AWS::DynamoDB::Table", ["module.kb.aws_dynamodb_table.t"]),
    ]
    old[0]["stack"] = ""
    new = [
        entry("", "DOCUMENTKB", "AWS::CloudFormation::Stack", ["module.documentkb"]),
        entry(
            "DOCUMENTKB",
            "T",
            "AWS::DynamoDB::Table",
            ["module.documentkb.aws_dynamodb_table.t"],
        ),
    ]
    renames = {"KB": {"from": "KB", "to": "DOCUMENTKB", "reason": "r"}}
    result = moved.plan_moves({"resources": old}, new, renames)
    assert result.moves == [("module.kb", "module.documentkb")]


def test_a_type_change_is_reported_not_moved():
    new = dict(BUCKET_NEW, cfnType="AWS::S3Express::DirectoryBucket")
    result = moved.plan_moves({"resources": [BUCKET_NEW]}, [new], {})
    assert result.moves == [] and "different type" in result.warnings[0]


def test_a_rename_naming_nothing_fails():
    renames = {"A": {"from": "A", "to": "Nowhere", "reason": "r"}}
    result = moved.plan_moves({}, [BUCKET_NEW], renames)
    assert "has no Nowhere" in result.errors[0]


def test_earlier_moves_are_kept_and_one_whose_source_exists_again_is_dropped():
    previous = {
        "moves": [
            {"from": "aws_x.a", "to": "aws_x.b"},
            {"from": "aws_y.c", "to": "aws_y.d"},
        ]
    }
    live = {"aws_x.b", "aws_y.c", "aws_y.d"}
    kept, warnings = moved.carry_history(previous, [("aws_x.b0", "aws_x.b")], live)
    assert kept == [("aws_x.a", "aws_x.b"), ("aws_x.b0", "aws_x.b")]
    assert "aws_y.c exists again" in warnings[0]


def test_a_chain_of_moves_is_followed_to_a_live_address():
    previous = {"moves": [{"from": "aws_x.a", "to": "aws_x.b"}]}
    kept, _ = moved.carry_history(previous, [("aws_x.b", "aws_x.c")], {"aws_x.c"})
    assert kept == [("aws_x.a", "aws_x.b"), ("aws_x.b", "aws_x.c")]


def test_the_committed_rename_table_loads():
    assert isinstance(moved.load_renames(), dict)


def _cached_terraform():
    cache = Path(
        os.environ.get("TERRAFORM_CHECK_CACHE", "~/.cache/idp-terraform-check")
    ).expanduser()
    found = sorted(cache.glob("bin/terraform/*/*/terraform"))
    plugins = cache / "plugins"
    if not found or not (plugins / "registry.terraform.io").is_dir():
        return None
    return found[-1], plugins


def _validate(root: Path) -> dict:
    cached = _cached_terraform()
    if cached is None:
        pytest.skip(
            "no cached terraform binary and providers; run `make terraform-check`"
        )
    exe, plugins = cached
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_tf_gate_for_moved", REPO / "scripts" / "terraform_check.py"
    )
    assert spec and spec.loader
    gate = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gate
    spec.loader.exec_module(gate)
    (root / gate.OVERRIDE_FILE).write_text(gate.override_tf(gate.gate_providers([])))
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "CHECKPOINT_DISABLE": "1"}
    env.pop("TF_CLI_ARGS", None)
    init = subprocess.run(  # nosec B603 - fixed argv, no shell
        [
            str(exe),
            "init",
            "-backend=false",
            "-input=false",
            "-no-color",
            f"-plugin-dir={plugins}",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        pytest.skip(f"offline init failed (provider cache incomplete?): {init.stderr}")
    out = subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(exe), "validate", "-json", "-no-color"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(out.stdout)


def test_moves_from_a_previous_release_are_written_and_validate(standard, tmp_path):
    """A nested-stack move (inferred) and a logical-id rename (declared) against
    the real standard module, then ``terraform validate`` on the result."""
    meta = json.loads((standard / "idp-terraform.json").read_text())
    previous = {"resources": [], "moves": []}
    for e in meta["resources"]:
        e = json.loads(json.dumps(e))
        if e["logicalId"] == "InputBucket" and e["stack"] == "":
            # Last release, the bucket was in the pattern stack.
            e["stack"] = "PATTERNSTACK"
            e["addresses"] = [f"module.patternstack.{a}" for a in e["addresses"]]
            e["address"] = f"module.patternstack.{e['address']}"
        elif e["logicalId"] == "TrackingTable" and e["stack"] == "":
            # Last release, the table was called Tracking.
            e["logicalId"] = "Tracking"
            e["addresses"] = [
                a.replace("tracking_table", "tracking") for a in e["addresses"]
            ]
            e["address"] = e["address"].replace("tracking_table", "tracking")
        previous["resources"].append(e)
    prev_path = tmp_path / "previous.json"
    prev_path.write_text(json.dumps(previous))
    renames = tmp_path / "renames.yaml"
    renames.write_text(
        "renames:\n  - from: Tracking\n    to: TrackingTable\n    reason: test\n"
    )
    out = tmp_path / "m"
    args = [
        str(REPO / "template.yaml"),
        "--offline",
        "--out",
        str(out),
        "--previous-manifest",
        str(prev_path),
        "--renames",
        str(renames),
    ]
    assert main(args) == 0
    text = (out / "moved.tf").read_text()
    assert (
        "from = module.patternstack.aws_s3_bucket.input_bucket\n"
        "  to   = aws_s3_bucket.input_bucket" in text
    )
    assert (
        "from = aws_dynamodb_table.tracking\n  to   = aws_dynamodb_table.tracking_table"
        in text
    )
    new_meta = json.loads((out / "idp-terraform.json").read_text())
    assert {
        "from": "aws_dynamodb_table.tracking",
        "to": "aws_dynamodb_table.tracking_table",
    } in new_meta["moves"]
    report = _validate(out)
    errors = [d for d in report["diagnostics"] if d["severity"] == "error"]
    assert report["valid"] and not errors, errors[:5]


# --------------------------------------------------------------- D: completeness
def test_a_value_a_mapper_drops_is_reported(schema):
    """Simulates a mapper that reads a property and writes nothing for one of
    its values: the shape of a guardrail wired to an empty string."""
    t = {
        "Parameters": {"GuardrailId": {"Type": "String", "Default": "gr-123"}},
        "Resources": {
            "L": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {
                    "RetentionInDays": 30,
                    "LogGroupName": {"Fn::Sub": "/idp/${GuardrailId}/logs"},
                },
            }
        },
    }
    tr = translated(t, schema)
    missing, _ = completeness.check(tr)
    assert missing == []
    tr.emitted["L"] = (
        tr.emitted["L"].replace("var.guardrail_id", '""').replace("30", "null")
    )
    missing, _ = completeness.check(tr)
    assert sorted(m.value for m in missing) == ["30", "Ref GuardrailId"]


def test_a_parameter_a_module_call_drops_is_reported(schema):
    child = {
        "Parameters": {"GuardrailId": {"Type": "String", "Default": ""}},
        "Resources": {"W": {"Type": "AWS::CloudFormation::WaitConditionHandle"}},
    }
    t = {
        "Parameters": {"GuardrailId": {"Type": "String", "Default": "gr-123"}},
        "Resources": {
            "Pattern": {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {
                    "TemplateURL": "./p/.aws-sam/packaged.yaml",
                    "Parameters": {"GuardrailId": {"Ref": "GuardrailId"}},
                },
            }
        },
    }
    tr = Translator(t, schema, nested_loader=lambda url: child)
    tr.render()
    render_tree(tr)
    assert completeness.check(tr)[0] == []
    tr.emitted["Pattern"] = tr.emitted["Pattern"].replace(
        "guardrail_id = var.guardrail_id", 'guardrail_id = ""'
    )
    (m,) = completeness.check(tr)[0]
    assert (m.logical_id, m.path, m.value) == (
        "Pattern",
        "Parameters/GuardrailId",
        "Ref GuardrailId",
    )


def test_a_dynamodb_sub_property_the_mapper_does_not_read_fails(schema):
    t = {
        "Resources": {
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
                    "AttributeDefinitions": [
                        {"AttributeName": "k", "AttributeType": "S"}
                    ],
                    "TimeToLiveSpecification": {
                        "AttributeName": "ttl",
                        "Enabled": True,
                        "Unknown": 1,
                    },
                },
            }
        }
    }
    with pytest.raises(translate.TranslationError, match="Unknown"):
        Translator(t, schema).render()


def test_point_in_time_recovery_period_is_carried(schema):
    t = {
        "Resources": {
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
                    "AttributeDefinitions": [
                        {"AttributeName": "k", "AttributeType": "S"}
                    ],
                    "PointInTimeRecoverySpecification": {
                        "PointInTimeRecoveryEnabled": True,
                        "RecoveryPeriodInDays": 7,
                    },
                },
            }
        }
    }
    tr = translated(t, schema)
    assert "recovery_period_in_days = 7" in tr.emitted["T"]
    assert completeness.check(tr)[0] == []


def test_the_manifest_records_every_address_a_resource_became(standard):
    meta = json.loads((standard / "idp-terraform.json").read_text())
    by_id = {(e["stack"], e["logicalId"]): e for e in meta["resources"]}
    bucket = by_id[("", "InputBucket")]
    assert bucket["address"] == "aws_s3_bucket.input_bucket"
    assert "aws_s3_bucket_versioning.input_bucket_versioning" in bucket["addresses"]
    assert by_id[("", "DOCUMENTKB")]["addresses"] == ["module.documentkb"]
    assert ("DOCUMENTKB", "S3DataSource") in by_id
    _ = tree_resources  # the function the manifest is built with


def test_a_hand_mapper_force_new_path_counts_as_create_only(schema, monkeypatch):
    """The registry lists only TableName for a table; the provider also replaces
    it for a key-schema change, which the mapper declares in force_new."""
    t = {
        "Resources": {
            "Fn": FN,
            "Name": {
                "Type": "Custom::GetLowercase",
                "Properties": {"ServiceToken": {"Fn::GetAtt": ["Fn", "Arn"]}},
            },
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "KeySchema": [
                        {
                            "AttributeName": {"Fn::GetAtt": ["Name", "Key"]},
                            "KeyType": "HASH",
                        }
                    ],
                    "AttributeDefinitions": [
                        {"AttributeName": "k", "AttributeType": "S"}
                    ],
                },
            },
        }
    }
    tr = translated(t, schema)
    (edge,) = safety.custom_output_edges(tr)
    assert (edge.logical_id, edge.prop, edge.stateful) == (
        "T",
        "KeySchema/AttributeName",
        True,
    )
    from idp_tf import aws_mappers_storage

    monkeypatch.setattr(aws_mappers_storage.DynamoDbTableMapper, "force_new", ())
    assert safety.custom_output_edges(translated(t, schema)) == []
