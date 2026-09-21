#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Integration test script for agent chat system.

This script tests the conversational agent system by directly invoking
Lambda functions and checking DynamoDB tables in a deployed AWS environment.

Usage:
    python test_agent_chat_integration.py --stack-name IDP --region us-east-1
"""

import argparse
import json
import logging
import sys
import time

import boto3

# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def get_stack_outputs(stack_name, region):
    """Get CloudFormation stack outputs."""
    cfn = boto3.client("cloudformation", region_name=region)

    try:
        response = cfn.describe_stacks(StackName=stack_name)
        stack = response["Stacks"][0]
        outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
        return outputs
    except Exception as e:
        logger.error(f"Error getting stack outputs: {e}")
        return {}


def get_function_name(stack_name, logical_id, region):
    """Get physical function name from logical ID."""
    cfn = boto3.client("cloudformation", region_name=region)

    try:
        response = cfn.describe_stack_resource(
            StackName=stack_name, LogicalResourceId=logical_id
        )
        return response["StackResourceDetail"]["PhysicalResourceId"]
    except Exception as e:
        logger.error(f"Error getting function name for {logical_id}: {e}")
        return None


def invoke_lambda(function_name, payload, region):
    """Invoke a Lambda function."""
    lambda_client = boto3.client("lambda", region_name=region)

    try:
        logger.info(f"Invoking {function_name}...")
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )

        result = json.loads(response["Payload"].read())
        logger.info(f"Response: {json.dumps(result, indent=2)}")

        # Check for errors in the response
        if "errorMessage" in result:
            logger.error(f"Lambda returned error: {result.get('errorMessage')}")
            if "errorType" in result:
                logger.error(f"Error type: {result.get('errorType')}")
            if "stackTrace" in result:
                logger.error(f"Stack trace: {result.get('stackTrace')}")

        return result
    except Exception as e:
        logger.error(f"Error invoking {function_name}: {e}")
        return None


def check_lambda_invocations(function_name, region, minutes=5):
    """Check if a Lambda function has been invoked recently."""
    cloudwatch = boto3.client("cloudwatch", region_name=region)

    try:
        end_time = time.time()
        start_time = end_time - (minutes * 60)

        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/Lambda",
            MetricName="Invocations",
            Dimensions=[{"Name": "FunctionName", "Value": function_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=60,
            Statistics=["Sum"],
        )

        datapoints = response.get("Datapoints", [])
        total_invocations = sum(dp.get("Sum", 0) for dp in datapoints)

        if total_invocations > 0:
            logger.info(
                f"✅ {function_name} was invoked {int(total_invocations)} time(s) in last {minutes} minutes"
            )
            return True
        else:
            logger.warning(
                f"⚠️  {function_name} has NOT been invoked in last {minutes} minutes"
            )
            return False

    except Exception as e:
        logger.error(f"Error checking Lambda invocations: {e}")
        return False


def check_dynamodb_table(table_name, pk, sk_prefix, region):
    """Check if items exist in DynamoDB table."""
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    try:
        logger.info(f"Checking {table_name} for PK={pk}...")
        response = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": pk},
        )

        items = response.get("Items", [])
        logger.info(f"Found {len(items)} items")

        for item in items:
            logger.info(f"  - SK: {item.get('SK')}, Role: {item.get('role', 'N/A')}")

        return items
    except Exception as e:
        logger.error(f"Error querying {table_name}: {e}")
        return []


def get_table_name(stack_name, logical_id, region):
    """Get physical table name from logical ID."""
    cfn = boto3.client("cloudformation", region_name=region)

    try:
        response = cfn.describe_stack_resource(
            StackName=stack_name, LogicalResourceId=logical_id
        )
        return response["StackResourceDetail"]["PhysicalResourceId"]
    except Exception as e:
        logger.error(f"Error getting table name for {logical_id}: {e}")
        return None


def check_cloudwatch_logs(function_name, session_id, region, minutes=5):
    """Check CloudWatch logs for subagent invocations."""
    logs_client = boto3.client("logs", region_name=region)
    log_group_name = f"/aws/lambda/{function_name}"

    try:
        logger.info(f"Checking CloudWatch logs for {function_name}...")

        # First check if log group exists
        try:
            logs_client.describe_log_groups(logGroupNamePrefix=log_group_name, limit=1)
        except logs_client.exceptions.ResourceNotFoundException:
            logger.warning(
                f"⚠️  Log group {log_group_name} doesn't exist yet - Lambda may not have been invoked"
            )
            return []

        # Calculate time range (last N minutes)
        end_time = int(time.time() * 1000)
        start_time = end_time - (minutes * 60 * 1000)

        # Search for logs containing the session ID
        response = logs_client.filter_log_events(
            logGroupName=log_group_name,
            startTime=start_time,
            endTime=end_time,
            filterPattern=session_id,
        )

        events = response.get("events", [])
        logger.info(f"Found {len(events)} log events for session {session_id}")

        # Look for subagent invocation patterns
        subagent_invocations = []
        for event in events:
            message = event.get("message", "")

            # Check for tool invocation patterns
            if any(
                keyword in message
                for keyword in [
                    "analytics",
                    "error_analyzer",
                    "error-analyzer",
                    "Invoking tool",
                    "Tool result",
                    "specialized_agent",
                ]
            ):
                subagent_invocations.append(message)
                logger.info(f"  - {message[:150]}...")

        if subagent_invocations:
            logger.info(
                f"✅ Found {len(subagent_invocations)} subagent invocation logs"
            )
        else:
            logger.warning("⚠️  No subagent invocation logs found")

        return subagent_invocations

    except Exception as e:
        logger.error(f"Error checking CloudWatch logs: {e}")
        return []


def test_agent_chat_flow(stack_name, region):
    """Test the complete agent chat flow."""
    logger.info("=" * 60)
    logger.info("Starting Agent Chat Integration Test")
    logger.info("=" * 60)

    # Get function names
    resolver_function = get_function_name(
        stack_name, "AgentChatResolverFunction", region
    )
    processor_function = get_function_name(
        stack_name, "AgentChatProcessorFunction", region
    )

    if not resolver_function or not processor_function:
        logger.error("Could not find Lambda functions")
        return False

    logger.info(f"Resolver: {resolver_function}")
    logger.info(f"Processor: {processor_function}")

    # Get table names
    chat_messages_table = get_table_name(stack_name, "ChatMessagesTable", region)
    memory_table = get_table_name(stack_name, "IdHelperChatMemoryTable", region)

    if not chat_messages_table or not memory_table:
        logger.error("Could not find DynamoDB tables")
        return False

    logger.info(f"ChatMessagesTable: {chat_messages_table}")
    logger.info(f"MemoryTable: {memory_table}")

    # Test session ID
    session_id = f"test-session-{int(time.time())}"
    logger.info(f"Using session ID: {session_id}")

    # Test 1: Invoke resolver with a message
    logger.info("\n" + "=" * 60)
    logger.info("Test 1: Send message via resolver")
    logger.info("=" * 60)

    resolver_payload = {
        "arguments": {
            "prompt": "Hello! What agents are available?",
            "sessionId": session_id,
            "method": "chat",
        },
        "identity": {"sub": "test-user-123"},
    }

    resolver_result = invoke_lambda(resolver_function, resolver_payload, region)

    if not resolver_result:
        logger.error("❌ Resolver invocation failed")
        return False

    if resolver_result.get("role") == "user":
        logger.info("✅ Resolver returned user message")
    else:
        logger.error(f"❌ Unexpected resolver response: {resolver_result}")
        return False

    # Test 2: Check message in ChatMessagesTable
    logger.info("\n" + "=" * 60)
    logger.info("Test 2: Verify message in ChatMessagesTable")
    logger.info("=" * 60)

    time.sleep(2)  # Wait for DynamoDB write

    chat_messages = check_dynamodb_table(chat_messages_table, session_id, "", region)

    if len(chat_messages) > 0:
        logger.info("✅ Message found in ChatMessagesTable")
    else:
        logger.warning("⚠️  No messages found in ChatMessagesTable yet")

    # Test 3: Wait for processor to complete
    logger.info("\n" + "=" * 60)
    logger.info("Test 3: Wait for processor to complete (60 seconds)")
    logger.info("=" * 60)

    logger.info("Waiting for agent response...")
    time.sleep(60)

    # Check for assistant response
    chat_messages = check_dynamodb_table(chat_messages_table, session_id, "", region)

    assistant_messages = [m for m in chat_messages if m.get("role") == "assistant"]

    if len(assistant_messages) > 0:
        logger.info("✅ Assistant response found!")
        logger.info(
            f"Response content: {assistant_messages[0].get('content', '')[:200]}..."
        )
    else:
        logger.warning("⚠️  No assistant response found yet")

    # Test 4: Check memory table
    logger.info("\n" + "=" * 60)
    logger.info("Test 4: Check conversation memory")
    logger.info("=" * 60)

    memory_items = check_dynamodb_table(
        memory_table, f"conversation#{session_id}", "", region
    )

    if len(memory_items) > 0:
        logger.info("✅ Conversation memory stored")
    else:
        logger.warning("⚠️  No memory items found")

    # Test 5: Test Analytics Agent (subagent invocation)
    logger.info("\n" + "=" * 60)
    logger.info("Test 5: Test Analytics Agent Invocation")
    logger.info("=" * 60)

    analytics_session_id = f"test-analytics-{int(time.time())}"
    logger.info(f"Using analytics session ID: {analytics_session_id}")

    analytics_payload = {
        "arguments": {
            "prompt": "What tables are available in the database?",
            "sessionId": analytics_session_id,
            "method": "chat",
        },
        "identity": {"sub": "test-user-123"},
    }

    resolver_result = invoke_lambda(resolver_function, analytics_payload, region)

    if resolver_result and resolver_result.get("role") == "user":
        logger.info("✅ Analytics query sent successfully")
    else:
        logger.error("❌ Analytics query failed")
        return False

    # Wait for analytics agent response (simple query but need time for cold start)
    logger.info("Waiting for Analytics Agent response (60 seconds)...")
    time.sleep(60)

    analytics_messages = check_dynamodb_table(
        chat_messages_table, analytics_session_id, "", region
    )
    analytics_assistant_messages = [
        m for m in analytics_messages if m.get("role") == "assistant"
    ]

    if len(analytics_assistant_messages) > 0:
        response_content = analytics_assistant_messages[0].get("content", "")
        logger.info("✅ Analytics Agent response found!")
        logger.info(f"Response content: {response_content[:300]}...")

        # Check if response indicates analytics agent was used
        analytics_keywords = [
            "table",
            "metering",
            "document_sections",
            "database",
            "analytics",
            "available",
        ]
        if any(keyword in response_content.lower() for keyword in analytics_keywords):
            logger.info("✅ Response appears to be from Analytics Agent")
        else:
            logger.warning(
                "⚠️  Response doesn't clearly indicate Analytics Agent was used"
            )
    else:
        logger.error("❌ No Analytics Agent response found")

    # Test 6: Test Error Analyzer Agent (subagent invocation)
    logger.info("\n" + "=" * 60)
    logger.info("Test 6: Test Error Analyzer Agent Invocation")
    logger.info("=" * 60)

    error_session_id = f"test-errors-{int(time.time())}"
    logger.info(f"Using error analyzer session ID: {error_session_id}")

    error_payload = {
        "arguments": {
            "prompt": "Are there any recent errors or failures in the system?",
            "sessionId": error_session_id,
            "method": "chat",
        },
        "identity": {"sub": "test-user-123"},
    }

    resolver_result = invoke_lambda(resolver_function, error_payload, region)

    if resolver_result and resolver_result.get("role") == "user":
        logger.info("✅ Error analysis query sent successfully")
    else:
        logger.error("❌ Error analysis query failed")
        return False

    # Wait for error analyzer response (simple query but need time for cold start)
    logger.info("Waiting for Error Analyzer Agent response (60 seconds)...")
    time.sleep(60)

    error_messages = check_dynamodb_table(
        chat_messages_table, error_session_id, "", region
    )
    error_assistant_messages = [
        m for m in error_messages if m.get("role") == "assistant"
    ]

    if len(error_assistant_messages) > 0:
        response_content = error_assistant_messages[0].get("content", "")
        logger.info("✅ Error Analyzer Agent response found!")
        logger.info(f"Response content: {response_content[:300]}...")

        # Check if response indicates error analyzer was used
        error_keywords = ["error", "log", "cloudwatch", "analyze", "help", "system"]
        if any(keyword in response_content.lower() for keyword in error_keywords):
            logger.info("✅ Response appears to be from Error Analyzer Agent")
        else:
            logger.warning(
                "⚠️  Response doesn't clearly indicate Error Analyzer Agent was used"
            )
    else:
        logger.error("❌ No Error Analyzer Agent response found")

    # Test 7: Multi-turn conversation with context
    logger.info("\n" + "=" * 60)
    logger.info("Test 7: Test multi-turn conversation with context")
    logger.info("=" * 60)

    resolver_payload2 = {
        "arguments": {
            "prompt": "Can you provide more details about that?",
            "sessionId": session_id,
            "method": "chat",
        },
        "identity": {"sub": "test-user-123"},
    }

    resolver_result2 = invoke_lambda(resolver_function, resolver_payload2, region)

    if resolver_result2 and resolver_result2.get("role") == "user":
        logger.info("✅ Follow-up message sent successfully")
    else:
        logger.error("❌ Follow-up message failed")
        return False

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("Test Summary")
    logger.info("=" * 60)
    logger.info(f"General session ID: {session_id}")
    logger.info(f"Analytics session ID: {analytics_session_id}")
    logger.info(f"Error analyzer session ID: {error_session_id}")
    logger.info(f"Messages in ChatMessagesTable: {len(chat_messages)}")
    logger.info(f"Assistant responses: {len(assistant_messages)}")
    logger.info(f"Analytics responses: {len(analytics_assistant_messages)}")
    logger.info(f"Error analyzer responses: {len(error_assistant_messages)}")
    logger.info(f"Memory items: {len(memory_items)}")

    logger.info("\n✅ Integration test completed!")
    logger.info("\nNext steps:")
    logger.info("1. Check CloudWatch Logs for AgentChatProcessorFunction")
    logger.info(
        f"   - Search for session IDs: {session_id}, {analytics_session_id}, {error_session_id}"
    )
    logger.info("2. Verify streaming worked correctly")
    logger.info("3. Look for tool invocation logs (subagent calls)")
    logger.info("4. Check for any errors in the logs")

    # Return success if we got at least one subagent response
    success = (
        len(assistant_messages) > 0
        and len(analytics_assistant_messages) > 0
        and len(error_assistant_messages) > 0
    )

    if success:
        logger.info("\n✅ All tests passed - subagents are responding!")
    else:
        logger.warning("\n⚠️  Some tests failed - check CloudWatch logs for details")

    return success


def test_subagent_responses_only(stack_name, region):
    """
    Focused test to verify subagent responses are working.

    This test specifically checks if subagents (Analytics, Error Analyzer)
    are being invoked and returning responses.
    """
    logger.info("=" * 60)
    logger.info("Testing Subagent Responses")
    logger.info("=" * 60)

    # Get function names
    resolver_function = get_function_name(
        stack_name, "AgentChatResolverFunction", region
    )
    processor_function = get_function_name(
        stack_name, "AgentChatProcessorFunction", region
    )

    if not resolver_function or not processor_function:
        logger.error("Could not find Lambda functions")
        return False

    # Get table name
    chat_messages_table = get_table_name(stack_name, "ChatMessagesTable", region)

    if not chat_messages_table:
        logger.error("Could not find ChatMessagesTable")
        return False

    test_results = {}

    # # Test 1: Analytics Agent
    # logger.info("\n" + "=" * 60)
    # logger.info("Test 1: Analytics Agent Response")
    # logger.info("=" * 60)

    # analytics_session_id = f"test-analytics-debug-{int(time.time())}"
    # logger.info(f"Session ID: {analytics_session_id}")

    # analytics_payload = {
    #     "arguments": {
    #         "prompt": "What database tables are available?",
    #         "sessionId": analytics_session_id,
    #         "method": "chat",
    #     },
    #     "identity": {"sub": "test-user-123"},
    # }

    # logger.info("Sending analytics query...")
    # resolver_result = invoke_lambda(resolver_function, analytics_payload, region)

    # if not resolver_result or resolver_result.get("role") != "user":
    #     logger.error("❌ Failed to send analytics query")
    #     test_results["analytics"] = False
    # else:
    #     logger.info("✅ Analytics query sent, waiting for response (60 seconds)...")
    #     time.sleep(60)

    #     # Check for response
    #     messages = check_dynamodb_table(
    #         chat_messages_table, analytics_session_id, "", region
    #     )
    #     assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    #     if assistant_messages:
    #         response = assistant_messages[0].get("content", "")
    #         logger.info("✅ Assistant response found!")
    #         logger.info(f"Response length: {len(response)} characters")
    #         logger.info(f"Response preview: {response[:400]}...")
    #         logger.info("\n⚠️  MANUAL VERIFICATION REQUIRED:")
    #         logger.info(f"   Check CloudWatch logs for session: {analytics_session_id}")
    #         logger.info("   Look for: 'Invoking tool' or 'analytics_agent_v1'")
    #         logger.info(f"   Log group: /aws/lambda/{processor_function}")
    #         test_results["analytics"] = True
    #     else:
    #         logger.error("❌ No assistant response found")

    #         # Check if processor was invoked
    #         logger.info("\nChecking if processor Lambda was invoked...")
    #         was_invoked = check_lambda_invocations(
    #             processor_function, region, minutes=2
    #         )

    #         if not was_invoked:
    #             logger.error(
    #                 "❌ Processor Lambda was NOT invoked - check resolver Lambda logs"
    #             )
    #         else:
    #             logger.warning(
    #                 "⚠️  Processor was invoked but no response yet - may need more time"
    #             )

    #         test_results["analytics"] = False

    # # Test 2: Error Analyzer Agent
    # logger.info("\n" + "=" * 60)
    # logger.info("Test 2: Error Analyzer Agent Response")
    # logger.info("=" * 60)

    # error_session_id = f"test-errors-debug-{int(time.time())}"
    # logger.info(f"Session ID: {error_session_id}")

    # error_payload = {
    #     "arguments": {
    #         "prompt": "What can you help me analyze?",
    #         "sessionId": error_session_id,
    #         "method": "chat",
    #     },
    #     "identity": {"sub": "test-user-123"},
    # }

    # logger.info("Sending error analysis query...")
    # resolver_result = invoke_lambda(resolver_function, error_payload, region)

    # if not resolver_result or resolver_result.get("role") != "user":
    #     logger.error("❌ Failed to send error analysis query")
    #     test_results["error_analyzer"] = False
    # else:
    #     logger.info(
    #         "✅ Error analysis query sent, waiting for response (60 seconds)..."
    #     )
    #     time.sleep(60)

    #     # Check for response
    #     messages = check_dynamodb_table(
    #         chat_messages_table, error_session_id, "", region
    #     )
    #     assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    #     if assistant_messages:
    #         response = assistant_messages[0].get("content", "")
    #         logger.info("✅ Error Analyzer Agent responded!")
    #         logger.info(f"Response length: {len(response)} characters")
    #         logger.info(f"Response preview: {response[:400]}...")
    #         test_results["error_analyzer"] = True

    #         # Check CloudWatch logs
    #         logger.info("\nChecking CloudWatch logs for tool invocations...")
    #         check_cloudwatch_logs(
    #             processor_function, error_session_id, region, minutes=3
    #         )
    #     else:
    #         logger.error("❌ No Error Analyzer Agent response found")
    #         logger.info("Checking CloudWatch logs for errors...")
    #         check_cloudwatch_logs(
    #             processor_function, error_session_id, region, minutes=3
    #         )
    #         test_results["error_analyzer"] = False

    # Test 3: Code Intelligence Agent
    logger.info("\n" + "=" * 60)
    logger.info("Test 3: Code Intelligence Agent Response")
    logger.info("=" * 60)

    code_intel_session_id = f"test-code-intel-debug-{int(time.time())}"
    logger.info(f"Session ID: {code_intel_session_id}")

    code_intel_payload = {
        "arguments": {
            "prompt": "Using the Code Intelligence Agent, what is this repository about?",
            "sessionId": code_intel_session_id,
            "method": "chat",
        },
        "identity": {"sub": "test-user-123"},
    }

    logger.info("Sending code intelligence query...")
    resolver_result = invoke_lambda(resolver_function, code_intel_payload, region)

    if not resolver_result or resolver_result.get("role") != "user":
        logger.error("❌ Failed to send code intelligence query")
        test_results["code_intelligence"] = False
    else:
        logger.info(
            "✅ Code intelligence query sent, waiting for response (60 seconds)..."
        )
        time.sleep(60)

        # Check for response
        messages = check_dynamodb_table(
            chat_messages_table, code_intel_session_id, "", region
        )
        assistant_messages = [m for m in messages if m.get("role") == "assistant"]

        if assistant_messages:
            response = assistant_messages[0].get("content", "")
            logger.info("✅ Code Intelligence Agent responded!")
            logger.info(f"Response length: {len(response)} characters")
            logger.info(f"Response preview: {response[:400]}...")

            # Check if response contains repository-related keywords
            repo_keywords = [
                "document",
                "processing",
                "idp",
                "intelligent",
                "repository",
                "aws",
            ]
            found_keywords = [kw for kw in repo_keywords if kw in response.lower()]
            if found_keywords:
                logger.info(
                    f"✅ Response contains repository keywords: {found_keywords}"
                )
            else:
                logger.warning(
                    "⚠️  Response doesn't contain expected repository keywords"
                )

            test_results["code_intelligence"] = True

            # Check CloudWatch logs for DeepWiki tool calls
            logger.info("\nChecking CloudWatch logs for DeepWiki tool invocations...")
            logs = check_cloudwatch_logs(
                processor_function, code_intel_session_id, region, minutes=3
            )

            # Look for repoName parameter in logs
            repo_name_found = any(
                "aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws"
                in log
                for log in logs
            )
            if repo_name_found:
                logger.info("✅ Found repoName parameter in tool calls")
            else:
                logger.warning(
                    "⚠️  Could not verify repoName parameter in logs (may need manual check)"
                )
        else:
            logger.error("❌ No Code Intelligence Agent response found")
            logger.info("Checking CloudWatch logs for errors...")
            check_cloudwatch_logs(
                processor_function, code_intel_session_id, region, minutes=3
            )
            test_results["code_intelligence"] = False

    # # Test 4: General orchestrator response (no subagent)
    # logger.info("\n" + "=" * 60)
    # logger.info("Test 4: Orchestrator Direct Response (No Subagent)")
    # logger.info("=" * 60)

    # general_session_id = f"test-general-debug-{int(time.time())}"
    # logger.info(f"Session ID: {general_session_id}")

    # general_payload = {
    #     "arguments": {
    #         "prompt": "What agents are available to help me?",
    #         "sessionId": general_session_id,
    #         "method": "chat",
    #     },
    #     "identity": {"sub": "test-user-123"},
    # }

    # logger.info("Sending general query...")
    # resolver_result = invoke_lambda(resolver_function, general_payload, region)

    # if not resolver_result or resolver_result.get("role") != "user":
    #     logger.error("❌ Failed to send general query")
    #     test_results["orchestrator"] = False
    # else:
    #     logger.info("✅ General query sent, waiting for response (60 seconds)...")
    #     time.sleep(60)

    #     # Check for response
    #     messages = check_dynamodb_table(
    #         chat_messages_table, general_session_id, "", region
    #     )
    #     assistant_messages = [m for m in messages if m.get("role") == "assistant"]

    #     if assistant_messages:
    #         response = assistant_messages[0].get("content", "")
    #         logger.info("✅ Orchestrator responded!")
    #         logger.info(f"Response length: {len(response)} characters")
    #         logger.info(f"Response preview: {response[:400]}...")
    #         test_results["orchestrator"] = True
    #     else:
    #         logger.error("❌ No orchestrator response found")
    #         logger.info("Checking CloudWatch logs for errors...")
    #         check_cloudwatch_logs(
    #             processor_function, general_session_id, region, minutes=2
    #         )
    #         test_results["orchestrator"] = False

    # Summary
    logger.info("\n" + "=" * 60)
    logger.info("Subagent Test Summary")
    logger.info("=" * 60)

    for test_name, passed in test_results.items():
        status = "✅ PASSED" if passed else "❌ FAILED"
        logger.info(f"{test_name}: {status}")

    all_passed = all(test_results.values())

    if all_passed:
        logger.info("\n✅ All subagent tests passed!")
    else:
        logger.warning("\n⚠️  Some subagent tests failed")
        logger.info("\nDebugging tips:")
        logger.info("1. Check CloudWatch Logs for AgentChatProcessorFunction")
        logger.info("2. Look for 'Creating orchestrator with' log messages")
        logger.info("3. Search for 'Invoking tool' or tool names in logs")
        logger.info("4. Check for any exceptions or errors")
        logger.info("5. Verify environment variables are set correctly")

    return all_passed


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Test agent chat integration")
    parser.add_argument("--stack-name", default="IDP", help="CloudFormation stack name")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument(
        "--test-type",
        choices=["full", "subagents"],
        default="full",
        help="Type of test to run (full or subagents only)",
    )

    args = parser.parse_args()

    try:
        if args.test_type == "subagents":
            success = test_subagent_responses_only(args.stack_name, args.region)
        else:
            success = test_agent_chat_flow(args.stack_name, args.region)

        sys.exit(0 if success else 1)
    except Exception as e:
        logger.error(f"Test failed with error: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
