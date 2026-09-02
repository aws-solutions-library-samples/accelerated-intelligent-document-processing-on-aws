# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import html
import json
import logging
import os
import re
import urllib.parse

import boto3
from idp_common.utils.log_sanitizer import sanitize_event_for_logging
from idp_common.utils.settings_helper import get_setting

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.getLevelName(os.environ.get("LOG_LEVEL", "INFO")))
# Get LOG_LEVEL from environment variable with INFO as default

print("Boto3 version: ", boto3.__version__)

# Try environment variable first (backward compatibility), then Settings
KB_ID = os.environ.get("KB_ID")
if not KB_ID:
    KB_ID = get_setting("KnowledgeBaseId", default="")

KB_ACCOUNT_ID = os.environ.get("KB_ACCOUNT_ID")
KB_REGION = os.environ.get("KB_REGION") or os.environ["AWS_REGION"]
MODEL_ID = os.environ.get("MODEL_ID")
# Partition-aware: a hardcoded `arn:aws:` can never resolve outside the
# commercial partition (AWS account ids do not span partitions), so every
# Knowledge Base query in GovCloud/China would fail on an invalid model ARN.
# AWS_PARTITION is injected by the template (!Ref AWS::Partition); default to
# the commercial partition so a stale deployment without the env var behaves as
# it did before rather than breaking.
AWS_PARTITION = os.environ.get("AWS_PARTITION") or "aws"
MODEL_ARN = (
    f"arn:{AWS_PARTITION}:bedrock:{KB_REGION}:{KB_ACCOUNT_ID}"
    f":inference-profile/{MODEL_ID}"
)
GUARDRAIL_ENV = os.environ.get("GUARDRAIL_ID_AND_VERSION", "")

KB_CLIENT = (
    boto3.client(service_name="bedrock-agent-runtime", region_name=KB_REGION)
    if KB_ID
    else None
)


def get_kb_response(query, sessionId):
    # Check if Knowledge Base is configured
    if not KB_ID:
        return {"systemMessage": "Knowledge Base is not configured for this deployment"}

    input = {
        "input": {"text": query},
        "retrieveAndGenerateConfiguration": {
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": KB_ID,
                "modelArn": MODEL_ARN,
            },
            "type": "KNOWLEDGE_BASE",
        },
    }

    # Apply Bedrock Guardrail if configured
    if GUARDRAIL_ENV:
        try:
            guardrail_id, guardrail_version = GUARDRAIL_ENV.split(":")
            if guardrail_id and guardrail_version:
                if (
                    "generationConfiguration"
                    not in input["retrieveAndGenerateConfiguration"][
                        "knowledgeBaseConfiguration"
                    ]
                ):
                    input["retrieveAndGenerateConfiguration"][
                        "knowledgeBaseConfiguration"
                    ]["generationConfiguration"] = {}

                input["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"][
                    "generationConfiguration"
                ]["guardrailConfiguration"] = {
                    "guardrailId": guardrail_id,
                    "guardrailVersion": guardrail_version,
                }
                logger.debug(
                    f"Using Bedrock Guardrail ID: {guardrail_id}, Version: {guardrail_version}"
                )
        except ValueError:
            logger.warning(
                f"Invalid GUARDRAIL_ID_AND_VERSION format: {GUARDRAIL_ENV}. Expected format: 'id:version'"
            )

    if sessionId:
        input["sessionId"] = sessionId

    logger.info("Amazon Bedrock KB Request: %s", json.dumps(input))
    try:
        resp = KB_CLIENT.retrieve_and_generate(**input)
    except Exception as e:
        logger.error("Amazon Bedrock KB Exception: %s", e)
        resp = {"systemMessage": "Amazon Bedrock KB Error: " + str(e)}
    logger.debug("Amazon Bedrock KB Response: %s", json.dumps(resp))
    return resp


def extract_document_id(s3_uri):
    # Strip out the s3://bucketname/ prefix
    without_bucket = re.sub(r"^s3://[^/]+/", "", s3_uri)
    # Remove everything from /sections or /pages to the end
    document_id = re.sub(r"/(sections|pages)/.*$", "", without_bucket)
    return document_id


def markdown_response(kb_response):
    """Build a markdown response that embeds KB citation snippets.

    Security note: the assembled markdown is rendered by ReactMarkdown with
    rehype-raw on the frontend, so any HTML-meaningful characters in the
    snippet, document title, or URL must be escaped at the source to prevent
    stored XSS. The custom `<documentid>` tag is intentional and is mapped to
    a safe CustomLink component on the UI side (see
    src/ui/src/components/document-kb-query-layout/DocumentsQueryLayout.tsx).
    `snippet` and `documentId` are not attacker-controlled in a single-tenant
    Cognito deployment, but they can be derived from user-uploaded document
    content (OCR text, file names, etc.) and must be treated as untrusted
    for rendering purposes.
    """

    showContextText = True
    message = (
        kb_response.get("output", {}).get("text", {})
        or kb_response.get("systemMessage")
        or "No answer found"
    )
    markdown = message
    if showContextText:
        contextText = ""
        sourceLinks = []
        for source in kb_response.get("citations", []):
            for reference in source.get("retrievedReferences", []):
                snippet_raw = reference.get("content", {}).get(
                    "text", "no reference text"
                )
                if "location" in reference and "s3Location" in reference["location"]:
                    s3_uri = reference["location"]["s3Location"]["uri"]
                    documentId = extract_document_id(s3_uri)
                    # URL encode the documentId to make it URL-safe (no quote-
                    # injection into the href); then HTML-escape the result
                    # for safe interpolation inside the `href="..."` attribute.
                    url_safe_documentId = urllib.parse.quote(documentId, safe="")
                    url = html.escape(url_safe_documentId, quote=True)
                    # The visible title and snippet are rendered as HTML by
                    # the frontend, so escape &, <, >, ', and " to prevent
                    # stored-XSS via document content that contains markup.
                    title = html.escape(documentId, quote=True)
                    snippet = html.escape(snippet_raw, quote=False)
                    contextText = f'{contextText}<br><documentid href="{url}">{title}</documentid><br>{snippet}\n'
                    sourceLinks.append(f'<documentid href="{url}">{title}</documentid>')
        if contextText:
            markdown = f'{markdown}\n<details><summary>Context</summary><p style="white-space: pre-line;">{contextText}</p></details>'
        if len(sourceLinks):
            # Remove duplicate sources before joining
            unique_sourceLinks = list(dict.fromkeys(sourceLinks))
            markdown = f"{markdown}<br>Sources: " + ", ".join(unique_sourceLinks)
    return markdown


def handler(event, context):
    # Log a redacted copy — the raw event contains identity.claims and the
    # user's natural-language query, which we truncate rather than log in full.
    logger.info(
        "Received event: %s",
        json.dumps(sanitize_event_for_logging(event, extra_truncate_keys=["input"])),
    )
    query = event["arguments"]["input"]
    sessionId = event["arguments"].get("sessionId") or None
    kb_response = get_kb_response(query, sessionId)
    kb_response["markdown"] = markdown_response(kb_response)
    # KB response contains LLM output + document snippets. Truncate for logs
    # to limit log volume and avoid writing large PII-bearing documents
    # verbatim to CloudWatch.
    logger.debug(
        "Returning response: %s",
        json.dumps(
            sanitize_event_for_logging(
                kb_response,
                extra_truncate_keys=["markdown", "output", "citations"],
            )
        ),
    )
    return json.dumps(kb_response)
