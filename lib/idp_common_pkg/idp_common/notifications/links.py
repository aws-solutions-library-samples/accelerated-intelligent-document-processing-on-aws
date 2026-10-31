# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Deep links: internal in-app routes only.

``validate_link`` rejects the same set the UI's ``isInternalRoute``
(``src/ui/src/routes/constants.ts``) rejects, plus anything ``urlsplit`` reads as
having a scheme or host, so the service and the UI agree on every edge case.
"""

import urllib.parse

from idp_common.notifications.exceptions import NotificationValidationError

MAX_LINK_LENGTH = 1024


def _is_forbidden_char(ch: str) -> bool:
    code = ord(ch)
    return code <= 0x20 or 0x7F <= code <= 0x9F or ch.isspace() or ch == "\\"


def validate_link(link: str) -> str:
    """Return ``link`` unchanged if it is an internal route, else raise.

    Rejected: longer than 1024; not starting with ``/`` or starting with
    ``//``; containing ``\\``, a control character (U+0000–U+0020,
    U+007F–U+009F) or any Unicode whitespace; containing ``://``
    (case-insensitive); or carrying a scheme or netloc under ``urlsplit``.
    """
    if not isinstance(link, str):
        raise NotificationValidationError("link must be a string")
    if len(link) > MAX_LINK_LENGTH:
        raise NotificationValidationError("link is too long")
    if not link.startswith("/") or link.startswith("//"):
        raise NotificationValidationError("link must be an internal route")
    if any(_is_forbidden_char(ch) for ch in link):
        raise NotificationValidationError("link contains a forbidden character")
    if "://" in link.lower():
        raise NotificationValidationError("link must be an internal route")
    parts = urllib.parse.urlsplit(link)
    if parts.scheme or parts.netloc:
        raise NotificationValidationError("link must be an internal route")
    return link


def encode_uri_component(value: str) -> str:
    """Byte-identical to JavaScript's ``encodeURIComponent``."""
    return urllib.parse.quote(value, safe="!~*'()")


def document_detail_link(object_key: str) -> str:
    """Route to a document's detail page.

    Byte-identical to the UI's ``encodeURIComponent`` encoding of the key.
    """
    return "/documents/" + encode_uri_component(object_key)


def document_edit_link(object_key: str) -> str:
    """Route to a document's edit page: its direct edit, or its review item in review.

    Byte-identical to the UI's ``documentEditPath``.
    """
    return document_detail_link(object_key) + "/edit"
