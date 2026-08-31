---
title: "Configuration Versions (renamed)"
---

Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
SPDX-License-Identifier: MIT-0

# Configuration Versions → Configuration Profiles

This page has moved to **[Configuration Profiles](configuration-profiles.md)**.

"Configuration version" was one term doing two jobs: the *named configuration* you
select and control access to, and — once history was added — a *snapshot in time* of
one of those. The vocabulary is now split:

| Term | Meaning |
|---|---|
| **Configuration Profile** | The named entity: `default`, `Production`, `lending`. The access-control unit, the document partition, and what you activate. |
| **Revision** (`r7`) | An immutable numbered snapshot of one profile's configuration, cut on every save. |

API and stored field names are unchanged for compatibility —
`getConfigVersions`, `versionName`, `ConfigVersion`, and `allowedConfigVersions`
all still refer to **profiles**. The CLI and SDK accept `--config-profile` /
`config_profile=` as well as `--config-version` / `config_version=`; both set the
same value, and the older spelling is kept for backward compatibility.

This stub remains so existing links and bookmarks do not break.
