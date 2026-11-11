"""``moved`` blocks that keep a resource's state when its address changes.

A Terraform address is ``[module.<nested stack>.]<type>.<snake logical id>``.
Two template changes move a resource to a new address without changing what it
is: a nested stack moving or being renamed, and a resource moving between
nested stacks. A logical-id rename changes the address too. Without a ``moved``
block Terraform plans each of these as a delete and a create, which for a
bucket, a table or a user pool loses its data (``idp-cli deploy --terraform``
refuses such a plan; ``statefulAddresses``).

The translator records, in ``idp-terraform.json``, every resource's identity:
its nested-stack path, logical id and type, and every Terraform address it
became (a bucket becomes the bucket and one resource per configuration block).
Given the previous release's manifest it pairs each previous resource with its
current one and writes ``moved.tf``:

- by identity: the same nested-stack path and logical id, whose addresses
  differ only because a nested stack's address changed;
- through ``renames.yaml``: a declared rename of a logical id or a nested-stack
  path, which cannot be inferred;
- by inference: a logical id and type that left one nested stack and appear in
  exactly one other, and nowhere else on either side.

A resource whose Terraform type changed cannot be moved (the state formats
differ) and is reported instead; the plan guard is what stops it. Moves are
kept in the manifest and written again by every later release, so a deployment
that skips releases still follows every move.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RENAMES_FILE = Path(__file__).resolve().parents[1] / "renames.yaml"


@dataclass
class MoveResult:
    moves: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def identity(entry: dict[str, Any]) -> str:
    return (
        f"{entry['stack']}/{entry['logicalId']}"
        if entry["stack"]
        else entry["logicalId"]
    )


def load_renames(path: Path = RENAMES_FILE) -> dict[str, dict[str, str]]:
    """``{old identity: entry}`` from the rename table."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    out: dict[str, dict[str, str]] = {}
    for entry in data.get("renames") or []:
        missing = {"from", "to", "reason"} - set(entry)
        if missing:
            raise ValueError(f"{path}: rename entry {entry} lacks {sorted(missing)}")
        if entry["from"] in out:
            raise ValueError(f"{path}: {entry['from']} is renamed twice")
        out[entry["from"]] = entry
    return out


def _split(address: str) -> tuple[str, str, str]:
    """``module.a.aws_x.name`` -> ("module.a.", "aws_x", "name"); a module
    address ``module.a.module.b`` splits as ("module.a.", "module", "b")."""
    parts = address.split(".")
    prefix = ".".join(parts[:-2])
    return (prefix + "." if prefix else ""), parts[-2], parts[-1]


def _renamed(old_id: str, renames: dict[str, dict[str, str]]) -> str | None:
    """The identity a rename table gives ``old_id``: an exact entry, or the
    longest renamed nested-stack path it is under."""
    if old_id in renames:
        return renames[old_id]["to"]
    parts = old_id.split("/")
    for n in range(len(parts) - 1, 0, -1):
        stack = "/".join(parts[:n])
        if stack in renames:
            return "/".join([renames[stack]["to"], *parts[n:]])
    return None


def _counterpart(old: str, old_entry: dict, new_entry: dict) -> str | None:
    """The current address a previous companion address maps to."""
    o_prefix, o_type, o_label = _split(old)
    o_name = _split(old_entry["address"])[2]
    n_name = _split(new_entry["address"])[2]
    label = n_name + o_label[len(o_name) :] if o_label.startswith(o_name) else o_label
    for candidate in new_entry["addresses"]:
        _, c_type, c_label = _split(candidate)
        if c_type == o_type and c_label == label:
            return candidate
    return None


def plan_moves(
    previous: dict[str, Any],
    current: list[dict[str, Any]],
    renames: dict[str, dict[str, str]],
) -> MoveResult:
    """Moves from ``previous`` (a manifest) to ``current`` (resource entries)."""
    result = MoveResult()
    cur_by_id = {identity(e): e for e in current}
    live = {a for e in current for a in e["addresses"]}
    for old_id, entry in renames.items():
        if entry["to"] not in cur_by_id:
            result.errors.append(
                f"renames.yaml: {old_id} -> {entry['to']}, but the template has no "
                f"{entry['to']}"
            )
        if old_id in cur_by_id:
            result.errors.append(
                f"renames.yaml: {old_id} is renamed, but the template still has it"
            )
    prev_entries = previous.get("resources") or []
    prev_ids = {identity(e) for e in prev_entries}

    # Logical ids that appear exactly once on each side, for inferring a move
    # between nested stacks.
    def unique(entries, ids_elsewhere):
        seen: dict[tuple[str, str], list[dict]] = {}
        for e in entries:
            if identity(e) not in ids_elsewhere:
                seen.setdefault((e["logicalId"], e["cfnType"]), []).append(e)
        return {k: v[0] for k, v in seen.items() if len(v) == 1}

    arrivals = unique(current, prev_ids)
    pairs: list[tuple[dict, dict, str]] = []
    for old in prev_entries:
        old_id = identity(old)
        renamed = _renamed(old_id, renames)
        if renamed is not None:
            new = cur_by_id.get(renamed)
            how = "renames.yaml"
        elif old_id in cur_by_id:
            new, how = cur_by_id[old_id], "same identity"
        else:
            new = arrivals.get((old["logicalId"], old["cfnType"]))
            how = "moved between nested stacks"
        if new is None:
            continue
        if new["cfnType"] != old["cfnType"]:
            result.warnings.append(
                f"{old_id} ({old['cfnType']}) is now {identity(new)} "
                f"({new['cfnType']}): a different type, not moved"
            )
            continue
        pairs.append((old, new, how))
    # Whole modules first: moving a module moves everything under it.
    module_moves: list[tuple[str, str]] = []
    for old, new, _ in pairs:
        if (
            old["cfnType"] == "AWS::CloudFormation::Stack"
            and old["address"] != new["address"]
        ):
            if old["address"] not in live:
                module_moves.append((old["address"], new["address"]))

    def after_module_moves(address: str) -> str:
        for src, dst in module_moves:
            if address.startswith(src + "."):
                return dst + address[len(src) :]
        return address

    result.moves.extend(module_moves)
    for old, new, how in pairs:
        if old["cfnType"] == "AWS::CloudFormation::Stack":
            continue
        for old_addr in old["addresses"]:
            src = after_module_moves(old_addr)
            dst = _counterpart(old_addr, old, new)
            if dst is None:
                if old_addr not in live:
                    result.warnings.append(
                        f"{identity(old)}: {old_addr} has no counterpart in "
                        f"{identity(new)} ({how}); it will be destroyed"
                    )
                continue
            if src == dst or src in live:
                continue
            result.moves.append((src, dst))
    return result


def carry_history(
    previous: dict[str, Any], new_moves: list[tuple[str, str]], live: set[str]
) -> tuple[list[tuple[str, str]], list[str]]:
    """Every earlier release's moves plus this one's, without any whose source is
    a live address again (Terraform refuses those) or whose chain ends nowhere."""
    warnings = []
    history = [(m["from"], m["to"]) for m in previous.get("moves") or []]
    out: list[tuple[str, str]] = []
    for move in history + new_moves:
        if move in out:
            continue
        out.append(move)
    targets = {src: dst for src, dst in out}

    def end(address: str) -> str:
        seen = set()
        while address in targets and address not in seen:
            seen.add(address)
            address = targets[address]
        return address

    def exists(address: str) -> bool:
        return address in live or any(a.startswith(address + ".") for a in live)

    kept = []
    for src, dst in out:
        if exists(src):
            warnings.append(f"moved {src} -> {dst} dropped: {src} exists again")
            continue
        final = end(dst)
        if not exists(final):
            warnings.append(f"moved {src} -> {dst} dropped: {final} is gone")
            continue
        kept.append((src, dst))
    return kept, warnings


def moved_tf(moves: list[tuple[str, str]]) -> str:
    header = (
        "# Generated by the translator from the previous release's manifest and\n"
        "# terraform/translator/renames.yaml: each block keeps the state of a\n"
        "# resource whose address changed, so Terraform does not recreate it.\n"
    )
    blocks = [f"moved {{\n  from = {src}\n  to   = {dst}\n}}\n" for src, dst in moves]
    return header + "\n" + "\n".join(blocks)
