# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
IDP SDK Exceptions

Custom exception classes for the IDP SDK.
"""


class IDPError(Exception):
    """Base exception for all IDP SDK errors."""

    pass


class IDPConfigurationError(IDPError):
    """
    Raised when SDK is misconfigured.

    Examples:
        - stack_name required but not provided
        - Invalid region
        - Missing credentials
    """

    pass


class IDPStackError(IDPError):
    """
    Raised when there's an issue with the CloudFormation stack.

    Examples:
        - Stack not found
        - Stack in invalid state
        - Stack operation failed
    """

    pass


class IDPProcessingError(IDPError):
    """
    Raised when document processing fails.

    Examples:
        - Batch processing failure
        - Document upload failure
        - S3 operation failure
    """

    pass


class IDPValidationError(IDPError):
    """
    Raised when validation fails.

    Examples:
        - Invalid manifest format
        - Invalid configuration
        - Missing required fields
    """

    pass


class IDPResourceNotFoundError(IDPError):
    """
    Raised when a requested resource is not found.

    Examples:
        - Batch not found
        - Document not found
        - Test set not found
    """

    pass


class ReprocessRefusedError(IDPError):
    """
    Raised when the server refuses a whole reprocess request.

    A document that cannot be processed again is not an error: it comes back as
    that document's result, with a reason. This is raised when the request
    itself is refused and nothing was queued.

    Examples:
        - A prefix mapping refuses the configuration profile that was asked for
        - The request names a step or a profile revision that does not exist

    ``reason`` is the name of the error the server raised.
    """

    def __init__(self, message: str, reason: str = ""):
        self.reason = reason
        super().__init__(message)


class IDPTimeoutError(IDPError):
    """
    Raised when an operation times out.

    Examples:
        - Stack deployment timeout
        - Monitoring timeout
    """

    pass
