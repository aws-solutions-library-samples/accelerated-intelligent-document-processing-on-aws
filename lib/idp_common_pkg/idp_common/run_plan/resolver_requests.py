# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Ask the reprocess resolver to process many documents again, in parts.

The reprocess resolver is the one place that decides whether a document can be
processed again and queues it. It has a fixed time to answer, so a caller with
many documents sends them in requests of bounded size and joins the answers. The
SDK and the MCP ``reprocess`` tool both do, with the function here: each gives it
a ``send`` that makes one request its own way (the SDK through the stack's
outputs, the tool with the caller it acts for).

Pure: this module imports the standard library and the step list only.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Sequence

from .steps import RunPlanError, StepNotInMode, UnknownStep

#: What a document's result says happened to it.
QUEUED = "QUEUED"
REFUSED = "REFUSED"
FAILED = "FAILED"

#: Documents per request to the resolver.
DOCUMENTS_PER_REQUEST = 25

#: What the resolver raises to refuse a request, by class name, as opposed to
#: failing on it: an authorization refusal, a bad argument, a step that is not one.
REFUSAL_ERRORS = frozenset(
    {
        "PermissionError",
        "ValueError",
        *(error.__name__ for error in (RunPlanError, UnknownStep, StepNotInMode)),
    }
)


class ResolverCallError(Exception):
    """The resolver function raised. Carries what it raised.

    ``error_type`` is the name of the exception class in the function and
    ``error_message`` its text. ``send`` raises this, or a subclass of it.
    """

    def __init__(self, error_type: str, error_message: str):
        self.error_type = error_type
        self.error_message = error_message
        super().__init__(error_message)

    @property
    def is_refusal(self) -> bool:
        """Whether the function refused the request, rather than failed on it."""
        return self.error_type in REFUSAL_ERRORS


def reprocess_in_requests(
    send: Callable[[Dict[str, Any]], Any],
    object_keys: Sequence[str],
    arguments: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Send ``reprocessDocument`` for every document; one result entry per document.

    ``send`` takes the arguments of one request (``arguments`` with that
    request's ``objectKeys``) and returns the resolver's answer, or raises
    :class:`ResolverCallError` when the function raised. Each entry is what the
    resolver returns for a document: ``objectKey``, ``status``, ``startStep``,
    ``reason`` and ``message``.

    The first request decides whether the request as a whole is acceptable: an
    error from it is raised as it is, and nothing was queued. A later request
    that is refused or fails is reported on the documents it carried (``REFUSED``
    or ``FAILED``, with the error's class as ``reason`` and its text as
    ``message``), because documents of the earlier requests are already queued,
    and the remaining requests are still sent.

    Raises:
        ResolverCallError: the first request was refused or failed.
        ValueError: an answer has no ``results`` list.
    """
    entries: List[Dict[str, Any]] = []
    for start in range(0, len(object_keys), DOCUMENTS_PER_REQUEST):
        keys = list(object_keys[start : start + DOCUMENTS_PER_REQUEST])
        try:
            answer = send({"objectKeys": keys, **arguments})
        except ResolverCallError as error:
            if start == 0:
                raise
            entries.extend(
                {
                    "objectKey": key,
                    "status": REFUSED if error.is_refusal else FAILED,
                    "startStep": None,
                    "reason": error.error_type,
                    "message": error.error_message,
                }
                for key in keys
            )
            continue
        if not isinstance(answer, dict) or not isinstance(answer.get("results"), list):
            raise ValueError(
                "The reprocess resolver returned an answer with no results: "
                f"{answer!r:.200}"
            )
        entries.extend(answer["results"])
    return entries
