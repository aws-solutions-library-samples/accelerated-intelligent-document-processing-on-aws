# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Compression Test Matrix — replicates the test matrix from GitHub Issue #200.

The original issue demonstrated that DynamoDB's 400KB item limit blocked
configs with 48+ document classes. This test suite proves that gzip compression
eliminates this limitation, supporting 500+ classes comfortably.

Original issue test matrix (without compression):
| Classes | YAML Size | Upload Result           |
|---------|-----------|-------------------------|
| 16      | 319 KB    | Success                 |
| 20      | 346 KB    | Success                 |
| 30      | 416 KB    | Success                 |
| 40      | 484 KB    | Success                 |
| 45      | 519 KB    | Success                 |
| 48      | 539 KB    | FAILED - 400KB limit    |
| 50      | 554 KB    | FAILED - 400KB limit    |

Run with `-s` flag to see the full matrix output:
    pytest tests/unit/config/test_compression_matrix.py -v -s
"""

import json

import pytest

from idp_common.config.configuration_manager import (
    _COMPRESSED_DATA_FIELD,
    _DYNAMODB_ITEM_SIZE_LIMIT,
    ConfigurationManager,
)

# ========================================================================
# Realistic document class generator
# ========================================================================


def _generate_realistic_class(class_index: int, num_fields: int = 20) -> dict:
    """
    Generate a realistic document class schema matching the issue reporter's format.

    Each class has:
    - Standard JSON Schema headers ($schema, $id, type, description)
    - IDP extensions (x-aws-idp-document-type, x-aws-idp-document-name-regex)
    - Properties with type, description (~25 chars), and x-aws-idp-evaluation-method
    - Required array

    This produces ~10-12KB per class in serialized JSON, matching the ~11KB/class
    ratio observed in the issue (539KB YAML / 48 classes ≈ 11.2KB per class).
    """
    properties = {}
    required = []
    for j in range(num_fields):
        field_name = f"field_{j:02d}"
        properties[field_name] = {
            "type": "string",
            "description": f"Extracted value for {field_name}",
            "x-aws-idp-evaluation-method": "llm",
        }
        if j < num_fields * 2 // 3:  # ~67% of fields are required
            required.append(field_name)

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"document-class-{class_index:04d}",
        "type": "object",
        "description": f"Document class {class_index} for enterprise form processing",
        "x-aws-idp-document-type": f"FormType_{class_index:04d}",
        "x-aws-idp-document-name-regex": f".*form_type_{class_index:04d}.*",
        "properties": properties,
        "required": required,
    }


def _generate_full_config_item(num_classes: int, fields_per_class: int = 20) -> dict:
    """
    Generate a complete DynamoDB config item with the specified number of classes.

    Includes all the standard IDP config sections (ocr, classification, extraction,
    assessment, summarization) plus the document classes.
    """
    classes = [
        _generate_realistic_class(i, fields_per_class) for i in range(num_classes)
    ]

    return {
        "Configuration": "Config#v1",
        "IsActive": True,
        "Description": f"Config with {num_classes} document classes",
        "CreatedAt": "2024-01-01T00:00:00Z",
        "UpdatedAt": "2024-06-01T00:00:00Z",
        "_config_format": "full",
        "ocr": {
            "backend": "textract",
            "features": ["TABLES", "FORMS"],
            "max_workers": 20,
        },
        "classification": {
            "model": "us.amazon.nova-pro-v1:0",
            "temperature": 0.0,
            "top_p": 0.1,
            "top_k": 5.0,
            "max_tokens": 4096,
            "system_prompt": "",
            "task_prompt": "",
            "classificationMethod": "multimodalPageLevelClassification",
            "sectionSplitting": "llm_determined",
        },
        "extraction": {
            "model": "us.amazon.nova-pro-v1:0",
            "temperature": 0.0,
            "top_p": 0.1,
            "top_k": 5.0,
            "max_tokens": 10000,
            "system_prompt": "",
            "task_prompt": "",
        },
        "assessment": {"enabled": True},
        "summarization": {"enabled": False},
        "classes": classes,
    }


def _compute_sizes(item: dict) -> dict:
    """Compute raw and compressed sizes for a config item."""
    raw_json = json.dumps(item, default=str, separators=(",", ":"))
    raw_size = len(raw_json.encode("utf-8"))

    compressed_item = ConfigurationManager._compress_item(item)
    compressed_size = len(compressed_item[_COMPRESSED_DATA_FIELD])

    ratio = raw_size / compressed_size if compressed_size > 0 else float("inf")
    fits = compressed_size < _DYNAMODB_ITEM_SIZE_LIMIT

    return {
        "raw_size": raw_size,
        "compressed_size": compressed_size,
        "ratio": ratio,
        "fits_400kb": fits,
    }


# ========================================================================
# Test Matrix — Issue #200 class counts
# ========================================================================

# Exact class counts from the issue's test matrix
ISSUE_CLASS_COUNTS = [16, 20, 30, 40, 45, 48, 50]

# Extended class counts for large-scale validation
EXTENDED_CLASS_COUNTS = [100, 200, 300, 500, 750, 1000]

# All class counts combined
ALL_CLASS_COUNTS = ISSUE_CLASS_COUNTS + EXTENDED_CLASS_COUNTS


class TestCompressionMatrix:
    """
    Replicates the test matrix from Issue #200 and extends it.

    Every class count that previously FAILED (48, 50) must now PASS with compression.
    Extended counts (100-1000) demonstrate enterprise-scale capacity.
    """

    @pytest.mark.parametrize(
        "num_classes",
        ISSUE_CLASS_COUNTS,
        ids=[f"{n}-classes" for n in ISSUE_CLASS_COUNTS],
    )
    def test_issue_200_class_counts_all_fit(self, num_classes):
        """All class counts from Issue #200 must fit after compression (including 48 and 50)."""
        item = _generate_full_config_item(num_classes)
        sizes = _compute_sizes(item)

        print(
            f"\n  {num_classes:>4} classes | "
            f"Raw: {sizes['raw_size']:>10,} bytes | "
            f"Compressed: {sizes['compressed_size']:>10,} bytes | "
            f"Ratio: {sizes['ratio']:>5.1f}x | "
            f"{'✅ FITS' if sizes['fits_400kb'] else '❌ EXCEEDS 400KB'}"
        )

        assert sizes["fits_400kb"], (
            f"Config with {num_classes} classes compressed to {sizes['compressed_size']:,} bytes, "
            f"exceeding DynamoDB 400KB limit. "
            f"Raw size: {sizes['raw_size']:,} bytes, ratio: {sizes['ratio']:.1f}x"
        )

    @pytest.mark.parametrize(
        "num_classes",
        EXTENDED_CLASS_COUNTS,
        ids=[f"{n}-classes" for n in EXTENDED_CLASS_COUNTS],
    )
    def test_extended_class_counts_fit(self, num_classes):
        """Extended class counts for enterprise scale must also fit."""
        item = _generate_full_config_item(num_classes)
        sizes = _compute_sizes(item)

        print(
            f"\n  {num_classes:>4} classes | "
            f"Raw: {sizes['raw_size']:>10,} bytes | "
            f"Compressed: {sizes['compressed_size']:>10,} bytes | "
            f"Ratio: {sizes['ratio']:>5.1f}x | "
            f"{'✅ FITS' if sizes['fits_400kb'] else '❌ EXCEEDS 400KB'}"
        )

        # Every extended count fits, 750 and 1000 included: measured compressed
        # sizes are ~21KB and ~28KB against a 400KB limit, so the assertion is
        # unconditional. It used to sit behind `if num_classes <= 500`, which
        # left the two largest ids -- the only ones the smaller counts do not
        # already imply -- asserting nothing at all (#1129).
        assert sizes["fits_400kb"], (
            f"Config with {num_classes} classes compressed to {sizes['compressed_size']:,} bytes, "
            f"exceeding DynamoDB 400KB limit."
        )

    def test_compression_is_what_brings_an_over_limit_item_under_it(self):
        """Compression, not fixture size, is what makes the largest items fit.

        Both parametrized tests above assert only one side — that the
        *compressed* item fits — and every count they cover would pass that on
        a fixture small enough to fit uncompressed too. The two-sided property
        is the one the suite exists to demonstrate, and it needs a count whose
        raw item genuinely exceeds the limit, so that count is derived from the
        matrix rather than named: the smallest entry that does.

        This replaces a print-only `test_full_matrix_summary`, which
        regenerated all thirteen rows, printed a table and asserted nothing
        (#1129). Every row that table printed is asserted by the two
        parametrized tests above, so the table itself was duplicated work; what
        it was *about* is asserted here.
        """
        over_limit = [
            n
            for n in ALL_CLASS_COUNTS
            if _compute_sizes(_generate_full_config_item(n))["raw_size"]
            > _DYNAMODB_ITEM_SIZE_LIMIT
        ]
        assert over_limit, (
            "no class count in the matrix produces an item that exceeds "
            f"{_DYNAMODB_ITEM_SIZE_LIMIT:,} bytes uncompressed, so nothing here "
            "demonstrates that compression is what lifts the limit"
        )

        num_classes = min(over_limit)
        sizes = _compute_sizes(_generate_full_config_item(num_classes))

        assert sizes["raw_size"] > _DYNAMODB_ITEM_SIZE_LIMIT
        assert sizes["fits_400kb"], (
            f"{num_classes} classes is {sizes['raw_size']:,} bytes uncompressed "
            f"and {sizes['compressed_size']:,} compressed — still over the "
            f"{_DYNAMODB_ITEM_SIZE_LIMIT:,}-byte limit"
        )


# Upper bound of the capacity sweep below. Every field count tested reaches it,
# so the sweep reports a lower bound rather than an inflection point; the test
# asserts that explicitly so the number is not read as a measured ceiling.
SWEEP_CEILING = 3000

# The documented minimum the compressed format must support at any field count.
MIN_SUPPORTED_CLASSES = 500


class TestCompressionCapacityEstimator:
    """
    Establishes a lower bound on the document classes that fit in the 400KB limit.

    Sweeps class counts upward until the compressed config exceeds 400KB. No
    field count tested reaches that point below `SWEEP_CEILING`, so what the
    sweep yields is "at least this many", which is what the tests assert.
    """

    @pytest.mark.parametrize(
        "fields_per_class,label",
        [
            (10, "simple forms"),
            (20, "standard"),
            (30, "complex forms"),
        ],
        ids=["10-fields", "20-fields", "30-fields"],
    )
    def test_at_least_500_classes_fit_at_every_field_count(
        self, fields_per_class, label
    ):
        """Every field count supports at least `MIN_SUPPORTED_CLASSES` classes.

        The assertion used to sit behind `if fields_per_class <= 20`, so the
        30-fields case — the widest schema, and the one most likely to be the
        first to stop fitting — asserted nothing at all (#1129). It is
        unconditional now: all three field counts run the sweep to
        `SWEEP_CEILING` without exceeding the limit.
        """
        max_fitting, first_exceeding = self._sweep(fields_per_class)

        print(f"\n  Capacity ({label}, {fields_per_class} fields/class):")
        print(f"    Classes that fit in 400KB: at least {max_fitting}")

        assert max_fitting >= MIN_SUPPORTED_CLASSES, (
            f"expected at least {MIN_SUPPORTED_CLASSES} classes with "
            f"{fields_per_class} fields/class, but the largest that fit was "
            f"{max_fitting}"
        )

        if first_exceeding is None:
            # Saturating the sweep is today's outcome for all three field
            # counts. Pinning it keeps `max_fitting` from being mistaken for a
            # measured capacity, and turns a future genuine inflection point
            # into a visible change rather than a silently smaller number.
            assert max_fitting == SWEEP_CEILING, (
                f"the sweep found no exceeding count yet stopped at "
                f"{max_fitting}, below its {SWEEP_CEILING} ceiling"
            )
        else:
            assert first_exceeding > max_fitting
            over = _compute_sizes(
                _generate_full_config_item(first_exceeding, fields_per_class)
            )
            assert not over["fits_400kb"]
            print(f"    First count that does not fit: {first_exceeding}")

    @staticmethod
    def _sweep(fields_per_class: int) -> tuple:
        """Return (largest class count that fits, first that does not)."""
        max_fitting = 0
        first_exceeding = None

        # Coarse sweep: 50-class increments
        for num_classes in range(50, SWEEP_CEILING + 1, 50):
            sizes = _compute_sizes(
                _generate_full_config_item(num_classes, fields_per_class)
            )
            if sizes["fits_400kb"]:
                max_fitting = num_classes
            else:
                first_exceeding = num_classes
                break

        # Fine sweep around the boundary
        if first_exceeding:
            for num_classes in range(max_fitting, first_exceeding + 1):
                sizes = _compute_sizes(
                    _generate_full_config_item(num_classes, fields_per_class)
                )
                if sizes["fits_400kb"]:
                    max_fitting = num_classes
                else:
                    first_exceeding = num_classes
                    break

        return max_fitting, first_exceeding
