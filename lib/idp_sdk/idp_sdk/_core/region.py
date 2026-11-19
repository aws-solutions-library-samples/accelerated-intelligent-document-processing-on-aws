# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one way the SDK and CLI work out which Region a call goes to."""

from typing import Optional


def resolve_region(region: Optional[str]) -> Optional[str]:
    """The Region a boto3 client built with ``region_name=region`` talks to.

    ``region`` when one was given; otherwise the Region boto3's default session
    resolves for a client built with no Region: ``AWS_DEFAULT_REGION``, then the
    ``region`` of the selected profile, or the one given to
    ``boto3.setup_default_session``. boto3 does not read ``AWS_REGION``, so a
    process that reads that variable itself, or falls back to a hard-coded Region,
    can address a different Region from the one its own clients reach. Every
    caller here asks this function instead, so a stack is looked up, its tables
    read and its models called in the same Region.

    The answer is boto3's default session's, which is where a client built with
    no Region takes its Region from, rather than a freshly built
    ``boto3.session.Session()``'s, because a fresh session ignores a Region given
    to ``boto3.setup_default_session``. Reading the session rather than building
    a client also leaves credentials unresolved, so asking costs no credential
    lookup (on EC2, an instance-metadata call). A session substituted for boto3's
    (a test double) carries no real Region, and then the answer is None rather
    than a non-string. None also means nothing resolves at all; there is no
    hard-coded default.
    """
    if region:
        return region

    import boto3

    if boto3.DEFAULT_SESSION is None:
        boto3.setup_default_session()
    found = getattr(boto3.DEFAULT_SESSION, "region_name", None)
    return found if isinstance(found, str) and found else None
