#!/bin/bash

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

##############################################################################################
# Linux Development Environment Setup Script
# 
# This script automates the installation of development tools for the GenAI IDP accelerator
# on Amazon Linux 2023 systems. It installs Python 3.13, AWS CLI, SAM CLI, Node.js, Docker,
# and other essential development tools.
#
# Usage: ./scripts/setup/dev_setup_al2023.sh (from the repository root)
# Note: Reboot required after completion
##############################################################################################

# exit on failure
set -ex

pushd /tmp

# force python 3.9 (yum/dnf don;t seem to work well with 3.12)
sudo alternatives --install /usr/bin/python3 python3 /usr/bin/python3.9 1
sudo alternatives --set python3 /usr/bin/python3.9

# developer tools
sudo yum groupinstall "Development Tools" -y

# python/pip/conda
sudo dnf update -y
sudo dnf install python3.12 -y
sudo alternatives --install /usr/bin/python3 python3 /usr/bin/python3.12 1
sudo alternatives --install /usr/bin/python python /usr/bin/python3.12 1
python3.12 -m ensurepip --upgrade
pip3 install --upgrade pip
pip3 install virtualenv
curl -LO https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda || bash Miniconda3-latest-Linux-x86_64.sh -b -p $HOME/miniconda -u

# aws cli
sudo yum remove awscli -y
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install --update

# sam cli
wget https://github.com/aws/aws-sam-cli/releases/latest/download/aws-sam-cli-linux-x86_64.zip
unzip aws-sam-cli-linux-x86_64.zip -d ./sam-cli
sudo ./sam-cli/install --update

# node 22
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash  # nosemgrep: bash.curl.security.curl-pipe-bash.curl-pipe-bash - Official NVM installation script for development environment only
source ~/.bashrc
nvm install 18

# docker
sudo yum install docker -y
sudo service docker start
sudo usermod -a -G docker ec2-user

# local .bashrc scripts
mkdir -p ~/.bashrc.d
cat <<_EOF > ~/.bashrc.d/bob-prefs

_bash_history_sync() {
  builtin history -a
}

# modifications needed only in interactive mode
if [ "\$PS1" != "" ]; then
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
        if [ "\$USER" = root ]; then
            echo "\$USER"
        else
            echo ""
        fi
    }
    PS1='\[\033[01;32m\]\$(_prompt_user)\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$(__git_ps1 " (%s)" 2>/dev/null) \$ '
 
    alias python=python3
    alias pip=pip3
    alias interpreter="interpreter --model bedrock/us.anthropic.claude-3-haiku-20240307-v1:0"
fi
_EOF

popd

echo "DONE - Please reboot to complete the setup."
