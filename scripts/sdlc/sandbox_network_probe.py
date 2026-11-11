#!/usr/bin/env python3
"""Measure what network access a Bedrock AgentCore Code Interpreter session has.

The analytics agent's Python tool (``idp_common.agents.analytics.tools.
code_interpreter_tools``) runs model-written code on the AWS-managed interpreter
``aws.codeinterpreter.v1``, the ``bedrock_agentcore`` SDK default. What that
session can reach is a property of the managed service, not of anything in this
repository, so it can change without a code change here. This script measures it.

It needs no IDP stack. One run:

1. creates a throwaway bucket ``ab-sandbox-probe-<random>`` and a presigned PUT
   URL for one key in it (15-minute expiry);
2. starts a session on the interpreter, exactly as the tool does, and runs a
   fixed stdlib-only Python program in it that tries DNS, HTTPS, the presigned
   S3 PUT, a raw TCP connection, and looks for AWS credentials (environment,
   credential files, the instance metadata service, the container credential
   endpoint), reporting each credential source only as present or absent;
3. checks from this host whether the PUT object arrived;
4. stops the session and deletes the bucket, whatever happened before.

Each observation is compared with ``EXPECTED_POSTURE``, the posture last measured
by hand, and a pass/fail table is printed. A FAIL means the interpreter's network
posture is different from the recorded one, in either direction; the exit code is
1 in that case. Exit 2 means the probe could not run (no session, no result line,
a teardown error), which is not a measurement.

The presigned URL is the only per-run value placed in the session. It
authorises one PUT of one key in the throwaway bucket and expires in 15 minutes.

Usage:
  python3 scripts/sdlc/sandbox_network_probe.py [--region us-west-2] [--json-out f]

Requires AWS credentials for the account (AWS_PROFILE=default or idp-ci) with
permission to start interpreter sessions and to create and delete a bucket.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from dataclasses import dataclass
from typing import Any

INTERPRETER_ID = "aws.codeinterpreter.v1"
BUCKET_PREFIX = "ab-sandbox-probe-"
PRESIGN_EXPIRY_SECONDS = 900
RESULT_MARKER = "SANDBOX_PROBE_RESULT="
PROBE_KEY = "probe/put-from-interpreter.txt"
PROBE_BODY = "written from the interpreter session"

# Every check the in-session program reports, in table order.
CHECK_NAMES = (
    "dns_example_com",
    "dns_s3_endpoint",
    "https_example_com",
    "s3_presigned_put",
    "tcp_1_1_1_1_443",
    "tcp_s3_other_region",
    "https_sts_regional",
    "imds_reachable",
    "creds_env",
    "creds_files",
    "creds_imds",
    "creds_container_endpoint",
)

# The posture measured by hand on 2026-11-10 in us-west-2: public names do not
# resolve and public addresses are unreachable, Amazon S3 in the session's own
# region is reachable (a presigned PUT lands), S3 in another region and STS are
# unreachable, the instance metadata service answers but lists no role, and no
# credential source is present. ``True`` means the check succeeded (reachable, or
# present). Only us-west-2 was measured; another region may differ. Update this
# only after re-measuring by hand and updating docs/agent-analysis.md to match.
EXPECTED_POSTURE: dict[str, bool] = {
    "dns_example_com": False,
    "dns_s3_endpoint": True,
    "https_example_com": False,
    "s3_presigned_put": True,
    "tcp_1_1_1_1_443": False,
    "tcp_s3_other_region": False,
    "https_sts_regional": False,
    "imds_reachable": True,
    "creds_env": False,
    "creds_files": False,
    "creds_imds": False,
    "creds_container_endpoint": False,
    "object_arrived": True,
}

# The program run inside the session. Standard library only, no network access
# beyond the named targets, and it prints exactly one marker line of JSON. The
# presigned URL is substituted in as a JSON string literal. Credential checks
# record presence only and never read a value into the output.
SESSION_PROGRAM = r"""
import json, os, socket, ssl, urllib.request, urllib.error

PUT_URL = __PUT_URL__
S3_HOST = __S3_HOST__
S3_OTHER_HOST = __S3_OTHER_HOST__
STS_HOST = __STS_HOST__
results = {}

def _err(e):
    text = f"{type(e).__name__}: {e}"
    return text.split("?", 1)[0][:300]

def check(name, fn):
    try:
        ok, detail = fn()
        results[name] = {"ok": bool(ok), "detail": str(detail)[:300]}
    except Exception as e:
        results[name] = {"ok": False, "detail": _err(e)}

def dns(host):
    def run():
        addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443)})
        return bool(addrs), f"{len(addrs)} address(es)"
    return run

def https_get():
    try:
        with urllib.request.urlopen("https://example.com/", timeout=10) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"

def s3_put():
    req = urllib.request.Request(
        PUT_URL, data=__PUT_BODY__.encode(), method="PUT")
    with urllib.request.urlopen(req, timeout=15) as r:
        return 200 <= r.status < 300, f"HTTP {r.status}"

def tcp(host):
    def run():
        s = socket.create_connection((host, 443), timeout=8)
        s.close()
        return True, "connected"
    return run

def https_sts():
    try:
        with urllib.request.urlopen(f"https://{STS_HOST}/", timeout=10) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"

def imds_token():
    req = urllib.request.Request(
        "http://169.254.169.254/latest/api/token", method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
    with urllib.request.urlopen(req, timeout=3) as r:
        return bool(r.read()), f"HTTP {r.status}, token issued"

ENV_NAMES = __ENV_NAMES__

def creds_env():
    present = [n for n in ENV_NAMES if os.environ.get(n)]
    return bool(present), "present: " + (",".join(present) or "none")

def creds_files():
    paths = [os.path.expanduser("~/.aws/credentials"),
             os.path.expanduser("~/.aws/config")]
    paths += [os.environ[n] for n in ("AWS_SHARED_CREDENTIALS_FILE",
                                      "AWS_CONFIG_FILE") if os.environ.get(n)]
    present = [p for p in paths if os.path.exists(p)]
    return bool(present), f"{len(present)} file(s) present"

def creds_imds():
    token = None
    try:
        req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token", method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"})
        with urllib.request.urlopen(req, timeout=3) as r:
            token = r.read().decode()
    except Exception as e:
        token_err = _err(e)
    else:
        token_err = None
    headers = {"X-aws-ec2-metadata-token": token} if token else {}
    req = urllib.request.Request(
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            listed = [x for x in r.read().decode().split() if x]
    except Exception as e:
        return False, f"token: {token_err or 'ok'}; list: {_err(e)}"
    return bool(listed), f"{len(listed)} role(s) listed"

def creds_container():
    rel = os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
    url = os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI") or (
        "http://169.254.170.2" + rel if rel else None)
    if not url:
        return False, "no container credential URI in environment"
    # The URI can carry a credential path, so an error reports its type only.
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            body = json.loads(r.read().decode() or "{}")
    except Exception as e:
        return False, f"URI set; request failed: {type(e).__name__}"
    return "AccessKeyId" in body, "credential document returned"

check("dns_example_com", dns("example.com"))
check("dns_s3_endpoint", dns(S3_HOST))
check("https_example_com", https_get)
check("s3_presigned_put", s3_put)
check("tcp_1_1_1_1_443", tcp("1.1.1.1"))
check("tcp_s3_other_region", tcp(S3_OTHER_HOST))
check("https_sts_regional", https_sts)
check("imds_reachable", imds_token)
check("creds_env", creds_env)
check("creds_files", creds_files)
check("creds_imds", creds_imds)
check("creds_container_endpoint", creds_container)
print("__MARKER__" + json.dumps(results, sort_keys=True))
"""


def other_region(region: str) -> str:
    """A region other than ``region``, for the cross-region S3 check."""
    return "us-east-1" if region != "us-east-1" else "us-west-2"


# The environment variables botocore's credential chain reads (EnvProvider,
# ContainerProvider, web identity, profile and file selection). The session
# reports which of them are set, never their values.
CREDENTIAL_ENV_NAMES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
)


def build_session_program(put_url: str, region: str) -> str:
    """Return the fixed in-session program with the per-run values filled in."""
    return (
        SESSION_PROGRAM.replace("__PUT_URL__", json.dumps(put_url))
        .replace("__S3_HOST__", json.dumps(f"s3.{region}.amazonaws.com"))
        .replace(
            "__S3_OTHER_HOST__",
            json.dumps(f"s3.{other_region(region)}.amazonaws.com"),
        )
        .replace("__STS_HOST__", json.dumps(f"sts.{region}.amazonaws.com"))
        .replace("__PUT_BODY__", json.dumps(PROBE_BODY))
        .replace("__ENV_NAMES__", json.dumps(list(CREDENTIAL_ENV_NAMES)))
        .replace("__MARKER__", RESULT_MARKER)
    )


def collect_stdout(events: list[dict[str, Any]]) -> str:
    """Concatenate the stdout an ``executeCode`` stream returned.

    Each stream event carries ``result``; the text appears both in
    ``structuredContent.stdout`` and in ``content[].text``. ``stdout`` is
    preferred, and ``content`` text is used only when no event had it. Any
    other event member is one of the service's exception events and is raised.
    """
    stdout_parts: list[str] = []
    content_parts: list[str] = []
    for event in events:
        other = sorted(set(event) - {"result"})
        if other:
            raise ValueError(f"the session returned an error event: {other}")
        result = event.get("result") or {}
        structured = result.get("structuredContent") or {}
        if structured.get("stdout"):
            stdout_parts.append(structured["stdout"])
        for item in result.get("content") or []:
            if item.get("type") == "text" and item.get("text"):
                content_parts.append(item["text"])
    return "".join(stdout_parts) if stdout_parts else "".join(content_parts)


def parse_session_output(stdout: str) -> dict[str, dict[str, Any]]:
    """Extract the result object from the marker line the program printed.

    Raises ``ValueError`` when there is no marker line, when it is not a JSON
    object, or when it does not report exactly the checks in ``CHECK_NAMES``.
    A partial report is refused rather than read as absent checks, because an
    absent network check would otherwise read as "unreachable".
    """
    lines = [ln for ln in stdout.splitlines() if ln.startswith(RESULT_MARKER)]
    if len(lines) != 1:
        raise ValueError(f"expected one result line, found {len(lines)}")
    data = json.loads(lines[0][len(RESULT_MARKER) :])
    if not isinstance(data, dict):
        raise ValueError("result line is not a JSON object")
    if set(data) != set(CHECK_NAMES):
        missing = sorted(set(CHECK_NAMES) - set(data))
        extra = sorted(set(data) - set(CHECK_NAMES))
        raise ValueError(f"result checks differ: missing={missing} extra={extra}")
    for name, entry in data.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("ok"), bool):
            raise ValueError(f"check {name!r} has no boolean 'ok'")
    return data


@dataclass(frozen=True)
class Row:
    check: str
    observed: bool
    expected: bool
    detail: str

    @property
    def passed(self) -> bool:
        return self.observed == self.expected


def evaluate(
    results: dict[str, dict[str, Any]],
    object_arrived: bool,
    expected: dict[str, bool] | None = None,
) -> list[Row]:
    """Compare observations with the expected posture, one row per check."""
    expected = EXPECTED_POSTURE if expected is None else expected
    rows = [
        Row(name, results[name]["ok"], expected[name], results[name].get("detail", ""))
        for name in CHECK_NAMES
    ]
    rows.append(
        Row(
            "object_arrived",
            object_arrived,
            expected["object_arrived"],
            "object found in bucket" if object_arrived else "object not in bucket",
        )
    )
    return rows


def render_table(rows: list[Row]) -> str:
    """Return the pass/fail table as text."""

    def yn(value: bool) -> str:
        return "yes" if value else "no"

    header = f"{'check':26s} {'observed':9s} {'expected':9s} {'result':7s} detail"
    out = [header, "-" * len(header)]
    for r in rows:
        verdict = "PASS" if r.passed else "FAIL"
        out.append(
            f"{r.check:26s} {yn(r.observed):9s} {yn(r.expected):9s} {verdict:7s} "
            f"{r.detail}"
        )
    return "\n".join(out)


def exit_code(rows: list[Row]) -> int:
    """0 when every row matches the expected posture, 1 otherwise."""
    return 0 if rows and all(r.passed for r in rows) else 1


def _run_live(region: str) -> tuple[dict[str, dict[str, Any]], bool]:
    import boto3
    from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter
    from botocore.config import Config

    session = boto3.Session(region_name=region)
    s3 = session.client(
        "s3",
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )
    bucket = BUCKET_PREFIX + secrets.token_hex(6)
    create_args: dict[str, Any] = {"Bucket": bucket}
    if region != "us-east-1":
        create_args["CreateBucketConfiguration"] = {"LocationConstraint": region}
    print(f"Creating bucket {bucket} in {region}", file=sys.stderr)
    s3.create_bucket(**create_args)
    client = None
    try:
        put_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": PROBE_KEY},
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
        )
        client = CodeInterpreter(region, session=session)
        client.start(identifier=INTERPRETER_ID)
        print(f"Started session on {INTERPRETER_ID}", file=sys.stderr)
        response = client.invoke(
            "executeCode",
            {
                "code": build_session_program(put_url, region),
                "language": "python",
                "clearContext": True,
            },
        )
        events = list(response["stream"])
        results = parse_session_output(collect_stdout(events))
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=PROBE_KEY)
        arrived = any(o["Key"] == PROBE_KEY for o in listed.get("Contents", []))
        return results, arrived
    finally:
        if client is not None:
            try:
                client.stop()
            except Exception as e:  # report, keep tearing down
                print(f"WARNING: stopping the session failed: {e}", file=sys.stderr)
        _delete_bucket(s3, bucket)


def _delete_bucket(s3: Any, bucket: str) -> None:
    try:
        for obj in s3.list_objects_v2(Bucket=bucket).get("Contents", []):
            s3.delete_object(Bucket=bucket, Key=obj["Key"])
        s3.delete_bucket(Bucket=bucket)
    except Exception as e:
        print(f"ERROR: bucket {bucket} was not deleted: {e}", file=sys.stderr)
        raise
    print(f"Deleted bucket {bucket}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--json-out", help="also write the observations here")
    args = parser.parse_args(argv)

    try:
        results, arrived = _run_live(args.region)
    except Exception as e:
        print(f"ERROR: the probe could not run: {type(e).__name__}: {e}")
        return 2
    rows = evaluate(results, arrived)
    print(render_table(rows))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(
                {
                    "region": args.region,
                    "interpreter": INTERPRETER_ID,
                    "results": results,
                    "object_arrived": arrived,
                },
                f,
                indent=2,
                sort_keys=True,
            )
    code = exit_code(rows)
    print(
        "\nAll checks match the recorded posture."
        if code == 0
        else "\nThe interpreter's network posture differs from the recorded one."
    )
    return code


if __name__ == "__main__":
    sys.exit(main())
