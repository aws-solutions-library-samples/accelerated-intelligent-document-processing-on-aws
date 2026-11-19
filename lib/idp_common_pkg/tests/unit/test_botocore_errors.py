# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The shapes ``tests/unit/_botocore_errors.py`` hands the retry tests (#2050).

The helper's value is that botocore, not the test, decides what an error looks like,
so this pins what botocore decides for each reply the retry tests depend on. A
botocore upgrade that changed one of these shapes would change what every migrated
retry test is exercising, and should stop here first rather than pass there.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from tests.unit._botocore_errors import (
    SERVER_ERROR_STATUSES,
    empty_reply,
    html_reply,
    named_reply,
    service_error,
)


@pytest.mark.unit
def test_each_reply_becomes_the_error_botocore_builds_for_it():
    wrong = []

    def check(what, error, cls, code, status):
        got = (
            type(error).__name__,
            error.response["Error"]["Code"],
            error.response["ResponseMetadata"]["HTTPStatusCode"],
        )
        if got != (cls, code, status) or not isinstance(error, ClientError):
            wrong.append(f"{what}: got {got}, expected {(cls, code, status)}")

    for status in (429, *SERVER_ERROR_STATUSES):
        for reply in (empty_reply(status), html_reply(status)):
            # No error type in the reply, so botocore makes the bare status the code
            # (#2036) and raises the base class.
            for operation in ("Converse", "ConverseStream", "InvokeModel"):
                what = f"{reply.label} {operation}"
                check(
                    what,
                    service_error(reply, operation),
                    "ClientError",
                    str(status),
                    status,
                )

    # A named error is the client's modelled subclass, at the status the model
    # declares for it; an unmodelled name keeps the status it was given.
    for code, status in [
        ("ThrottlingException", 429),
        ("InternalServerException", 500),
        ("ServiceUnavailableException", 503),
    ]:
        check(code, service_error(named_reply(code)), code, code, status)
    check(
        "InternalFailure",
        service_error(named_reply("InternalFailure", 500)),
        "ClientError",
        "InternalFailure",
        500,
    )
    check(
        "Lambda TooManyRequestsException",
        service_error(
            named_reply("TooManyRequestsException", service="lambda"),
            "Invoke",
            "lambda",
        ),
        "TooManyRequestsException",
        "TooManyRequestsException",
        429,
    )
    assert not wrong, "\n".join(wrong)

    # A fresh exception each call, so a test raising it twice builds no chain.
    reply = empty_reply(503)
    assert service_error(reply) is not service_error(reply)

    # A name the model does not declare is refused rather than given a guessed status.
    with pytest.raises(ValueError, match="pass it explicitly"):
        named_reply("NoSuchBedrockError")
