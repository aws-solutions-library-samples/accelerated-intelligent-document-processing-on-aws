"""Command line: translate one packaged CloudFormation template to a module."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .awscc_schema import AwsccSchema
from .expand import expand, load_template
from .static_files import VERSIONS_TF, bridge_tf, label_tf
from .translate import TranslationError, Translator, report_json

BRIDGE_SRC = Path(__file__).resolve().parents[2] / "bridge" / "index.py"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="idp-tf")
    ap.add_argument("template", type=Path, help="packaged CloudFormation template")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--awscc-schema", type=Path, required=True)
    ap.add_argument("--mode", choices=["hybrid", "awscc"], default="hybrid")
    ap.add_argument(
        "--label-template",
        type=Path,
        help="published main template whose Description labels the deployment",
    )
    ap.add_argument("--version", help="published version for the label stack")
    ap.add_argument(
        "--survey",
        action="store_true",
        help="collect every failure and print them instead of stopping",
    )
    args = ap.parse_args(argv)

    expanded = expand(load_template(args.template))
    try:
        tr = Translator(
            expanded, AwsccSchema(args.awscc_schema), mode=args.mode, survey=args.survey
        )
        files = tr.render()
        if args.survey:
            for e in tr.errors:
                print(e)
            print(f"{len(tr.errors)} failure(s); {tr.report.resource_kinds}")
            return 1 if tr.errors else 0
    except TranslationError as exc:
        print(f"translation failed: {exc}", file=sys.stderr)
        return 1
    files["versions.tf"] = VERSIONS_TF
    if tr.custom_tokens:
        files["bridge.tf"] = bridge_tf(tr.custom_tokens)
    if args.label_template:
        if not args.version:
            ap.error("--label-template needs --version")
        desc = load_template(args.label_template)["Description"]
        files["label.tf"] = label_tf(desc, args.version)

    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.out / name).write_text(text)
    if tr.custom_tokens:
        (args.out / "bridge").mkdir(exist_ok=True)
        shutil.copy(BRIDGE_SRC, args.out / "bridge" / "index.py")
    (args.out / "translation-report.json").write_text(report_json(tr))
    print(f"wrote {len(files)} files to {args.out}: {tr.report.resource_kinds}")
    for w in tr.report.warnings:
        print(f"warning: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
