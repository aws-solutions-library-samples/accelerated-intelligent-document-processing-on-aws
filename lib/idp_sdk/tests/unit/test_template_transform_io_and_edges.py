# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""File-level entry points and the narrow branches of both template transformers.

`idp_sdk/_core/template_transform.py` holds two transformers that rewrite the
solution's main CloudFormation template before it is published:
`HeadlessTemplateTransformer`, which strips every UI-facing service so the stack
deploys API-only, and `GovCloudTemplateTransformer`, which strips the resource
types that do not exist in a GovCloud partition (`AWS::CloudFront::*`,
`AWS::Lambda::Url`, and the Lambda functions that layer in the commercial-only
Lambda Web Adapter).

Two suites already exercise these classes: `test_template_transform.py` and
`test_govcloud_template_transform.py`. Both work almost entirely **in memory** —
they build a dict, call `apply_transforms`, and assert on the result — and both
concentrate on the whole-template outcome. What neither covers, and what this
file covers, is:

* the **file-level** `transform()` / `load_template()` / `save_template()` round
  trip on both classes, including what each does with a missing file, malformed
  YAML, an output path whose parent directory does not exist yet, and a
  mid-transform failure (`transform()` swallows every exception and returns
  `False`, so a caller that reads the return value is the only thing standing
  between a failed transform and a published template);
* `validate_template_basic()` and `validate_no_cloudfront()` driven **to their
  failure verdicts** — these are the publish gate, and a validator that cannot
  say "no" is not a gate;
* the small structural branches the whole-template tests step over because the
  real template happens not to contain the shape: a scalar `DependsOn`, a
  `Principal.Service` given as a list or behind an `Fn::Sub`, a `PolicyName`
  that is an intrinsic rather than a string, a statement list holding a
  non-object entry, a `Statement` given as a single dict, an already-partitioned
  ARN;
* the two `_update_arn_partitions` implementations and the headless
  `_update_configuration_maps_for_govcloud`, which are pure text/dict rewrites
  whose wrong answer is a template that deploys in the wrong partition or
  against a model id that does not exist in the target region.

The tests assert on the transformed structure rather than on log output
wherever a structure exists to assert on, because the log line is a report and
the structure is what CloudFormation receives.
"""

from typing import Any, Dict

import pytest
import yaml

from idp_sdk._core.template_transform import (
    GovCloudTemplateTransformer,
    HeadlessTemplateTransformer,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


def _core_resources() -> Dict[str, Any]:
    """The resources `validate_template_basic` insists survive the transform."""
    return {
        "InputBucket": {"Type": "AWS::S3::Bucket"},
        "OutputBucket": {"Type": "AWS::S3::Bucket"},
        "WorkingBucket": {"Type": "AWS::S3::Bucket"},
        "TrackingTable": {"Type": "AWS::DynamoDB::Table"},
        "ConfigurationTable": {"Type": "AWS::DynamoDB::Table"},
        "CustomerManagedEncryptionKey": {"Type": "AWS::KMS::Key"},
        "PATTERNSTACK": {"Type": "AWS::CloudFormation::Stack"},
    }


def _valid_headless_input() -> Dict[str, Any]:
    """A template small enough to reason about that survives headless validation."""
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "IDP",
        "Parameters": {
            "AdminEmail": {"Type": "String"},
            "ConfigurationPreset": {
                "Type": "String",
                "Default": "lending-package-sample",
                "AllowedValues": ["lending-package-sample", "rvl-cdip"],
            },
        },
        "Mappings": {
            "ConfigurationMap": {
                "lending-package-sample": {"ConfigPath": "lending-package-sample"}
            }
        },
        "Rules": {
            "ValidateExistingPrivateWorkforceArn": {"Assertions": []},
            "KeepThisRule": {"Assertions": []},
        },
        "Resources": _core_resources(),
        "Outputs": {"InputBucketName": {"Value": {"Ref": "InputBucket"}}},
    }


# --------------------------------------------------------------------------
# Headless: load_template / save_template / transform
# --------------------------------------------------------------------------


def test_headless_load_template_reads_yaml(tmp_path):
    src = tmp_path / "t.yaml"
    src.write_text(yaml.dump(_valid_headless_input()), encoding="utf-8")

    loaded = HeadlessTemplateTransformer().load_template(str(src))

    assert loaded["Resources"]["InputBucket"]["Type"] == "AWS::S3::Bucket"


def test_headless_load_template_missing_file_raises_filenotfound(tmp_path):
    """The distinct exception type matters: `transform()` turns any exception into
    a `False` return, so the only place the *cause* is visible is a direct call.
    """
    with pytest.raises(FileNotFoundError) as excinfo:
        HeadlessTemplateTransformer().load_template(str(tmp_path / "absent.yaml"))

    assert "absent.yaml" in str(excinfo.value)


def test_headless_load_template_malformed_yaml_raises_valueerror(tmp_path):
    bad = tmp_path / "bad.yaml"
    # An unclosed flow mapping is a parse error, not a semantic one.
    bad.write_text("Resources: {a: b\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Failed to parse YAML template"):
        HeadlessTemplateTransformer().load_template(str(bad))


def test_headless_save_template_creates_missing_parent_directory(tmp_path):
    """`save_template` is handed a path under `.aws-sam/` that may not exist yet;
    if it did not create the parent, the publish would fail after the transform
    had already succeeded.
    """
    out = tmp_path / "nested" / "deeper" / "out.yaml"

    HeadlessTemplateTransformer().save_template({"Resources": {}}, str(out))

    assert yaml.safe_load(out.read_text(encoding="utf-8")) == {"Resources": {}}


def test_headless_save_template_to_a_bare_filename_stays_in_cwd(tmp_path, monkeypatch):
    """A path with no directory component must not become an `os.makedirs("")`.

    The code substitutes "." for an empty dirname; a failure here would be a
    `FileNotFoundError` on every relative output path.
    """
    monkeypatch.chdir(tmp_path)

    HeadlessTemplateTransformer().save_template({"Resources": {}}, "bare.yaml")

    assert (tmp_path / "bare.yaml").is_file()


def test_headless_save_template_wraps_write_failure_in_valueerror(tmp_path):
    target = tmp_path / "adir"
    target.mkdir()

    with pytest.raises(ValueError, match="Failed to save template"):
        HeadlessTemplateTransformer().save_template({"Resources": {}}, str(target))


def test_headless_transform_writes_a_transformed_file_and_returns_true(tmp_path):
    """The end-to-end file path: the output must exist, parse, and show the
    transform's effects — not merely be a copy of the input.
    """
    src = tmp_path / "in.yaml"
    out = tmp_path / "sub" / "out.yaml"
    src.write_text(yaml.dump(_valid_headless_input()), encoding="utf-8")

    assert HeadlessTemplateTransformer().transform(str(src), str(out)) is True

    written = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert written["Description"].endswith("(Headless)")
    # AdminEmail is in `parameters_to_remove`; ConfigurationPreset is not.
    assert "AdminEmail" not in written["Parameters"]
    assert "ConfigurationPreset" in written["Parameters"]
    # The A2I rule goes, the unrelated rule stays.
    assert set(written["Rules"]) == {"KeepThisRule"}


def test_headless_transform_returns_false_when_validation_fails(tmp_path):
    """A template missing a core resource must not be published.

    The file is still written — the transform saves before validating — so the
    return value is the only signal. A caller that ignores it publishes an
    unusable template, which is why this assertion is on the boolean and not on
    the file.
    """
    incomplete = _valid_headless_input()
    del incomplete["Resources"]["TrackingTable"]
    src = tmp_path / "in.yaml"
    out = tmp_path / "out.yaml"
    src.write_text(yaml.dump(incomplete), encoding="utf-8")

    assert HeadlessTemplateTransformer().transform(str(src), str(out)) is False
    assert out.is_file()


def test_headless_transform_returns_false_when_the_input_is_absent(tmp_path):
    assert (
        HeadlessTemplateTransformer().transform(
            str(tmp_path / "nope.yaml"), str(tmp_path / "out.yaml")
        )
        is False
    )


def test_headless_transform_verbose_failure_logs_a_traceback(tmp_path, caplog):
    """`verbose=True` adds the traceback at DEBUG. The branch is worth pinning
    because a silent `except Exception: return False` is the only error path this
    class has, and losing the traceback makes a transform failure undiagnosable.
    """
    transformer = HeadlessTemplateTransformer(verbose=True)
    with caplog.at_level("DEBUG", logger="idp_sdk._core.template_transform"):
        assert (
            transformer.transform(
                str(tmp_path / "nope.yaml"), str(tmp_path / "out.yaml")
            )
            is False
        )

    assert any("Traceback" in record.message for record in caplog.records)


# --------------------------------------------------------------------------
# Headless: validate_template_basic
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mutate", "expected_fragment"),
    [
        (lambda t: t.pop("AWSTemplateFormatVersion"), "AWSTemplateFormatVersion"),
        (lambda t: t.pop("Resources"), "Resources"),
        (lambda t: t["Resources"].pop("InputBucket"), "InputBucket"),
        (lambda t: t["Resources"].pop("CustomerManagedEncryptionKey"), "Encryption"),
        (lambda t: t["Resources"].pop("PATTERNSTACK"), "PATTERNSTACK"),
    ],
)
def test_validate_template_basic_rejects_each_missing_essential(
    mutate, expected_fragment, caplog
):
    """One parametrised case per thing the validator is responsible for noticing.

    A failure means the publish gate would pass a template that CloudFormation
    or the runtime rejects: no tracking table, no encryption key, or no pattern
    stack to do the processing.
    """
    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": _core_resources(),
    }
    mutate(template)

    with caplog.at_level("ERROR", logger="idp_sdk._core.template_transform"):
        assert HeadlessTemplateTransformer().validate_template_basic(template) is False

    assert expected_fragment in caplog.text


def test_validate_template_basic_accepts_a_complete_template():
    assert (
        HeadlessTemplateTransformer().validate_template_basic(
            {"AWSTemplateFormatVersion": "2010-09-09", "Resources": _core_resources()}
        )
        is True
    )


# --------------------------------------------------------------------------
# Headless: Rules section
# --------------------------------------------------------------------------


def test_rules_section_is_deleted_when_the_last_rule_goes():
    """CloudFormation rejects an empty `Rules: {}`, so the section itself must go
    once its only member is removed.
    """
    template = {
        "Resources": _core_resources(),
        "Rules": {"ValidateExistingPrivateWorkforceArn": {"Assertions": []}},
    }

    result = HeadlessTemplateTransformer()._remove_rules(template)

    assert "Rules" not in result


def test_a_template_with_no_rules_section_is_returned_untouched():
    template = {"Resources": _core_resources()}

    assert HeadlessTemplateTransformer()._remove_rules(template) is template


# --------------------------------------------------------------------------
# Headless: policy-statement removal reasons
# --------------------------------------------------------------------------


def test_graphql_grant_is_removed_whether_the_action_is_a_string_or_a_list():
    """Both spellings of the same grant must be recognised.

    The reason string is asserted, not just the removal: the record is what an
    SDK caller audits, and a removal attributed to the wrong reason is a
    misleading audit trail.
    """
    transformer = HeadlessTemplateTransformer()
    as_string = {"Action": "appsync:GraphQL", "Resource": "*"}
    as_list = {"Action": ["logs:PutLogEvents", "appsync:GraphQL"], "Resource": "*"}

    assert (
        transformer._statement_removal_reason(as_string)
        == HeadlessTemplateTransformer._GRAPHQL_GRANT_REASON
    )
    assert (
        transformer._statement_removal_reason(as_list)
        == HeadlessTemplateTransformer._GRAPHQL_GRANT_REASON
    )
    assert transformer._should_remove_statement(as_string) is True


def test_an_unrelated_statement_is_kept_and_has_no_removal_reason():
    transformer = HeadlessTemplateTransformer()
    stmt = {"Action": ["s3:GetObject"], "Resource": {"Ref": "InputBucket"}}

    assert transformer._statement_removal_reason(stmt) is None
    assert transformer._should_remove_statement(stmt) is False


def test_mcp_secret_grant_is_recognised_in_a_resource_list():
    transformer = HeadlessTemplateTransformer()
    stmt = {
        "Action": "secretsmanager:GetSecretValue",
        "Resource": [{"Ref": "SomethingElse"}, {"Ref": "ExternalMCPAgentsSecret"}],
    }

    assert (
        transformer._statement_removal_reason(stmt)
        == HeadlessTemplateTransformer._MCP_GRANT_REASON
    )


def test_references_removed_resource_ignores_a_plain_string_resource():
    """A `Resource: "arn:..."` string names no logical id, so it cannot be a
    reference to a removed one. Treating it as one would strip live permissions.
    """
    transformer = HeadlessTemplateTransformer()

    assert (
        transformer._references_removed_resource("*", "ExternalMCPAgentsSecret")
        is False
    )
    assert (
        transformer._references_removed_resource(
            [{"Ref": "Other"}], "ExternalMCPAgentsSecret"
        )
        is False
    )


def test_a_whole_policy_is_dropped_when_its_single_statement_dies():
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    policy = {
        "Statement": {
            "Action": "secretsmanager:GetSecretValue",
            "Resource": {"Ref": "ExternalMCPAgentsSecret"},
        }
    }

    assert transformer._clean_policy_statements(policy, "SomeFunction") is None
    assert [r.resource_identifier for r in transformer.policy_statement_removals] == [
        "SomeFunction"
    ]


def test_a_single_surviving_dict_statement_keeps_the_policy_object_identity():
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    policy = {"Statement": {"Action": "s3:GetObject", "Resource": "*"}}

    assert transformer._clean_policy_statements(policy, "SomeFunction") is policy
    assert transformer.policy_statement_removals == []


def test_a_non_object_entry_in_a_statement_list_is_dropped_and_reported():
    """Pins current behaviour on malformed input: a non-dict entry in a
    `Statement` list is silently discarded, and the discard is attributed to a
    "malformed" reason rather than raising. A failure here would mean a template
    carrying a stray scalar either crashes the publish or keeps an entry IAM
    would reject.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    policy = {
        "Statement": ["not-a-statement", {"Action": "s3:GetObject", "Resource": "*"}]
    }

    cleaned = transformer._clean_policy_statements(policy, "SomeFunction")

    assert cleaned is not None
    assert cleaned["Statement"] == [{"Action": "s3:GetObject", "Resource": "*"}]
    (record,) = transformer.policy_statement_removals
    assert record.removed == 1
    assert "malformed" in record.reason


def test_a_statement_that_is_neither_dict_nor_list_leaves_the_policy_alone():
    transformer = HeadlessTemplateTransformer()
    policy = {"Statement": "wat"}

    assert transformer._clean_policy_statements(policy, "SomeFunction") is policy


# --------------------------------------------------------------------------
# Headless: _clean_tracking_function
# --------------------------------------------------------------------------


def test_tracking_function_loses_appsync_url_and_gains_a_dynamodb_policy():
    """The headless template tracks documents straight to DynamoDB. If the empty
    `APPSYNC_API_URL` survived, the runtime would still branch on its presence;
    if the CRUD policy were not added, every write would be access-denied.
    """
    resources = {
        "Fn": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "Environment": {"Variables": {"APPSYNC_API_URL": ""}},
                "Policies": [
                    {
                        "Statement": [
                            {"Action": "appsync:GraphQL", "Resource": "*"},
                            {"Action": "s3:GetObject", "Resource": "*"},
                        ]
                    }
                ],
            },
        }
    }
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()

    transformer._clean_tracking_function(resources, "Fn")

    env = resources["Fn"]["Properties"]["Environment"]["Variables"]
    assert "APPSYNC_API_URL" not in env
    assert env["DOCUMENT_TRACKING_MODE"] == "dynamodb"
    assert env["TRACKING_TABLE"] == {"Ref": "TrackingTable"}

    policies = resources["Fn"]["Properties"]["Policies"]
    assert {"DynamoDBCrudPolicy": {"TableName": {"Ref": "TrackingTable"}}} in policies
    # The appsync statement went; the S3 one stayed.
    statement_policy = next(p for p in policies if "Statement" in p)
    assert statement_policy["Statement"] == [
        {"Action": "s3:GetObject", "Resource": "*"}
    ]


def test_tracking_function_with_an_existing_crud_policy_does_not_get_a_second():
    resources = {
        "Fn": {
            "Type": "AWS::Serverless::Function",
            "Properties": {
                "Policies": [
                    {"DynamoDBCrudPolicy": {"TableName": {"Ref": "TrackingTable"}}}
                ]
            },
        }
    }

    HeadlessTemplateTransformer()._clean_tracking_function(resources, "Fn")

    policies = resources["Fn"]["Properties"]["Policies"]
    assert sum("DynamoDBCrudPolicy" in p for p in policies) == 1


# --------------------------------------------------------------------------
# Headless: nested stack parameters
# --------------------------------------------------------------------------


def test_nested_stack_params_are_hardcoded_and_discovery_params_deleted():
    resources = {
        "PATTERNSTACK": {
            "Type": "AWS::CloudFormation::Stack",
            "DependsOn": ["GraphQLApi", "InputBucket"],
            "Properties": {
                "Parameters": {
                    "EnableHITL": {"Ref": "EnableHITL"},
                    "SageMakerA2IReviewPortalURL": {"Fn::GetAtt": ["X", "Url"]},
                    "AppSyncApiUrl": {"Fn::GetAtt": ["GraphQLApi", "GraphQLUrl"]},
                    "AppSyncApiArn": {"Fn::GetAtt": ["GraphQLApi", "Arn"]},
                    "DiscoveryBucket": {"Ref": "DiscoveryBucket"},
                    "DiscoveryTrackingTable": {"Ref": "DiscoveryTrackingTable"},
                    "DiscoveryBucketName": {"Ref": "DiscoveryBucket"},
                    "DiscoveryTrackingTableName": {"Ref": "DiscoveryTrackingTable"},
                    "MultiDocDiscoveryStateMachineArn": {"Ref": "SM"},
                    "KeepMe": {"Ref": "InputBucket"},
                }
            },
        }
    }

    HeadlessTemplateTransformer()._clean_nested_stack_params(resources, "PATTERNSTACK")

    params = resources["PATTERNSTACK"]["Properties"]["Parameters"]
    assert params == {
        "EnableHITL": "false",
        "SageMakerA2IReviewPortalURL": '""',
        "KeepMe": {"Ref": "InputBucket"},
    }
    assert resources["PATTERNSTACK"]["DependsOn"] == ["InputBucket"]


def test_a_scalar_dependson_naming_the_removed_api_is_deleted_entirely():
    """`DependsOn` may be a bare string. Filtering it as a list would leave the
    string's characters, so this branch is separate code — and a surviving
    `DependsOn: GraphQLApi` is a template CloudFormation refuses outright.
    """
    resources = {
        "PATTERNSTACK": {
            "Type": "AWS::CloudFormation::Stack",
            "DependsOn": "GraphQLApi",
            "Properties": {"Parameters": {}},
        }
    }

    HeadlessTemplateTransformer()._clean_nested_stack_params(resources, "PATTERNSTACK")

    assert "DependsOn" not in resources["PATTERNSTACK"]


# --------------------------------------------------------------------------
# Headless: orphaned outputs and UpdateSettingsValues
# --------------------------------------------------------------------------


def test_outputs_referencing_removed_resources_are_dropped_by_both_routes():
    """Two detection routes exist: a direct `Ref`, and the resource's name
    appearing anywhere in a nested value. Both must fire, and an unrelated
    output must survive both.
    """
    template = {
        "Outputs": {
            "ByRef": {"Value": {"Ref": "AgentTable"}},
            "ByRefBucket": {"Value": {"Ref": "WebUIBucket"}},
            "Nested": {"Value": {"Fn::Sub": "${AgentTable.Arn}"}},
            "Unrelated": {"Value": {"Ref": "InputBucket"}},
            "NotADict": "scalar",
        }
    }

    HeadlessTemplateTransformer()._clean_orphaned_outputs(template)

    assert set(template["Outputs"]) == {"Unrelated", "NotADict"}


def test_update_settings_values_is_neutralised_for_headless():
    template = {
        "Resources": {
            "UpdateSettingsValues": {
                "Properties": {
                    "SettingsKeyValuePairs": {
                        "KnowledgeBaseId": {"Ref": "DocumentKB"},
                        "ShouldUseDocumentKnowledgeBase": True,
                        "AllowedSignUpEmailDomains": "example.com",
                        "DiscoveryBucket": {"Ref": "DiscoveryBucket"},
                        "PublicArtifactsBucket": "b",
                        "PublicArtifactsPrefix": "p",
                        "Keep": "yes",
                    }
                }
            }
        }
    }

    HeadlessTemplateTransformer()._clean_update_settings_values(template)

    kvp = template["Resources"]["UpdateSettingsValues"]["Properties"][
        "SettingsKeyValuePairs"
    ]
    assert kvp == {
        "KnowledgeBaseId": "",
        "ShouldUseDocumentKnowledgeBase": False,
        "Keep": "yes",
    }


def test_update_settings_values_absent_is_a_no_op():
    template = {"Resources": {}}

    HeadlessTemplateTransformer()._clean_update_settings_values(template)

    assert template == {"Resources": {}}


# --------------------------------------------------------------------------
# Headless: CloudFront principal cleaning, the shapes the real template lacks
# --------------------------------------------------------------------------


def test_cloudfront_principal_behind_an_fn_sub_is_removed():
    """The real template writes the service principal as
    `{"Fn::Sub": "cloudfront.${AWS::URLSuffix}"}`; a transform that only matched
    plain strings would leave a grant to a service that does not exist in the
    target partition.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    doc = {
        "Statement": [
            {"Principal": {"Service": {"Fn::Sub": "cloudfront.${AWS::URLSuffix}"}}},
            {"Principal": {"Service": "s3.amazonaws.com"}},
        ]
    }

    transformer._clean_policy_document_cloudfront(doc, "LoggingBucketPolicy")

    assert doc["Statement"] == [{"Principal": {"Service": "s3.amazonaws.com"}}]
    (record,) = transformer.policy_statement_removals
    assert (record.removed, record.narrowed) == (1, 0)


def test_a_principal_service_list_is_narrowed_not_dropped():
    """Narrowing and removal are different outcomes and are counted separately.
    A statement granting two services must keep the surviving one, or the
    logging bucket loses its S3 log-delivery grant as collateral.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    doc = {
        "Statement": [
            {
                "Principal": {
                    "Service": [
                        "cloudfront.amazonaws.com",
                        {"Fn::Sub": "cloudfront.${AWS::URLSuffix}"},
                        "logging.s3.amazonaws.com",
                    ]
                }
            }
        ]
    }

    transformer._clean_policy_document_cloudfront(doc, "LoggingBucketPolicy")

    assert doc["Statement"][0]["Principal"]["Service"] == ["logging.s3.amazonaws.com"]
    (record,) = transformer.policy_statement_removals
    assert (record.removed, record.narrowed) == (0, 1)


def test_a_principal_service_list_of_only_cloudfront_drops_the_statement():
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    doc = {"Statement": [{"Principal": {"Service": ["cloudfront.amazonaws.com"]}}]}

    transformer._clean_policy_document_cloudfront(doc, "P")

    assert doc["Statement"] == []


def test_policy_document_cloudfront_ignores_shapes_it_cannot_read():
    """Three non-statements in one test: no `Statement` key, a `Statement` that
    is not a list, and a non-dict entry inside the list. None may raise, and the
    non-dict entry must be preserved rather than eaten.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._clean_policy_document_cloudfront({}, "P")
    transformer._clean_policy_document_cloudfront({"Statement": "x"}, "P")
    doc = {"Statement": ["scalar"]}
    transformer._clean_policy_document_cloudfront(doc, "P")

    assert doc["Statement"] == ["scalar"]


def test_role_policy_with_an_intrinsic_policy_name_is_reported_as_computed():
    """A `PolicyName` may be an `Fn::Sub`. The identifier is operator-facing, so
    rendering a dict repr into it would produce an unreadable audit line.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    template = {
        "Resources": {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": {"Fn::Sub": "${AWS::StackName}-p"},
                            "PolicyDocument": {
                                "Statement": [
                                    {"Principal": {"Service": "cloudfront.example"}}
                                ]
                            },
                        }
                    ]
                },
            },
            "NotADict": "scalar",
            "Bucket": {"Type": "AWS::S3::Bucket"},
        }
    }

    transformer._clean_cloudfront_policy_statements(template)

    (record,) = transformer.policy_statement_removals
    assert record.resource_identifier == "Role.<computed>"


def test_bucket_policy_statements_are_cleaned_through_the_dispatcher():
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    template = {
        "Resources": {
            "LoggingBucketPolicy": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {"Principal": {"Service": "cloudfront.amazonaws.com"}}
                        ]
                    }
                },
            }
        }
    }

    transformer._clean_cloudfront_policy_statements(template)

    assert (
        template["Resources"]["LoggingBucketPolicy"]["Properties"]["PolicyDocument"][
            "Statement"
        ]
        == []
    )
    assert transformer.policy_statement_removals[0].resource_identifier == (
        "LoggingBucketPolicy"
    )


# --------------------------------------------------------------------------
# ARN partitions (both transformers) and GovCloud config maps
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "transformer",
    [HeadlessTemplateTransformer(), GovCloudTemplateTransformer()],
    ids=["headless", "govcloud"],
)
def test_hardcoded_arn_partitions_are_rewritten(transformer):
    """Both classes carry their own copy of this rewrite, and a GovCloud stack
    deployed with `arn:aws:` ARNs fails on every IAM evaluation. The input mixes
    a hardcoded ARN with an already-parameterised one so a rewrite that clobbers
    the correct form is also caught.
    """
    template = {
        "Resources": {
            "R": {
                "Properties": {
                    "Bad": "arn:aws:s3:::my-bucket/*",
                    "Good": "arn:${AWS::Partition}:s3:::other/*",
                }
            }
        }
    }

    result = transformer._update_arn_partitions(template)

    props = result["Resources"]["R"]["Properties"]
    assert props["Bad"] == "arn:${AWS::Partition}:s3:::my-bucket/*"
    assert props["Good"] == "arn:${AWS::Partition}:s3:::other/*"


@pytest.mark.parametrize(
    "transformer",
    [HeadlessTemplateTransformer(), GovCloudTemplateTransformer()],
    ids=["headless", "govcloud"],
)
def test_an_already_partitioned_template_is_returned_unchanged(transformer):
    """The no-rewrite branch. It matters because the rewrite round-trips the
    whole template through YAML; skipping it when nothing needs fixing is what
    keeps the published diff readable.
    """
    template = {
        "Resources": {"R": {"Properties": {"A": "arn:${AWS::Partition}:s3:::b"}}}
    }

    assert transformer._update_arn_partitions(template) == template


def test_headless_govcloud_config_map_adds_the_preset_and_defaults_to_it():
    template = {
        "Mappings": {
            "ConfigurationMap": {
                "lending-package-sample": {"ConfigPath": "lending-package-sample"}
            }
        },
        "Parameters": {
            "ConfigurationPreset": {
                "Type": "String",
                "Default": "lending-package-sample",
                "AllowedValues": ["lending-package-sample", "rvl-cdip"],
            }
        },
    }

    result = HeadlessTemplateTransformer()._update_configuration_maps_for_govcloud(
        template
    )

    assert result["Mappings"]["ConfigurationMap"][
        "lending-package-sample-govcloud"
    ] == {"ConfigPath": "lending-package-sample-govcloud"}
    preset = result["Parameters"]["ConfigurationPreset"]
    assert preset["Default"] == "lending-package-sample-govcloud"
    assert preset["AllowedValues"][0] == "lending-package-sample-govcloud"
    # The commercial presets are still selectable.
    assert "rvl-cdip" in preset["AllowedValues"]


def test_headless_govcloud_config_map_is_idempotent():
    """`publish.py` may transform a template that has already been transformed.
    A second pass must not insert the preset into `AllowedValues` twice, which
    CloudFormation rejects.
    """
    transformer = HeadlessTemplateTransformer()
    template = {
        "Mappings": {"ConfigurationMap": {}},
        "Parameters": {
            "ConfigurationPreset": {"Type": "String", "AllowedValues": ["x"]}
        },
    }

    transformer._update_configuration_maps_for_govcloud(template)
    transformer._update_configuration_maps_for_govcloud(template)

    allowed = template["Parameters"]["ConfigurationPreset"]["AllowedValues"]
    assert allowed.count("lending-package-sample-govcloud") == 1


def test_headless_govcloud_config_map_tolerates_a_template_without_either_section():
    template: Dict[str, Any] = {}

    assert (
        HeadlessTemplateTransformer()._update_configuration_maps_for_govcloud(template)
        == {}
    )


def test_headless_description_is_not_double_marked():
    transformer = HeadlessTemplateTransformer()

    once = transformer._update_description({"Description": "IDP"})
    twice = transformer._update_description(dict(once))

    assert once["Description"] == "IDP (Headless)"
    assert twice["Description"] == "IDP (Headless)"


def test_a_govcloud_marked_description_is_left_alone_by_the_headless_transform():
    """The two transforms compose: `--headless --govcloud` runs both. A second
    "(Headless)" suffix on an already-GovCloud description would be noise in the
    published template.
    """
    result = HeadlessTemplateTransformer()._update_description(
        {"Description": "IDP (GovCloud - CloudFront removed)"}
    )

    assert result["Description"] == "IDP (GovCloud - CloudFront removed)"


# --------------------------------------------------------------------------
# GovCloud: file entry points
# --------------------------------------------------------------------------


def _govcloud_input() -> Dict[str, Any]:
    """A small GovCloud-transformable template.

    The CloudFront output carries `Condition: UseCloudFrontHosting`, which is how
    the real template writes it — and, as the next-but-one test shows, the only
    way the transform knows to drop it.
    """
    return {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "IDP",
        "Parameters": {
            "WebUIHosting": {
                "Type": "String",
                "Default": "CloudFront",
                "AllowedValues": ["CloudFront", "APIGateway"],
            },
            "CloudFrontPriceClass": {"Type": "String", "Default": "PriceClass_100"},
        },
        "Conditions": {
            "UseCloudFrontHosting": {
                "Fn::Equals": [{"Ref": "WebUIHosting"}, "CloudFront"]
            }
        },
        "Resources": {
            "InputBucket": {"Type": "AWS::S3::Bucket"},
            "CloudFrontDistribution": {"Type": "AWS::CloudFront::Distribution"},
        },
        "Outputs": {
            "Url": {
                "Condition": "UseCloudFrontHosting",
                "Value": {"Fn::GetAtt": ["CloudFrontDistribution", "DomainName"]},
            }
        },
    }


def test_govcloud_transform_writes_a_cloudfront_free_file(tmp_path):
    """End to end through the file path: every CloudFront *declaration* goes.

    The assertions are structural rather than a scan for the string
    "CloudFront", because two survivors are deliberate prose: the description
    marker, and the rewritten `WebUIHosting` parameter description explaining why
    CloudFront is unavailable. What must not survive is anything CloudFormation
    acts on — the resource, the CloudFront-only parameter, the hosting condition,
    and the output conditioned on it.
    """
    src = tmp_path / "in.yaml"
    out = tmp_path / "deep" / "out.yaml"
    src.write_text(yaml.dump(_govcloud_input()), encoding="utf-8")

    assert GovCloudTemplateTransformer().transform(str(src), str(out)) is True

    reloaded = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert set(reloaded["Resources"]) == {"InputBucket"}
    assert "CloudFrontPriceClass" not in reloaded["Parameters"]
    assert reloaded.get("Conditions", {}) == {}
    assert reloaded.get("Outputs", {}) == {}
    assert reloaded["Parameters"]["WebUIHosting"]["Default"] == "APIGateway"
    assert reloaded["Parameters"]["WebUIHosting"]["AllowedValues"] == ["APIGateway"]
    assert reloaded["Description"].endswith("(GovCloud - CloudFront removed)")


def test_an_unconditioned_output_on_a_removed_distribution_fails_the_publish(tmp_path):
    """Pins an asymmetry in how the GovCloud transform prunes.

    `_remove_cloudfront_outputs` selects outputs by their `Condition`, not by
    what they reference, and `_prune_references_to` is driven from
    `_removed_logical_ids`, which the CloudFront removal path does not populate
    (only the Lambda-Web-Adapter path does). So an output that `Fn::GetAtt`s
    `CloudFrontDistribution` **without** a `Condition` survives a transform that
    deleted the distribution.

    This is caught rather than shipped: `validate_no_cloudfront` scans the
    serialised template for the removed logical ids and refuses, so `transform`
    returns False. The behaviour is fail-safe, and the test exists so that a
    future change which starts pruning by reference — or one which stops
    validating — is visible rather than silent. A failure meaning `True` would
    mean a template with a dangling `Fn::GetAtt` reached `cfn-lint` and deploy.
    """
    template = _govcloud_input()
    del template["Outputs"]["Url"]["Condition"]
    src = tmp_path / "in.yaml"
    out = tmp_path / "out.yaml"
    src.write_text(yaml.dump(template), encoding="utf-8")

    assert GovCloudTemplateTransformer().transform(str(src), str(out)) is False

    # The dangling output really is what survived.
    assert "CloudFrontDistribution" in out.read_text(encoding="utf-8")


def test_govcloud_transform_returns_false_when_the_input_is_absent(tmp_path):
    assert (
        GovCloudTemplateTransformer().transform(
            str(tmp_path / "nope.yaml"), str(tmp_path / "out.yaml")
        )
        is False
    )


def test_govcloud_transform_returns_false_when_validation_fails(tmp_path, monkeypatch):
    """The publish gate again: if a CloudFront type survives, the transform must
    report failure rather than hand cfn-lint a template that raises E3006.

    `apply_transforms` is stubbed to a pass-through so the validator sees the
    untransformed input — that is the only way to reach the failure verdict
    through the file entry point without a defect in the transform itself.
    """
    transformer = GovCloudTemplateTransformer()
    monkeypatch.setattr(transformer, "apply_transforms", lambda template: template)
    src = tmp_path / "in.yaml"
    out = tmp_path / "out.yaml"
    src.write_text(yaml.dump(_govcloud_input()), encoding="utf-8")

    assert transformer.transform(str(src), str(out)) is False


def test_govcloud_load_template_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        GovCloudTemplateTransformer().load_template(str(tmp_path / "absent.yaml"))


def test_govcloud_save_template_creates_missing_parent_directory(tmp_path):
    out = tmp_path / "a" / "b" / "out.yaml"

    GovCloudTemplateTransformer().save_template({"Resources": {}}, str(out))

    assert out.is_file()


def test_govcloud_save_template_to_a_bare_filename_stays_in_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    GovCloudTemplateTransformer().save_template({"Resources": {}}, "bare.yaml")

    assert (tmp_path / "bare.yaml").is_file()


# --------------------------------------------------------------------------
# GovCloud: validate_no_cloudfront failure verdicts
# --------------------------------------------------------------------------


def test_validate_no_cloudfront_rejects_a_surviving_lambda_url():
    """`AWS::Lambda::Url` does not exist in GovCloud either, and it is not a
    `CloudFront::` type, so it needs its own membership check.
    """
    transformer = GovCloudTemplateTransformer()

    assert (
        transformer.validate_no_cloudfront(
            {"Resources": {"Url": {"Type": "AWS::Lambda::Url"}}}
        )
        is False
    )


def test_validate_no_cloudfront_rejects_a_dangling_condition_reference():
    transformer = GovCloudTemplateTransformer()
    template = {
        "Resources": {"B": {"Type": "AWS::S3::Bucket"}},
        "Conditions": {"UseCloudFrontHosting": {"Fn::Equals": ["a", "b"]}},
    }

    assert transformer.validate_no_cloudfront(template) is False


def test_validate_no_cloudfront_rejects_a_leftover_reference_to_a_removed_id():
    """`_removed_logical_ids` is populated by the transform; the validator then
    proves nothing still names them. Word-boundary matching is what makes this
    usable, and the next test pins the other half of that rule.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = {"ChatStreamFunction"}
    template = {
        "Resources": {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": "p",
                            "PolicyDocument": {
                                "Statement": [
                                    {
                                        "Action": "lambda:InvokeFunction",
                                        "Resource": {
                                            "Fn::GetAtt": ["ChatStreamFunction", "Arn"]
                                        },
                                    }
                                ]
                            },
                        }
                    ]
                },
            }
        }
    }

    assert transformer.validate_no_cloudfront(template) is False


def test_a_surviving_id_that_merely_contains_a_removed_one_is_not_a_dangling_ref():
    """The case a raw substring scan gets wrong.

    `ChatStreamFunctionAlarm` survives and contains the removed
    `ChatStreamFunction`, so a substring scan would fail the publish for a
    non-reason. The validator falls back to structural reference detection, and
    nothing here actually `Ref`s the removed id — so the verdict must be True.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = {"ChatStreamFunction"}
    template = {
        "Resources": {
            "ChatStreamFunctionAlarm": {
                "Type": "AWS::CloudWatch::Alarm",
                "Properties": {"AlarmName": "alarm"},
            }
        }
    }

    assert transformer.validate_no_cloudfront(template) is True


def test_a_surviving_containing_id_still_fails_when_a_real_reference_remains():
    """The other side of the same branch: the fallback is a *structural* check,
    not an unconditional pass. With `ChatStreamFunctionAlarm` present AND a real
    `Ref` to the removed function, the verdict must still be False — otherwise
    naming a surviving resource after a removed one would disable the check.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = {"ChatStreamFunction"}
    template = {
        "Resources": {
            "ChatStreamFunctionAlarm": {"Type": "AWS::CloudWatch::Alarm"},
            "Perm": {
                "Type": "AWS::Lambda::Permission",
                "Properties": {"FunctionName": {"Ref": "ChatStreamFunction"}},
            },
        }
    }

    assert transformer.validate_no_cloudfront(template) is False


def test_validate_no_cloudfront_rejects_an_empty_inline_policy_statement_list():
    """IAM answers 400 "Syntax errors in policy" for `Statement: []`, and the
    stack then rolls back somewhere unrelated-looking. Catching it here is the
    difference between a failed publish and a confusing deploy failure.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Resources": {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {"PolicyName": "p", "PolicyDocument": {"Statement": []}}
                    ]
                },
            }
        }
    }

    assert transformer.validate_no_cloudfront(template) is False


def test_validate_no_cloudfront_rejects_an_empty_standalone_policy_document():
    transformer = GovCloudTemplateTransformer()
    template = {
        "Resources": {
            "MP": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {"PolicyDocument": {"Statement": []}},
            }
        }
    }

    assert transformer.validate_no_cloudfront(template) is False


def test_validate_no_cloudfront_tolerates_non_dict_resources_and_properties():
    """Malformed-but-parseable YAML must not crash the validator: a scalar
    resource, and a resource whose `Properties` is a string.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Resources": {
            "Scalar": "not-a-dict",
            "OddProps": {"Type": "AWS::S3::Bucket", "Properties": "nope"},
        }
    }

    assert transformer.validate_no_cloudfront(template) is True


# --------------------------------------------------------------------------
# GovCloud: structural pruning branches
# --------------------------------------------------------------------------


def test_prune_references_to_with_an_empty_removal_set_is_a_no_op():
    transformer = GovCloudTemplateTransformer()
    template = {"Resources": {"A": {"Type": "AWS::S3::Bucket"}}}

    transformer._prune_references_to(template, set())

    assert template == {"Resources": {"A": {"Type": "AWS::S3::Bucket"}}}


def test_prune_references_to_clears_a_scalar_dependson_and_trims_a_list():
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = set()
    template = {
        "Resources": {
            "ScalarDep": {"Type": "AWS::S3::Bucket", "DependsOn": "Gone"},
            "ListDep": {"Type": "AWS::S3::Bucket", "DependsOn": ["Gone", "Kept"]},
            "AllGoneDep": {"Type": "AWS::S3::Bucket", "DependsOn": ["Gone"]},
            "Kept": {"Type": "AWS::S3::Bucket"},
        }
    }

    transformer._prune_references_to(template, {"Gone"})

    resources = template["Resources"]
    assert "DependsOn" not in resources["ScalarDep"]
    assert resources["ListDep"]["DependsOn"] == ["Kept"]
    assert "DependsOn" not in resources["AllGoneDep"]


def test_prune_references_to_leaves_an_intentionally_empty_policies_list_alone():
    """Pruning must not rewrite a resource it did not change. An unconditional
    rewrite widened the published GovCloud diff and made the transform harder to
    audit, which is why `Policies: []` has to survive untouched.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = set()
    template = {
        "Resources": {
            "Role": {"Type": "AWS::IAM::Role", "Properties": {"Policies": []}},
            "Other": {
                "Type": "AWS::IAM::Role",
                "Properties": {"Policies": ["not-a-dict", {"NoPolicyDocument": 1}]},
            },
        }
    }

    transformer._prune_references_to(template, {"Gone"})

    assert template["Resources"]["Role"]["Properties"]["Policies"] == []
    assert template["Resources"]["Other"]["Properties"]["Policies"] == [
        "not-a-dict",
        {"NoPolicyDocument": 1},
    ]


def test_a_statementless_policy_resource_is_not_deleted_by_pruning():
    """ "Only remove the resource if pruning is what emptied it." A policy that
    arrived with no statements is not this transform's to delete.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = set()
    template = {
        "Resources": {
            "MP": {
                "Type": "AWS::IAM::ManagedPolicy",
                "Properties": {"PolicyDocument": {"Statement": []}},
            }
        }
    }

    transformer._prune_references_to(template, {"Gone"})

    assert "MP" in template["Resources"]


def test_prune_statements_accepts_a_single_dict_statement():
    transformer = GovCloudTemplateTransformer()
    transformer._reset_policy_statement_removals()
    doc = {"Statement": {"Resource": {"Ref": "Gone"}}}

    assert transformer._prune_statements(doc, {"Gone"}, "Role.p") is False
    assert doc["Statement"] == []


def test_prune_statements_leaves_a_non_list_statement_alone():
    transformer = GovCloudTemplateTransformer()
    doc = {"Statement": "wat"}

    assert transformer._prune_statements(doc, {"Gone"}, "Role.p") is True
    assert doc["Statement"] == "wat"


def test_prune_statements_keeps_a_non_object_entry():
    transformer = GovCloudTemplateTransformer()
    transformer._reset_policy_statement_removals()
    doc = {"Statement": ["scalar", {"Resource": {"Ref": "Gone"}}]}

    assert transformer._prune_statements(doc, {"Gone"}, "Role.p") is True
    assert doc["Statement"] == ["scalar"]


def test_node_references_with_an_empty_name_set_is_false():
    """The short-circuit guard. Without it, `_prune_references_to` called with
    nothing removed would walk the whole template for no reason — and, worse,
    `any(... for n in set())` is False, so a missing guard is silent.
    """
    assert GovCloudTemplateTransformer._node_references({"Ref": "X"}, set()) is False


def test_node_references_matches_a_dotted_getatt_string():
    """`Fn::GetAtt` may be written as `"Fn.Attribute"` rather than a two-element
    list. Both spellings mean the same thing to CloudFormation.
    """
    assert (
        GovCloudTemplateTransformer._node_references(
            {"Fn::GetAtt": "Gone.Arn"}, {"Gone"}
        )
        is True
    )


def test_node_references_matches_an_fn_sub_given_as_a_list():
    assert (
        GovCloudTemplateTransformer._node_references(
            {"Fn::Sub": ["${Gone}", {"X": "y"}]}, {"Gone"}
        )
        is True
    )


def test_node_references_does_not_match_a_longer_logical_id_in_an_fn_sub():
    """`${FooAlarm}` is not a reference to `Foo`. This is the prefix trap: a
    transform that matched it would strip the surviving alarm's IAM statement.
    """
    assert (
        GovCloudTemplateTransformer._node_references(
            {"Fn::Sub": "${FooAlarm}"}, {"Foo"}
        )
        is False
    )
    assert (
        GovCloudTemplateTransformer._node_references({"Fn::Sub": "${Foo.Arn}"}, {"Foo"})
        is True
    )


def test_blank_function_url_refs_handles_getatt_and_fn_sub_and_counts_them():
    """The UI reads `VITE_STREAM_URL` from the Function URL. In GovCloud the URL
    resource is removed, so every reference must become an empty string — a
    dangling `Fn::GetAtt` would be rejected outright, and a surviving
    `${Url.FunctionUrl}` token would render literally into the UI's config.
    """
    transformer = GovCloudTemplateTransformer()
    node = {
        "Direct": {"Fn::GetAtt": ["StreamUrl", "FunctionUrl"]},
        "Dotted": {"Fn::GetAtt": "StreamUrl.FunctionUrl"},
        "Subbed": {"Fn::Sub": "prefix-${StreamUrl.FunctionUrl}-suffix"},
        "SubbedList": {"Fn::Sub": ["${StreamUrl.FunctionUrl}", {}]},
        "Nested": [{"Deep": {"Fn::GetAtt": ["StreamUrl", "FunctionUrl"]}}],
        "Untouched": {"Fn::GetAtt": ["StreamUrl", "Arn"]},
    }

    count = transformer._blank_function_url_refs(node, {"StreamUrl"})

    assert node["Direct"] == ""
    assert node["Dotted"] == ""
    assert node["Subbed"] == "prefix--suffix"
    assert node["Nested"][0]["Deep"] == ""
    assert node["Untouched"] == {"Fn::GetAtt": ["StreamUrl", "Arn"]}
    # Five blanked: Direct, Dotted, Subbed, SubbedList and the nested one. The
    # count is asserted because it is what the transform logs as its evidence.
    assert count == 5


def test_uses_lwa_layer_rejects_non_function_resources_and_missing_layers():
    """Three negative shapes in one place. A false positive here deletes a
    function GovCloud can run perfectly well.
    """
    cls = GovCloudTemplateTransformer

    assert cls._uses_lwa_layer("not-a-dict") is False
    assert cls._uses_lwa_layer({"Type": "AWS::S3::Bucket"}) is False
    assert cls._uses_lwa_layer({"Type": "AWS::Lambda::Function", "Properties": {}}) is (
        False
    )


def test_uses_lwa_layer_matches_the_layer_behind_an_fn_if():
    """The real template hides the layer ARN behind an `Fn::If` choosing between
    a parameter override and the published default, so the match is against the
    serialised subtree rather than a fixed shape.
    """
    resource = {
        "Type": "AWS::Serverless::Function",
        "Properties": {
            "Layers": [
                {
                    "Fn::If": [
                        "HasLambdaWebAdapterLayerArn",
                        {"Ref": "LambdaWebAdapterLayerArn"},
                        "arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerX86:25",
                    ]
                }
            ]
        },
    }

    assert GovCloudTemplateTransformer._uses_lwa_layer(resource) is True


def test_lwa_parameter_survives_while_a_surviving_resource_still_references_it():
    """`_parameter_still_used` probes everything except `Parameters` and
    `Metadata`. A parameter a surviving resource still references must not be
    deleted, or the template stops resolving.

    The condition goes in the same call — nothing outside the `Conditions`
    section names it — which is the correct outcome and is asserted so the two
    decisions are visibly independent of each other.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Parameters": {"LambdaWebAdapterLayerArn": {"Type": "String"}},
        "Conditions": {
            "HasLambdaWebAdapterLayerArn": {
                "Fn::Not": [{"Fn::Equals": [{"Ref": "LambdaWebAdapterLayerArn"}, ""]}]
            }
        },
        "Resources": {
            "Survivor": {
                "Type": "AWS::Serverless::Function",
                "Properties": {"Layers": [{"Ref": "LambdaWebAdapterLayerArn"}]},
            }
        },
    }

    transformer._remove_lwa_parameters_and_conditions(template)

    assert "LambdaWebAdapterLayerArn" in template["Parameters"]
    assert template["Conditions"] == {}


def test_lwa_condition_survives_while_a_resource_still_branches_on_it():
    """A condition a surviving resource still uses in an `Fn::If` must stay, or
    CloudFormation rejects the template for naming an undefined condition. The
    parameter then stays too, because the surviving condition references it.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Parameters": {"LambdaWebAdapterLayerArn": {"Type": "String"}},
        "Conditions": {
            "HasLambdaWebAdapterLayerArn": {
                "Fn::Not": [{"Fn::Equals": [{"Ref": "LambdaWebAdapterLayerArn"}, ""]}]
            }
        },
        "Resources": {
            "Survivor": {
                "Type": "AWS::S3::Bucket",
                "Properties": {
                    "Tags": {
                        "Fn::If": [
                            "HasLambdaWebAdapterLayerArn",
                            [],
                            {"Ref": "AWS::NoValue"},
                        ]
                    }
                },
            }
        },
    }

    transformer._remove_lwa_parameters_and_conditions(template)

    assert "HasLambdaWebAdapterLayerArn" in template["Conditions"]
    assert "LambdaWebAdapterLayerArn" in template["Parameters"]


def test_dead_lwa_parameter_and_condition_are_removed_with_their_console_labels():
    """Also pins the *order* of the two removals, which is load-bearing.

    `HasLambdaWebAdapterLayerArn` contains `LambdaWebAdapterLayerArn` as a
    substring, and the condition's own body `Ref`s the parameter. If the
    parameter were probed first, the still-present condition would make it look
    used and it would survive with nothing to configure. Conditions are
    therefore removed first; a regression to the other order leaves
    `LambdaWebAdapterLayerArn` in `Parameters` and fails here.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Parameters": {
            "LambdaWebAdapterLayerArn": {"Type": "String"},
            "Keep": {"Type": "String"},
        },
        "Conditions": {
            "HasLambdaWebAdapterLayerArn": {
                "Fn::Not": [{"Fn::Equals": [{"Ref": "LambdaWebAdapterLayerArn"}, ""]}]
            }
        },
        "Resources": {"B": {"Type": "AWS::S3::Bucket"}},
        "Metadata": {
            "AWS::CloudFormation::Interface": {
                "ParameterGroups": [
                    {"Parameters": ["LambdaWebAdapterLayerArn", "Keep"]}
                ],
                "ParameterLabels": {
                    "LambdaWebAdapterLayerArn": {"default": "LWA layer"},
                    "Keep": {"default": "Keep"},
                },
            }
        },
    }

    transformer._remove_lwa_parameters_and_conditions(template)

    assert "LambdaWebAdapterLayerArn" not in template["Parameters"]
    assert template["Conditions"] == {}
    interface = template["Metadata"]["AWS::CloudFormation::Interface"]
    assert interface["ParameterGroups"][0]["Parameters"] == ["Keep"]
    assert "LambdaWebAdapterLayerArn" not in interface["ParameterLabels"]


def test_is_cloudfront_service_statement_reads_both_principal_spellings():
    cls = GovCloudTemplateTransformer

    assert (
        cls._is_cloudfront_service_statement(
            {"Principal": {"Service": "cloudfront.amazonaws.com"}}
        )
        is True
    )
    assert (
        cls._is_cloudfront_service_statement(
            {"Principal": {"Service": {"Fn::Sub": "cloudfront.${AWS::URLSuffix}"}}}
        )
        is True
    )
    assert (
        cls._is_cloudfront_service_statement(
            {"Principal": {"Service": "s3.amazonaws.com"}}
        )
        is False
    )
    assert cls._is_cloudfront_service_statement({}) is False


def test_removal_record_is_reset_between_two_transforms_of_one_instance():
    """`publish.py` reuses a transformer across templates. If the record
    accreted, the second template's audit would report the first one's removals.
    """
    transformer = HeadlessTemplateTransformer()
    template = {
        "Resources": {
            "P": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {"Principal": {"Service": "cloudfront.amazonaws.com"}}
                        ]
                    }
                },
            }
        }
    }
    transformer._reset_policy_statement_removals()
    transformer._clean_cloudfront_policy_statements(template)
    assert len(transformer.policy_statement_removals) == 1

    transformer._reset_policy_statement_removals()

    assert transformer.policy_statement_removals == []


def test_removal_record_is_lazily_created_on_a_fresh_instance():
    """The record is created on first read so the two transformers need no
    `__init__` cooperation; a caller may read it before any transform runs.
    """
    assert HeadlessTemplateTransformer().policy_statement_removals == []


def test_zero_removals_are_summarised_explicitly(caplog):
    """ "Removed 0 policy statements" is deliberate: silence would be
    indistinguishable from a summary that never ran.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()

    with caplog.at_level("INFO", logger="idp_sdk._core.template_transform"):
        transformer._log_policy_statement_removal_summary()

    assert "Removed 0 policy statements" in caplog.text


def test_headless_apply_transforms_can_opt_into_the_govcloud_config_defaults():
    """`publish.py --headless --govcloud` sets this flag; without it the headless
    template keeps the commercial `ConfigurationPreset`, whose model ids do not
    exist in a GovCloud region. The flag is off by default, and both states are
    asserted so a default flip is visible.
    """
    transformer = HeadlessTemplateTransformer()

    without = transformer.apply_transforms(_valid_headless_input())
    with_govcloud = transformer.apply_transforms(
        _valid_headless_input(), update_govcloud_config=True
    )

    assert without["Parameters"]["ConfigurationPreset"]["Default"] == (
        "lending-package-sample"
    )
    assert with_govcloud["Parameters"]["ConfigurationPreset"]["Default"] == (
        "lending-package-sample-govcloud"
    )


def test_a_policy_whose_entire_statement_list_dies_is_dropped():
    """The list branch's all-dead case. Returning the policy with
    `Statement: []` instead would reach IAM, which answers 400.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()
    policy = {
        "Statement": [
            {"Action": "appsync:GraphQL", "Resource": "*"},
            {
                "Action": "secretsmanager:GetSecretValue",
                "Resource": {"Ref": "ExternalMCPAgentsSecret"},
            },
        ]
    }

    assert transformer._clean_policy_statements(policy, "Fn") is None
    (record,) = transformer.policy_statement_removals
    assert record.removed == 2
    # Both reasons are reported, de-duplicated and sorted.
    assert "; " in record.reason


def test_govcloud_transform_verbose_failure_logs_a_traceback(tmp_path, caplog):
    transformer = GovCloudTemplateTransformer(verbose=True)

    with caplog.at_level("DEBUG", logger="idp_sdk._core.template_transform"):
        assert (
            transformer.transform(
                str(tmp_path / "nope.yaml"), str(tmp_path / "out.yaml")
            )
            is False
        )

    assert any("Traceback" in record.message for record in caplog.records)


def test_an_unnamed_cloudfront_typed_resource_is_removed_by_type():
    """The defensive second pass.

    `CLOUDFRONT_RESOURCES` is a fixed list of logical ids. A CloudFront resource
    added to the template under a new name would otherwise survive and raise
    cfn-lint E3006 in GovCloud, so the transform also sweeps by resource type.
    A failure here means adding a CloudFront resource to `template.yaml` breaks
    the GovCloud publish silently until someone updates the list.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Resources": {
            "SomeNewFunctionAssociation": {"Type": "AWS::CloudFront::Function"},
            "NotADict": "scalar",
            "InputBucket": {"Type": "AWS::S3::Bucket"},
        }
    }

    transformer._remove_cloudfront_resources(template)

    assert set(template["Resources"]) == {"NotADict", "InputBucket"}


def test_a_log_group_still_used_elsewhere_survives_the_lwa_removal():
    """The retention branch: a removed function's log group is only deleted when
    nothing else names it. Deleting a shared log group would take another
    function's logging configuration with it, and CloudFormation would reject
    the template for the dangling reference.
    """
    transformer = GovCloudTemplateTransformer()
    template = {
        "Resources": {
            "StreamFn": {
                "Type": "AWS::Serverless::Function",
                "Properties": {
                    "Layers": [
                        "arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerX86:25"
                    ],
                    "LoggingConfig": {"LogGroup": {"Ref": "SharedLogGroup"}},
                },
            },
            "SharedLogGroup": {"Type": "AWS::Logs::LogGroup"},
            "OtherFn": {
                "Type": "AWS::Serverless::Function",
                "Properties": {
                    "LoggingConfig": {"LogGroup": {"Ref": "SharedLogGroup"}}
                },
            },
        }
    }

    transformer._remove_lwa_dependent_functions(template)

    assert "StreamFn" not in template["Resources"]
    assert "SharedLogGroup" in template["Resources"]
    assert "SharedLogGroup" not in transformer._removed_logical_ids


def test_govcloud_policy_statement_sweep_skips_shapes_it_cannot_read():
    """A scalar resource and a `PolicyDocument` that is not a dict must not
    raise — a template that merely fails to match is left alone.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._reset_policy_statement_removals()
    template = {
        "Resources": {
            "Scalar": "not-a-dict",
            "OddDoc": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {"PolicyDocument": "nope"},
            },
            "Real": {
                "Type": "AWS::S3::BucketPolicy",
                "Properties": {
                    "PolicyDocument": {
                        "Statement": [
                            {"Principal": {"Service": "cloudfront.amazonaws.com"}},
                            {"Principal": {"Service": "logging.s3.amazonaws.com"}},
                        ]
                    }
                },
            },
        }
    }

    transformer._remove_cloudfront_policy_statements(template)

    kept = template["Resources"]["Real"]["Properties"]["PolicyDocument"]["Statement"]
    assert kept == [{"Principal": {"Service": "logging.s3.amazonaws.com"}}]


def test_prune_references_to_skips_a_scalar_resource_and_scalar_properties():
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = set()
    template = {
        "Resources": {
            "Scalar": "not-a-dict",
            "OddProps": {"Type": "AWS::S3::Bucket", "Properties": "nope"},
        }
    }

    transformer._prune_references_to(template, {"Gone"})

    assert set(template["Resources"]) == {"Scalar", "OddProps"}


def test_a_role_whose_every_inline_policy_dies_loses_the_policies_key():
    """`Policies: []` on a role is not the same as no `Policies` key: an empty
    list is what IAM rejects. The key must be popped, not emptied.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = set()
    template = {
        "Resources": {
            "Role": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "Policies": [
                        {
                            "PolicyName": "invoke",
                            "PolicyDocument": {
                                "Statement": [{"Resource": {"Ref": "Gone"}}]
                            },
                        }
                    ]
                },
            }
        }
    }

    transformer._prune_references_to(template, {"Gone"})

    assert "Policies" not in template["Resources"]["Role"]["Properties"]


def test_pruning_removes_an_output_that_references_a_removed_resource():
    transformer = GovCloudTemplateTransformer()
    transformer._removed_logical_ids = set()
    template = {
        "Resources": {},
        "Outputs": {
            "Dangling": {"Value": {"Fn::GetAtt": ["Gone", "Arn"]}},
            "Fine": {"Value": "static"},
        },
    }

    transformer._prune_references_to(template, {"Gone"})

    assert set(template["Outputs"]) == {"Fine"}


def test_a_statement_whose_resource_list_is_entirely_removed_is_dropped():
    """Distinguishes dropping from narrowing on a list-valued `Resource`.

    With no surviving entry the statement goes (an Allow with an empty
    `Resource` is invalid); with one surviving entry it is narrowed and kept.
    Both are in one test because the two outcomes share a branch and a fixture
    that only exercised one would leave the other unproven.
    """
    transformer = GovCloudTemplateTransformer()
    transformer._reset_policy_statement_removals()
    doc = {
        "Statement": [
            {"Sid": "AllGone", "Resource": [{"Ref": "Gone"}, {"Ref": "AlsoGone"}]},
            {"Sid": "Partial", "Resource": [{"Ref": "Gone"}, {"Ref": "Kept"}]},
        ]
    }

    assert transformer._prune_statements(doc, {"Gone", "AlsoGone"}, "Role.p") is True

    assert doc["Statement"] == [{"Sid": "Partial", "Resource": [{"Ref": "Kept"}]}]
    (record,) = transformer.policy_statement_removals
    assert (record.removed, record.narrowed) == (1, 1)


def test_a_removal_of_nothing_is_not_recorded():
    """`removed=0, narrowed=0` must not create a record, or every untouched
    policy document would appear in the audit as a removal of nothing.
    """
    transformer = HeadlessTemplateTransformer()
    transformer._reset_policy_statement_removals()

    transformer._record_policy_statement_removal("R", "reason", removed=0, narrowed=0)

    assert transformer.policy_statement_removals == []
