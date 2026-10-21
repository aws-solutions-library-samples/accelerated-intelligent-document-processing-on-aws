# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``maxPagesForClassification`` overrides ``sectionSplitting``; the resolved
config says so.

With the limit set to anything but ``ALL``,
``ClassificationService._apply_limited_classification_to_all_pages`` collapses
the result into one all-pages section **for every configuration** — so a stored
``sectionSplitting`` of ``llm_determined`` or ``page`` is a value the runtime
ignores. Issue #1328 called that override out as silent and asked for the field
to be gated on the splitting value.

⚠️ **Gating would have made it worse**, which is why this is a normalisation
instead. Hiding the control while the override stays in force turns a
visible-but-surprising setting into an invisible one; resolving the ignored key
to what actually happens is what stops it being silent, and leaves both fields
on screen.

⚠️ **And it does not raise.** A validator that raises on read runs on every
configuration read in the system, so refusing the combination would fail
documents on a configuration that loaded cleanly before — which is #1522. A
normalisation cannot.
"""

from __future__ import annotations

import logging

import pytest

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.models import IDPConfig


def _resolve(**classification):
    base = merge_config_with_defaults({}, validate=False)
    base["classification"].update(classification)
    return IDPConfig(**base).classification


@pytest.mark.unit
class TestTheOverrideIsVisibleInTheResolvedConfig:
    def test_the_default_is_untouched(self):
        c = _resolve()
        assert c.maxPagesForClassification == "ALL"
        assert c.sectionSplitting == "llm_determined"

    @pytest.mark.parametrize("splitting", ["llm_determined", "page"])
    @pytest.mark.parametrize("limit", ["3", 3, "10"])
    def test_a_limit_resolves_splitting_to_disabled(self, limit, splitting):
        c = _resolve(maxPagesForClassification=limit, sectionSplitting=splitting)
        assert c.sectionSplitting == "disabled", (
            "the runtime collapses to one section regardless, so any other value "
            "here is one the config claims and the run ignores"
        )
        # The limit itself is preserved — it is the setting that won.
        assert str(c.maxPagesForClassification) == str(limit)

    def test_it_warns_naming_both_settings(self, caplog):
        with caplog.at_level(logging.WARNING):
            _resolve(maxPagesForClassification="3", sectionSplitting="page")
        assert "maxPagesForClassification" in caplog.text
        assert "sectionSplitting" in caplog.text

    def test_an_already_consistent_config_is_not_warned_about(self, caplog):
        with caplog.at_level(logging.WARNING):
            c = _resolve(maxPagesForClassification="3", sectionSplitting="disabled")
        assert c.sectionSplitting == "disabled"
        assert "cannot take effect" not in caplog.text

    @pytest.mark.parametrize("limit", ["ALL", "all", "", None])
    def test_no_limit_leaves_splitting_alone(self, limit):
        c = _resolve(maxPagesForClassification=limit, sectionSplitting="llm_determined")
        assert c.sectionSplitting == "llm_determined"

    @pytest.mark.parametrize("limit", ["banana", "0", "-1"])
    def test_a_value_the_service_treats_as_all_overrides_nothing(self, limit):
        """⚠️ The service warns and falls back to ALL for a non-numeric or
        non-positive value, so it collapses nothing — and this must agree with
        it, or the config would claim `disabled` for a run that sections
        normally."""
        c = _resolve(maxPagesForClassification=limit, sectionSplitting="llm_determined")
        assert c.sectionSplitting == "llm_determined"

    def test_it_never_raises(self):
        """The whole point of normalising rather than refusing."""
        for limit in ("ALL", "3", "banana", "0", None, 7):
            for splitting in ("llm_determined", "page", "disabled"):
                _resolve(maxPagesForClassification=limit, sectionSplitting=splitting)
