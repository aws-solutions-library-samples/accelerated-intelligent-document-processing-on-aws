# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sign the persona in over CDP before recording, and read what the page reports.

The model never signs in. It is handed a browser that is already signed in as the
persona the prompt names, so the password never reaches the model, its
transcript, the recorder's event log or the video. This module does the sign-in
through the same DevTools session the harness's self-tests use, before the
recorder attaches, and it reads the persona map from ``UX_TEST_PERSONAS``.

Where the password goes, and where it does not
----------------------------------------------
It reaches the page exactly once, as ``Input.insertText`` into a password field
that the harness has just focused and confirmed is focused. It is never part of a
``Runtime.evaluate`` expression, an argv, a log line or an exception: selectors are
embedded in page JavaScript only through ``json.dumps``, every error raised here is
raised ``from None`` with the persona's username and password scrubbed from its
text, and :class:`Persona` leaves both out of its ``repr``. The page itself does
receive it, because it is the login form; that residual is the MR's own code
running in a browser holding a password its author can already read in the CI
variable, and the job's documentation says so.

What "signed in" means
----------------------
Amplify's Authenticator keeps its session in ``localStorage`` under
``CognitoIdentityServiceProvider.<client>.LastAuthUser`` and friends. The sign-in
is complete when no password field remains on the page and such a key exists.
An alert shown before that is a refusal, quoted in the error after its control
characters are blanked and it is cut to 160 characters, since it is text the MR's
page rendered. After sign-in the welcome page is dismissed the way the UI itself
records it, and the page is reloaded once, which clears the blank page a cold
sign-in can leave behind, and the session is confirmed to have survived it.

The selectors are inferred from Amplify UI v6's sign-in form, not measured
against every version of it; a form they do not match fails here, by name,
rather than in the middle of a recording.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from . import chrome, paths

USERNAME_SELECTORS = (
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[type="email"]',
)
PASSWORD_SELECTORS = ('input[name="password"]', 'input[type="password"]')
SUBMIT_SELECTORS = ('button[type="submit"]',)
#: The ``localStorage`` key the UI sets when the welcome page is dismissed
#: (``WELCOME_DISMISSED_KEY`` in ``src/ui/src/routes/constants.ts``).
WELCOME_DISMISSED_KEY = "idp-welcome-dismissed"

#: Where Amplify UI shows a sign-in error.
_ALERT_SELECTORS = ('[role="alert"]', ".amplify-alert")
_OLD_DOCUMENT_FLAG = "__uxRunnerOldDocument"
_POLL_SECONDS = 0.25
_ALERT_LIMIT = 160
_FACT_LIMIT = 120
_URL_LIMIT = 300
_REDACTED = "[redacted]"
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9A-Za-z-]+){1,4}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PERSONAS_SHAPE = "UX_TEST_PERSONAS must map each persona to a username and a password"


class SignInError(Exception):
    """The persona could not be signed in, or ``UX_TEST_PERSONAS`` is malformed."""


@dataclass(frozen=True)
class Persona:
    """One ``UX_TEST_PERSONAS`` entry. The credentials are kept out of ``repr``."""

    name: str
    username: str = field(repr=False)
    password: str = field(repr=False)


def personas_from_env(value: str) -> dict[str, Persona]:
    """Decode ``UX_TEST_PERSONAS``: base64 of ``{name: {username, password}}``.

    Base64 because GitLab's masking alphabet has no ``{``, ``"`` or ``!``, so raw
    JSON could not be a masked variable. No error message names a persona or
    quotes a value.
    """
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise SignInError("UX_TEST_PERSONAS is not valid base64") from None
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SignInError("UX_TEST_PERSONAS is not a JSON object") from None
    if not isinstance(decoded, dict):
        raise SignInError("UX_TEST_PERSONAS is not a JSON object")
    personas: dict[str, Persona] = {}
    for name, entry in decoded.items():
        login = entry.get("username") if isinstance(entry, dict) else None
        credential = entry.get("password") if isinstance(entry, dict) else None
        if not (
            isinstance(login, str)
            and login
            and isinstance(credential, str)
            and credential
        ):
            raise SignInError(_PERSONAS_SHAPE)
        personas[name] = Persona(name=name, username=login, password=credential)
    return personas


def _first_match(selectors: Sequence[str], action: str) -> str:
    """JavaScript that finds the first element matching ``selectors`` and acts.

    ``action`` is a JavaScript expression over ``el`` whose value is returned;
    ``false`` when nothing matches.
    """
    return (
        f"(() => {{ for (const s of {json.dumps(list(selectors))}) {{ "
        "const el = document.querySelector(s); "
        f"if (el) {{ return {action}; }} }} return false; }})()"
    )


def _focus(selectors: Sequence[str]) -> str:
    return _first_match(selectors, "(el.focus(), document.activeElement === el)")


_PASSWORD_QUERY = json.dumps(list(PASSWORD_SELECTORS))
_ALERT_QUERY = json.dumps(", ".join(_ALERT_SELECTORS))
_STATE_JS = (
    "(() => { "
    f"const field = {_PASSWORD_QUERY}"
    ".some((s) => document.querySelector(s) !== null); "
    "let authed = false; "
    "try { for (let i = 0; i < localStorage.length; i += 1) { "
    "const key = localStorage.key(i); "
    "if (key && key.endsWith('.LastAuthUser')) { authed = true; break; } } "
    "} catch (e) { authed = false; } "
    f"const shown = Array.from(document.querySelectorAll({_ALERT_QUERY}))"
    ".map((el) => String(el.textContent || '').trim()).find((t) => t.length > 0); "
    "return { passwordField: field, authed: authed, "
    "alert: shown ? shown.slice(0, 400) : '' }; })()"
)
_MARK_JS = f"(() => {{ window.{_OLD_DOCUMENT_FLAG} = true; return true; }})()"
_LOADED_JS = (
    f"document.readyState === 'complete' && !window.{_OLD_DOCUMENT_FLAG} "
    "&& String(location.href)"
)
_FACTS_JS = (
    "(() => { "
    "const pick = (h) => document.querySelector('a[href=\"#' + h + '\"]') "
    "|| document.getElementById(h); "
    "const text = (h) => { const el = pick(h); "
    f"return el ? String(el.textContent || '').slice(0, {_FACT_LIMIT}) : null; }}; "
    "return { stack: text('stackname'), version: text('version'), "
    "build: text('builddatetime'), w: window.innerWidth, h: window.innerHeight, "
    "dpr: window.devicePixelRatio, "
    f"url: String(location.href).slice(0, {_URL_LIMIT}) }}; }})()"
)


def _scrub(text: str, persona: Persona) -> str:
    for value in sorted((persona.password, persona.username), key=len, reverse=True):
        if value:
            text = text.replace(value, _REDACTED)
    return text


def _page_text(text: str, limit: int) -> str:
    """Page-rendered text made safe to quote: printable, one line, bounded."""
    flat = "".join(char if char.isprintable() else " " for char in text)
    return " ".join(flat.split())[:limit]


def _wait_until_loaded(
    session: chrome.CdpLike,
    prefix: str,
    *,
    deadline: float,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    """Wait for a NEW document whose URL starts with ``prefix`` to finish loading.

    The old document is flagged before every navigation, so a ``readyState`` of
    ``complete`` read from it, before the new one has replaced it, does not count.
    """
    while True:
        href = chrome.evaluate(session, _LOADED_JS)
        if isinstance(href, str) and href.startswith(prefix):
            return
        if clock() >= deadline:
            raise SignInError(f"the sign-in page did not load within {timeout:g} s")
        sleep(_POLL_SECONDS)


def _origin_prefix(url: str) -> str:
    """``scheme://host[:port]/`` of ``url``, as ``location.href`` spells it."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}/"


def _navigate(session: chrome.CdpLike, method: str, params: dict[str, Any]) -> None:
    """Flag the current document as old, then navigate or reload.

    Chrome's own ``errorText`` (``net::ERR_...``) is quoted when there is one; it
    is Chrome's text, not the page's.
    """
    chrome.evaluate(session, _MARK_JS)
    reply = session.call(method, params)
    error = reply.get("errorText") if isinstance(reply, dict) else None
    if error:
        raise SignInError(
            f"the sign-in page could not be loaded: {_page_text(str(error), 80)}"
        )


def _state(session: chrome.CdpLike) -> dict[str, Any]:
    state = chrome.evaluate(session, _STATE_JS)
    return state if isinstance(state, dict) else {}


def _signed_in(state: dict[str, Any]) -> bool:
    return state.get("authed") is True and state.get("passwordField") is False


def _sign_in(
    session: chrome.CdpLike,
    persona: Persona,
    *,
    base_url: str,
    timeout: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> None:
    deadline = clock() + timeout
    prefix = _origin_prefix(base_url)
    session.call("Page.enable")
    session.call("Runtime.enable")
    _navigate(session, "Page.navigate", {"url": base_url})
    _wait_until_loaded(
        session, prefix, deadline=deadline, timeout=timeout, sleep=sleep, clock=clock
    )
    while (
        chrome.evaluate(session, _first_match(USERNAME_SELECTORS, "true")) is not True
    ):
        if clock() >= deadline:
            raise SignInError(f"the sign-in form did not appear within {timeout:g} s")
        sleep(_POLL_SECONDS)
    if chrome.evaluate(session, _focus(USERNAME_SELECTORS)) is not True:
        raise SignInError("the sign-in form's username field could not be focused")
    session.call("Input.insertText", {"text": persona.username})
    if chrome.evaluate(session, _focus(PASSWORD_SELECTORS)) is not True:
        raise SignInError("the sign-in form's password field could not be focused")
    session.call("Input.insertText", {"text": persona.password})
    if (
        chrome.evaluate(session, _first_match(SUBMIT_SELECTORS, "(el.click(), true)"))
        is not True
    ):
        raise SignInError("the sign-in form has no submit button")
    while True:
        state = _state(session)
        if _signed_in(state):
            break
        alert = state.get("alert")
        if isinstance(alert, str) and alert.strip():
            shown = _page_text(_scrub(alert, persona), _ALERT_LIMIT)
            raise SignInError(f"sign-in was refused: {shown}")
        if clock() >= deadline:
            raise SignInError(
                f"the signed-in state was not reached within {timeout:g} s"
            )
        sleep(_POLL_SECONDS)
    chrome.evaluate(
        session, f"localStorage.setItem({json.dumps(WELCOME_DISMISSED_KEY)}, 'true')"
    )
    _navigate(session, "Page.reload", {"ignoreCache": True})
    _wait_until_loaded(
        session, prefix, deadline=deadline, timeout=timeout, sleep=sleep, clock=clock
    )
    if not _signed_in(_state(session)):
        raise SignInError("the session did not survive the reload after sign-in")


def sign_in(
    session: chrome.CdpLike,
    persona: Persona,
    *,
    base_url: str = paths.UI_ORIGIN + "/",
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Sign ``persona`` in at ``base_url`` within ``timeout`` seconds, or raise.

    Every failure is a :class:`SignInError` whose text carries neither the
    username nor the password, whatever the page or DevTools put in it.
    """
    try:
        _sign_in(
            session,
            persona,
            base_url=base_url,
            timeout=timeout,
            sleep=sleep,
            clock=clock,
        )
    except SignInError as exc:
        raise SignInError(_scrub(str(exc), persona)) from None
    except Exception as exc:
        detail = _page_text(_scrub(str(exc), persona), _ALERT_LIMIT)
        raise SignInError(f"sign-in failed: {type(exc).__name__}: {detail}") from None


@dataclass(frozen=True)
class PageFacts:
    """What the signed-in page reports about the stack and the viewport.

    The MR's own UI renders the stack name, version and build date, so callers
    label them "as the UI reports it" rather than as facts about the stack.
    """

    stack_name: str | None
    version: str | None
    build: str | None
    viewport: tuple[int, int, float] | None
    url: str | None


def _labelled(value: Any, prefix: str) -> str | None:
    """A sidebar item's text without its ``prefix``, bounded; ``None`` when empty."""
    if not isinstance(value, str):
        return None
    text = _page_text(value, _FACT_LIMIT * 2)
    if text.startswith(prefix):
        text = text[len(prefix) :].strip()
    return text[:_FACT_LIMIT] or None


def _version(value: Any) -> str | None:
    text = _labelled(value, "Version:")
    token = text.split()[0] if text else ""
    return token if _VERSION_RE.fullmatch(token) else None


def _build_date(value: Any) -> str | None:
    text = _labelled(value, "Build:") or ""
    found = _DATE_RE.search(text)
    if found is None:
        return None
    try:
        return date.fromisoformat(found.group(0)).isoformat()
    except ValueError:
        return None


def _viewport(width: Any, height: Any, ratio: Any) -> tuple[int, int, float] | None:
    numbers = (width, height, ratio)
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in numbers):
        return None
    if not all(math.isfinite(n) for n in numbers):
        return None
    return int(width), int(height), float(ratio)


def page_facts(session: chrome.CdpLike) -> PageFacts:
    """Stack name, version, build date, viewport and URL, in one evaluation.

    The sidebar's ``Stack Name:``, ``Version:`` and ``Build:`` items are read by
    their ``#stackname``, ``#version`` and ``#builddatetime`` links. A version
    that does not have a version's shape and a build without a date read as
    ``None``.
    """
    value = chrome.evaluate(session, _FACTS_JS)
    facts = value if isinstance(value, dict) else {}
    url = facts.get("url")
    shown_url = _page_text(url, _URL_LIMIT) if isinstance(url, str) else ""
    return PageFacts(
        stack_name=_labelled(facts.get("stack"), "Stack Name:"),
        version=_version(facts.get("version")),
        build=_build_date(facts.get("build")),
        viewport=_viewport(facts.get("w"), facts.get("h"), facts.get("dpr")),
        url=shown_url or None,
    )
