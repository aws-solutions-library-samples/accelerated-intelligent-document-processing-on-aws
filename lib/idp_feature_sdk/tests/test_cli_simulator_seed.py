# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`_seed_simulator_product` — the zero-touch marketplace-simulator seed that
`idp-feature-cli deploy --from-code` performs when the host stack points at a
simulator.

What it is for: a paid feature installed into a host that enforces entitlement
cannot be subscribed to until the product exists in the authority the host
checks. Seeding it at deploy time is what lets an admin press Subscribe
immediately instead of hand-crafting two `curl` calls. It is deliberately
best-effort — an offline simulator must not fail a deploy — and that is exactly
what makes it worth testing: every failure path here is *swallowed*, so a wrong
URL, a wrong payload shape, or a create failure that is mistaken for success
leaves a deployed feature nobody can subscribe to, with a green deploy and a
yellow log line nobody reads.

The tests therefore assert what was sent, not that something was sent: the two
POST paths in order, the payload keys the simulator's `/admin/products` contract
requires, and which manifest fields supply them. They also separate the three
outcomes of the create call that the code treats differently — created,
already-exists (proceed to publish), and a genuine error (do **not** publish) —
because collapsing the last two is a silent way to report a product published
that never was.
"""

from __future__ import annotations

import json
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest
from rich.console import Console

from idp_feature_sdk.cli import _seed_simulator_product

pytestmark = pytest.mark.unit


@dataclass
class _Marketplace:
    productCode: Optional[str] = "prod-demo"  # noqa: N815 — matches manifest casing
    pricingModel: Optional[str] = None  # noqa: N815
    dimensions: list = field(default_factory=list)


@dataclass
class _Manifest:
    displayName: str = "Demo Feature"  # noqa: N815
    marketplace: _Marketplace = field(default_factory=_Marketplace)


class _Recorder:
    """Stands in for ``urllib.request.urlopen``, recording each request and
    replaying a scripted outcome per call."""

    def __init__(self, outcomes: Optional[list] = None) -> None:
        self.requests: list = []
        self.timeouts: list = []
        self.contexts: list = []
        self._outcomes = list(outcomes or [])

    def __call__(self, req, timeout=None, context=None):
        self.requests.append(req)
        self.timeouts.append(timeout)
        self.contexts.append(context)
        outcome = self._outcomes.pop(0) if self._outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return _Response()

    # Convenience views over what was recorded.
    @property
    def urls(self) -> list[str]:
        return [r.full_url for r in self.requests]

    def body(self, index: int) -> Any:
        raw = self.requests[index].data
        return json.loads(raw.decode("utf-8")) if raw else None


class _Response:
    status = 200

    def read(self):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://sim/x", code, "boom", {}, None)  # type: ignore[arg-type]


@pytest.fixture
def recorder(monkeypatch):
    def _install(outcomes=None) -> _Recorder:
        rec = _Recorder(outcomes)
        monkeypatch.setattr("urllib.request.urlopen", rec)
        return rec

    return _install


def _seed(manifest: _Manifest, endpoint: str = "https://sim.example.com") -> str:
    console = Console(record=True, width=200)
    _seed_simulator_product(
        simulator_endpoint=endpoint,
        manifest=manifest,
        console=console,
    )
    return console.export_text()


# ---------------------------------------------------------------------------
# The early return
# ---------------------------------------------------------------------------


def test_a_feature_with_no_product_code_makes_no_calls_at_all(recorder) -> None:
    """An OSS feature is not a marketplace product. Posting a product with an
    empty code would create an unsubscribable phantom listing in the simulator
    that the host's entitlement check would then match nothing against."""
    rec = recorder()
    output = _seed(_Manifest(marketplace=_Marketplace(productCode=None)))
    assert rec.requests == []
    # And it returns quietly — no "Seeding…" banner for a feature that is not
    # being seeded.
    assert "Seeding" not in output


def test_an_empty_string_product_code_is_treated_as_absent(recorder) -> None:
    """Absent and empty must behave the same here; `""` reaching the POST would
    build the path `/admin/products//publish`."""
    rec = recorder()
    _seed(_Manifest(marketplace=_Marketplace(productCode="")))
    assert rec.requests == []


# ---------------------------------------------------------------------------
# The happy path — what is actually sent
# ---------------------------------------------------------------------------


def test_the_create_and_publish_calls_are_made_in_that_order(recorder) -> None:
    """Publish locks pricing and dimensions, like the real Marketplace. Sending
    it first would lock an empty product and the create would then be rejected,
    leaving a published product with no dimensions — subscribable but unusable."""
    rec = recorder()
    output = _seed(_Manifest())
    assert rec.urls == [
        "https://sim.example.com/admin/products",
        "https://sim.example.com/admin/products/prod-demo/publish",
    ]
    assert [r.method for r in rec.requests] == ["POST", "POST"]
    assert "Created simulator product prod-demo" in output
    assert "Published simulator product prod-demo" in output


def test_the_create_payload_carries_the_fields_the_admin_api_requires(
    recorder,
) -> None:
    """The four keys `/admin/products` reads. `productCode` is what the host's
    entitlement check matches on, and `name` is what the admin sees in the
    Subscribe dialog — a payload missing either produces a product that cannot
    be found or cannot be recognised."""
    rec = recorder()
    _seed(_Manifest())
    assert rec.body(0) == {
        "productCode": "prod-demo",
        "name": "Demo Feature",
        "pricingModel": "free",
        "dimensions": [
            {"apiName": "cap_units", "displayName": "Capacity", "category": "Units"}
        ],
    }
    # The publish call carries no body — pricing is already locked by the create.
    assert rec.body(1) is None


def test_the_content_type_header_is_set_on_both_calls(recorder) -> None:
    rec = recorder()
    _seed(_Manifest())
    for req in rec.requests:
        assert req.get_header("Content-type") == "application/json"


def test_a_declared_pricing_model_and_dimensions_replace_the_defaults(
    recorder,
) -> None:
    """The defaults exist so a plain feature seeds *something*. A feature that
    declares its own metering must not have it overwritten by `free` /
    `cap_units`, or the simulator meters the wrong dimension and the buyer is
    billed against a dimension the extension never reports."""
    rec = recorder()
    _seed(
        _Manifest(
            marketplace=_Marketplace(
                productCode="prod-paid",
                pricingModel="usage",
                dimensions=[{"apiName": "pages", "displayName": "Pages"}],
            )
        )
    )
    body = rec.body(0)
    assert body["pricingModel"] == "usage"
    assert body["dimensions"] == [{"apiName": "pages", "displayName": "Pages"}]
    assert "cap_units" not in json.dumps(body)


def test_an_empty_dimensions_list_falls_back_to_the_default(recorder) -> None:
    """Empty is treated as "did not say", not as "explicitly no dimensions": a
    product with zero dimensions cannot be metered at all."""
    rec = recorder()
    _seed(_Manifest(marketplace=_Marketplace(productCode="prod-x", dimensions=[])))
    assert rec.body(0)["dimensions"][0]["apiName"] == "cap_units"


def test_a_trailing_slash_on_the_endpoint_does_not_double_the_separator(
    recorder,
) -> None:
    """A host stack parameter copied with a trailing slash is ordinary. `//admin`
    is a different path to most routers, so the seed would 404 and be logged as
    unreachable."""
    rec = recorder()
    _seed(_Manifest(), endpoint="https://sim.example.com/")
    assert rec.urls[0] == "https://sim.example.com/admin/products"
    assert "//admin" not in rec.urls[0]


def test_tls_verification_is_relaxed_only_for_this_call(recorder) -> None:
    """The simulator runs behind a self-signed / nip.io certificate, so an SSL
    context with verification off is passed explicitly rather than by mutating
    the process default — which would silently disable verification for every
    later HTTPS call in the same CLI run."""
    import ssl

    rec = recorder()
    _seed(_Manifest())
    for ctx in rec.contexts:
        assert ctx is not None
        assert ctx.check_hostname is False
        assert ctx.verify_mode == ssl.CERT_NONE
    # The process-wide default is untouched.
    assert ssl.create_default_context().verify_mode == ssl.CERT_REQUIRED


# ---------------------------------------------------------------------------
# The three distinct create outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 409])
def test_an_already_exists_response_still_proceeds_to_publish(
    recorder, code: int
) -> None:
    """Re-deploying a feature is routine, and the second deploy's create is
    rejected. Treating that as fatal would leave a product created-but-never-
    published on a redeploy that followed a partial first run."""
    rec = recorder([_http_error(code)])
    output = _seed(_Manifest())
    assert rec.urls == [
        "https://sim.example.com/admin/products",
        "https://sim.example.com/admin/products/prod-demo/publish",
    ]
    assert "already exists" in output


def test_a_genuine_create_error_does_not_attempt_publish(recorder) -> None:
    """A 500 means the product does not exist. Publishing a code the simulator
    has never heard of is at best a second error, and logging "Published" after
    it would say the seed succeeded when nothing was created."""
    rec = recorder([_http_error(500)])
    output = _seed(_Manifest())
    assert rec.urls == ["https://sim.example.com/admin/products"]
    assert "Could not create simulator product" in output
    assert "Published simulator product" not in output
    assert "Seed it manually" in output


def test_an_unreachable_simulator_is_logged_and_the_deploy_continues(
    recorder,
) -> None:
    """The whole reason this is best-effort. An offline simulator must not raise
    — a raised exception here aborts `deploy` after the feature stack has already
    been created."""
    rec = recorder([urllib.error.URLError("connection refused")])
    output = _seed(_Manifest())
    assert rec.urls == ["https://sim.example.com/admin/products"]
    assert "unreachable" in output
    assert "Published simulator product" not in output


def test_a_connection_reset_mid_create_is_also_tolerated(recorder) -> None:
    """`OSError` rather than `URLError`: a TCP reset or a DNS failure surfaces as
    a bare OSError on some platforms, and an unhandled one would abort deploy."""
    rec = recorder([ConnectionResetError("reset by peer")])
    output = _seed(_Manifest())
    assert len(rec.urls) == 1
    assert "skipping" in output


# ---------------------------------------------------------------------------
# Publish-step outcomes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 409])
def test_an_already_published_product_is_not_an_error(recorder, code: int) -> None:
    output = _seed_with_publish_outcome(_http_error(code))
    assert "already published" in output


def test_a_failing_publish_is_logged_rather_than_raised(recorder) -> None:
    output = _seed_with_publish_outcome(_http_error(503))
    assert "Could not publish simulator product" in output


def test_an_unreachable_simulator_at_publish_time_is_logged(recorder) -> None:
    output = _seed_with_publish_outcome(urllib.error.URLError("gone"))
    assert "Could not publish simulator product" in output


def _seed_with_publish_outcome(outcome: Exception) -> str:
    """Create succeeds, publish raises ``outcome``. Separate helper because the
    monkeypatch has to be installed before the call and after the create
    outcome is chosen."""
    import unittest.mock

    rec = _Recorder([None, outcome])
    with unittest.mock.patch("urllib.request.urlopen", rec):
        console = Console(record=True, width=200)
        _seed_simulator_product(
            simulator_endpoint="https://sim.example.com",
            manifest=_Manifest(),
            console=console,
        )
        text = console.export_text()
    assert len(rec.urls) == 2, "publish must still be attempted after a clean create"
    return text
