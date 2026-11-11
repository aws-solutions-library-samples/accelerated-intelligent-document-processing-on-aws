# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Changes to the stack's MCP gateway that must keep the rest of its configuration.

``UpdateGateway`` REPLACES a gateway's configuration rather than patching it.
Measured against a live gateway: an update that omitted
``interceptorConfigurations`` removed the interceptor, and one that omitted
``protocolConfiguration`` turned semantic search off, which cannot then be turned
back on for a gateway that has targets. So every change here reads the gateway and
writes back every field it has, with only the intended field altered, and the list
of fields comes from the client's own service model so a field the API gains is not
cleared by the next update.

The caller-interceptor custom resource uses it to attach the interceptor and, on
Delete, to detach it, so a rollback or a downgrade never leaves the gateway naming
an interceptor function that no longer exists.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Sequence

logger = logging.getLogger(__name__)


def gateway_update_fields(control_client: Any) -> tuple:
    """Every field ``UpdateGateway`` accepts besides the identifier."""
    shape = control_client.meta.service_model.operation_model("UpdateGateway")
    return tuple(
        name for name in shape.input_shape.members if name != "gatewayIdentifier"
    )


def full_update_request(
    gateway: Dict[str, Any], fields: Sequence[str], **changes: Any
) -> Dict[str, Any]:
    """An ``UpdateGateway`` request keeping every field of ``gateway`` except
    ``changes``. Fields the gateway does not have are left out, not sent as null."""
    request: Dict[str, Any] = {"gatewayIdentifier": gateway["gatewayId"]}
    for field in fields:
        value = changes[field] if field in changes else gateway.get(field)
        if value is not None:
            request[field] = value
    return request


#: States from which an update may be attempted. A gateway left in
#: ``UPDATE_UNSUCCESSFUL`` by an earlier failed update still serves its previous
#: configuration and accepts a new update; refusing to try from it would make every
#: later stack operation fail without ever retrying.
UPDATABLE = ("READY", "UPDATE_UNSUCCESSFUL")


def wait_until_ready(
    control_client: Any,
    gateway_id: str,
    attempts: int = 60,
    delay: float = 5,
    accept: Sequence[str] = ("READY",),
) -> Dict[str, Any]:
    status = None
    for _ in range(attempts):
        gateway = control_client.get_gateway(gatewayIdentifier=gateway_id)
        status = gateway.get("status")
        if status in accept:
            return gateway
        if status in ("FAILED", "UPDATE_UNSUCCESSFUL"):
            break
        time.sleep(delay)
    raise RuntimeError(f"gateway {gateway_id} did not become READY (status {status})")


def desired_interceptors(interceptor_arn: str) -> List[Dict[str, Any]]:
    """One Lambda for both points, with request headers passed so it can read the
    caller's validated access token."""
    return [
        {
            "interceptor": {"lambda": {"arn": interceptor_arn}},
            "interceptionPoints": ["REQUEST", "RESPONSE"],
            "inputConfiguration": {"passRequestHeaders": True},
        }
    ]


def ensure_interceptor(control_client: Any, gateway_id: str, interceptor_arn: str):
    """Attach the caller interceptor, keeping everything else. ``True`` when an
    update was made. Raises when it cannot be applied, so the caller's operation
    fails rather than leaving a gateway without it.

    The stack owns the gateway's interceptor list: it is set to exactly this one,
    so an interceptor added to the gateway by hand is removed on the next stack
    update. That is deliberate. The handler accepts only this interceptor's signed
    caller block, and an interceptor the stack did not deploy is one nothing here
    has tested in front of it."""
    if not interceptor_arn:
        raise ValueError("InterceptorArn is required")
    gateway = wait_until_ready(control_client, gateway_id, accept=UPDATABLE)
    desired = desired_interceptors(interceptor_arn)
    if gateway.get("interceptorConfigurations") == desired:
        logger.info(f"Gateway {gateway_id} already carries the caller interceptor")
        return False
    logger.info(f"Attaching the caller interceptor to gateway {gateway_id}")
    control_client.update_gateway(
        **full_update_request(
            gateway,
            gateway_update_fields(control_client),
            interceptorConfigurations=desired,
        )
    )
    wait_until_ready(control_client, gateway_id)
    return True


def detach_interceptor(control_client: Any, gateway_id: str, interceptor_arn: str):
    """Remove the interceptor naming ``interceptor_arn``, keeping everything else.
    ``True`` when an update was made; a gateway that does not exist is a no-op."""
    try:
        gateway = wait_until_ready(control_client, gateway_id, accept=UPDATABLE)
    except control_client.exceptions.ResourceNotFoundException:
        return False
    current = list(gateway.get("interceptorConfigurations") or [])
    remaining = [
        c
        for c in current
        if ((c.get("interceptor") or {}).get("lambda") or {}).get("arn")
        != interceptor_arn
    ]
    if remaining == current:
        return False
    control_client.update_gateway(
        **full_update_request(
            gateway,
            gateway_update_fields(control_client),
            interceptorConfigurations=remaining or None,
        )
    )
    wait_until_ready(control_client, gateway_id)
    return True
