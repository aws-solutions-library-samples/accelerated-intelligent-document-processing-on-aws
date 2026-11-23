# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``resolve_processing_mode``: the mode a document runs in.

The queue processor routes a run by this answer and maps the run plan's start step
with it, so it must agree with what the state machine's mode choice reads
(``use_bda`` at the pinned revision, with a linked BDA project) and it must never
raise: a document whose configuration cannot be read still has to start.
"""

import logging
from types import SimpleNamespace

import pytest

from idp_common.run_plan.mode import (
    BDA,
    PIPELINE,
    ProcessingMode,
    resolve_processing_mode,
)

_ARN = "arn:aws:bedrock:us-east-1:123456789012:data-automation-project/p"


class _Manager:
    """A configuration manager that records what it was asked for."""

    def __init__(self, *, head=None, revisions=None, project_arn=None, fail=None):
        self.head = head
        self.revisions = revisions or {}
        self.project_arn = project_arn
        self.fail = fail or set()
        self.reads = []
        self.project_reads = []

    def get_merged_configuration(self, version, revision=None):
        self.reads.append((version, revision))
        if ("head" if revision is None else revision) in self.fail:
            raise RuntimeError(f"cannot read {version} r{revision}")
        return self.head if revision is None else self.revisions[revision]

    def get_bda_project_arn(self, version):
        self.project_reads.append(version)
        return self.project_arn


def _config(use_bda):
    return SimpleNamespace(use_bda=use_bda)


@pytest.mark.unit
class TestResolveProcessingMode:
    def test_a_pipeline_profile(self):
        config = _config(False)
        manager = _Manager(head=config)

        mode = resolve_processing_mode(manager, "invoices", None)

        assert mode == ProcessingMode(PIPELINE, None, config, False)
        assert manager.reads == [("invoices", None)]
        # The project is read only when BDA is asked for.
        assert manager.project_reads == []

    def test_a_bda_profile_with_a_project(self):
        config = _config(True)
        manager = _Manager(head=config, project_arn=_ARN)

        mode = resolve_processing_mode(manager, "claims", None)

        assert mode == ProcessingMode(BDA, _ARN, config, False)
        assert manager.project_reads == ["claims"]

    def test_a_bda_profile_without_a_project_falls_back(self, caplog):
        config = _config(True)
        manager = _Manager(head=config, project_arn=None)

        with caplog.at_level(logging.WARNING):
            mode = resolve_processing_mode(manager, "claims", None)

        assert mode == ProcessingMode(PIPELINE, None, config, True)
        assert "Falling back to pipeline mode" in caplog.text

    def test_the_pinned_revision_decides_not_the_head(self):
        pinned = _config(True)
        manager = _Manager(head=_config(False), revisions={4: pinned}, project_arn=_ARN)

        mode = resolve_processing_mode(manager, "claims", 4)

        assert mode.mode == BDA
        assert mode.config is pinned
        assert manager.reads == [("claims", 4)]

    def test_an_unreadable_revision_is_read_from_the_head(self, caplog):
        head = _config(True)
        manager = _Manager(head=head, project_arn=_ARN, fail={4})

        with caplog.at_level(logging.WARNING):
            mode = resolve_processing_mode(manager, "claims", 4)

        assert mode == ProcessingMode(BDA, _ARN, head, False)
        assert manager.reads == [("claims", 4), ("claims", None)]
        assert "Routing from the profile head" in caplog.text

    def test_a_manager_that_raises_gives_pipeline_mode(self, caplog):
        manager = _Manager(fail={"head"})

        with caplog.at_level(logging.WARNING):
            mode = resolve_processing_mode(manager, "claims", None)

        assert mode == ProcessingMode(PIPELINE, None, None, False)
        assert "Defaulting to pipeline mode" in caplog.text

    def test_a_failed_project_read_gives_pipeline_mode(self):
        class _Broken(_Manager):
            def get_bda_project_arn(self, version):
                raise RuntimeError("table unavailable")

        mode = resolve_processing_mode(_Broken(head=_config(True)), "claims", None)

        assert mode == ProcessingMode(PIPELINE, None, None, False)

    def test_no_configuration_is_pipeline_mode(self):
        mode = resolve_processing_mode(_Manager(head=None), "claims", None)

        assert mode == ProcessingMode(PIPELINE, None, None, False)
