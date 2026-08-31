# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
`resolve_config_profile`: the one place the old and new argument names collapse.

The interesting case is the conflict. Silently preferring one name would run the
caller's documents through a configuration they did not select, and the whole
point of Configuration Profiles is that which configuration produced a result is
knowable. So a genuine disagreement is an error, not a precedence rule.
"""

import importlib
import inspect
import pkgutil

import pytest

from idp_sdk._core.naming import resolve_config_profile


def _aliased_methods():
    """Every public operation method that accepts the former keyword."""
    import idp_sdk.operations as operations

    for module_info in pkgutil.iter_modules(operations.__path__):
        module = importlib.import_module(f"idp_sdk.operations.{module_info.name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__:
                continue
            for method_name, method in inspect.getmembers(cls, inspect.isfunction):
                if method_name.startswith("_"):
                    continue
                params = inspect.signature(method).parameters
                if "config_version" in params:
                    yield f"{cls.__name__}.{method_name}", params


@pytest.mark.unit
def test_neither_name_given():
    assert resolve_config_profile() is None


@pytest.mark.unit
def test_only_the_new_name():
    assert resolve_config_profile(config_profile="lending") == "lending"


@pytest.mark.unit
def test_only_the_former_name():
    assert resolve_config_profile(config_version="lending") == "lending"


@pytest.mark.unit
def test_both_names_agreeing_is_fine():
    assert resolve_config_profile("lending", "lending") == "lending"


@pytest.mark.unit
def test_both_names_disagreeing_is_an_error():
    with pytest.raises(ValueError) as excinfo:
        resolve_config_profile("lending", "payroll")
    message = str(excinfo.value)
    # The message has to name both values; "conflicting arguments" alone leaves
    # the caller guessing which of their two call sites is wrong.
    assert "lending" in message and "payroll" in message


@pytest.mark.unit
def test_an_empty_string_is_not_treated_as_absent():
    # `""` is not a valid profile name, but it is also not None: swallowing it
    # here would turn a caller's bug into a silent fall back to the active
    # profile. Normalization does not get to make that judgment.
    assert resolve_config_profile(config_profile="") == ""


@pytest.mark.unit
def test_required_and_missing_names_both_spellings():
    with pytest.raises(ValueError) as excinfo:
        resolve_config_profile(required=True)
    message = str(excinfo.value)
    assert "config_profile" in message and "config_version" in message


@pytest.mark.unit
def test_every_method_taking_the_old_keyword_also_takes_the_new_one():
    surface = dict(_aliased_methods())
    assert len(surface) >= 12, f"expected the known operations, found {len(surface)}"
    missing = [name for name, params in surface.items() if "config_profile" not in params]
    assert not missing, f"SDK methods missing the config_profile keyword: {missing}"


@pytest.mark.unit
def test_the_new_keyword_is_last_and_keyword_only():
    """
    Adding a parameter in the MIDDLE of these signatures would silently change
    what a positional call means: `config.activate("lending", "my-stack")` passed
    the stack name as the second positional, and inserting `config_profile` there
    would make it the profile instead. So the new keyword goes last, and is
    keyword-only so it can never be positional in the first place.
    """
    offenders = []
    for name, params in _aliased_methods():
        ordered = list(params.values())
        config_profile = params.get("config_profile")
        if config_profile is None:
            continue
        if config_profile.kind is not inspect.Parameter.KEYWORD_ONLY:
            offenders.append(f"{name}: {config_profile.kind.name}")
            continue
        # Last, ignoring **kwargs.
        positional_or_named = [
            p for p in ordered if p.kind is not inspect.Parameter.VAR_KEYWORD
        ]
        if positional_or_named[-1].name != "config_profile":
            offenders.append(f"{name}: followed by {positional_or_named[-1].name}")
    assert not offenders, f"config_profile is not last/keyword-only in: {offenders}"


@pytest.mark.unit
@pytest.mark.parametrize("method_name", ["upload", "activate", "delete"])
def test_the_new_keyword_alone_satisfies_a_formerly_required_argument(method_name):
    """
    `config_version` used to be a required positional on these three. Adding
    `config_profile` next to it was not enough: calling with only the new name
    raised `TypeError: missing 1 required positional argument: 'config_version'`,
    which tells the caller to use the name being retired. The parameter is now
    optional in the signature, with the requirement enforced in the resolver.
    """
    import inspect

    from idp_sdk.operations.config import ConfigOperation

    signature = inspect.signature(getattr(ConfigOperation, method_name))
    assert signature.parameters["config_version"].default is None, (
        f"ConfigOperation.{method_name} still requires config_version positionally, "
        f"so config_profile= alone raises a TypeError naming the former argument"
    )
    # And the new name must bind as a keyword (other required arguments of these
    # methods, e.g. upload's config_file, are not this test's concern).
    signature.bind_partial(object(), config_profile="lending")
