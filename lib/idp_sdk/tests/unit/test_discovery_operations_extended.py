# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The inside of `operations/discovery.py`: the four private workers and the helpers.

Discovery asks a Bedrock model to look at a document and write a JSON Schema for
it. `DiscoveryOperation` has two modes and four workers, and the existing suite
tests only the dispatch between them — every test in
`test_discovery_operations.py` patches `_run_with_stack`, `_run_local`,
`_auto_detect_with_stack` or `_auto_detect_local` and asserts an argument was
forwarded. That leaves the bodies of all four, which is where the work happens,
unmeasured.

This file tests those bodies. Three things shaped it.

**The request built for Bedrock is the product.** `_run_local` selects a model,
loads prompts from the system defaults, slices a page range out of the PDF and
assembles the Converse content blocks. A wrong answer there is not an exception —
it is a schema for the wrong pages, or a model that silently drops the document
and hallucinates. So the `BedrockClient` is replaced with a stand-in that records
the call, and the assertions are on **what was sent**: the model id, the
prompts, the sampling parameters, and — for the page-range case — the actual PDF
bytes in the document block, reopened with `pypdfium2` and counted. Building that
input with a real four-page PDF and letting the real
`ClassesDiscovery.extract_pdf_pages` do the slicing is what makes "pages 2-3
were extracted" a measurement rather than a mock assertion.

**The response parsing is real.** `_extract_json`, `_validate_json_schema` and
`idp_common`'s own `extract_text_from_response` are left unpatched, and the
stand-in returns Converse-shaped responses including the `reasoningContent` block
that reasoning models put *before* the text. A retry loop that cannot read a
fenced response, or that accepts a schema missing `x-aws-idp-document-type`,
fails here.

**Every worker swallows its exceptions into `status="FAILED"`.** That is the
right shape for a batch, and it also means a bug in any of them looks exactly
like a Bedrock problem. Each failure path is therefore tested for the *content*
of `error`, not just for the status.

`_get_config_table` resolution goes through real `moto` CloudFormation, so the
`CONFIGURATION_TABLE_NAME` the workers export is a physical table name from a
real stack.
"""

from __future__ import annotations

import io
import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import boto3
import pypdfium2 as pdfium
import pytest
from moto import mock_aws

from idp_sdk import IDPClient
from idp_sdk.models import (
    AutoDetectResult,
    DiscoveryBatchResult,
    DiscoveryResult,
    MultiDocDiscoveryResult,
)
from idp_sdk.operations.discovery import (
    _extract_json,
    _prompt_with_gt,
    _prompt_without_gt,
    _sample_output_format,
    _validate_json_schema,
)

STACK = "idp-discovery"

STACK_TEMPLATE = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Resources": {
        "ConfigurationTable": {
            "Type": "AWS::DynamoDB::Table",
            "Properties": {
                "KeySchema": [{"AttributeName": "Configuration", "KeyType": "HASH"}],
                "AttributeDefinitions": [
                    {"AttributeName": "Configuration", "AttributeType": "S"}
                ],
                "BillingMode": "PAY_PER_REQUEST",
            },
        }
    },
}

# A schema that passes `_validate_json_schema`: every required key, root type
# "object", and a dict of properties.
VALID_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "Invoice",
    "x-aws-idp-document-type": "Invoice",
    "type": "object",
    "description": "A commercial invoice",
    "properties": {"InvoiceNumber": {"type": "string", "description": "number"}},
}

# Model ids the guard in both local workers must refuse: Converse `document`
# blocks are what discovery sends, and these routes accept text and images only.
# Taken from `idp_common.bedrock.client.DOCUMENT_BLOCK_UNSUPPORTED_ROUTES`.
DOCUMENT_BLOCK_REFUSED = [
    "xai.grok-4-fast-reasoning-v1:0",
    "us.openai.gpt-5-2025-08-07",
]


@pytest.fixture
def discovery_env(monkeypatch):
    """Contain `CONFIGURATION_TABLE_NAME`, which the stack workers export.

    Set through monkeypatch first so its teardown restores the prior state
    (including absence); the production code writes it with a bare
    `os.environ[...] = ...`, which nothing else would undo, and a leaked value
    lets a later test read an earlier test's table.
    """
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "set-by-fixture")
    monkeypatch.delenv("CONFIGURATION_TABLE_NAME")


def _pdf(pages: int) -> bytes:
    """A blank multi-page PDF, built with the library production already uses.

    `pypdfium2` rather than `pypdf`: it is a declared dependency of
    `lib/idp_common_pkg` and is what `discovery.py` itself slices pages with, so
    this fixture needs nothing installed that the code under test does not. `pypdf`
    happens to be importable on a developer machine as somebody else's transitive
    dependency and is declared nowhere, which is green locally and
    `ModuleNotFoundError` in CI. Choosing the declared one also keeps the licence
    decision that moved this repo off AGPL-licensed PyMuPDF intact.
    """
    document = pdfium.PdfDocument.new()
    for _ in range(pages):
        document.new_page(200, 200)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


#: A minimal valid 1x1 PNG, written out byte for byte.
#:
#: Literal rather than generated with Pillow: these tests only need "a file that is
#: not a PDF", and Pillow is declared in `lib/idp_common_pkg`'s extras rather than in
#: `idp_sdk`'s, so whether it is importable depends on which extras the installing CI
#: chose. A fixture that needs a dependency the tree does not guarantee is a test that
#: passes on one machine and errors on another, for reasons unrelated to what it asserts.
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
    "de0000000c49444154789c63f8ffff3f0005fe02fe0def46b80000000049454e"
    "44ae426082"
)


def _png() -> bytes:
    return _PNG_1X1


def _converse_response(text: str) -> dict:
    """A Converse response shaped the way a reasoning model returns one.

    The `reasoningContent` block comes *before* the answer, which is why the real
    `extract_text_from_response` is used here rather than `content[0]["text"]`.
    """
    return {
        "output": {
            "message": {
                "content": [
                    {"reasoningContent": {"reasoningText": {"text": "thinking"}}},
                    {"text": text},
                ]
            }
        }
    }


def _fenced(payload) -> str:
    return "Here is the schema:\n```json\n" + json.dumps(payload) + "\n```\n"


def _bedrock(*responses):
    """A `BedrockClient` stand-in returning the given responses in order."""
    client = MagicMock()
    if len(responses) == 1:
        client.invoke_model.return_value = responses[0]
    else:
        client.invoke_model.side_effect = list(responses)
    return client


def _create_stack(region: str) -> str:
    cfn = boto3.client("cloudformation", region_name=region)
    cfn.create_stack(StackName=STACK, TemplateBody=json.dumps(STACK_TEMPLATE))
    return boto3.client("dynamodb", region_name=region).list_tables()["TableNames"][0]


class FakeClassesDiscovery:
    """Stand-in for `idp_common`'s `ClassesDiscovery`.

    Patched rather than faked with `moto`: the real class calls Bedrock and writes
    through the configuration layer, and this module's job is only to drive it and
    shape its answer. Every construction and call is recorded on the class so the
    tests can assert on the arguments.
    """

    constructions: list[dict] = []
    calls: list[tuple[str, dict]] = []
    result: dict = {"schema": VALID_SCHEMA}
    sections: list[dict] = []
    raise_on_version = False
    fail_with: Exception | None = None

    def __init__(self, **kwargs):
        type(self).constructions.append(kwargs)
        if type(self).raise_on_version and kwargs.get("version") is not None:
            raise RuntimeError("no such configuration version")
        self.version = kwargs.get("version")

    def _record(self, name, kwargs):
        type(self).calls.append((name, kwargs))
        if type(self).fail_with is not None:
            raise type(self).fail_with

    def discovery_classes_with_document(self, **kwargs):
        self._record("without_gt", kwargs)
        return type(self).result

    def discovery_classes_with_document_and_ground_truth(self, **kwargs):
        self._record("with_gt", kwargs)
        return type(self).result

    def auto_detect_sections(self, **kwargs):
        self._record("auto_detect", kwargs)
        return type(self).sections


@pytest.fixture
def fake_classes_discovery():
    """Install `FakeClassesDiscovery` with per-test state reset."""
    FakeClassesDiscovery.constructions = []
    FakeClassesDiscovery.calls = []
    FakeClassesDiscovery.result = {"schema": VALID_SCHEMA}
    FakeClassesDiscovery.sections = []
    FakeClassesDiscovery.raise_on_version = False
    FakeClassesDiscovery.fail_with = None
    with patch(
        "idp_common.discovery.classes_discovery.ClassesDiscovery",
        FakeClassesDiscovery,
    ):
        yield FakeClassesDiscovery


# --------------------------------------------------------------------------
# Response helpers
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestExtractJson:
    """Stripping the markdown fence a chat model wraps its JSON in."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ('```json\n{"a": 1}\n```', '{"a": 1}'),
            ('```\n{"a": 1}\n```', '{"a": 1}'),
            ('prose before\n```json\n{"a": 1}\n```\nprose after', '{"a": 1}'),
            ('{"a": 1}', '{"a": 1}'),
            ('```json{"a": 1}```', '{"a": 1}'),
        ],
    )
    def test_the_fence_is_removed_and_bare_json_is_left_alone(self, raw, expected):
        assert json.loads(_extract_json(raw)) == json.loads(expected)

    def test_only_the_first_fenced_block_is_taken(self):
        """A model that explains itself twice must not produce concatenated JSON."""
        raw = '```json\n{"first": 1}\n```\nand also\n```json\n{"second": 2}\n```'
        assert json.loads(_extract_json(raw)) == {"first": 1}

    def test_an_unterminated_fence_is_returned_unchanged(self):
        """No closing fence means no match, so the text passes through.

        The caller then fails in `json.loads`, which is the retry loop's cue to
        ask again — the right outcome for a truncated response.
        """
        raw = '```json\n{"a": 1}'
        assert _extract_json(raw) == raw


@pytest.mark.unit
class TestValidateJsonSchema:
    """What the retry loop accepts as a usable JSON Schema."""

    def test_a_complete_schema_is_accepted_with_no_message(self):
        assert _validate_json_schema(VALID_SCHEMA) == (True, "")

    @pytest.mark.parametrize(
        "missing", ["$schema", "$id", "type", "properties", "x-aws-idp-document-type"]
    )
    def test_every_required_key_is_required(self, missing):
        """Naming the missing key is what the retry prompt feeds back to the model."""
        schema = {k: v for k, v in VALID_SCHEMA.items() if k != missing}

        valid, message = _validate_json_schema(schema)

        assert valid is False
        assert missing in message

    def test_a_root_type_other_than_object_is_refused(self):
        """A schema for an array of documents is not a document class."""
        valid, message = _validate_json_schema({**VALID_SCHEMA, "type": "array"})

        assert (valid, message) == (False, "Root type must be 'object'")

    @pytest.mark.parametrize("properties", [[], "InvoiceNumber", None, 3])
    def test_properties_must_be_a_mapping(self, properties):
        """An empty *dict* is fine; a list or a string is not.

        The pipeline indexes `properties` by attribute name, so a list here would
        get past validation and fail much later, inside extraction.
        """
        valid, message = _validate_json_schema(
            {**VALID_SCHEMA, "properties": properties}
        )

        assert (valid, message) == (False, "Properties must be an object")

    def test_an_empty_properties_mapping_is_accepted(self):
        assert _validate_json_schema({**VALID_SCHEMA, "properties": {}}) == (True, "")

    def test_the_missing_key_check_runs_before_the_type_checks(self):
        """An empty dict reports the first absent key, not "root type".

        The order matters because the message becomes the retry instruction, and
        "Root type must be 'object'" for a response that contained no keys at all
        would send the model in the wrong direction.
        """
        valid, message = _validate_json_schema({})

        assert valid is False
        assert message == "Missing required field: $schema"


@pytest.mark.unit
class TestPromptFallbacks:
    """The prompts used when the system defaults carry none."""

    def test_the_sample_format_is_a_valid_schema_shaped_example(self):
        """It is pasted verbatim into the prompt, so it has to parse.

        A syntax error here would teach the model to emit the same broken JSON,
        and the retry loop would burn all three attempts on it.
        """
        sample = json.loads(_sample_output_format())

        assert _validate_json_schema(sample) == (True, "")
        assert sample["properties"]["Dependents"]["type"] == "array"

    def test_the_no_ground_truth_prompt_asks_for_structure_not_values(self):
        prompt = _prompt_without_gt()

        assert "Do not extract the actual values" in prompt
        assert "x-aws-idp-document-type" in prompt

    def test_the_ground_truth_prompt_embeds_the_ground_truth(self):
        """The data is inlined, so a caller's field names reach the model."""
        prompt = _prompt_with_gt({"InvoiceNumber": "INV-1", "Lines": [{"Qty": 2}]})

        assert "<GROUND_TRUTH_REFERENCE>" in prompt
        assert '"InvoiceNumber": "INV-1"' in prompt
        assert "Preserve the exact field names" in prompt


# --------------------------------------------------------------------------
# _run_with_stack
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestRunWithStack:
    @mock_aws
    def test_the_config_table_is_resolved_and_exported_before_the_discovery_runs(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """`ClassesDiscovery` reads `CONFIGURATION_TABLE_NAME` from the environment.

        It takes no table argument, so exporting the physical name resolved from
        CloudFormation is the only way the stack's own configuration is reached.
        Without it the discovery reads whatever table the ambient environment
        names — in a Lambda-shaped environment, a different stack's.
        """
        table = _create_stack(aws_credentials)
        document = tmp_path / "invoice.pdf"
        document.write_bytes(_pdf(1))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document)
        )

        assert result.status == "SUCCESS"
        assert os.environ["CONFIGURATION_TABLE_NAME"] == table
        assert fake_classes_discovery.constructions[0] == {
            "input_bucket": "local",
            "input_prefix": "invoice.pdf",
            "region": aws_credentials,
            "version": None,
        }

    @mock_aws
    def test_the_document_bytes_are_passed_and_no_bucket_is_used(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """`input_bucket="local"` is a sentinel: nothing is uploaded to S3.

        The file's bytes travel in the call. A regression that started uploading
        would put customer documents in a bucket the caller never named.
        """
        _create_stack(aws_credentials)
        content = _pdf(2)
        document = tmp_path / "invoice.pdf"
        document.write_bytes(content)

        IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(str(document))

        name, kwargs = fake_classes_discovery.calls[0]
        assert name == "without_gt"
        assert kwargs["file_bytes"] == content
        assert kwargs["input_bucket"] == "local"
        assert kwargs["input_prefix"] == "invoice.pdf"

    @mock_aws
    def test_nothing_is_saved_unless_a_profile_was_named(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """`save_to_config` follows from `config_version`, and that is deliberate.

        Discovery without a named profile is exploratory — the caller is looking
        at a schema, not adopting it. Saving by default would mutate the stack's
        live configuration on a read-only-looking call.
        """
        _create_stack(aws_credentials)
        document = tmp_path / "invoice.pdf"
        document.write_bytes(_pdf(1))
        operation = IDPClient(stack_name=STACK, region=aws_credentials).discovery

        operation.run(str(document))
        operation.run(str(document), config_version="v2")

        assert [call[1]["save_to_config"] for call in fake_classes_discovery.calls] == [
            False,
            True,
        ]

    @mock_aws
    def test_a_missing_profile_falls_back_to_the_active_config_and_still_targets_it(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """Discovering *into* a profile that does not exist yet has to work.

        `ClassesDiscovery(version="v9")` refuses an unknown profile, so the
        operation retries against the active configuration and then re-points the
        instance at `v9` before saving. The two assertions that matter are that
        the second construction asked for `version=None` (so the read succeeded)
        and that `discovery.version` ended up as `v9` (so the write lands in the
        profile the caller asked to create). Getting the second wrong would write
        the schema into whatever profile is currently active.
        """
        _create_stack(aws_credentials)
        fake_classes_discovery.raise_on_version = True
        document = tmp_path / "w2.pdf"
        document.write_bytes(_pdf(1))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document), config_version="v9"
        )

        assert result.status == "SUCCESS"
        assert result.config_version == "v9"
        versions = [c["version"] for c in fake_classes_discovery.constructions]
        assert versions == ["v9", None], "the retry must not ask for v9 again"
        assert fake_classes_discovery.calls[0][1]["save_to_config"] is True

    @mock_aws
    def test_a_failure_constructing_without_a_profile_is_not_retried(
        self, aws_credentials, discovery_env, tmp_path
    ):
        """With no `config_version` there is no fallback, so the error surfaces.

        Retrying here would loop, and swallowing it would report a discovery that
        never ran.
        """
        _create_stack(aws_credentials)

        class AlwaysFails(FakeClassesDiscovery):
            def __init__(self, **kwargs):
                raise RuntimeError("configuration table is unreadable")

        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))

        with patch(
            "idp_common.discovery.classes_discovery.ClassesDiscovery", AlwaysFails
        ):
            result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
                str(document)
            )

        assert result.status == "FAILED"
        assert result.error is not None
        assert "configuration table is unreadable" in result.error
        assert result.document_path == str(document)

    @mock_aws
    def test_ground_truth_selects_the_other_entry_point(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """Two different prompts and two different methods on the far side."""
        _create_stack(aws_credentials)
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        truth = tmp_path / "a.json"
        truth.write_text(json.dumps({"InvoiceNumber": "INV-1"}))

        IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document), ground_truth_path=str(truth)
        )

        name, kwargs = fake_classes_discovery.calls[0]
        assert name == "with_gt"
        assert kwargs["ground_truth_data"] == {"InvoiceNumber": "INV-1"}

    @mock_aws
    @pytest.mark.parametrize(
        ("schema", "expected"),
        [
            ({"$id": "FromId", "x-aws-idp-document-type": "FromType"}, "FromId"),
            ({"x-aws-idp-document-type": "FromType"}, "FromType"),
            ({"properties": {}}, None),
        ],
    )
    def test_the_class_name_prefers_id_then_falls_back_to_the_idp_type(
        self,
        aws_credentials,
        discovery_env,
        fake_classes_discovery,
        tmp_path,
        schema,
        expected,
    ):
        """`$id` is the canonical name; the vendor extension is the fallback.

        A schema carrying neither yields `None` rather than an invented name — a
        made-up class would be saved under a name nothing else refers to.
        """
        _create_stack(aws_credentials)
        fake_classes_discovery.result = {"schema": schema}
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document)
        )

        assert result.document_class == expected
        assert result.json_schema == schema

    @mock_aws
    def test_a_result_without_a_schema_is_still_a_success_with_no_class(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """The worker reports what it got; it does not invent a failure.

        A caller checks `json_schema`, and conflating "no schema returned" with
        "the call failed" would hide the difference between a model that produced
        nothing and one that errored.
        """
        _create_stack(aws_credentials)
        fake_classes_discovery.result = {}
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document)
        )

        assert result.status == "SUCCESS"
        assert result.json_schema is None
        assert result.document_class is None

    @mock_aws
    def test_the_page_range_and_hint_reach_the_discovery_and_the_result(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        _create_stack(aws_credentials)
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(5))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document),
            page_range="2-4",
            class_name_hint="W2 Form",
            model_id="us.anthropic.claude-sonnet-4-6",
        )

        kwargs = fake_classes_discovery.calls[0][1]
        assert kwargs["page_range"] == "2-4"
        assert kwargs["class_name_hint"] == "W2 Form"
        assert kwargs["model_id"] == "us.anthropic.claude-sonnet-4-6"
        assert result.page_range == "2-4"

    @mock_aws
    def test_a_discovery_exception_becomes_a_failed_result_carrying_the_message(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """A batch must not stop on one bad document, so the error is data."""
        _create_stack(aws_credentials)
        fake_classes_discovery.fail_with = RuntimeError("ThrottlingException")
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document)
        )

        assert result.status == "FAILED"
        assert result.error is not None and "ThrottlingException" in result.error
        assert result.json_schema is None

    @mock_aws
    def test_a_stack_without_a_configuration_table_fails_the_document(
        self, aws_credentials, discovery_env, tmp_path
    ):
        """The resource lookup failure is caught like any other.

        It is reported per document rather than raised, which is consistent with
        the rest of the worker, though it does mean a misconfigured stack looks
        like N document failures rather than one stack failure.
        """
        boto3.client("cloudformation", region_name=aws_credentials).create_stack(
            StackName=STACK,
            TemplateBody=json.dumps(
                {
                    "AWSTemplateFormatVersion": "2010-09-09",
                    "Resources": {"Unrelated": {"Type": "AWS::SQS::Queue"}},
                }
            ),
        )
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document)
        )

        assert result.status == "FAILED"
        assert result.error is not None and "ConfigurationTable" in result.error


# --------------------------------------------------------------------------
# _run_local
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestRunLocal:
    def test_the_bedrock_request_is_built_from_the_system_defaults(self, tmp_path):
        """Model, prompts and sampling all come from `discovery.without_ground_truth`.

        Asserted against the values `load_system_defaults` actually returns rather
        than against literals, so this stays true when the defaults are retuned
        and still fails if the operation stops reading them and falls back to its
        own hardcoded values.
        """
        from idp_common.config.merge_utils import load_system_defaults

        expected = load_system_defaults("pattern-2")["discovery"][
            "without_ground_truth"
        ]
        document = tmp_path / "invoice.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client) as ctor:
            result = IDPClient(region="us-west-2").discovery.run(str(document))

        assert result.status == "SUCCESS"
        assert result.document_class == "Invoice"
        assert ctor.call_args.kwargs == {"region": "us-west-2"}
        sent = client.invoke_model.call_args.kwargs
        assert sent["model_id"] == expected["model_id"]
        assert sent["system_prompt"] == expected["system_prompt"]
        assert sent["temperature"] == expected["temperature"]
        assert sent["top_p"] == expected["top_p"]
        assert sent["max_tokens"] == expected["max_tokens"]
        assert sent["context"] == "ClassesDiscoveryLocal"

    def test_a_pdf_travels_as_a_document_block_and_the_prompt_as_text(self, tmp_path):
        """The block shape is the contract with Converse.

        A `document` block is how the whole PDF reaches the model; sending it as
        text or as an image would silently change what the model sees.
        """
        content = _pdf(1)
        document = tmp_path / "invoice.pdf"
        document.write_bytes(content)
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            IDPClient().discovery.run(str(document))

        blocks = client.invoke_model.call_args.kwargs["content"]
        assert [set(block) for block in blocks] == [{"document"}, {"text"}]
        assert blocks[0]["document"] == {
            "format": "pdf",
            "name": "document_messages",
            "source": {"bytes": content},
        }
        assert (
            "Format the extracted data using the below JSON format" in blocks[1]["text"]
        )

    def test_a_page_range_slices_the_pdf_before_it_is_sent(self, tmp_path):
        """Pages 2-3 of a four-page packet, counted in the bytes that were sent.

        The slicing is done by the real `ClassesDiscovery.extract_pdf_pages`, so
        reopening the document block with `pypdfium2` and finding two pages is a
        measurement of the extraction rather than a record that a helper was
        called. An off-by-one here produces a schema for the wrong document and
        nothing anywhere reports an error.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(4))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document), page_range="2-3")

        assert result.status == "SUCCESS"
        sent = client.invoke_model.call_args.kwargs["content"][0]["document"]["source"][
            "bytes"
        ]
        assert len(pdfium.PdfDocument(io.BytesIO(sent))) == 2
        assert sent != document.read_bytes()

    def test_a_page_range_on_a_non_pdf_is_ignored_rather_than_refused(self, tmp_path):
        """An image has one page, so there is nothing to slice.

        Refusing would break `run_multi_section` over a directory of scans; the
        image is sent whole.
        """
        content = _png()
        document = tmp_path / "scan.png"
        document.write_bytes(content)
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document), page_range="2-3")

        assert result.status == "SUCCESS"
        blocks = client.invoke_model.call_args.kwargs["content"]
        assert set(blocks[0]) == {"image"}
        assert blocks[0]["image"]["source"]["bytes"] == content

    def test_a_class_name_hint_is_appended_to_the_prompt(self, tmp_path):
        """The hint has to name both keys, because the pipeline reads both."""
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            IDPClient().discovery.run(str(document), class_name_hint="Paystub")

        prompt = client.invoke_model.call_args.kwargs["content"][-1]["text"]
        assert '"Paystub" as the document class name' in prompt
        assert '"$id" and "x-aws-idp-document-type" to "Paystub"' in prompt

    def test_ground_truth_is_substituted_into_the_configured_prompt(self, tmp_path):
        """The defaults' prompt carries a `{ground_truth_json}` placeholder.

        Leaving it unsubstituted would send the model the literal token and no
        ground truth at all, and the run would look entirely normal.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        truth = tmp_path / "a.json"
        truth.write_text(json.dumps({"InvoiceNumber": "INV-42"}))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            IDPClient().discovery.run(str(document), ground_truth_path=str(truth))

        prompt = client.invoke_model.call_args.kwargs["content"][-1]["text"]
        assert "{ground_truth_json}" not in prompt
        assert '"InvoiceNumber": "INV-42"' in prompt

    def test_defaults_carrying_no_prompts_fall_back_to_the_built_in_ones(
        self, tmp_path
    ):
        """A trimmed or older defaults file must still produce a usable request.

        With `discovery` absent from the defaults, every value has to come from
        the literals in the module: the Nova model id, temperature 0.0, and the
        two module-level prompt builders.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with (
            patch("idp_common.bedrock.BedrockClient", return_value=client),
            patch(
                "idp_common.config.merge_utils.load_system_defaults",
                return_value={},
            ),
        ):
            result = IDPClient().discovery.run(str(document))

        assert result.status == "SUCCESS"
        sent = client.invoke_model.call_args.kwargs
        assert sent["model_id"] == "us.amazon.nova-pro-v1:0"
        assert sent["temperature"] == 0.0
        assert sent["max_tokens"] == 10000
        assert sent["system_prompt"].startswith("You are an expert in processing forms")
        assert _prompt_without_gt() in sent["content"][-1]["text"]

    def test_defaults_carrying_no_prompts_use_the_ground_truth_builder(self, tmp_path):
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        truth = tmp_path / "a.json"
        truth.write_text(json.dumps({"Total": "9.99"}))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with (
            patch("idp_common.bedrock.BedrockClient", return_value=client),
            patch(
                "idp_common.config.merge_utils.load_system_defaults",
                return_value={},
            ),
        ):
            IDPClient().discovery.run(str(document), ground_truth_path=str(truth))

        prompt = client.invoke_model.call_args.kwargs["content"][-1]["text"]
        assert _prompt_with_gt({"Total": "9.99"}) in prompt

    def test_the_caller_model_id_overrides_the_configured_one(self, tmp_path):
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            IDPClient().discovery.run(
                str(document), model_id="us.anthropic.claude-sonnet-4-6"
            )

        assert client.invoke_model.call_args.kwargs["model_id"] == (
            "us.anthropic.claude-sonnet-4-6"
        )

    @pytest.mark.parametrize("model_id", DOCUMENT_BLOCK_REFUSED)
    def test_a_model_that_cannot_read_document_blocks_is_refused(
        self, tmp_path, model_id
    ):
        """The guard runs before Bedrock is reached, and nothing is sent.

        These routes accept text and images only. Sent a `document` block they do
        not error — they drop the PDF and answer from the prompt alone, producing
        a confident schema for a document the model never saw. The stack path is
        guarded by config validation and the picklists; this path takes a
        caller-supplied id, so it is guarded here.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document), model_id=model_id)

        assert result.status == "FAILED"
        assert result.error is not None
        assert "not supported for discovery" in result.error
        assert "Anthropic or Nova" in result.error
        client.invoke_model.assert_not_called()

    def test_an_invalid_schema_is_retried_with_the_reason_fed_back(self, tmp_path):
        """The second attempt tells the model what was wrong with the first.

        Retrying with an identical prompt at temperature 0 would produce an
        identical answer, so the feedback is what makes the retry worth anything.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        incomplete = {k: v for k, v in VALID_SCHEMA.items() if k != "$id"}
        client = _bedrock(
            _converse_response(_fenced(incomplete)),
            _converse_response(_fenced(VALID_SCHEMA)),
        )

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document))

        assert result.status == "SUCCESS"
        assert client.invoke_model.call_count == 2
        first, second = [
            call.kwargs["content"][-1]["text"]
            for call in client.invoke_model.call_args_list
        ]
        assert "PREVIOUS ATTEMPT FAILED" not in first
        assert "PREVIOUS ATTEMPT FAILED: Missing required field: $id" in second

    def test_unparseable_json_is_retried(self, tmp_path):
        """A truncated response is a retry, not a failure.

        The feedback names JSON specifically, so the model is told to fix its
        syntax rather than its schema.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(
            _converse_response('```json\n{"$id": "Inv",\n```'),
            _converse_response(_fenced(VALID_SCHEMA)),
        )

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document))

        assert result.status == "SUCCESS"
        second = client.invoke_model.call_args_list[1].kwargs["content"][-1]["text"]
        assert "PREVIOUS ATTEMPT FAILED: Invalid JSON format" in second

    def test_exhausting_the_retries_reports_how_many_were_tried(self, tmp_path):
        """Three attempts, then a failure naming the count.

        Asserted through the private worker so the retry budget can be set
        explicitly; `run()` fixes it at 3, which is also checked below.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        never_valid = _converse_response(_fenced({"type": "object"}))
        client = _bedrock(never_valid, never_valid)

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery._run_local(
                tmp_path / "a.pdf", document.read_bytes(), None, max_retries=2
            )

        assert result.status == "FAILED"
        assert result.error == "Failed to generate valid schema after 2 attempts"
        assert client.invoke_model.call_count == 2

    def test_run_allows_three_attempts(self, tmp_path):
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        never_valid = _converse_response(_fenced({"type": "object"}))
        client = _bedrock(never_valid, never_valid, never_valid)

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document))

        assert client.invoke_model.call_count == 3
        assert result.error is not None and "after 3 attempts" in result.error

    def test_a_transient_bedrock_error_is_retried_but_the_last_one_is_fatal(
        self, tmp_path
    ):
        """Two different outcomes for the same exception, depending on when.

        A throttle on attempt 1 is worth retrying. The same throttle on the final
        attempt is re-raised so the caller's `error` names the AWS failure instead
        of the generic "no valid schema after N attempts", which would send them
        looking at the model's output rather than at their quota.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))

        recovers = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))
        recovers.invoke_model.side_effect = [
            RuntimeError("ThrottlingException"),
            _converse_response(_fenced(VALID_SCHEMA)),
        ]
        with patch("idp_common.bedrock.BedrockClient", return_value=recovers):
            assert IDPClient().discovery.run(str(document)).status == "SUCCESS"

        always = MagicMock()
        always.invoke_model.side_effect = RuntimeError("AccessDeniedException")
        with patch("idp_common.bedrock.BedrockClient", return_value=always):
            result = IDPClient().discovery.run(str(document))

        assert result.status == "FAILED"
        assert result.error == "AccessDeniedException"
        assert always.invoke_model.call_count == 3

    def test_an_empty_response_is_treated_as_unparseable_and_retried(self, tmp_path):
        """A model that returns no text block at all.

        `extract_text_from_response` yields `""` for it, which fails `json.loads`
        — so this lands on the retry path rather than raising out of the worker.
        """
        document = tmp_path / "a.pdf"
        document.write_bytes(_pdf(1))
        client = _bedrock(
            {"output": {"message": {"content": []}}},
            _converse_response(_fenced(VALID_SCHEMA)),
        )

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.run(str(document))

        assert result.status == "SUCCESS"
        assert client.invoke_model.call_count == 2

    def test_the_local_result_does_not_carry_the_page_range(self, tmp_path):
        """DEFECT — `operations/discovery.py:662-667`.

        `_run_with_stack` puts `page_range` on its `DiscoveryResult`; `_run_local`
        does not, although it is the worker that actually sliced the PDF. So the
        same call reports the page range in stack mode and `None` in local mode,
        and a caller that discovered several sections of one packet locally
        cannot tell the results apart by anything but list order.

        `run_multi_section` overwrites the field itself, which is why this has not
        surfaced — it only shows on a direct `run(page_range=...)` with no stack.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(4))
        client = _bedrock(_converse_response(_fenced(VALID_SCHEMA)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            local = IDPClient().discovery.run(str(document), page_range="2-3")

        assert local.status == "SUCCESS"
        assert local.page_range is None, "the slicing worker reports no page range"


# --------------------------------------------------------------------------
# _auto_detect_with_stack and _auto_detect_local
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoDetectWithStack:
    @mock_aws
    def test_sections_come_back_typed_with_the_table_exported(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        table = _create_stack(aws_credentials)
        fake_classes_discovery.sections = [
            {"start": 1, "end": 2, "type": "Letter"},
            {"start": 3, "end": 5, "type": "W2"},
        ]
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(5))

        result = IDPClient(
            stack_name=STACK, region=aws_credentials
        ).discovery.auto_detect_sections(
            str(document), model_id="us.amazon.nova-pro-v1:0"
        )

        assert isinstance(result, AutoDetectResult)
        assert result.status == "SUCCESS"
        assert [(s.start, s.end, s.type) for s in result.sections] == [
            (1, 2, "Letter"),
            (3, 5, "W2"),
        ]
        assert result.document_path == str(document)
        assert os.environ["CONFIGURATION_TABLE_NAME"] == table
        assert (
            fake_classes_discovery.calls[0][1]["model_id"] == "us.amazon.nova-pro-v1:0"
        )

    @mock_aws
    def test_a_section_missing_its_bounds_defaults_to_page_one(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """An LLM that omits `start`/`end` must not produce a validation error.

        `AutoDetectSection` requires both, so the defaults are what keep one
        malformed entry from failing the whole detection. The resulting 1-1 range
        is visibly wrong, which is the intended outcome — better than a traceback
        and better than a guessed range.
        """
        _create_stack(aws_credentials)
        fake_classes_discovery.sections = [{"type": "Mystery"}]
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))

        result = IDPClient(
            stack_name=STACK, region=aws_credentials
        ).discovery.auto_detect_sections(str(document))

        assert (result.sections[0].start, result.sections[0].end) == (1, 1)
        assert result.sections[0].type == "Mystery"

    @mock_aws
    def test_a_detection_failure_is_reported_with_no_sections(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        _create_stack(aws_credentials)
        fake_classes_discovery.fail_with = RuntimeError("model timed out")
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))

        result = IDPClient(
            stack_name=STACK, region=aws_credentials
        ).discovery.auto_detect_sections(str(document))

        assert result.status == "FAILED"
        assert result.error is not None and "model timed out" in result.error
        assert result.sections == []
        assert result.document_path == str(document)


@pytest.mark.unit
class TestAutoDetectLocal:
    def test_the_request_uses_the_auto_split_settings(self, tmp_path):
        """Section detection has its own model and prompts, under `auto_split`.

        It is a different job from schema discovery — reading a whole packet for
        boundaries rather than one document for fields — and the defaults give it
        a different (larger-context) model. Reading the wrong block would silently
        use the schema-discovery model for it.
        """
        from idp_common.config.merge_utils import load_system_defaults

        expected = load_system_defaults("pattern-2")["discovery"]["auto_split"]
        content = _pdf(3)
        document = tmp_path / "packet.pdf"
        document.write_bytes(content)
        sections = [{"start": 1, "end": 2, "type": "Letter"}]
        client = _bedrock(_converse_response(_fenced(sections)))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient(region="us-west-2").discovery.auto_detect_sections(
                str(document)
            )

        assert result.status == "SUCCESS"
        assert [(s.start, s.end, s.type) for s in result.sections] == [(1, 2, "Letter")]
        sent = client.invoke_model.call_args.kwargs
        assert sent["model_id"] == expected["model_id"]
        assert sent["system_prompt"] == expected["system_prompt"]
        assert sent["top_p"] == expected["top_p"]
        assert sent["temperature"] == 0.0
        assert sent["context"] == "AutoDetectSectionsLocal"
        assert sent["content"][0]["document"]["source"]["bytes"] == content
        assert sent["content"][1]["text"] == expected["user_prompt"]

    def test_defaults_carrying_no_auto_split_block_fall_back(self, tmp_path):
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))
        client = _bedrock(_converse_response(json.dumps([{"start": 1, "end": 1}])))

        with (
            patch("idp_common.bedrock.BedrockClient", return_value=client),
            patch(
                "idp_common.config.merge_utils.load_system_defaults",
                return_value={},
            ),
        ):
            result = IDPClient().discovery.auto_detect_sections(str(document))

        assert result.status == "SUCCESS"
        sent = client.invoke_model.call_args.kwargs
        assert sent["model_id"] == "us.amazon.nova-pro-v1:0"
        assert sent["max_tokens"] == 4096
        assert sent["top_p"] == 0.1
        assert "expert document analyst" in sent["system_prompt"]
        assert '"start": the first page number' in sent["content"][1]["text"]

    def test_a_json_object_instead_of_an_array_is_refused(self, tmp_path):
        """The contract is a list of sections.

        A dict would iterate over its keys and produce sections from strings, so
        the type is checked explicitly and the error names what was received.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))
        client = _bedrock(_converse_response(_fenced({"sections": []})))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.auto_detect_sections(str(document))

        assert result.status == "FAILED"
        assert result.error == "Expected JSON array, got dict"

    def test_an_unparseable_response_is_reported_without_retrying(self, tmp_path):
        """Section detection has no retry loop — one attempt, then report.

        Worth pinning because schema discovery in the same module does retry, and
        the difference is easy to lose.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))
        client = _bedrock(_converse_response("not json at all"))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.auto_detect_sections(str(document))

        assert result.status == "FAILED"
        assert result.error is not None and "Expecting value" in result.error
        assert client.invoke_model.call_count == 1

    @pytest.mark.parametrize("model_id", DOCUMENT_BLOCK_REFUSED)
    def test_a_model_that_cannot_read_document_blocks_is_refused(
        self, tmp_path, model_id
    ):
        """Same guard as schema discovery: the whole packet goes in one block."""
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))
        client = _bedrock(_converse_response(_fenced([])))

        with patch("idp_common.bedrock.BedrockClient", return_value=client):
            result = IDPClient().discovery.auto_detect_sections(
                str(document), model_id=model_id
            )

        assert result.status == "FAILED"
        assert (
            result.error is not None and "not supported for discovery" in result.error
        )
        client.invoke_model.assert_not_called()

    def test_the_region_falls_back_to_the_environment(self, tmp_path, monkeypatch):
        """A client built with no region still has to reach Bedrock.

        `AWS_REGION` is the fallback, and `us-west-2` the last resort — a hard
        default rather than letting boto3 fail with `NoRegionError` deep in the
        call.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))
        monkeypatch.setenv("AWS_REGION", "eu-central-1")
        client = _bedrock(_converse_response(_fenced([])))

        with patch("idp_common.bedrock.BedrockClient", return_value=client) as ctor:
            IDPClient().discovery.auto_detect_sections(str(document))

        assert ctor.call_args.kwargs == {"region": "eu-central-1"}


# --------------------------------------------------------------------------
# _run_auto_detect_and_discover
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestAutoDetectAndDiscover:
    def test_detected_sections_become_the_page_ranges_that_are_discovered(
        self, tmp_path
    ):
        """The join between the two halves: `type` becomes the class-name hint.

        Losing it would discover every section with no hint and let the model
        invent its own class names, which is the difference between a packet
        described as Letter/W2 and one described as two unrelated guesses.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(5))
        detected = AutoDetectResult(
            status="SUCCESS",
            sections=[
                {"start": 1, "end": 2, "type": "Letter"},
                {"start": 3, "end": 5, "type": "W2"},
            ],
        )
        operation = IDPClient(stack_name=STACK).discovery

        with (
            patch.object(
                operation, "auto_detect_sections", return_value=detected
            ) as detect,
            patch.object(
                operation,
                "run_multi_section",
                return_value=DiscoveryBatchResult(
                    total=2, succeeded=2, failed=0, results=[]
                ),
            ) as multi,
        ):
            result = operation.run(
                str(document),
                auto_detect=True,
                config_version="v1",
                model_id="us.amazon.nova-pro-v1:0",
            )

        assert isinstance(result, DiscoveryBatchResult)
        assert detect.call_args.kwargs["model_id"] == "us.amazon.nova-pro-v1:0"
        assert multi.call_args.kwargs["page_ranges"] == [
            {"start": 1, "end": 2, "label": "Letter"},
            {"start": 3, "end": 5, "label": "W2"},
        ]
        assert multi.call_args.kwargs["config_version"] == "v1"
        assert multi.call_args.kwargs["model_id"] == "us.amazon.nova-pro-v1:0"

    @pytest.mark.parametrize(
        "detected",
        [
            AutoDetectResult(status="FAILED", error="model timed out"),
            AutoDetectResult(status="SUCCESS", sections=[]),
        ],
    )
    def test_nothing_detected_means_nothing_discovered(self, tmp_path, detected):
        """No sections: return an empty batch rather than discovering the whole file.

        Falling back to "discover the whole document" would spend a model call per
        failed detection and return a schema for a packet rather than for a
        document class, which is exactly what auto-detect exists to avoid.
        """
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(3))
        operation = IDPClient(stack_name=STACK).discovery

        with (
            patch.object(operation, "auto_detect_sections", return_value=detected),
            patch.object(operation, "run_multi_section") as multi,
        ):
            result = operation.run(str(document), auto_detect=True)

        assert (result.total, result.succeeded, result.failed) == (0, 0, 0)
        assert result.results == []
        multi.assert_not_called()

    @mock_aws
    def test_the_whole_chain_runs_end_to_end_over_one_packet(
        self, aws_credentials, discovery_env, fake_classes_discovery, tmp_path
    ):
        """Detect two sections, then discover each — nothing stubbed in between.

        This is the only test here that drives `run(auto_detect=True)` through
        both workers, and it is what catches a mismatch between the keys
        `_run_auto_detect_and_discover` emits and the keys `run_multi_section`
        reads.
        """
        _create_stack(aws_credentials)
        fake_classes_discovery.sections = [
            {"start": 1, "end": 2, "type": "Letter"},
            {"start": 3, "end": 4, "type": "W2"},
        ]
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(4))

        result = IDPClient(stack_name=STACK, region=aws_credentials).discovery.run(
            str(document), auto_detect=True, config_version="v1"
        )

        assert (result.total, result.succeeded) == (2, 2)
        assert [r.page_range for r in result.results] == ["1-2", "3-4"]
        discovery_calls = [
            c for c in fake_classes_discovery.calls if c[0] != "auto_detect"
        ]
        assert [c[1]["class_name_hint"] for c in discovery_calls] == ["Letter", "W2"]
        assert [c[1]["page_range"] for c in discovery_calls] == ["1-2", "3-4"]


# --------------------------------------------------------------------------
# run_multi_section / run_batch details not already covered
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestMultiSectionRangeDefaults:
    def test_a_range_with_only_a_start_is_a_single_page(self, tmp_path):
        """`{"start": 4}` means page 4 alone, and a bare `{}` means page 1."""
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(5))
        operation = IDPClient(stack_name=STACK).discovery

        with patch.object(
            operation, "run", return_value=DiscoveryResult(status="SUCCESS")
        ) as run:
            operation.run_multi_section(
                document_path=str(document), page_ranges=[{"start": 4}, {}]
            )

        assert [call.kwargs["page_range"] for call in run.call_args_list] == [
            "4-4",
            "1-1",
        ]

    def test_an_empty_page_range_list_produces_an_empty_batch(self, tmp_path):
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))

        result = IDPClient(stack_name=STACK).discovery.run_multi_section(
            document_path=str(document), page_ranges=[]
        )

        assert (result.total, result.succeeded, result.failed) == (0, 0, 0)

    def test_a_range_with_no_label_passes_no_hint(self, tmp_path):
        document = tmp_path / "packet.pdf"
        document.write_bytes(_pdf(2))
        operation = IDPClient(stack_name=STACK).discovery

        with patch.object(
            operation, "run", return_value=DiscoveryResult(status="SUCCESS")
        ) as run:
            operation.run_multi_section(
                document_path=str(document), page_ranges=[{"start": 1, "end": 2}]
            )

        assert run.call_args.kwargs["class_name_hint"] is None


@pytest.mark.unit
class TestRunBatchGroundTruthPairing:
    def test_a_none_entry_in_the_ground_truth_list_means_no_ground_truth(
        self, tmp_path
    ):
        """The lists are positional, and a hole in the second one is legal.

        A caller with truth for some documents and not others passes `None` for
        the rest; mis-pairing would score one document against another's baseline.
        """
        first = tmp_path / "a.pdf"
        first.write_bytes(_pdf(1))
        second = tmp_path / "b.pdf"
        second.write_bytes(_pdf(1))
        truth = tmp_path / "b.json"
        truth.write_text("{}")
        operation = IDPClient(stack_name=STACK).discovery

        with patch.object(
            operation, "run", return_value=DiscoveryResult(status="SUCCESS")
        ) as run:
            operation.run_batch(
                [str(first), str(second)], ground_truth_paths=[None, str(truth)]
            )

        assert [call.kwargs["ground_truth_path"] for call in run.call_args_list] == [
            None,
            str(truth),
        ]

    def test_an_empty_batch_is_not_an_error(self):
        result = IDPClient(stack_name=STACK).discovery.run_batch([])

        assert (result.total, result.succeeded, result.failed) == (0, 0, 0)


# --------------------------------------------------------------------------
# run_multi_doc
# --------------------------------------------------------------------------


def _pipeline_result(
    discovered_classes,
    successful=None,
    failed=0,
    documents=10,
    clusters=1,
    noise=0,
):
    """A stand-in for `MultiDocumentDiscovery`'s dataclass result."""
    if successful is None:
        successful = len([c for c in discovered_classes if not c.get("error")])
    return SimpleNamespace(
        discovered_classes=discovered_classes,
        reflection_report="# Reflection",
        total_documents=documents,
        num_clusters=clusters,
        num_failed_embeddings=noise,
        num_successful_schemas=successful,
        num_failed_schemas=failed,
    )


def _multi_doc(result):
    pipeline = MagicMock()
    pipeline.return_value.run_local_pipeline.return_value = result
    return pipeline


@pytest.mark.unit
class TestRunMultiDoc:
    def test_the_pipeline_result_is_converted_into_the_sdk_model(self, tmp_path):
        """Every field a caller reads, including the coerced sample doc ids.

        `sample_doc_ids` are stringified because the clustering stage numbers its
        documents; leaving them as ints would make the model reject them and take
        down a whole run over a cosmetic field.
        """
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {
                        "cluster_id": 0,
                        "classification": "BankStatement",
                        "json_schema": VALID_SCHEMA,
                        "document_count": 7,
                        "sample_doc_ids": [1, 2],
                    }
                ],
                documents=7,
                clusters=1,
                noise=2,
            )
        )

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), region="eu-west-1"
            )

        assert isinstance(result, MultiDocDiscoveryResult)
        assert result.status == "SUCCESS"
        assert result.total_documents == 7
        assert result.total_clusters == 1
        assert result.noise_documents == 2
        assert result.reflection_report == "# Reflection"
        assert result.config_version is None
        discovered = result.discovered_classes[0]
        assert discovered.classification == "BankStatement"
        assert discovered.document_count == 7
        assert discovered.sample_doc_ids == ["1", "2"]
        assert pipeline.call_args.kwargs["region"] == "eu-west-1"

    def test_model_overrides_reach_the_pipeline_config(self, tmp_path):
        """Only the overrides that were given appear, so the rest stay defaulted.

        Passing `None` through would override the pipeline's own defaults with
        nothing.
        """
        pipeline = _multi_doc(_pipeline_result([]))

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path),
                embedding_model_id="us.cohere.embed-v4:0",
                analysis_model_id="us.anthropic.claude-sonnet-4-6",
            )
            IDPClient().discovery.run_multi_doc(document_dir=str(tmp_path))

        assert pipeline.call_args_list[0].kwargs["config"] == {
            "embedding_model_id": "us.cohere.embed-v4:0",
            "analysis_model_id": "us.anthropic.claude-sonnet-4-6",
        }
        assert pipeline.call_args_list[1].kwargs["config"] == {}

    def test_explicit_document_paths_are_forwarded(self, tmp_path):
        pipeline = _multi_doc(_pipeline_result([]))
        paths = [str(tmp_path / "a.pdf"), str(tmp_path / "b.pdf")]

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(document_paths=paths)

        forwarded = pipeline.return_value.run_local_pipeline.call_args.kwargs
        assert forwarded["document_paths"] == paths
        assert forwarded["document_dir"] is None

    def test_a_progress_callback_is_handed_to_the_pipeline(self, tmp_path):
        pipeline = _multi_doc(_pipeline_result([]))

        def callback(step, data):
            return None

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), progress_callback=callback
            )

        assert (
            pipeline.return_value.run_local_pipeline.call_args.kwargs[
                "progress_callback"
            ]
            is callback
        )

    @mock_aws
    def test_saving_to_a_stack_exports_the_config_table_and_pins_the_profile(
        self, aws_credentials, discovery_env, tmp_path
    ):
        """`save_to_config=True` is the only path that touches the stack.

        The table has to be exported before the pipeline runs, because the
        pipeline writes schemas through the configuration layer, and the profile
        name has to be handed to it as `config_version` — without which the
        schemas are generated and then written nowhere.
        """
        table = _create_stack(aws_credentials)
        pipeline = _multi_doc(_pipeline_result([]))

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient(
                stack_name=STACK, region=aws_credentials
            ).discovery.run_multi_doc(
                document_dir=str(tmp_path), save_to_config=True, config_version="v3"
            )

        assert os.environ["CONFIGURATION_TABLE_NAME"] == table
        assert (
            pipeline.return_value.run_local_pipeline.call_args.kwargs["config_version"]
            == "v3"
        )
        assert result.config_version == "v3"

    def test_without_save_to_config_the_pipeline_is_told_to_save_nothing(
        self, tmp_path
    ):
        """A local run must not write into a stack it was given for other reasons."""
        pipeline = _multi_doc(_pipeline_result([]))

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient(stack_name=STACK).discovery.run_multi_doc(
                document_dir=str(tmp_path), config_version="v3"
            )

        assert (
            pipeline.return_value.run_local_pipeline.call_args.kwargs["config_version"]
            is None
        )
        assert result.config_version is None

    @pytest.mark.parametrize(
        ("successful", "failed", "expected"),
        [
            (2, 0, "SUCCESS"),
            (1, 1, "PARTIAL"),
            (0, 2, "FAILED"),
            (0, 0, "SUCCESS"),
        ],
    )
    def test_the_overall_status_summarises_the_per_cluster_outcomes(
        self, tmp_path, successful, failed, expected
    ):
        """PARTIAL is the case that matters: some schemas, some not.

        Reporting it as SUCCESS would let a caller adopt an incomplete set of
        document classes; reporting it as FAILED would throw away the ones that
        did work. The zero/zero row is a pipeline that found no clusters at all,
        which is not a failure.
        """
        pipeline = _multi_doc(
            _pipeline_result([], successful=successful, failed=failed)
        )

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient().discovery.run_multi_doc(document_dir=str(tmp_path))

        assert result.status == expected

    def test_a_pipeline_exception_becomes_a_failed_result(self, tmp_path):
        pipeline = MagicMock()
        pipeline.return_value.run_local_pipeline.side_effect = RuntimeError(
            "embedding model is not enabled in this account"
        )

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient().discovery.run_multi_doc(document_dir=str(tmp_path))

        assert result.status == "FAILED"
        assert result.error is not None and "not enabled" in result.error
        assert result.discovered_classes == []

    def test_a_failed_cluster_is_carried_through_with_its_error(self, tmp_path):
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {
                        "cluster_id": 3,
                        "document_count": 4,
                        "error": "agent analysis failed",
                    }
                ],
                successful=0,
                failed=1,
            )
        )

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient().discovery.run_multi_doc(document_dir=str(tmp_path))

        assert result.status == "FAILED"
        assert result.discovered_classes[0].cluster_id == 3
        assert result.discovered_classes[0].error == "agent analysis failed"
        assert result.discovered_classes[0].json_schema is None

    def test_a_cluster_with_no_fields_at_all_gets_the_sentinel_cluster_id(
        self, tmp_path
    ):
        """`cluster_id=-1` marks a result the pipeline did not identify.

        Defaulting to `0` would collide with a real cluster and make two
        different things indistinguishable in the output directory.
        """
        pipeline = _multi_doc(_pipeline_result([{}], successful=1))

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            result = IDPClient().discovery.run_multi_doc(document_dir=str(tmp_path))

        discovered = result.discovered_classes[0]
        assert discovered.cluster_id == -1
        assert discovered.document_count == 0
        assert discovered.sample_doc_ids == []


@pytest.mark.unit
class TestWriteSchemasToDir:
    def test_each_schema_is_written_under_a_filesystem_safe_class_name(self, tmp_path):
        """The class name comes from an LLM, so it can hold anything.

        A `/` in it would write outside the output directory (or fail); the
        sanitiser replaces every character outside `[\\w\\-.]`. The assertion is
        on the filename *and* on the file's content, because a sanitiser that
        collapsed two names to one would silently drop a schema.
        """
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {
                        "cluster_id": 0,
                        "classification": "Bank Statement / Q1",
                        "json_schema": VALID_SCHEMA,
                        "document_count": 3,
                    }
                ]
            )
        )
        output = tmp_path / "schemas"

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), output_dir=str(output)
            )

        written = sorted(p.name for p in output.iterdir())
        assert written == ["Bank_Statement___Q1.json"]
        assert json.loads((output / written[0]).read_text()) == VALID_SCHEMA

    def test_the_output_directory_is_created_including_parents(self, tmp_path):
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {
                        "cluster_id": 0,
                        "classification": "Invoice",
                        "json_schema": VALID_SCHEMA,
                        "document_count": 1,
                    }
                ]
            )
        )
        output = tmp_path / "deep" / "deeper" / "schemas"

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), output_dir=str(output)
            )

        assert (output / "Invoice.json").is_file()

    def test_an_unclassified_schema_is_named_after_its_cluster(self, tmp_path):
        """No `classification`: fall back to `$id`, then to the cluster number.

        Every written file needs a name a human can match back to a cluster, and
        two unnamed clusters must not overwrite each other.
        """
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {
                        "cluster_id": 1,
                        "json_schema": VALID_SCHEMA,
                        "document_count": 1,
                    },
                    {
                        "cluster_id": 2,
                        "json_schema": {
                            k: v for k, v in VALID_SCHEMA.items() if k != "$id"
                        },
                        "document_count": 1,
                    },
                ]
            )
        )
        output = tmp_path / "schemas"

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), output_dir=str(output)
            )

        assert sorted(p.name for p in output.iterdir()) == [
            "Invoice.json",
            "cluster-2.json",
        ]

    def test_failed_and_empty_clusters_write_no_file(self, tmp_path):
        """An error or a missing schema is skipped, not written as `null`.

        A directory of schemas is consumed as a directory of schemas; a file
        containing `null` in it would be read as one and fail much later.
        """
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {"cluster_id": 0, "classification": "Bad", "error": "boom"},
                    {"cluster_id": 1, "classification": "Empty", "json_schema": None},
                    {
                        "cluster_id": 2,
                        "classification": "Good",
                        "json_schema": VALID_SCHEMA,
                    },
                ],
                successful=1,
                failed=1,
            )
        )
        output = tmp_path / "schemas"

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), output_dir=str(output)
            )

        assert [p.name for p in output.iterdir()] == ["Good.json"]

    def test_unicode_in_a_schema_survives_the_write(self, tmp_path):
        """Written with `ensure_ascii=False`, so a description stays readable."""
        schema = {**VALID_SCHEMA, "description": "Facture — montant dû"}
        pipeline = _multi_doc(
            _pipeline_result(
                [
                    {
                        "cluster_id": 0,
                        "classification": "Facture",
                        "json_schema": schema,
                        "document_count": 1,
                    }
                ]
            )
        )
        output = tmp_path / "schemas"

        with patch(
            "idp_common.discovery.multi_document_discovery.MultiDocumentDiscovery",
            pipeline,
        ):
            IDPClient().discovery.run_multi_doc(
                document_dir=str(tmp_path), output_dir=str(output)
            )

        raw = (output / "Facture.json").read_text(encoding="utf-8")
        assert "montant dû" in raw
        assert json.loads(raw) == schema
