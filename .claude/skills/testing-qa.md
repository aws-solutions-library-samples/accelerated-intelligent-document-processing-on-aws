# Testing & QA — GenAI IDP Accelerator

## Test Framework
- **Framework**: pytest
- **Markers**: `@pytest.mark.unit`, `@pytest.mark.integration`
- **Default**: Unit tests only (`addopts = -m "not integration"`)
- **AWS Mocking**: `moto` (`@mock_aws` decorator)
- **Mocking**: `unittest.mock` (`MagicMock`, `patch`)
- **Coverage**: `pytest-cov` for coverage reports

## Test Locations
| Package | Test Directory | Command |
|---------|---------------|---------|
| `idp_common` | `lib/idp_common_pkg/tests/` | `cd lib/idp_common_pkg && make test` |
| `idp_cli` | `lib/idp_cli_pkg/tests/` | `cd lib/idp_cli_pkg && pytest -v` |
| `idp_sdk` | `lib/idp_sdk/tests/` | `cd lib/idp_sdk && pytest -m "not integration" -v` |
| Capacity Lambda | `src/lambda/calculate_capacity/` | `make test-capacity` |
| Config Library | `config_library/test_config_library.py` | `make test-config-library` |

## Test File Structure
Tests mirror the module structure:
```
tests/
├── conftest.py              # Global setup (AWS creds, module mocks)
├── unit/
│   ├── agents/              # Mirrors idp_common/agents/
│   ├── assessment/          # Mirrors idp_common/assessment/
│   ├── classification/      # Mirrors idp_common/classification/
│   ├── config/              # Mirrors idp_common/config/
│   ├── extraction/          # Mirrors idp_common/extraction/
│   ├── ocr/                 # Mirrors idp_common/ocr/
│   └── ...                  # 50+ test files
├── integration/             # Integration tests (require AWS)
└── resources/               # Test data / fixtures
```

## conftest.py Pattern
The global conftest.py does critical setup:
```python
import os, sys
from unittest.mock import MagicMock

# Set AWS credentials BEFORE any boto3 imports
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Mock heavy external dependencies that aren't needed in unit tests
sys.modules["strands"] = MagicMock()
sys.modules["strands.agent"] = MagicMock()
sys.modules["bedrock_agentcore"] = MagicMock()
# ... more module mocks
```

## Writing Unit Tests
### Class-Based Pattern (preferred)
```python
import pytest
import boto3
from moto import mock_aws
from idp_common.models import Document, Status

class TestMyFeature:
    def setup_method(self):
        """Set up test fixtures."""
        self.document = Document(
            id="test-doc-123",
            input_bucket="input-bucket",
            input_key="test.pdf",
            status=Status.QUEUED,
        )
        self.bucket = "test-working-bucket"

    @pytest.mark.unit
    @mock_aws
    def test_happy_path(self):
        """Test the normal processing flow."""
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket=self.bucket)
        # ... test logic
        assert result["status"] == "success"

    @pytest.mark.unit
    def test_error_handling(self):
        """Test error cases."""
        with pytest.raises(ValueError, match="Invalid document"):
            process_document(None)
```

### Function-Based Tests
```python
@pytest.mark.unit
def test_specific_function():
    result = my_function(input_data)
    assert result == expected
```

## Proving a test is load-bearing: break the code and watch it go red

A test that passes tells you nothing about whether it *can* fail. Where it matters —
a test you have just corrected, or one whose assertion you are relying on — mutate
the production code, confirm that test and only that test goes red, then restore.
Restore from a **copy you made under `/tmp`**, never with `git checkout --` or
`git restore`: those discard uncommitted work in the same file and have cost real
edits here. Run the matrix in the **foreground**, one mutation at a time.

⚠️ **Delete `__pycache__` between mutations, or a same-length mutation can run
bytecode that no longer matches the file on disk.** CPython decides a cached `.pyc`
is still valid by comparing the source's `(mtime, size)` — so a mutation that changes
*neither* can be written, and the stale `.pyc` still used. Plenty of the most useful
mutations are exactly that shape: swapping the two arms of a ternary, `>` for `<`,
one status string for another of equal length.

**Clearing the cache is what fixes this. `-B` does not.** `-B` suppresses *writing*
`.pyc` files and does nothing about *reading* one that already exists. Measured, with
a 59-character mutation replacing 59 characters and the mtime held back:

| | cleared | `-B` | result |
|---|---|---|---|
| nothing done | no | no | **stale** |
| `-B` alone | no | yes | **stale** |
| clear alone | yes | no | correct |
| clear and `-B` | yes | yes | correct |

So use both, but know which does the work: the clear is the fix, and `-B`'s narrower
job is to stop a *new* stale `.pyc` being written for the next mutation in the loop.
Reading "run it with `-B`" as the remedy and skipping the clear reproduces the bug.

⚠️ **The hazard is intermittent, which is worse than deterministic.** Whether it
bites depends on whether the clock second ticked between the compile and the rewrite.
In a tight loop it bites every time; insert a pause and the same mutation reports
correctly. So a matrix can give a true result on one run and a false one on the next
with nothing visible distinguishing them — which is why this is a thing to do
unconditionally rather than when you suspect it.

Two presentations, neither of which looks like a caching problem:

- A mutation that appears to **change nothing**, which reads as "this test is
  vacuous" — the opposite of the truth.
- A **baseline** failure *after* you have restored the original, because the stale
  `.pyc` from the mutated version is still considered valid. Nothing about the tree
  looks wrong at that point, so it reads as a real defect in code you just put back.

```python
for cache in package_dir.rglob("__pycache__"):
    shutil.rmtree(cache, ignore_errors=True)
subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", ...], cwd=package_dir)
```

`-p no:cacheprovider` is a different thing and does not help at all: it disables
pytest's own `.pytest_cache`, not CPython's bytecode cache.

Two shapes worth mutating for specifically, because both pass while asserting
nothing: an expectation table that a loop only `print`s rather than compares, and a
predicate that is a tautology against a mock (`hasattr(mock, anything)`,
`assert mock.attr is not None`, `assert isinstance(mock, Mock)`). A `Mock` answers
to every attribute name, so a claim about one is a claim about your own fixture.

## ⚠️ Never chdir to the repo root for code that writes

A test that exercises a real build or packaging step often has to set the cwd, because
the step resolves its inputs relatively. **Set it to a staging root, never to the
checkout** — many of those steps resolve their *output* relatively too, so the test
leaves build artefacts in the working tree.

The cost is not tidiness. `make srt-scan` detects an `.aws-sam` directory and drops
into a degraded mode (phantom LOCAL-ONLY findings, and a documented risk of crashing
checkov on the largest templates), so a test that leaves one makes **the security gate
depend on whether the test suite ran first** — it stops being the scan CI runs. That
shipped once ([#1491](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1491)).

Build the staging root out of **symlinks** to the paths the step reads, under
`tmp_path`. `staging_checkout` in
`lib/idp_sdk/tests/unit/test_multi_doc_discovery_source_zip.py` is the worked example,
and `no_build_output_in_checkout` in `lib/idp_sdk/tests/conftest.py` is the suite-wide
control that catches the class rather than one instance.

Check, rather than assume, that the artefact is identical to one built in the checkout.
It is for that step because it derives each archive name with
`os.path.relpath(file_path, ".")`, which is cwd-relative — but a step that builds its
member names from an absolute path would embed the staging root instead, and the content
contract the test asserts would quietly change.

Five things that are easy to get wrong:

- **A test that only expects the step to *fail* leaks too.** The artefact is usually
  written before the validation that raises, so an aborting call still writes.
- **Assert "unchanged by this run", not "absent".** `publish.py` legitimately writes
  these paths in a developer's checkout, so a guard that demands absence goes red on an
  ordinary built tree and blames the current run. Snapshot existence and mtime either
  side of the call instead.
- **Watch the directory, not only the file.** What degrades `make srt-scan` is an
  `.aws-sam` *directory*; a helper that creates one without writing the artefact — for a
  lock file, say — slips past a file-only guard while still degrading the scan.
- **Stage the top of each walk, never a child of a directory the step walks.**
  `os.walk` traverses a symlinked top but, with `followlinks=False`, descends into no
  symlinked *sub*directory. So staging `a/b` and `a/c` as links and letting the step
  `os.walk("a")` yields neither — a silently short artefact and a passing content test.
- **`mkdir(parents=True)` follows an existing symlink.** If the set of staged paths ever
  contains one nested inside another, creating the second link's parent creates a real
  directory *inside the repository* — the exact thing being prevented. Assert no staged
  path is a prefix of another, especially when the set is imported from elsewhere and can
  grow without the test being touched.

⚠️ **A staging root of symlinks is an alias, not a sandbox.** Writes the step makes
*under* a staged path land in the real tree and no checkout guard will see them. What
makes the arrangement safe is that the step's writes go somewhere that is not a staged
path — verify that, do not assume it.

## Moto Usage
Always use `@mock_aws` decorator:
```python
from moto import mock_aws

@mock_aws
def test_s3_operation():
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="test-bucket")
    # ... operations happen against moto mock
```

## Local Lambda Testing
```bash
cd patterns/unified/
sam build
sam local invoke OCRFunction -e ../../testing/OCRFunction-event.json --env-vars ../../testing/env.json
```

## Sample Documents
Available in `samples/` directory for testing document processing.

## Evaluation Framework
For document processing accuracy:
- Baseline data in S3 Evaluation Baseline bucket
- Per-field evaluation methods: `EXACT`, `NUMERIC_EXACT`, `FUZZY`
- `stickler-eval` library for metrics
- Run via CLI: `idp-cli run-evaluation --stack-name <name>`

## Test Studio (Web UI)
Interactive testing via the web UI — upload documents, compare results across config versions.

## Commands Reference
```bash
make test                    # Every non-integration suite, auto-discovered
make test-integration-all    # Every integration-marked suite (needs AWS)
make test-list               # Show discovered roots (run vs quarantined)
make test-cli                # CLI tests only
make test-config-library     # Config validation only
make test-capacity           # Capacity planning tests
make test-capacity-coverage  # With coverage report

# Direct pytest invocations
cd lib/idp_common_pkg
make test-unit               # Unit tests only
make test-integration        # Integration tests (needs AWS)
pytest -m "unit" -k "test_extraction"   # Filter by name
pytest -v --tb=short         # Verbose with short tracebacks
pytest --cov=idp_common --cov-report=html   # Coverage report
```

⚠️ **A bare `pytest` is not pinned to this checkout; a `make` target is.** The `make`
lines above go through `PYTEST_HERMETIC`, which exports an absolute `PYTHONPATH` naming
every `lib/*` package root of the checkout the makefile belongs to. The three bare
`pytest` lines inherit whatever the editable-install pointer in the interpreter's
`site-packages` says, which on this host is rewritten by any `pip install -e` in any
checkout. Pin them yourself — **every** root, because the packages import each other,
and absolute, because a subprocess that changes directory drops a relative entry:

```bash
W=$(git rev-parse --show-toplevel)
export PYTHONPATH=$W/lib/idp_common_pkg:$W/lib/idp_sdk:$W/lib/idp_cli_pkg:$W/lib/idp_mcp_connector_pkg:$W/lib/idp_feature_sdk
```

The provenance guard wired into several conftests prepends those roots for you and warns
that it did; where it cannot, it refuses, and a refusal reading `REFUSED:` means the run
**did not happen** rather than that a test failed. `python3
scripts/check_first_party_deps.py` says whether the environment itself points here.

## Writing a gate exemption

Turning a gate off for anything means registering it in
`scripts/tests/gate_exemptions.json`; `test_gate_exemption_registry.py` fails on an
unregistered exemption list and names it. The full rules are in CLAUDE.md
("Every gate exemption is registered"), but the four that decide most reviews:

1. **One entry per file, ideally per line.** A reason bound to a directory answers for
   every file under it, and an aggregate reading of it passes even when it is false of
   most of them. That is the exact shape of four shipped defects.
2. **Compute the premise if you can.** `scripts/tests/gate_premises.py` holds the
   predicates (`not_a_nested_stack_of_parent`, `built_separately_from_main_stack`,
   `file_absent_or_untracked`, `installer_manifest_pins_parameter`). Each takes **one**
   member — parametrise over your members rather than asking whether the reason holds
   generally.
3. **`JUDGEMENT` is allowed, with a written reason.** It says there is nothing to
   compute; it does not say nobody looked.
4. **Give it a ratchet**, or declare the gap in `ratchetGap`. Non-vacuity (it must
   shield something today), count pinning (it shields only as many sites as were
   audited), universe closure (nothing may sit outside both sets), staleness.

Two patterns worth copying rather than reinventing:
`lib/idp_common_pkg/tests/unit/bedrock/test_long_context_metering_key.py` stores
`(reason, site count)` so a new site in an exempt file still fails, and
`scripts/tests/test_log_group_encryption.py::test_every_log_group_template_is_categorised`
derives its universe and fails if any member is in no category — which is why its
categories can be trusted, and how it found eleven log groups a hand-built inventory
missed.

## How `make test` finds every suite (scripts/run_all_tests.py)
The repo's Python tests live in ~30 separate roots (packages + per-Lambda dirs).
A single `pytest` from the repo root FAILS: the many `tests/conftest.py` files
all import as the module `tests.conftest` and collide
(`ImportPathMismatchError`; `--import-mode=importlib` hits a duplicate-plugin
error too). So `make test` runs `scripts/run_all_tests.py`, which runs each root
as its own isolated pytest invocation.

The script DISCOVERS every directory containing `test_*.py` and checks it against
two registries in the file: `RUN_ROOTS` (run in the gate) and `QUARANTINE`
(excluded, each with a reason). **A discovered dir in neither list is a hard
error** — so when you add tests in a NEW location, `make test`/CI fails until you
add that dir to `RUN_ROOTS` (if green headless) or `QUARANTINE` (with a reason).
This is deliberate: it's the guard that stops new tests from being silently
skipped, which is exactly how ~200 Lambda tests went unrun under the old
hand-maintained `make test`. **Run `make test-list` for the current quarantined set
and read the reason beside each entry in `QUARANTINE`** — an enumeration here goes
stale the moment one is added or paid off, and the reasons are the part that matters:
they are per-directory, and `scripts/tests/test_run_all_tests_registry.py` computes
the ones that are computable (whether a root claiming to collect nothing really does,
and whether the single failing test a root is held back by still exists and still
fails). `docs/testing.md` carries the same list as prose, and that page is checked
against the registry in both directions.

An exclusion covers **only the directory named** — nesting under one does not
inherit it, in `run_all_tests` or in the CI-coverage gate. That matters most for the
bare `scripts` entry, which is excluded for one mis-collected file: inherited, it
would cover `scripts/tests`, `scripts/sdlc/tests`, `scripts/srt/tests` and
`scripts/security/tests`, which is the whole gate layer.

**Being registered in `RUN_ROOTS` does not mean a suite runs on a pull request.**
`make test` runs in neither CI. CI runs `make test-cicd -C lib/idp_common_pkg` and
`make test-packages-cicd`, and the second is a hand-enumerated recipe — so a new root
has to be added there as well, on its own `cd <dir> && $(PYTEST_HERMETIC) …` line if it
defines a module named `index`. `scripts/tests/test_src_lambda_tests_in_ci.py` derives
both sides and fails until you do; 22 roots holding 506 tests were in `RUN_ROOTS` and
in neither CI before it was generalised beyond `src/lambda/`. Everything gated goes
through `$(PYTEST_HERMETIC)`, which strips the AWS environment, so a suite that needs a
region or placeholder credentials supplies them from its own `conftest.py` with
`os.environ.setdefault`.
