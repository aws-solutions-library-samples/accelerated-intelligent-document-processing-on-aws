#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Process PDF documents via MCP Server"""

import base64
import configparser
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import boto3
import requests


class DocumentProcessor:
    """Process documents via MCP Server with Cognito authentication"""

    def __init__(
        self,
        endpoint: str,
        client_id: str,
        client_secret: str,
        token_url: str,
        username: Optional[str] = None,
        password: Optional[str] = None,
        user_pool_id: Optional[str] = None,
        region: str = "us-east-1",
    ):
        self.endpoint = endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.username = username
        self.password = password
        self.user_pool_id = user_pool_id
        self.region = region
        self.access_token: Optional[str] = None
        self.cognito = boto3.client("cognito-idp", region_name=region)

    def get_secret_hash(self, username: str) -> str:
        """Compute SECRET_HASH for Cognito authentication"""
        message = bytes(username + self.client_id, "utf-8")
        secret = bytes(self.client_secret, "utf-8")
        dig = hmac.new(secret, message, hashlib.sha256).digest()
        return base64.b64encode(dig).decode()

    def authenticate(self) -> bool:
        """Get access token from Cognito"""
        try:
            if self.username and self.password and self.user_pool_id:
                print(f"    Using user credentials for {self.username}")
                secret_hash = self.get_secret_hash(self.username)
                response = self.cognito.admin_initiate_auth(
                    UserPoolId=self.user_pool_id,
                    ClientId=self.client_id,
                    AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                    AuthParameters={
                        "USERNAME": self.username,
                        "PASSWORD": self.password,
                        "SECRET_HASH": secret_hash,
                    },
                )
                self.access_token = response["AuthenticationResult"]["AccessToken"]
                print(f"    Token obtained (length: {len(self.access_token)})")
                return True
            else:
                print("    Using client credentials flow via token endpoint")

                auth = (self.client_id, self.client_secret)
                data = {
                    "grant_type": "client_credentials",
                }

                response = requests.post(
                    self.token_url,
                    auth=auth,
                    data=data,
                    timeout=10,
                )
                print(f"    Token response status: {response.status_code}")

                if response.status_code != 200:
                    print(f"    Token request failed: {response.text}")
                    return False

                token_data = response.json()
                self.access_token = token_data.get("access_token")
                print(f"    Token obtained (length: {len(self.access_token)})")
                return True
        except Exception as e:
            print(f"    Authentication failed: {e}")
            import traceback

            traceback.print_exc()
            return False

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Call MCP tool via AgentCore Gateway"""
        if not self.access_token:
            raise RuntimeError("Not authenticated. Call authenticate() first.")

        if "IDPTools" not in tool_name:
            tool_name = f"IDPTools___{tool_name}"

        request_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        print(f"    Calling tool: {tool_name}")

        try:
            response = requests.post(
                self.endpoint,
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
                timeout=30,
            )
            print(f"    Response status: {response.status_code}")
            result = response.json()
            return result
        except Exception as e:
            print(f"    Tool call exception: {e}")
            return {"error": str(e)}


def extract_response_data(response: dict) -> dict:
    """Extract tool result from MCP gateway response"""
    print("    Extracting result from response...")

    # Handle MCP gateway wrapper: result.content[0].text
    if "result" in response and isinstance(response["result"], dict):
        result = response["result"]
        if (
            "content" in result
            and isinstance(result["content"], list)
            and len(result["content"]) > 0
        ):
            content_item = result["content"][0]
            if isinstance(content_item, dict) and "text" in content_item:
                text = content_item["text"]
                if isinstance(text, str):
                    try:
                        data = json.loads(text)
                        print("    Parsed MCP content text")

                        # Handle Lambda statusCode/body wrapper
                        if isinstance(data, dict) and "statusCode" in data:
                            body = data.get("body")
                            if isinstance(body, str):
                                try:
                                    data = json.loads(body)
                                    print("    Parsed Lambda body")
                                except json.JSONDecodeError:
                                    pass

                        return data
                    except json.JSONDecodeError as e:
                        print(f"    JSON parse error: {e}")
                        return {"error": "Invalid JSON in content text"}

    return response


def load_config(config_path: str) -> dict:
    """Load configuration from properties file"""
    config = configparser.ConfigParser()
    config.read(config_path)
    return dict(config["DEFAULT"])


def process_pdf(processor: DocumentProcessor, pdf_path: str) -> Optional[str]:
    """Process PDF by converting to base64 and calling MCP server"""
    print(f"\n[1] Reading PDF: {pdf_path}")
    filename = Path(pdf_path).name
    file_size = Path(pdf_path).stat().st_size / 1024
    print(f"    PDF size: {file_size:.2f} KB")

    with open(pdf_path, "rb") as f:
        pdf_binary = f.read()
    pdf_base64 = base64.b64encode(pdf_binary).decode("utf-8")

    print("\n[2] Sending process request...")
    result = processor.call_tool("process", {"content": pdf_base64, "name": filename})

    if result.get("error"):
        print(f"    Error: {result.get('error')}")
        return None

    result_data = extract_response_data(result)

    if not result_data.get("success"):
        print(f"    Error: {result_data.get('error', 'Unknown error')}")
        return None

    batch_id = result_data.get("batch_id")
    print(f"    Batch ID: {batch_id}")
    print(f"    Documents queued: {result_data.get('documents_queued')}")
    return batch_id


def check_status(
    processor: DocumentProcessor, batch_id: str, max_wait: int = 300
) -> dict:
    """Poll status until processing completes or timeout"""
    print("\n[3] Checking processing status...")
    start_time = time.time()

    while time.time() - start_time < max_wait:
        result = processor.call_tool("status", {"batch_id": batch_id})

        if result.get("error"):
            print(f"    Error: {result.get('error')}")
            return result

        result_data = extract_response_data(result)

        if not result_data.get("success"):
            print(f"    Error: {result_data.get('error', 'Unknown error')}")
            return result

        status = result_data.get("status", {})
        progress = result_data.get("progress", {})
        percentage = progress.get("percentage", 0)

        print(
            f"    Progress: {percentage:.1f}% | "
            f"Completed: {status.get('completed')}/{status.get('total')} | "
            f"Failed: {status.get('failed')}"
        )

        if result_data.get("all_complete"):
            # Settled: nothing queued or running. A document held for human
            # review counts as settled and has no final result yet.
            awaiting = status.get("awaiting_review", 0)
            if awaiting:
                print(f"    Processing finished; {awaiting} document(s) await review")
            else:
                print("    Processing complete!")
            return result

        time.sleep(5)

    print("    Timeout waiting for processing to complete")
    return result


def get_results(processor: DocumentProcessor, batch_id: str) -> dict:
    """Retrieve processing results for completed batch"""
    print("\n[4] Retrieving processing results...")
    result = processor.call_tool(
        "get_results", {"batch_id": batch_id, "section_id": 1, "limit": 10}
    )

    if result.get("error"):
        print(f"    Error: {result.get('error')}")
        return result

    result_data = extract_response_data(result)

    if not result_data.get("success"):
        print(f"    Error: {result_data.get('error', 'Unknown error')}")
        return result

    print(f"    Retrieved {result_data.get('count')} documents")
    print(f"    Total in batch: {result_data.get('total_in_batch')}")

    documents = result_data.get("documents", [])
    if documents:
        print("\n    Document Results:")
        for doc in documents:
            print(f"      - {doc.get('document_id')}")
            print(f"        Class: {doc.get('document_class')}")
            print(f"        Status: {doc.get('status')}")
            if doc.get("fields"):
                print(f"        Fields: {json.dumps(doc.get('fields'), indent=10)}")
            if doc.get("confidence"):
                print(
                    f"        Confidence: {json.dumps(doc.get('confidence'), indent=10)}"
                )

    return result


def main():
    """Main workflow"""
    config_path = Path(__file__).parent / "mcp_client_config.properties"

    if not config_path.exists():
        print(f"Error: Config file not found at {config_path}")
        sys.exit(1)

    config = load_config(str(config_path))

    required = [
        "mcp_endpoint",
        "mcp_client_id",
        "mcp_client_secret",
        "mcp_token_url",
        "mcp_username",
        "mcp_password",
        "mcp_user_pool_id",
    ]
    missing = [k for k in required if not config.get(k)]
    if missing:
        print(f"Error: Missing config: {', '.join(missing)}")
        sys.exit(1)

    processor = DocumentProcessor(
        endpoint=config["mcp_endpoint"],
        client_id=config["mcp_client_id"],
        client_secret=config["mcp_client_secret"],
        token_url=config["mcp_token_url"],
        username=config["mcp_username"],
        password=config["mcp_password"],
        user_pool_id=config["mcp_user_pool_id"],
    )

    print("=" * 60)
    print("MCP Client Example - PDF Processing")
    print("=" * 60)

    print("\n[0] Authenticating with Cognito...")
    if not processor.authenticate():
        print("Authentication failed")
        sys.exit(1)
    print("    Authentication successful")

    pdf_path = config.get("sample_pdf_path", "./sample.pdf")
    if not Path(pdf_path).exists():
        print(f"Error: Sample PDF not found at {pdf_path}")
        sys.exit(1)

    batch_id = process_pdf(processor, pdf_path)
    if not batch_id:
        sys.exit(1)

    check_status(processor, batch_id)
    get_results(processor, batch_id)

    print("\n" + "=" * 60)
    print("Processing completed successfully!")
    print("=" * 60)


if __name__ == "__main__":
    main()
