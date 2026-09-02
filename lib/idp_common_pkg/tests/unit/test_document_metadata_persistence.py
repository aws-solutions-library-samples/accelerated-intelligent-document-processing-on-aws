# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Document.metadata serialization (GitHub #706).

``Document.metadata`` is a declared dataclass field that was absent from both
``Document.to_dict`` and ``Document.from_dict``. Since ``serialize_document``
and the compression path both go through ``to_dict``, anything written there
existed only for the lifetime of one Lambda invocation.

``ClassificationService`` writes ``metadata["failed_page_exceptions"]``
expecting a later stage to read it, and the read never saw it — the same class
of defect as the per-page ``document_boundary`` signal.
"""

from __future__ import annotations

import json

import pytest

from idp_common.models import Document


@pytest.mark.unit
class TestDocumentMetadataRoundTrip:
    def test_to_dict_emits_metadata_when_set(self):
        doc = Document(input_key="k")
        doc.metadata["failed_page_exceptions"] = {
            "3": {
                "exception_type": "ThrottlingException",
                "exception_message": "Too many requests",
                "exception_class": "botocore.errorfactory.ThrottlingException",
            }
        }
        payload = doc.to_dict()
        assert payload["metadata"]["failed_page_exceptions"]["3"]["exception_type"] == (
            "ThrottlingException"
        )

    def test_to_dict_omits_metadata_when_empty(self):
        """Byte-identical output for documents that never set metadata."""
        assert Document(input_key="k").metadata == {}
        assert "metadata" not in Document(input_key="k").to_dict()

    def test_from_dict_tolerates_missing_and_null_metadata(self):
        assert Document.from_dict({"input_key": "k"}).metadata == {}
        assert Document.from_dict({"input_key": "k", "metadata": None}).metadata == {}

    def test_metadata_survives_the_full_dict_round_trip(self):
        doc = Document(input_key="k")
        doc.metadata["failed_page_exceptions"] = {"3": {"exception_type": "Throttling"}}
        doc.metadata["primary_exception"] = "already stringified"

        restored = Document.from_dict(doc.to_dict())
        assert restored.metadata["failed_page_exceptions"] == {
            "3": {"exception_type": "Throttling"}
        }
        assert restored.metadata["primary_exception"] == "already stringified"

    def test_metadata_survives_the_json_hop(self):
        """to_json / from_json is what compress / decompress actually use."""
        doc = Document(input_key="k")
        doc.metadata["failed_page_exceptions"] = {"3": {"exception_type": "Throttling"}}

        restored = Document.from_json(doc.to_json())
        assert restored.metadata["failed_page_exceptions"]["3"]["exception_type"] == (
            "Throttling"
        )

    def test_live_exception_value_is_coerced_not_leaked(self):
        """``metadata["primary_exception"]`` holds a live Exception for
        in-process re-raise.

        ``serialize_document``'s uncompressed branch hands ``to_dict()`` straight
        back to Lambda, whose response serializer has no ``default=str`` — so an
        Exception object reaching the payload would turn a dropped field into a
        hard serialization failure.
        """
        doc = Document(input_key="k")
        doc.metadata["primary_exception"] = ValueError("boom")

        payload = doc.to_dict()
        assert payload["metadata"]["primary_exception"] == "boom"
        # Serializable with a plain dumps -- no default= hook required.
        json.dumps(payload)
