# Lint and gate rules

Applies to every change. It lists the local gates to run, how lint debt and gate
exemptions are handled, and how CI parity is kept.

## Must
- Before every commit, run `make lint` (or `make fastlint`), `make test` and
  `make typecheck`. Before opening a PR, also run `make srt-scan` and read its
  findings table instead of trusting the exit code.
- Run `make dep-audit` whenever dependencies change.
- Use `make lint-cicd` when you need the CI-equivalent run. A UI-lint checksum hit
  is a skip, not a pass.
- To find out whether a file is linted, run
  `python3 scripts/check_lint_debt.py --explain <path>`. Ruff's own probes
  misreport excluded files.
- To pay down lint debt, fix the file and run `scripts/check_lint_debt.py --write`.
  For formatting debt, name the path explicitly in `ruff format <path>`.
- Register every gate exemption in `scripts/tests/gate_exemptions.json`, and
  for each entry:
  - give one member's worth of reason
  - compute the premise where possible (otherwise write `JUDGEMENT`)
  - add a ratchet, or a stated `ratchetGap`
  - name the exemption constant with a word from the exemption vocabulary
- When you fix an SRT finding in source, delete its entry in
  `scripts/srt/issues.json` too. Give every suppression and every dep-audit
  allowlist entry a specific justification.
- Treat the whole-tree `make typecheck` as the type gate.
  `make typecheck-pr` is a local convenience only.
- Keep the GitHub and GitLab gate sets identical when you add or move a gate.
- Before you merge a change on the deploy path, start the manual
  `integration_tests` job.
- Treat a red gate as blocking even though branch protection isn't enforced.

## Must not
- Hand-edit the exclusion arrays in `ruff.toml`, or grow lint debt without
  `--allow-new-debt "<reason>"`.
- Exempt a directory when a file will do, or a file when a line will do.
- Attach one justification to a whole set of members.
- Write counts of files, gates, tests or other items into prose. Describe how to
  derive the number instead; `scripts/tests/test_documented_counts.py` enforces
  this.
- Unpin the `cfn-lint` version, or add a gate to one CI only.
- Put `[skip ci]` (or any equivalent) in a commit message, or skip hooks with
  `--no-verify`.

## Verify with
- `make lint-cicd` and `make typecheck`
- `make test-hooks`
- `scripts/tests/test_gate_exemption_registry.py`,
  `scripts/tests/test_ci_gate_parity.py`, `scripts/tests/test_documented_counts.py`

## Sources
- [CLAUDE.md](../../CLAUDE.md): lint coverage, cfn-lint and the link checker, the
  gate exemption registry, and CI parity
- [.claude/skills/code-review.md](../skills/code-review.md): pre-submit checks
- [.claude/skills/srt-security-scan.md](../skills/srt-security-scan.md): SRT
  triage and suppression
- [.claude/skills/testing-qa.md](../skills/testing-qa.md): how to write a gate
  exemption
