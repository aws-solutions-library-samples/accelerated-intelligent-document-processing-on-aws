# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Inbox resolver as a dispatcher: caller key, argument checks, error classes.

The service behaviour (paging, marking, preferences) is covered end to end over a
moto table by ``lib/idp_common_pkg/tests/unit/lambdas/test_notifications_resolver.py``.
This suite pins the handler's own contract with a stub service: the caller key
comes from verified claims only, each field reaches the right service call with
validated arguments, and errors leave as exactly ``ValueError``,
``PermissionError`` or ``ResourceNotFound`` (the dispatcher maps by class name).
"""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from idp_common.api_adapter import ResourceNotFound
from idp_common.notifications.exceptions import (
    NotificationNotFoundError,
    NotificationValidationError,
)
from idp_common.notifications.keys import new_notification_id

_INDEX = Path(__file__).with_name("index.py")
EMAIL = "Alice.Example@Example.COM"
CALLER_KEY = "alice.example@example.com"


def _load():
    name = "notifications_resolver_index_under_test"
    with patch.dict(os.environ, {"NOTIFICATIONS_TABLE_NAME": ""}):
        spec = importlib.util.spec_from_file_location(name, _INDEX)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return module


def _event(field, args=None, *, claims=None):
    if claims is None:
        claims = {"email": EMAIL, "sub": "sub-1", "cognito:groups": ["Viewer"]}
    return {
        "info": {"fieldName": field},
        "arguments": {} if args is None else args,
        "identity": {"claims": claims},
    }


def _model(payload):
    m = MagicMock()
    m.model_dump.return_value = payload
    return m


@pytest.fixture
def module():
    m = _load()
    assert m._SERVICE is None  # no table at import
    m._SERVICE = MagicMock(name="NotificationService")
    return m


@pytest.mark.unit
class TestRouting:
    def test_unsupported_field_is_a_bad_request(self, module):
        with pytest.raises(ValueError, match="Unsupported field"):
            module.handler(_event("dropTable"), None)

    def test_unconfigured_store_is_a_server_fault(self, module):
        module._SERVICE = None
        with pytest.raises(RuntimeError):
            module.handler(_event("getInboxUnreadCount"), None)

    def test_list_passes_validated_arguments_and_the_caller_key(self, module):
        module._SERVICE.list_messages.return_value = _model({"items": []})
        result = module.handler(
            _event("listInboxMessages", {"read": False, "limit": 5}), None
        )
        assert result == {"items": []}
        module._SERVICE.list_messages.assert_called_once_with(
            CALLER_KEY,
            read=False,
            types=None,
            severities=None,
            limit=5,
            next_token=None,
        )

    def test_mark_reads_ids_and_flag(self, module):
        message_id = new_notification_id()
        module._SERVICE.mark.return_value = _model({"updated": 1})
        result = module.handler(
            _event("markInboxMessages", {"ids": [message_id], "read": True}), None
        )
        assert result == {"updated": 1}
        module._SERVICE.mark.assert_called_once_with(
            CALLER_KEY, [message_id], read=True
        )

    def test_unread_count_is_returned_as_is(self, module):
        module._SERVICE.unread_count.return_value = 7
        assert module.handler(_event("getInboxUnreadCount"), None) == 7
        module._SERVICE.unread_count.assert_called_once_with(CALLER_KEY)


@pytest.mark.unit
class TestCaller:
    def test_a_caller_with_no_group_is_refused(self, module):
        with pytest.raises(PermissionError, match="no group"):
            module.handler(_event("getInboxUnreadCount", claims={"email": EMAIL}), None)
        module._SERVICE.unread_count.assert_not_called()

    def test_a_caller_with_no_identity_is_refused(self, module):
        with pytest.raises(PermissionError, match="no caller identity"):
            module.handler(
                _event("getInboxUnreadCount", claims={"cognito:groups": ["Viewer"]}),
                None,
            )
        module._SERVICE.unread_count.assert_not_called()

    def test_a_user_argument_is_not_accepted(self, module):
        with pytest.raises(ValueError):
            module.handler(
                _event("listInboxMessages", {"recipient": "bob@example.com"}), None
            )
        module._SERVICE.list_messages.assert_not_called()


@pytest.mark.unit
class TestErrors:
    def test_arguments_to_a_no_argument_operation_are_refused(self, module):
        with pytest.raises(ValueError, match="takes no arguments: x"):
            module.handler(_event("markAllInboxMessagesRead", {"x": 1}), None)
        module._SERVICE.mark_all_read.assert_not_called()

    def test_validation_error_becomes_exactly_value_error(self, module):
        with pytest.raises(ValueError) as exc:
            module.handler(_event("deleteInboxMessages", {"ids": []}), None)
        assert type(exc.value) is ValueError
        assert "ids" in str(exc.value)

    def test_service_validation_error_becomes_exactly_value_error(self, module):
        module._SERVICE.clear_read.side_effect = NotificationValidationError("bad")
        with pytest.raises(ValueError) as exc:
            module.handler(_event("clearReadInboxMessages"), None)
        assert type(exc.value) is ValueError

    def test_not_found_becomes_resource_not_found(self, module):
        module._SERVICE.get_preferences.side_effect = NotificationNotFoundError("gone")
        with pytest.raises(ResourceNotFound):
            module.handler(_event("getInboxPreferences"), None)
