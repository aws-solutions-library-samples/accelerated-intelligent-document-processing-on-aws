---
paths:
  - "**/tests/**"
  - "scripts/tests/**"
  - "**/test_*.py"
---

# Testing rules

Covers how Python tests are written, registered and run, and how to show that a
test can actually fail. Gate exemptions are covered in
[lint-and-gates.md](lint-and-gates.md).

## Must
- Mark tests `@pytest.mark.unit` or `@pytest.mark.integration`. Integration tests
  need AWS and are excluded by default.
- Mock AWS with moto's `@mock_aws`. Prefer class-based tests with
  `setup_method`, and mirror the module structure under `tests/unit/<subpackage>/`.
- Register any directory that gains a `test_*.py` in `scripts/run_all_tests.py`.
  Put it in `RUN_ROOTS`, or in `QUARANTINE` with a reason.
- Add each new root to the `test-packages-cicd` recipe through
  `$(PYTEST_HERMETIC)`, on its own line if it defines a module named `index`.
  Being in `RUN_ROOTS` alone doesn't put it in CI.
- Run tests through `make`, which pins `PYTHONPATH` to this checkout. If you run
  `pytest` directly, pin every `lib/*` root using absolute paths. A `REFUSED:`
  line means the run did not happen.
- Let a suite that needs a region or placeholder credentials supply them from its
  own `conftest.py` with `os.environ.setdefault`.
- Patch mocks where the code under test looks the name up, which depends on how
  the consumer imported it (see the lazy-loading note in the backend skill).
  Patching the wrong target fails silently.
- Before you rely on a test, prove it is load-bearing:
  - Mutate the production code and confirm that a named test goes red.
  - Restore the code from a copy you saved under `/tmp`.
  - Delete `__pycache__` between mutations as well as running with `-B`.
- Assert the mechanism that produced a result, not only the result. For example,
  assert which code path ran.
- In live harnesses, keep the outcomes distinct: a SKIP or an ERROR is not a PASS.

## Must not
- Run a single `pytest` from the repo root. The `tests.conftest` modules collide.
- Restore mutated files with `git checkout --` or `git restore`.
- Write assertions that can't fail, such as `hasattr` on a `Mock`, `is not None`
  on a mock attribute, or expectation tables that are printed instead of
  compared.
- Delete or weaken an existing test without a replacement and a stated reason.
- Leave a new test directory out of both registries.

## Verify with
- `make test`, `make test-list` and `make test-packages-cicd`
- `cd lib/idp_common_pkg && make test-unit`
- `scripts/tests/test_run_all_tests_registry.py`,
  `scripts/tests/test_src_lambda_tests_in_ci.py`, `scripts/tests/test_testing_doc.py`
- `make check-coverage-debt`

## Sources
- [.claude/skills/testing-qa.md](../skills/testing-qa.md): conftest setup, the
  mutation procedure and cache trap, suite discovery and CI registration
- [.claude/skills/code-review.md](../skills/code-review.md): the test review
  checklist
- [.claude/skills/full-test-battery.md](../skills/full-test-battery.md): the full
  battery and its pre-existing-failure baseline
- [docs/testing.md](../../docs/testing.md): each test tier and its entry point,
  and whether CI runs it
