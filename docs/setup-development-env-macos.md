---
title: "Development Environment Setup Guide on MacOS"
---

# Development Environment Setup Guide on MacOS
# Introduction
This guide configures a comprehensive local development environment on macOS systems for the GenAI IDP accelerator.  

Purpose: Delivers an automated, scripted installation of all required development tools optimized for Apple hardware, ensuring rapid setup with minimal manual configuration. This approach leverages native macOS performance while maintaining consistency with project requirements.  

When to use this guide:  
• You're setting up development on a Mac for the first time  
• You're experiencing configuration issues with your current macOS setup  
• You prefer automated installation over manual tool configuration  
• You want to ensure exact tool versions match project specifications  

# What you'll achieve: 
A fully functional local development environment with Python 3.13, AWS CLI, Docker Desktop, VS Code with AI extensions, and integrated development tools - all configured through a single automated setup script. This document provides a step-by-step guide to setting up a development environment on macOS. It covers installing essential tools such as VS Code, Python 3.13.x, AWS CLI, SAM CLI, Node.js, Docker Desktop, Miniconda, Cline for VS Code, and Amazon Q in Terminal.

# Prerequisites

-   macOS 12+ (Monterey) recommended; Apple Silicon (M1/M2/M3) and Intel
    are both supported.

-   An admin account on the Mac (you may be prompted for your password).

-   Stable internet connection.

### **Clone the Repository**

To get the sample project locally, run:
```bash
git clone https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws.git
```

# Quickstart (One-Command Script)

If you prefer to automate everything, run the setup script from the repository root:
```bash
cd accelerated-intelligent-document-processing-on-aws
chmod +x ./scripts/setup/mac_setup.sh
./scripts/setup/mac_setup.sh
```


# Step-by-Step Instructions

## 1) Install Xcode Command Line Tools (and Rosetta, if needed)

Xcode Command Line Tools provide compilers and essential build tools.
Rosetta is required for some Intel-only tools on Apple Silicon.

```bash
# Install Xcode Command Line Tools
xcode-select --install 2>/dev/null || echo "Xcode CLT already installed or prompt will appear."

# (Apple Silicon only) Install Rosetta if not installed
if [[ "$(uname -m)" == "arm64" ]]; then
    if ! /usr/bin/pgrep oahd >/dev/null 2>&1; then
        softwareupdate --install-rosetta --agree-to-license || true
    fi
fi
```


## 2) Install Homebrew

Homebrew is the package manager we will use to install most dependencies.

```bash
# Install Homebrew (skips if already installed)
if ! command -v brew >/dev/null 2>&1; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# Add brew to PATH for current shell session
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
```


## 3) Install Visual Studio Code

You can install VS Code via Homebrew (recommended) or manually from
Microsoft.

```bash
\# Install VS Code via Homebrew Cask\
brew install \--cask visual-studio-code
```


## 4) Python (pyenv + optional Miniconda)

We'll install pyenv to manage Python versions and optionally Miniconda for data workflows. This replaces the Linux-specific alternatives/yum/dnf steps in your original script.

```bash
# Install pyenv and dependencies
brew update
brew install pyenv

# Initialize pyenv for zsh
if ! grep -q 'pyenv init' ~/.zshrc 2>/dev/null; then
    echo '
# pyenv setup' >> ~/.zshrc
    echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
    echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
    echo 'eval "$(pyenv init -)"' >> ~/.zshrc
fi

# Use pyenv to install and set Python 3.12 (adjust if you prefer 3.11, etc.)
eval "$(pyenv init -)"
pyenv install -s 3.12.5
pyenv global 3.12.5

# Upgrade pip and install virtualenv
python -m pip install --upgrade pip
python -m pip install virtualenv

# (Optional) Miniconda via Homebrew
# brew install --cask miniconda
```

## 5) AWS CLI v2

On macOS, the simplest way is via Homebrew.

```bash
brew install awscli

# Test
aws --version
```


## 6) AWS SAM CLI

Install SAM CLI from AWS Homebrew tap.

```bash
brew tap aws/tap
brew install aws-sam-cli

# Test
sam --version
```


## 7) Node.js via nvm (Node 22 LTS)

Use nvm to manage Node versions. This mirrors your Linux section but uses zsh-friendly profile updates.

```bash
# Install nvm
if [ ! -d "$HOME/.nvm" ]; then
    curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
fi

# Load nvm for current shell
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

# Install Node 22 LTS
nvm install 22
nvm alias default 22

# Test
node -v
npm -v
```



## 8) Docker Desktop

Docker Desktop is the recommended way to run containers on macOS. It includes a lightweight VM and provides Docker Engine & Compose.

```bash
brew install --cask docker

# After installation, launch Docker.app from /Applications or via Spotlight.
# Give it a minute to start the backend; then test:
# docker version
```


## 9) Open Interpreter (optional)

Install with pip. You can later configure any model/provider you want (kept generic here).

```bash
python -m pip install --upgrade open-interpreter

# Run with: interpreter
```



## 10) Shell Quality-of-Life Settings (zsh)

On macOS, zsh is the default shell. We'll add a small include folder to keep the main ~/.zshrc tidy.

```bash
# Create include directory for .zshrc snippets
mkdir -p ~/.zshrc.d

# Add an include line to ~/.zshrc if not already present
if ! grep -q 'for f in ~/.zshrc.d/*.zsh' ~/.zshrc 2>/dev/null; then
    cat <<'EOF' >> ~/.zshrc

# Load custom zsh snippets
if [ -d "$HOME/.zshrc.d" ]; then
    for f in $HOME/.zshrc.d/*.zsh; do
        [ -r "$f" ] && source "$f"
    done
fi
EOF
fi

# Create a generic preferences snippet
cat <<'EOF' > ~/.zshrc.d/00-preferences.zsh
# History settings
setopt APPEND_HISTORY
HISTSIZE=100000
SAVEHIST=100000

# Prompt (simple)
PROMPT='%F{green}%n@%m%f:%F{blue}%~%f$(git_prompt_info) $ '

# Aliases
alias python=python3
alias pip=pip3

# (Optional) nvm autoload
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
EOF
```

## 11) Verify Your Setup

- VS Code: Open it from Applications. Install extensions as needed (e.g., Python, Docker, AWS Toolkit).
- Python: run `python --version` and `pip3 --version`.
- Node: run `node -v` and `npm -v`.
- AWS CLI: run `aws --version`.
- SAM CLI: run `sam --version`.
- Docker: launch Docker.app, then run `docker version` in Terminal.

## **12) Install the IDP CLI**

Install the IDP CLI and all required dependencies. `make setup-venv` needs [`uv`](https://docs.astral.sh/uv/) on `PATH`: it reads the dependency lock, so the installed versions are the ones CI tests.

```bash
make setup-venv
source .venv/bin/activate
```

> **Note**: This installs the `idp-cli` command along with all required Python dependencies.

## **13) Configure AWS CLI**
### Refer this link for AWS configure
https://docs.aws.amazon.com/cli/latest/userguide/getting-started-quickstart.html  

## **14) Test the Build**

### Using `idp-cli publish`

Test CLI help:
```bash
idp-cli publish --help
```

Test standard build and publish:
```bash
idp-cli publish --source-dir . --region us-east-1
```

The container-packaged Lambda functions are built and pushed in CodeBuild during
stack deployment, not on this machine, so publishing needs no local Docker daemon
and no ECR permissions of your own. Docker is needed here only for local Lambda
testing with `sam local invoke`.

**Troubleshooting Build Issues:**
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

- If `brew` is not found, add it to PATH: 
```bash
eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
```

- If `pyenv` shims don't work, ensure its init lines are in `~/.zshrc` and open a new Terminal.

- On Apple Silicon, some images/tools may need Rosetta. Install it with `softwareupdate --install-rosetta --agree-to-license`.

- Docker needs to be running before `docker` commands succeed. Launch Docker.app and wait for the whale icon to be steady.

# Troubleshooting Tips
### Cline
What it is: An AI coding assistant that runs as a VS Code extension, powered by various LLMs (Claude, GPT, etc.)  

Key capabilities:
- Autonomous code editing across multiple files  
- Executes terminal commands and reads file outputs  
- Can browse the web for documentation/research  
- Maintains context across entire codebases  
- Handles complex, multi-step development tasks  

Why it's helpful: Acts like an AI pair programmer that can actually write, test, and debug code independently while you supervise.  
- You can install it from "Extensions" tab on VSCode.

### Amazon Q Developer
What it is: AWS's AI coding assistant integrated into IDEs, specifically designed for AWS development

Key capabilities:
- Code suggestions and completions optimized for AWS services  
- Security vulnerability scanning and fixes  
- AWS best practices recommendations  
- Infrastructure as Code (CloudFormation, CDK) assistance  
- Direct integration with AWS documentation and services  

Why it's helpful: Specialized for AWS development with deep knowledge of AWS services, perfect for this GenAI-IDP project since it's 
built entirely on AWS.
- You can install it from https://docs.aws.amazon.com/amazonq/latest/qdeveloper-ug/command-line-installing.html