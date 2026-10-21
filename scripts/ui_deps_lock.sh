#!/bin/sh
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Run one shell command from src/ui while holding an exclusive lock on this
# checkout's src/ui/node_modules.
#
# WHY. Six make targets reinstall src/ui/node_modules with `npm ci`, and `npm ci`
# deletes the tree before rebuilding it — by design. Every one of those targets
# then runs a node tool out of that same tree. Two of them at once in ONE checkout
# therefore corrupt each other, and the failure does not look like contention: the
# lane that loses reports a missing or half-removed module from whatever package
# the other lane happened to be unpacking. Measured once as three red gates in
# three lanes with three unrelated-looking errors — a missing esbuild/install.js,
# a missing eslint formatter, and `ENOTEMPTY ... rmdir .../argparse` — all three
# green on an uncontended re-run (issue #1447).
#
# `make lint-cicd` performs three of those targets itself (ui-lint, ui-build-only,
# codegen-check), so `lint-cicd` plus any ONE other UI target is enough to collide.
# "Lint in one lane, UI tests in another" is the realistic composition, and it
# looks like two unrelated things.
#
# The lock covers the whole command rather than just the `npm ci`, because the
# reader half is equally exposed: a wipe-and-rebuild under a running eslint is how
# the missing-formatter failure above happened, and that lane's own install had
# already finished.
#
# WHAT IT COSTS. One `flock` exec per target when nothing is contending, and no
# output. A concurrent second target waits instead of racing, so a deliberately
# parallel UI run is serialised — which is the point, and is slower than the
# corrupting version by exactly the time it does not spend corrupting.
#
# STALE LOCKS CANNOT HAPPEN. flock(2) is held on an open file descriptor and the
# kernel drops it when the process exits, however it exits. A killed or
# Ctrl-C'd make leaves the lock FILE behind — zero bytes, no state — and the next
# run locks it again with no recovery step. Nothing here ever reads the file's
# contents, and it is opened for append rather than truncation so that a waiter
# cannot disturb a holder.
#
# IT CANNOT WEDGE A CI JOB. Neither CI can contend at all: GitLab splits these
# gates across four jobs and GitHub across four, each a separate runner with a
# separate checkout, so the lock is always free there and is taken without waiting.
# Even so the wait is bounded (UI_DEPS_LOCK_WAIT_SECONDS, default 900) and the
# bound is an error rather than a fall-through: on timeout nothing runs and the
# exit status is 1, because running anyway is the corruption this exists to stop.
#
# IT CANNOT DEADLOCK AGAINST ITSELF. `lint-cicd` does not hold the lock; its three
# sub-makes take and release it one at a time. Nesting is nonetheless made safe
# rather than merely avoided: the holder exports UI_DEPS_LOCK_HELD, and a nested
# invocation that sees it runs the command directly instead of waiting on a lock
# its own ancestor holds.
#
# Usage:  scripts/ui_deps_lock.sh '<shell command, run with src/ui as cwd>'

set -u

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
    echo "usage: $0 '<shell command, run with src/ui as cwd>'" >&2
    exit 2
fi
UI_COMMAND="$1"

REPO_ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
LOCK_PATH="$REPO_ROOT/.ui-deps.lock"
WAIT_SECONDS="${UI_DEPS_LOCK_WAIT_SECONDS:-900}"

run_ui_command() {
    UI_DEPS_LOCK_HELD=1
    export UI_DEPS_LOCK_HELD
    cd "$REPO_ROOT/src/ui" || exit 1
    eval "$UI_COMMAND"
}

# An ancestor already holds it. Waiting here would be a deadlock against our own
# process tree, so run directly — the tree is already protected.
if [ -n "${UI_DEPS_LOCK_HELD:-}" ]; then
    run_ui_command
    exit $?
fi

# Fail open, loudly, on the two conditions that make locking impossible: no
# flock(1) (macOS does not ship one), or a checkout we cannot write a lock file
# into. A guard that refuses to run the gate buys nothing; one that runs it
# without saying the guard is off is how this hazard stayed invisible.
if ! command -v flock >/dev/null 2>&1 || ! touch "$LOCK_PATH" 2>/dev/null; then
    echo "NOTE: cannot lock src/ui/node_modules (no flock(1), or $LOCK_PATH is not writable)." >&2
    echo "      Running anyway. Do NOT run another src/ui make target concurrently" >&2
    echo "      in this checkout — see issue #1447." >&2
    run_ui_command
    exit $?
fi

exec 9>>"$LOCK_PATH"

# Non-blocking first so the uncontended case stays silent, then a bounded wait
# with an explanation. Without the split, every serial run would print a line
# about a hazard it is not in.
if ! flock -n 9; then
    echo "src/ui/node_modules is in use by another make target in this checkout." >&2
    echo "Waiting up to ${WAIT_SECONDS}s for it (issue #1447)." >&2
    if ! flock -w "$WAIT_SECONDS" 9; then
        echo "ERROR: gave up after ${WAIT_SECONDS}s waiting for src/ui/node_modules." >&2
        echo "       NOTHING RAN; this target made no change and reports failure." >&2
        echo "       Another make target in this checkout is still holding" >&2
        echo "       $LOCK_PATH. Let it finish and re-run, or raise the bound with" >&2
        echo "       UI_DEPS_LOCK_WAIT_SECONDS=<seconds>." >&2
        exit 1
    fi
fi

run_ui_command
