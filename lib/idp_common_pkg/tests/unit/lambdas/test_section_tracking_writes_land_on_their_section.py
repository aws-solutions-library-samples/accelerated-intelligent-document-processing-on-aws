# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every interim section write must land on that section's slot (#1690).

Inside the ``ProcessSections`` map each per-section Lambda writes its section to
the tracking item with ``SET Sections[i] = :section``, so the UI shows results
while the rest of the document is still running.

Extraction, the extraction failure helpers and rule validation take ``i`` from the
full document their map iterates, so their ``i`` is right. The assessment Lambda
took it from the document it receives, and on the normal path that is the
extraction step's *section document*: extraction narrows ``document.sections`` to
the one section it worked on. So assessment found every section at index 0 and
wrote section N's result over section 1's slot. ProcessResults rewrites the item
when the document succeeds; a document whose execution fails is not rewritten, so
there the misplaced write was the lasting record.

Two pieces, each with its own tests here:

* **The writer** (``DocumentDynamoDBService.update_document_section``) accepts
  ``section_index=None``, meaning "place this section by its ``Id`` in the stored
  ``Sections``". If no stored slot carries the ``Id`` it writes nothing rather than
  pick another section's slot. An integer index is still written exactly as given,
  because the full document is the authority during the map: a postClassification
  hook may add, remove or reorder sections the stored item does not reflect yet.
* **The assessment handler** passes ``None``.

After such a hook the stored list can hold the same ``Id`` twice for a while —
classification's stale copy and the one extraction wrote at the map index — and a
sibling iteration can write a slot between the writer's read and its write. So the
by-``Id`` path prefers the copy carrying the section's extraction result URI, and
its write is conditional on the slot still carrying the ``Id`` (re-resolved once,
then abandoned). ⚠️ **Residual:** on a reprocess both copies can carry the
previous run's URI, and the first ``Id`` match is taken; the conditional write
still keeps that from overwriting another section.

The handlers are the real deployed ones, run in workflow order, against the real
``DocumentDynamoDBService`` on a ``moto`` table. ⚠️ The list-index semantics are
therefore **moto's**, not DynamoDB's own expression engine; nothing offline can
establish the latter. The stubs are the outbound edges: configuration, the
extraction service's model call, the S3 read of the stored extraction result, and
the assessment service, which is stubbed to record one confidence alert per
section — the one thing in the slot that only assessment writes.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

# Imported before load_handler on purpose. It execs each handler inside
# patch.dict("sys.modules"), which removes on exit every module first imported
# during the exec. A service module first imported there stays bound inside the
# handler but leaves sys.modules, so a later test file's
# patch("idp_common.image.prepare_image") patches a fresh copy and the real
# function runs (measured: test_assessment_service.py went red after this file).
import idp_common.assessment.service  # noqa: F401
import idp_common.extraction.service  # noqa: F401
from idp_common.config.models import IDPConfig
from idp_common.dynamodb.client import DynamoDBClient, DynamoDBError
from idp_common.dynamodb.service import DocumentDynamoDBService
from idp_common.models import Document, Page, Section, Status
from tests.unit.lambdas._goldens import load_handler

extraction_index = load_handler(
    "section_slot_extraction_index", "extraction_function/index.py"
)
assessment_index = load_handler(
    "section_slot_assessment_index", "assessment_function/index.py"
)

TABLE = "section-slot-tracking"
OBJECT_KEY = "folder/packet.pdf"
DOC_KEY = {"PK": f"doc#{OBJECT_KEY}", "SK": "none"}
EXEC = "arn:aws:states:us-east-1:111122223333:execution:sm:exec-1"
CLASSES = ["w2", "bank-statement", "payslip"]
IDS = [str(i) for i in range(1, len(CLASSES) + 1)]


class _Context:
    function_name = "fn"
    memory_limit_in_mb = 1024
    invoked_function_arn = "arn:aws:lambda:us-east-1:111122223333:function:fn"
    aws_request_id = "req-1"

    def get_remaining_time_in_millis(self):
        return 300_000


def _classified_document() -> Document:
    """A freshly classified three-section document: nothing extracted yet."""
    return Document(
        id=OBJECT_KEY,
        input_bucket="in",
        input_key=OBJECT_KEY,
        output_bucket="out",
        status=Status.CLASSIFYING,
        initial_event_time="2026-10-31T10:00:00Z",
        queued_time="2026-10-31T10:00:01Z",
        num_pages=len(CLASSES),
        config_version="default",
        pages={pid: Page(page_id=pid) for pid in IDS},
        sections=[
            Section(section_id=sid, classification=cls, page_ids=[sid])
            for sid, cls in zip(IDS, CLASSES)
        ],
    )


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")  # nosec B105 - moto dummy
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        ).wait_until_exists()
        yield DocumentDynamoDBService(
            dynamodb_client=DynamoDBClient(table_name=TABLE, region="us-east-1")
        )


def _stored_sections():
    item = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(TABLE)
        .get_item(Key=DOC_KEY)
        .get("Item", {})
    )
    return item.get("Sections", [])


def _seed(service: DocumentDynamoDBService, document: Document) -> None:
    """Persist the document the way classification does before the map starts."""
    service.create_document(document)
    service.update_document(document)
    assert [s["Id"] for s in _stored_sections()] == IDS


# ---------------------------------------------------------------------------
# The handlers, in workflow order
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch, service):
    monkeypatch.setenv("WORKING_BUCKET", "")
    config = IDPConfig()
    for module in (extraction_index, assessment_index):
        monkeypatch.setattr(module, "get_config", lambda **kw: config)
        monkeypatch.setattr(module, "create_document_service", lambda *a, **kw: service)

    def _extract(self, document, section_id, **kwargs):
        # The model call is the outbound edge. Record what extraction would: a
        # result URI on the section it was handed.
        section = next(s for s in document.sections if s.section_id == section_id)
        section.extraction_result_uri = (
            f"s3://out/{OBJECT_KEY}/sections/{section_id}/result.json"
        )
        return document

    monkeypatch.setattr(
        extraction_index.extraction.ExtractionService,
        "process_document_section",
        _extract,
    )
    # No explainability in the stored result, so assessment is not skipped.
    monkeypatch.setattr(
        assessment_index.s3,
        "get_json_content",
        lambda uri: {"inference_result": {"Total": "1.00"}},
    )

    def _assess(self, document, section_id, **kwargs):
        section = next(s for s in document.sections if s.section_id == section_id)
        section.confidence_threshold_alerts = [
            {
                "attribute_name": _alert_field(section_id),
                "confidence": 0.1,
                "confidence_threshold": 0.9,
            }
        ]
        return document

    monkeypatch.setattr(
        assessment_index.assessment.AssessmentService,
        "process_document_section",
        _assess,
    )
    return service


def _alert_field(section_id: str) -> str:
    return f"assessed-field-of-section-{section_id}"


def _alert_fields(slot) -> list:
    return [a["attributeName"] for a in slot.get("ConfidenceThresholdAlerts", [])]


def _run_section(classification_document: dict, section_id: str) -> dict:
    extracted = extraction_index.handler(
        {
            "execution_arn": EXEC,
            "document": classification_document,
            "section_id": section_id,
        },
        _Context(),
    )
    # The incoming document is the extraction step's section document.
    assert [
        s.section_id
        for s in Document.load_document(extracted["document"], "", None).sections
    ] == [section_id]
    return assessment_index.handler(
        {
            "execution_arn": EXEC,
            "document": extracted["document"],
            "section_id": section_id,
        },
        _Context(),
    )


@pytest.mark.unit
def test_assessment_writes_each_section_to_its_own_slot(wired):
    document = _classified_document()
    _seed(wired, document)
    classification_document = document.to_dict()

    for done, section_id in enumerate(IDS, start=1):
        _run_section(classification_document, section_id)
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == IDS
        # Every section assessed so far carries its own alert, and only its own;
        # the ones not yet run carry none.
        assert [_alert_fields(s) for s in stored] == [
            [_alert_field(sid)] if i < done else [] for i, sid in enumerate(IDS)
        ]
        slot = stored[IDS.index(section_id)]
        assert slot["Class"] == CLASSES[IDS.index(section_id)]
        assert slot["OutputJSONUri"].endswith(f"/sections/{section_id}/result.json")


@pytest.mark.unit
def test_assessment_of_a_later_section_leaves_the_first_slot_alone(wired):
    """The single shape the defect takes, isolated: only section 3 runs."""
    document = _classified_document()
    _seed(wired, document)

    _run_section(document.to_dict(), "3")

    stored = _stored_sections()
    assert [s["Id"] for s in stored] == IDS
    assert stored[0]["Class"] == "w2"
    assert stored[0]["OutputJSONUri"] == ""
    assert _alert_fields(stored[0]) == []
    assert _alert_fields(stored[2]) == [_alert_field("3")]


# ---------------------------------------------------------------------------
# The writer, on its own
# ---------------------------------------------------------------------------


def _section(section_id: str, classification: str, uri: str = "") -> Section:
    return Section(
        section_id=section_id,
        classification=classification,
        page_ids=[section_id],
        extraction_result_uri=uri,
    )


@pytest.mark.unit
class TestTheWriter:
    def test_an_index_is_written_as_given(self, service):
        _seed(service, _classified_document())
        service.update_document_section(OBJECT_KEY, 1, _section("2", "x", "s3://b/2"))
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == IDS
        assert stored[1]["Class"] == "x"
        assert stored[1]["OutputJSONUri"] == "s3://b/2"

    def test_no_index_places_the_section_by_id(self, service):
        _seed(service, _classified_document())
        result = service.update_document_section(
            OBJECT_KEY, None, _section("3", "x", "s3://b/3")
        )
        assert [s["Class"] for s in result["Sections"]] == ["w2", "bank-statement", "x"]
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == IDS
        assert [s["Class"] for s in stored] == ["w2", "bank-statement", "x"]
        assert stored[2]["OutputJSONUri"] == "s3://b/3"

    def test_no_index_and_an_unknown_id_writes_nothing(self, service):
        _seed(service, _classified_document())
        result = service.update_document_section(
            OBJECT_KEY, None, _section("9", "x", "s3://b/9")
        )
        assert result == {}
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == IDS
        assert [s["Class"] for s in stored] == CLASSES

    def test_a_hook_reshaped_section_list_is_written_in_the_maps_order(self, service):
        """The full document's index stays authoritative for indexed writes.

        A postClassification hook inserted section 4 after section 1, so the map
        iterates ``[1, 4, 2, 3]`` while the stored item still holds ``[1, 2, 3]``.
        Each extraction write uses its full-document index, and the stored list
        must end in the map's order with nothing dropped or duplicated.
        """
        _seed(service, _classified_document())
        for index, sid in enumerate(["1", "4", "2", "3"]):
            service.update_document_section(
                OBJECT_KEY, index, _section(sid, f"c{sid}", f"s3://b/{sid}")
            )
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == ["1", "4", "2", "3"]
        assert [s["OutputJSONUri"] for s in stored] == [
            f"s3://b/{sid}" for sid in ["1", "4", "2", "3"]
        ]

    def test_no_index_prefers_the_copy_extraction_wrote(self, service):
        """A hook inserted section 4 after 1; the map runs ``[1, 4, 2, 3]``.

        Extraction of 2 wrote its copy at map index 2 while classification's
        stale copy of 2 is still at slot 1, so the stored list reads
        ``[1, 2(stale), 2(extracted)]``. Assessment of 2 must land on the copy
        carrying its extraction result, not on the first ``Id`` match, which
        extraction of 4 then overwrites.
        """
        _seed(service, _classified_document())
        service.update_document_section(OBJECT_KEY, 0, _section("1", "c1", "s3://b/1"))
        service.update_document_section(OBJECT_KEY, 2, _section("2", "c2", "s3://b/2"))
        service.update_document_section(
            OBJECT_KEY, None, _section("2", "assessed", "s3://b/2")
        )
        service.update_document_section(OBJECT_KEY, 1, _section("4", "c4", "s3://b/4"))
        service.update_document_section(OBJECT_KEY, 3, _section("3", "c3", "s3://b/3"))
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == ["1", "4", "2", "3"]
        assert [s["Class"] for s in stored] == ["c1", "c4", "assessed", "c3"]

    def test_no_index_never_overwrites_a_slot_that_changed_after_the_read(
        self, service, monkeypatch
    ):
        """A sibling iteration writes the chosen slot between the read and the write.

        The stored list is ``[1, 2, 3]`` and the section carries no result URI,
        so the first ``Id`` match (slot 1) is chosen. Before the write lands,
        extraction of a hook-inserted section 4 takes slot 1 and extraction of 2
        writes its map index 2. The refused write must be resolved again from a
        fresh read, landing on slot 2, and section 4 must survive.
        """
        _seed(service, _classified_document())
        real_get = service.client.get_item
        reads = []

        def _get_then_sibling_writes(*args, **kwargs):
            item = real_get(*args, **kwargs)
            reads.append([s["Id"] for s in item["Sections"]])
            if len(reads) == 1:
                service.update_document_section(
                    OBJECT_KEY, 1, _section("4", "c4", "s3://b/4")
                )
                service.update_document_section(OBJECT_KEY, 2, _section("2", "c2", ""))
            return item

        monkeypatch.setattr(service.client, "get_item", _get_then_sibling_writes)
        service.update_document_section(OBJECT_KEY, None, _section("2", "assessed"))
        stored = _stored_sections()
        assert reads == [["1", "2", "3"], ["1", "4", "2"]]
        assert [s["Id"] for s in stored] == ["1", "4", "2"]
        assert [s["Class"] for s in stored] == ["w2", "c4", "assessed"]

    def test_no_index_gives_up_when_the_section_vanished_under_it(
        self, service, monkeypatch
    ):
        _seed(service, _classified_document())
        real_get = service.client.get_item

        def _get_then_overwrite(*args, **kwargs):
            item = real_get(*args, **kwargs)
            service.update_document_section(
                OBJECT_KEY, 1, _section("4", "c4", "s3://b/4")
            )
            return item

        monkeypatch.setattr(service.client, "get_item", _get_then_overwrite)
        result = service.update_document_section(
            OBJECT_KEY, None, _section("2", "assessed")
        )
        assert result == {}
        assert [s["Id"] for s in _stored_sections()] == ["1", "4", "3"]

    def test_no_index_gives_up_after_the_slot_changes_twice(self, service, monkeypatch):
        """Every read is followed by a sibling write that moves section 2 on."""
        _seed(service, _classified_document())
        real_get = service.client.get_item
        newcomers = iter(["4", "5", "6"])

        def _get_then_move(*args, **kwargs):
            item = real_get(*args, **kwargs)
            ids = [s["Id"] for s in item["Sections"]]
            slot = ids.index("2")
            sid = next(newcomers)
            service.update_document_section(OBJECT_KEY, slot, _section(sid, f"c{sid}"))
            service.update_document_section(
                OBJECT_KEY, len(ids), _section("2", "moved")
            )
            return item

        monkeypatch.setattr(service.client, "get_item", _get_then_move)
        result = service.update_document_section(
            OBJECT_KEY, None, _section("2", "assessed")
        )
        assert result == {}
        stored = _stored_sections()
        assert [s["Id"] for s in stored] == ["1", "4", "3", "5", "2"]
        assert "assessed" not in [s["Class"] for s in stored]

    def test_no_index_still_raises_a_failure_other_than_the_condition(
        self, service, monkeypatch
    ):
        """Only a refused condition is re-resolved; any other error propagates."""
        _seed(service, _classified_document())

        def _throttled(**kwargs):
            raise DynamoDBError("throttled", "ProvisionedThroughputExceededException")

        monkeypatch.setattr(service.client, "update_item", _throttled)
        with pytest.raises(DynamoDBError) as raised:
            service.update_document_section(
                OBJECT_KEY, None, _section("2", "x", "s3://b/2")
            )
        assert raised.value.error_code == "ProvisionedThroughputExceededException"

    def test_an_index_past_the_end_appends(self, service):
        _seed(service, _classified_document())
        service.update_document_section(OBJECT_KEY, 3, _section("4", "x", "s3://b/4"))
        assert [s["Id"] for s in _stored_sections()] == IDS + ["4"]

    def test_a_missing_document_still_raises_for_an_indexed_write(self, service):
        with pytest.raises(DynamoDBError) as raised:
            service.update_document_section(
                "missing.pdf", 0, _section("1", "x", "s3://b/1")
            )
        assert raised.value.error_code == "ValidationException"
