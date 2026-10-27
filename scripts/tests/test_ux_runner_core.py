# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the UX runner's toolchain paths and its redaction.

``paths`` is a contract with ``install.sh``, the managed policy and the egress
proxy, so what is pinned here is the part only this module decides: which prefix
wins, where each binary is on each platform, what counts as missing, and how a bad
``VERSIONS.json`` reads. ``redact`` is what stands between the job's secrets and
everything it publishes, so each of its layers is shown to fire on the shape it
exists for and to leave alone what it must not touch, such as the region in a
report. The last tests hold the package to its import rule: no module of the
runner core imports a third-party library at module level, because the precheck
runs on a bare ``python3``. Nothing here needs AWS, a network or a browser.
"""

from __future__ import annotations

import ast
import json
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import paths, redact  # noqa: E402

PACKAGE = SCRIPTS / "ux_runner"
CORE_MODULES = (
    "__init__.py",
    "paths.py",
    "processes.py",
    "redact.py",
    "tool_profiles.py",
    "agent.py",
    "account.py",
)


def _ui_env() -> dict[str, str]:
    """A parsed UX_TEST_UI_ENV in the WebUITestEnvFile shape, every id fake."""
    return {
        "VITE_USER_POOL_ID": "us-east-1_AbCdEfGhI",
        "VITE_USER_POOL_CLIENT_ID": "1a2b3c4d5e6f7g8h9i0j1k2l3m",
        "VITE_IDENTITY_POOL_ID": "us-east-1:0a1b2c3d-1111-2222-3333-4e5f6a7b8c9d",
        "VITE_API_BASE_URL": "https://abcde12345.execute-api.us-east-1.amazonaws.com/api",
        "VITE_STREAM_URL": (
            "https://abcdefghijklmnopqrstuvwxyz012345.lambda-url.us-east-1.on.aws/"
        ),
        "VITE_AWS_REGION": "us-east-1",
        "VITE_ENABLE_QUICK_START_WIDGET": "true",
        "VITE_SETTINGS_PARAMETER": "idp-ux-nightly-Settings",
        "VITE_TEST_SET_BUCKET_NAME": "idp-ux-nightly-testsetbucket-abc123",
        "VITE_INPUT_BUCKET_NAME": "idp-ux-nightly-inputbucket-def456",
        "VITE_LATENCY_OPTIONS": "[60, 120, 300, 600, 900, 1800, 3600]",
        "VITE_DEFAULT_MAX_LATENCY": "60",
        "UX_TEST_BUCKETS": (
            "idp-ux-nightly-outputbucket-ghi789,idp-ux-nightly-workingbucket-jkl012"
        ),
    }


@pytest.mark.unit
def test_an_explicit_prefix_beats_the_environment_which_beats_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environ = {paths.PREFIX_ENV: "/from/env"}
    assert paths.toolchain("/from/arg", environ).prefix == Path("/from/arg")
    assert paths.toolchain(Path("/from/arg"), environ).prefix == Path("/from/arg")
    assert paths.toolchain(None, environ).prefix == Path("/from/env")
    assert paths.toolchain(None, {}).prefix == paths.DEFAULT_PREFIX
    assert paths.toolchain("", {paths.PREFIX_ENV: ""}).prefix == paths.DEFAULT_PREFIX

    monkeypatch.setenv(paths.PREFIX_ENV, "/from/process/env")
    assert paths.toolchain().prefix == Path("/from/process/env")
    monkeypatch.delenv(paths.PREFIX_ENV)
    assert paths.toolchain().prefix == Path("/opt/ux-runner")


@pytest.mark.unit
def test_the_chrome_binary_is_found_where_each_platform_unpacks_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chain = paths.Toolchain(Path("/opt/ux-runner"))
    monkeypatch.setattr(paths.sys, "platform", "linux")
    assert chain.chrome == Path("/opt/ux-runner/chrome/chrome")
    monkeypatch.setattr(paths.sys, "platform", "darwin")
    assert chain.chrome == Path(
        "/opt/ux-runner/chrome/Google Chrome for Testing.app/Contents/MacOS/"
        "Google Chrome for Testing"
    )


@pytest.mark.unit
def test_the_node_tools_live_in_the_lockfile_install(tmp_path: Path) -> None:
    chain = paths.Toolchain(tmp_path)
    assert chain.node_modules == tmp_path / "node_modules"
    assert chain.claude == tmp_path / "node_modules" / ".bin" / "claude"
    assert chain.devtools_mcp == (
        tmp_path / "node_modules" / ".bin" / "chrome-devtools-mcp"
    )
    assert chain.versions_file == tmp_path / "VERSIONS.json"


@pytest.mark.unit
def test_missing_names_exactly_the_binaries_that_are_not_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(paths.sys, "platform", "linux")
    chain = paths.Toolchain(tmp_path)
    assert chain.missing() == ["chrome", "claude", "chrome-devtools-mcp"]

    chain.claude.parent.mkdir(parents=True)
    chain.claude.write_text("#!/bin/sh\n", encoding="utf-8")
    assert chain.missing() == ["chrome", "chrome-devtools-mcp"]

    chain.chrome.parent.mkdir(parents=True)
    chain.chrome.write_text("", encoding="utf-8")
    chain.devtools_mcp.symlink_to(tmp_path / "nowhere")
    assert chain.missing() == ["chrome-devtools-mcp"], (
        "a dangling node_modules/.bin link must read as missing"
    )

    target = tmp_path / "server.js"
    target.write_text("", encoding="utf-8")
    chain.devtools_mcp.unlink()
    chain.devtools_mcp.symlink_to(target)
    assert chain.missing() == []


@pytest.mark.unit
def test_versions_is_empty_unless_the_record_is_an_object_of_strings(
    tmp_path: Path,
) -> None:
    chain = paths.Toolchain(tmp_path)
    assert chain.versions() == {}, "an absent VERSIONS.json"

    chain.versions_file.write_text("{not json", encoding="utf-8")
    assert chain.versions() == {}, "invalid JSON"

    chain.versions_file.write_text('["1.10.1"]', encoding="utf-8")
    assert chain.versions() == {}, "a JSON list"

    chain.versions_file.write_text('{"node": 22}', encoding="utf-8")
    assert chain.versions() == {}, "a non-string value"

    chain.versions_file.write_bytes(b"\xff\xfe")
    assert chain.versions() == {}, "bytes that are not UTF-8"

    record = {"chrome": "153.0.7000.0", "node": "v22.14.0", "claude_code": "2.1.281"}
    chain.versions_file.write_text(json.dumps(record), encoding="utf-8")
    assert chain.versions() == record


@pytest.mark.unit
def test_the_fixed_contract_values_are_the_ones_install_and_the_policy_use() -> None:
    assert (paths.PROXY_HOST, paths.PROXY_PORT) == ("127.0.0.1", 3128)
    assert (paths.CDP_HOST, paths.CDP_PORT) == ("127.0.0.1", 9222)
    assert paths.UI_ORIGIN == "http://localhost:3000"
    assert paths.UI_ORIGIN == f"http://{paths.UI_HOST}:{paths.UI_PORT}"
    assert paths.VIEWPORT == (1600, 1000)
    uids = (paths.BROWSER_UID, paths.PROXY_UID, paths.RENDER_UID)
    users = (paths.BROWSER_USER, paths.PROXY_USER, paths.RENDER_USER)
    assert len(set(uids)) == len(set(users)) == 3, "each tool runs as a uid of its own"
    assert paths.POLICY_FILE == paths.PACKAGE_DIR / "chrome_policy.json"
    assert paths.POLICY_INSTALL_NAME == "ux-runner.json"
    assert paths.POLICY_INSTALL_DIRS[0] == Path(
        "/etc/opt/chrome_for_testing/policies/managed"
    )


@pytest.mark.unit
def test_the_package_directories_resolve_to_this_checkout() -> None:
    assert paths.PACKAGE_DIR == PACKAGE
    assert paths.SCRIPTS_DIR == SCRIPTS
    assert paths.HARNESS_ROOT == SCRIPTS.parent
    assert (paths.HARNESS_ROOT / "scripts" / "ux_runner" / "paths.py").is_file()


@pytest.mark.unit
def test_registered_values_are_replaced_longest_first() -> None:
    scrub = redact.Redactor(["abcdef", "abcdefghij"])
    out = scrub("one abcdefghij two abcdef three")
    assert out == "one [redacted] two [redacted] three"
    assert "ghij" not in out, "the shorter value must not split the longer one"


@pytest.mark.unit
def test_values_shorter_than_the_minimum_are_left_alone() -> None:
    assert redact.Redactor(["Admin"])("as Admin") == "as Admin"
    assert redact.Redactor(["Admin"], min_length=5)("as Admin") == "as [redacted]"
    scrub = redact.Redactor()
    scrub.add("   ", "short", "  long-enough-value\n")
    assert scrub("short long-enough-value") == "short [redacted]"


@pytest.mark.unit
def test_a_twelve_digit_run_is_an_account_and_a_thirteen_digit_run_is_not() -> None:
    scrub = redact.Redactor()
    assert scrub("account 123456789012 here") == "account [account] here"
    assert scrub("arn:aws:iam::111122223333:role/x") == "arn:aws:iam::[account]:role/x"
    assert scrub("ts 1234567890123") == "ts 1234567890123"


@pytest.mark.unit
def test_access_key_ids_and_jwts_are_replaced() -> None:
    scrub = redact.Redactor()
    assert scrub("key=AKIAIOSFODNN7EXAMPLE;") == "key=[access-key];"
    assert scrub("ASIAIOSFODNN7EXAMPLE") == "[access-key]"
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1eC10ZXN0In0.c2lnbmF0dXJlLXZhbHVlLXRleHQ"
    assert scrub(f"Bearer {jwt}") == "Bearer [token]"
    jwe = f"{jwt}.ZW5jcnlwdGVkLWJvZHk.dGFnLXZhbHVlLXh5eg"
    assert scrub(f'"{jwe}"') == '"[token]"', "every segment of a JWE goes"
    assert scrub("eyJshort.abc.def") == "eyJshort.abc.def", "segments under 8 chars"


@pytest.mark.unit
def test_email_addresses_are_replaced_and_package_versions_are_not() -> None:
    scrub = redact.Redactor()
    assert scrub("from someone@example.invalid.") == "from [email]."
    assert scrub("<ux-persona-admin@example.invalid>") == "<[email]>"
    assert scrub("chrome-devtools-mcp@1.10.1") == "chrome-devtools-mcp@1.10.1"


#: A presigned GetObject URL, its values percent-encoded.
PRESIGNED_PATH = (
    "https://idp-ux-nightly-inputbucket-def456.s3.us-east-1.amazonaws.com"
    "/input/lending_package.pdf"
)
PRESIGNED_KEY_ID = "ASIAIOSFODNN7EXAMPLE"
PRESIGNED_SESSION = (
    "IQoJb3JpZ2luX2VjEJr%2F%2F%2F%2F%2F%2F%2F%2F%2F%2FwEaCXVzLWVhc3QtMSJHMEUCIQD"
    "x7Yz%2BK3qv%3D"
)
PRESIGNED_SIGNATURE = "0d1e2f3a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9a0b1c2d3e4f50"
PRESIGNED_URL = (
    f"{PRESIGNED_PATH}?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={PRESIGNED_KEY_ID}%2F20261015%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261015T120000Z&X-Amz-Expires=900"
    f"&X-Amz-Security-Token={PRESIGNED_SESSION}"
    f"&X-Amz-Signature={PRESIGNED_SIGNATURE}&X-Amz-SignedHeaders=host&x-id=GetObject"
)


@pytest.mark.unit
def test_a_presigned_url_keeps_its_path_and_loses_every_signing_value() -> None:
    """The key id must go on its own rule, not because ``%2F`` ends its word."""
    scrub = redact.Redactor()
    for text in (PRESIGNED_URL, f"[the document]({PRESIGNED_URL})"):
        out = scrub(text)
        for value in (PRESIGNED_KEY_ID, PRESIGNED_SESSION[:24], PRESIGNED_SIGNATURE):
            assert value not in out
        assert PRESIGNED_PATH in out
        assert "X-Amz-Credential=[redacted]&X-Amz-Date=20261015T120000Z" in out
        assert "X-Amz-Security-Token=[redacted]&X-Amz-Signature=[redacted]&" in out
        assert out.endswith(
            "X-Amz-SignedHeaders=host&x-id=GetObject" + (")" if text[0] == "[" else "")
        )
    sigv2 = (
        "https://b.s3.amazonaws.com/k?AWSAccessKeyId=AKIAIOSFODNN7EXAMPLE"
        "&Expires=1760529600&Signature=abc%2Bdef%3D&x-amz-security-token=FwoGZXIvYXdz"
    )
    assert scrub(sigv2) == (
        "https://b.s3.amazonaws.com/k?AWSAccessKeyId=[redacted]&Expires=1760529600"
        "&Signature=[redacted]&x-amz-security-token=[redacted]"
    )
    nested = "https://e.example/go?to=" + PRESIGNED_URL.replace("&", "%26").replace(
        "=", "%3D"
    )
    assert PRESIGNED_SIGNATURE not in scrub(nested)
    assert PRESIGNED_KEY_ID not in scrub(nested)


@pytest.mark.unit
def test_an_opaque_bearer_value_and_a_gitlab_token_header_are_replaced() -> None:
    scrub = redact.Redactor()
    assert scrub("Authorization: Bearer 0pAqUe-v4lue.with~chars") == (
        "Authorization: Bearer [token]"
    )
    assert scrub("authorization: bearer 0pAqUe-v4lue-0123456789") == (
        "authorization: bearer [token]"
    )
    assert scrub("PRIVATE-TOKEN: 0paque-gitlab-value") == "PRIVATE-TOKEN: [token]"
    assert scrub('{"PRIVATE-TOKEN": "0paque-gitlab-value"}') == (
        '{"PRIVATE-TOKEN": "[token]"}'
    )
    assert scrub("JOB-TOKEN=0paque-job-value-1") == "JOB-TOKEN=[token]"
    prose = "the API uses bearer authentication, and the PRIVATE-TOKEN header"
    assert scrub(prose) == prose


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "glpat-AbCdEfGhIj0123456789",
        "glcbt-64_AbCdEfGhIjKlMnOpQrStUv",
        "gldt-AbCdEfGhIj0123456789",
        "glrt-t1_AbCdEfGhIj0123456789",
        "glptt-0123456789abcdef0123",
        "GR1348941AbCdEfGhIj0123456789",
    ],
)
def test_a_gitlab_token_nobody_registered_is_replaced_by_its_prefix(value: str) -> None:
    assert redact.Redactor()(f"token {value} leaked") == "token [token] leaked"


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        "IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJHMEUCIQD+K3qv=",
        "FwoGZXIvYXdzEBYaDOrK0J2Gq1R5q8N7WCKCAa8y2hZ",
        "AQoDYXdzEPT//////////wEa4AOb3kU1aRNkNMs2q1w5",
        "IQoJb3JpZ2luX2VjEJr%2F%2F%2F%2F%2FwEaCXVzLWVhc3Q",
    ],
)
def test_an_sts_session_token_nobody_registered_is_replaced_by_its_prefix(
    value: str,
) -> None:
    """The browser's own Cognito-vended session is never registered, and a page can
    show it; STS issues every session token with one of these prefixes."""
    assert redact.Redactor()(f"session {value} here") == "session [token] here"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "X-Amz-Security-Token: IQoJb3JpZ2luX2VjEJr+abc/def==",
            "X-Amz-Security-Token: [redacted]",
        ),
        (
            "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "aws_secret_access_key = [redacted]",
        ),
        (
            '{"SecretAccessKey": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}',
            '{"SecretAccessKey": "[redacted]"}',
        ),
        ("AWS_SESSION_TOKEN=opaque-session-value", "AWS_SESSION_TOKEN=[redacted]"),
        ("the secret access key and the session token", None),
    ],
)
def test_a_security_token_header_and_a_keyed_aws_secret_lose_their_values(
    text: str, expected: str | None
) -> None:
    assert redact.Redactor()(text) == (text if expected is None else expected)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        PRESIGNED_URL,
        f"[the document]({PRESIGNED_URL})",
        "https://b.s3.amazonaws.com/k?AWSAccessKeyId=AKIAIOSFODNN7EXAMPLE&Signature=x",
        "PRIVATE-TOKEN: 0paque-gitlab-value and JOB-TOKEN=0paque-job-value-1",
        "Authorization: Bearer 0pAqUe-v4lue.with~chars",
        "glpat-AbCdEfGhIj0123456789 and GR1348941AbCdEfGhIj0123456789",
        "session IQoJb3JpZ2luX2VjEJr//////////wEaCXVzLWVhc3QtMSJHMEUCIQD+K3qv=",
        "X-Amz-Security-Token: IQoJb3JpZ2luX2VjEJr+abc/def==",
        "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "account 123456789012, key AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1eC10ZXN0In0.c2lnbmF0dXJlLXZhbHVlLXRleHQ",
        "from someone@example.invalid and a registered-value-123",
    ],
)
def test_redacting_twice_gives_what_redacting_once_gives(text: str) -> None:
    """The job redacts model text more than once, so no shape may read one of its
    own placeholders as a value and wrap or extend it."""
    scrub = redact.Redactor(["registered-value-123"])
    once = scrub(text)
    assert once != text
    assert scrub(once) == once


@pytest.mark.unit
def test_a_gitlab_prefix_without_a_token_after_it_is_left_alone() -> None:
    text = "a glpat- prefix, glpat-short and the glossary-entry stay"
    assert redact.Redactor()(text) == text


@pytest.mark.unit
def test_the_shape_patterns_stay_linear_on_a_long_run_without_a_match() -> None:
    scrub = redact.Redactor()
    blob = "a" * 80_000 + " " + "eyJ" * 25_000
    started = time.perf_counter()
    assert scrub(blob) == blob
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0, (
        f"redacting {len(blob)} unmatched characters took {elapsed:.1f}s; a shape "
        "pattern is rescanning the run from every offset (it takes ~10 ms anchored "
        "and ~30 s without the anchors)"
    )


@pytest.mark.unit
def test_tree_redacts_every_string_leaf_and_keeps_every_key() -> None:
    scrub = redact.Redactor(["persona-password-value"])
    document = {
        "someone@example.invalid": "persona-password-value",
        "nested": [
            "account 123456789012",
            {"deeper": ("persona-password-value", 3)},
            7,
            None,
            True,
        ],
    }
    out = scrub.tree(document)
    assert out == {
        "someone@example.invalid": "[redacted]",
        "nested": ["account [account]", {"deeper": ("[redacted]", 3)}, 7, None, True],
    }
    assert document["someone@example.invalid"] == "persona-password-value", (
        "the input must not be modified"
    )


@pytest.mark.unit
def test_ui_env_values_cover_the_stack_hosts_and_buckets_but_not_the_region() -> None:
    env = _ui_env()
    values = redact.values_from_ui_env(env)
    assert "abcde12345.execute-api.us-east-1.amazonaws.com" in values
    assert env["VITE_API_BASE_URL"] in values
    assert "abcdefghijklmnopqrstuvwxyz012345.lambda-url.us-east-1.on.aws" in values
    for bucket in (
        "idp-ux-nightly-inputbucket-def456",
        "idp-ux-nightly-testsetbucket-abc123",
        "idp-ux-nightly-outputbucket-ghi789",
        "idp-ux-nightly-workingbucket-jkl012",
    ):
        assert bucket in values
    assert env["VITE_USER_POOL_ID"] in values
    assert env["VITE_USER_POOL_CLIENT_ID"] in values
    assert env["VITE_IDENTITY_POOL_ID"] in values
    assert env["VITE_SETTINGS_PARAMETER"] in values
    assert "us-east-1" not in values
    assert env["VITE_LATENCY_OPTIONS"] not in values
    assert "true" not in values
    assert len(values) == len(set(values))


@pytest.mark.unit
def test_a_redactor_built_from_the_ui_env_leaves_no_stack_identifier() -> None:
    env = _ui_env()
    scrub = redact.Redactor(redact.values_from_ui_env(env))
    text = "\n".join(f"{key}={value}" for key, value in env.items())
    out = scrub(text)
    for key in (
        "VITE_USER_POOL_ID",
        "VITE_USER_POOL_CLIENT_ID",
        "VITE_IDENTITY_POOL_ID",
        "VITE_API_BASE_URL",
        "VITE_STREAM_URL",
        "VITE_SETTINGS_PARAMETER",
        "VITE_INPUT_BUCKET_NAME",
        "VITE_TEST_SET_BUCKET_NAME",
    ):
        assert env[key] not in out, key
    assert "execute-api" not in out
    assert "lambda-url" not in out
    assert "idp-ux-nightly-" not in out
    assert "VITE_AWS_REGION=us-east-1" in out


@pytest.mark.unit
def test_the_web_url_line_is_redacted_whole_and_by_its_host() -> None:
    """A run against the deployed UI reads the stack's web URL from a line of the
    variable itself, so it identifies the stack from the first line printed."""
    url = "https://d1a2b3c4d5e6f7.cloudfront.net/"
    values = redact.values_from_ui_env({**_ui_env(), "UX_TEST_WEB_URL": url})
    assert url in values
    assert "d1a2b3c4d5e6f7.cloudfront.net" in values
    out = redact.Redactor(values)(
        f"opened {url}#/documents, then d1a2b3c4d5e6f7.cloudfront.net refused"
    )
    assert "cloudfront" not in out
    assert "d1a2b3c4d5e6f7" not in out


@pytest.mark.unit
def test_absent_and_empty_ui_env_keys_contribute_nothing() -> None:
    assert redact.values_from_ui_env({}) == []
    assert (
        redact.values_from_ui_env({"VITE_USER_POOL_ID": " ", "UX_TEST_BUCKETS": ","})
        == []
    )


def _top_level_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


@pytest.mark.unit
def test_no_core_module_imports_a_third_party_library_at_module_level() -> None:
    lazy_only = {"boto3", "botocore", "PIL", "yaml"}
    for name in CORE_MODULES:
        imported = _top_level_imports(PACKAGE / name)
        assert not imported & lazy_only, (
            f"scripts/ux_runner/{name} imports {sorted(imported & lazy_only)} at module "
            "level; the precheck runs on a bare python3"
        )
        assert "ai_mr_review" not in imported, (
            f"scripts/ux_runner/{name} imports ai_mr_review; gitlab_api.py is the "
            "single import point"
        )
        stdlib_or_future = imported - {"__future__"}
        assert stdlib_or_future <= sys.stdlib_module_names, (
            f"scripts/ux_runner/{name} imports {sorted(stdlib_or_future)} at module "
            "level, which is not all stdlib"
        )


@pytest.mark.unit
def test_the_package_marker_is_a_docstring_and_nothing_else() -> None:
    tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    assert len(tree.body) == 1
    only = tree.body[0]
    assert isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant)
    assert isinstance(only.value.value, str) and only.value.value.strip()
