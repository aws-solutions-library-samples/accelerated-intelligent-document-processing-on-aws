# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for scripts/ux_runner/artifacts.py, the shared redactor and artifact writes.

The orchestrator's own suite drives these through whole runs; here each piece is
pinned on its own, because every orchestrator of the confined runner now shares
them: what :class:`artifacts.Scrub` keeps exact and what it redacts, which
environment values :func:`artifacts.redactor_for` registers, that no artifact is
written or made through a symlink, the modes, and that both shared modules import
on a bare python3 with nothing outside the standard library.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import artifacts, gitlab_api, redact, section  # noqa: E402

review = gitlab_api.review
ACCOUNT = "123456789012"
DIGIT_HEAD = "ab" + "123456789012" + "cd" * 13
FORGE_HOST = "gitlab.internal.example"
API_HOST = "abcde12345.execute-api.us-east-1.amazonaws.com"
PERSONA_USER = "ux-persona-admin@example.invalid"
PERSONA_CREDENTIAL = "Ux!3cretive-persona-9aZ"
UI_ENV_TEXT = (
    "VITE_AWS_REGION=us-east-1\n"
    "VITE_USER_POOL_ID=us-east-1_AbCdEfGhI\n"
    f"VITE_API_BASE_URL=https://{API_HOST}/api\n"
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _scrub(*values: str) -> artifacts.Scrub:
    return artifacts.Scrub(redact.Redactor(values))


@pytest.mark.unit
def test_kept_strings_pass_exactly_while_everything_around_them_is_redacted() -> None:
    """A SHA can hold a run of twelve digits, which the account shape rewrites."""
    scrub = _scrub()
    marker = f"<!-- ux-test: sha={DIGIT_HEAD} rev=1 upload=none -->"
    assert scrub(marker) != marker
    scrub.keep(DIGIT_HEAD, marker, "")
    assert scrub(f"{marker}\naccount {ACCOUNT}") == f"{marker}\naccount [account]"
    assert scrub(f"head {DIGIT_HEAD}.") == f"head {DIGIT_HEAD}."
    assert scrub(404) == "404"


@pytest.mark.unit
def test_the_longest_kept_string_wins_where_two_overlap() -> None:
    scrub = _scrub("secret-value")
    scrub.keep("keep", "keep-secret-value")
    assert scrub("keep-secret-value and secret-value") == (
        "keep-secret-value and [redacted]"
    )


@pytest.mark.unit
def test_a_note_never_turns_a_placeholder_into_markup() -> None:
    """``Bearer <token>(...)`` would become ``Bearer [token](...)``, a link."""
    scrub = _scrub()
    text = "Bearer AbCdEfGhIjKlMnOpQr(//e.example/x)"
    assert "](" in scrub(text)
    noted = scrub.note(text)
    assert "](" not in noted
    assert (
        noted.replace(section.ZERO_WIDTH_SPACE, "") == "Bearer [token](//e.example/x)"
    )


@pytest.mark.unit
def test_a_note_leaves_no_issue_key_or_www_after_a_placeholder() -> None:
    """Twelve digits before ``ABC-12`` or ``www.`` keep both inert through
    ``sanitize``; the ``[account]`` that replaces them must not undo that."""
    scrub = _scrub()
    text = section.sanitize(f"ref {ACCOUNT}ABC-12 see {ACCOUNT}www.evil.example", 200)
    assert "ABC-12" in text and "www.evil" in text
    noted = scrub.note(text)
    assert "ABC-12" not in noted and "www.evil" not in noted
    assert noted.replace(section.ZERO_WIDTH_SPACE, "") == (
        "ref [account]ABC-12 see [account]www.evil.example"
    )


@pytest.mark.unit
def test_a_field_is_flattened_before_it_is_redacted() -> None:
    """A bidi mark between ``Bearer`` and its token hides the shape until then."""
    scrub = _scrub()
    spaced = "Bearer\u200eAbCdEfGhIjKlMnOpQr"
    assert "AbCdEfGhIjKlMnOpQr" in scrub(spaced)
    assert scrub.field(spaced) == "Bearer [token]"


@pytest.mark.unit
def test_a_tree_has_every_string_leaf_redacted_and_its_keys_left_alone() -> None:
    scrub = _scrub("secret-value")
    value = {
        "secret-value": ["a secret-value", ("secret-value",), 3, None, True],
        "nested": {"spaced": "Bearer\u200eAbCdEfGhIjKlMnOpQr"},
    }
    assert scrub.tree(value) == {
        "secret-value": ["a [redacted]", ["[redacted]"], 3, None, True],
        "nested": {"spaced": "Bearer\u200eAbCdEfGhIjKlMnOpQr"},
    }
    fielded = scrub.tree(value, fields=True)
    assert isinstance(fielded, dict)
    assert fielded["nested"] == {"spaced": "Bearer [token]"}


@pytest.mark.unit
def test_the_redactor_holds_every_value_the_job_can_see() -> None:
    personas = _b64(
        json.dumps(
            {"Admin": {"username": PERSONA_USER, "password": PERSONA_CREDENTIAL}}
        )
    )
    environ = {
        "GITLAB_REVIEW_TOKEN": "review-token-value",
        "CI_JOB_TOKEN": "job-token-value",
        "AWS_SECRET_ACCESS_KEY": "aws-secret-value",
        "AWS_SECURITY_TOKEN": "aws-security-value",
        "UX_TEST_AWS_ACCOUNT_ID": ACCOUNT,
        "CI_SERVER_HOST": FORGE_HOST,
        "CI_API_V4_URL": "https://api.gitlab.internal.example/api/v4",
        "UX_TEST_UI_ENV": _b64(UI_ENV_TEXT),
        "UX_TEST_PERSONAS": personas,
        "UX_TEST_STORIES_SLACK_WEBHOOK_URL": "https://hooks.slack.example/t/a1b2c3",
        "UNRELATED": "unrelated-value",
    }
    redactor = artifacts.redactor_for(environ)
    for name in (
        "GITLAB_REVIEW_TOKEN",
        "CI_JOB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SECURITY_TOKEN",
        "UX_TEST_UI_ENV",
        "UX_TEST_PERSONAS",
        "UX_TEST_STORIES_SLACK_WEBHOOK_URL",
    ):
        assert redactor(f"<{environ[name]}>") == "<[redacted]>", name
    for value in (
        FORGE_HOST,
        "api.gitlab.internal.example",
        API_HOST,
        "us-east-1_AbCdEfGhI",
        PERSONA_USER,
        PERSONA_CREDENTIAL,
    ):
        assert value not in redactor(f"seen {value} here"), value
    assert redactor("unrelated-value us-east-1") == "unrelated-value us-east-1"


@pytest.mark.unit
def test_a_variable_that_does_not_decode_adds_its_raw_value_and_nothing_fails() -> None:
    environ = {"UX_TEST_UI_ENV": "not base64 at all!", "UX_TEST_PERSONAS": "{nope"}
    redactor = artifacts.redactor_for(environ)
    assert redactor("x not base64 at all! y") == "x [redacted] y"
    assert redactor("x {nope y") == "x {nope y"
    assert artifacts.redactor_for({})("plain text") == "plain text"


@pytest.mark.unit
def test_the_environment_names_are_the_audited_ones() -> None:
    assert artifacts.CREDENTIAL_ENV_NAMES == (
        review.GITLAB.credential_env,
        "CI_JOB_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
        "UX_TEST_AWS_ACCOUNT_ID",
        "UX_TEST_STORIES_SLACK_WEBHOOK_URL",
    )
    assert review.GITLAB.credential_env == "GITLAB_REVIEW_TOKEN"
    assert artifacts.FORGE_HOST_ENV_NAMES == ("CI_SERVER_HOST", "CI_SERVER_FQDN")
    assert artifacts.FORGE_URL_ENV_NAMES == ("CI_API_V4_URL", "CI_SERVER_URL")


@pytest.mark.unit
def test_an_artifact_error_is_still_a_review_failure() -> None:
    """Every ``except review.Failure`` in an orchestrator keeps catching it."""
    assert issubclass(artifacts.ArtifactError, review.Failure)


@pytest.mark.unit
def test_the_artifact_directory_is_made_and_a_symlink_or_a_file_refused(
    tmp_path: Path,
) -> None:
    made = artifacts.artifact_dir(tmp_path / "a" / "ux-tests")
    assert made.is_dir() and made == tmp_path / "a" / "ux-tests"
    assert artifacts.artifact_dir(made) == made
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(
        artifacts.ArtifactError,
        match=re.escape(
            f"the artifact directory {link} is a symlink; nothing is written through it"
        ),
    ):
        artifacts.artifact_dir(link)
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    with pytest.raises(
        artifacts.ArtifactError,
        match=re.escape(f"the artifact directory {blocked} cannot be made: "),
    ):
        artifacts.artifact_dir(blocked)
    assert blocked.read_text(encoding="utf-8") == "not a directory"
    assert list(elsewhere.iterdir()) == []


@pytest.mark.unit
def test_an_artifact_is_never_written_through_a_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("untouched", encoding="utf-8")
    (tmp_path / "summary.json").symlink_to(outside)
    with pytest.raises(
        artifacts.ArtifactError, match="^summary.json could not be written: "
    ):
        artifacts.write_artifact(tmp_path / "summary.json", "{}")
    assert outside.read_text(encoding="utf-8") == "untouched"


@pytest.mark.unit
def test_an_artifact_is_truncated_and_made_readable_to_all_and_writable_to_one(
    tmp_path: Path,
) -> None:
    path = tmp_path / "section.md"
    path.write_text("an older and much longer section", encoding="utf-8")
    previous = os.umask(0o022)
    try:
        artifacts.write_artifact(path, "née\n")
        fresh = tmp_path / "fresh.md"
        artifacts.write_artifact(fresh, "x")
    finally:
        os.umask(previous)
    assert path.read_bytes() == "née\n".encode()
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o644


@pytest.mark.unit
def test_the_open_flags_never_follow_a_link_and_always_truncate() -> None:
    expected = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    assert expected == artifacts.ARTIFACT_OPEN_FLAGS


@pytest.mark.unit
def test_json_is_written_redacted_indented_and_with_its_text_as_written(
    tmp_path: Path,
) -> None:
    scrub = _scrub("secret-value")
    value = {"detail": "secret-value in café", "count": 2}
    artifacts.write_json(tmp_path / "summary.json", value, scrub)
    expected = {"detail": "[redacted] in café", "count": 2}
    text = (tmp_path / "summary.json").read_text(encoding="utf-8")
    assert text == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"


@pytest.mark.unit
def test_a_private_directory_is_its_owners_alone_even_when_it_existed(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "agent"
    existing.mkdir(mode=0o755)
    os.chmod(existing, 0o755)
    for path in (existing, tmp_path / "render" / "home"):
        assert artifacts.private_dir(path) == path
        assert stat.S_IMODE(path.stat().st_mode) == 0o700


@pytest.mark.unit
def test_the_shared_modules_import_nothing_outside_the_standard_library() -> None:
    """The precheck imports both on a bare python3, before the toolchain exists."""
    program = "import ux_runner.artifacts, ux_runner.session"
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-B", "-X", "importtime", "-c", program],
        cwd=SCRIPTS,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    imported = re.findall(r"\|\s+(\S+)\s*$", completed.stderr, flags=re.M)
    assert "ux_runner.session" in imported and "ux_runner.artifacts" in imported
    heavy = sorted(
        {m for m in imported if m.split(".")[0] in ("boto3", "botocore", "yaml", "PIL")}
    )
    assert not heavy, f"importing the shared modules imported {heavy}"
