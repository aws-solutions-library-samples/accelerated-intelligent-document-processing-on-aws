# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Review queue administration for IDP SDK.

Production review queues are what a profile's routing rules send documents to.
These operations list, create, change and delete them through the stack's review
queues resolver, which runs the same checks as the web UI's Review Management.
"""

from typing import Any, Dict, Optional, Union

from idp_sdk.exceptions import (
    IDPResourceNotFoundError,
    IDPValidationError,
)
from idp_sdk.models import ReviewQueueDefinition, ReviewQueuePage

#: The nested API resolver stack output that holds the review queues resolver's ARN.
REVIEW_QUEUES_OUTPUT = "ReviewQueuesResolverFunctionArn"

#: What the resolver raises, by class name, and the SDK error a caller gets.
_ERRORS = {
    "ValueError": IDPValidationError,
    "ResourceNotFound": IDPResourceNotFoundError,
}


class ReviewQueueOperation:
    """Administration of the production review queues (``client.review_queues``)."""

    def __init__(self, client):
        self._client = client

    def _invoke(self, field: str, arguments: Dict[str, Any], stack_name: Optional[str]):
        from idp_sdk._core.resolver_invoke import (
            ResolverFunctionError,
            invoke_api_resolver,
        )

        name = self._client._require_stack(stack_name)
        try:
            return invoke_api_resolver(
                name, self._client._region, REVIEW_QUEUES_OUTPUT, field, arguments
            )
        except ResolverFunctionError as e:
            error = _ERRORS.get(e.error_type)
            if error is None:
                raise
            raise error(e.error_message) from e

    @staticmethod
    def _input(
        definition: Union[Dict[str, Any], ReviewQueueDefinition],
    ) -> Dict[str, Any]:
        """The definition as the API's input: its own names, no read-only fields."""
        try:
            model = (
                definition
                if isinstance(definition, ReviewQueueDefinition)
                else ReviewQueueDefinition.model_validate(definition)
            )
        except ValueError as e:
            raise IDPValidationError(f"Not a review queue definition: {e}") from e
        return model.model_dump(
            by_alias=True, exclude_none=True, exclude={"is_default_queue", "version"}
        )

    def list(
        self,
        limit: int = 25,
        next_token: Optional[str] = None,
        stack_name: Optional[str] = None,
    ) -> ReviewQueuePage:
        """List the production review queues, a page at a time.

        Args:
            limit: Queues per page (the server allows up to 100)
            next_token: The ``next_token`` of the page before
            stack_name: Optional stack name override

        Returns:
            ReviewQueuePage; its ``next_token`` is None on the last page

        Raises:
            IDPValidationError: the server refused an argument
            IDPResourceNotFoundError: the stack has no review queues resolver output
            IDPProcessingError: the request failed
        """
        arguments: Dict[str, Any] = {"limit": limit}
        if next_token:
            arguments["nextToken"] = next_token
        page = self._invoke("listReviewQueues", arguments, stack_name) or {}
        queues = [
            ReviewQueueDefinition.model_validate(item)
            for item in page.get("items") or []
        ]
        return ReviewQueuePage(
            queues=queues, count=len(queues), next_token=page.get("nextToken") or None
        )

    def create(
        self,
        definition: Union[Dict[str, Any], ReviewQueueDefinition],
        stack_name: Optional[str] = None,
    ) -> ReviewQueueDefinition:
        """Create a review queue.

        Only ``id`` is required; what is left out takes the default for a new
        queue. A queue nobody is listed on (``eligible_annotators``) is worked by
        Admins only.

        Args:
            definition: The queue, as a ReviewQueueDefinition or a dict
            stack_name: Optional stack name override

        Returns:
            The queue as stored

        Raises:
            IDPValidationError: the definition is not valid, or the id is taken
        """
        return ReviewQueueDefinition.model_validate(
            self._invoke(
                "createReviewQueue", {"input": self._input(definition)}, stack_name
            )
        )

    def update(
        self,
        definition: Union[Dict[str, Any], ReviewQueueDefinition],
        expected_version: Optional[int] = None,
        stack_name: Optional[str] = None,
    ) -> ReviewQueueDefinition:
        """Replace a review queue's definition.

        The definition replaces the stored one: a field left out is reset to its
        default, except ``enabled`` and ``skip_reasons``, which keep their stored
        values. To change one setting, read the queue with ``list``, change the
        field and send the whole definition back.

        Args:
            definition: The queue, as a ReviewQueueDefinition or a dict
            expected_version: The ``version`` the definition was read at. When
                given, the update is refused if the queue was written since
                (``QUEUE_CHANGED``); when left out, the update replaces whatever
                is stored
            stack_name: Optional stack name override

        Returns:
            The queue as stored

        Raises:
            IDPValidationError: the definition is not valid, or the queue changed
                since ``expected_version``
        """
        update = self._input(definition)
        if expected_version is not None:
            update["version"] = expected_version
        return ReviewQueueDefinition.model_validate(
            self._invoke("updateReviewQueue", {"input": update}, stack_name)
        )

    def delete(self, queue_id: str, stack_name: Optional[str] = None) -> int:
        """Delete a review queue; the documents in it move to the Default queue.

        Routing rules that name the queue are not changed: documents they send
        there go to the Default queue from then on.

        Args:
            queue_id: The queue's id
            stack_name: Optional stack name override

        Returns:
            How many documents moved to the Default queue

        Raises:
            IDPValidationError: the Default queue cannot be deleted, or a
                document of the queue is being finalized
        """
        moved = self._invoke("deleteReviewQueue", {"id": queue_id}, stack_name)
        # True is what a stack answers whose resolver does not report the count.
        return moved if isinstance(moved, int) and not isinstance(moved, bool) else 0
