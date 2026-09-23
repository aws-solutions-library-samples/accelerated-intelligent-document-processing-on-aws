#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
e2e_test_headless.py

End-to-end test for the IDP Headless/GovCloud Jobs REST API.

Flow:
  1. Acquire OAuth token via scripts/get_api_token.sh
  2. POST /jobs to create a job + presigned upload URL
  3. Upload the provided input file using the presigned POST
  4. Poll GET /jobs/{job_id} until status is terminal
  5. Download result zip and verify it is non-empty
  6. Print PASS/FAIL summary

Prerequisites:
  - AWS CLI credentials configured for the target account
  - Bastion SSH tunnel running in another terminal: ./scripts/bastion.sh <STACK_NAME>

Usage:
  ./scripts/e2e_test_headless.py <STACK_NAME> <PATH_TO_FILE>
  ex: ./scripts/e2e_test_headless.py idp-stack samples/irs_documents.zip --timeout 1800 --poll 15
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import requests
import urllib3

REPO_ROOT = Path(__file__).resolve().parent.parent
GET_TOKEN_SCRIPT = REPO_ROOT / "scripts" / "get_api_token.sh"

TERMINAL_STATUSES = {"SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED", "ABORTED"}
SUCCESS_STATUSES = {"SUCCEEDED", "PARTIALLY_SUCCEEDED"}


def log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def get_token(stack_name: str) -> str:
    """Invoke scripts/get_api_token.sh and return the bearer token."""
    log("Acquiring OAuth bearer token…")
    result = subprocess.run(
        [str(GET_TOKEN_SCRIPT), stack_name],
        capture_output=True,
        text=True,
        check=True,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError(
            f"get_api_token.sh produced no token. stderr: {result.stderr}"
        )
    return token


def get_api_endpoint(stack_name: str) -> str:
    """Read ApiGatewayEndpoint output from the stack."""
    result = subprocess.run(
        [
            "aws",
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            stack_name,
            "--query",
            "Stacks[0].Outputs[?OutputKey=='ApiGatewayEndpoint'].OutputValue",
            "--output",
            "text",
            "--no-cli-pager",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    endpoint = result.stdout.strip()
    if not endpoint:
        raise RuntimeError(f"ApiGatewayEndpoint not found on stack {stack_name}")
    return endpoint


def create_job(
    api_endpoint: str, token: str, filename: str, session: requests.Session
) -> dict:
    """POST /jobs — returns {jobId, upload: {uploadUrl, requiredHeaders, ...}}."""
    log(f"POST {api_endpoint}/jobs (fileName={filename})")
    r = session.post(
        f"{api_endpoint}/jobs",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"fileName": filename},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    log(f"Created jobId={data['jobId']}")
    return data


def upload_zip(upload_info: dict, zip_path: Path, session: requests.Session) -> None:
    """Upload zip to the presigned POST URL."""
    log(f"Uploading {zip_path.name} ({zip_path.stat().st_size:,} bytes)…")
    with zip_path.open("rb") as fh:
        r = session.post(
            upload_info["uploadUrl"],
            data=upload_info["requiredHeaders"],
            files={"file": (zip_path.name, fh)},
            timeout=600,
        )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Upload failed HTTP {r.status_code}: {r.text[:500]}")
    log(f"Upload succeeded (HTTP {r.status_code})")


def poll_job(
    api_endpoint: str,
    token: str,
    job_id: str,
    timeout: int,
    interval: int,
    session: requests.Session,
) -> dict:
    """Poll GET /jobs/{id} until terminal status or timeout."""
    deadline = time.time() + timeout
    last_status = None
    while time.time() < deadline:
        try:
            r = session.get(
                f"{api_endpoint}/jobs/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=60,
            )
            r.raise_for_status()
        except (requests.Timeout, requests.ConnectionError) as e:
            log(f"Transient poll error ({type(e).__name__}): {e}. Retrying…")
            time.sleep(interval)
            continue
        data = r.json()
        status = data.get("status", "UNKNOWN")
        if status != last_status:
            log(f"Status: {status}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return data
        time.sleep(interval)
    raise TimeoutError(f"Job {job_id} did not reach terminal state within {timeout}s")


def download_result(download_url: str, job_id: str, session: requests.Session) -> Path:
    """Download the result zip and return the local path."""
    out_dir = REPO_ROOT / "test_results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"results_{job_id}.zip"
    log(f"Downloading results → {out_path}")
    r = session.get(download_url, timeout=600)
    r.raise_for_status()
    out_path.write_bytes(r.content)
    return out_path


def main() -> int:
    parser = argparse.ArgumentParser(description="E2E test for IDP Headless Jobs API")
    parser.add_argument("stack_name", help="IDP CloudFormation stack name")
    parser.add_argument(
        "file",
        help="Path to the input file to upload (e.g. samples/irs_documents.zip)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Max poll wait in seconds (default: 1800)",
    )
    parser.add_argument(
        "--poll", type=int, default=15, help="Poll interval in seconds (default: 15)"
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification (needed when bastion tunnel serves self-signed certs via /etc/hosts remap)",
    )
    args = parser.parse_args()

    input_path = Path(args.file).expanduser().resolve()
    if not input_path.is_file():
        log(f"ERROR: Input file not found: {input_path}")
        return 2

    session = requests.Session()
    if args.insecure:
        session.verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        api_endpoint = get_api_endpoint(args.stack_name)
        log(f"API endpoint: {api_endpoint}")

        token = get_token(args.stack_name)

        job = create_job(api_endpoint, token, input_path.name, session)
        job_id = job["jobId"]
        upload_zip(job["upload"], input_path, session)

        final = poll_job(api_endpoint, token, job_id, args.timeout, args.poll, session)
        status = final.get("status")

        if status not in SUCCESS_STATUSES:
            log(f"FAIL: Job ended with status={status}")
            log(f"Final payload: {json.dumps(final, indent=2)}")
            return 1

        result = final.get("result") or {}
        download_url = result.get("downloadUrl")
        if not download_url:
            log(
                f"FAIL: No downloadUrl in terminal response: {json.dumps(final, indent=2)}"
            )
            return 1

        out_path = download_result(download_url, job_id, session)
        size = out_path.stat().st_size
        if size == 0:
            log(f"FAIL: Downloaded result is empty ({out_path})")
            return 1

        log(f"PASS: jobId={job_id} status={status} result={out_path} ({size:,} bytes)")
        return 0

    except subprocess.CalledProcessError as e:
        log(f"ERROR: subprocess failed: {e.cmd}\nstderr: {e.stderr}")
        return 2
    except requests.HTTPError as e:
        log(f"ERROR: HTTP {e.response.status_code}: {e.response.text[:500]}")
        return 2
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
