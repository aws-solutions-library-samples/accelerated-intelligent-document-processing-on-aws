# SRT (Sample Security Review Tool) Integration

This directory contains scripts to integrate the AWS Sample Security Review Tool (SRT) into the IDP accelerator build and CI/CD pipeline.

## Overview

The Sample Security Review Tool is an open-source security scanning tool that helps identify security vulnerabilities and compliance issues in your codebase.

- **GitHub Repository**: https://github.com/aws-samples/sample-security-review-tool
- **Latest Releases**: https://github.com/aws-samples/sample-security-review-tool/releases

## Quick Start

### Local Development

```bash
# Full workflow (setup → scan → optional fix)
make srt

# Or run individual steps:
make srt-setup     # Download and configure SRT
make srt-scan      # Run security assessment
make srt-fix       # Interactive fix mode
```

### Running Tests

```bash
# Run all tests (excludes SRT)
make test

# Run SRT security scan separately
make srt
```

SRT has a dedicated target and is not part of `make test` to avoid slowing down the development test loop. It runs automatically in CI/CD on merge requests to `develop`.

## CI/CD Integration

### GitLab CI

The SRT tool is integrated into the GitLab CI pipeline with a dedicated `security_review` stage that:

- **Only runs on merge requests targeting `develop` branch** (not on feature branches or after merge)
- Downloads the latest SRT version automatically
- Runs configuration and assessment
- **Fails the pipeline if security findings are detected**
- Runs after integration tests complete successfully

This ensures that security issues are caught before code is merged to `develop` while not blocking development velocity on other feature branches.

## Scripts

### setup.py

Downloads and configures the SRT tool:

1. Detects the current platform (Linux/macOS, x86_64/ARM64)
2. Fetches the latest release from GitHub
3. Downloads the appropriate binary for your platform
4. Extracts and makes it executable
5. Runs one-time configuration (AWS profile + PATH setup)

**Features:**
- Automatic version detection and upgrades
- Skips download if latest version already installed
- Platform-specific binary selection
- Interactive configuration

### run.py

Runs the SRT security assessment on the project:

- Executes `srt` command in the project root
- Fails with exit code 1 if security issues are found
- Suitable for CI/CD integration

### fix.py

Runs interactive fixing mode:

- Executes `srt fix` to iterate through findings
- Allows interactive remediation of issues
- Best used in local development, not CI/CD

## Configuration

SRT configuration is stored in `.srt/.srt-config` and includes:

- AWS profile to use for assessments
- PATH configuration for tool execution

Run `cd .srt && ./srt config` to reconfigure.

## Suppression Persistence

SRT tracks issue suppressions and resolutions in `issues.json`. To persist suppressions across runs and in CI/CD:

**File Locations:**
- `scripts/srt/issues.json` - **Committed to git** (source of truth for team)
- `.srt/issues.json` - **Gitignored** (working copy for SRT tool)

**Workflow:**
1. **Setup** (`make srt-setup`) - Copies `scripts/srt/issues.json` → `.srt/issues.json` (restore suppressions)
2. **Fix** (`make srt-fix`) - Copies `.srt/issues.json` → `scripts/srt/issues.json` (save suppressions), keeping only HIGH non-`Open` findings **on git-tracked paths** (see "CI sees only tracked files" below)
3. **Commit** - After fixing/suppressing issues, commit updated `scripts/srt/issues.json` to git

Only mark a finding `suppressed` (with a `suppressionReason`) if you intend it to
stay quiet. `resolved` is **not** sticky — SRT flips a re-detected `resolved`
finding to `reopened`, which gates the build.

This ensures suppressions persist across:
- Local development (between runs)
- CI/CD pipeline (across builds)
- Team members (via git)

## Files Generated

- `.srt/` - SRT installation directory (gitignored)
  - `srt` - The SRT binary
  - `srt-*.tar.gz` - Downloaded archives
  - `srtconfig.json` - Tool configuration
  - `issues.json` - Working copy (copied from scripts/srt/)
  - Assessment results and reports
- `scripts/srt/issues.json` - **Committed to git** (suppression database)

## GitLab CI Job Details

The `srt_security_review` job in `.gitlab-ci.yml`:

```yaml
srt_security_review:
  stage: fast_checks
  timeout: 50m
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
    - if: $CI_COMMIT_BRANCH
  needs: []          # no build stage precedes it — see "CI sees only tracked files"
  script:
    - make srt-setup
    - make srt-scan
```

### Why on every push, not just MRs to `develop`?

The job needs no AWS and runs in **parallel** with `code_checks` (`needs: []`), so
it adds no wall-clock to the fast gate while still gating the expensive deploy
stages. Running it on every push surfaces security regressions early on any
branch, and GitLab emails the committer on failure.

### CI sees only tracked files

Because `needs: []` means no build stage runs first, the CI checkout contains
**tracked files only** — no `.aws-sam/` build output, no vendored
`layer/python/` trees, no `scratch/`. A local working tree usually does have
those, and `srt assess` has no `--exclude` option, so it scans and flags them.

`ci_paths.py` classifies every finding by whether its path is git-tracked:

- `run.py` gates only on tracked-file findings and prints gitignored ones in a
  separate non-blocking `ℹ️ LOCAL-ONLY FINDINGS` table.
- `fix.py` drops gitignored-path findings before writing the committed baseline,
  so `make srt-fix` cannot pollute it with artifact-path entries.
- Classification **fails closed**: if `git ls-files` cannot be run, every finding
  gates.

Never suppress an `.aws-sam/` finding. SRT keys suppressions on
`(path, resourceType, resourceName, check_id)`, so an artifact-path entry cannot
cover the same resource in the source `template.yaml` — and `srt fix` writes it
back as `resolved`, which is not sticky, so the next scan re-detects it as
`reopened` and *that* gates. Use `make srt-clean` to match CI exactly.
`scripts/srt/tests/test_ci_paths.py` enforces this on the committed baseline.

## Best Practices

1. **Local Development**: Run `make srt` before pushing to catch issues early
2. **CI/CD**: Let the pipeline catch issues on develop branch
3. **Fix Promptly**: Address security findings before merging to main/production
4. **Stay Updated**: The tool auto-downloads latest versions to catch new vulnerability patterns

## Troubleshooting

### SRT not found

```bash
make srt-setup
```

### Configuration issues

```bash
cd .srt && ./srt config
```

### Platform not supported

SRT currently supports:
- Linux (x86_64, ARM64)
- macOS (x86_64, ARM64)

Windows support may be added in future releases.

## Learn More

- [SRT GitHub Repository](https://github.com/aws-samples/sample-security-review-tool)
- [Latest Releases](https://github.com/aws-samples/sample-security-review-tool/releases)
- [Documentation](https://github.com/aws-samples/sample-security-review-tool#readme)
