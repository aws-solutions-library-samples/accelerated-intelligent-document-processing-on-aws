# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one way the SDK and CLI work out which Region a call goes to."""

from typing import Optional


def resolve_region(region: Optional[str]) -> Optional[str]:
    """The Region a call goes to, worked out one way for the SDK and CLI.

    In order: ``region`` when one was given; otherwise the Region boto3's default
    session resolves for a client built with no Region (``AWS_DEFAULT_REGION``,
    then the ``region`` of the selected profile, or the one given to
    ``boto3.setup_default_session``); otherwise ``AWS_REGION``, which boto3 itself
    does not read but many shells and other AWS SDKs set on its own. None when
    none of those names a Region; there is no hard-coded default.

    boto3's answer comes before ``AWS_REGION`` so that the Region named here is
    the one a client built without a Region reaches. A caller that falls through
    to ``AWS_REGION`` must pass the answer to every client it builds, because
    boto3 would not find it.

    The default session is read rather than a freshly built
    ``boto3.session.Session()``, because a fresh session ignores a Region given to
    ``boto3.setup_default_session``; and rather than a client, because building a
    client resolves credentials (on EC2, an instance-metadata call). A session
    substituted for boto3's (a test double) carries no real Region, and is then
    passed over rather than returning a non-string.
    """
    if region:
        return region

    import os

    import boto3

    if boto3.DEFAULT_SESSION is None:
        boto3.setup_default_session()
    found = getattr(boto3.DEFAULT_SESSION, "region_name", None)
    if isinstance(found, str) and found:
        return found
    return os.environ.get("AWS_REGION") or None
