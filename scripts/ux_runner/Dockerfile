# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# The MR UX test's toolchain as an image, built from the same install.sh the
# ai_ux_test CI job runs. Build context is scripts/ux_runner/, not the repo root,
# whose .dockerignore drops *.md and tests. scripts/ux_runner/run_in_docker.py
# builds it for the host's own arch. A shared multi-arch image:
#   docker buildx build --platform linux/amd64,linux/arm64 -t <repo>/idp-ux-runner:<tag> scripts/ux_runner
# checkov:skip=CKV_DOCKER_2: "a one-shot test runner, not a service; there is nothing to health-check"
# checkov:skip=CKV_DOCKER_3: "the harness must start as root to run Chrome and the egress proxy as the unprivileged uxbrowser and uxproxy users; nothing else runs in the image"
FROM public.ecr.aws/docker/library/python:3.13-bookworm
COPY install.sh package.json package-lock.json requirements.txt chrome_policy.json /tmp/ux-runner/
RUN bash /tmp/ux-runner/install.sh && rm -rf /tmp/ux-runner /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 UX_RUNNER_PREFIX=/opt/ux-runner LANG=C.UTF-8 TZ=UTC
WORKDIR /work
