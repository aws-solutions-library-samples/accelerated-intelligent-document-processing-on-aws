# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the CDP sign-in and the page facts, ``ux_runner/signin.py``.

The sign-in runs against a scripted fake of the DevTools session: a small state
machine that plays an Amplify sign-in page (a form until it is submitted, then a
signed-in app, with ``localStorage`` and a reload) and records every call. That
call log is what pins the module's central property: the password reaches the
page in exactly one call, ``Input.insertText``, and never in page JavaScript or in
any error, whatever the page throws back. ``UX_TEST_PERSONAS`` decoding and the
sidebar facts are covered alongside, with every message checked for values.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ux_runner import signin  # noqa: E402

SAMPLE_LOGIN = "ux-persona-admin@example.invalid"
SAMPLE_CREDENTIAL = "Ux!Zq7-sample-Kd29-9aZ"
EMPTY = ""
STORAGE_PREFIX = "CognitoIdentityServiceProvider.1a2b3c4d5e6f7g8h9i0j1k2l3m"


def _persona() -> signin.Persona:
    return signin.Persona("Admin", SAMPLE_LOGIN, SAMPLE_CREDENTIAL)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeLoginPage:
    """An Amplify sign-in page as the DevTools session sees it.

    ``accept`` decides whether a submit signs in; ``alert`` is what the page shows
    on a refused one; ``loading_polls`` is how many readiness checks still see the
    old document after a navigation; ``throw_on`` makes one kind of evaluation
    raise in the page with the given description.
    """

    def __init__(
        self,
        *,
        accept: bool = True,
        alert: str = "",
        loading_polls: int = 2,
        form_after_polls: int = 1,
        survives_reload: bool = True,
        throw_on: tuple[str, str] | None = None,
        navigate_error: str = "",
        unfocusable: str | None = None,
        submit_button: bool = True,
    ) -> None:
        self.accept = accept
        self.alert = alert
        self.loading_polls = loading_polls
        self.form_after_polls = form_after_polls
        self.survives_reload = survives_reload
        self.throw_on = throw_on
        self.navigate_error = navigate_error
        self.unfocusable = unfocusable
        self.submit_button = submit_button
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.href = "http://ux-proxy-selftest.invalid/"
        self.old_href = self.href
        self.flagged = False
        self.old_flagged = False
        self.loading = 0
        self.form_polls = 0
        self.focused: str | None = None
        self.typed: list[tuple[str | None, str]] = []
        self.submitted = False
        self.storage: dict[str, str] = {}

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        params = params or {}
        self.calls.append((method, params))
        if method == "Runtime.evaluate":
            kind = self._kind(params["expression"])
            if self.throw_on is not None and self.throw_on[0] == kind:
                details = {
                    "text": "Uncaught",
                    "exception": {"description": self.throw_on[1]},
                }
                return {"exceptionDetails": details}
            return {"result": {"value": self._evaluate(kind, params["expression"])}}
        if method == "Page.navigate":
            if self.navigate_error:
                return {"frameId": "F", "errorText": self.navigate_error}
            self._load(params["url"] + "#/")
            return {"frameId": "F"}
        if method == "Page.reload":
            if not self.survives_reload:
                self.storage.clear()
                self.submitted = False
            self._load(self.href)
            return {}
        if method == "Input.insertText":
            self.typed.append((self.focused, params["text"]))
        return {}

    def _load(self, url: str) -> None:
        """A navigation: the old document answers for a while, then the new one.

        The new document never carries the old one's flag, so a caller that did
        not flag the old document would read it as already loaded.
        """
        self.old_href, self.old_flagged = self.href, self.flagged
        self.href, self.flagged = url, False
        self.loading = self.loading_polls
        self.form_polls = 0

    def _signed_in(self) -> bool:
        return any(key.endswith(".LastAuthUser") for key in self.storage)

    def _kind(self, expression: str) -> str:
        usernames = json.dumps(list(signin.USERNAME_SELECTORS))
        passwords = json.dumps(list(signin.PASSWORD_SELECTORS))
        if "document.readyState" in expression:
            return "loaded"
        if "__uxRunnerOldDocument = true" in expression:
            return "mark"
        if "LastAuthUser" in expression:
            return "state"
        if "localStorage.setItem" in expression:
            return "dismiss"
        if "builddatetime" in expression:
            return "facts"
        if "el.click()" in expression:
            return "submit"
        if "el.focus()" in expression and usernames in expression:
            return "focus-username"
        if "el.focus()" in expression and passwords in expression:
            return "focus-password"
        if usernames in expression:
            return "has-username"
        raise AssertionError(f"unexpected expression: {expression}")

    def _evaluate(self, kind: str, expression: str) -> Any:
        if kind == "mark":
            if self.loading > 0:
                self.old_flagged = True
            else:
                self.flagged = True
            return True
        if kind == "loaded":
            if self.loading > 0:
                self.loading -= 1
                return False if self.old_flagged else self.old_href
            return False if self.flagged else self.href
        form = not self._signed_in()
        if kind == "has-username":
            self.form_polls += 1
            return form and self.form_polls > self.form_after_polls
        if kind in ("focus-username", "focus-password"):
            field_name = kind.removeprefix("focus-")
            if field_name == self.unfocusable:
                return False
            self.focused = field_name if form else None
            return form
        if kind == "submit":
            if not self.submit_button:
                return False
            self.submitted = True
            if self.accept:
                self.storage[f"{STORAGE_PREFIX}.LastAuthUser"] = "user-id"
            return True
        if kind == "state":
            refused = self.submitted and not self.accept
            return {
                "passwordField": form,
                "authed": self._signed_in(),
                "alert": self.alert if refused else "",
            }
        if kind == "dismiss":
            self.storage[signin.WELCOME_DISMISSED_KEY] = "true"
            return None
        raise AssertionError(kind)

    def kinds(self) -> list[str]:
        """Each call as a short name: the method, or the evaluation's kind."""
        return [
            self._kind(params["expression"]) if method == "Runtime.evaluate" else method
            for method, params in self.calls
        ]


def _sign_in(page: FakeLoginPage, timeout: float = 5.0) -> None:
    fake = FakeTime()
    signin.sign_in(
        page, _persona(), timeout=timeout, sleep=fake.sleep, clock=fake.clock
    )


@pytest.mark.unit
def test_the_password_reaches_the_page_exactly_once_through_insert_text() -> None:
    page = FakeLoginPage()
    _sign_in(page)
    carrying = [
        method
        for method, params in page.calls
        if SAMPLE_CREDENTIAL in json.dumps(params)
    ]
    assert carrying == ["Input.insertText"]
    for method, params in page.calls:
        if method == "Runtime.evaluate":
            assert SAMPLE_CREDENTIAL not in params["expression"]
            assert SAMPLE_LOGIN not in params["expression"]
    assert page.typed == [("username", SAMPLE_LOGIN), ("password", SAMPLE_CREDENTIAL)]


@pytest.mark.unit
def test_each_credential_is_typed_right_after_its_field_is_focused() -> None:
    page = FakeLoginPage()
    _sign_in(page)
    kinds = page.kinds()
    inserts = [i for i, kind in enumerate(kinds) if kind == "Input.insertText"]
    assert [kinds[i - 1] for i in inserts] == ["focus-username", "focus-password"]
    assert kinds.index("submit") > inserts[-1]


@pytest.mark.unit
def test_the_welcome_page_is_dismissed_and_then_the_page_is_reloaded_once() -> None:
    page = FakeLoginPage()
    _sign_in(page)
    kinds = page.kinds()
    assert kinds.count("Page.reload") == 1
    assert kinds.index("dismiss") < kinds.index("Page.reload")
    reload_params = page.calls[kinds.index("Page.reload")][1]
    assert reload_params == {"ignoreCache": True}
    assert page.storage[signin.WELCOME_DISMISSED_KEY] == "true"
    assert kinds[-1] == "state"


@pytest.mark.unit
def test_the_old_document_is_flagged_before_every_navigation_and_reload() -> None:
    page = FakeLoginPage(loading_polls=3)
    _sign_in(page)
    kinds = page.kinds()
    assert kinds[:2] == ["Page.enable", "Runtime.enable"]
    for index, kind in enumerate(kinds):
        if kind in ("Page.navigate", "Page.reload"):
            assert kinds[index - 1] == "mark"
    assert page.calls[kinds.index("Page.navigate")][1] == {
        "url": "http://localhost:3000/"
    }


@pytest.mark.unit
def test_the_reload_is_waited_out_until_the_new_document_has_loaded() -> None:
    page = FakeLoginPage(loading_polls=3)
    _sign_in(page)
    kinds = page.kinds()
    after_reload = kinds[kinds.index("Page.reload") :]
    assert after_reload.count("loaded") == 4
    assert page.loading == 0


@pytest.mark.unit
def test_a_refused_sign_in_quotes_the_alert_bounded_printable_and_scrubbed() -> None:
    alert = (
        f"Incorrect username or password for {SAMPLE_LOGIN}\x07\n\t"
        + "x" * 300
        + SAMPLE_CREDENTIAL
    )
    with pytest.raises(signin.SignInError) as raised:
        _sign_in(FakeLoginPage(accept=False, alert=alert))
    message = str(raised.value)
    prefix = "sign-in was refused: "
    assert message.startswith(prefix + "Incorrect username or password for [redacted]")
    assert len(message) - len(prefix) <= 160
    assert message.isprintable()
    assert SAMPLE_LOGIN not in message and SAMPLE_CREDENTIAL not in message
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.unit
def test_sign_in_times_out_when_the_signed_in_state_never_arrives() -> None:
    with pytest.raises(
        signin.SignInError, match="^the signed-in state was not reached within 5 s$"
    ):
        _sign_in(FakeLoginPage(accept=False))


@pytest.mark.unit
def test_sign_in_times_out_when_the_form_never_appears() -> None:
    with pytest.raises(signin.SignInError, match="form did not appear within 5 s"):
        _sign_in(FakeLoginPage(form_after_polls=10**6))


@pytest.mark.unit
def test_an_exception_thrown_by_the_page_never_carries_the_credentials_out() -> None:
    page = FakeLoginPage(throw_on=("submit", f"Error: captured {SAMPLE_CREDENTIAL}"))
    with pytest.raises(signin.SignInError) as raised:
        _sign_in(page)
    message = str(raised.value)
    assert message.startswith("sign-in failed: ChromeError: the page script failed")
    assert "[redacted]" in message
    assert SAMPLE_CREDENTIAL not in message
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.unit
def test_a_navigation_chrome_refuses_is_reported_in_chromes_own_words() -> None:
    page = FakeLoginPage(navigate_error="net::ERR_TUNNEL_CONNECTION_FAILED")
    with pytest.raises(signin.SignInError, match="ERR_TUNNEL_CONNECTION_FAILED"):
        _sign_in(page)


@pytest.mark.unit
def test_a_page_that_never_finishes_loading_times_out() -> None:
    with pytest.raises(signin.SignInError, match="page did not load within 5 s"):
        _sign_in(FakeLoginPage(loading_polls=10**6))


@pytest.mark.unit
@pytest.mark.parametrize(
    ("field_name", "typed"),
    [("username", []), ("password", [("username", SAMPLE_LOGIN)])],
)
def test_a_field_that_cannot_be_focused_stops_before_anything_is_typed_into_it(
    field_name: str, typed: list[tuple[str, str]]
) -> None:
    page = FakeLoginPage(unfocusable=field_name)
    with pytest.raises(signin.SignInError, match=f"{field_name} field could not be"):
        _sign_in(page)
    assert page.typed == typed
    assert "submit" not in page.kinds()


@pytest.mark.unit
def test_a_form_without_a_submit_button_fails_by_name() -> None:
    with pytest.raises(signin.SignInError, match="has no submit button"):
        _sign_in(FakeLoginPage(submit_button=False))


@pytest.mark.unit
def test_a_session_lost_on_the_reload_fails_the_sign_in() -> None:
    with pytest.raises(signin.SignInError, match="did not survive the reload"):
        _sign_in(FakeLoginPage(survives_reload=False))


@pytest.mark.unit
def test_a_persona_never_shows_its_credentials_in_its_repr() -> None:
    shown = repr(_persona())
    assert "Admin" in shown
    assert SAMPLE_LOGIN not in shown and SAMPLE_CREDENTIAL not in shown


def _encoded(document: Any) -> str:
    raw = document if isinstance(document, bytes) else json.dumps(document).encode()
    return base64.b64encode(raw).decode()


@pytest.mark.unit
def test_personas_decode_from_base64_json_keyed_by_group() -> None:
    entry = {"username": SAMPLE_LOGIN, "password": SAMPLE_CREDENTIAL}
    personas = signin.personas_from_env(_encoded({"Admin": entry}) + "\n")
    assert personas == {"Admin": _persona()}
    assert personas["Admin"].password == SAMPLE_CREDENTIAL


@pytest.mark.unit
@pytest.mark.parametrize(
    ("value", "message"),
    [
        (f"{SAMPLE_LOGIN}:{SAMPLE_CREDENTIAL}", "not valid base64"),
        (_encoded(f"{SAMPLE_LOGIN} {SAMPLE_CREDENTIAL}".encode()), "not a JSON object"),
        (_encoded(b"\xff" + SAMPLE_CREDENTIAL.encode()), "not a JSON object"),
        (_encoded([SAMPLE_LOGIN, SAMPLE_CREDENTIAL]), "not a JSON object"),
        (_encoded({"Admin": {"username": SAMPLE_LOGIN}}), "username and a password"),
        (_encoded({"Admin": SAMPLE_CREDENTIAL}), "username and a password"),
        (
            _encoded({"Admin": {"username": SAMPLE_LOGIN, "password": EMPTY}}),
            "username and a password",
        ),
    ],
)
def test_malformed_personas_are_refused_without_naming_any_value(
    value: str, message: str
) -> None:
    with pytest.raises(signin.SignInError) as raised:
        signin.personas_from_env(value)
    text = str(raised.value)
    assert message in text and "UX_TEST_PERSONAS" in text
    assert SAMPLE_LOGIN not in text and SAMPLE_CREDENTIAL not in text
    assert "Admin" not in text
    assert raised.value.__cause__ is None


class FactsPage:
    """A session whose one evaluation answers with ``value``."""

    def __init__(self, value: Any) -> None:
        self.value = value
        self.expressions: list[str] = []

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        assert method == "Runtime.evaluate" and params is not None
        self.expressions.append(params["expression"])
        return {"result": {"value": self.value}}


@pytest.mark.unit
def test_page_facts_strip_the_sidebar_prefixes_and_read_the_viewport() -> None:
    page = FactsPage(
        {
            "stack": "Stack Name: idp-ux-nightly",
            "version": "Version: 0.6.12",
            "build": "Build: 2026-10-13T21:04:11Z",
            "w": 1600,
            "h": 1000,
            "dpr": 1,
            "url": "http://localhost:3000/#/documents",
        }
    )
    assert signin.page_facts(page) == signin.PageFacts(
        stack_name="idp-ux-nightly",
        version="0.6.12",
        build="2026-10-13",
        viewport=(1600, 1000, 1.0),
        url="http://localhost:3000/#/documents",
    )
    assert len(page.expressions) == 1
    assert 'a[href="#' in page.expressions[0]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("Version: 0.6.12", "0.6.12"),
        ("Version: 0.6.12-rc1 Update", "0.6.12-rc1"),
        ("Version: latest", None),
        ("Version: 0.6.12!", None),
        ("Version: 1", None),
        (None, None),
    ],
)
def test_a_version_without_a_versions_shape_reads_as_none(
    version: str | None, expected: str | None
) -> None:
    assert signin.page_facts(FactsPage({"version": version})).version == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("build", "expected"),
    [
        ("Build: 2026-10-13 21:04", "2026-10-13"),
        ("Build: built on 2026-10-13, again 2026-10-14", "2026-10-13"),
        ("Build: 2026-13-45", None),
        ("Build: yesterday", None),
    ],
)
def test_the_build_is_reduced_to_its_first_real_date(
    build: str, expected: str | None
) -> None:
    assert signin.page_facts(FactsPage({"build": build})).build == expected


@pytest.mark.unit
def test_page_rendered_facts_are_bounded_and_printable() -> None:
    facts = signin.page_facts(
        FactsPage(
            {"stack": "Stack Name: " + "s" * 300, "url": "http://x/\n" + "p" * 400}
        )
    )
    assert facts.stack_name == "s" * 120
    assert facts.url is not None and facts.url.isprintable() and len(facts.url) == 300


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        None,
        "not an object",
        {"w": True, "h": 1000, "dpr": 1},
        {"w": 1600, "h": "x"},
        {"w": 1600, "h": 1000, "dpr": float("nan")},
    ],
)
def test_missing_or_malformed_facts_read_as_none(value: Any) -> None:
    facts = signin.page_facts(FactsPage(value))
    assert facts.viewport is None
    assert facts.stack_name is None and facts.version is None and facts.build is None
