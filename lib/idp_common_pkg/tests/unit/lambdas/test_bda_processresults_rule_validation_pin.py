# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The BDA ProcessResults rule-validation check reads the document's pinned
revision, like the handler's other configuration reads.

`is_rule_validation_enabled` took a `config_revision` argument, and both of its
callers pass the document's, but it never forwarded it to `get_config`. So the
routing decision was made from the profile's current head rather than from the
revision the document was pinned to, and — because an unpinned read is the one
that can write the profile back (#1613) — it was the one read in this handler
able to modify the configuration table.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from idp_common.config.models import IDPConfig
from tests.unit.lambdas._goldens import load_handler

bda_index = load_handler(
    "bda_processresults_rule_validation_pin_index",
    "bda_processresults_function/index.py",
    extra_modules={"pypdfium2": MagicMock()},
)

_PINNED = 7

_ENABLED = IDPConfig.model_validate(
    {
        "rule_validation": {"enabled": True},
        "policy_classes": [{"$id": "Policy", "rule_properties": {}}],
    }
)
_DISABLED = IDPConfig.model_validate({"rule_validation": {"enabled": False}})


@pytest.mark.unit
def test_the_pinned_revision_decides_rule_validation(monkeypatch):
    """The pinned revision enables rule validation and the head does not; the
    answer must be the pinned revision's."""
    calls = []

    def fake_get_config(*, as_model=False, version=None, revision=None, **_):
        calls.append({"version": version, "revision": revision})
        return _ENABLED if revision == _PINNED else _DISABLED

    monkeypatch.setattr(bda_index, "get_config", fake_get_config)

    assert bda_index.is_rule_validation_enabled("my-profile", _PINNED) is True
    assert calls == [{"version": "my-profile", "revision": _PINNED}]


@pytest.mark.unit
def test_an_unpinned_document_still_reads_the_head(monkeypatch):
    calls = []

    def fake_get_config(*, as_model=False, version=None, revision=None, **_):
        calls.append(revision)
        return _ENABLED

    monkeypatch.setattr(bda_index, "get_config", fake_get_config)

    assert bda_index.is_rule_validation_enabled("my-profile", None) is True
    assert calls == [None]
