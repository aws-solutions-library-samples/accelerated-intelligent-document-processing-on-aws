"""Deterministic zip archives the translator ships inside a module.

A zip built at plan time by an ``archive_file`` data source is unknown at plan
time whenever its module has ``depends_on`` (Terraform defers every data source
in such a module), so the function using it would show a change on every plan.
Building the zip here, with fixed timestamps and permissions, makes the archive
and its hash part of the generated module instead.
"""

from __future__ import annotations

import io
import zipfile

_EPOCH = (1980, 1, 1, 0, 0, 0)


def deterministic_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, files[name])
    return buf.getvalue()
