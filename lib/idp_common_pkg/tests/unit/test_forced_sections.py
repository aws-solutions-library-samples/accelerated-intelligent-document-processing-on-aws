# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A forced section grouping: one section of a test-set packet re-extracted (#1699).

Re-extracting one section under a corrected class runs the whole document with its
grouping fixed — every other section keeps its ground-truth pages, class and id —
so the classification step builds exactly those sections instead of asking the
model. The grouping crosses three Lambdas as S3 object metadata
(``document-sections``): copier → S3 → ``Document.from_s3_event`` → classification.

These tests drive that chain through the real code: the codec, the copier's real
``copy_object`` against moto S3, the real ``head_object`` in ``from_s3_event``, and
``apply_forced_document_class``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from idp_common.classification import apply_forced_document_class
from idp_common.forced_sections import (
    MAX_ENCODED_BYTES,
    MAX_PAGES,
    METADATA_KEY,
    ForcedSectionsError,
    decode_forced_sections,
    encode_forced_sections,
)
from idp_common.models import Document, Page, Status

_REPO = Path(__file__).resolve().parents[4]

#: A three-section packet as its test-set baseline records it: 0-based page
#: indices, in reading order (section 3's pages are deliberately out of order).
GROUPING = [
    {"section_id": "1", "classification": "Payslip", "page_indices": [0, 1]},
    {"section_id": "2", "classification": "W2", "page_indices": [2]},
    {"section_id": "3", "classification": "Bank-checks", "page_indices": [5, 3, 4]},
]


def _load(path: Path, name: str):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _doc(pages=6, forced_sections=None, forced_class=None):
    doc = Document(
        id="run1/packet.pdf",
        input_key="run1/packet.pdf",
        output_bucket="out",
        status=Status.OCR,
        forced_sections=forced_sections,
        forced_document_class=forced_class,
    )
    doc.pages = {str(n): Page(page_id=str(n)) for n in range(1, pages + 1)}
    return doc


@pytest.mark.unit
class TestTheCodec:
    def test_a_grouping_round_trips_exactly(self):
        assert decode_forced_sections(encode_forced_sections(GROUPING)) == GROUPING

    def test_reading_order_is_kept_and_only_ascending_runs_are_compressed(self):
        """A section's page list is its reading order as well as its membership
        (graded packet scoring compares the lists with ``==``), so compressing
        ``[5, 3, 4]`` to a sorted range would silently reorder it."""
        encoded = json.loads(encode_forced_sections(GROUPING))

        assert encoded == [
            ["1", "Payslip", "0-1"],
            ["2", "W2", "2"],
            ["3", "Bank-checks", "5,3-4"],
        ]

    def test_the_encoding_is_ascii_even_when_it_cannot_be(self):
        """S3 user metadata travels as HTTP headers; the class vocabulary is ASCII
        already, and the JSON is written with ``ensure_ascii`` as well."""
        encoded = encode_forced_sections(GROUPING)

        assert encoded.isascii()

    def test_a_grouping_that_does_not_fit_in_object_metadata_is_refused(self):
        """S3 allows 2 KB of user metadata per object. A grouping that would not
        fit must be refused where it is built, not discovered as a failed copy."""
        big = [
            {
                "section_id": str(n),
                "classification": "C" * 100,
                "page_indices": [n],
            }
            for n in range(40)
        ]

        with pytest.raises(ForcedSectionsError, match="bytes of object metadata"):
            encode_forced_sections(big)

    def test_a_grouping_at_the_limit_still_fits(self):
        """The refusal is at the budget, not below it."""
        sections = []
        n = 0
        while True:
            candidate = sections + [
                {"section_id": str(n), "classification": "W2", "page_indices": [n]}
            ]
            try:
                encode_forced_sections(candidate)
            except ForcedSectionsError:
                break
            sections, n = candidate, n + 1
        encoded = encode_forced_sections(sections)

        assert MAX_ENCODED_BYTES - 20 < len(encoded) <= MAX_ENCODED_BYTES

    @pytest.mark.parametrize(
        "value, reason",
        [
            ("not json", "not JSON"),
            ('{"1": "W2"}', "not a list"),
            ('[["1", "W2"]]', "malformed section entry"),
            ('[["1", "W2", 3]]', "malformed section entry"),
            ('[["1", "W2", "1-"]]', "malformed pages"),
            ('[["1", "W2", "3-1"]]', "does not ascend"),
            ('[["1", "W2", "2-2"]]', "does not ascend"),
            ('[["1", "W2", "0-999999999"]]', f"more than {MAX_PAGES} pages"),
            ('[["1", "W2", "0-1"], ["2", "W2", "1"]]', "in both section"),
            ('[["1", "W2", "0"], ["1", "W2", "1"]]', "more than once"),
            ('[["1", "W2;rm", "0"]]', "invalid class"),
            ('[["../1", "W2", "0"]]', "invalid section id"),
            ("[]", "at least one section"),
        ],
    )
    def test_anything_malformed_is_refused_rather_than_guessed(self, value, reason):
        """The pipeline's alternative to a grouping it cannot read is classifying
        the document, which replaces every section — so there is no lenient
        reading."""
        with pytest.raises(ForcedSectionsError, match=reason):
            decode_forced_sections(value)

    @pytest.mark.parametrize(
        "section, reason",
        [
            ({"page_indices": []}, "has no pages"),
            ({"page_indices": [-1]}, "invalid page index"),
            ({"page_indices": [True]}, "invalid page index"),
            ({"section_id": "a/b"}, "invalid section id"),
            ({"classification": ""}, "invalid class"),
        ],
    )
    def test_the_encoder_refuses_what_the_decoder_would(self, section, reason):
        """The encoder is the resolver's half of the contract, so it enforces the
        same rules itself rather than relying on its caller."""
        bad = {"section_id": "1", "classification": "W2", "page_indices": [0]}
        bad.update(section)

        with pytest.raises(ForcedSectionsError, match=reason):
            encode_forced_sections([bad])

    def test_a_section_id_longer_than_the_limit_is_refused(self):
        with pytest.raises(ForcedSectionsError, match="invalid section id"):
            decode_forced_sections(json.dumps([["a" * 65, "W2", "0"]]))
        assert decode_forced_sections(json.dumps([["a" * 64, "W2", "0"]]))

    def test_the_page_limit_is_for_the_whole_grouping(self):
        """A grouping whose pages total more than the limit is refused even when
        each section is under it. (Whether the decoder stops expanding at the first
        range over budget or only after the last is not observable here: both end
        in this refusal, and either bounds the work by the metadata size.)"""
        half = MAX_PAGES // 2 + 1
        value = json.dumps(
            [["1", "W2", f"0-{half - 1}"], ["2", "W2", f"{half}-{2 * half - 1}"]]
        )

        with pytest.raises(ForcedSectionsError, match=f"more than {MAX_PAGES} pages"):
            decode_forced_sections(value)

    def test_a_value_longer_than_the_metadata_budget_is_refused(self):
        value = json.dumps([["1", "W2", ",".join(str(n) for n in range(0, 800, 2))]])
        assert len(value) > MAX_ENCODED_BYTES

        with pytest.raises(ForcedSectionsError, match="right size"):
            decode_forced_sections(value)

    def test_more_sections_than_the_limit_is_refused(self):
        from idp_common.forced_sections import MAX_SECTIONS

        sections = [
            {"section_id": str(n), "classification": "W", "page_indices": [n]}
            for n in range(MAX_SECTIONS + 1)
        ]

        with pytest.raises(ForcedSectionsError, match=f"maximum is {MAX_SECTIONS}"):
            encode_forced_sections(sections)

    @pytest.mark.parametrize("value", [None, ""])
    def test_no_value_means_no_grouping(self, value):
        assert decode_forced_sections(value) is None


@pytest.mark.unit
class TestTheDocumentCarriesIt:
    def test_it_survives_a_serialization_round_trip(self):
        """Read from metadata before OCR and applied at classification, so it
        crosses step boundaries as JSON; dropping it would re-enable
        classification and regroup every section."""
        encoded = encode_forced_sections(GROUPING)
        doc = Document(id="d.pdf", input_key="d.pdf", forced_sections=encoded)

        assert Document.from_dict(doc.to_dict()).forced_sections == encoded

    def test_an_ordinary_document_has_none(self):
        doc = Document(id="d.pdf", input_key="d.pdf")

        assert Document.from_dict(doc.to_dict()).forced_sections is None


@pytest.mark.unit
class TestClassificationBuildsExactlyThoseSections:
    def test_every_section_keeps_its_id_pages_and_class(self):
        doc = _doc(forced_sections=encode_forced_sections(GROUPING))

        applied = apply_forced_document_class(doc)

        assert applied
        assert [(s.section_id, s.classification, s.page_ids) for s in doc.sections] == [
            ("1", "Payslip", ["1", "2"]),
            ("2", "W2", ["3"]),
            ("3", "Bank-checks", ["6", "4", "5"]),
        ]
        assert all(s.confidence == 1.0 for s in doc.sections)

    def test_section_ids_are_kept_rather_than_renumbered(self):
        """The harvest and the review flow look sections up by id, so ids that are
        not in position order must come through unchanged."""
        grouping = [
            {"section_id": "10", "classification": "Payslip", "page_indices": [0]},
            {"section_id": "2", "classification": "W2", "page_indices": [1]},
            {"section_id": "x-7", "classification": "W2", "page_indices": [2]},
        ]
        doc = _doc(pages=3, forced_sections=encode_forced_sections(grouping))

        apply_forced_document_class(doc)

        assert [s.section_id for s in doc.sections] == ["10", "2", "x-7"]

    def test_each_section_carries_its_baseline_page_indices(self):
        """Extraction writes ``attributes["page_indices"]`` out as the result's
        ``split_document.page_indices`` — so the harvested label records the same
        pages in the same reading order as the baseline it replaces."""
        doc = _doc(forced_sections=encode_forced_sections(GROUPING))

        apply_forced_document_class(doc)

        assert [s.attributes for s in doc.sections] == [
            {"page_indices": [0, 1]},
            {"page_indices": [2]},
            {"page_indices": [5, 3, 4]},
        ]

    def test_every_named_page_gets_its_sections_class(self):
        doc = _doc(forced_sections=encode_forced_sections(GROUPING))

        apply_forced_document_class(doc)

        assert {pid: p.classification for pid, p in doc.pages.items()} == {
            "1": "Payslip",
            "2": "Payslip",
            "3": "W2",
            "4": "Bank-checks",
            "5": "Bank-checks",
            "6": "Bank-checks",
        }

    def test_a_page_no_section_names_keeps_no_class(self):
        """The baseline may not mention every page. Inventing a class for one would
        be a label nobody chose; the classification step skips the model because
        something was forced (see the handler test in patterns/unified/tests)."""
        doc = _doc(pages=7, forced_sections=encode_forced_sections(GROUPING))

        apply_forced_document_class(doc)

        assert doc.pages["7"].classification is None
        assert all("7" not in s.page_ids for s in doc.sections)

    def test_a_grouping_wins_over_a_whole_document_class(self):
        doc = _doc(forced_sections=encode_forced_sections(GROUPING), forced_class="W2")

        apply_forced_document_class(doc)

        assert len(doc.sections) == 3

    def test_a_page_the_document_does_not_have_fails_the_document(self):
        """A baseline that does not match the document cannot be reproduced, and
        classifying instead would replace every section."""
        doc = _doc(pages=5, forced_sections=encode_forced_sections(GROUPING))

        with pytest.raises(ValueError, match="names page"):
            apply_forced_document_class(doc)

    def test_a_malformed_grouping_fails_the_document(self):
        doc = _doc(forced_sections='[["1", "W2", "0-"]]')

        with pytest.raises(ValueError):
            apply_forced_document_class(doc)

    def test_the_whole_document_class_still_builds_one_section(self):
        """The pre-existing path, unchanged: one section over every page."""
        doc = _doc(pages=3, forced_class="W2")

        assert apply_forced_document_class(doc) == "W2"
        assert [(s.section_id, s.page_ids) for s in doc.sections] == [
            ("1", ["1", "2", "3"])
        ]


@pytest.mark.unit
class TestTheMetadataReachesThePipeline:
    """Copier → S3 → ``from_s3_event`` → classification, on moto S3, with each
    Lambda's own code. The metadata key is a contract between two Lambdas that never
    import each other, so it is checked by passing through the real thing."""

    _ENV = {
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",  # nosec B105 - fake moto credential  # pragma: allowlist secret
        "AWS_SESSION_TOKEN": "testing",  # nosec B105 - fake moto credential
    }

    def _copy(self, **kwargs):
        copier = _load(
            _REPO / "src/lambda/test_file_copier/index.py",
            f"test_file_copier_sections_{len(kwargs)}",
        )
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-set-bucket")
        s3.create_bucket(Bucket="input-bucket")
        s3.put_object(Bucket="test-set-bucket", Key="ts1/input/packet.pdf", Body=b"x")
        with patch.object(copier, "s3", s3):
            copied = copier._copy_files_to_bucket(
                "test-set-bucket",
                "ts1/input/",
                "input-bucket",
                "run1/",
                ["packet.pdf"],
                config_version="v3",
                submission_source="test-studio",
                test_set_id="ts1",
                **kwargs,
            )
        assert copied == ["packet.pdf"], "the copy itself failed"
        event = {
            "detail": {
                "bucket": {"name": "input-bucket"},
                "object": {"key": "run1/packet.pdf"},
            },
            "time": "2026-10-31T00:00:00Z",
        }
        return Document.from_s3_event(event, "out")

    def test_a_grouping_survives_the_copy_and_builds_the_sections(self):
        encoded = encode_forced_sections(GROUPING)
        with mock_aws(), patch.dict(os.environ, self._ENV):
            doc = self._copy(forced_sections=encoded)

        assert doc.forced_sections == encoded
        assert doc.forced_document_class is None
        doc.pages = {str(n): Page(page_id=str(n)) for n in range(1, 7)}
        apply_forced_document_class(doc)
        assert [s.section_id for s in doc.sections] == ["1", "2", "3"]

    def test_the_metadata_key_is_the_one_the_codec_names(self):
        with mock_aws(), patch.dict(os.environ, self._ENV):
            self._copy(forced_sections=encode_forced_sections(GROUPING))
            head = boto3.client("s3", region_name="us-east-1").head_object(
                Bucket="input-bucket", Key="run1/packet.pdf"
            )

        assert METADATA_KEY in head["Metadata"]

    def test_the_copier_handler_carries_the_grouping_from_its_message(self):
        """Through the handler, from the SQS message the runner sends to the copied
        object's metadata — the hop the tests above enter below."""
        encoded = encode_forced_sections(GROUPING)
        copier = _load(
            _REPO / "src/lambda/test_file_copier/index.py", "test_file_copier_handler"
        )
        env = {
            **self._ENV,
            "TEST_SET_BUCKET": "test-set-bucket",
            "INPUT_BUCKET": "input-bucket",
            "BASELINE_BUCKET": "baseline-bucket",
        }
        with mock_aws(), patch.dict(os.environ, env):
            s3 = boto3.client("s3", region_name="us-east-1")
            for bucket in ("test-set-bucket", "input-bucket", "baseline-bucket"):
                s3.create_bucket(Bucket=bucket)
            s3.put_object(
                Bucket="test-set-bucket", Key="ts1/input/packet.pdf", Body=b"x"
            )
            ddb = boto3.resource("dynamodb", region_name="us-east-1")
            ddb.create_table(
                TableName="tracking",
                KeySchema=[
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
            message = {
                "testRunId": "run1",
                "testSetId": "ts1",
                "trackingTable": "tracking",
                "filesToProcess": 1,
                "objectKeys": ["packet.pdf"],
                "purpose": "draft-labeling",
                "documentSections": encoded,
            }
            with patch.object(copier, "s3", s3), patch.object(copier, "dynamodb", ddb):
                copier.handler({"Records": [{"body": json.dumps(message)}]}, None)
            head = s3.head_object(Bucket="input-bucket", Key="run1/packet.pdf")

        assert head["Metadata"].get(METADATA_KEY) == encoded
        assert "document-class" not in head["Metadata"]

    def test_an_ordinary_copy_carries_no_grouping(self):
        with mock_aws(), patch.dict(os.environ, self._ENV):
            doc = self._copy()

        assert doc.forced_sections is None
