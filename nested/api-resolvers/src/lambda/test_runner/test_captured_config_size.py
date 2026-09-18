# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
A test run records its configuration compressed, so a profile that fits in the
configuration table also fits on every run that captures it.

The configuration table gzip-compresses profile bodies, so a profile can be
several megabytes raw. The runner used to copy the decompressed body inline
onto the run's metadata item, which gave the run a lower ceiling than the
profile it copied: every Run Test of a large profile failed at submit with
DynamoDB's raw "Item size has exceeded the maximum allowed size".

The properties under test:

- the run item carries the configuration as a gzip Binary attribute, not inline;
- the results resolver reads that attribute back to the same configuration, and
  still reads runs created before this change;
- the version, revision and confidence fingerprint stay top-level attributes,
  because other resolvers query them;
- a configuration that does not fit even compressed fails with a message that
  names the sizes, not DynamoDB's error.
"""

import gzip
import importlib.util
import json
import os
import random
import string
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")

DYNAMODB_ITEM_LIMIT = 400 * 1024


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def runner():
    module = _load("test_runner_index_capture", Path(__file__).with_name("index.py"))
    module.dynamodb = MagicMock()
    return module


@pytest.fixture
def results_resolver():
    path = Path(__file__).parent.parent / "test_results_resolver" / "index.py"
    module = _load("test_results_resolver_index_capture", path)
    module.dynamodb = MagicMock()
    return module


_VOCABULARY = [
    "".join(random.Random(i).choices(string.ascii_lowercase, k=3 + i % 8))
    for i in range(400)
]


def _words(rng, n):
    """Prose-like text: a bounded vocabulary compresses the way real prompts do."""
    return " ".join(rng.choices(_VOCABULARY, k=n))


def _large_config(n_classes, seed=7):
    """A profile whose inline copy is well past the DynamoDB item limit."""
    rng = random.Random(seed)
    return {
        "classification": {"model": "m", "temperature": Decimal("0.0")},
        "extraction": {"model": "m", "temperature": Decimal("0.0")},
        "classes": [
            {
                "name": f"class_{i}",
                "description": _words(rng, 60),
                "attributes": [
                    {
                        "name": f"attribute_{j}",
                        "description": _words(rng, 30),
                        "confidence_threshold": Decimal("0.85"),
                    }
                    for j in range(25)
                ],
            }
            for i in range(n_classes)
        ],
    }


def _stored_item(runner):
    return runner.dynamodb.Table.return_value.put_item.call_args.kwargs["Item"]


def _store(runner, config, **overrides):
    kwargs = dict(
        tracking_table="tracking",
        test_run_id="test-set-20260916-120000",
        test_set_id="set-1",
        test_set_name="test-set",
        config=config,
        files=[],
        context="ctx",
        file_count=23,
        config_version="config_custom_other_change_1",
        test_set_version=None,
        config_revision=4,
    )
    kwargs.update(overrides)
    runner._store_test_run_metadata(**kwargs)


class TestTheRunItemStoresTheConfigurationCompressed:
    def test_a_profile_past_the_item_limit_inline_is_written_compressed(self, runner):
        config = {"Config": _large_config(60)}
        inline = json.dumps(config, default=str, separators=(",", ":")).encode()
        assert len(inline) > DYNAMODB_ITEM_LIMIT, "fixture must exceed the item limit"

        _store(runner, config)

        item = _stored_item(runner)
        assert "Config" not in item
        assert item["_config_storage"] == "compressed"
        assert isinstance(item["_compressed_config"], bytes)
        assert len(item["_compressed_config"]) < len(inline) // 4
        assert len(item["_compressed_config"]) < DYNAMODB_ITEM_LIMIT

    def test_the_stored_body_round_trips_with_numbers_kept_numeric(self, runner):
        config = {"Config": _large_config(3)}
        _store(runner, config)

        body = json.loads(gzip.decompress(_stored_item(runner)["_compressed_config"]))
        assert body["Config"]["classes"][0]["name"] == "class_0"
        assert body["Config"]["extraction"]["temperature"] == 0
        assert (
            body["Config"]["classes"][0]["attributes"][0]["confidence_threshold"]
            == 0.85
        )
        assert not isinstance(body["Config"]["extraction"]["temperature"], str)

    def test_large_but_float_finite_decimals_are_serialised_not_crashed(
        self, runner
    ):
        """The integer-test branch used to be ``value % 1 == 0``, which
        raises ``decimal.InvalidOperation`` (``DivisionImpossible``) on
        Decimals whose coefficient exceeds the current context precision
        (default 28 digits) — but ``Decimal('1E30')`` and similar are
        finite floats (``1e+30`` is well inside float64's range) that
        must NOT crash ``startTestRun``. The prior overflow-check fix
        left this middle case unprotected: ``float()`` says "fine, that
        fits", then ``value % 1`` raises anyway.

        ``value == value.to_integral_value()`` is a rounding-only op
        with no context-precision requirement, so it never trips on
        this path. Verify:

        * a large integer-valued Decimal (``1E30``) returns ``int`` and
          does not crash — the regression-guard case;
        * a small non-integer Decimal (``0.85``) still returns ``float``
          — the pre-change happy path stayed intact.
        """
        # Regression case: previously raised DivisionImpossible on the
        # modulo. Must return int(10**30) cleanly.
        result = runner._json_default(Decimal("1E30"))
        assert result == 10**30
        assert isinstance(result, int)

        # Happy path: non-integer Decimals still coerce to float.
        result = runner._json_default(Decimal("0.85"))
        assert result == 0.85
        assert isinstance(result, float)

    def test_subnormal_decimal_raises_rather_than_silently_truncating_to_zero(
        self, runner
    ):
        """``Decimal('1E-500')`` is finite (``is_finite()`` returns True) but
        ``float()`` underflows it to ``0.0`` — silent numeric truncation
        that persists to the compressed config. Similarly a huge Decimal
        like ``Decimal('1E500')`` overflows to ``inf`` on ``float()``,
        which JSON cannot represent. Both paths must raise loudly rather
        than round-trip a corrupted value.
        """
        with pytest.raises(ValueError, match="underflows to 0.0"):
            runner._json_default(Decimal("1E-500"))
        with pytest.raises(ValueError, match="overflows"):
            runner._json_default(Decimal("1E500"))

    def test_non_finite_decimals_raise_a_clear_error_not_invalid_operation(
        self, runner
    ):
        """A ``Decimal("NaN")`` in a captured config used to fail
        ``startTestRun`` with ``decimal.InvalidOperation`` — ``value % 1``
        raises that before the modulo comparison is even evaluated —
        which surfaces as a cryptic stack trace hiding what's wrong.
        Non-finite decimals cannot round-trip through JSON at all, so the
        default now raises ``ValueError`` naming the offending value.
        """
        with pytest.raises(ValueError, match="non-finite Decimal"):
            runner._json_default(Decimal("NaN"))
        with pytest.raises(ValueError, match="non-finite Decimal"):
            runner._json_default(Decimal("Infinity"))
        with pytest.raises(ValueError, match="non-finite Decimal"):
            runner._json_default(Decimal("-Infinity"))

    def test_non_decimal_non_json_types_raise_typeerror_not_silent_str(
        self, runner
    ):
        """A ``datetime`` / ``bytes`` / ``UUID`` in a captured config used
        to be silently coerced to ``str(value)``, corrupting the round-
        trip and hiding a real config-validity problem. The default now
        falls through to ``TypeError`` — the same behaviour ``json.dumps``
        has without a custom default — so the failure is loud and names
        the offending type.
        """
        import datetime as _dt
        import uuid as _uuid

        with pytest.raises(TypeError, match="not JSON-serialisable"):
            runner._json_default(_dt.datetime(2026, 9, 18))
        with pytest.raises(TypeError, match="not JSON-serialisable"):
            runner._json_default(b"raw-bytes")
        with pytest.raises(TypeError, match="not JSON-serialisable"):
            runner._json_default(_uuid.uuid4())

    def test_queryable_attributes_stay_top_level(self, runner):
        _store(runner, {"Config": _large_config(2)})

        item = _stored_item(runner)
        assert item["ConfigVersion"] == "config_custom_other_change_1"
        assert item["ConfigRevision"] == 4
        assert item["Status"] == "QUEUED"
        assert item["FilesCount"] == 23
        assert item["Files"] == []

    def test_a_body_too_large_even_compressed_fails_with_the_sizes(self, runner):
        rng = random.Random(3)
        incompressible = {
            "Config": {
                "classes": [
                    {"name": f"c{i}", "description": rng.randbytes(4096).hex()}
                    for i in range(80)
                ]
            }
        }

        with pytest.raises(ValueError) as exc:
            _store(runner, incompressible)

        message = str(exc.value)
        assert "too large" in message
        assert "after compression" in message
        assert "bytes raw" in message
        runner.dynamodb.Table.return_value.put_item.assert_not_called()


class TestTheResultsResolverReadsBothStorageShapes:
    def test_a_compressed_run_reads_back_to_the_captured_configuration(
        self, runner, results_resolver
    ):
        config = {"Config": _large_config(60)}
        _store(runner, config)
        item = _stored_item(runner)
        table = results_resolver.dynamodb.Table.return_value
        table.get_item.return_value = {"Item": item}

        read_back = results_resolver._get_test_run_config(item["TestRunId"])

        # ``_get_test_run_config`` normalizes Decimals to float/int at
        # its own boundary via ``convert_decimals``, so the caller sees
        # a JSON-serialisable dict — but the read INSIDE
        # ``_captured_config_of`` now uses ``parse_float=Decimal`` for
        # type-symmetry with the legacy-inline path (see the direct
        # boundary test ``test_captured_config_of_returns_decimal``
        # below). Comparing here against the double-roundtripped shape
        # asserts the end-to-end invariant callers depend on.
        expected = json.loads(json.dumps(config, default=runner._json_default))
        assert read_back == expected

    def test_captured_config_of_returns_decimal_symmetrically_across_storage_formats(
        self, runner, results_resolver
    ):
        """``_captured_config_of`` sits below ``_get_test_run_config``'s
        Decimal→float normalization and returns the raw config shape any
        future direct-caller sees. Both storage formats must yield the
        same TYPE for non-integer numbers so a caller that switches on
        ``isinstance(x, Decimal)`` — or does equality against a
        ``Decimal`` literal — doesn't branch differently based on
        storage format.

        The legacy-inline path returns Decimals naturally (DDB's
        resource client hands numbers back that way); the compressed
        path now uses ``parse_float=Decimal`` to match.
        """
        # Compressed path
        _store(runner, {"Config": _large_config(2)})
        compressed_item = _stored_item(runner)
        compressed = results_resolver._captured_config_of(compressed_item)
        threshold_c = compressed["Config"]["classes"][0]["attributes"][0][
            "confidence_threshold"
        ]
        assert isinstance(threshold_c, Decimal), (
            "compressed path must return Decimals so it matches the "
            "legacy-inline path — downstream isinstance() checks otherwise "
            "branch differently across storage formats"
        )

        # Legacy-inline path
        legacy_item = {
            "Config": {"Config": {"threshold": Decimal("0.85")}},
        }
        legacy = results_resolver._captured_config_of(legacy_item)
        assert isinstance(legacy["Config"]["threshold"], Decimal)

    def test_a_run_created_before_this_change_still_reads_inline(
        self, results_resolver
    ):
        legacy = {
            "PK": "testrun#old",
            "SK": "metadata",
            "Config": {
                "Config": {"notes": "x", "extraction": {"temperature": Decimal("0.5")}}
            },
        }
        table = results_resolver.dynamodb.Table.return_value
        table.get_item.return_value = {"Item": legacy}

        assert results_resolver._get_test_run_config("old") == {
            "Config": {"notes": "x", "extraction": {"temperature": 0.5}}
        }

    def test_a_run_without_any_configuration_reads_as_empty(self, results_resolver):
        table = results_resolver.dynamodb.Table.return_value
        table.get_item.return_value = {"Item": {"PK": "testrun#bare", "SK": "metadata"}}

        assert results_resolver._get_test_run_config("bare") == {}

    def test_a_corrupt_blob_reads_as_empty_rather_than_failing_the_results_page(
        self, results_resolver
    ):
        assert (
            results_resolver._captured_config_of(
                {"_config_storage": "compressed", "_compressed_config": b"not gzip"}
            )
            == {}
        )
