// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

/**
 * Central definition of the public GitHub repository used for feedback and
 * issue reporting from the web UI. Previously the owner/repo slug and derived
 * URLs were repeated as string literals across several components (nav links,
 * tools panels, capacity-planning "Beta Feedback" alert). Import from here
 * instead so there is a single source of truth.
 *
 * Note: the canonical source lives in GitLab and is mirrored to this public
 * GitHub repo. User-facing feedback/issues are logged on GitHub (public).
 */
export const GITHUB_OWNER_REPO = 'aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws';

export const GITHUB_REPO_URL = `https://github.com/${GITHUB_OWNER_REPO}`;

/** Issues list. */
export const GITHUB_ISSUES_URL = `${GITHUB_REPO_URL}/issues`;

/**
 * New-issue endpoint. App-generated links append `?template=<file>.yml` plus
 * per-field values keyed by the form's element ids, so a report filed from the
 * UI lands in the same issue form — and picks up the same labels and required
 * fields — as one filed from GitHub's "New issue" chooser. See
 * utils/github-feedback.ts for the field mapping and the `?body=` exclusivity
 * rule that makes it necessary.
 */
export const GITHUB_NEW_ISSUE_URL = `${GITHUB_ISSUES_URL}/new`;

/** Published documentation site (GitHub Pages). */
export const DOCS_BASE_URL = `https://aws-solutions-library-samples.github.io/accelerated-intelligent-document-processing-on-aws`;
