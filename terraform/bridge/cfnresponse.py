"""Stand-in for the ``cfnresponse`` module CloudFormation provides to inline functions.

CloudFormation makes a module named ``cfnresponse`` importable by Python Lambda
functions whose code is given inline (``Code.ZipFile``). A Terraform deployment
of the same function has no such module, so the translator packages this file
beside every inline function that imports it. It implements the documented
interface: ``SUCCESS``, ``FAILED`` and
``send(event, context, responseStatus, responseData, physicalResourceId=None,
noEcho=False, reason=None)``.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

SUCCESS = "SUCCESS"
FAILED = "FAILED"


def send(
    event: dict[str, Any],
    context: Any,
    responseStatus: str,  # noqa: N803 - the documented parameter names
    responseData: dict[str, Any] | None,  # noqa: N803
    physicalResourceId: str | None = None,  # noqa: N803
    noEcho: bool = False,  # noqa: N803
    reason: str | None = None,
) -> None:
    body = {
        "Status": responseStatus,
        "Reason": reason
        or f"See the details in CloudWatch Log Stream: {context.log_stream_name}",
        "PhysicalResourceId": physicalResourceId or context.log_stream_name,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "NoEcho": noEcho,
        "Data": responseData or {},
    }
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        event["ResponseURL"],
        data=data,
        method="PUT",
        headers={"Content-Type": "", "Content-Length": str(len(data))},
    )
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310  # nosec B310 - URL is the custom-resource response URL from the invoking event
            print(f"Status code: {response.status}")
    except Exception as exc:  # noqa: BLE001 - match the original: log and return
        print(f"send(..) failed executing request: {exc}")
