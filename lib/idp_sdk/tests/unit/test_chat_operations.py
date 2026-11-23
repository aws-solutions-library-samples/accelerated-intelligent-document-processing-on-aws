# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for Chat operations (mocked).
"""

from unittest.mock import MagicMock, patch

import pytest

from idp_sdk import IDPClient
from idp_sdk.models.chat import ChatResponse
from idp_sdk.operations.chat import ChatOperation


@pytest.mark.unit
class TestChatModels:
    """Test chat model dataclasses."""

    def test_chat_response_defaults(self):
        resp = ChatResponse(response="hello", session_id="s1")
        assert resp.response == "hello"
        assert resp.session_id == "s1"
        assert resp.agent_names == []

    def test_chat_response_with_agents(self):
        resp = ChatResponse(
            response="result",
            session_id="s2",
            agent_names=["Analytics Agent"],
        )
        assert resp.agent_names == ["Analytics Agent"]


@pytest.mark.unit
class TestChatOperationInit:
    """Test ChatOperation initialization."""

    def test_client_has_chat_operation(self):
        client = IDPClient(stack_name="test-stack")
        assert hasattr(client, "chat")
        assert isinstance(client.chat, ChatOperation)

    def test_chat_requires_stack(self):
        from idp_sdk.exceptions import IDPConfigurationError

        client = IDPClient()
        with pytest.raises(IDPConfigurationError):
            client.chat._get_processor()

    def test_chat_lazy_processor(self):
        client = IDPClient(stack_name="test-stack")
        assert client.chat._processor is None


@pytest.mark.unit
class TestChatProcessorSetupEnv:
    """Test ChatProcessor environment setup."""

    @patch("idp_sdk._core.chat_processor.StackInfo")
    def test_setup_env_sets_variables(self, mock_stack_info_cls):
        from idp_sdk._core.chat_processor import ChatProcessor

        # Mock StackInfo
        mock_info = MagicMock()
        mock_info.get_resources.return_value = {
            "LookupFunctionName": "IDP-LookupFn",
            "SettingsParameter": "IDP-Settings",
        }
        mock_info._get_stack_outputs.return_value = {
            "S3ReportingBucketName": "reporting-bucket",
            "ReportingDatabase": "idp_reporting_db",
        }
        mock_info.cfn.get_paginator.return_value.paginate.return_value = [
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "ConfigurationTable",
                        "PhysicalResourceId": "IDP-ConfigTable-ABC",
                    },
                    {
                        "LogicalResourceId": "TrackingTable",
                        "PhysicalResourceId": "IDP-TrackingTable-DEF",
                    },
                    {
                        "LogicalResourceId": "IdHelperChatMemoryTable",
                        "PhysicalResourceId": "IDP-MemoryTable-GHI",
                    },
                ]
            }
        ]
        mock_stack_info_cls.return_value = mock_info

        import os

        # Use patch.dict so env vars set by _setup_env() don't leak to other tests
        with patch.dict(os.environ, os.environ.copy(), clear=True):
            processor = ChatProcessor(stack_name="IDP", region="us-east-1")
            processor._setup_env()

            assert os.environ.get("AWS_STACK_NAME") == "IDP"
            assert os.environ.get("ATHENA_DATABASE") == "idp_reporting_db"
            assert os.environ.get("CONFIGURATION_TABLE_NAME") == "IDP-ConfigTable-ABC"
            assert os.environ.get("TRACKING_TABLE_NAME") == "IDP-TrackingTable-DEF"
            assert (
                os.environ.get("ATHENA_OUTPUT_LOCATION")
                == "s3://reporting-bucket/athena-results/"
            )
            assert processor._env_ready is True

    @patch("idp_sdk._core.chat_processor.StackInfo")
    def test_with_no_region_the_agents_get_the_region_boto3_resolves(
        self, mock_stack_info_cls, monkeypatch
    ):
        """The Region the stack lookup's client used, not a fresh session's.

        A fresh `boto3.Session()` ignores a Region given to
        `boto3.setup_default_session`, which the stack lookup honours, so the
        agents would call another Region's tables and models.
        """
        import os

        import boto3

        from idp_sdk._core.chat_processor import ChatProcessor

        mock_info = MagicMock()
        mock_info.get_resources.return_value = {}
        mock_info._get_stack_outputs.return_value = {}
        mock_info.cfn.get_paginator.return_value.paginate.return_value = []
        mock_stack_info_cls.return_value = mock_info
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
        boto3.setup_default_session(region_name="eu-central-1")

        with patch.dict(os.environ, os.environ.copy(), clear=True):
            ChatProcessor(stack_name="IDP")._setup_env()

            assert os.environ.get("AWS_REGION") == "eu-central-1"
            assert os.environ.get("BEDROCK_REGION") == "eu-central-1"

    @patch("idp_sdk._core.chat_processor.StackInfo")
    def test_setup_env_cached(self, mock_stack_info_cls):
        from idp_sdk._core.chat_processor import ChatProcessor

        processor = ChatProcessor(stack_name="IDP")
        processor._env_ready = True

        processor._setup_env()

        # StackInfo should not be called if already set up
        mock_stack_info_cls.assert_not_called()


@pytest.mark.unit
class TestChatOperationSendMessage:
    """Test ChatOperation.send_message with mocked processor."""

    @patch("idp_sdk._core.chat_processor.ChatProcessor")
    def test_send_message(self, mock_processor_cls):
        mock_processor = MagicMock()
        mock_processor.send_message.return_value = ChatResponse(
            response="42 documents",
            session_id="sdk-abc123",
        )
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        # Inject the mock
        client.chat._processor = mock_processor

        result = client.chat.send_message("how many documents?")

        assert isinstance(result, ChatResponse)
        assert result.response == "42 documents"
        assert result.session_id == "sdk-abc123"
        mock_processor.send_message.assert_called_once_with(
            prompt="how many documents?", session_id=None
        )

    @patch("idp_sdk._core.chat_processor.ChatProcessor")
    def test_send_message_with_session(self, mock_processor_cls):
        mock_processor = MagicMock()
        mock_processor.send_message.return_value = ChatResponse(
            response="by type: W2=20, 1099=22",
            session_id="sdk-abc123",
        )
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        client.chat._processor = mock_processor

        result = client.chat.send_message("break down by type", session_id="sdk-abc123")

        assert result.response == "by type: W2=20, 1099=22"
        mock_processor.send_message.assert_called_once_with(
            prompt="break down by type", session_id="sdk-abc123"
        )

    @patch("idp_sdk._core.chat_processor.ChatProcessor")
    def test_send_message_error_wraps_exception(self, mock_processor_cls):
        from idp_sdk.exceptions import IDPProcessingError

        mock_processor = MagicMock()
        mock_processor.send_message.side_effect = RuntimeError("bedrock down")
        mock_processor_cls.return_value = mock_processor

        client = IDPClient(stack_name="test-stack")
        client.chat._processor = mock_processor

        with pytest.raises(IDPProcessingError, match="Chat request failed"):
            client.chat.send_message("test")
