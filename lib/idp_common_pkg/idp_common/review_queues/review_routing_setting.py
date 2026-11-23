# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``hitl.enabled`` (Review Routing) from the ACTIVE Configuration Profile.

Review Management shows a warning while routing rules are not evaluated, and one
cause is Review Routing being off. The queues it lists span profiles, so the
switch is read from the active profile (cached ~60 s, see
:class:`idp_common.config.active_profile.ActiveProfileValue`).

The value is ``None`` when the profile could not be read: the page then shows no
warning about the switch, because it does not know its state.
"""

from typing import Optional

from idp_common.config.active_profile import ActiveProfileValue
from idp_common.config.feature_flags import review_routing_enabled

_PROVIDER: Optional[ActiveProfileValue[Optional[bool]]] = None


def review_routing_enabled_provider() -> ActiveProfileValue[Optional[bool]]:
    """The module-wide cached reader (one per Lambda container)."""
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = ActiveProfileValue(
            review_routing_enabled, lambda: None, name="hitl.enabled"
        )
    return _PROVIDER
