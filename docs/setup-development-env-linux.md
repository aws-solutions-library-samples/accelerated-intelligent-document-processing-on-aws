---
title: "Development Environment Setup Guide on Linux"
---

# Development Environment Setup Guide on Linux

# Introduction

This guide establishes a cloud-based development environment using Amazon Linux 2023 or Ubuntu 24.04 on AWS EC2, specifically designed for the GenAI IDP accelerator.

Purpose: Provides a standardized, scalable development infrastructure that combines the familiar VSCode interface on your local machine with powerful cloud compute resources. This approach eliminates local environment configuration issues while ensuring consistent development experiences across team members.

When to use this guide:

- You need a new development environment
- Your current setup has configuration issues
- You prefer cloud-based development with scalable resources
- You want a clean, isolated environment for this project

What you'll achieve:  
A hybrid development setup where your code runs on a pre-configured Amazon Linux EC2 instance while you work through VS Code on your local machine, providing both performance and consistency.

# Step 1: Launch EC2 Instance

## 1.1 Navigate to EC2 Console

1. Log into [AWS Management Console](https://console.aws.amazon.com/)
2. Navigate to EC2 service
3. Click Launch Instance

## 1.2 Configure Instance Settings

Name: genai-idp-dev-environment(example)  
AMI Selection (choose one):  
**Option 1: Amazon Linux 2023**
**Option 2: Ubuntu 24.04**

Instance Type:

- Heavy development: t3.2xlarge (8 vCPU, 32 GB RAM)(recommended)  
  (Other instance types will also work, but this is one we tested)

## 1.3 Key Pair Setup

1. Click Create new key pair (or select existing)
2. Name: genai-idp-dev-key (example)
3. Type: RSA
4. Format: .pem
5. Download and save the .pem file securely

## 1.4 Network Settings

Security Group Configuration:

1. Create new security group
2. Add these inbound rules:

- **SSH**: Port 22, Source: My IP

## 1.5 Storage Configuration

- Size: 720 GiB
- Type: gp3
- Delete on termination: Yes

## 1.6 Launch

Click Launch instance and wait for it to reach "Running" state.

# Step 2: Connect to Your Instance

## Get Connection Info

1. Select your instance in EC2 console
2. Note the Public IPv4 address

## SSH Connection Command

On local machine:

### CMD Terminal

ssh -i /path/to/genai-idp-dev-key.pem ec2-user@YOUR_INSTANCE_IP

### To install git run this command on EC2 Instance

**For Amazon Linux 2023:**
```bash
sudo dnf install git -y
```

**For Ubuntu 24.04:**
```bash
sudo apt update && sudo apt install git -y
```

### Clone Repository

git clone https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws

### Go to directory

cd accelerated-intelligent-document-processing-on-aws/

### Run the setup script for development tools which is scripts directory

cd scripts  

**For Amazon Linux 2023:**
```bash
chmod +x ./dev_setup_al2023.sh
./dev_setup_al2023.sh
```

**For Ubuntu 24.04:**
```bash
chmod +x ./dev_setup_ubuntu.sh
./dev_setup_ubuntu.sh
```

### To upgrade the python version run this command on EC2 instance

**For Amazon Linux 2023:**
```bash
source /home/ec2-user/miniconda/bin/activate base
```

**For Ubuntu 24.04:**
```bash
source /home/ubuntu/miniconda/bin/activate base
```

# Step 4: Install Visual Studio Code on Local Machine

## 4.1 Visit the official website: Go to [https://code.visualstudio.com/](https://code.visualstudio.com/)

Download: Click the "Download for Windows" button

- This will download the User Installer (recommended for most users)
- File name will be something like VSCodeUserSetup-x64-1.x.x.exe

Install:

- Run the downloaded installer
- Choose installation location (default is recommended)

Launch: Click "Launch Visual Studio Code" when installation completes  
Install Remote - SSH extension (by Microsoft)

## 4.2 Connect via VSCode: Update your SSH config

To open "SSH Config" go to "Remote Explorer" option on left bar  
You can open and edit "SSH config" file by settings option on "SSH" Tab

**For Amazon Linux 2023:**
```
Host genai-idp-dev
    HostName YOUR_INSTANCE_PUBLIC_IP
    User ec2-user
    IdentityFile /path/to/genai-idp-dev-key.pem
    Port 22
```

**For Ubuntu 24.04:**
```
Host genai-idp-dev-ubuntu
    HostName YOUR_INSTANCE_PUBLIC_IP
    User ubuntu
    IdentityFile /path/to/genai-idp-dev-key.pem
    Port 22
```

# 4.3 Connect via VSCode

1. Press Ctrl+Shift+P
2. Type "Remote-SSH: Connect to Host"
3. Select "genai-idp-dev" (Amazon Linux) or "genai-idp-dev-ubuntu" (Ubuntu)
4. Open folder: 
   - Amazon Linux: `/home/ec2-user/accelerated-intelligent-document-processing-on-aws`
   - Ubuntu: `/home/ubuntu/accelerated-intelligent-document-processing-on-aws`

# Step 5: AWS Configure

### Refer this link for AWS configure

https://docs.aws.amazon.com/cli/latest/userguide/getting-started-quickstart.html

# Step 6: Install the IDP CLI

## Install the CLI and Dependencies

```bash
make setup-venv
source .venv/bin/activate
```

> **Note**: This installs the `idp-cli` command along with all required Python dependencies.

# Step 7: Test the Build

## Using `idp-cli publish`

### Test CLI help

```bash
idp-cli publish --help
```

### Test build

Standard build and publish:
```bash
idp-cli publish --source-dir . --region us-east-1
```

The container-packaged Lambda functions are built in CodeBuild during stack
deployment, not on this machine, so publishing needs no local Docker daemon and no
ECR permissions of your own. Docker is needed here only for local Lambda testing
with `sam local invoke`.

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

# Step 7: Cline & Q Developer installation

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

