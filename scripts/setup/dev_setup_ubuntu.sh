#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

##############################################################################################
# Linux Development Environment Setup Script
# 
# This script automates the installation of development tools for the GenAI IDP accelerator
# on Ubuntu 24.04 systems. It installs Python 3.12, AWS CLI, SAM CLI, Node.js, Docker,
# and other essential development tools.
#
# Usage: ./dev_setup_ubuntu.sh
# Note: Logout/login required after completion for Docker group permissions
##############################################################################################

# exit on failure
set -ex

pushd /tmp

# developer tools
sudo apt update -y
sudo apt install build-essential -y

# python/pip/conda
sudo apt install python3.12 python3.12-venv python3-pip unzip wget zip -y
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.12 1
pip3 install --upgrade pip --break-system-packages
# ruff is pinned: the lint gates invoke it as a bare command, so whatever this
# installs is what `make lint` measures, and findings and formatting are both
# properties of the linter version. Keep in step with RUFF_VERSION in the
# Makefile — scripts/tests/test_ci_gate_parity.py fails if they drift.
pip3 install virtualenv typer 'ruff==0.15.13' --break-system-packages
curl -LO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda || bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda -u

# aws cli
sudo apt remove awscli -y || true
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
sudo ./aws/install --update

# sam cli
wget -q https://github.com/aws/aws-sam-cli/releases/latest/download/aws-sam-cli-linux-x86_64.zip
unzip -q aws-sam-cli-linux-x86_64.zip -d ./sam-cli
sudo ./sam-cli/install --update

# node 22
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash  # nosemgrep: bash.curl.security.curl-pipe-bash.curl-pipe-bash - Official NVM installation script
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"
nvm install 20

# docker
sudo apt install docker.io -y
sudo systemctl start docker
sudo systemctl enable docker
sudo usermod -a -G docker $USER

# local .bashrc scripts
mkdir -p ~/.bashrc.d
cat <<'_EOF' > ~/.bashrc.d/bob-prefs

_bash_history_sync() {
  builtin history -a
}

# modifications needed only in interactive mode
if [ "$PS1" != "" ]; then
    # keep more history
    shopt -s histappend
    export HISTSIZE=100000
    export HISTFILESIZE=100000
    export PROMPT_COMMAND="history -a;"

    if [[ "$PROMPT_COMMAND" != *_bash_history_sync* ]]; then
      PROMPT_COMMAND="_bash_history_sync; $PROMPT_COMMAND"
    fi

    # default prompt (from Cloud9)
    _prompt_user() {
        if [ "$USER" = root ]; then
            echo "$USER"
        else
            echo ""
        fi
    }
    PS1='\[\033[01;32m\]$(_prompt_user)\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]$(__git_ps1 " (%s)" 2>/dev/null) \$ '
 
    alias python=python3
    alias pip=pip3
    alias interpreter="interpreter --model bedrock/us.anthropic.claude-3-haiku-20240307-v1:0"
    
    # Add local bin to PATH
    export PATH="$HOME/.local/bin:$PATH"
fi
_EOF

popd

echo "DONE - Please log out and log back in for Docker group permissions to take effect."
