# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for the two schema-discovery commands, `idp-cli discover` and
`idp-cli discover-multidoc`, and for the output writer they share.

Both commands ask Amazon Bedrock to infer a JSON Schema for a document class and
then write that schema to disk, where the rest of the solution consumes it as
configuration. That makes two things worth testing far more than the rendering in
between. The first is **input selection**: which documents are sent, and what the
command does when the selection is empty, ambiguous, or larger than the mode
supports. The second is **output writing**: exactly what lands on disk, under which
filename, for a given combination of schema count, `--output` shape and batch flag.
Everything here reads the written file back off the filesystem and parses it, or
parses the JSON the command printed to stdout; nothing asserts that a mock was
called with a filename.

A third thing these tests pin is that the model id and region the user asked for
are the ones that reach the client and the operation. A discovery command that
silently uses a different model than the one requested produces a schema the user
did not ask for and cannot account for later, and the wiring is a plain keyword
hand-off that is easy to drop.

Nothing here reaches Bedrock. Both commands re-import `IDPClient` inside their own
function bodies (`from idp_sdk import IDPClient`), so the binding that decides what
gets constructed is `idp_sdk.IDPClient`, not `idp_cli.cli.IDPClient` — the
module-level name in `cli.py` is shadowed by the local import. Both are patched, so
the tests keep working whichever binding the command ends up using. The results the
fake client returns are the **real** pydantic models from `idp_sdk.models.discovery`
rather than `MagicMock`s, because a `MagicMock` attribute is truthy whatever its
name and would absorb a misspelled field.

Neither command touches S3: `discover` takes local paths that click has already
checked exist, and `discover-multidoc` forwards a local directory or a local file
list. The output side writes with `open()`. So there is nothing here for `moto` to
mock, and the `no_outbound_http` fixture in `conftest.py` is what proves it — a
client built by accident would fail the test rather than reach an account.

Several tests below pin behaviour that is wrong. Each one says so in its docstring
and names the consequence; they are here so that a change to the behaviour shows up
as a failing test rather than going unnoticed.

The filename a schema is written under is the one part of the output contract where a
value from the model becomes a path, so it is tested on both layers that keep it under
`-o` — the name is derived through the canonical class-id rule, and the directory the
file is written into is resolved and checked against `-o` — and each layer has a test
that fails when only that layer is removed. Those tests assert where the file landed,
not that nothing was raised: unsanitised, this code raises nothing and writes the file
to the wrong place.
"""

import io
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from rich.console import Console

from idp_sdk.models.discovery import (
    AutoDetectResult,
    AutoDetectSection,
    DiscoveredClassResult,
    DiscoveryBatchResult,
    DiscoveryResult,
    MultiDocDiscoveryResult,
)

SCHEMA_A = {
    "$id": "Invoice",
    "type": "object",
    "properties": {"total": {"type": "string"}, "date": {"type": "string"}},
}
SCHEMA_B = {
    "$id": "W2",
    "type": "object",
    "properties": {"wages": {"type": "string"}},
}


@pytest.fixture
def runner():
    return CliRunner()


class FakeSdk:
    """The patched `IDPClient` plus the client instance the command will get."""

    def __init__(self, factory: MagicMock, client: MagicMock):
        self.factory = factory
        self.client = client

    @property
    def construction(self):
        """The kwargs `IDPClient(...)` was constructed with."""
        assert self.factory.call_count == 1, (
            f"expected exactly one IDPClient construction, got "
            f"{self.factory.call_count}"
        )
        return self.factory.call_args.kwargs

    def assert_never_constructed(self):
        assert self.factory.call_count == 0, (
            "a refusal must happen before any client is built; "
            f"IDPClient was constructed {self.factory.call_count} time(s)"
        )

    def assert_no_discovery(self):
        """No discovery of any kind was started."""
        discovery = self.client.discovery
        for name in ("run", "run_multi_section", "run_multi_doc"):
            called = getattr(discovery, name).call_args_list
            assert called == [], f"discovery.{name} was called: {called}"
        assert discovery.auto_detect_sections.call_args_list == []


@pytest.fixture
def sdk():
    """Patch `IDPClient` at both bindings and hand back the fake client."""
    client = MagicMock()
    # A bare MagicMock would make `discovery.run` return a MagicMock whose
    # `.status` is truthy but never equal to "SUCCESS"; every test sets an
    # explicit return value or side effect, so leave these unset and let a
    # test that forgets fail on the comparison rather than pass vacuously.
    factory = MagicMock(return_value=client)
    with (
        patch("idp_sdk.IDPClient", factory),
        patch("idp_cli.cli.IDPClient", factory),
    ):
        yield FakeSdk(factory, client)


def _doc(tmp_path: Path, name: str) -> str:
    """Create a stand-in document file and return its path as a string."""
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 not really a pdf")
    return str(path)


def _first_json_value(text: str):
    """Parse the first complete JSON object or array embedded in `text`.

    `emit_json` writes the payload to stdout with `click.echo`, unwrapped and
    unhighlighted, but the surrounding progress lines Rich prints are in the same
    captured stream. This finds the payload rather than requiring the whole stream
    to be JSON, which is what lets these tests assert on the parsed structure
    instead of on a substring.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    raise AssertionError(f"no JSON payload found in output:\n{text}")


# ---------------------------------------------------------------------------
# _write_discover_output — the output contract, tested directly
# ---------------------------------------------------------------------------


@pytest.fixture
def writer_console():
    """A console whose text can be read back, for the helper's own messages.

    `_write_discover_output` takes the console as a parameter, so it can be given
    one writing into a buffer. Its "schemas to stdout" branch goes through
    `emit_json`/`click.echo` instead and is read with `capsys`.
    """
    return Console(file=io.StringIO(), width=200, force_terminal=False)


def _writer():
    from idp_cli.cli import _write_discover_output

    return _write_discover_output


@pytest.mark.unit
def test_no_schemas_writes_no_file_and_prints_nothing(writer_console, tmp_path):
    """An empty schema list is a no-op even when an output path was requested.

    This is the shape a wholly failed discovery leaves behind, and the important
    half is that the requested file is *not* created: a zero-byte or empty-array
    schema file would be picked up as configuration by everything downstream.
    """
    out = tmp_path / "schemas.json"
    _writer()(str(out), [], writer_console, is_batch=True)

    assert not out.exists()
    assert writer_console.file.getvalue() == ""


@pytest.mark.unit
def test_batch_without_output_prints_one_schema_as_an_object(
    writer_console, capsys, tmp_path
):
    """One schema and no `-o` prints the schema itself, not a one-element array."""
    _writer()(None, [SCHEMA_A], writer_console, is_batch=True)

    payload = _first_json_value(capsys.readouterr().out)
    assert payload == SCHEMA_A
    assert "Discovered schemas" in writer_console.file.getvalue()


@pytest.mark.unit
def test_batch_without_output_prints_several_schemas_as_an_array(
    writer_console, capsys
):
    """Several schemas and no `-o` print as a JSON array, in discovery order."""
    _writer()(None, [SCHEMA_A, SCHEMA_B], writer_console, is_batch=True)

    payload = _first_json_value(capsys.readouterr().out)
    assert payload == [SCHEMA_A, SCHEMA_B]


@pytest.mark.unit
def test_single_mode_without_output_prints_nothing_here(writer_console, capsys):
    """In single-document mode the helper stays silent when there is no `-o`.

    The command has already printed the schema inline by this point, so a second
    copy from the helper would duplicate it. `is_batch=False` is what suppresses
    it, and that parameter is only ever passed from the standard discovery path.
    """
    _writer()(None, [SCHEMA_A], writer_console, is_batch=False)

    assert capsys.readouterr().out == ""
    assert writer_console.file.getvalue() == ""


@pytest.mark.unit
def test_one_schema_to_a_file_path_lands_verbatim_on_disk(writer_console, tmp_path):
    """The single-schema file is the schema object itself, indented, nothing else."""
    out = tmp_path / "invoice.schema.json"
    _writer()(str(out), [SCHEMA_A], writer_console, is_batch=False)

    assert json.loads(out.read_text(encoding="utf-8")) == SCHEMA_A
    assert f"Schema written to: {out}" in writer_console.file.getvalue()


@pytest.mark.unit
def test_several_schemas_to_a_file_path_land_as_a_json_array(writer_console, tmp_path):
    """A `.json` path plus several schemas gives one file holding an array."""
    out = tmp_path / "all.json"
    _writer()(str(out), [SCHEMA_A, SCHEMA_B], writer_console, is_batch=True)

    assert json.loads(out.read_text(encoding="utf-8")) == [SCHEMA_A, SCHEMA_B]
    assert "2 schemas written to" in writer_console.file.getvalue()


@pytest.mark.unit
def test_an_existing_directory_gets_one_file_per_schema_named_by_id(
    writer_console, tmp_path
):
    """Directory mode names each file after the schema's `$id`."""
    out = tmp_path / "schemas"
    out.mkdir()
    _writer()(str(out), [SCHEMA_A, SCHEMA_B], writer_console, is_batch=True)

    assert sorted(p.name for p in out.iterdir()) == ["Invoice.json", "W2.json"]
    assert json.loads((out / "Invoice.json").read_text(encoding="utf-8")) == SCHEMA_A
    assert json.loads((out / "W2.json").read_text(encoding="utf-8")) == SCHEMA_B


@pytest.mark.unit
def test_the_filename_falls_back_from_id_to_document_type_to_unknown(
    writer_console, tmp_path
):
    """Three naming sources in priority order, the last of them a constant.

    A schema carrying neither `$id` nor `x-aws-idp-document-type` is written as
    `unknown.json`, so two such schemas in one run would collide — which is the
    overwrite the next test pins.
    """
    out = tmp_path / "schemas"
    out.mkdir()
    typed = {"x-aws-idp-document-type": "BankStatement", "properties": {}}
    anonymous = {"type": "object", "properties": {}}

    _writer()(str(out), [SCHEMA_A, typed, anonymous], writer_console, is_batch=True)

    assert sorted(p.name for p in out.iterdir()) == [
        "BankStatement.json",
        "Invoice.json",
        "unknown.json",
    ]
    assert json.loads((out / "unknown.json").read_text(encoding="utf-8")) == anonymous


@pytest.mark.unit
def test_batch_creates_a_missing_directory_when_the_path_has_no_suffix(
    writer_console, tmp_path
):
    """A suffix-less path in batch mode is treated as a directory and created."""
    out = tmp_path / "nested" / "schemas"
    _writer()(str(out), [SCHEMA_A, SCHEMA_B], writer_console, is_batch=True)

    assert out.is_dir()
    assert sorted(p.name for p in out.iterdir()) == ["Invoice.json", "W2.json"]


@pytest.mark.unit
def test_a_missing_parent_directory_raises_for_the_single_file_path(
    writer_console, tmp_path
):
    """The single-schema branch does not create parent directories.

    Only the directory branch calls `mkdir(parents=True)`. `-o
    ./out/schemas/invoice.json` into a directory that does not exist therefore
    raises `FileNotFoundError` from `open`. In the command that is caught and
    reported (see `test_a_missing_output_directory_is_reported_as_file_not_found`),
    but the discovery has already been paid for by then and the schema is lost.
    """
    out = tmp_path / "does-not-exist" / "invoice.json"

    with pytest.raises(FileNotFoundError):
        _writer()(str(out), [SCHEMA_A], writer_console, is_batch=False)

    assert not out.exists()


@pytest.mark.unit
def test_whether_a_suffixless_path_becomes_a_file_or_a_directory_depends_on_the_count(
    writer_console, tmp_path
):
    """DEFECT: the same `-o ./schemas` is a file or a directory by schema count.

    `cli.py:5742` tests `len(all_schemas) == 1 and not output_path.is_dir()` before
    the directory branch at 5747, so a batch run that happens to yield exactly one
    schema writes a *regular file* named `schemas`, while the same command over the
    same corpus yielding two schemas creates a *directory* `schemas/` holding
    `Invoice.json` and `W2.json`.

    The consequence is that a caller cannot write a script around `-o ./schemas`:
    whether it later globs `schemas/*.json` or reads `schemas` as a file depends on
    how many of its documents succeeded. Worse, a run that previously created the
    directory will fail the next time it yields one schema, because `is_dir()` is
    then true and the path takes the directory branch instead — so the behaviour is
    also order-dependent.
    """
    one = tmp_path / "one"
    _writer()(str(one), [SCHEMA_A], writer_console, is_batch=True)
    assert one.is_file(), "one schema + suffix-less path wrote a regular file"
    assert json.loads(one.read_text(encoding="utf-8")) == SCHEMA_A

    two = tmp_path / "two"
    _writer()(str(two), [SCHEMA_A, SCHEMA_B], writer_console, is_batch=True)
    assert two.is_dir(), "two schemas + suffix-less path made a directory"


@pytest.mark.unit
def test_two_schemas_sharing_an_id_silently_overwrite_in_directory_mode(
    writer_console, tmp_path
):
    """DEFECT: directory mode loses a schema when two share an `$id`.

    The filename is `f"{class_name}.json"` with no collision check
    (`cli.py:5756`), so the second schema overwrites the first and the run still
    reports success for both — two "✓ Schema written to" lines naming the same
    path. The `$id` comes from the model, and asking it to classify several
    similar documents is exactly how two identical `$id` values arise, so the
    silently-dropped schema is a realistic outcome of a batch run rather than a
    contrived one.
    """
    out = tmp_path / "schemas"
    out.mkdir()
    first = {"$id": "Invoice", "properties": {"a": {}}}
    second = {"$id": "Invoice", "properties": {"b": {}}}

    _writer()(str(out), [first, second], writer_console, is_batch=True)

    assert [p.name for p in out.iterdir()] == ["Invoice.json"]
    assert json.loads((out / "Invoice.json").read_text(encoding="utf-8")) == second
    # Both writes were reported as successful, to the same path.
    messages = writer_console.file.getvalue()
    assert messages.count(f"Schema written to: {out / 'Invoice.json'}") == 2


def _escaping_ids(tmp_path: Path) -> dict[str, str]:
    """Three shapes of a path-like `$id`, which behave differently unsanitised.

    `relative` walks *up* out of the directory `-o` named; `absolute` replaces it
    outright, because `Path("/out") / "/elsewhere"` is `/elsewhere`. Both are
    pointed at `tmp_path`, a real and writable directory, so that unsanitised they
    genuinely write somewhere they should not — pointed at a path that does not
    exist, `open` would raise `FileNotFoundError` and a containment assertion
    would pass for the wrong reason.

    `relative-deeper` is the shape that does *not* write: its intermediate
    directories do not exist, so unsanitised it raises `FileNotFoundError`
    instead of escaping. It is here because the sanitised behaviour is the same
    for all three — a filename under `-o` — and because a run that ends in a
    traceback is still not a run that produced the schema the operator asked for.
    """
    return {
        "relative": "../escaped",
        "relative-deeper": "nested/deeper/../../../escaped",
        "absolute": str(tmp_path / "escaped"),
    }


@pytest.mark.unit
@pytest.mark.parametrize("shape", ["relative", "relative-deeper", "absolute"])
def test_a_path_like_id_is_written_inside_the_output_directory(
    writer_console, tmp_path, shape
):
    """A `$id` naming a path lands under `-o` anyway, as a sanitised filename.

    `$id` is generated by the model from document content, so for a
    document-processing accelerator its value is influenced by material the
    operator did not author, and it reaches `_write_discover_output` as the whole
    of the filename. The filename is therefore derived from it — through
    `sanitize_class_name`, the same rule every other consumer of a class id
    applies — and the directory the file is written into is then resolved and
    checked against the resolved `-o`.

    The assertion is on the **resolved path**, not on the absence of an
    exception: the unfixed code raises nothing at all here, it writes the file
    happily to the wrong place. The expected name is computed with the authority
    rather than spelled out, so this cannot drift from the rule it is asserting.
    """
    from idp_common.config.class_names import is_valid_class_name, sanitize_class_name

    escaping_id = _escaping_ids(tmp_path)[shape]
    out = tmp_path / "schemas"
    escaping = {"$id": escaping_id, "properties": {}}

    _writer()(str(out), [escaping, SCHEMA_B], writer_console, is_batch=True)

    # Nothing was written outside `-o`: the parent of the output directory holds
    # the output directory and nothing else. This is the assertion the unfixed
    # code fails, and it is about a resolved location rather than about whether
    # anything was raised — the unfixed code raises nothing here.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schemas"]

    expected = out / f"{sanitize_class_name(escaping_id)}.json"
    assert expected.is_file(), (
        f"expected the schema at {expected}, found {sorted(out.iterdir())}"
    )
    assert expected.resolve().parent == out.resolve(), (
        "the derived name is not a direct child of the directory named by -o"
    )
    assert is_valid_class_name(expected.stem), (
        f"{expected.stem!r} is not a usable class id, so it is not a safe filename"
    )
    assert json.loads(expected.read_text(encoding="utf-8")) == escaping
    assert sorted(p.name for p in out.iterdir()) == sorted([expected.name, "W2.json"])


@pytest.mark.unit
def test_the_containment_check_holds_even_if_the_sanitising_stops_working(tmp_path):
    """The second half of the fix, exercised on its own.

    Sanitising decides what the filename should be; the containment check decides
    whether the result is somewhere the operator asked for. The second is the half
    that survives a spelling the first does not anticipate, so it is worth having
    even though the first makes it unreachable today — and the only way to see it
    work is to take the first one out, which is what patching the filename helper
    to a pass-through does here.
    """
    from idp_cli import cli as cli_module

    out = tmp_path / "schemas"
    out.mkdir()

    with patch.object(cli_module, "_schema_output_filename", lambda name: name):
        for unsanitised in _escaping_ids(tmp_path).values():
            with pytest.raises(ValueError, match="outside"):
                cli_module._schema_output_path(out, unsanitised)

    assert list(tmp_path.iterdir()) == [out]
    assert list(out.iterdir()) == []


@pytest.mark.unit
def test_a_symlink_at_the_target_filename_is_followed_rather_than_refused(
    writer_console, tmp_path
):
    """What the containment check resolves is the directory, not the file.

    An operator who links a schema file inside `-o` out to somewhere else — a
    configuration repository is the ordinary reason — has decided where that file
    lives, and following the link is what this command did before the check
    existed. Resolving the *file* would refuse the write for a class id as
    ordinary as `Invoice`, and because the writer raises rather than skipping, it
    would abandon every schema after it in the batch. So the check asks only
    whether the directory being written into is the one `-o` named.

    This is a boundary rather than an oversight, and it is pinned because a change
    from `file_path.parent.resolve()` to `file_path.resolve()` looks equivalent
    and is not.
    """
    out = tmp_path / "schemas"
    out.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (out / "Invoice.json").symlink_to(elsewhere / "Invoice.json")

    _writer()(str(out), [SCHEMA_A, SCHEMA_B], writer_console, is_batch=True)

    assert json.loads((elsewhere / "Invoice.json").read_text(encoding="utf-8")) == (
        SCHEMA_A
    )
    # The rest of the batch was written too, which is the half a refusal loses.
    assert json.loads((out / "W2.json").read_text(encoding="utf-8")) == SCHEMA_B


@pytest.mark.unit
def test_a_legitimate_class_id_needing_a_rewrite_is_written_and_the_rewrite_reported(
    writer_console, tmp_path
):
    """The case an operator actually meets, and the notice that explains it.

    Every other test here uses an adversarial class id. This one uses `Bank
    Statement`, which is a real `$id` in this repository's own configuration
    library: it is a perfectly ordinary class name and it is outside the class-id
    character set, so the filename is not the id. That substitution has to be
    visible — an operator looking for `Bank Statement.json` and finding
    `Bank-Statement.json` with nothing said about it has to guess — so the notice
    is part of the contract and is asserted here rather than left untested.
    """
    out = tmp_path / "schemas"
    out.mkdir()
    spaced = {"$id": "Bank Statement", "properties": {}}

    _writer()(str(out), [spaced], writer_console, is_batch=True)

    assert [p.name for p in out.iterdir()] == ["Bank-Statement.json"]
    assert (
        json.loads((out / "Bank-Statement.json").read_text(encoding="utf-8")) == spaced
    )
    messages = writer_console.file.getvalue()
    assert "'Bank Statement' is not a usable class id" in messages
    assert "written as Bank-Statement.json" in messages
    # A class id that needed no rewrite gets no notice.
    writer_console.file.truncate(0)
    writer_console.file.seek(0)
    _writer()(str(out), [SCHEMA_B], writer_console, is_batch=True)
    assert "usable class id" not in writer_console.file.getvalue()


@pytest.mark.unit
def test_an_unusable_class_id_falls_back_to_the_same_name_as_no_id_at_all(
    writer_console, tmp_path
):
    """A `$id` of `".."` has nothing usable in it, and is not dropped for that.

    `sanitize_class_name` returns an empty string when no character survives, and
    an empty filename would be `.json`. The fallback is `unknown`, which is the
    name a schema carrying no id at all already gets — so a schema with an
    unusable id is still on disk for the operator to look at.
    """
    out = tmp_path / "schemas"
    out.mkdir()

    _writer()(
        str(out), [{"$id": "..", "properties": {}}], writer_console, is_batch=True
    )

    assert [p.name for p in out.iterdir()] == ["unknown.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["schemas"]


# ---------------------------------------------------------------------------
# discover — input selection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_without_a_document_is_a_usage_error(runner, sdk):
    """`--document` is `required=True`, so click refuses before anything runs."""
    from idp_cli.cli import discover

    result = runner.invoke(discover, [])

    assert result.exit_code == 2, result.output
    assert "--document" in result.output
    sdk.assert_never_constructed()


@pytest.mark.unit
def test_discover_refuses_a_document_path_that_does_not_exist(runner, sdk, tmp_path):
    """`click.Path(exists=True)` catches a typo'd path before any model call."""
    from idp_cli.cli import discover

    result = runner.invoke(discover, ["-d", str(tmp_path / "missing.pdf")])

    assert result.exit_code == 2, result.output
    assert "does not exist" in result.output
    sdk.assert_never_constructed()


@pytest.mark.unit
def test_the_requested_region_and_model_id_reach_the_client_and_the_operation(
    runner, sdk, tmp_path
):
    """The two values a wrong answer would make invisible in the result.

    `--region` decides which account's stack config and which Bedrock endpoint are
    used; `--model-id` decides which model produced the schema. Neither is echoed
    back in the schema file, so if the command dropped one the user would have no
    way to notice from the output — which is why both are asserted at the exact
    hand-off rather than in the printed header.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
    )
    doc = _doc(tmp_path, "invoice.pdf")

    result = runner.invoke(
        discover,
        [
            "-d",
            doc,
            "--region",
            "eu-west-2",
            "--model-id",
            "us.anthropic.claude-opus-4-6-v1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sdk.construction == {"stack_name": None, "region": "eu-west-2"}
    call = sdk.client.discovery.run.call_args.kwargs
    assert call["model_id"] == "us.anthropic.claude-opus-4-6-v1"
    assert call["document_path"] == doc
    assert "Model ID override: us.anthropic.claude-opus-4-6-v1" in result.output


@pytest.mark.unit
def test_no_model_id_is_passed_as_none_rather_than_omitted(runner, sdk, tmp_path):
    """Omitting `--model-id` hands `None` down so the SDK picks the configured model.

    The keyword is always passed; the default lives in the SDK and in the stack
    config, not in the CLI. A CLI-side default would silently override a stack's
    configured discovery model.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", json_schema=SCHEMA_A
    )

    result = runner.invoke(discover, ["-d", _doc(tmp_path, "invoice.pdf")])

    assert result.exit_code == 0, result.output
    assert sdk.client.discovery.run.call_args.kwargs["model_id"] is None
    assert "Model ID override" not in result.output


@pytest.mark.unit
def test_stack_name_class_hint_and_config_profile_are_forwarded_and_echoed(
    runner, sdk, tmp_path
):
    """Stack mode: the header reports what was asked for and the call carries it."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", document_class="W2 Tax Form", json_schema=SCHEMA_B
    )

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "form.pdf"),
            "--stack-name",
            "my-idp-stack",
            "--config-profile",
            "v2",
            "--class-hint",
            "W2 Tax Form",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sdk.construction == {"stack_name": "my-idp-stack", "region": None}
    call = sdk.client.discovery.run.call_args.kwargs
    assert call["config_version"] == "v2"
    assert call["class_name_hint"] == "W2 Tax Form"
    assert "Stack: my-idp-stack" in result.output
    assert "Config profile: v2" in result.output
    assert "Class hint: W2 Tax Form" in result.output
    # The "saved to configuration" claim requires stack + profile + a success.
    assert "saved to configuration" in result.output


@pytest.mark.unit
def test_config_version_is_still_accepted_as_the_former_spelling(runner, sdk, tmp_path):
    """`--config-version` and `--config-profile` write the same parameter."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", json_schema=SCHEMA_A
    )

    result = runner.invoke(
        discover,
        ["-d", _doc(tmp_path, "invoice.pdf"), "--config-version", "legacy"],
    )

    assert result.exit_code == 0, result.output
    assert sdk.client.discovery.run.call_args.kwargs["config_version"] == "legacy"


@pytest.mark.unit
def test_a_local_run_without_a_stack_does_not_claim_to_have_saved_anything(
    runner, sdk, tmp_path
):
    """A config profile with no `--stack-name` cannot be saved, and says so by silence.

    Local mode returns the schema without persisting it. Claiming "saved to
    configuration" here would tell the user their stack had been updated when it
    had not.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", json_schema=SCHEMA_A
    )

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "invoice.pdf"), "--config-profile", "v2"]
    )

    assert result.exit_code == 0, result.output
    assert "saved to configuration" not in result.output
    assert "Stack:" not in result.output


# ---------------------------------------------------------------------------
# discover — output and failure handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_single_document_prints_a_parseable_schema_and_a_field_count(
    runner, sdk, tmp_path
):
    """Single-document mode prints the schema through `emit_json`, unwrapped.

    The schema is printed with `click.echo` rather than through Rich precisely so
    that `idp-cli discover -d x.pdf > schema.json` is parseable; this parses it
    back to prove it.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
    )

    result = runner.invoke(discover, ["-d", _doc(tmp_path, "invoice.pdf")])

    assert result.exit_code == 0, result.output
    assert "Document Class: Invoice" in result.output
    assert "Properties: 2 top-level fields" in result.output
    assert _first_json_value(result.output) == SCHEMA_A


@pytest.mark.unit
def test_a_single_document_with_output_writes_exactly_the_schema(runner, sdk, tmp_path):
    """`-o file.json` in single mode holds the schema object and nothing else."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
    )
    out = tmp_path / "invoice.schema.json"

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "invoice.pdf"), "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8")) == SCHEMA_A


@pytest.mark.unit
def test_a_failed_discovery_exits_one_names_the_error_and_writes_no_file(
    runner, sdk, tmp_path
):
    """A FAILED result must not leave a partial schema file behind."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="FAILED", error="Bedrock throttled the request"
    )
    out = tmp_path / "invoice.schema.json"

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "invoice.pdf"), "-o", str(out)]
    )

    assert result.exit_code == 1, result.output
    assert "Discovery failed" in result.output
    assert "Bedrock throttled the request" in result.output
    assert not out.exists()


@pytest.mark.unit
def test_a_success_carrying_no_schema_writes_nothing_and_exits_zero(
    runner, sdk, tmp_path
):
    """DEFECT: `status="SUCCESS"` with `json_schema=None` exits 0 having written nothing.

    `json_schema` is `Optional` on `DiscoveryResult`, and `cli.py:5680` appends it
    only when truthy, so an empty schema list reaches
    `_write_discover_output`, which returns immediately (`cli.py:5728`). The
    command then reports "✓ Discovery completed successfully", creates no file at
    the path the user named with `-o`, and exits 0.

    The consequence is that a scripted caller — `idp-cli discover -o schema.json &&
    deploy schema.json` — proceeds to the next step with no file and a zero status.
    A missing schema on a successful exit is indistinguishable from a schema that
    was written, so the failure surfaces later and somewhere else.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", document_class="Invoice", json_schema=None
    )
    out = tmp_path / "invoice.schema.json"

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "invoice.pdf"), "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert "Discovery completed successfully" in result.output
    assert not out.exists(), "no file was written, yet the command reported success"
    assert "Schema written to" not in result.output


@pytest.mark.unit
def test_a_batch_with_one_failure_exits_one_but_keeps_the_successful_schema(
    runner, sdk, tmp_path
):
    """Partial success: the good schema is written, and the exit code is still 1.

    Writing what succeeded is the right call — re-running the whole batch to
    recover one document would be wasteful — but the non-zero exit is what stops a
    caller treating the output directory as complete.
    """
    from idp_cli.cli import discover

    good = _doc(tmp_path, "invoice.pdf")
    bad = _doc(tmp_path, "w2.pdf")

    def _run(**kwargs):
        if kwargs["document_path"] == good:
            return DiscoveryResult(
                status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
            )
        return DiscoveryResult(status="FAILED", error="unreadable page")

    sdk.client.discovery.run.side_effect = _run
    out = tmp_path / "schemas"
    out.mkdir()

    result = runner.invoke(discover, ["-d", good, "-d", bad, "-o", str(out)])

    assert result.exit_code == 1, result.output
    assert "Total: 2, Succeeded: 1, Failed: 1" in result.output
    assert "unreadable page" in result.output
    assert [p.name for p in out.iterdir()] == ["Invoice.json"]
    assert json.loads((out / "Invoice.json").read_text(encoding="utf-8")) == SCHEMA_A


@pytest.mark.unit
def test_a_whole_batch_succeeding_reports_the_counts_and_the_batch_header(
    runner, sdk, tmp_path
):
    """Batch mode is selected by document count, and it labels itself."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.side_effect = [
        DiscoveryResult(
            status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
        ),
        DiscoveryResult(status="SUCCESS", document_class="W2", json_schema=SCHEMA_B),
    ]

    result = runner.invoke(
        discover,
        ["-d", _doc(tmp_path, "invoice.pdf"), "-d", _doc(tmp_path, "w2.pdf")],
    )

    assert result.exit_code == 0, result.output
    assert "IDP Discovery (batch)" in result.output
    assert "Documents: 2" in result.output
    assert "Total: 2, Succeeded: 2, Failed: 0" in result.output
    assert "Batch discovery complete" in result.output
    # No -o in batch mode → both schemas printed as an array.
    assert _first_json_value(result.output.split("Discovered schemas")[1]) == [
        SCHEMA_A,
        SCHEMA_B,
    ]


@pytest.mark.unit
def test_the_ground_truth_match_count_is_reported_in_the_header(runner, sdk, tmp_path):
    """`Ground truth matched: n/m` is how a user checks the pairing took effect."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", json_schema=SCHEMA_A
    )
    doc = _doc(tmp_path, "invoice.pdf")
    other = _doc(tmp_path, "w2.pdf")
    gt = tmp_path / "invoice.json"
    gt.write_text("{}", encoding="utf-8")

    result = runner.invoke(discover, ["-d", doc, "-d", other, "-g", str(gt)])

    assert result.exit_code == 0, result.output
    assert "Ground truth matched: 1/2" in result.output
    assert "(with GT: invoice.json)" in result.output


@pytest.mark.unit
def test_single_mode_names_the_ground_truth_file_it_paired(runner, sdk, tmp_path):
    """Single-document mode prints the ground truth path it decided to use.

    One document plus one ground truth file are paired by position, so the names
    need not match and the user has no other way to confirm which file was used.
    Printing it is the only feedback, and dropping the line would make the
    positional pairing invisible.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", json_schema=SCHEMA_A
    )
    doc = _doc(tmp_path, "invoice.pdf")
    gt = tmp_path / "baseline" / "sections" / "1" / "result.json"
    gt.parent.mkdir(parents=True)
    gt.write_text("{}", encoding="utf-8")

    result = runner.invoke(discover, ["-d", doc, "-g", str(gt)])

    assert result.exit_code == 0, result.output
    assert f"Ground Truth: {gt}" in result.output
    assert "Ground truth matched: 1/1" in result.output
    assert sdk.client.discovery.run.call_args.kwargs["ground_truth_path"] == str(gt)


@pytest.mark.unit
def test_an_exception_from_the_operation_exits_one_with_the_message(
    runner, sdk, tmp_path
):
    """Any exception the SDK raises becomes one red line and exit 1, not a traceback."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.side_effect = RuntimeError("no model access in eu-west-3")

    result = runner.invoke(discover, ["-d", _doc(tmp_path, "invoice.pdf")])

    assert result.exit_code == 1, result.output
    assert "Error: no model access in eu-west-3" in result.output
    assert "Traceback" not in result.output


@pytest.mark.unit
def test_a_file_not_found_from_the_operation_is_reported_as_such(runner, sdk, tmp_path):
    """`FileNotFoundError` has its own arm, so the message names the cause."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.side_effect = FileNotFoundError("baseline/result.json")

    result = runner.invoke(discover, ["-d", _doc(tmp_path, "invoice.pdf")])

    assert result.exit_code == 1, result.output
    assert "File not found: baseline/result.json" in result.output


@pytest.mark.unit
def test_a_missing_output_directory_is_reported_as_file_not_found(
    runner, sdk, tmp_path
):
    """`-o` into a non-existent directory fails after discovery has been paid for.

    The write is the last thing the command does, so the model call has already
    happened and its result is discarded. Exit 1 with "File not found" is at least
    honest about nothing having been written; the schema itself is not recoverable
    from the output, because single-document mode's inline dump is the only other
    copy and it is not emitted when `-o` was given.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", json_schema=SCHEMA_A
    )
    out = tmp_path / "nope" / "invoice.json"

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "invoice.pdf"), "-o", str(out)]
    )

    assert result.exit_code == 1, result.output
    assert "File not found" in result.output
    assert not out.exists()


# ---------------------------------------------------------------------------
# discover --auto-detect
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_auto_detect_refuses_more_than_one_document(runner, sdk, tmp_path):
    """Section detection is about one package, so two documents are refused."""
    from idp_cli.cli import discover

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "a.pdf"),
            "-d",
            _doc(tmp_path, "b.pdf"),
            "--auto-detect",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "--auto-detect works with a single document only" in result.output
    sdk.assert_no_discovery()


@pytest.mark.unit
def test_detect_only_lists_the_boundaries_and_starts_no_discovery(
    runner, sdk, tmp_path
):
    """`--detect-only` is the cheap half: boundaries printed, no schema inferred."""
    from idp_cli.cli import discover

    sdk.client.discovery.auto_detect_sections.return_value = AutoDetectResult(
        status="SUCCESS",
        sections=[
            AutoDetectSection(start=1, end=2, type="Cover Letter"),
            AutoDetectSection(start=3, end=5, type=None),
        ],
    )
    doc = _doc(tmp_path, "package.pdf")

    result = runner.invoke(
        discover, ["-d", doc, "--auto-detect", "--detect-only", "--model-id", "m-1"]
    )

    assert result.exit_code == 0, result.output
    assert "Detected 2 section(s)" in result.output
    assert "Pages 1-2: Cover Letter" in result.output
    # A section with no type label renders as "Unknown" rather than "None".
    assert "Pages 3-5: Unknown" in result.output
    assert sdk.client.discovery.auto_detect_sections.call_args.kwargs == {
        "document_path": doc,
        "model_id": "m-1",
    }
    assert sdk.client.discovery.run.call_args_list == []


@pytest.mark.unit
def test_detect_only_with_output_writes_the_boundaries_as_json(runner, sdk, tmp_path):
    """With `-o`, `--detect-only` writes the boundary list, not a schema.

    The same `-o` option carries two entirely different payload shapes depending
    on `--detect-only`; this pins the boundary shape so a change to it is visible.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.auto_detect_sections.return_value = AutoDetectResult(
        status="SUCCESS",
        sections=[AutoDetectSection(start=1, end=3, type="W2")],
    )
    out = tmp_path / "sections.json"

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--auto-detect",
            "--detect-only",
            "-o",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(out.read_text(encoding="utf-8")) == [
        {"start": 1, "end": 3, "type": "W2"}
    ]
    assert "Section boundaries written to" in result.output


@pytest.mark.unit
def test_detect_only_failure_exits_one_and_writes_nothing(runner, sdk, tmp_path):
    """A FAILED detection stops before the output file is created."""
    from idp_cli.cli import discover

    sdk.client.discovery.auto_detect_sections.return_value = AutoDetectResult(
        status="FAILED", error="document is not a PDF"
    )
    out = tmp_path / "sections.json"

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--auto-detect",
            "--detect-only",
            "-o",
            str(out),
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Auto-detect failed: document is not a PDF" in result.output
    assert not out.exists()


@pytest.mark.unit
def test_auto_detect_discovers_each_section_and_writes_one_file_per_class(
    runner, sdk, tmp_path
):
    """The full auto-detect path: one `run(auto_detect=True)` call, a batch result back."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=2,
        succeeded=2,
        failed=0,
        results=[
            DiscoveryResult(
                status="SUCCESS",
                document_class="Invoice",
                json_schema=SCHEMA_A,
                page_range="1-2",
            ),
            DiscoveryResult(
                status="SUCCESS",
                document_class="W2",
                json_schema=SCHEMA_B,
                page_range="3-5",
            ),
        ],
    )
    doc = _doc(tmp_path, "package.pdf")
    out = tmp_path / "schemas"

    result = runner.invoke(
        discover,
        ["-d", doc, "--auto-detect", "-o", str(out), "--config-profile", "v2"],
    )

    assert result.exit_code == 0, result.output
    assert sdk.client.discovery.run.call_args.kwargs == {
        "document_path": doc,
        "config_version": "v2",
        "auto_detect": True,
        "model_id": None,
    }
    assert "Invoice (pages 1-2)" in result.output
    assert "Summary: 2/2 succeeded" in result.output
    assert sorted(p.name for p in out.iterdir()) == ["Invoice.json", "W2.json"]


@pytest.mark.unit
def test_a_path_like_id_from_a_real_discovery_result_stays_under_the_output_directory(
    runner, sdk, tmp_path
):
    """The containment holds on the command path, not only on the writer.

    The writer tests above call `_write_discover_output` with dicts. This one
    carries the same `$id` through the real `DiscoveryResult` the SDK returns and
    the whole `discover` command, which is what establishes that a model-generated
    `$id` reaches the filename at all — the claim the fix rests on. The `-o`
    directory is asserted to be the file's parent after resolution, and the
    parent of `-o` is asserted to hold nothing new.
    """
    from idp_cli.cli import discover
    from idp_common.config.class_names import sanitize_class_name

    escaping_id = "../escaped"
    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=1,
        succeeded=1,
        failed=0,
        results=[
            DiscoveryResult(
                status="SUCCESS",
                document_class="Invoice",
                json_schema={"$id": escaping_id, "properties": {}},
                page_range="1-2",
            ),
        ],
    )
    doc = _doc(tmp_path, "package.pdf")
    out = tmp_path / "schemas"
    out.mkdir()

    result = runner.invoke(discover, ["-d", doc, "--auto-detect", "-o", str(out)])

    assert result.exit_code == 0, result.output
    written = out / f"{sanitize_class_name(escaping_id)}.json"
    assert written.resolve().parent == out.resolve()
    assert [p.name for p in out.iterdir()] == [written.name]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["package.pdf", "schemas"]


@pytest.mark.unit
def test_auto_detect_in_stack_mode_echoes_the_stack_and_claims_the_save(
    runner, sdk, tmp_path
):
    """The save claim in auto-detect mode needs all three of stack, profile, success.

    `--config-profile` on its own persists nothing, because the SDK only saves when
    it has a stack to save into. A "saved to configuration" line printed without a
    stack would tell the user their deployment had been reconfigured when it had
    not, so the condition is asserted in both directions: here all three hold and
    the line appears, and `test_a_local_run_without_a_stack_...` covers the case
    where it must not.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=1,
        succeeded=1,
        failed=0,
        results=[
            DiscoveryResult(
                status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
            )
        ],
    )

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--auto-detect",
            "--stack-name",
            "my-stack",
            "--config-profile",
            "v4",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sdk.construction == {"stack_name": "my-stack", "region": None}
    assert "Stack: my-stack" in result.output
    assert "saved to configuration (version: v4)" in result.output


@pytest.mark.unit
def test_auto_detect_with_a_failed_section_exits_one_after_writing_the_rest(
    runner, sdk, tmp_path
):
    """One failed section is enough to make the whole run non-zero."""
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=2,
        succeeded=1,
        failed=1,
        results=[
            DiscoveryResult(
                status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
            ),
            DiscoveryResult(status="FAILED", error="blank pages", page_range="3-5"),
        ],
    )

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "package.pdf"), "--auto-detect"]
    )

    assert result.exit_code == 1, result.output
    assert "Failed (pages 3-5): blank pages" in result.output
    assert "Summary: 1/2 succeeded" in result.output


@pytest.mark.unit
def test_auto_detect_finding_no_sections_exits_zero_having_produced_nothing(
    runner, sdk, tmp_path
):
    """DEFECT: zero sections is reported as success with no schema and no file.

    An empty `DiscoveryBatchResult` has `failed == 0`, so the `failed > 0` check at
    `cli.py:5471` does not fire, and the empty schema list makes
    `_write_discover_output` a no-op. The command prints "Summary: 0/0 succeeded"
    and exits 0 with nothing written to the `-o` path.

    The consequence is the same as for a success carrying no schema: a caller that
    chains on the exit code proceeds as though a schema directory existed.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=0, succeeded=0, failed=0, results=[]
    )
    out = tmp_path / "schemas"

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "package.pdf"), "--auto-detect", "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert "Summary: 0/0 succeeded" in result.output
    assert not out.exists()


@pytest.mark.unit
def test_auto_detect_silently_discards_ground_truth_and_the_class_hint(
    runner, sdk, tmp_path
):
    """DEFECT: `-g` and `--class-hint` are accepted with `--auto-detect` and dropped.

    The auto-detect arm calls `client.discovery.run` with only
    `document_path`, `config_version`, `auto_detect` and `model_id`
    (`cli.py:5438-5443`). A ground truth file and a class-name hint the user
    supplied on the same command line are never passed on, and nothing is printed
    about it — the header does not mention either.

    The consequence is a schema inferred without the ground truth the user
    explicitly provided, which is the exact failure issue #310 was filed about for
    the standard path: measurably worse extraction quality, with no signal in the
    output that the ground truth was ignored. Click accepts the combination, so
    there is not even a usage error to notice.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=1,
        succeeded=1,
        failed=0,
        results=[DiscoveryResult(status="SUCCESS", json_schema=SCHEMA_A)],
    )
    gt = tmp_path / "package.json"
    gt.write_text("{}", encoding="utf-8")

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "-g",
            str(gt),
            "--class-hint",
            "Lending Package",
            "--auto-detect",
        ],
    )

    assert result.exit_code == 0, result.output
    passed = sdk.client.discovery.run.call_args.kwargs
    assert "ground_truth_path" not in passed
    assert "class_name_hint" not in passed
    assert "Ground truth" not in result.output
    assert "Class hint" not in result.output


@pytest.mark.unit
def test_detect_only_without_auto_detect_runs_a_full_discovery_instead(
    runner, sdk, tmp_path
):
    """DEFECT: `--detect-only` alone is ignored and a paid discovery runs.

    `--detect-only` is only consulted inside the `if auto_detect:` block
    (`cli.py:5397`). Given on its own it falls through to standard discovery, so a
    user who asked for the cheap boundary-detection step gets a full
    schema-inference call against Bedrock — the opposite of what the flag is for,
    at a cost, and with no warning. The help text says "use with --auto-detect"
    but the command does not enforce it.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryResult(
        status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
    )

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "package.pdf"), "--detect-only"]
    )

    assert result.exit_code == 0, result.output
    assert sdk.client.discovery.auto_detect_sections.call_args_list == []
    assert sdk.client.discovery.run.call_count == 1
    assert sdk.client.discovery.run.call_args.kwargs["document_path"].endswith(
        "package.pdf"
    )
    assert "Discovery completed successfully" in result.output


@pytest.mark.unit
def test_auto_detect_takes_precedence_over_page_range_without_saying_so(
    runner, sdk, tmp_path
):
    """DEFECT: `--auto-detect` and `--page-range` together silently ignore the ranges.

    The `if auto_detect:` block returns before the `if page_range:` block is
    reached (`cli.py:5381` vs `cli.py:5476`), so explicit page ranges are
    discarded. These two options are alternative ways of deciding the same thing —
    where the sections are — so giving both is a contradiction the command should
    refuse rather than resolve by source order. The user gets AI-detected
    boundaries while believing they pinned them by hand, and the printed header
    says "Auto-Detect Sections" without mentioning the ranges it dropped.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run.return_value = DiscoveryBatchResult(
        total=1,
        succeeded=1,
        failed=0,
        results=[DiscoveryResult(status="SUCCESS", json_schema=SCHEMA_A)],
    )

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--auto-detect",
            "--page-range",
            "1-2",
            "--page-label",
            "W2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Auto-Detect Sections" in result.output
    assert sdk.client.discovery.run_multi_section.call_args_list == []
    assert sdk.client.discovery.run.call_args.kwargs["auto_detect"] is True
    assert "Page ranges" not in result.output


# ---------------------------------------------------------------------------
# discover --page-range
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_page_range_refuses_more_than_one_document(runner, sdk, tmp_path):
    """Page numbers only mean something against one document."""
    from idp_cli.cli import discover

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "a.pdf"),
            "-d",
            _doc(tmp_path, "b.pdf"),
            "--page-range",
            "1-2",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "--page-range works with a single document only" in result.output
    sdk.assert_no_discovery()


@pytest.mark.unit
def test_page_ranges_are_parsed_into_start_end_and_label_triples(runner, sdk, tmp_path):
    """The parse is the whole of this mode's input handling, so pin every case.

    Three shapes matter: `"1-2"` becomes start 1 / end 2; a bare `"7"` becomes
    start *and* end 7 rather than an error; and a range with no corresponding
    `--page-label` gets `label=None` rather than an index error. Labels are
    positional, so a mis-parse silently attaches a label to the wrong pages —
    which would name a schema after the wrong document class.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run_multi_section.return_value = DiscoveryBatchResult(
        total=3,
        succeeded=3,
        failed=0,
        results=[
            DiscoveryResult(status="SUCCESS", json_schema=SCHEMA_A),
            DiscoveryResult(status="SUCCESS", json_schema=SCHEMA_B),
            DiscoveryResult(status="SUCCESS", json_schema={"$id": "Third"}),
        ],
    )
    doc = _doc(tmp_path, "package.pdf")

    result = runner.invoke(
        discover,
        [
            "-d",
            doc,
            "--page-range",
            "1-2",
            "--page-label",
            "Cover Letter",
            "--page-range",
            " 3-5 ",
            "--page-label",
            "W2 Form",
            "--page-range",
            "7",
        ],
    )

    assert result.exit_code == 0, result.output
    call = sdk.client.discovery.run_multi_section.call_args.kwargs
    assert call["document_path"] == doc
    assert call["page_ranges"] == [
        {"start": 1, "end": 2, "label": "Cover Letter"},
        {"start": 3, "end": 5, "label": "W2 Form"},
        {"start": 7, "end": 7, "label": None},
    ]
    assert "Page ranges: 3" in result.output
    assert "Range 1: pages 1-2 → Cover Letter" in result.output
    assert "Range 3: pages 7-7" in result.output


@pytest.mark.unit
def test_more_page_labels_than_ranges_drops_the_extra_labels_silently(
    runner, sdk, tmp_path
):
    """DEFECT: an unmatched `--page-label` is discarded without a word.

    Labels are paired to ranges by index (`cli.py:5496`), and a label beyond the
    last range is simply never read. Since the two options are repeated and
    order-dependent, a user who adds a label but forgets its range — or who lists
    them in two separate blocks — loses the class-name hint for that section and
    gets a model-chosen `$id` instead, which then becomes the schema's filename.
    """
    from idp_cli.cli import discover

    sdk.client.discovery.run_multi_section.return_value = DiscoveryBatchResult(
        total=1,
        succeeded=1,
        failed=0,
        results=[DiscoveryResult(status="SUCCESS", json_schema=SCHEMA_A)],
    )

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--page-range",
            "1-2",
            "--page-label",
            "Cover Letter",
            "--page-label",
            "Orphaned Label",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sdk.client.discovery.run_multi_section.call_args.kwargs["page_ranges"] == [
        {"start": 1, "end": 2, "label": "Cover Letter"}
    ]
    assert "Orphaned Label" not in result.output


@pytest.mark.unit
def test_an_unparseable_page_range_exits_one_with_a_message_about_int(
    runner, sdk, tmp_path
):
    """DEFECT: a malformed `--page-range` surfaces as a raw `int()` ValueError.

    `int(parts[0])` at `cli.py:5499` is unguarded, so `--page-range "first"` is
    caught only by the command's blanket `except Exception` and printed as
    "✗ Error: invalid literal for int() with base 10: 'first'". The exit code is
    correct; the message names neither the option nor the expected `start-end`
    form, which for a repeatable option is the thing the user needs to know. The
    same happens for `"3-"`, where `int("")` fails.
    """
    from idp_cli.cli import discover

    result = runner.invoke(
        discover, ["-d", _doc(tmp_path, "package.pdf"), "--page-range", "first"]
    )

    assert result.exit_code == 1, result.output
    assert "invalid literal for int()" in result.output
    assert "--page-range" not in result.output.split("Error:")[-1]
    sdk.assert_no_discovery()


@pytest.mark.unit
def test_a_failed_page_range_section_exits_one(runner, sdk, tmp_path):
    """Multi-section mode uses the same failed-count rule as auto-detect."""
    from idp_cli.cli import discover

    sdk.client.discovery.run_multi_section.return_value = DiscoveryBatchResult(
        total=2,
        succeeded=1,
        failed=1,
        results=[
            DiscoveryResult(
                status="SUCCESS", document_class="Invoice", json_schema=SCHEMA_A
            ),
            DiscoveryResult(
                status="FAILED", error="page 9 out of range", page_range="9-9"
            ),
        ],
    )

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--page-range",
            "1-2",
            "--page-range",
            "9",
        ],
    )

    assert result.exit_code == 1, result.output
    assert "Failed (pages 9-9): page 9 out of range" in result.output
    assert "Summary: 1/2 succeeded" in result.output


@pytest.mark.unit
def test_page_range_stack_mode_reports_the_save_and_forwards_the_model(
    runner, sdk, tmp_path
):
    """Stack plus profile plus at least one success is what earns the save message."""
    from idp_cli.cli import discover

    sdk.client.discovery.run_multi_section.return_value = DiscoveryBatchResult(
        total=1,
        succeeded=1,
        failed=0,
        results=[DiscoveryResult(status="SUCCESS", json_schema=SCHEMA_A)],
    )

    result = runner.invoke(
        discover,
        [
            "-d",
            _doc(tmp_path, "package.pdf"),
            "--page-range",
            "1-2",
            "--stack-name",
            "my-stack",
            "--config-profile",
            "v3",
            "--model-id",
            "m-9",
            "--region",
            "us-west-2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert sdk.construction == {"stack_name": "my-stack", "region": "us-west-2"}
    call = sdk.client.discovery.run_multi_section.call_args.kwargs
    assert call["config_version"] == "v3"
    assert call["model_id"] == "m-9"
    assert "Stack: my-stack" in result.output
    assert "Model ID override: m-9" in result.output
    assert "saved to configuration (version: v3)" in result.output


# ---------------------------------------------------------------------------
# discover-multidoc — input selection
# ---------------------------------------------------------------------------

#: `multi_discover` builds its own `rich.Console()` rather than using the module
#: console `conftest`'s autouse fixture pins, so its width comes from the
#: environment. Rich reads `COLUMNS` last and lets it win, so passing this to
#: `runner.invoke(env=...)` is what keeps the results table from being ellipsized
#: at 80 columns — the same sensitivity that fixture exists to remove.
WIDE = {"COLUMNS": "200"}


def _multidoc_result(**kwargs) -> MultiDocDiscoveryResult:
    defaults = {
        "status": "SUCCESS",
        "discovered_classes": [
            DiscoveredClassResult(
                cluster_id=0,
                classification="Invoice",
                json_schema=SCHEMA_A,
                document_count=4,
            )
        ],
        "total_documents": 4,
        "total_clusters": 1,
    }
    defaults.update(kwargs)
    return MultiDocDiscoveryResult(**defaults)


@pytest.mark.unit
def test_multidoc_with_neither_dir_nor_document_refuses_before_building_a_client(
    runner, sdk
):
    """An empty selection is refused with exit 1, not a usage error.

    Both input options are optional as far as click is concerned, so the
    "one of these is required" rule is the command's own. The refusal happens
    before `IDPClient` is constructed, which is what makes it free.
    """
    from idp_cli.cli import multi_discover

    result = runner.invoke(multi_discover, [], env=WIDE)

    assert result.exit_code == 1, result.output
    assert "Either --dir or --document/-d must be provided" in result.output
    sdk.assert_never_constructed()


@pytest.mark.unit
def test_multidoc_accepts_both_dir_and_document_and_forwards_both(
    runner, sdk, tmp_path
):
    """DEFECT: an ambiguous selection is accepted and passed on whole.

    The guard at `cli.py:5863` only rejects the case where *neither* input was
    given. Supplying `--dir` and `-d` together is not refused, and both reach
    `run_multi_doc(document_dir=..., document_paths=...)` even though the
    operation's own contract says "Provide either ``document_dir`` or
    ``document_paths``, not both"
    (`lib/idp_sdk/idp_sdk/operations/discovery.py:717-719`).

    The consequence is that which corpus was actually clustered is decided
    downstream, silently, and the user cannot tell from the CLI output which of
    their two selections was used — the summary reports a document count without
    saying where the documents came from. Both options are also repeatable-adjacent
    in the examples, so giving both is an easy mistake to make while iterating.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    extra = _doc(tmp_path, "extra.pdf")

    result = runner.invoke(
        multi_discover, ["--dir", str(corpus), "-d", extra], env=WIDE
    )

    assert result.exit_code == 0, result.output
    call = sdk.client.discovery.run_multi_doc.call_args.kwargs
    assert call["document_dir"] == str(corpus)
    assert call["document_paths"] == [extra]


@pytest.mark.unit
def test_multidoc_save_to_config_without_a_stack_name_is_refused(runner, sdk, tmp_path):
    """There is nowhere to save without a stack, so refuse before spending a model call."""
    from idp_cli.cli import multi_discover

    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(
        multi_discover,
        ["--dir", str(corpus), "--save-to-config", "--config-profile", "v2"],
        env=WIDE,
    )

    assert result.exit_code == 1, result.output
    assert "--stack-name is required when using --save-to-config" in result.output
    sdk.assert_never_constructed()


@pytest.mark.unit
def test_multidoc_save_to_config_without_a_config_profile_is_refused(
    runner, sdk, tmp_path
):
    """The profile names the configuration record, so it is required too."""
    from idp_cli.cli import multi_discover

    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(
        multi_discover,
        ["--dir", str(corpus), "--save-to-config", "--stack-name", "my-stack"],
        env=WIDE,
    )

    assert result.exit_code == 1, result.output
    assert "--config-profile is required when using --save-to-config" in result.output
    sdk.assert_never_constructed()


@pytest.mark.unit
def test_multidoc_refuses_a_directory_that_does_not_exist(runner, sdk, tmp_path):
    """`--dir` is `click.Path(exists=True, file_okay=False)`, so a file is refused too."""
    from idp_cli.cli import multi_discover

    missing = runner.invoke(multi_discover, ["--dir", str(tmp_path / "nope")], env=WIDE)
    assert missing.exit_code == 2, missing.output

    a_file = runner.invoke(
        multi_discover, ["--dir", _doc(tmp_path, "one.pdf")], env=WIDE
    )
    assert a_file.exit_code == 2, a_file.output
    assert "is a file" in a_file.output

    sdk.assert_never_constructed()


@pytest.mark.unit
def test_the_two_multidoc_model_ids_and_the_region_reach_the_operation(
    runner, sdk, tmp_path
):
    """Two different models are selectable here and they must not be swapped.

    `--embedding-model` decides how documents are compared and `--analysis-model`
    decides which LLM writes the schema; they are different model families
    (Cohere embeddings versus a Claude text model), so transposing them would fail
    at the Bedrock call with an opaque error. `--region` is passed twice — once to
    the client and once to the operation — and both are asserted.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result()
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(
        multi_discover,
        [
            "--dir",
            str(corpus),
            "--embedding-model",
            "us.cohere.embed-v4:0",
            "--analysis-model",
            "us.anthropic.claude-sonnet-4-6",
            "--region",
            "ap-southeast-2",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert sdk.construction == {"stack_name": None, "region": "ap-southeast-2"}
    call = sdk.client.discovery.run_multi_doc.call_args.kwargs
    assert call["embedding_model_id"] == "us.cohere.embed-v4:0"
    assert call["analysis_model_id"] == "us.anthropic.claude-sonnet-4-6"
    assert call["region"] == "ap-southeast-2"
    assert call["save_to_config"] is False
    assert call["config_version"] is None


@pytest.mark.unit
def test_multidoc_with_explicit_documents_sends_a_list_and_no_directory(
    runner, sdk, tmp_path
):
    """`-d` repeated becomes `document_paths`, and `document_dir` stays `None`."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result()
    one = _doc(tmp_path, "one.pdf")
    two = _doc(tmp_path, "two.png")

    result = runner.invoke(multi_discover, ["-d", one, "-d", two], env=WIDE)

    assert result.exit_code == 0, result.output
    call = sdk.client.discovery.run_multi_doc.call_args.kwargs
    assert call["document_dir"] is None
    assert call["document_paths"] == [one, two]


@pytest.mark.unit
def test_multidoc_save_to_config_forwards_the_flag_and_the_profile(
    runner, sdk, tmp_path
):
    """With stack and profile present, the save request is passed through."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        config_version="v2"
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(
        multi_discover,
        [
            "--dir",
            str(corpus),
            "--save-to-config",
            "--stack-name",
            "my-stack",
            "--config-version",
            "v2",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert sdk.construction == {"stack_name": "my-stack", "region": None}
    call = sdk.client.discovery.run_multi_doc.call_args.kwargs
    assert call["save_to_config"] is True
    assert call["config_version"] == "v2"
    # Only reported because the *result* carried a config_version back.
    assert "Schemas saved to config profile: v2" in result.output


@pytest.mark.unit
def test_a_missing_idp_sdk_points_at_the_local_checkout_and_exits_one(
    runner, sdk, tmp_path, monkeypatch
):
    """The import-failure message must not suggest `pip install idp-sdk`.

    `idp-sdk` on public PyPI belongs to an unrelated party, so a message telling a
    user to install it by name is a dependency-confusion instruction. The arm at
    `cli.py:5883-5891` names the local checkout instead, and also prints the
    underlying error so the real cause (a missing extra, usually) is visible.

    The branch is reached by putting a module object with no `IDPClient` attribute
    in `sys.modules`: `from idp_sdk import IDPClient` then raises `ImportError`
    from the attribute lookup, which is the same exception a genuinely broken or
    squatted install produces.
    """
    from idp_cli.cli import multi_discover

    hollow = types.ModuleType("idp_sdk")
    monkeypatch.setitem(sys.modules, "idp_sdk", hollow)
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 1, result.output
    assert "idp-sdk is required" in result.output
    assert "make setup" in result.output
    assert "pip install -e lib/idp_sdk" in result.output
    assert "pip install idp-sdk" not in result.output
    assert "Underlying import error" in result.output
    sdk.assert_never_constructed()


# ---------------------------------------------------------------------------
# discover-multidoc — results, output and failure handling
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_failed_multidoc_run_exits_one_and_prints_no_results_table(
    runner, sdk, tmp_path
):
    """`status == "FAILED"` stops before the table, so there is nothing to misread."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = MultiDocDiscoveryResult(
        status="FAILED", error="multi_document_discovery extra is not installed"
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 1, result.output
    assert "Discovery failed: multi_document_discovery extra is not installed" in (
        result.output
    )
    assert "Multi-Document Discovery Results" not in result.output


@pytest.mark.unit
def test_the_results_table_lists_each_cluster_with_its_document_and_field_counts(
    runner, sdk, tmp_path
):
    """The table is the command's primary answer: what was found, and how big.

    The field count is derived from the schema's own `properties` at render time
    rather than reported by the SDK, so it is the CLI's own arithmetic and worth
    pinning: `SCHEMA_A` has two properties, `SCHEMA_B` has one.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        discovered_classes=[
            DiscoveredClassResult(
                cluster_id=0,
                classification="Invoice",
                json_schema=SCHEMA_A,
                document_count=4,
            ),
            DiscoveredClassResult(
                cluster_id=1,
                classification="W2",
                json_schema=SCHEMA_B,
                document_count=2,
            ),
        ],
        total_documents=6,
        total_clusters=2,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Multi-Document Discovery Results" in result.output
    rows = [line for line in result.output.splitlines() if "Invoice" in line]
    assert rows, result.output
    assert "Summary: 6 documents → 2 clusters → 2 schemas" in result.output


@pytest.mark.unit
def test_a_cluster_whose_analysis_errored_renders_as_an_error_row(
    runner, sdk, tmp_path
):
    """An errored cluster shows dashes rather than a plausible-looking zero.

    Printing "0 fields" for a cluster that failed would read as a document class
    with no fields, which is a different and much less alarming statement than
    "this cluster produced nothing". The errored cluster is also excluded from the
    schema count in the summary line, so "2 clusters → 1 schemas" is the honest
    arithmetic.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        discovered_classes=[
            DiscoveredClassResult(
                cluster_id=0,
                classification="Invoice",
                json_schema=SCHEMA_A,
                document_count=4,
            ),
            DiscoveredClassResult(
                cluster_id=1,
                classification=None,
                json_schema=None,
                document_count=3,
                error="agent exceeded max tokens",
            ),
        ],
        total_documents=7,
        total_clusters=2,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "✗ Error" in result.output
    assert "Summary: 7 documents → 2 clusters → 1 schemas" in result.output


@pytest.mark.unit
def test_a_cluster_with_no_classification_renders_as_unknown(runner, sdk, tmp_path):
    """A schema produced without a class name is labelled, not left blank."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        discovered_classes=[
            DiscoveredClassResult(
                cluster_id=0,
                classification=None,
                json_schema=SCHEMA_A,
                document_count=2,
            )
        ],
        total_documents=2,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    # The label belongs to the cluster's own row, next to its id and counts, not
    # to some other line of the output.
    rows = [
        line
        for line in result.output.splitlines()
        if "Unknown" in line and line.lstrip().startswith("│")
    ]
    assert len(rows) == 1, result.output
    assert "✓" in rows[0]


@pytest.mark.unit
def test_a_partial_multidoc_run_prints_the_table_then_exits_one(runner, sdk, tmp_path):
    """`PARTIAL` is reported after the results, because the partial results are useful."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        status="PARTIAL",
        discovered_classes=[
            DiscoveredClassResult(
                cluster_id=0,
                classification="Invoice",
                json_schema=SCHEMA_A,
                document_count=4,
            ),
            DiscoveredClassResult(cluster_id=1, document_count=2, error="boom"),
        ],
        total_documents=6,
        total_clusters=2,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 1, result.output
    assert "Multi-Document Discovery Results" in result.output
    assert "Some clusters failed analysis" in result.output


@pytest.mark.unit
def test_schemas_print_to_stdout_as_an_array_when_nothing_else_was_asked_for(
    runner, sdk, tmp_path
):
    """With no `-o` and no `--save-to-config`, stdout is the only output.

    The array is emitted through `emit_json`, so it is parseable even though the
    table above it is not — the command is usable in a pipeline. Errored clusters
    are excluded, which is what keeps a `null` out of the array.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        discovered_classes=[
            DiscoveredClassResult(
                cluster_id=0,
                classification="Invoice",
                json_schema=SCHEMA_A,
                document_count=4,
            ),
            DiscoveredClassResult(
                cluster_id=1,
                classification="W2",
                json_schema=SCHEMA_B,
                document_count=2,
            ),
            DiscoveredClassResult(cluster_id=2, document_count=1, error="boom"),
        ],
        total_documents=7,
        total_clusters=3,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    tail = result.output.split("Discovered schemas")[1]
    assert _first_json_value(tail) == [SCHEMA_A, SCHEMA_B]


@pytest.mark.unit
def test_save_to_config_suppresses_the_stdout_schema_dump(runner, sdk, tmp_path):
    """A schema already persisted to the stack is not also dumped to stdout."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        config_version="v2"
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(
        multi_discover,
        [
            "--dir",
            str(corpus),
            "--save-to-config",
            "--stack-name",
            "my-stack",
            "--config-profile",
            "v2",
        ],
        env=WIDE,
    )

    assert result.exit_code == 0, result.output
    assert "Discovered schemas" not in result.output


@pytest.mark.unit
def test_documents_that_failed_embedding_are_reported_as_a_warning(
    runner, sdk, tmp_path
):
    """`noise_documents` is reported so a silently-shrunk corpus is visible.

    The count is documents the pipeline could not place in any cluster. The CLI
    calls them "documents failed embedding" while the model field documents them
    as "couldn't be clustered (noise/outliers)" — two different causes, and the
    wording here picks one of them. Either way the number reaching the user is the
    field's, which is what this asserts.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        noise_documents=3, total_documents=7
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "3 documents failed embedding" in result.output


@pytest.mark.unit
def test_zero_noise_documents_prints_no_warning(runner, sdk, tmp_path):
    """The guard is `> 0`, so a clean run says nothing about noise."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        noise_documents=0
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "failed embedding" not in result.output


@pytest.mark.unit
def test_the_reflection_report_is_rendered_as_markdown(runner, sdk, tmp_path):
    """The report is the qualitative half of the answer and is printed when present."""
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        reflection_report="# Findings\n\nTwo clusters look like the same class."
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Reflection Report" in result.output
    assert "Two clusters look like the same class." in result.output


@pytest.mark.unit
def test_output_is_reported_as_written_even_when_no_schema_was_discovered(
    runner, sdk, tmp_path
):
    """DEFECT: `-o` always prints "✓ Schemas written to", whatever happened.

    Unlike `discover`, this command does not write the schemas itself — it hands
    `output_dir` to the SDK and then prints the success line unconditionally at
    `cli.py:6032-6033`, with no check that the directory exists or holds anything.
    Here every cluster errored, so no schema was produced, and the command still
    claims the schemas were written and exits 0.

    The consequence is that the one line a user or a script would check to confirm
    the run produced files is not evidence of anything. The directory asserted
    below does not exist at all.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        discovered_classes=[
            DiscoveredClassResult(cluster_id=0, document_count=2, error="boom")
        ],
        total_documents=2,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    out = tmp_path / "schemas"

    result = runner.invoke(
        multi_discover, ["--dir", str(corpus), "-o", str(out)], env=WIDE
    )

    assert result.exit_code == 0, result.output
    assert f"Schemas written to: {out}" in result.output
    assert not out.exists(), "the success line named a directory that does not exist"
    assert sdk.client.discovery.run_multi_doc.call_args.kwargs["output_dir"] == str(out)


@pytest.mark.unit
def test_finding_no_clusters_at_all_exits_zero_with_an_empty_table(
    runner, sdk, tmp_path
):
    """DEFECT: a corpus that yields no document class is reported as success.

    The only non-zero exits in this command are `status == "FAILED"` and
    `status == "PARTIAL"`. A `SUCCESS` with an empty `discovered_classes` — which
    is what a corpus of fewer than two similar documents produces, since the
    pipeline discards clusters smaller than two as noise — prints an empty table,
    the line "0 documents → 0 clusters → 0 schemas", and exits 0.

    The consequence is that `idp-cli discover-multidoc --dir ./samples -o ./schemas
    && deploy ./schemas` proceeds with an empty schema set on a zero exit code. The
    most likely cause of this outcome is also the least obvious one — the
    two-documents-per-class requirement is in the help text, not in the output —
    so the user is given no hint about what to change.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = MultiDocDiscoveryResult(
        status="SUCCESS", discovered_classes=[], total_documents=0, total_clusters=0
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Summary: 0 documents → 0 clusters → 0 schemas" in result.output
    assert "Discovered schemas" not in result.output


@pytest.mark.unit
def test_a_single_cluster_holding_a_single_document_is_reported_as_found(
    runner, sdk, tmp_path
):
    """The smallest non-empty grouping still renders a row and exits 0.

    The pipeline normally discards a cluster of one as noise, so this is the shape
    a caller sees when that filter is relaxed or bypassed. The CLI applies no
    minimum of its own: it reports one document, one cluster, one schema. A test
    that this is *not* refused is worth having because the docstring's
    "at least 2 documents per expected class" is a property of the clustering
    stage, not a validation the command performs.
    """
    from idp_cli.cli import multi_discover

    sdk.client.discovery.run_multi_doc.return_value = _multidoc_result(
        discovered_classes=[
            DiscoveredClassResult(
                cluster_id=0,
                classification="Invoice",
                json_schema=SCHEMA_A,
                document_count=1,
            )
        ],
        total_documents=1,
        total_clusters=1,
    )
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert "Summary: 1 documents → 1 clusters → 1 schemas" in result.output
    assert _first_json_value(result.output.split("Discovered schemas")[1]) == [SCHEMA_A]


@pytest.mark.unit
def test_every_progress_step_the_pipeline_emits_is_handled(runner, sdk, tmp_path):
    """The progress callback must accept every step name the pipeline sends.

    `_progress_callback` is a chain of `elif`s over string literals with no final
    `else`, so an unrecognised step is silently ignored — which means a renamed
    step degrades the progress display rather than raising, and would not be
    noticed. The step names below are the full set emitted by
    `idp_common/discovery/multi_document_discovery.py`, including the six the CLI
    does not handle, so a rename on either side shows up here as a missing
    handler.

    A failure of this test means either that a handler raised — `data` arriving as
    `None` for the no-payload steps is the realistic way that happens, and
    `data = data or {}` is what prevents it — or, if the assertion on the
    handled set fails, that the two sides have drifted apart.
    """
    from idp_cli.cli import multi_discover

    emitted = [
        ("listing_documents", {"dir": "corpus", "paths": None}),
        ("documents_found", {"count": 6}),
        ("generating_embeddings", {"total": 6}),
        ("embedding_progress", {"done": 3, "total": 6}),
        ("embeddings_complete", {"valid": 6}),
        ("clustering", {"num_documents": 6}),
        ("clustering_complete", {"num_clusters": 2}),
        ("preparing_images", None),
        ("analyzing_clusters", {"total": 2}),
        (
            "cluster_analysis_progress",
            {"done": 1, "total": 2, "classification": "Invoice"},
        ),
        ("cluster_analysis_progress", {"done": 2, "total": 2, "classification": ""}),
        ("analysis_complete", {"classes": []}),
        ("reflecting", None),
        ("reflection_complete", None),
        ("saving_to_config", {"version": "v2"}),
        ("save_complete", None),
        ("pipeline_complete", {"status": "SUCCESS"}),
        # Not emitted by the pipeline today; proves an unknown step is a no-op
        # rather than a crash, which is what the missing `else` arm means.
        ("a_step_that_does_not_exist", {"anything": 1}),
    ]
    seen = []

    def _run(**kwargs):
        callback = kwargs["progress_callback"]
        for step, data in emitted:
            callback(step, data)
            seen.append(step)
        return _multidoc_result()

    sdk.client.discovery.run_multi_doc.side_effect = _run
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    result = runner.invoke(multi_discover, ["--dir", str(corpus)], env=WIDE)

    assert result.exit_code == 0, result.output
    assert seen == [step for step, _ in emitted]
    # The steps the CLI turns into a description, as of today.
    handled = {
        "documents_found",
        "generating_embeddings",
        "embedding_progress",
        "clustering",
        "clustering_complete",
        "analyzing_clusters",
        "cluster_analysis_progress",
        "reflecting",
        "saving_to_config",
        "pipeline_complete",
    }
    source = Path(sys.modules["idp_cli.cli"].__file__).read_text(encoding="utf-8")
    body = source.split("def multi_discover(")[1].split("\n@cli.command")[0]
    for step in handled:
        assert f'step == "{step}"' in body, f"{step} is no longer handled"
