#!/usr/bin/env python3
"""Execute a single notebook end-to-end with papermill, live.

Companion to _validate_notebooks.py (which only does static import/symbol
checks). This actually runs a notebook against live Bedrock/Textract/S3 so
output cells and runtime behavior can be verified.

idp_common is already installed editable in the kernel env, so only the
`%pip uninstall idp_common` / `%pip install -e ...idp_common...` cells (which
would clobber the shared editable install and are slow) plus `%load_ext
autoreload` / `%autoreload` magics are neutralized before execution. Genuine
third-party installs (e.g. seaborn) are left intact.

Executes against the `idp313` Jupyter kernel (miniconda python with idp_common
installed). Executed/neutralized copies are written under /tmp/nb_runs so the
repo tree stays clean.

Usage: python3 _run_nb.py <notebook.ipynb> [output.ipynb]
Exit 0 on success, 1 on execution error. Prints a compact error summary.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import papermill as pm


def neutralize_pip(nb_path: Path, tmp_in: Path) -> None:
    """Copy notebook to tmp_in with %pip / pip-magic lines commented out."""
    data = json.loads(nb_path.read_text())
    for cell in data.get("cells", []):
        if cell.get("cell_type") != "code":
            continue
        new_src = []
        for line in cell.get("source", []):
            stripped = line.lstrip()
            is_pip = re.match(r"^[%!]\s*pip\b", stripped)
            # Only neutralize pip lines that (un)install idp_common itself — it is
            # already installed editable in the kernel, and re-running the
            # uninstall/install-e churns/clobbers the shared env. Genuine
            # third-party installs (e.g. seaborn) are left intact so notebooks
            # that declare their own deps still work.
            touches_idp = is_pip and ("idp_common" in line or "idp_common_pkg" in line)
            is_magic = re.match(r"^%(load_ext|autoreload)\b", stripped)
            if touches_idp or is_magic:
                new_src.append("# [neutralized-for-run] " + line)
            else:
                new_src.append(line)
        cell["source"] = new_src
    tmp_in.write_text(json.dumps(data))


def main() -> int:
    nb_path = Path(sys.argv[1]).resolve()
    run_dir = Path("/tmp/nb_runs")  # nosec B108 - developer-local notebook scratch dir, created with default perms
    run_dir.mkdir(parents=True, exist_ok=True)
    stem = nb_path.name.replace(".ipynb", "")
    if len(sys.argv) > 2:
        out_path = Path(sys.argv[2]).resolve()
    else:
        out_path = run_dir / f"{stem}.executed.ipynb"
    tmp_in = run_dir / f"{stem}.neutralized.ipynb"
    neutralize_pip(nb_path, tmp_in)

    cwd = str(nb_path.parent)
    try:
        pm.execute_notebook(
            str(tmp_in),
            str(out_path),
            kernel_name="idp313",
            cwd=cwd,
            progress_bar=False,
            log_output=True,
            stdout_file=sys.stdout,
            stderr_file=sys.stderr,
        )
    except pm.PapermillExecutionError as e:
        print(f"\n=== EXECUTION ERROR in {nb_path.name} ===")
        print(f"cell index (executed): {e.cell_index}")
        print(f"{e.ename}: {e.evalue}")
        tb = "\n".join(e.traceback) if e.traceback else ""
        print(tb[-3000:])
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"\n=== HARNESS ERROR in {nb_path.name} ===")
        print(f"{type(e).__name__}: {e}")
        return 1
    finally:
        if tmp_in.exists():
            tmp_in.unlink()
    print(f"\n=== OK: {nb_path.name} executed cleanly ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
