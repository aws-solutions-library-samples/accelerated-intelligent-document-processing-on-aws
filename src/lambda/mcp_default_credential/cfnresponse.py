# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Send a custom resource's result to CloudFormation.

A vendored equivalent of the ``cfn-response`` module that AWS publishes in the
CloudFormation User Guide ("cfn-response module"), with the same interface:
``send(event, context, responseStatus, responseData, physicalResourceId=None,
noEcho=False, reason=None)`` and the ``SUCCESS`` / ``FAILED`` constants. It is
kept here instead of being installed by bare name from the public package
index. CloudFormation provides that module only to functions whose code is
inline in the template, and this function's code is not.

It differs from the published module in these ways, none of which changes
what CloudFormation receives:

- The PUT has an explicit timeout (``TIMEOUT_SECONDS``). The published module
  sets none, so an unresponsive endpoint could hold the invocation until the
  function timed out.
- It uses the standard library's ``urllib.request`` rather than ``urllib3``,
  so the module has no dependency of its own.
- It does not log the response URL, which is a presigned URL, and it sends
  only to an ``https`` URL.

As in the published module, a failed PUT is logged and not raised.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

SUCCESS = "SUCCESS"
FAILED = "FAILED"

# Seconds allowed for each blocking step of the PUT (connect, send, read the
# reply). The response body is a few hundred bytes, and the handlers leave
# about 20 seconds of their invocation for this call.
TIMEOUT_SECONDS = 10

logger = logging.getLogger(__name__)


def send(
    event: dict[str, Any],
    context: Any,
    responseStatus: str,
    responseData: Any,
    physicalResourceId: str | None = None,
    noEcho: bool = False,
    reason: str | None = None,
) -> None:
    """PUT the result of a custom resource request to its ``ResponseURL``."""
    response_url = event["ResponseURL"]
    response_body = {
        "Status": responseStatus,
        "Reason": reason
        or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physicalResourceId or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": noEcho,
        "Data": responseData,
    }
    body = json.dumps(response_body).encode("utf-8")
    logger.info("Response body: %s", body.decode("utf-8"))

    if not response_url.lower().startswith("https://"):
        logger.error("Not sending the response: ResponseURL is not an https URL")
        return

    # An empty Content-Type, as in the published module: the presigned URL
    # CloudFormation supplies is signed without one.
    request = urllib.request.Request(
        response_url,
        data=body,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(body))},
    )
    try:
        # The scheme is checked above, so this cannot open a file: or other URL.
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # nosec B310
            logger.info("Status code: %s", response.status)
    except Exception as e:
        logger.error("send(..) failed executing the PUT request: %s", e)
