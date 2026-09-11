# SAM `BuildMethod: makefile` target for the PII Anonymizer preprocessing hook.
#
# Why a makefile instead of letting SAM's PythonPipBuilder handle it: that
# builder asks pip for exactly ONE platform tag — `--platform
# manylinux2014_aarch64` — and Pillow stopped publishing aarch64 wheels under
# that tag at 12.3.0 (12.2.0 is the last one); 12.3.0 ships
# `manylinux_2_27_aarch64`/`manylinux_2_28_aarch64` only. Since this feature's
# functions are arm64 (see Globals in ../template.yaml) and the build host is
# usually x86_64, PythonPipBuilder fails with
# `PythonPipBuilder:ResolveDependencies - {pillow==12.3.0(wheel)}`.
#
# Staying on 12.2.0 was the previous workaround, but that leaves 9 HIGH Pillow
# advisories unfixed (heap out-of-bounds writes in Image.paste/crop and
# ImageCmsTransform, decompression-bomb DoS in the PDF/font parsers, ...) in the
# one component that renders UNTRUSTED customer PDFs — the worst place to carry
# them. So instead of downgrading, name the platform tags explicitly.
#
# BOTH tags are passed, and that is the point: this dependency set straddles the
# manylinux generations. Pillow 12.3.0 ships manylinux_2_27/2_28_aarch64 only,
# while pypdfium2 still ships manylinux2014_aarch64 (= manylinux_2_17) only — so
# either tag ALONE fails on one of them. pip accepts --platform repeatedly and
# takes a wheel matching ANY listed tag, which is exactly the semantics needed
# here, and is precisely what SAM's single-tag builder cannot express. Naming
# 2.28 is correct for the runtime: python3.12 Lambda runs on AL2023 (glibc
# 2.34), well past that floor.
#
# Same pattern and rationale as feature-platform/confbench-testset/shared/Makefile
# (which hit this with pyarrow). Keep the two in sync if the approach changes.
#
# ARTIFACTS_DIR is supplied by SAM and becomes the function's deployment package,
# so hand-authored source must be copied in as well as the dependencies —
# PythonPipBuilder used to do the copying for us.
build-PiiAnonymizerHookFunction:
	cp handler.py "$(ARTIFACTS_DIR)/"
	cp -r vendor "$(ARTIFACTS_DIR)/"
	# Drop any __pycache__ the local checkout picked up — those .pyc files are
	# built for the developer's interpreter/arch, not the arm64 Lambda runtime,
	# so shipping them is dead weight at best.
	find "$(ARTIFACTS_DIR)/vendor" -name __pycache__ -type d -prune -exec rm -rf {} +
	python3 -m pip install \
		--requirement requirements.txt \
		--target "$(ARTIFACTS_DIR)" \
		--platform manylinux2014_aarch64 \
		--platform manylinux_2_28_aarch64 \
		--python-version 3.12 \
		--implementation cp \
		--only-binary=:all: \
		--no-compile \
		--upgrade
