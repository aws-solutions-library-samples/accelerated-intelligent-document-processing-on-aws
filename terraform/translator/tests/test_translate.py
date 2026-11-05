"""Offline tests for the CloudFormation -> Terraform translator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from idp_tf.awscc_schema import AwsccSchema
from idp_tf.translate import TranslationError, Translator

SCHEMA = {
    "format_version": "1.0",
    "provider_schemas": {
        "registry.terraform.io/hashicorp/awscc": {
            "resource_schemas": {
                "awscc_sqs_queue": {
                    "block": {
                        "attributes": {
                            "queue_name": {"type": "string", "optional": True},
                            "visibility_timeout": {"type": "number", "optional": True},
                            "redrive_policy": {"type": "string", "optional": True},
                            "arn": {"type": "string", "computed": True},
                            "id": {"type": "string", "computed": True},
                            "tags": {
                                "nested_type": {
                                    "nesting_mode": "set",
                                    "attributes": {
                                        "key": {"type": "string", "required": True},
                                        "value": {"type": "string", "required": True},
                                    },
                                },
                                "optional": True,
                            },
                        }
                    }
                }
            }
        }
    },
}


@pytest.fixture
def schema(tmp_path: Path) -> AwsccSchema:
    p = tmp_path / "schema.json"
    p.write_text(json.dumps(SCHEMA))
    return AwsccSchema(p)


def render(template: dict, schema: AwsccSchema) -> dict[str, str]:
    return Translator(template, schema).render()


def queue(**props) -> dict:
    return {"Resources": {"Q": {"Type": "AWS::SQS::Queue", "Properties": props}}}


def test_awscc_attributes_are_matched_exactly(schema):
    out = render(queue(QueueName="q", VisibilityTimeout=30), schema)["main.tf"]
    assert 'resource "awscc_sqs_queue" "q"' in out
    assert 'queue_name = "q"' in out
    assert "visibility_timeout = 30" in out


def test_unknown_property_fails_instead_of_being_dropped(schema):
    with pytest.raises(TranslationError, match="NoSuchProperty"):
        render(queue(NoSuchProperty=1), schema)


def test_unknown_type_fails(schema):
    t = {"Resources": {"X": {"Type": "AWS::Nope::Thing", "Properties": {}}}}
    with pytest.raises(TranslationError, match="AWS::Nope::Thing"):
        render(t, schema)


def test_json_valued_string_attribute_is_jsonencoded(schema):
    out = render(queue(RedrivePolicy={"maxReceiveCount": 3}), schema)["main.tf"]
    assert "redrive_policy = jsonencode(" in out
    assert '"maxReceiveCount" = 3' in out


def test_sub_escapes_literal_interpolation_and_resolves_names(schema):
    t = queue(QueueName={"Fn::Sub": "${AWS::StackName}-${!Literal}-%{x}"})
    out = render(t, schema)["main.tf"]
    assert 'queue_name = "${var.stack_name}-$${Literal}-%%{x}"' in out


def test_condition_becomes_count_and_references_use_one(schema):
    t = {
        "Parameters": {"Flag": {"Type": "String", "Default": ""}},
        "Conditions": {"On": {"Fn::Not": [{"Fn::Equals": [{"Ref": "Flag"}, ""]}]}},
        "Resources": {
            "Q": {"Type": "AWS::SQS::Queue", "Condition": "On", "Properties": {}},
        },
        "Outputs": {"Arn": {"Value": {"Fn::GetAtt": ["Q", "Arn"]}}},
    }
    files = render(t, schema)
    assert "count = local.c_on ? 1 : 0" in files["main.tf"]
    assert "one(awscc_sqs_queue.q[*].arn)" in files["outputs.tf"]
    assert "c_on = !(" in files["locals.tf"]


def test_novalue_list_items_are_removed_with_concat(schema):
    t = {
        "Parameters": {"Flag": {"Type": "String", "Default": ""}},
        "Conditions": {"On": {"Fn::Equals": [{"Ref": "Flag"}, "x"]}},
        "Resources": {
            "Q": {
                "Type": "AWS::SQS::Queue",
                "Properties": {
                    "Tags": [
                        {"Key": "a", "Value": "1"},
                        {
                            "Fn::If": [
                                "On",
                                {"Key": "b", "Value": "2"},
                                {"Ref": "AWS::NoValue"},
                            ]
                        },
                    ]
                },
            }
        },
    }
    out = render(t, schema)["main.tf"]
    assert "concat(" in out
    assert "(local.c_on ? [" in out and ": [])" in out


def test_novalue_map_entries_use_merge(schema):
    t = {
        "Conditions": {"On": {"Fn::Equals": ["a", "a"]}},
        "Resources": {
            "Q": {
                "Type": "AWS::SQS::Queue",
                "Properties": {
                    "RedrivePolicy": {
                        "a": 1,
                        "b": {"Fn::If": ["On", 2, {"Ref": "AWS::NoValue"}]},
                    }
                },
            }
        },
    }
    out = render(t, schema)["main.tf"]
    assert "jsonencode(merge(" in out


def test_parameter_named_stack_name_does_not_collide(schema):
    t = queue(QueueName={"Ref": "StackName"})
    t["Parameters"] = {"StackName": {"Type": "String"}}
    files = render(t, schema)
    assert 'variable "param_stack_name"' in files["variables.tf"]
    assert 'variable "stack_name"' in files["variables.tf"]
    assert "queue_name = var.param_stack_name" in files["main.tf"]


def test_custom_resource_goes_through_the_bridge(schema):
    t = {
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Role": "arn:x",
                    "Code": {"S3Bucket": "b", "S3Key": "k"},
                    "Handler": "index.handler",
                    "Runtime": "python3.12",
                },
            },
            "C": {
                "Type": "Custom::Thing",
                "Properties": {"ServiceToken": {"Fn::GetAtt": ["Fn", "Arn"]}, "X": 1},
            },
        },
        "Outputs": {"V": {"Value": {"Fn::GetAtt": ["C", "Value"]}}},
    }
    tr = Translator(t, schema)
    files = tr.render()
    assert 'resource "aws_lambda_invocation" "c"' in files["main.tf"]
    assert 'lifecycle_scope = "CRUD"' in files["main.tf"]
    assert (
        "jsondecode(aws_lambda_invocation.c.result).Data.Value" in files["outputs.tf"]
    )
    assert tr.custom_tokens == ["aws_lambda_function.fn.arn"]


def test_hand_mapped_type_rejects_unmapped_property(schema):
    t = {
        "Resources": {
            "L": {
                "Type": "AWS::Logs::LogGroup",
                "Properties": {"RetentionInDays": 7, "DataProtectionPolicy": {}},
            }
        }
    }
    with pytest.raises(TranslationError, match="DataProtectionPolicy"):
        render(t, schema)


def test_iam_role_splits_policies_and_dependents_wait_for_them(schema):
    t = {
        "Resources": {
            "R": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "AssumeRolePolicyDocument": {"Version": "2012-10-17"},
                    "Policies": [{"PolicyName": "p", "PolicyDocument": {"a": 1}}],
                    "ManagedPolicyArns": ["arn:m"],
                },
            },
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Role": {"Fn::GetAtt": ["R", "Arn"]},
                    "Code": {"S3Bucket": "b", "S3Key": "k"},
                },
            },
        }
    }
    out = render(t, schema)["main.tf"]
    assert 'resource "aws_iam_role_policy" "r_0"' in out
    assert 'resource "aws_iam_role_policy_attachment" "r"' in out
    fn = out[out.index('resource "aws_lambda_function"') :]
    assert "aws_iam_role_policy.r_0" in fn
    assert "aws_iam_role_policy_attachment.r" in fn
    # A function name is required by the provider and absent here.
    assert "function_name = " in fn


def lambda_fn(**extra) -> dict:
    props = {"Role": "arn:x", "Code": {"S3Bucket": "b", "S3Key": "k"}}
    props.update(extra)
    return {"Type": "AWS::Lambda::Function", "Properties": props}


def test_nested_if_block_becomes_mutually_exclusive_dynamic_blocks(schema):
    vpc = {
        "Fn::If": [
            "A",
            {"SubnetIds": ["s1"], "SecurityGroupIds": ["g"]},
            {
                "Fn::If": [
                    "B",
                    {"SubnetIds": ["s2"], "SecurityGroupIds": ["g"]},
                    {"Ref": "AWS::NoValue"},
                ]
            },
        ]
    }
    t = {
        "Conditions": {
            "A": {"Fn::Equals": ["1", "1"]},
            "B": {"Fn::Equals": ["1", "2"]},
        },
        "Resources": {"F": lambda_fn(VpcConfig=vpc)},
    }
    out = render(t, schema)["main.tf"]
    assert out.count('dynamic "vpc_config"') == 2
    assert "for_each = (local.c_a) ? [1] : []" in out
    assert "for_each = (!local.c_a && local.c_b) ? [1] : []" in out


def test_inline_zipfile_becomes_archive_file(schema):
    t = {"Resources": {"F": lambda_fn(Runtime="python3.12", Code={"ZipFile": "x=1"})}}
    out = render(t, schema)["main.tf"]
    assert 'data "archive_file" "f_zipfile"' in out
    assert 'source_content_filename = "index.py"' in out
    assert "filename = data.archive_file.f_zipfile.output_path" in out


def test_json_text_literal_is_minified_in_source_order(schema):
    t = queue(RedrivePolicy='{\n  "z": 1,\n  "a": 2\n}')
    out = render(t, schema)["main.tf"]
    assert 'redrive_policy = "{\\"z\\":1,\\"a\\":2}"' in out


def test_queue_policy_becomes_one_resource_per_queue(schema):
    t = {
        "Resources": {
            "P": {
                "Type": "AWS::SQS::QueuePolicy",
                "Properties": {"Queues": ["u1", "u2"], "PolicyDocument": {"a": 1}},
            }
        }
    }
    out = render(t, schema)["main.tf"]
    assert 'resource "aws_sqs_queue_policy" "p_queues_0"' in out
    assert 'resource "aws_sqs_queue_policy" "p_queues_1"' in out


def test_survey_mode_collects_every_failure(schema):
    t = {
        "Resources": {
            "X": {"Type": "AWS::Nope::One", "Properties": {}},
            "Y": {"Type": "AWS::Nope::Two", "Properties": {}},
        }
    }
    tr = Translator(t, schema, survey=True)
    tr.render()
    assert len(tr.errors) == 2
