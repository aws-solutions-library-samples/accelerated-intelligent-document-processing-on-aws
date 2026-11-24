# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which configuration profiles send documents to a review queue.

Before a queue is deleted or disabled, an Admin is shown the routing rules that
name it. A rule keeps naming a queue after the queue is gone, and a run then
places the document in the Default queue instead, so the list is what tells the
Admin which profiles will start doing that.

Two configurations of a profile can matter. New documents run the profile's
published revision. When the newest revision is another one, what the editor
shows and what documents run differ, and a rule in either refers to the queue,
so both are read and each reference says which it is from.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List, Optional, Sequence, Tuple

from pydantic import BaseModel

from idp_common.config.routing_models import review_queue_of
from idp_common.config_scope import scope_allows

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_PROFILES_PER_PAGE",
    "MAX_PROFILES_PER_PAGE",
    "QueueReference",
    "find_queue_references",
]

#: How many profiles one call reads.
DEFAULT_PROFILES_PER_PAGE = 10
MAX_PROFILES_PER_PAGE = 25
_MAX_WORKERS = 8


class QueueReference(BaseModel):
    """One place a profile's routing table sends documents to a queue."""

    profile: str
    #: The revision the reference is in; ``None`` for a profile with no revisions.
    revision: Optional[int] = None
    #: True when the reference is in the configuration new documents run.
    published: bool
    #: The rule; empty when it is the table's default outcome.
    rule_id: str
    rule_label: str = ""


def _references_in(
    config: Any,
    queue_id: str,
    *,
    profile: str,
    revision: Optional[int],
    published: bool,
) -> List[QueueReference]:
    """The rules, and the default outcome, of ``config`` that name ``queue_id``.

    A disabled rule counts: it still names the queue, and enabling it again
    would send documents there.
    """
    found: List[QueueReference] = []
    routing = getattr(config, "routing", None)
    for table in getattr(routing, "tables", None) or []:
        for rule in table.rules:
            if review_queue_of(rule.actions) == queue_id:
                found.append(
                    QueueReference(
                        profile=profile,
                        revision=revision,
                        published=published,
                        rule_id=rule.id,
                        rule_label=rule.label,
                    )
                )
        if review_queue_of(table.default_outcome.actions) == queue_id:
            found.append(
                QueueReference(
                    profile=profile, revision=revision, published=published, rule_id=""
                )
            )
    return found


def _profile_references(
    manager: Any, profile: dict, queue_id: str
) -> List[QueueReference]:
    """The references of one profile: its newest configuration, and the published
    revision when that is another one."""
    name = profile["versionName"]
    latest = profile.get("latestRevision")
    published = profile.get("publishedRevision")
    newest_is_published = published is None or published == latest
    found = _references_in(
        manager.get_merged_configuration(name),
        queue_id,
        profile=name,
        revision=latest,
        published=newest_is_published,
    )
    if not newest_is_published:
        found += _references_in(
            manager.get_merged_configuration(name, revision=published),
            queue_id,
            profile=name,
            revision=published,
            published=True,
        )
    return found


def find_queue_references(
    manager: Any,
    queue_id: str,
    *,
    limit: int = DEFAULT_PROFILES_PER_PAGE,
    cursor: Optional[str] = None,
    allowed_versions: Optional[Sequence[str]] = None,
) -> Tuple[List[QueueReference], Optional[str]]:
    """One page of the references to ``queue_id``, and the next page's cursor.

    A page covers up to ``limit`` profiles (at most ``MAX_PROFILES_PER_PAGE``),
    in name order after ``cursor``, which is the last profile name of the page
    before. It holds every reference those profiles have, which can be none, so
    a caller pages until the cursor is ``None`` and does not stop at an empty
    page. The references are in profile order, then newest configuration before
    published revision, then rule order.

    Args:
        manager: A ``ConfigurationManager``.
        queue_id: The review queue to look for.
        limit: How many profiles to read.
        cursor: The cursor a previous call returned.
        allowed_versions: The caller's configuration scope; ``None`` reads every
            profile.

    Raises:
        Whatever the manager raises for a profile list or a configuration it
        cannot read: an incomplete list would read as "nothing references this
        queue".
    """
    limit = max(1, min(MAX_PROFILES_PER_PAGE, limit))
    profiles = sorted(
        (
            profile
            for profile in manager.list_config_versions(raise_errors=True)
            if scope_allows(allowed_versions, profile["versionName"])
            and (cursor is None or profile["versionName"] > cursor)
        ),
        key=lambda profile: profile["versionName"],
    )
    page = profiles[:limit]
    next_cursor = page[-1]["versionName"] if len(profiles) > limit else None
    if not page:
        return [], None
    with ThreadPoolExecutor(
        max_workers=min(_MAX_WORKERS, len(page)),
        thread_name_prefix="queue-references",
    ) as pool:
        per_profile = list(
            pool.map(lambda p: _profile_references(manager, p, queue_id), page)
        )
    return [reference for found in per_profile for reference in found], next_cursor
