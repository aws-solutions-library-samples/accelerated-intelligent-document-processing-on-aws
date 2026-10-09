# AWS CodeBuild Buildspec Validator

A Python script to validate AWS CodeBuild `buildspec.yml` files for syntax errors, structural issues, and best practices.

## Features

- **YAML Syntax Validation**: Ensures buildspec files are valid YAML
- **Structure Validation**: Checks for required fields (`version`, `phases`)
- **Type Checking**: Validates that commands are strings, not accidentally parsed as objects
- **Best Practices**: Warns about unknown phases or deprecated features
- **Fail-fast loops**: Errors on a multi-line command whose `for`/`while`/`until` loop does not enable `errexit`. Such a command's exit status is only its last iteration's, so a failure in any earlier one is silently skipped and the phase succeeds — see [#1310](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1310)
- **Multi-file Support**: Can validate multiple buildspec files at once

## Installation

The validator requires Python 3.12+ and PyYAML:

```bash
pip install pyyaml
```

For development environments with externally-managed Python (like macOS with Homebrew), create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml
```

## Usage

### Validate a single file

```bash
python3 scripts/sdlc/validate_buildspec.py patterns/unified/buildspec.yml
```

### Validate every buildspec in the tree

Pass the files explicitly, or let `git` find them. Do **not** use a
`patterns/*/buildspec.yml` glob: it matches one of the four buildspec files here,
and the ones it misses are the ones nobody is looking at.

```bash
python3 scripts/sdlc/validate_buildspec.py $(git ls-files | grep -E '(^|/)buildspec[^/]*\.ya?ml$')
```

`make validate-buildspec` does exactly this, and fails outright if the discovery
returns nothing.

### Using the Makefile target

```bash
make validate-buildspec
```

This is also included in the `make lint` command.

## Output

The validator provides clear output with:
- ✅ Success indicators
- ❌ Error messages with specific line numbers
- ⚠️ Warnings for non-critical issues
- 📊 Summary of phases and command counts

### Example Output

```
Validating: patterns/unified/buildspec.yml
======================================================================
✅ Valid buildspec file

Summary:
  Version: 0.2
  Phases: pre_build, build, post_build
    - pre_build: 7 commands
    - build: 39 commands
    - post_build: 8 commands
```

### Example Error Output

```
Validating: patterns/unified/buildspec.yml
======================================================================

❌ ERRORS (1):
  - Phase 'post_build', command #5 must be a string, got dict

❌ Invalid buildspec file
```

## Common Issues Detected

### 1. Colons in Command Strings

**Problem**: YAML interprets colons as key-value separators, even in quoted strings in some cases.

```yaml
# ❌ BAD - May be parsed as a dictionary
- echo "Note: This is a message"

# ✅ GOOD - Use single quotes around the entire command
- 'echo "Note: This is a message"'
```

### 2. Missing Required Fields

The validator checks for:
- `version` field (must be 0.1 or 0.2)
- `phases` section (must have at least one phase)

### 3. Invalid Command Types

All commands must be strings:

```yaml
# ❌ BAD - Command is a dictionary
phases:
  build:
    commands:
      - echo: "This is wrong"

# ✅ GOOD - Command is a string
phases:
  build:
    commands:
      - echo "This is correct"
```

## Exit Codes

- `0`: All buildspec files are valid
- `1`: One or more buildspec files have errors

This makes it suitable for use in CI/CD pipelines:

```yaml
- name: Validate Buildspec
  run: make validate-buildspec
```

## Limitations

This validator checks for:
- YAML syntax errors
- Required fields and structure
- Data type correctness
- Common mistakes

It does **not** validate:
- AWS-specific runtime environments
- Environment variable references
- S3 artifact paths
- IAM permissions

For complete validation, test your buildspec in an actual CodeBuild environment.

## Integration with CI/CD

### GitHub Actions

Already integrated in `.github/workflows/developer-tests.yml` via the `make lint` command.

### Local Pre-commit Hook

Add to `.git/hooks/pre-commit`:

```bash
#!/bin/bash
make validate-buildspec || exit 1
```

## Troubleshooting

### "ModuleNotFoundError: No module named 'yaml'"

Install PyYAML:
```bash
pip install pyyaml
```

### "externally-managed-environment"

On macOS with Homebrew Python, use a virtual environment:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install pyyaml
```

## Contributing

When adding new buildspec files to the repository, ensure they pass validation:

```bash
make validate-buildspec
```

This is automatically checked in CI/CD pipelines.
