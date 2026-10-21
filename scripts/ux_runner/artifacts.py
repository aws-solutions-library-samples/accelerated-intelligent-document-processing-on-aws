# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The redactor every printed line and artifact goes through, and the artifact writes.

Shared by every orchestrator of the confined runner, so each one redacts what it
prints and writes, and refuses a symlinked artifact, in the same way.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import IO

import ux_test_proxy

from . import egress, gitlab_api, redact, section, signin

review = gitlab_api.review

#: ``os.open`` flags for an artifact: truncate, never through a symlink, and
#: binary where the platform distinguishes it.
ARTIFACT_OPEN_FLAGS: int = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_TRUNC
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_BINARY", 0)
)
#: Environment values that are credentials or identify the account, redacted
#: wherever they appear.
CREDENTIAL_ENV_NAMES: tuple[str, ...] = (
    review.GITLAB.credential_env,
    "CI_JOB_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "UX_TEST_AWS_ACCOUNT_ID",
)
#: Environment values naming the GitLab instance, whose host may resolve only on
#: an internal network: host names as they are, URLs by their host.
FORGE_HOST_ENV_NAMES: tuple[str, ...] = ("CI_SERVER_HOST", "CI_SERVER_FQDN")
FORGE_URL_ENV_NAMES: tuple[str, ...] = ("CI_API_V4_URL", "CI_SERVER_URL")


class ArtifactError(review.Failure):
    """An artifact directory or file that cannot be used; still a review.Failure."""


class Scrub:
    """The one redactor every printed line and every artifact goes through.

    ``redact.Redactor`` replaces any run of exactly twelve digits with
    ``[account]``, and a 40-character hex SHA, an upload's secret or an upload id
    can hold such a run: measured over random SHAs, about one in sixty does.
    Redacting the marker ``<!-- ux-test: sha=... -->`` in that case would break the
    idempotency it keys, and redacting the video line would break the video. So
    :meth:`keep` names exact strings the harness itself computed (the SHAs, the
    marker line and the video line), which pass through unchanged while everything
    around them is redacted. The code review's own body, which a post appends to,
    is never passed through here: it is the review's text, not this job's.
    """

    def __init__(self, redactor: redact.Redactor) -> None:
        self.redactor = redactor
        self._exact: list[str] = []
        self._split: re.Pattern[str] | None = None

    def keep(self, *values: str) -> None:
        """Pass ``values`` through unredacted wherever they occur."""
        for value in values:
            if value and value not in self._exact:
                self._exact.append(value)
        ordered = sorted(self._exact, key=len, reverse=True)
        alternation = "|".join(re.escape(value) for value in ordered)
        self._split = re.compile(f"({alternation})") if ordered else None

    def __call__(self, text: object) -> str:
        """``text`` redacted, apart from the kept strings."""
        return self._apply(text, self.redactor)

    def note(self, text: object) -> str:
        """``text`` redacted for a GitLab note, where no placeholder becomes markup.

        A placeholder brings brackets in, and redacting text that was sanitised
        first can put one right before the ``(`` of a link: ``Bearer [token](...)``,
        or its closing ``]`` right before an uppercase issue key or a ``www.`` that
        a letter or digit had kept inert: ``[account]www.example``. So each
        redacted piece also goes through ``section.inert_markup``, which breaks
        those as ``sanitize`` breaks the model's own. The kept strings, the marker
        and the video line among them, pass unchanged.
        """
        return self._apply(
            text, lambda piece: section.inert_markup(self.redactor(piece))
        )

    def field(self, value: str) -> str:
        """One model-written field, redacted as ``sanitize`` will read it.

        ``sanitize`` first turns control and bidi characters into spaces, which
        can make a shape such as ``Bearer`` and its token appear only then. So the
        field is flattened the same way (``section.flatten``) before it is
        redacted, and the redaction happens before the field is cut to its bound.
        """
        return self(section.flatten(value))

    def tree(self, value: object, *, fields: bool = False) -> object:
        """A JSON-like value with every string leaf redacted; keys are left alone.

        With ``fields``, each leaf is redacted as a model-written field
        (:meth:`field`).
        """
        if isinstance(value, str):
            return self.field(value) if fields else self(value)
        if isinstance(value, dict):
            return {key: self.tree(item, fields=fields) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.tree(item, fields=fields) for item in value]
        return value

    def _apply(self, text: object, scrub: Callable[[str], str]) -> str:
        shown = text if isinstance(text, str) else str(text)
        if self._split is None:
            return scrub(shown)
        pieces = self._split.split(shown)
        return "".join(
            piece if index % 2 else scrub(piece) for index, piece in enumerate(pieces)
        )


def _forge_hosts(environ: Mapping[str, str]) -> list[str]:
    """The GitLab instance's host names, from GitLab CI's own variables."""
    hosts = [environ.get(name, "") for name in FORGE_HOST_ENV_NAMES]
    for name in FORGE_URL_ENV_NAMES:
        with contextlib.suppress(ValueError):
            hosts.append(urllib.parse.urlsplit(environ.get(name, "")).hostname or "")
    return [host for host in hosts if host.strip()]


def redactor_for(environ: Mapping[str, str]) -> redact.Redactor:
    """A redactor holding every secret and stack identifier this job can see.

    The token and the AWS key material, the expected account id, the UI
    environment's identifying values and every persona's username and password,
    plus the raw base64 of both variables, and the GitLab instance's host, which
    ``inputs.json``'s web URL and an API error would otherwise carry. A variable
    that does not decode adds only its raw value: the precheck reports it, and the
    redactor must not fail first.
    """
    redactor = redact.Redactor()
    for name in CREDENTIAL_ENV_NAMES:
        value = environ.get(name, "")
        if value.strip():
            redactor.add(value)
    redactor.add(*_forge_hosts(environ))
    ui_text = environ.get("UX_TEST_UI_ENV", "")
    if ui_text.strip():
        redactor.add(ui_text)
        with contextlib.suppress(ux_test_proxy.EgressConfigError):
            redactor.add(*redact.values_from_ui_env(egress.ui_env_from_b64(ui_text)))
    persona_text = environ.get("UX_TEST_PERSONAS", "")
    if persona_text.strip():
        redactor.add(persona_text)
        with contextlib.suppress(signin.SignInError):
            for persona in signin.personas_from_env(persona_text).values():
                redactor.add(persona.username, persona.password)
    return redactor


def artifact_dir(path: Path) -> Path:
    """The artifact directory, made when absent; a symlink in its place is refused.

    In CI it sits in the MR's checkout and the harness writes into it as root, so
    a directory the MR committed as a symlink would decide where those writes
    land, and the job reads its gating marker back from it. The job replaces
    whatever the checkout holds there with a fresh directory first; this is the
    harness's own check, for every caller, and :func:`open_artifact` refuses a
    symlink inside it.
    """
    if path.is_symlink():
        raise ArtifactError(
            f"the artifact directory {path} is a symlink; nothing is written through it"
        )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise ArtifactError(
            f"the artifact directory {path} cannot be made: {reason}"
        ) from None
    if path.is_symlink() or not path.is_dir():
        raise ArtifactError(f"the artifact directory {path} is not a directory")
    return path


def open_artifact(path: Path) -> IO[bytes]:
    """Open one artifact for writing, refusing a symlink in its place.

    ``O_NOFOLLOW`` makes the open itself refuse, so there is no window between a
    check and the write. Neither flag exists on Windows, where ``--collect-only``
    runs on the host; there the file is simply opened.
    """
    try:
        descriptor = os.open(path, ARTIFACT_OPEN_FLAGS, 0o644)
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise ArtifactError(f"{path.name} could not be written: {reason}") from None
    return os.fdopen(descriptor, "wb")


def write_artifact(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8, through :func:`open_artifact`."""
    with open_artifact(path) as handle:
        handle.write(text.encode("utf-8"))


def write_json(path: Path, value: object, scrub: Scrub) -> None:
    """Write ``value`` redacted by ``scrub``, as indented JSON and a newline."""
    text = json.dumps(scrub.tree(value), indent=2, ensure_ascii=False)
    write_artifact(path, text + "\n")


def private_dir(path: Path) -> Path:
    """``path``, made with its parents when absent, then set to mode ``0o700``."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path
