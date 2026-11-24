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
records it, unless the caller keeps it for a story about a first visit, and the
page is reloaded once, which clears the blank page a cold sign-in can leave
behind, and the session is confirmed to have survived it.

The selectors are inferred from Amplify UI v6's sign-in form, not measured
against every version of it; a form they do not match fails here, by name,
rather than in the middle of a recording.

Switching the UI's language
---------------------------
A screen review needs the signed-in UI in another locale, and the model cannot
run page JavaScript, so :func:`set_locale` does it the way the UI records a
user's choice: it writes the locale under ``idp-locale`` in ``localStorage`` and
reloads. The locale is one of :data:`REVIEW_LOCALES`, a constant, never text a
model or a page chose. The page counts as switched only once ``<html lang>``
names the locale and the sidebar's stack-name link no longer starts with its
English label, because the UI sets ``lang`` at once while the catalog loads in
the background, shows English until it arrives, and keeps English when it fails
to load. It runs after :func:`page_facts` has read the sidebar, whose labels it
strips only in English.
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
#: The ``localStorage`` key the UI keeps the user's locale under
#: (``LOCALE_STORAGE_KEY`` in ``src/ui/src/i18n/locales.ts``).
LOCALE_STORAGE_KEY = "idp-locale"
#: The locales :func:`set_locale` switches to: the one being translated, and the
#: pseudo-locale that shows text bypassing the catalog. The UI offers a locale
#: only when its build holds that catalog.
REVIEW_LOCALES: tuple[str, ...] = ("fr-CA", "en-XA")
#: The sidebar's Deployment Info items :func:`page_facts` reads, by fact: the
#: ``href`` of each one's link and the label its text starts with.
#: ``src/ui/src/components/genaiidp-layout/navigation.tsx`` renders each as a link
#: whose whole text is the label and the value, ``Version: 0.6.12``.
DEPLOYMENT_INFO_LINKS: dict[str, tuple[str, str]] = {
    "stack": ("#stackname", "Stack Name:"),
    "version": ("#version", "Version:"),
    "build": ("#builddatetime", "Build:"),
}

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
    "const text = (href) => { "
    "const el = document.querySelector('a[href=\"' + href + '\"]'); "
    f"return el ? String(el.textContent || '').slice(0, {_FACT_LIMIT}) : null; }}; "
    "return { "
    + "".join(
        f"{fact}: text({json.dumps(href)}), "
        for fact, (href, _label) in DEPLOYMENT_INFO_LINKS.items()
    )
    + "w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio, "
    f"url: String(location.href).slice(0, {_URL_LIMIT}) }}; }})()"
)
_STACK_LINK_QUERY = json.dumps('a[href="' + DEPLOYMENT_INFO_LINKS["stack"][0] + '"]')
_LOCALE_JS = (
    "(() => { "
    f"const el = document.querySelector({_STACK_LINK_QUERY}); "
    "return { "
    f"lang: String(document.documentElement.lang || '').slice(0, {_FACT_LIMIT}), "
    f"label: el ? String(el.textContent || '').slice(0, {_FACT_LIMIT}) : null }}; }})()"
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
    dismiss_welcome: bool,
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
    if dismiss_welcome:
        chrome.evaluate(
            session,
            f"localStorage.setItem({json.dumps(WELCOME_DISMISSED_KEY)}, 'true')",
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
    dismiss_welcome: bool = True,
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Sign ``persona`` in at ``base_url`` within ``timeout`` seconds, or raise.

    Every failure is a :class:`SignInError` whose text carries neither the
    username nor the password, whatever the page or DevTools put in it. With
    ``dismiss_welcome`` false the welcome page is kept for a story about a first
    visit: nothing records it as dismissed, and a fresh Chrome profile, which
    every flow and every browser container starts with, never has, so the UI
    lands on it. The reload after sign-in happens either way.
    """
    try:
        _sign_in(
            session,
            persona,
            base_url=base_url,
            dismiss_welcome=dismiss_welcome,
            timeout=timeout,
            sleep=sleep,
            clock=clock,
        )
    except SignInError as exc:
        raise SignInError(_scrub(str(exc), persona)) from None
    except Exception as exc:
        detail = _page_text(_scrub(str(exc), persona), _ALERT_LIMIT)
        raise SignInError(f"sign-in failed: {type(exc).__name__}: {detail}") from None


def _locale_problem(value: Any, locale: str) -> str:
    """Why the page does not show ``locale`` yet, or ``""`` once it does.

    Each reason is fixed text: the page's values are compared, never quoted.
    """
    shown = value if isinstance(value, dict) else {}
    if shown.get("lang") != locale:
        return f"<html lang> is not {locale}"
    label = shown.get("label")
    if not isinstance(label, str):
        return "the sidebar shows no stack name"
    if _page_text(label, _FACT_LIMIT).startswith(DEPLOYMENT_INFO_LINKS["stack"][1]):
        return "the sidebar still reads in English"
    return ""


def _set_locale(
    session: chrome.CdpLike,
    locale: str,
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
    chrome.evaluate(
        session,
        f"localStorage.setItem({json.dumps(LOCALE_STORAGE_KEY)}, {json.dumps(locale)})",
    )
    _navigate(session, "Page.reload", {"ignoreCache": True})
    _wait_until_loaded(
        session, prefix, deadline=deadline, timeout=timeout, sleep=sleep, clock=clock
    )
    if not _signed_in(_state(session)):
        raise SignInError(
            f"the session did not survive the reload that switched the UI to {locale}"
        )
    while True:
        problem = _locale_problem(chrome.evaluate(session, _LOCALE_JS), locale)
        if not problem:
            return
        if clock() >= deadline:
            raise SignInError(
                f"the UI did not switch to {locale} within {timeout:g} s ({problem}); "
                f"is {locale} in this build?"
            )
        sleep(_POLL_SECONDS)


def set_locale(
    session: chrome.CdpLike,
    locale: str,
    *,
    base_url: str = paths.UI_ORIGIN + "/",
    timeout: float = 60.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Switch the signed-in UI at ``base_url`` to ``locale``, or raise in ``timeout`` s.

    ``locale`` must be one of :data:`REVIEW_LOCALES`; anything else is refused
    before the session is used. The session may be a fresh one on the signed-in
    page, so both domains are enabled first. The locale is written where the UI
    keeps the user's choice and the page is reloaded; the session must survive
    the reload, and then ``<html lang>`` must name the locale and the sidebar's
    stack name must have left its English label, which is when the catalog has
    arrived (see the module docstring). Every failure is a :class:`SignInError`;
    Chrome's or the page's own words in it are cut to 160 printable characters.
    """
    if locale not in REVIEW_LOCALES:
        raise SignInError(f"the UI locale must be one of {', '.join(REVIEW_LOCALES)}")
    try:
        _set_locale(
            session,
            locale,
            base_url=base_url,
            timeout=timeout,
            sleep=sleep,
            clock=clock,
        )
    except SignInError:
        raise
    except Exception as exc:
        detail = _page_text(str(exc), _ALERT_LIMIT)
        raise SignInError(
            f"switching the UI to {locale} failed: {type(exc).__name__}: {detail}"
        ) from None


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
    text = _labelled(value, DEPLOYMENT_INFO_LINKS["version"][1])
    token = text.split()[0] if text else ""
    return token if _VERSION_RE.fullmatch(token) else None


def _build_date(value: Any) -> str | None:
    text = _labelled(value, DEPLOYMENT_INFO_LINKS["build"][1]) or ""
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


def page_facts(
    session: chrome.CdpLike,
    *,
    timeout: float = 30.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> PageFacts:
    """Stack name, version, build date, viewport and URL, once the sidebar has them.

    The sidebar's Deployment Info items are read by their links
    (:data:`DEPLOYMENT_INFO_LINKS`). The UI renders no sidebar at all until its
    settings, which carry those three, have arrived from SSM after the sign-in, so
    straight after the reload the page shows only a spinner. The page is read
    again every quarter second until one of the three links is there or
    ``timeout`` seconds have passed, and then taken as it is: a fact the page
    does not show reads as ``None``, and so do a version without a version's
    shape and a build without a date.
    """
    deadline = clock() + timeout
    while True:
        value = chrome.evaluate(session, _FACTS_JS)
        facts = value if isinstance(value, dict) else {}
        shown = any(facts.get(fact) is not None for fact in DEPLOYMENT_INFO_LINKS)
        if shown or clock() >= deadline:
            break
        sleep(_POLL_SECONDS)
    url = facts.get("url")
    shown_url = _page_text(url, _URL_LIMIT) if isinstance(url, str) else ""
    return PageFacts(
        stack_name=_labelled(facts.get("stack"), DEPLOYMENT_INFO_LINKS["stack"][1]),
        version=_version(facts.get("version")),
        build=_build_date(facts.get("build")),
        viewport=_viewport(facts.get("w"), facts.get("h"), facts.get("dpr")),
        url=shown_url or None,
    )
