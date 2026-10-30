# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the CDP sign-in and the page facts, ``ux_runner/signin.py``.

The sign-in runs against a scripted fake of the DevTools session: a small state
machine that plays an Amplify sign-in page (a form until it is submitted, then a
signed-in app, with ``localStorage`` and a reload) and records every call. That
call log is what pins the module's central property: the password reaches the
page in exactly one call, ``Input.insertText``, and never in page JavaScript or in
any error, whatever the page throws back. ``UX_TEST_PERSONAS`` decoding and the
sidebar facts are covered alongside, with every message checked for values. The
facts script itself runs under node against the signed-in sidebar's markup; that
test skips when node is absent, except in CI, where it fails instead.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
NAVIGATION_TSX = (
    SCRIPTS.parent / "src" / "ui" / "src" / "components" / "genaiidp-layout"
) / "navigation.tsx"
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
def test_a_first_visit_keeps_the_welcome_page_and_still_reloads_once() -> None:
    """A story about a first visit must land on the welcome page, so nothing marks
    it dismissed; the reload that clears a cold sign-in's blank page still runs."""
    page = FakeLoginPage()
    fake = FakeTime()
    signin.sign_in(
        page,
        _persona(),
        dismiss_welcome=False,
        timeout=5.0,
        sleep=fake.sleep,
        clock=fake.clock,
    )
    kinds = page.kinds()
    assert "dismiss" not in kinds
    assert kinds.count("Page.reload") == 1
    assert signin.WELCOME_DISMISSED_KEY not in page.storage
    assert kinds[-1] == "state"
    parameter = inspect.signature(signin.sign_in).parameters["dismiss_welcome"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is True


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
    for href, _label in signin.DEPLOYMENT_INFO_LINKS.values():
        assert f"text({json.dumps(href)})" in page.expressions[0]


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
    facts = signin.page_facts(FactsPage({"version": version}), timeout=0)
    assert facts.version == expected


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
    facts = signin.page_facts(FactsPage(value), timeout=0)
    assert facts.viewport is None
    assert facts.stack_name is None and facts.version is None and facts.build is None


#: The signed-in sidebar's Deployment Info section on the review stack, as
#: Cloudscape's SideNavigation renders the items navigation.tsx gives it: each item
#: an ``li`` holding one link whose only child is a ``span`` of the item's text, and
#: the version's "Update" badge a sibling of that link, not inside it. The classes
#: are the UI bundle's; the expandable section's header is cut to its text and the
#: popover around the badge is left out.
DEPLOYMENT_INFO_HTML = """\
<div class="idp-side-nav"><nav class="awsui_root_l0dv0_1qeic_145">
<ul class="awsui_list_l0dv0_1qeic_224 awsui_list-variant-root_l0dv0_1qeic_245">
<li data-itemid="item-1" class="awsui_list-item_l0dv0_1qeic_260"><a href="#/documents" \
class="awsui_link_l0dv0_1qeic_318 awsui_link-active_l0dv0_1qeic_327" aria-current="page">\
<span class="awsui_link-text_eymn4_rug8v_6">Document List</span></a></li>
<li data-itemid="item-9" class="awsui_list-item_l0dv0_1qeic_260">\
<div class="awsui_section_l0dv0_1qeic_271 awsui_refresh_l0dv0_1qeic_284">\
<div role="button" aria-expanded="true"><span>Deployment Info</span></div><div>\
<ul class="awsui_list_l0dv0_1qeic_224">
<li data-itemid="item-1" class="awsui_list-item_l0dv0_1qeic_260"><a href="#stackname" \
class="awsui_link_l0dv0_1qeic_318"><span class="awsui_link-text_eymn4_rug8v_6">\
Stack Name: idp-ux-nightly</span></a></li>
<li data-itemid="item-2" class="awsui_list-item_l0dv0_1qeic_260"><a href="#version" \
class="awsui_link_l0dv0_1qeic_318"><span class="awsui_link-text_eymn4_rug8v_6">\
Version: 0.6.12</span></a><span class="awsui_info_l0dv0_1qeic_364 awsui_info_1fhsi_6k3bb_5">\
<span class="awsui_badge_1yjyg_12ixj_145 awsui_badge-color-blue_1yjyg_12ixj_197">\
Update</span></span></li>
<li data-itemid="item-3" class="awsui_list-item_l0dv0_1qeic_260"><a href="#builddatetime" \
class="awsui_link_l0dv0_1qeic_318"><span class="awsui_link-text_eymn4_rug8v_6">\
Build: 2026-10-16 21:07:20</span></a></li>
<li data-itemid="item-4" class="awsui_list-item_l0dv0_1qeic_260"><a href="#idppattern" \
class="awsui_link_l0dv0_1qeic_318"><span class="awsui_link-text_eymn4_rug8v_6">\
Pattern: Unified</span></a></li>
<li data-itemid="item-5" class="awsui_list-item_l0dv0_1qeic_260"><a href="#region" \
class="awsui_link_l0dv0_1qeic_318"><span class="awsui_link-text_eymn4_rug8v_6">\
Region: us-east-1</span></a></li>
</ul></div></div></li>
</ul></nav></div>
"""
#: What the page holds after the sign-in's reload and until the UI's settings
#: arrive: the layout waits for them behind a spinner, so there is no sidebar.
SPINNER_HTML = (
    '<div style="display: flex; justify-content: center">'
    '<span role="progressbar" aria-label="Loading"></span></div>'
)


class _Dom(HTMLParser):
    """A fixture page as a tree: each element a ``{"tag", "attrs", "children"}``
    mapping, and each run of text a string among its parent's children."""

    def __init__(self, markup: str) -> None:
        super().__init__()
        self.root: dict[str, Any] = {"tag": "#document", "attrs": {}, "children": []}
        self._open = [self.root]
        self.feed(markup)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = {
            "tag": tag,
            "attrs": {name: value or "" for name, value in attrs},
            "children": [],
        }
        self._open[-1]["children"].append(element)
        self._open.append(element)

    def handle_endtag(self, tag: str) -> None:
        while len(self._open) > 1 and self._open.pop()["tag"] != tag:
            pass

    def handle_data(self, data: str) -> None:
        self._open[-1]["children"].append(data)

    def link_text(self, href: str) -> str | None:
        """``document.querySelector('a[href="<href>"]').textContent``, or None."""
        for element in _elements(self.root):
            if element["tag"] == "a" and element["attrs"].get("href") == href:
                return _text_content(element)
        return None


def _elements(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every element under ``node``, in document order."""
    for child in node["children"]:
        if isinstance(child, dict):
            yield child
            yield from _elements(child)


def _text_content(node: dict[str, Any]) -> str:
    return "".join(
        child if isinstance(child, str) else _text_content(child)
        for child in node["children"]
    )


def _facts_of(dom: _Dom) -> dict[str, Any]:
    """What the facts script answers on ``dom``, computed here in Python: each
    Deployment Info link's text content cut to the reader's bound, or ``None``
    when the page has no such link, and the stand-in window's viewport and URL.
    ``test_the_facts_script_reads_the_sidebar_links_under_node`` runs the script
    itself over the same markup and requires the same answer."""
    value: dict[str, Any] = {}
    for fact, (href, _label) in signin.DEPLOYMENT_INFO_LINKS.items():
        text = dom.link_text(href)
        value[fact] = None if text is None else text[: signin._FACT_LIMIT]
    value.update(w=1600, h=1000, dpr=1, url="http://localhost:3000/#/documents")
    return value


class SidebarPage:
    """A signed-in page whose facts script reads fixture markup.

    Each evaluation sees the next of ``pages`` and the last for good, so a page
    can show the spinner a few times before its layout. The answer is
    :func:`_facts_of` that page, which the node test holds equal to what the
    script itself computes.
    """

    def __init__(self, *pages: str) -> None:
        self.pages = [_Dom(page) for page in pages]
        self.evaluations = 0

    def call(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        assert method == "Runtime.evaluate" and params is not None
        assert params["expression"] == signin._FACTS_JS
        dom = self.pages[min(self.evaluations, len(self.pages) - 1)]
        self.evaluations += 1
        return {"result": {"value": _facts_of(dom)}}


@pytest.mark.unit
def test_the_facts_are_read_from_the_links_the_sidebar_renders() -> None:
    """The live review stack's sidebar showed "Version: 0.6.12" and "Build:
    2026-10-16 21:07:20" under Deployment Info; read from that structure, the
    version is the release and the build its date, and the "Update" badge beside
    the version's link stays out of the version."""
    time = FakeTime()
    page = SidebarPage(DEPLOYMENT_INFO_HTML)
    facts = signin.page_facts(page, sleep=time.sleep, clock=time.clock)
    assert facts == signin.PageFacts(
        stack_name="idp-ux-nightly",
        version="0.6.12",
        build="2026-10-16",
        viewport=(1600, 1000, 1.0),
        url="http://localhost:3000/#/documents",
    )
    assert (page.evaluations, time.now) == (1, 0.0)


@pytest.mark.unit
def test_the_facts_wait_for_the_sidebar_the_ui_renders_once_its_settings_arrive() -> (
    None
):
    """Straight after the sign-in's reload the UI shows only a spinner, because it
    renders its layout once its settings have come from SSM; reading then gave
    a run no version and no build although the sidebar showed both."""
    time = FakeTime()
    page = SidebarPage(SPINNER_HTML, SPINNER_HTML, SPINNER_HTML, DEPLOYMENT_INFO_HTML)
    facts = signin.page_facts(page, sleep=time.sleep, clock=time.clock)
    assert (facts.stack_name, facts.version, facts.build) == (
        "idp-ux-nightly",
        "0.6.12",
        "2026-10-16",
    )
    assert page.evaluations == 4
    assert time.now == pytest.approx(3 * signin._POLL_SECONDS)


@pytest.mark.unit
def test_a_sidebar_that_never_appears_gives_no_facts_once_the_wait_is_over() -> None:
    time = FakeTime()
    page = SidebarPage(SPINNER_HTML)
    facts = signin.page_facts(page, timeout=5.0, sleep=time.sleep, clock=time.clock)
    assert (facts.stack_name, facts.version, facts.build) == (None, None, None)
    assert facts.viewport == (1600, 1000, 1.0)
    assert 5.0 <= time.now < 5.0 + signin._POLL_SECONDS
    assert page.evaluations == round(5.0 / signin._POLL_SECONDS) + 1


@pytest.mark.unit
def test_the_links_read_are_the_ones_the_ui_source_renders() -> None:
    """Each fact's ``href`` and label are those navigation.tsx gives the
    Deployment Info item, so a change to either there fails here first."""
    source = NAVIGATION_TSX.read_text(encoding="utf-8")
    for href, label in signin.DEPLOYMENT_INFO_LINKS.values():
        item = (
            rf"text: `{re.escape(label)} \$\{{settings\.\w+\}}`,\s*"
            rf"href: '{re.escape(href)}'"
        )
        assert re.search(item, source), f"navigation.tsx renders no {label} {href}"


HAS_NODE = shutil.which("node") is not None
IN_CI = bool(os.getenv("CI") or os.getenv("GITLAB_CI") or os.getenv("GITHUB_ACTIONS"))

#: Runs a page script under node against stub documents built from fixture trees:
#: elements with their attributes and children, ``textContent`` as the DOM defines
#: it, and a ``querySelector`` that reads ``tag[attr="value"]`` and throws on any
#: other selector, so one the stub cannot read fails the test instead of matching
#: nothing. ``window`` and ``location`` stand in with the values ``_facts_of``
#: assumes.
FACTS_HARNESS_JS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Text {
  constructor(data) { this.data = data; }
  get textContent() { return this.data; }
}
class Element {
  constructor(node) {
    this.localName = node.tag;
    this.attrs = node.attrs;
    this.childNodes = node.children.map(
      (child) => (typeof child === 'string' ? new Text(child) : new Element(child)));
  }
  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  }
  get textContent() { return this.childNodes.map((child) => child.textContent).join(''); }
  *elements() {
    for (const child of this.childNodes) {
      if (child instanceof Element) { yield child; yield* child.elements(); }
    }
  }
}
const matcher = (selector) => {
  const parts = /^([a-z][a-z0-9-]*)\[([a-z-]+)="([^"]*)"\]$/.exec(selector);
  if (!parts) {
    throw new Error('selector syntax this stub does not implement: ' + JSON.stringify(selector));
  }
  const [, tag, name, value] = parts;
  return (el) => el.localName === tag && el.getAttribute(name) === value;
};
const answers = input.pages.map((page) => {
  const root = new Element(page);
  globalThis.document = {
    querySelector: (selector) => {
      const test = matcher(selector);
      for (const el of root.elements()) { if (test(el)) { return el; } }
      return null;
    },
  };
  globalThis.window = { innerWidth: 1600, innerHeight: 1000, devicePixelRatio: 1 };
  globalThis.location = { href: 'http://localhost:3000/#/documents' };
  return (0, eval)(input.script);
});
process.stdout.write(JSON.stringify(answers));
"""


@pytest.fixture(scope="module")
def facts_under_node(tmp_path_factory: pytest.TempPathFactory) -> list[Any]:
    """``signin._FACTS_JS`` as shipped, run under node over the signed-in
    sidebar and the spinner before it; skipped without node, except in CI, where
    a skip would let the only check on the script itself go missing."""
    if not HAS_NODE:
        reason = "node is not on PATH, and this test runs the facts script under it"
        if IN_CI:
            pytest.fail(f"{reason}. CI must run it, so give this job node.")
        pytest.skip(reason)
    harness = tmp_path_factory.mktemp("facts") / "harness.js"
    harness.write_text(FACTS_HARNESS_JS, encoding="utf-8")
    pages = [_Dom(DEPLOYMENT_INFO_HTML).root, _Dom(SPINNER_HTML).root]
    done = subprocess.run(
        ["node", str(harness)],
        input=json.dumps({"script": signin._FACTS_JS, "pages": pages}),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if done.returncode != 0:
        pytest.fail(f"node could not run the facts script: {done.stderr.strip()}")
    return json.loads(done.stdout)


@pytest.mark.unit
def test_the_facts_script_reads_the_sidebar_links_under_node(
    facts_under_node: list[Any],
) -> None:
    """Every other facts test reads the fixture through ``_facts_of``, a Python
    stand-in for the script. This runs the script itself, so a selector, a
    property or a bound it gets wrong fails here: on the sidebar it answers each
    link's whole text, on the spinner nothing, and both answers equal the
    stand-in's."""
    sidebar, spinner = facts_under_node
    assert sidebar == _facts_of(_Dom(DEPLOYMENT_INFO_HTML))
    assert spinner == _facts_of(_Dom(SPINNER_HTML))
    assert (sidebar["stack"], sidebar["version"], sidebar["build"]) == (
        "Stack Name: idp-ux-nightly",
        "Version: 0.6.12",
        "Build: 2026-10-16 21:07:20",
    )
    assert [spinner[fact] for fact in signin.DEPLOYMENT_INFO_LINKS] == [None] * 3
    facts = signin.page_facts(FactsPage(sidebar), timeout=0)
    assert (facts.stack_name, facts.version, facts.build) == (
        "idp-ux-nightly",
        "0.6.12",
        "2026-10-16",
    )
