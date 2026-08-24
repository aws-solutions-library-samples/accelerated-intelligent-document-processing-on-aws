# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Active-config-version resolution in the queue sender and the test runner.

Both Lambdas locate the active configuration version with a FILTERED `Scan` over
the ConfigurationTable. DynamoDB applies its 1MB page size (and any `Limit`) to
the items it EXAMINES, not to the items that pass `FilterExpression`, so a
single scan call reports "no active version" whenever the active row sorts
beyond the first page. Neither call paginated, and neither projected — so each
page was spent reading whole config bodies, fitting only a handful of versions.

The consequences differ by caller but are silent in both cases:

- `queue_sender` stamps `config_version` on every inbound document. Losing it
  processes the document under the DEFAULT configuration rather than the active
  one — different classes, prompts and models, with no error anywhere.
- `test_runner` captures the configuration into a test run's metadata. Losing it
  scores the run's comparisons against a config the documents were not
  processed under.

Same root cause as the pipeline-hooks dispatcher failure in issue #599; see
tests/unit/lambdas/test_pipeline_hooks_dispatcher.py for that site.
"""

import importlib.util
import os
from unittest.mock import patch

import pytest

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "../../../../..")


def _load(alias, relpath, env):
    """Load a flat Lambda `index.py` by path with env vars set at import time."""
    with patch.dict(os.environ, env):
        with patch("boto3.client"), patch("boto3.resource"):
            spec = importlib.util.spec_from_file_location(
                alias, os.path.join(_REPO_ROOT, relpath)
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"Could not load {relpath}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod


class _PagedScanTable:
    """A table whose scan() reproduces DynamoDB's examine-then-filter paging.

    `rows` is the full scan order. Each call examines at most `page_size` of
    them, applies the IsActive filter to just those, and reports a
    LastEvaluatedKey while rows remain — which is how the real service bounds a
    filtered scan. A correct caller pages until it matches or runs out.
    """

    def __init__(self, rows, page_size=10):
        self.rows = rows
        self.page_size = page_size
        self.scan_calls = 0
        self.projections = []

    def scan(self, **kwargs):
        self.scan_calls += 1
        self.projections.append(kwargs.get("ProjectionExpression"))
        window = min(self.page_size, kwargs.get("Limit", self.page_size))
        start = 0
        if "ExclusiveStartKey" in kwargs:
            key = kwargs["ExclusiveStartKey"]["Configuration"]
            start = next(
                i + 1 for i, r in enumerate(self.rows) if r["Configuration"] == key
            )
        examined = self.rows[start : start + window]
        matched = [r for r in examined if r.get("IsActive") is True]
        resp = {"Items": matched}
        if start + window < len(self.rows):
            resp["LastEvaluatedKey"] = {"Configuration": examined[-1]["Configuration"]}
        return resp

    def get_item(self, Key):
        for r in self.rows:
            if r["Configuration"] == Key["Configuration"]:
                return {"Item": r}
        return {}


def _config_rows(active_at, total=35):
    """`total` Config# rows, exactly one active, at index `active_at`."""
    return [
        {
            "Configuration": f"Config#v{i}",
            "IsActive": i == active_at,
            "classes": [{"name": f"Class{i}"}],
        }
        for i in range(total)
    ]


# ---------------------------------------------------------------------------
# queue_sender
# ---------------------------------------------------------------------------


@pytest.fixture
def queue_sender():
    return _load(
        "queue_sender_index",
        "src/lambda/queue_sender/index.py",
        {
            "QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/123456789012/q",
            "DATA_RETENTION_IN_DAYS": "30",
            "CONFIG_TABLE": "test-config-table",
            # create_document_service() runs at import time and needs a table.
            "TRACKING_TABLE": "test-tracking-table",
            "AWS_REGION": "us-east-1",
        },
    )


@pytest.mark.unit
class TestQueueSenderActiveVersion:
    def test_resolves_active_version_beyond_the_first_scan_page(self, queue_sender):
        """The regression: active row at position 33 of 35. Unpaginated, this
        returned None and the document was processed under the default config."""
        table = _PagedScanTable(_config_rows(active_at=33, total=35))
        assert queue_sender.resolve_active_config_version(table) == "v33"
        assert table.scan_calls > 1, "must page rather than trust one scan call"

    def test_resolves_active_version_on_the_last_page(self, queue_sender):
        table = _PagedScanTable(_config_rows(active_at=34, total=35))
        assert queue_sender.resolve_active_config_version(table) == "v34"

    def test_stops_at_the_first_match(self, queue_sender):
        """Runs per document, so it must not walk the whole table needlessly."""
        table = _PagedScanTable(_config_rows(active_at=0, total=500))
        assert queue_sender.resolve_active_config_version(table) == "v0"
        assert table.scan_calls == 1

    def test_projects_only_the_key(self, queue_sender):
        """Without a projection the scan reads whole config bodies, so far fewer
        versions fit in each 1MB page — the projection is what keeps the
        examine window wide."""
        table = _PagedScanTable(_config_rows(active_at=5))
        queue_sender.resolve_active_config_version(table)
        assert table.projections == ["Configuration"]

    def test_returns_none_when_nothing_is_active(self, queue_sender):
        rows = [{"Configuration": f"Config#v{i}", "IsActive": False} for i in range(35)]
        assert queue_sender.resolve_active_config_version(_PagedScanTable(rows)) is None

    def test_returns_none_for_a_malformed_key(self, queue_sender):
        rows = [{"Configuration": "NoHashHere", "IsActive": True}]
        assert queue_sender.resolve_active_config_version(_PagedScanTable(rows)) is None

    def test_empty_table_returns_none(self, queue_sender):
        assert queue_sender.resolve_active_config_version(_PagedScanTable([])) is None


# ---------------------------------------------------------------------------
# test_runner
# ---------------------------------------------------------------------------


@pytest.fixture
def test_runner():
    return _load(
        "test_runner_index_scan",
        "nested/api-resolvers/src/lambda/test_runner/index.py",
        {
            "TRACKING_TABLE": "test-table",
            "CONFIG_TABLE": "test-config-table",
            "FILE_COPY_QUEUE_URL": "https://sqs.us-east-1.amazonaws.com/1/q",
            "AWS_REGION": "us-east-1",
        },
    )


@pytest.mark.unit
class TestTestRunnerActiveVersion:
    def test_resolves_active_key_beyond_the_first_scan_page(self, test_runner):
        table = _PagedScanTable(_config_rows(active_at=33, total=35))
        assert test_runner._resolve_active_config_key(table) == "Config#v33"
        assert table.scan_calls > 1

    def test_projects_only_the_key(self, test_runner):
        """The body is fetched with GetItem afterwards, so the scan stays cheap
        however large the configs are."""
        table = _PagedScanTable(_config_rows(active_at=5))
        test_runner._resolve_active_config_key(table)
        assert table.projections == ["Configuration"]

    def test_returns_none_when_nothing_is_active(self, test_runner):
        rows = [{"Configuration": f"Config#v{i}", "IsActive": False} for i in range(35)]
        assert test_runner._resolve_active_config_key(_PagedScanTable(rows)) is None

    def test_capture_config_reads_the_late_active_version(
        self, test_runner, monkeypatch
    ):
        """End to end through _capture_config: the captured config must be the
        active version's body, not the default's."""
        table = _PagedScanTable(_config_rows(active_at=33, total=35))
        monkeypatch.setattr(test_runner.dynamodb, "Table", lambda name: table)
        config = test_runner._capture_config("ConfigTable")
        assert config["Config"]["classes"] == [{"name": "Class33"}]

    def test_capture_config_honors_an_explicit_version(self, test_runner, monkeypatch):
        """An explicitly requested version still bypasses the scan entirely."""
        table = _PagedScanTable(_config_rows(active_at=33, total=35))
        monkeypatch.setattr(test_runner.dynamodb, "Table", lambda name: table)
        config = test_runner._capture_config("ConfigTable", config_version="v7")
        assert config["Config"]["classes"] == [{"name": "Class7"}]
        assert table.scan_calls == 0

    def test_capture_config_is_empty_when_nothing_is_active(
        self, test_runner, monkeypatch
    ):
        """No active version must yield no captured config rather than an
        arbitrary one."""
        rows = [{"Configuration": f"Config#v{i}", "IsActive": False} for i in range(35)]
        table = _PagedScanTable(rows)
        monkeypatch.setattr(test_runner.dynamodb, "Table", lambda name: table)
        assert test_runner._capture_config("ConfigTable") == {}

    def test_active_version_is_named_not_just_captured(self, test_runner, monkeypatch):
        """The run must record WHICH version it captured, not only its body.

        `configVersion` is optional on startTestRun and the UI defaults to "Active
        configuration", so a run's ConfigVersion was left unset on the ordinary
        path. Anything comparing one run's config to another's then silently does
        nothing — including the guard that refuses to score a config against the
        labels that same config drafted.
        """
        table = _PagedScanTable(_config_rows(active_at=33, total=35))
        monkeypatch.setattr(test_runner.dynamodb, "Table", lambda name: table)
        assert test_runner._active_config_version("ConfigTable") == "v33"

    def test_active_version_is_none_when_nothing_is_active(
        self, test_runner, monkeypatch
    ):
        rows = [{"Configuration": f"Config#v{i}", "IsActive": False} for i in range(35)]
        monkeypatch.setattr(
            test_runner.dynamodb, "Table", lambda name: _PagedScanTable(rows)
        )
        assert test_runner._active_config_version("ConfigTable") is None
