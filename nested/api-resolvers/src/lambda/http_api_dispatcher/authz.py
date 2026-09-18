# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Default-deny group authorization for the HTTP API dispatcher.

WHY THIS EXISTS
---------------
Under AppSync the schema directive
``@aws_cognito_user_pools(cognito_groups: [...])`` gated a field at the API layer
before any resolver ran, and a field the caller's groups did not satisfy was
rejected there. The REST API that replaced AppSync has one route
(``POST /op/{field}``) behind a Cognito authorizer that **authenticates only** —
it reads no group directives. Authorization therefore became opt-in per resolver:
an operation whose resolver omits its check is reachable by any authenticated
caller (including a caller in no group at all, which self-signup can produce),
and a NEWLY added operation is open unless somebody remembers to add a check.

This module restores the AppSync posture at the API layer. Every request must
name an operation that appears in the bundled manifest AND satisfy that
operation's required groups, or it is denied before dispatch.

**Unmapped means denied.** A field with no manifest entry is rejected — that is
the whole point, not an edge case. A new operation is closed until its required
groups are declared in ``scripts/api_rbac_expectations.yaml`` (which
``scan_api_rbac.py`` already forces for any routable op), rather than open until
someone notices.

RELATIONSHIP TO THE RESOLVER CHECKS
-----------------------------------
This is a floor, not a replacement. The per-resolver group checks (and
``ddb_direct._REQUIRED_GROUPS``) stay exactly as they are: they are the layer
that also enforces per-object scope (config version, test set, ownership), which
a field-level manifest cannot express. Both layers deny with ``PermissionError``,
which the dispatcher maps to HTTP 403 / ``errorType: "Unauthorized"``.

FAIL-CLOSED, UNLIKE ``validation.py``
-------------------------------------
``validation.py`` deliberately fails **open** on its own internal errors: a bug
in an input-shape checker must not 500 the API. This module fails **closed** — a
missing, unreadable or structurally invalid manifest denies every request. The
two are not inconsistent: a validator that lets a malformed argument through
loses a defense-in-depth check, while an authorization layer that lets an unknown
operation through loses the guarantee it exists for. The manifest is generated at
build time, committed in this Lambda's CodeUri and drift-guarded in CI
(``generate_api_rbac_manifest.py --check`` plus a unit test), so "manifest
missing" means a broken build, which is a condition to surface loudly rather than
absorb.

"Structurally invalid" is checked at LOAD time, not at comparison time, because
the comparison is where fail-closed would otherwise stop being true. ``enforce``
tests membership with ``set(required)``, and Python builds a set from a bare
string just as happily as from a list: an entry of ``"Admin"`` rather than
``["Admin"]`` would become the CHARACTER set ``{'A','d','m','i','n'}``, which a
caller in a single-character group would satisfy — a fail-OPEN outcome from a
malformed policy. A non-iterable value (a number) fails the other way but no
better: ``TypeError`` inside ``enforce`` becomes a 500, not a 403. The generator
emits only lists and the two sentinels, so neither shape is reachable through
it; the point of validating anyway is that the paragraph above is a guarantee
rather than an observation about the current generator.

OPERABILITY OF THE DENY-ALL STATE
---------------------------------
Denying everything is correct but it looks, from the outside, exactly like an
outage: every caller gets a 403 that is indistinguishable per-request from a
legitimate authorization denial. So the condition is announced once at cold
start under the stable marker ``DENY_ALL_MARKER`` — alarm on that string in this
function's log group. The status deliberately stays 403 rather than becoming
something more obviously exceptional: a 5xx would invite a retry, and the
request really is unauthorized. The explanation belongs in the logs, not in the
status code.
"""

import json
import logging
import os
from typing import Any, Dict, List, Union

logger = logging.getLogger()

_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "api_rbac_manifest.json")

# Manifest schema version this code understands. A manifest from the future is
# treated as unreadable (deny) rather than interpreted optimistically.
_SUPPORTED_VERSION = 1

# Policy sentinels, carried through from scripts/api_rbac_expectations.yaml.
_ANY = "ANY"  # any authenticated Cognito caller
_IAM_ONLY = "IAM_ONLY"  # backend/IAM principals only; no Cognito caller
_SENTINELS = (_ANY, _IAM_ONLY)

# "This field has no policy entry at all" -> DENY.
#
# A distinct sentinel OBJECT, not the ``None`` that ``dict.get`` would hand back,
# because ``None`` is the sentinel ``ddb_direct._REQUIRED_GROUPS`` uses for the
# exact OPPOSITE meaning: there it means "any authenticated caller". Two tables
# on the same request path must not give one value two opposite meanings, so
# ``ddb_direct`` names its own ``_ANY_AUTHENTICATED`` and this one is named here.
# ``_load_manifest`` also rejects a JSON ``null`` policy outright, so ``None``
# cannot reach ``REQUIRED_GROUPS`` and be mistaken for either.
_UNDECLARED = object()

# Stable, greppable marker for "the policy file is unusable, so I am refusing
# every operation". Alarm on this string in this function's log group. Without
# it the symptom — every request 403 — reads as an authorization problem with the
# callers rather than as a missing policy file, which is a different fault with a
# different fix. There is no CloudWatch metric alongside it because the
# dispatcher emits no metrics at all; adding a PutMetricData/EMF path for this
# one condition would put a new dependency on the request path.
DENY_ALL_MARKER = "API_RBAC_MANIFEST_UNAVAILABLE"


def _validated_policy(field: str, policy: object) -> Union[str, List[str]]:
    """Return ``policy`` if ``enforce`` can evaluate it; raise ``ValueError`` if not.

    See the "FAIL-CLOSED" section of the module docstring for why a bare string
    or a non-iterable has to be rejected here rather than tolerated at the
    comparison in ``enforce``.
    """
    if isinstance(policy, str):
        if policy not in _SENTINELS:
            raise ValueError(
                f"{field}: policy {policy!r} is a bare string that is not one of "
                f"{_SENTINELS}; a list of group names was expected"
            )
        return policy
    if not isinstance(policy, list) or not policy:
        raise ValueError(
            f"{field}: policy must be a non-empty list of group names or one of "
            f"{_SENTINELS}, not {type(policy).__name__}"
        )
    if not all(isinstance(g, str) and g for g in policy):
        raise ValueError(f"{field}: policy contains a non-string group name")
    return policy


def _load_manifest() -> Dict[str, Union[str, List[str]]]:
    """Load the bundled required-groups manifest once, at cold start.

    On any failure — unreadable file, unsupported version, or an entry in a shape
    ``enforce`` cannot evaluate — return an EMPTY map, which denies every
    operation (see the module docstring). The error is logged at ERROR so the
    cause is one log line away rather than inferred from a wall of 403s.

    One bad entry rejects the WHOLE manifest rather than just that operation, for
    the same reason the version check does: the manifest is a generated artifact
    with a CI drift guard, so any structural deviation means a broken build, and
    a build that produced one malformed policy is not a build to trust the other
    117 entries from.
    """
    try:
        with open(_MANIFEST_PATH, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        version = manifest.get("version")
        if version != _SUPPORTED_VERSION:
            logger.error(
                "api_rbac_manifest.json version %s is not supported (expected %s); "
                "denying all operations",
                version,
                _SUPPORTED_VERSION,
            )
            return {}
        operations = manifest.get("operations") or {}
        if not isinstance(operations, dict) or not operations:
            logger.error(
                "api_rbac_manifest.json carries no operations; denying all requests"
            )
            return {}
        validated: Dict[str, Union[str, List[str]]] = {}
        for field, policy in operations.items():
            if not isinstance(field, str) or not field:
                raise ValueError(f"operation key {field!r} is not a field name")
            validated[field] = _validated_policy(field, policy)
        return validated
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Could not load a usable api_rbac_manifest.json (%s); denying all "
            "operations. The manifest is generated by "
            "scripts/sdlc/generate_api_rbac_manifest.py and must be bundled in "
            "this function's CodeUri.",
            e,
        )
        return {}


def _announce_if_denying_everything(required: Dict[str, Union[str, List[str]]]) -> None:
    """Log the alarmable marker if no policy at all was loaded.

    Called once per cold start rather than per request: the per-request warning in
    ``enforce`` cannot say WHY the field is unmapped, and 118 operations' worth of
    them would bury this line anyway.
    """
    if required:
        return
    logger.error(
        "%s: no usable required-groups manifest was loaded, so EVERY API "
        "operation will be refused with 403 until this is fixed. This is a "
        "build/packaging fault rather than an authorization decision about the "
        "callers — see the ERROR logged immediately above for the cause. Alarm "
        "on this marker.",
        DENY_ALL_MARKER,
    )


REQUIRED_GROUPS: Dict[str, Union[str, List[str]]] = _load_manifest()
_announce_if_denying_everything(REQUIRED_GROUPS)


def caller_groups(event: Dict[str, Any]) -> List[str]:
    """The caller's Cognito groups, from the VERIFIED token claim only.

    Read from ``identity.claims['cognito:groups']``, which
    ``idp_common.api_adapter.normalize_event`` populates from the API Gateway
    authorizer's JWT claims (and normalizes back to a list). Never from the
    request body or a header, both of which the caller controls.
    """
    identity = event.get("identity") or {}
    claims = identity.get("claims") or {}
    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return [g for g in groups if isinstance(g, str)]


def enforce(field: str, event: Dict[str, Any]) -> None:
    """Raise ``PermissionError`` unless the caller may invoke ``field``.

    ``PermissionError`` is the dispatcher's existing authorization-denial idiom:
    the handler already maps it to 403 with ``errorType: "Unauthorized"``, which
    is what the UI keys on, so a denial here is indistinguishable to the client
    from a denial raised inside a resolver.
    """
    required = REQUIRED_GROUPS.get(field, _UNDECLARED)

    if required is _UNDECLARED:
        # Unmapped: either an unknown field or a routable one whose policy was
        # never declared. Both are denied. The message deliberately does not
        # distinguish the two, so it cannot be used to enumerate operations.
        logger.warning(
            "Denied %s: no required-groups entry in api_rbac_manifest.json "
            "(default deny). Declare the operation's groups in "
            "scripts/api_rbac_expectations.yaml and regenerate the manifest.",
            field,
        )
        raise PermissionError(f"Unauthorized: {field} is not an authorized operation")

    if required == _IAM_ONLY:
        logger.warning("Denied %s: IAM-only operation invoked by a Cognito caller", field)
        raise PermissionError(f"Unauthorized: {field} is not callable via the API")

    if required == _ANY:
        # Any authenticated caller. Authentication itself is established by the
        # route's Cognito authorizer (asserted by check S5 of scan_api_rbac.py),
        # which rejects a missing or invalid token with 401 before this runs.
        return

    # ``required`` is a non-empty list of group-name strings: _load_manifest
    # rejected every other shape, so this set() cannot be a character set built
    # from a bare string, nor a TypeError from a non-iterable.
    groups = caller_groups(event)
    if not set(required).intersection(groups):
        # Log the caller's groups (not the token) so a legitimate denial can be
        # told apart from a misconfigured group mapping.
        logger.warning(
            "Denied %s: caller groups %s do not include any of %s",
            field,
            groups,
            sorted(required),
        )
        raise PermissionError(
            f"Unauthorized: {field} requires one of {sorted(required)}"
        )
