# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``Document.from_s3`` returns pages and sections in ID order (#2133).

The evaluation loads its baseline through ``from_s3``, and the order of the
baseline's sections is the order of the per-packet section details and of the
sums behind the grouping scores. The directories used to be collected into a set
and iterated as-is, and string hashing is randomised per process, so each
Lambda process could produce a different order from the same baseline.
"""

import random
from unittest.mock import MagicMock, patch

import pytest

from idp_common.models import Document

IDS = [str(i) for i in range(1, 13)] + ["cover", "appendix"]
NUMERIC_ORDER = [str(i) for i in range(1, 13)] + ["appendix", "cover"]


def _client(listing_order):
    """An S3 client whose listing returns ``listing_order`` as directories."""

    def paginate(Bucket, Prefix, Delimiter):
        return [
            {"CommonPrefixes": [{"Prefix": f"{Prefix}{i}/"} for i in listing_order]}
        ]

    client = MagicMock()
    client.get_paginator.return_value.paginate.side_effect = paginate
    return client


def _content(uri):
    part_id = uri.rstrip("/").split("/")[-2]
    return {"classification": "Invoice", "page_ids": [part_id]}


@pytest.mark.unit
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_pages_and_sections_come_back_in_id_order(seed):
    listing = IDS[:]
    random.Random(seed).shuffle(listing)
    with (
        patch("boto3.client", return_value=_client(listing)),
        patch("idp_common.s3.get_json_content", side_effect=_content),
    ):
        document = Document.from_s3("bucket", "doc.pdf")

    assert list(document.pages) == NUMERIC_ORDER
    assert [s.section_id for s in document.sections] == NUMERIC_ORDER


@pytest.mark.unit
def test_ids_with_the_same_number_still_have_a_fixed_order():
    """``"1"`` and ``"01"`` are the same number; the order must not fall back to
    whatever order the set produced them in."""
    from idp_common.models import _directory_id_order

    entries = [("1", "p/1/"), ("01", "p/01/"), ("2", "p/2/")]
    assert sorted(entries, key=_directory_id_order) == sorted(
        entries[::-1], key=_directory_id_order
    )


@pytest.mark.unit
def test_a_digit_that_is_not_a_decimal_number_sorts_as_a_string():
    """``"²"`` passes ``str.isdigit`` but ``int()`` refuses it; such an ID must sort
    with the other non-numeric IDs rather than fail the whole baseline load."""
    from idp_common.models import _directory_id_order

    entries = [("²", "p/²/"), ("2", "p/2/")]
    assert sorted(entries, key=_directory_id_order) == [("2", "p/2/"), ("²", "p/²/")]
