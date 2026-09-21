# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The prefix a shard cleanup lists has to be the prefix shards are written to.

Per-shard results are keyed by a **content-derived** section id —
``{class_label}_{first_page}_{last_page}`` — not by the section's ordinal
``section_id``, which classification assigns as ``str(idx)`` and which is not
stable across a reclassify. A cleanup that built its prefix from the ordinal
listed ``checkpoints/{arn}/0/shards/`` while the writer had written
``checkpoints/{arn}/bank-statement_1_5/shards/``. The listing came back empty, the
cleanup deleted nothing, and it logged success either way.

**One deployed handler has a cleanup and one deliberately has none.** The
in-process handler (``index.py``) releases its resume state after the response is
built; the shard-merge handler releases nothing, because ``merge_section_shards``
requires every shard present and a retry can follow a merge that already succeeded.
Which handler is in which camp is asserted below rather than assumed, so a cleanup
reintroduced into the merge handler fails here as well as in
``patterns/unified/tests/test_shard_retention.py``, where the window it would open
is reproduced.

Two kinds of assertion here, and the split matters. The structural ones hold
whatever the format strings are, because ``shard_result_key`` is *built on*
``shard_results_prefix`` — that is the part a future edit cannot break by changing
one string and forgetting the other. The producer/consumer ones drive the REAL
service and the REAL deployed handlers, because the defect was not in either
format string individually: each was self-consistent, and they disagreed.

A test that restated the expected prefix as a literal would have passed against
the broken code, since the literal would have been copied from the cleanup side.
"""

from __future__ import annotations

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

from idp_common.extraction.runtime import (
    shard_persistence_section_id,
    shard_result_key,
    shard_results_prefix,
)
from idp_common.models import Section

_SRC = os.path.join(
    os.path.dirname(__file__),
    "../../../../../patterns/unified/src/extraction_function",
)

EXECUTION_ARN = "arn:aws:states:us-east-1:123456789012:execution:idp:abc-123"

#: Every extraction handler that is deployed, and whether it is allowed to release a
#: section's per-shard results. ``test_the_camps_are_what_this_file_assumes`` checks
#: this against the modules, so neither adding a cleanup nor losing one goes unseen.
#:
#: ⚠️ Both lists are **authored**, and the check over them is by attribute NAME, so
#: it is hardening rather than a closed gate: a cleanup reintroduced into the merge
#: handler as ``_release_shard_state`` would pass it, and a third handler added to the
#: directory would be in neither list. What actually closes those two holes is
#: ``patterns/unified/tests/test_shard_retention.py``, which asserts on the S3 calls a
#: merge makes rather than on what the module is called — so a cleanup under any name
#: fails there. Do not treat a green run here as proof the merge handler is clean.
DEPLOYED_HANDLERS = ["index.py", "sfn_runtime_handler.py"]
HANDLERS_WITH_A_CLEANUP = ["index.py"]


def _cleanup_of(module):
    """The callable a handler uses to release per-shard results, or ``None``.

    Both spellings are checked: ``index.py`` imports ``delete_shard_results`` from
    ``idp_common.extraction.runtime``, and a local helper has historically been
    called ``_cleanup_shards``.
    """
    return getattr(module, "delete_shard_results", None) or getattr(
        module, "_cleanup_shards", None
    )


def _load(module_name: str, filename: str):
    """Load a deployed handler by path with X-Ray's import-time work stubbed."""
    recorder = MagicMock()
    recorder.capture.return_value = lambda fn: fn
    xray_core = MagicMock()
    xray_core.patch_all = lambda: None
    xray_core.xray_recorder = recorder
    with patch.dict(
        "sys.modules",
        {"aws_xray_sdk": MagicMock(), "aws_xray_sdk.core": xray_core},
    ):
        spec = importlib.util.spec_from_file_location(
            module_name, os.path.join(_SRC, filename)
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _section() -> Section:
    """Ordinal id "0", pages 1–5 — so the two candidate prefixes differ."""
    return Section(
        section_id="0",
        classification="bank-statement",
        page_ids=["1", "2", "3", "4", "5"],
    )


# ---------------------------------------------------------------------------
# Structural: the key is built on the prefix, so they cannot diverge.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_shard_key_starts_with_the_prefix_a_cleanup_lists():
    persist_id = shard_persistence_section_id("bank-statement", ["1", "2", "3"])
    prefix = shard_results_prefix(EXECUTION_ARN, persist_id)
    key = shard_result_key(EXECUTION_ARN, persist_id, 0, 2)
    assert key.startswith(prefix), (
        f"a shard is written to {key} but a cleanup lists {prefix}, so the cleanup "
        "cannot see it"
    )
    assert key != prefix


@pytest.mark.unit
def test_the_persistence_id_is_derived_from_content_not_from_the_ordinal():
    section = _section()
    persist_id = shard_persistence_section_id(section.classification, section.page_ids)
    assert persist_id == "bank-statement_1_5"
    assert persist_id != section.section_id
    # Page order in the section must not change the id: the shard writer and the
    # cleanup may see the ids in different orders.
    assert (
        shard_persistence_section_id("bank-statement", ["5", "3", "1", "4", "2"])
        == persist_id
    )
    # Integers and strings must agree — page ids arrive as both.
    assert shard_persistence_section_id("bank-statement", [1, 2, 3, 4, 5]) == persist_id


@pytest.mark.unit
def test_a_section_with_no_pages_is_refused_rather_than_keyed_ambiguously():
    """Silently producing e.g. "bank-statement__" would let two such sections
    share a prefix and delete each other's shards."""
    with pytest.raises(ValueError, match="no page ids"):
        shard_persistence_section_id("bank-statement", [])


# ---------------------------------------------------------------------------
# Producer vs consumer: the real service against the real handlers.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_service_keys_shards_under_the_id_the_cleanup_derives():
    """`ExtractionService._persist_section_id` is the producer. It takes a
    `SectionInfo`; the cleanup callers only have the `Section`. Those two paths
    must land on the same string."""
    from idp_common.config.models import IDPConfig
    from idp_common.extraction.service import ExtractionService

    section = _section()
    document = MagicMock()
    document.output_bucket = "out"
    document.input_key = "doc.pdf"
    document.sections = [section]
    document.errors = []

    service = ExtractionService(config=IDPConfig())
    section_info = service._prepare_section_info(document, section)

    assert service._persist_section_id(section_info) == shard_persistence_section_id(
        section.classification, section.page_ids
    )


@pytest.mark.unit
def test_the_camps_are_what_this_file_assumes():
    """Derive which handlers have a cleanup rather than trusting the list above.

    Without this, dropping the merge handler out of the parametrisation would also
    silently stop noticing a cleanup put back into it — and a cleanup there is the
    one that can turn a paid-for extraction into a permanent failure.
    """
    with_a_cleanup = [
        name
        for name in DEPLOYED_HANDLERS
        if _cleanup_of(_load(name.replace(".py", ""), name)) is not None
    ]
    assert with_a_cleanup == HANDLERS_WITH_A_CLEANUP, (
        f"{with_a_cleanup} expose a per-shard cleanup, expected "
        f"{HANDLERS_WITH_A_CLEANUP}. The shard-merge handler must have none: "
        "merge_section_shards requires every shard present, and its own tail can "
        "fail transiently after the merge has already succeeded."
    )


@pytest.mark.unit
@pytest.mark.parametrize("handler_module", HANDLERS_WITH_A_CLEANUP)
def test_a_deployed_cleanup_lists_the_prefix_shards_are_written_to(handler_module):
    """The regression pin, driven through the deployed handler.

    Each is given a fake S3 that answers a hit for the CORRECT prefix only —
    which is what the real bucket does. A cleanup listing the ordinal prefix gets
    an empty page and deletes nothing, so this fails rather than passing against a
    mock that answers everything.
    """
    module = _load(handler_module.replace(".py", ""), handler_module)
    section = _section()
    correct_prefix = shard_results_prefix(
        EXECUTION_ARN,
        shard_persistence_section_id(section.classification, section.page_ids),
    )
    shard_key = shard_result_key(
        EXECUTION_ARN,
        shard_persistence_section_id(section.classification, section.page_ids),
        0,
        4,
    )
    listed: list[str] = []

    def _list_objects_v2(Bucket, Prefix):  # noqa: N803 - boto3 kwarg casing
        listed.append(Prefix)
        if Prefix == correct_prefix:
            return {"Contents": [{"Key": shard_key}]}
        return {}

    s3 = MagicMock()
    s3.list_objects_v2.side_effect = _list_objects_v2

    with patch.object(module, "_get_s3_client", lambda: s3):
        _cleanup_of(module)("working", EXECUTION_ARN, section)

    assert listed == [correct_prefix], (
        f"{handler_module} listed {listed} instead of [{correct_prefix!r}]. Shards "
        "are written under a content-derived section id, so a prefix built from "
        "the ordinal section_id addresses a location nothing was written to."
    )
    s3.delete_objects.assert_called_once()
    assert s3.delete_objects.call_args.kwargs["Delete"] == {
        "Objects": [{"Key": shard_key}]
    }


@pytest.mark.unit
@pytest.mark.parametrize("handler_module", HANDLERS_WITH_A_CLEANUP)
def test_a_cleanup_that_finds_nothing_deletes_nothing_and_does_not_raise(
    handler_module,
):
    """An unsharded section has no shard objects at all; that is not an error, and
    a raise here would fail a document whose extraction had succeeded."""
    module = _load(handler_module.replace(".py", ""), handler_module)
    s3 = MagicMock()
    s3.list_objects_v2.return_value = {}
    with patch.object(module, "_get_s3_client", lambda: s3):
        _cleanup_of(module)("working", EXECUTION_ARN, _section())
    assert not s3.delete_objects.called
