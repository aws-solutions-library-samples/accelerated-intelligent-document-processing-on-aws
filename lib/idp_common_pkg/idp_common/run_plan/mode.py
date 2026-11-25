# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The processing mode a document runs in: Pipeline mode or BDA mode.

``use_bda`` in the document's configuration selects the mode, read at the revision
the document is pinned to. A profile that selects BDA mode but has no BDA project
linked runs in Pipeline mode instead. The queue processor uses the answer to
route the run and to map the run plan's start step; reprocess eligibility uses it
to say which start steps exist for a document.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)

PIPELINE = "pipeline"
BDA = "bda"


@dataclass(frozen=True)
class ProcessingMode:
    """The mode a run takes, and what it was decided from."""

    mode: Literal["pipeline", "bda"]
    bda_project_arn: Optional[str]
    #: The configuration at the pinned revision, or ``None`` when it could not
    #: be read.
    config: Any
    #: ``use_bda`` was true but no BDA project is linked to the profile, so the
    #: run takes Pipeline mode.
    fell_back: bool


def routing_configuration(
    manager: Any, config_version: str, config_revision: Optional[int]
) -> Any:
    """The configuration ``use_bda`` routing is read from.

    The document's pinned revision when it has one, so that a save made while
    the document was queued cannot send it down a branch its own configuration
    does not select (#1619). With no pin, the profile head, as before revisions
    existed.

    A pinned read that fails falls back to the profile head, with a WARNING
    naming the revision, rather than to pipeline mode. The head is what routed
    every document before this read was pinned, so the fallback is never worse
    than that; defaulting to pipeline would send every BDA document on the
    stack down the wrong branch whenever the revision cannot be read, which is
    what an unreadable bucket did when this read was first pinned. The steps
    after routing load the pinned revision themselves and fail the document if
    it is gone, so a revision that is really unavailable is still reported.
    A head read that fails as well raises, into the handling every head-read
    failure has always had.
    """
    if config_revision is None:
        return manager.get_merged_configuration(config_version)
    try:
        return manager.get_merged_configuration(
            config_version, revision=config_revision
        )
    except Exception as e:
        logger.warning(
            f"Could not read revision r{config_revision} of configuration profile "
            f"'{config_version}' for routing: {e}. Routing from the profile head.",
            exc_info=True,
        )
        return manager.get_merged_configuration(config_version)


def resolve_processing_mode(
    manager: Any, version: str, revision: Optional[int]
) -> ProcessingMode:
    """The mode for a document pinned to ``version`` at ``revision``.

    Never raises: a configuration that cannot be read gives Pipeline mode with no
    configuration, and a WARNING. ``manager`` is a ``ConfigurationManager``.
    """
    try:
        # Read use_bda from the full config (properly decompresses gzip
        # storage), at the revision the document is pinned to, so routing
        # agrees with every later step (#1619).
        config = routing_configuration(manager, version, revision)
        use_bda = getattr(config, "use_bda", False) if config else False
        if not use_bda:
            logger.info(
                f"Config version '{version}': use_bda=False, using pipeline mode"
            )
            return ProcessingMode(PIPELINE, None, config, False)

        # The per-version BDA project ARN is top-level metadata on the profile.
        bda_project_arn = manager.get_bda_project_arn(version)
        if bda_project_arn:
            logger.info(
                f"Config version '{version}': use_bda=True, "
                f"bda_project_arn={bda_project_arn}"
            )
            return ProcessingMode(BDA, bda_project_arn, config, False)

        # No BDA project linked — fall back to pipeline to avoid errors
        logger.warning(
            f"Config version '{version}' has use_bda=True but no BDA project ARN "
            f"linked. Falling back to pipeline mode. Please sync the config "
            f"version to a BDA project."
        )
        return ProcessingMode(PIPELINE, None, config, True)
    except Exception as e:
        logger.warning(
            f"Could not read config from ConfigurationManager: {e}. "
            f"Defaulting to pipeline mode.",
            exc_info=True,
        )
        return ProcessingMode(PIPELINE, None, None, False)
