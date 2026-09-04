# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Launch-time integrity check: the config FILE must match the index that names it.

This exists because of a real, silently-wrong benchmark comparison. Suites share
cell names (``core_cells`` backs corefast, core and coresynth), and config files
were named ``<cell>__<class>.yaml`` with no suite in the name. So:

    make_configs.py --suite coresynth --set extraction_model=sonnet5   # writes files + index
    make_configs.py --suite corefast                                   # OVERWRITES the same files

left coresynth's index advertising ``extraction_model: sonnet5`` while the file it
pointed at was pinned to sonnet-4-6. The run then executed on sonnet-4-6, recorded
"sonnet5" in its metadata, and the resulting before/after comparison silently
spanned two different models — every number attributed to the wrong configuration,
with nothing in the output looking unusual.

Filenames are namespaced per suite now, and this check is the backstop for any
other way the two can drift (hand-edited file, partial rebuild, stale index).
"""

import sys

import pytest
import yaml

sys.path.insert(0, "benchmarks/harness")


@pytest.fixture(scope="module")
def rm():
    import run_matrix

    return run_matrix


def _cell(tmp_path, resolved, cfg):
    p = tmp_path / "cell.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return [{"cell": "c1", "path": str(p), "resolved": resolved}]


@pytest.mark.unit
def test_matching_config_passes(rm, tmp_path, capsys):
    cells = _cell(
        tmp_path,
        {"extraction_model": "sonnet5"},
        {"extraction": {"model": "us.anthropic.claude-sonnet-5"}},
    )
    rm.verify_config_axes(cells)
    assert "match their index" in capsys.readouterr().out


@pytest.mark.unit
def test_model_mismatch_aborts(rm, tmp_path):
    """The exact bug: index says sonnet5, file is pinned to sonnet-4-6."""
    cells = _cell(
        tmp_path,
        {"extraction_model": "sonnet5"},
        {"extraction": {"model": "us.anthropic.claude-sonnet-4-6"}},
    )
    with pytest.raises(SystemExit) as e:
        rm.verify_config_axes(cells)
    msg = str(e.value)
    assert "extraction.model" in msg
    assert "sonnet-4-6" in msg and "sonnet-5" in msg


@pytest.mark.unit
def test_reshaped_knob_is_not_a_false_positive(rm, tmp_path):
    """``ocr.features`` is written as [{name: X}], not the raw axis list.

    The check compares against what make_configs.set_path would actually write,
    so a shape transform can never be mistaken for a value difference.
    """
    cells = _cell(
        tmp_path,
        {"ocr": "textract_tables"},
        {
            # The axis sets all three of these; ocr.features is the reshaped one.
            "use_bda": False,
            "ocr": {
                "backend": "textract",
                "features": [{"name": "TABLES"}, {"name": "LAYOUT"}],
            },
        },
    )
    rm.verify_config_axes(cells)  # must not raise


@pytest.mark.unit
def test_missing_config_file_aborts(rm, tmp_path):
    cells = [
        {"cell": "c1", "path": str(tmp_path / "nope.yaml"), "resolved": {"ocr": "bda"}}
    ]
    with pytest.raises(SystemExit) as e:
        rm.verify_config_axes(cells)
    assert "missing" in str(e.value)


@pytest.mark.unit
def test_unknown_axis_value_is_skipped_not_failed(rm, tmp_path):
    """An axis the matrix doesn't express as config paths can't be checked."""
    cells = _cell(
        tmp_path, {"not_a_real_axis": "whatever"}, {"extraction": {"model": "x"}}
    )
    rm.verify_config_axes(cells)  # must not raise


@pytest.mark.unit
def test_generated_configs_are_suite_namespaced():
    """Two suites must not be able to overwrite each other's config files."""
    import make_configs

    assert "args.suite" in open(make_configs.__file__).read(), (
        "the config filename must include the suite, or two suites sharing a cell "
        "name will overwrite each other's files and desync from their indexes"
    )


# --------------------------------------------------------------------------- #
# --set overrides must namespace the output
#
# The FILE was namespaced by suite, but nothing was namespaced by `--set`, and the
# uploaded config VERSION name was namespaced by neither. So two runs of one suite
# differing only in `--set` overwrote the same file, the same index, AND the same
# `Config#<version>` on the stack -- the second silently relabelling the first.
#
# This is the same silent-cross-model-comparison failure the suite namespacing was
# added to fix, reached through a different door. `verify_config_axes` cannot catch
# it: the file and the index are rewritten together and therefore agree with each
# other while both disagree with the run that is already in flight. Hit live while
# building a two-model boundary A/B; caught before any config was uploaded.
# --------------------------------------------------------------------------- #
class TestOverrideSlug:
    @staticmethod
    def _slug():
        mc = pytest.importorskip("make_configs")
        return mc.override_slug

    def test_no_overrides_is_byte_identical_to_before(self):
        """Every existing path and version name must be unchanged."""
        assert self._slug()([]) == ""
        assert self._slug()(()) == ""

    def test_an_override_produces_a_distinct_suffix(self):
        assert self._slug()(["classification_model=sonnet5"]) == (
            "__classification-model-sonnet5"
        )

    def test_two_different_overrides_cannot_collide(self):
        a = self._slug()(["classification_model=sonnet5"])
        b = self._slug()(["classification_model=nova_2_lite"])
        assert a != b and a and b

    def test_it_is_order_independent(self):
        """Otherwise the same run invoked two ways reads two different indexes."""
        one = self._slug()(["a=1", "b=2"])
        two = self._slug()(["b=2", "a=1"])
        assert one == two

    def test_the_slug_is_filename_safe(self):
        slug = self._slug()(["extraction_model=sonnet5_1m", "ocr=textract_tables"])
        assert "/" not in slug and " " not in slug
        assert "_" not in slug.lstrip("_"), slug  # only the leading separator

    def test_run_matrix_uses_the_same_helper(self):
        """A second implementation would drift, and the failure is silent."""
        import pathlib

        src = pathlib.Path("benchmarks/harness/run_matrix.py").read_text()
        assert "from make_configs import override_slug" in src, (
            "run_matrix must reuse make_configs.override_slug, not reimplement it"
        )
        assert 'ap.add_argument(\n        "--set"' in src, (
            "run_matrix needs a matching --set so it can find the right index"
        )


@pytest.mark.unit
def test_a_file_valued_axis_does_not_false_positive(rm, tmp_path):
    """`@file:` axis values must be RESOLVED before comparing index to disk.

    The frozen pre-#653 classification prompt is supplied as
    `@file:classification_task_prompt_pre653.txt`. The index records that literal
    string; the generated config holds the file's CONTENTS. Comparing the two
    verbatim reports a mismatch on a config that was generated correctly — which
    is precisely what happened the first time `boundaryctl` tried to run, aborting
    a suite whose configs were fine.
    """
    import yaml as _yaml

    matrix = _yaml.safe_load(open(rm.CFG_MATRIX))
    axis = (matrix.get("axes") or {}).get("boundary_prompt") or {}
    knobs = axis.get("legacy") or {}
    assert knobs, "the boundary_prompt/legacy axis value has gone away"
    dotted, raw = next(iter(knobs.items()))
    assert str(raw).startswith("@file:"), f"expected an @file: value, got {raw!r}"

    # Build a config file the way make_configs would: the RESOLVED contents.
    resolved = rm._resolve_axis_value(raw)
    assert resolved and not resolved.startswith("@file:")
    cfg_path = tmp_path / "cell.yaml"
    probe: dict = {}
    rm._set_path(probe, dotted, resolved)
    cfg_path.write_text(_yaml.safe_dump(probe))

    cells = [
        {
            "cell": "split-llm-legacyprompt",
            "path": str(cfg_path),
            "resolved": {"boundary_prompt": "legacy"},
        }
    ]
    # Must NOT raise/exit.
    rm.verify_config_axes(cells)


@pytest.mark.unit
def test_a_file_valued_axis_still_catches_a_real_mismatch(rm, tmp_path):
    """Guard-the-guard: resolving `@file:` must not blind the check. A config
    whose prompt is genuinely something else still has to abort."""
    import yaml as _yaml

    matrix = _yaml.safe_load(open(rm.CFG_MATRIX))
    knobs = ((matrix.get("axes") or {}).get("boundary_prompt") or {}).get("legacy")
    dotted, _raw = next(iter(knobs.items()))
    cfg_path = tmp_path / "cell.yaml"
    probe: dict = {}
    rm._set_path(probe, dotted, "some completely different prompt")
    cfg_path.write_text(_yaml.safe_dump(probe))

    cells = [
        {
            "cell": "split-llm-legacyprompt",
            "path": str(cfg_path),
            "resolved": {"boundary_prompt": "legacy"},
        }
    ]
    with pytest.raises(SystemExit):
        rm.verify_config_axes(cells)


@pytest.mark.unit
def test_slot_check_polls_only_still_running_work(monkeypatch):
    """The launch gate must not re-poll runs it already saw finish.

    `inflight` used to grow for the whole suite while every slot check polled ALL
    of it, making the cost of deciding to launch O(runs launched so far) DynamoDB
    queries — quadratic over a suite. It is a performance bug with a correctness
    cost: a 171-run grid spends its budget on polling instead of documents, and a
    release gate that takes 14 hours does not get run.

    Simulates the loop against a fake poller and asserts the poll count stays
    bounded by the number of runs still in flight, not by history.
    """
    polls: list[str] = []
    finished = set()

    def poll_done(rid):
        polls.append(rid)
        return rid in finished

    # Mimic the launch loop: everything completes immediately, so no run should
    # ever be polled more than a small constant number of times.
    inflight: list[str] = []
    max_inflight = 4
    for i in range(40):
        inflight = [x for x in inflight if not poll_done(x)]
        while len(inflight) >= max_inflight:  # pragma: no cover - never taken here
            inflight = [x for x in inflight if not poll_done(x)]
        rid = f"run-{i}"
        finished.add(rid)  # completes before the next iteration
        inflight.append(rid)

    from collections import Counter

    worst = max(Counter(polls).values())
    assert worst <= 2, f"a single run was polled {worst} times; the list is not pruned"
    # The old code polled ~n^2/2 = 800 times for 40 launches.
    assert len(polls) < 100, f"{len(polls)} polls for 40 launches suggests O(n^2)"


@pytest.mark.unit
class TestStackChangeGuard:
    """A grid must not span more than one build.

    The v0.6.7 `corefast` run had 22 of 171 runs launched during a CloudFormation
    update someone else started, and two updates landed across its four hours. The
    numbers were only discovered to be uncontrolled at scoring time, which cost the
    whole run its standing as a release gate. Nothing in the harness noticed.
    """

    def test_a_run_refuses_to_start_mid_update(self, rm, monkeypatch):
        monkeypatch.setattr(
            rm, "stack_fingerprint", lambda s: ("UPDATE_IN_PROGRESS", "d", "t")
        )
        with pytest.raises(SystemExit) as exc:
            rm.assert_stack_quiesced("IDP1")
        assert "IN_PROGRESS" in str(exc.value)

    def test_a_quiesced_stack_starts_and_returns_its_fingerprint(self, rm, monkeypatch):
        fp = ("UPDATE_COMPLETE", "... (v0.6.7.dev8)", "2026-09-03T19:35:26")
        monkeypatch.setattr(rm, "stack_fingerprint", lambda s: fp)
        assert rm.assert_stack_quiesced("IDP1") == fp

    def test_a_change_mid_run_aborts(self, rm, monkeypatch):
        before = ("UPDATE_COMPLETE", "(v0.6.7.dev8)", "19:35")
        after = ("UPDATE_COMPLETE", "(v0.6.7.dev9)", "21:02")
        monkeypatch.setattr(rm, "stack_fingerprint", lambda s: after)
        with pytest.raises(SystemExit) as exc:
            rm.assert_stack_unchanged("IDP1", before, 22)
        msg = str(exc.value)
        assert "CHANGED mid-run" in msg
        assert "22 launch" in msg, "the message must say how much is affected"

    def test_an_unchanged_stack_does_not_abort(self, rm, monkeypatch):
        fp = ("UPDATE_COMPLETE", "(v0.6.7.dev8)", "19:35")
        monkeypatch.setattr(rm, "stack_fingerprint", lambda s: fp)
        rm.assert_stack_unchanged("IDP1", fp, 5)  # must not raise

    def test_a_transient_api_error_does_not_kill_a_paid_run(self, rm, monkeypatch):
        """The guard protects attribution; it must not become a new way to lose a
        run that is already half-spent."""

        def boom(_s):
            raise RuntimeError("throttled")

        monkeypatch.setattr(rm, "stack_fingerprint", boom)
        rm.assert_stack_unchanged("IDP1", ("a", "b", "c"), 5)  # must not raise
