"""Offline tests for the CloudFormation -> Terraform translator."""

from __future__ import annotations

import json
import re
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
    # Dependants wait on the role's propagation wait, which itself waits on
    # every policy and attachment of the role.
    assert "time_sleep.r_iam" in fn
    sleep = out[out.index('resource "time_sleep" "r_iam"') :]
    sleep = sleep[: sleep.index("\n}")]
    assert "aws_iam_role_policy.r_0" in sleep
    assert "aws_iam_role_policy_attachment.r" in sleep
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


def test_inline_zipfile_becomes_a_deterministic_zip_shipped_in_the_module(schema):
    import io
    import zipfile

    t = {"Resources": {"F": lambda_fn(Runtime="python3.12", Code={"ZipFile": "x=1"})}}
    tr = Translator(t, schema)
    out = tr.render()["main.tf"]
    assert 'filename = "${path.module}/zips/f_zipfile.zip"' in out
    assert 'filebase64sha256("${path.module}/zips/f_zipfile.zip")' in out
    first = tr.assets["zips/f_zipfile.zip"]
    again = Translator(t, schema)
    again.render()
    assert again.assets["zips/f_zipfile.zip"] == first  # byte-identical
    assert zipfile.ZipFile(io.BytesIO(first)).read("index.py") == b"x=1"


def test_inline_code_importing_cfnresponse_gets_the_stand_in(schema):
    import io
    import zipfile

    code = {"ZipFile": "import cfnresponse\ndef handler(e, c): pass"}
    t = {"Resources": {"F": lambda_fn(Runtime="python3.12", Code=code)}}
    tr = Translator(t, schema)
    tr.render()
    names = zipfile.ZipFile(io.BytesIO(tr.assets["zips/f_zipfile.zip"])).namelist()
    assert sorted(names) == ["cfnresponse.py", "index.py"]


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


def test_variable_names_keep_mixed_case_acronyms_whole():
    from idp_tf.translate import variable_name

    assert variable_name("WAFAllowedIPv4Ranges") == "waf_allowed_ipv4_ranges"
    assert variable_name("ExternalIdPOIDCClientId") == "external_idp_oidc_client_id"
    assert variable_name("AdminEmail") == "admin_email"


def test_every_function_carries_the_idp_stack_name_tag(schema):
    """Run-time code finds a deployment's functions by stack-name tag;
    CloudFormation's own key is reserved, so the module sets idp:stack-name."""
    t = {"Resources": {"F": lambda_fn(Tags=[{"Key": "a", "Value": "b"}])}}
    out = render(t, schema)["main.tf"]
    assert '"idp:stack-name" = var.stack_name' in out
    assert '"a" = "b"' in out


def test_a_retained_unnamed_table_gets_a_stable_suffix(schema):
    t = {
        "Resources": {
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "DeletionPolicy": "Retain",
                "Properties": {
                    "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
                    "AttributeDefinitions": [
                        {"AttributeName": "k", "AttributeType": "S"}
                    ],
                    "BillingMode": "PAY_PER_REQUEST",
                },
            }
        }
    }
    out = render(t, schema)["main.tf"]
    assert 'resource "time_static" "t_name_suffix"' in out
    assert "time_static.t_name_suffix.unix" in out


def test_generated_names_of_stateful_resources_survive_a_change_in_derivation(schema):
    """A generated table name or bucket prefix forces replacement when it
    changes; replacement would delete an existing deployment's data."""
    t = {
        "Resources": {
            "T": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {
                    "KeySchema": [{"AttributeName": "k", "KeyType": "HASH"}],
                    "AttributeDefinitions": [
                        {"AttributeName": "k", "AttributeType": "S"}
                    ],
                },
            },
            "B": {"Type": "AWS::S3::Bucket", "Properties": {}},
            "N": {"Type": "AWS::S3::Bucket", "Properties": {"BucketName": "fixed"}},
        }
    }
    out = render(t, schema)["main.tf"]
    assert "ignore_changes = [name]" in out
    assert "ignore_changes = [bucket_prefix]" in out
    named = out[out.index('resource "aws_s3_bucket" "n"') :]
    assert "ignore_changes" not in named[: named.index("\n}")]


def test_sam_expansion_needs_no_region_in_the_environment(monkeypatch, tmp_path):
    """CI's hermetic runs carry no AWS region, and SAM refuses to run without
    one unless it is given a session; the expansion must not depend on either."""
    from idp_tf.expand import expand

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "none"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "none"))
    out = expand(
        {
            "Transform": "AWS::Serverless-2016-10-31",
            "Resources": {
                "Fn": {
                    "Type": "AWS::Serverless::Function",
                    "Properties": {
                        "Runtime": "python3.12",
                        "Handler": "index.handler",
                        "CodeUri": "s3://bucket/key.zip",
                    },
                }
            },
        }
    )
    assert out["Resources"]["Fn"]["Type"] == "AWS::Lambda::Function"


# ------------------------------------------------- the bridge's key and logs
def _bridge_template(
    key_waits_on: str | None = "Check", with_bucket: bool = True
) -> dict:
    """A root with a custom resource (so a bridge), a key its log group uses,
    and, optionally, a bucket that logs to a logging bucket."""
    fn = {
        "Type": "AWS::Lambda::Function",
        "Properties": {
            "Role": "arn:x",
            "Code": {"S3Bucket": "b", "S3Key": "k"},
            "Handler": "index.handler",
            "Runtime": "python3.12",
        },
    }
    resources: dict = {
        "CheckFunction": fn,
        "Check": {
            "Type": "Custom::StacknameCheck",
            "Properties": {
                "ServiceToken": {"Fn::GetAtt": ["CheckFunction", "Arn"]},
                "InputString": {"Ref": "AWS::StackName"},
                "MaxLength": 25,
            },
        },
        "Other": {
            "Type": "Custom::Thing",
            "Properties": {"ServiceToken": {"Fn::GetAtt": ["CheckFunction", "Arn"]}},
        },
        "Key": {"Type": "AWS::KMS::Key", "Properties": {"Description": "k"}},
        "Group": {
            "Type": "AWS::Logs::LogGroup",
            "Properties": {
                "KmsKeyId": {"Fn::GetAtt": ["Key", "Arn"]},
                "RetentionInDays": 30,
            },
        },
    }
    if key_waits_on:
        resources["Key"]["DependsOn"] = key_waits_on
    if with_bucket:
        resources["Logs"] = {"Type": "AWS::S3::Bucket", "DependsOn": "Check"}
        resources["Data"] = {
            "Type": "AWS::S3::Bucket",
            "Properties": {
                "LoggingConfiguration": {"DestinationBucketName": {"Ref": "Logs"}}
            },
        }
    return {"Resources": resources}


@pytest.fixture
def vendored() -> AwsccSchema:
    from idp_tf.cli import VENDORED_SCHEMA

    return AwsccSchema(VENDORED_SCHEMA)


def _block(text: str, header: str) -> str:
    start = text.index(header)
    return text[start : text.index("\n}\n", start)]


def test_the_bridge_mirrors_the_key_and_logging_bucket_of_the_template(vendored):
    from idp_tf.static_files import bridge_tf

    tr = Translator(_bridge_template(), vendored)
    files = tr.render()
    assert tr.bridge_kms_key() == "awscc_kms_key.key.arn"
    assert tr.bridge_access_log_bucket() == "aws_s3_bucket.logs.id"
    bridge = bridge_tf(
        None,
        kms_key=tr.bridge_kms_key(),
        access_log_bucket=tr.bridge_access_log_bucket(),
    )
    group = _block(bridge, 'resource "aws_cloudwatch_log_group" "cfn_bridge"')
    assert "kms_key_id        = awscc_kms_key.key.arn" in group
    logging = _block(bridge, 'resource "aws_s3_bucket_logging" "cfn_bridge"')
    assert "target_bucket = aws_s3_bucket.logs.id" in logging
    assert 'target_prefix = "cfn-bridge-logs/${var.stack_name}/"' in logging
    # The key waited on the stack-name check, which the bridge runs: that edge
    # is left out (stack_name's validation enforces the check at plan), and
    # nothing else loses its DependsOn.
    key = _block(files["main.tf"], 'resource "awscc_kms_key" "key"')
    assert "aws_lambda_invocation.check" not in key
    logs = _block(files["main.tf"], 'resource "aws_s3_bucket" "logs"')
    assert "aws_lambda_invocation.check" in logs
    assert tr.bridge_dropped_deps == {("Key", "Check")}
    tr.render()
    assert [w for w in tr.report.warnings if "DependsOn Check left out" in w] == [
        "Key: DependsOn Check left out, so the custom-resource bridge can use the "
        "key; stack_name's validation enforces Check at plan"
    ]


def test_a_key_that_waits_on_another_custom_resource_is_not_used(vendored):
    """The bridge runs every custom resource, so a key waiting on one the
    root's validation does not stand in for would be a cycle: no key."""
    tr = Translator(_bridge_template(key_waits_on="Other"), vendored)
    files = tr.render()
    assert tr.bridge_kms_key() is None
    assert tr.bridge_dropped_deps == set()
    key = _block(files["main.tf"], 'resource "awscc_kms_key" "key"')
    assert "aws_lambda_invocation.other" in key


def test_a_stack_name_check_looser_than_the_validation_is_not_dropped(vendored):
    t = _bridge_template()
    t["Resources"]["Check"]["Properties"]["MaxLength"] = 24
    tr = Translator(t, vendored)
    tr.render()
    assert tr.bridge_kms_key() is None
    assert tr.bridge_dropped_deps == set()


def test_a_template_with_neither_gives_a_bridge_without_either(vendored):
    from idp_tf.static_files import bridge_tf

    t = _bridge_template(key_waits_on=None, with_bucket=False)
    del t["Resources"]["Group"]["Properties"]["KmsKeyId"]
    tr = Translator(t, vendored)
    tr.render()
    assert tr.bridge_kms_key() is None and tr.bridge_access_log_bucket() is None
    bridge = bridge_tf(None)
    assert "kms_key_id" not in bridge
    assert "aws_s3_bucket_logging" not in bridge


def test_nested_modules_inherit_the_bridge_key_and_logging_bucket(vendored):
    child = {
        "Parameters": {"KeyArn": {"Type": "String"}},
        "Resources": {
            "Thing": {
                "Type": "Custom::Thing",
                "Properties": {"ServiceToken": "arn:fn"},
            }
        },
    }
    t = _bridge_template()
    t["Resources"]["Pattern"] = {
        "Type": "AWS::CloudFormation::Stack",
        "Properties": {
            "TemplateURL": "./p/.aws-sam/packaged.yaml",
            "Parameters": {"KeyArn": {"Fn::GetAtt": ["Key", "Arn"]}},
        },
    }
    tr = Translator(t, vendored, nested_loader=lambda url: child)
    files = tr.render()
    module = _block(files["main.tf"], 'module "pattern"')
    assert "cfn_bridge_kms_key_arn = awscc_kms_key.key.arn" in module
    assert "cfn_bridge_access_log_bucket = aws_s3_bucket.logs.id" in module
    sub = tr.children["pattern"]
    sub_files = sub.render()
    for name in ("cfn_bridge_kms_key_arn", "cfn_bridge_access_log_bucket"):
        declared = _block(sub_files["variables.tf"], f'variable "{name}"')
        assert not re.search(r"^\s*default\s*=", declared, re.M), declared
    assert sub.bridge_kms_key() == "var.cfn_bridge_kms_key_arn"
    assert sub.bridge_access_log_bucket() == "var.cfn_bridge_access_log_bucket"
    # A root with neither passes null, and the child's bridge then has neither.
    bare = _bridge_template(key_waits_on=None, with_bucket=False)
    del bare["Resources"]["Group"]["Properties"]["KmsKeyId"]
    bare["Resources"]["Pattern"] = t["Resources"]["Pattern"]
    bare["Resources"]["Key2"] = {"Type": "AWS::KMS::Key", "Properties": {}}
    bare["Resources"]["Pattern"]["Properties"]["Parameters"] = {
        "KeyArn": {"Fn::GetAtt": ["Key2", "Arn"]}
    }
    tr2 = Translator(bare, vendored, nested_loader=lambda url: child)
    module2 = _block(tr2.render()["main.tf"], 'module "pattern"')
    assert "cfn_bridge_kms_key_arn = null" in module2
    assert "cfn_bridge_access_log_bucket = null" in module2
    assert tr2.children["pattern"].bridge_kms_key() is None
    assert tr2.children["pattern"].bridge_access_log_bucket() is None


def test_the_bridge_bucket_aborts_incomplete_uploads_bucket_wide():
    from idp_tf.static_files import bridge_tf

    lifecycle = _block(
        bridge_tf(None), 'resource "aws_s3_bucket_lifecycle_configuration" "cfn_bridge"'
    )
    rule = lifecycle[lifecycle.index('id     = "abort-incomplete-uploads"') :]
    assert "filter {}" in rule
    assert "days_after_initiation = 1" in rule


def test_a_parameter_named_like_a_bridge_variable_does_not_collide(schema):
    t = queue(QueueName={"Ref": "CfnBridgeKmsKeyArn"})
    t["Parameters"] = {"CfnBridgeKmsKeyArn": {"Type": "String"}}
    files = render(t, schema)
    assert "queue_name = var.param_cfn_bridge_kms_key_arn" in files["main.tf"]


def _sse(bridge: str) -> str:
    return _block(
        bridge,
        'resource "aws_s3_bucket_server_side_encryption_configuration" "cfn_bridge"',
    )


def test_the_bridge_bucket_is_encrypted_with_the_key_when_there_is_one():
    """The bucket holds custom-resource responses (which can carry a handler's
    outputs) and the resource directory, so it takes the stack key the
    template's own buckets use, and the bridge role, which writes every
    object handlers and responses put there, may use the key through S3."""
    from idp_tf.static_files import bridge_tf

    key = "awscc_kms_key.key.arn"
    bridge = bridge_tf(None, kms_key=key)
    sse = _sse(bridge)
    assert re.search(r'^\s*sse_algorithm\s*=\s*"aws:kms"$', sse, re.M), sse
    assert re.search(rf"^\s*kms_master_key_id\s*=\s*{re.escape(key)}$", sse, re.M)
    assert re.search(r"^\s*bucket_key_enabled\s*=\s*true$", sse, re.M)
    assert "AES256" not in bridge
    policy = _block(bridge, 'resource "aws_iam_role_policy" "cfn_bridge"')
    grant = re.search(
        r'Action\s*=\s*\["kms:GenerateDataKey", "kms:Decrypt"\]\s*'
        rf"Resource\s*=\s*{re.escape(key)}\s*"
        r'Condition\s*=\s*\{ StringEquals = \{ "kms:ViaService" = '
        r'"s3\.\$\{local\.pseudo\.region\}\.\$\{local\.pseudo\.dns_suffix\}" \} \}',
        policy,
    )
    assert grant, policy


def test_the_bridge_bucket_keeps_sse_s3_without_a_key():
    from idp_tf.static_files import bridge_tf

    bridge = bridge_tf(None)
    assert re.search(r'^\s*sse_algorithm\s*=\s*"AES256"$', _sse(bridge), re.M)
    assert "kms" not in bridge.lower()


def _waf_logging_template(**extra) -> dict:
    return {
        "Resources": {
            "Acl": {
                "Type": "AWS::WAFv2::WebACL",
                "Properties": {
                    "Scope": "REGIONAL",
                    "DefaultAction": {"Block": {}},
                    "VisibilityConfig": {
                        "SampledRequestsEnabled": True,
                        "CloudWatchMetricsEnabled": True,
                        "MetricName": "m",
                    },
                },
            },
            "AclLogs": {
                "Type": "AWS::WAFv2::LoggingConfiguration",
                "Properties": {
                    "ResourceArn": {"Fn::GetAtt": ["Acl", "Arn"]},
                    "LogDestinationConfigs": [
                        "arn:partition:logs:region:account:log-group:aws-waf-logs-x"
                    ],
                    **extra,
                },
            },
        }
    }


def test_a_waf_logging_configuration_maps_to_the_aws_provider(vendored):
    main_tf = Translator(_waf_logging_template(), vendored).render()["main.tf"]
    block = _block(
        main_tf, 'resource "aws_wafv2_web_acl_logging_configuration" "acl_logs"'
    )
    assert "resource_arn = aws_wafv2_web_acl.acl.arn" in block
    assert re.search(r"log_destination_configs = \[\s*\"arn:", block), block


def test_a_waf_logging_setting_the_mapping_does_not_carry_fails(vendored):
    t = _waf_logging_template(RedactedFields=[{"UriPath": {}}])
    with pytest.raises(TranslationError, match="RedactedFields"):
        Translator(t, vendored).render()


# -- nested templates by their published S3 URL ---------------------------------


class _RecordingS3:
    def __init__(self, body: str):
        self.body, self.calls = body, []

    def download_file(self, bucket, key, target):
        self.calls.append((bucket, key))
        Path(target).write_text(self.body)


@pytest.mark.parametrize(
    ("url", "region"),
    [
        # what the publisher writes in us-east-1: the global endpoint, no region
        ("https://s3.amazonaws.com/bkt/pre/0.6/abc.template", "us-east-1"),
        ("https://s3.us-west-2.amazonaws.com/bkt/pre/0.6/abc.template", "us-west-2"),
        (
            "https://s3-eu-central-1.amazonaws.com/bkt/pre/0.6/abc.template",
            "eu-central-1",
        ),
    ],
)
def test_a_published_nested_template_url_is_read_from_its_region(
    tmp_path, monkeypatch, url, region
):
    import boto3
    from idp_tf.cli import s3_loader

    fake = _RecordingS3("AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n")
    seen = {}

    def client(service, region_name=None):
        seen["service"], seen["region"] = service, region_name
        return fake

    monkeypatch.setattr(boto3, "client", client)
    template = s3_loader(tmp_path / "cache")(url)
    assert template.get("Resources") == {}
    assert seen == {"service": "s3", "region": region}
    assert fake.calls == [("bkt", "pre/0.6/abc.template")]


def test_a_url_that_is_not_s3_is_still_refused(tmp_path):
    from idp_tf.cli import TranslationError, s3_loader

    with pytest.raises(TranslationError, match="cannot read nested TemplateURL"):
        s3_loader(tmp_path)("https://example.com/bkt/abc.template")
