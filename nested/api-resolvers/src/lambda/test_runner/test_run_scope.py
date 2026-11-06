# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
``startTestRun`` applies the caller's configuration scope to the run's profile.

A run's documents are stamped with its profile and so become visible to every user
scoped to it. Under strict tenant isolation, ingest also accepts a Test Studio
document under the profile its run record names. Without a scope check a scoped
Author could therefore start a run under a profile outside their scope and place
documents into another scope's — another tenant's — view.

The properties under test:

- a scoped Author is refused a profile outside their scope, named or active;
- a scoped Author may run their own profile;
- an Admin is unrestricted, and a direct (IAM) invocation is not scoped;
- an unevaluable scope refuses the run (fail closed), before anything is written.
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from idp_common.config_scope import ScopeLookupError  # type: ignore

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("TRACKING_TABLE", "tracking")
os.environ.setdefault("CONFIG_TABLE", "config")
os.environ.setdefault("FILE_COPY_QUEUE_URL", "https://sqs.example/queue")


def _load(name):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name("index.py")
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def index():
    module = _load("test_runner_index_run_scope")
    module.sqs = MagicMock()
    module.dynamodb = MagicMock()
    module._get_test_set = MagicMock(
        return_value={"name": "lending", "fileCount": 3, "latestVersion": 0}
    )
    module._active_config_version = MagicMock(return_value="globex-default")
    module._require_profile = MagicMock()
    module._capture_config = MagicMock(return_value={"Config": {"notes": "x"}})
    module._published_revision = MagicMock(return_value=1)
    module._store_test_run_metadata = MagicMock()
    return module


def _event(profile=None, *, groups=("Author",), identity=True):
    run_input = {"testSetId": "lending"}
    if profile:
        run_input["configVersion"] = profile
    event = {"info": {"fieldName": "startTestRun"}, "arguments": {"input": run_input}}
    if identity:
        event["identity"] = {
            "claims": {
                "cognito:groups": list(groups),
                "email": "author@example.com",
                "sub": "sub-1",
            }
        }
    return event


def _scope(index, scope):
    index.resolve_allowed_config_versions = MagicMock(return_value=scope)


@pytest.mark.unit
@pytest.mark.parametrize("profile", ["globex-invoices", None])
def test_a_scoped_author_is_refused_a_profile_outside_their_scope(index, profile):
    # `None` exercises the ACTIVE profile (globex-default), which the run's
    # documents are stamped with just the same.
    _scope(index, ["acme-*"])
    with pytest.raises(PermissionError, match="not in your allowed scope"):
        index.handler(_event(profile), None)
    index._store_test_run_metadata.assert_not_called()
    index.sqs.send_message.assert_not_called()


@pytest.mark.unit
def test_a_scoped_author_may_run_their_own_profile(index):
    _scope(index, ["acme-*"])
    index.handler(_event("acme-invoices"), None)
    index._store_test_run_metadata.assert_called()


@pytest.mark.unit
def test_an_admin_is_not_looked_up(index):
    index.resolve_allowed_config_versions = MagicMock(
        side_effect=AssertionError("an Admin must not be scope-checked")
    )
    index.handler(_event("globex-invoices", groups=("Admin",)), None)
    index._store_test_run_metadata.assert_called()


@pytest.mark.unit
def test_a_direct_invocation_is_not_scoped(index):
    index.resolve_allowed_config_versions = MagicMock(
        side_effect=AssertionError("an IAM invocation has no caller to scope")
    )
    index.handler(_event("globex-invoices", identity=False), None)
    index._store_test_run_metadata.assert_called()


@pytest.mark.unit
def test_an_unevaluable_scope_refuses_the_run(index):
    index.resolve_allowed_config_versions = MagicMock(
        side_effect=ScopeLookupError("table unreadable")
    )
    with pytest.raises(PermissionError, match="could not be verified"):
        index.handler(_event("acme-invoices"), None)
    index._store_test_run_metadata.assert_not_called()


def _review_event(*, groups=("Author",)):
    return {
        "info": {"fieldName": "sendTestRunToReview"},
        "arguments": {"testRunId": "run-1"},
        "identity": {
            "claims": {
                "cognito:groups": list(groups),
                "email": "author@example.com",
                "sub": "sub-1",
            }
        },
    }


def _stored_run(index, profile):
    table = MagicMock()
    table.get_item.side_effect = lambda **kw: (
        {"Item": {"ConfigVersion": profile, "Files": []}}
        if kw["Key"]["PK"].startswith("testrun#")
        else {}
    )
    index.dynamodb.Table.return_value = table
    return table


@pytest.mark.unit
def test_a_scoped_author_cannot_send_another_scopes_run_to_review(index):
    _scope(index, ["acme-*"])
    table = _stored_run(index, "globex-invoices")
    with pytest.raises(PermissionError, match="globex-invoices"):
        index.handler(_review_event(), None)
    table.update_item.assert_not_called()


@pytest.mark.unit
def test_a_scoped_author_may_send_their_own_run_to_review(index):
    _scope(index, ["acme-*"])
    _stored_run(index, "acme-invoices")
    index.handler(_review_event(), None)


@pytest.mark.unit
def test_an_admin_sends_any_run_to_review_without_a_scope_lookup(index):
    _scope(index, ["acme-*"])
    _stored_run(index, "globex-invoices")
    index.handler(_review_event(groups=("Admin",)), None)
    index.resolve_allowed_config_versions.assert_not_called()
