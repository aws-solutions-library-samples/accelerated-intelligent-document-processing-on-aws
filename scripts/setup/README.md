# Development Environment Setup Scripts

This directory contains setup scripts for configuring development environments across different operating systems.

## Available Setup Scripts

| Script | Operating System | Description |
|--------|-----------------|-------------|
| `mac_setup.sh` | macOS | Complete setup for macOS development environment |
| `dev_setup_ubuntu.sh` | Ubuntu | Setup for Ubuntu Linux (including EC2 instances) |
| `dev_setup_al2023.sh` | Amazon Linux 2023 | Setup for Amazon Linux 2023 |
| `wsl_setup.sh` | Windows (WSL) | Setup for Windows Subsystem for Linux |

## Usage

### macOS
```bash
./scripts/setup/mac_setup.sh
```
See also: [Setup Development Environment - macOS](../../docs/setup-development-env-macos.md)

### Ubuntu
```bash
./scripts/setup/dev_setup_ubuntu.sh
```
See also: [Setup Development Environment - Linux](../../docs/setup-development-env-linux.md)

### Amazon Linux 2023
```bash
./scripts/setup/dev_setup_al2023.sh
```
See also: [Setup Development Environment - Linux](../../docs/setup-development-env-linux.md)

### Windows (WSL)
```bash
./scripts/setup/wsl_setup.sh
```
See also: [Setup Development Environment - WSL](../../docs/setup-development-env-WSL.md)

## Prerequisites

- Bash shell
- `sudo` access for package installation
- Internet connectivity for downloading packages

## What These Scripts Install

- Python 3.12 with pyenv
- Node.js 26 with nvm
- AWS CLI v2
- AWS SAM CLI
- Docker (where applicable)
- Development tools (git, make, etc.)
- Project-specific Python dependencies

## Notes

- These scripts are designed to be idempotent - running them multiple times is safe
- Some scripts may require a shell restart to activate new environment variables
- Review each script before running to understand what will be installed