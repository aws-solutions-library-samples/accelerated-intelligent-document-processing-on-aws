# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import html
import json
import logging
import os
import re
import time
import urllib.parse

import boto3
from botocore.exceptions import ClientError
from idp_common.chat_session_owner import claim_chat_session, recorded_owner
from idp_common.config_scope import unscoped_is_denied
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

# Who each Bedrock knowledge-base session belongs to. Recorded in the registry the
# chat paths share (ChatSessionOwnersTable, keyed sessionId), under a key of its own
# so a knowledge-base session id and a chat session id never share a row.
OWNERS_TABLE = os.environ.get("CHAT_SESSION_OWNERS_TABLE", "")
DATA_RETENTION_DAYS = int(os.environ.get("DATA_RETENTION_DAYS", "1"))
KB_SESSION_KEY_PREFIX = "knowledge-base#"

_dynamodb = boto3.resource("dynamodb")


class SessionNotOwned(PermissionError):
    """The request names a knowledge-base session the caller does not own."""


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

    # Ids and sizes only: the question is the user's own text.
    logger.info(
        "Amazon Bedrock KB request: knowledgeBaseId=%s queryChars=%d "
        "continuesSession=%s guardrail=%s",
        KB_ID,
        len(query or ""),
        bool(sessionId),
        bool(GUARDRAIL_ENV),
    )
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


def _refuse_non_admin_when_unscoped_is_denied(event):
    """Tenant isolation: the Knowledge Base indexes every tenant's documents.

    A retrieval over it returns snippets from whichever documents match, with no
    configuration-profile filter, so it cannot be scoped to one tenant. Under
    ``TenantIsolationMode=Strict``, or an external ``TenantGrantSource``
    (``IdpClaim`` or ``Api``), it is therefore limited to the Admin group. An
    invocation with no ``identity`` (a direct, IAM-gated invoke) is not a tenant
    user and is left alone, matching the other resolvers' backend-path rule.
    """
    if not unscoped_is_denied():
        return
    identity = event.get("identity")
    if identity is None:
        return
    groups = (identity.get("claims") or {}).get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if "Admin" not in groups:
        logger.warning(
            "Rejecting queryKnowledgeBase: tenant isolation restricts the "
            "Knowledge Base to Admin"
        )
        raise PermissionError(
            "Unauthorized: this deployment runs with tenant isolation, where "
            "the Document Knowledge Base (which indexes every tenant's documents) "
            "is limited to the Admin group"
        )


def _verified_sub(event):
    """The caller's Cognito ``sub``, from the verified identity, or ``""``."""
    identity = event.get("identity") or {}
    if not isinstance(identity, dict):
        return ""
    claims = identity.get("claims") or {}
    return str(identity.get("sub") or claims.get("sub") or "").strip()


def _owner_key(session_id):
    return f"{KB_SESSION_KEY_PREFIX}{session_id}"


def _expires_after():
    return int(time.time()) + DATA_RETENTION_DAYS * 24 * 60 * 60


def _session_to_continue(event, session_id):
    """The Bedrock session id this request may continue, or ``None`` for a new one.

    A session id is recorded against the caller who started it (see
    :func:`_record_session`). An id recorded against another caller is refused. An
    id with no record, such as one started before sessions were recorded or one
    whose record has expired, is not continued: the question starts a new session.
    An invocation with no ``identity`` is the IAM-gated backend path and is not
    checked. Fails closed: an unconfigured table, a caller with no ``sub`` or a
    DynamoDB error refuses the request.
    """
    if not session_id or event.get("identity") is None:
        return session_id
    caller_sub = _verified_sub(event)
    if not (OWNERS_TABLE and caller_sub):
        logger.error("Cannot verify knowledge base session ownership; refusing")
        raise SessionNotOwned(
            "Unauthorized: knowledge base session ownership cannot be verified"
        )
    table = _dynamodb.Table(OWNERS_TABLE)
    key = _owner_key(session_id)
    try:
        if claim_chat_session(
            table,
            key,
            caller_sub,
            expires_after=_expires_after(),
            allow_new=False,
            has_stored_messages=lambda: False,
            holds_earlier_record=lambda: False,
        ):
            return session_id
        owner = recorded_owner(table, key)
    except ClientError as e:
        logger.error("Could not verify knowledge base session ownership: %s", e)
        raise SessionNotOwned(
            "Unauthorized: knowledge base session ownership cannot be verified"
        ) from e
    if owner is not None:
        logger.warning("Refusing a knowledge base session owned by another user")
        raise SessionNotOwned(
            "Unauthorized: this knowledge base session belongs to another user"
        )
    logger.info("Knowledge base session has no recorded owner; starting a new one")
    return None


def _record_session(event, kb_response, continued):
    """Record a newly started Bedrock session against the caller.

    The session id Bedrock returns is handed back to the browser only once it is
    recorded as the caller's, so a later request can continue it. When it cannot
    be recorded, it is removed from the response and the next question starts a
    new session. A continued session was checked before the call and its record's
    expiry moved forward then.
    """
    session_id = kb_response.get("sessionId")
    if not session_id or event.get("identity") is None or session_id == continued:
        return
    caller_sub = _verified_sub(event)
    recorded = False
    if OWNERS_TABLE and caller_sub:
        try:
            recorded = claim_chat_session(
                _dynamodb.Table(OWNERS_TABLE),
                _owner_key(session_id),
                caller_sub,
                expires_after=_expires_after(),
                allow_new=True,
                has_stored_messages=lambda: False,
                holds_earlier_record=lambda: False,
            )
        except ClientError as e:
            logger.error("Could not record knowledge base session owner: %s", e)
    if not recorded:
        logger.warning("Knowledge base session not recorded; it will not continue")
        kb_response.pop("sessionId", None)


def handler(event, context):
    # Log a redacted copy — the raw event contains identity.claims and the
    # user's natural-language query, which is left out.
    logger.info(
        "Received event: %s",
        json.dumps(sanitize_event_for_logging(event, extra_deny_keys=["input"])),
    )
    _refuse_non_admin_when_unscoped_is_denied(event)
    query = event["arguments"]["input"]
    sessionId = _session_to_continue(event, event["arguments"].get("sessionId") or None)
    kb_response = get_kb_response(query, sessionId)
    _record_session(event, kb_response, sessionId)
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
