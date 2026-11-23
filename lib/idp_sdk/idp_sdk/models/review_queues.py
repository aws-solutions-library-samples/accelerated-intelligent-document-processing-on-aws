# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review queue models: the production queues documents are routed to for review."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class ReviewQueueDefinition(BaseModel):
    """A production review queue.

    Fields may be given by their Python names or by the names the API uses
    (``reject_reasons`` or ``rejectReasons``), so a definition can be built from
    a YAML or JSON file written either way.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str = Field(
        description=(
            "Queue id: lower-case letters, digits and hyphens. Routing rules "
            "name a queue by its id, so it does not change"
        )
    )
    name: Optional[str] = Field(default=None, description="Name shown to people")
    description: Optional[str] = Field(
        default=None, description="What the queue is for"
    )
    priority: Optional[int] = Field(
        default=None, description="Queues with a higher priority are worked first"
    )
    checklist: Optional[List[str]] = Field(
        default=None, description="What a reviewer confirms before completing"
    )
    reject_reasons: Optional[List[str]] = Field(
        default=None, description="Reasons a reviewer can reject a document for"
    )
    skip_reasons: Optional[List[str]] = Field(
        default=None, description="Reasons a reviewer can skip a document for"
    )
    time_limit_minutes: Optional[int] = Field(
        default=None, description="Minutes a claimed document is held for its reviewer"
    )
    eligible_annotators: Optional[List[str]] = Field(
        default=None,
        description="Emails of the Annotators who work this queue (none means none)",
    )
    enabled: Optional[bool] = Field(
        default=None,
        description="A disabled queue takes no new documents; they go to Default",
    )
    is_default_queue: Optional[bool] = Field(
        default=None, description="Whether this is the Default queue (read only)"
    )
    version: Optional[int] = Field(
        default=None,
        description="Changes with every write; pass it to update() as expected_version",
    )


class ReviewQueuePage(BaseModel):
    """One page of review queues.

    Iterating the page iterates its queues. ``next_token`` is set when there are
    more: pass it to the next ``list`` call.
    """

    queues: List[ReviewQueueDefinition] = Field(description="The queues of this page")
    count: int = Field(description="Number of queues in this page")
    next_token: Optional[str] = Field(
        default=None, description="Token for the next page; None on the last one"
    )

    def __iter__(self):
        return iter(self.queues)

    def __len__(self):
        return len(self.queues)

    def __getitem__(self, index):
        return self.queues[index]
