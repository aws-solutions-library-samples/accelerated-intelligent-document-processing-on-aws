# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Each lambda must execute the helper copy that ships beside it.

Every lambda here is a flat `index.py` with a bare `from log_sanitizer import ...`, and
each ships its own `log_sanitizer.py`. An unqualified import resolves once per process:
whichever copy loads first stays in `sys.modules` under that name, so without an
eviction every lambda but the first runs the first one's helper and seven of the eight
tracked copies are never executed by anything.

⚠️ **The symptom was a coverage figure that depended on the xdist worker count**, which
is the most expensive shape a finding can have. The number of copies measured is the
number of worker processes, and which copy each measures is its own partition's first
lambda — so over this suite `-n 2` and `-n 4` measured ONE copy and `-n 8` and `-n 16`
measured two. The per-file baseline was recorded under `-n auto`; any run at a lower
worker count therefore reported the other copy as "recorded but not mentioned", a
confident-looking loss in a file whose own suite is green.

The copies being byte-identical today is what kept that harmless, and it is not a
guarantee: `sanitize_event_for_logging` is what keeps request payloads out of the logs,
and a divergence in seven of the eight would have been invisible to every test here
because no test ever imported them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import boto3
import pytest
from conftest import PER_LAMBDA_HELPERS

LAMBDAS_DIR = Path(__file__).resolve().parents[1] / "lambdas"

#: Every lambda directory that ships its own copy of a `PER_LAMBDA_HELPERS` module.
#:
#: Derived from the tree rather than listed, so a lambda added with a helper copy is
#: covered here without an edit — which is the direction this test exists to protect.
WITH_OWN_HELPER = sorted(
    d.name
    for d in LAMBDAS_DIR.iterdir()
    if d.is_dir() and any((d / f"{h}.py").is_file() for h in PER_LAMBDA_HELPERS)
)


#: Every environment variable any lambda here reads at **module** level, with a value
#: that merely has to exist.
#:
#: Derived by reading the indexes rather than listed, so a lambda that starts requiring
#: a new one fails this module's own derivation check below instead of raising a bare
#: `KeyError` from inside `importlib`. Several of these are read as
#: `os.environ["NAME"]`, which raises at import, and this module imports every lambda
#: rather than only the one under test — so it needs the union, where each lambda's own
#: test file needs only its own.
#:
#: The values are deliberately inert placeholders: nothing here calls a handler, so no
#: assertion depends on any of them being meaningful. What is being asserted is which
#: FILE the import resolved to. The exceptions in :data:`VALIDATED_AT_IMPORT` are the
#: names whose value is PARSED while the module loads, so a placeholder raises there.
VALIDATED_AT_IMPORT = {
    # `logging.getLevelName`/`setLevel` rejects anything that is not a level name.
    "LOG_LEVEL": "DEBUG",
    "AWS_REGION": "us-east-1",
    "HOST_REGION": "us-east-1",
    "MARKETPLACE_AGREEMENT_REGION": "us-east-1",
    # Read through `int(...)` at module level.
    "LATEST_JSON_TTL_SECONDS": "300",
    "LATEST_JSON_NEGATIVE_TTL_SECONDS": "60",
}

IMPORT_TIME_ENV = {
    name: VALIDATED_AT_IMPORT.get(name, "placeholder-for-import")
    for name in sorted(
        {
            match.group(1)
            for index in LAMBDAS_DIR.glob("*/index.py")
            for match in re.finditer(
                r"os\.environ(?:\.get)?\(?\[?\s*\"([A-Z_]+)\"", index.read_text()
            )
        }
    )
}


@pytest.fixture(autouse=True)
def _import_time_env(monkeypatch):
    """Satisfy every module-level `os.environ[...]` before any lambda is imported.

    ⚠️ **`boto3.DEFAULT_SESSION` has to be cleared, not just the environment set.**
    These indexes build clients at module level through a bare `boto3.client(...)`,
    which creates a process-global default session on first use and **caches the region
    and credentials it resolved**. Under the hermetic environment the coverage producer
    runs (`AWS_CONFIG_FILE=/dev/null`, every `AWS_*` unset) there is nothing to resolve,
    so whichever test touched boto3 first in this worker pinned a session with no
    region, and a later `monkeypatch.setenv` is ignored by every client built
    afterwards. The symptom is a `NoRegionError` that depends on test order and on the
    worker count — the same class of defect this module exists to fix, and the reason it
    is worth spelling out twice.
    """
    for name, value in IMPORT_TIME_ENV.items():
        monkeypatch.setenv(name, value)
    # A real region and inert credentials: botocore validates the region even for a
    # client that is never called, and resolves credentials eagerly for some of them.
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None, raising=False)


@pytest.mark.unit
def test_the_import_time_env_derivation_still_finds_something() -> None:
    """Non-vacuity for the derivation above: an empty mapping would make the autouse
    fixture a no-op and send every test below back to a bare KeyError at import."""
    assert len(IMPORT_TIME_ENV) > 5, sorted(IMPORT_TIME_ENV)


@pytest.mark.unit
def test_the_helper_is_shipped_by_more_than_one_lambda() -> None:
    """Non-vacuity. With one copy there is nothing to shadow and every assertion below
    would pass over a loader that evicts nothing."""
    assert len(WITH_OWN_HELPER) > 1, (
        f"only {WITH_OWN_HELPER} ships a per-lambda helper, so the shadowing this "
        f"module is about cannot arise and these tests prove nothing"
    )


@pytest.mark.unit
@pytest.mark.parametrize("lambda_name", WITH_OWN_HELPER)
@pytest.mark.parametrize("helper", PER_LAMBDA_HELPERS)
def test_each_lambda_resolves_the_helper_beside_it(
    lambda_name: str, helper: str, load_lambda
) -> None:
    """The property, per lambda: the loaded helper's `__file__` is in THIS directory.

    Asserted on the file rather than on the module object, because two copies of
    identical source produce modules that compare equal in every way a test is likely
    to check — which is exactly why the shadowing survived.
    """
    load_lambda(lambda_name)
    loaded = sys.modules.get(helper)
    assert loaded is not None, (
        f"{lambda_name}'s index did not import {helper} at all, so this assertion has "
        f"nothing to check — has the import moved?"
    )
    assert (
        Path(loaded.__file__).resolve()
        == (LAMBDAS_DIR / lambda_name / f"{helper}.py").resolve()
    ), (
        f"{lambda_name} is running {Path(loaded.__file__).resolve()}, which belongs to "
        f"another lambda. `_load_module` must evict {helper!r} from sys.modules along "
        f"with `index`, or only the first copy loaded in each process is ever executed."
    )


@pytest.mark.unit
@pytest.mark.parametrize("helper", PER_LAMBDA_HELPERS)
def test_loading_every_lambda_in_one_process_executes_every_copy(
    helper: str, load_lambda
) -> None:
    """The whole-process form, which is what makes the measurement worker-independent.

    The per-lambda test above passes on a loader that evicts correctly for a single
    load. This one loads them all in sequence in one process and asserts the set of
    helper files executed is the complete set — the property that stops the coverage
    report from depending on how the suite was partitioned across workers.
    """
    seen = set()
    for lambda_name in WITH_OWN_HELPER:
        load_lambda(lambda_name)
        loaded = sys.modules.get(helper)
        if loaded is not None:
            seen.add(Path(loaded.__file__).resolve())
    expected = {
        (LAMBDAS_DIR / name / f"{helper}.py").resolve() for name in WITH_OWN_HELPER
    }
    assert seen == expected, (
        f"{len(seen)} of {len(expected)} {helper} copies were executed. The missing "
        f"ones are shadowed by a copy that loaded earlier in this process, which makes "
        f"this tree's coverage a function of the xdist worker count: "
        f"{sorted(expected - seen)}"
    )


@pytest.mark.unit
def test_the_loader_evicts_index_as_well(load_lambda) -> None:
    """The pre-existing half of the eviction, kept asserted alongside the new one.

    `index` is the name every one of these lambdas uses, so losing its eviction would
    make every lambda run the first one's handler — a far louder failure than the
    helper's, but it belongs in the same test for the same reason.
    """
    first, second = WITH_OWN_HELPER[0], WITH_OWN_HELPER[1]
    a = load_lambda(first)
    b = load_lambda(second)
    assert Path(a.__file__).resolve() != Path(b.__file__).resolve(), (
        f"{first} and {second} loaded the same index.py"
    )
    assert Path(b.__file__).resolve() == (LAMBDAS_DIR / second / "index.py").resolve()
