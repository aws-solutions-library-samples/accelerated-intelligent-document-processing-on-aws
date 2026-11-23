# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Test Set Generator's AgentCore image installs what its README describes.

``feature-platform/idp-data-generator/Dockerfile`` builds the runtime that runs
SEED. Its README records SEED's per-agent tool inventory for one release, and the
synthesis adapter's render policy needs an ``xhtml2pdf`` that has
``ResourceAccessPolicy``. These tests read the Dockerfile as text, because the
image itself is built only at install time by CodeBuild.
"""

import ast
import json
import re
import shlex
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
EXT = ROOT / "feature-platform" / "idp-data-generator"
DOCKERFILE = EXT / "Dockerfile"
README = EXT / "README.md"
RUNTIME_DIR = EXT / "agent-source" / "runtime"

# xhtml2pdf releases read for the render policy: 0.2.27 and 0.2.28 ship
# xhtml2pdf/config/resources.py with ResourceAccessPolicy, 0.2.17 does not.
FIRST_XHTML2PDF_WITH_POLICY = (0, 2, 27)


def _instructions():
    """Dockerfile instructions with comments dropped and continuations joined."""
    text = re.sub(r"\\\n", " ", DOCKERFILE.read_text(encoding="utf-8"))
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        keyword, _, rest = line.partition(" ")
        out.append((keyword.upper(), rest.strip()))
    return out


def _run_words():
    """Every shell word of every RUN instruction."""
    words = []
    for keyword, rest in _instructions():
        if keyword == "RUN":
            words.extend(shlex.split(rest))
    return words


def _normalise(word):
    """pip's name normalisation: ``seed_data``, ``Seed.Data`` and ``seed-data`` match."""
    return re.sub(r"[-_.]+", "-", word).lower()


def _pin(name):
    """The one exact ``name[extras]==version`` in any RUN, with no other mention.

    Matching words rather than ``pip install`` commands covers ``pip3``,
    ``python -m pip``, ``uv pip`` and a later ``pip install -U <name>``: any RUN
    word naming the package other than the single exact pin fails here.
    """
    pattern = re.compile(rf"^{re.escape(name)}(\[[^\]]*\])?==([\w-]+)$")
    words = [_normalise(w) for w in _run_words()]
    mentions = [w for w in words if re.match(rf"^{re.escape(name)}(?![a-z0-9])", w)]
    pins = [m for w in mentions if (m := pattern.match(w))]
    assert len(pins) == 1 and len(mentions) == 1, mentions
    return pins[0].group(1) or "", pins[0].group(2).replace("-", ".")


def _final(keyword):
    values = [rest for k, rest in _instructions() if k == keyword]
    assert values, f"no {keyword} instruction"
    return values[-1]


def test_seed_is_pinned_exactly_with_the_augment_extra():
    extras, _ = _pin("seed-data")
    assert "augment" in extras.strip("[]").split(",")


def test_readme_inventory_names_the_pinned_seed_release():
    _, version = _pin("seed-data")
    assert f"`seed-data` **{version}**" in README.read_text(encoding="utf-8")


def test_xhtml2pdf_is_pinned_to_a_release_with_the_access_policy():
    _, version = _pin("xhtml2pdf")
    assert tuple(int(p) for p in version.split(".")) >= FIRST_XHTML2PDF_WITH_POLICY


def test_app_directory_is_not_handed_to_the_runtime_user():
    """A denylist of the usual ways to give the runtime user write access.

    It catches chown/chmod/setfacl, coreutils ``install`` as a command and
    ``COPY``/``ADD --chown``; it does not parse every shell construct (a
    ``mkdir -m 777`` or a heredoc ``RUN``, for example).
    """
    for keyword, rest in _instructions():
        if keyword == "RUN":
            # chown/chmod/setfacl anywhere, or coreutils `install` as a command
            # (`install -d -o appuser ...`); `pip install` and `apt-get install`
            # are arguments, not commands, so they do not match.
            assert not re.search(r"\b(chown|chmod|setfacl)\b", rest), rest
            assert not re.search(r"(^|&&|;|\|\|?)\s*install\s", rest), rest
        if keyword in ("COPY", "ADD"):
            assert "--chown" not in rest and "--chmod" not in rest, rest
    user = _final("USER")
    assert not re.match(r"^(0|root)\b", user), user


def test_runtime_starts_outside_app_in_the_users_home():
    """augraphy creates ``augraphy_cache`` in the working directory on each call."""
    user = _final("USER")
    assert _final("WORKDIR") == f"/home/{user}"
    assert not [rest for k, rest in _instructions() if k == "ENTRYPOINT"]
    assert json.loads(_final("CMD")) == ["python", "/app/handler.py"]


def test_script_directory_is_kept_off_the_import_path():
    envs = [rest for keyword, rest in _instructions() if keyword == "ENV"]
    values = [
        m.group(1) for e in envs for m in re.finditer(r"\bPYTHONSAFEPATH=(\S*)", e)
    ]
    assert values and values[-1] == "1", values


def test_runtime_entrypoint_imports_no_sibling_module():
    """PYTHONSAFEPATH=1 drops /app from sys.path, which only works while this holds."""
    siblings = {p.stem for p in RUNTIME_DIR.glob("*.py")}
    for path in RUNTIME_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0, (path.name, "relative import")
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not siblings.intersection(names), (path.name, names)


def test_runtime_requirements_do_not_restate_the_pinned_packages():
    """A second spec in requirements.txt would be installed after the pins."""
    req = (EXT / "agent-source" / "requirements.txt").read_text(encoding="utf-8")
    lines = [ln.split("#")[0].strip() for ln in req.splitlines()]
    for name in ("seed-data", "xhtml2pdf"):
        hits = [ln for ln in lines if re.match(rf"^{name}(?![a-z0-9])", _normalise(ln))]
        assert not hits, hits
