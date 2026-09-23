---
title: "Development Environment Setup Guide on Windows (Native)"
---

# Development Environment Setup Guide on Windows (Native)

## Introduction

This guide establishes a native Windows development environment for the GenAI IDP accelerator using PowerShell, Git Bash, or Windows Terminal.

**Purpose**: Provides a straightforward setup process for Windows users who prefer native tools over WSL, ensuring compatibility with the project's AWS infrastructure.

**When to use this guide**:
- You're developing on Windows and want to use native Windows tools
- You prefer PowerShell or Git Bash over WSL
- You want a lightweight setup without Linux emulation

**Alternative**: If you prefer a Linux environment on Windows, see [Setup Guide for WSL](./setup-development-env-WSL.md).

## Prerequisites

### Required Software

1. **Python 3.12 or higher**
   - Download from: https://www.python.org/downloads/
   - ⚠️ **Important**: Check "Add Python to PATH" during installation
   - Verify: `python --version` or `python3 --version`

2. **Node.js 22.12 or higher**
   - Download from: https://nodejs.org/
   - Installs both Node.js and npm
   - Verify: `node --version` and `npm --version`

3. **Git for Windows**
   - Download from: https://git-scm.com/download/win
   - Installs Git Bash shell
   - Verify: `git --version`

4. **AWS CLI v2**
   - Download from: https://aws.amazon.com/cli/
   - Verify: `aws --version`

5. **AWS SAM CLI**
   - Download from: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html
   - Verify: `sam --version`

6. **Docker Desktop** (optional, for local Lambda testing)
   - Download from: https://www.docker.com/products/docker-desktop/
   - Required only if you want to test Lambda functions locally with `sam local invoke`

## Step 1: Clone the Repository

Open PowerShell, Git Bash, or Windows Terminal:

```powershell
git clone https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws.git
cd accelerated-intelligent-document-processing-on-aws
```

## Step 2: Create Python Virtual Environment

**Using PowerShell:**
```powershell
# Create virtual environment
python -m venv .venv

# Activate virtual environment
.\.venv\Scripts\Activate.ps1

# If you get an execution policy error, run:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

**Using Git Bash:**
```bash
# Create virtual environment
python -m venv .venv

# Activate virtual environment
source .venv/Scripts/activate
```

You should see `(.venv)` at the beginning of your prompt when activated.

## Step 3: Install the IDP CLI

⚠️ **Critical**: install all three packages in a **single** `pip install`, from the
repository root. They require each other by name, those names on public PyPI belong
to unrelated parties, and installing them one at a time lets pip resolve a
not-yet-installed sibling from the index instead of from `lib/`. See
[Installing First-Party Packages Safely](dependency-confusion.md).

```bash
pip install -e lib/idp_common_pkg -e lib/idp_sdk -e lib/idp_cli_pkg
```

> **Note**: This installs the `idp-cli` command along with all required Python dependencies. `make setup` does the same thing and adds the rest of the development dependencies.

## Step 4: Configure AWS CLI

Refer to: https://docs.aws.amazon.com/cli/latest/userguide/getting-started-quickstart.html

```bash
aws configure
```

## Step 5: Test the Build

### Test CLI help

```bash
idp-cli publish --help
```

### Test build

Standard build and publish:
```bash
idp-cli publish --source-dir . --region us-east-1
```

### Troubleshooting Build Issues

If the build fails, use the `--verbose` flag to see detailed error messages:

```bash
idp-cli publish --source-dir . --region us-east-1 --verbose
```

The verbose flag will show:
- Exact SAM build commands being executed
- Complete error output from failed builds
- Python version compatibility issues
- Missing dependencies or configuration problems

> **Note**: The legacy `publish.py` script is deprecated. Use `idp-cli publish` for all new builds.
